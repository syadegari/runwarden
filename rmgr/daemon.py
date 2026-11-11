"""A private Unix-socket API. One request and one response per connection."""

import asyncio
import fcntl
import json
import logging
import os
from pathlib import Path
import signal
import socket
import stat
import struct

from . import __version__
from .backend import SystemdBackend, gpu_capabilities
from .models import ACTIVE, Config, RmgrError, integer, prepare_home, require
from .scheduler import Scheduler
from .store import Store


MAX_REQUEST = 1024 * 1024
LOG = logging.getLogger(__name__)


class API:
    def __init__(self, scheduler: Scheduler, home: Path, capabilities: dict):
        self.scheduler = scheduler
        self.home = home
        # Older cached evidence may be reused when jobs survive a daemon upgrade.
        self.capabilities = dict(capabilities)
        self.capabilities.pop("gpu_device_access", None)
        self.capabilities.update(gpu_capabilities(scheduler.config))

    def dispatch(self, request: dict) -> dict:
        require(isinstance(request, dict), "Request must be an object")
        operation = request.get("op")
        store = self.scheduler.store
        if operation == "health":
            store.db.execute("SELECT 1").fetchone()
            jobs = store.all()
            error = self.scheduler.error
            try:
                self.scheduler.backend.ping()
            except RmgrError as exc:
                error = {"code": exc.code, "message": str(exc)}
            return {"status": "degraded" if error else "ready", "version": __version__,
                    "accepting_jobs": error is None, "error": error,
                    "running_jobs": sum(j["state"] in ACTIVE for j in jobs),
                    "queued_jobs": sum(j["state"] == "queued" for j in jobs),
                    "enforcement": self.capabilities}
        if operation == "resources":
            return self.scheduler.resources()
        if operation == "submit":
            # Already-accepted retries do not launch new work or require a
            # healthy backend; their token and exact specification still match.
            existing = store.retry(request.get("request_id"), request.get("spec"), request.get("control_token"))
            if existing is not None:
                return existing
            if self.scheduler.error:
                raise RmgrError("BACKEND_UNAVAILABLE", "Scheduler is degraded; retry with the same request_id")
            # An idle tick may not have touched systemd. Honor the readiness
            # reported by health even before the next attempted launch.
            self.scheduler.backend.ping()
            return self.scheduler.submit(request.get("request_id"), request.get("spec"), request.get("control_token"))
        if operation == "inspect":
            return store.get(request.get("job_id"))
        if operation == "cancel":
            return self.scheduler.cancel(request.get("job_id"), request.get("control_token"))
        if operation == "list":
            limit = integer(request.get("limit", 100), 1, "limit")
            offset = integer(request.get("offset", 0), 0, "offset")
            require(limit <= 100, "limit must be <= 100")
            jobs = store.all()
            state = request.get("state")
            if state:
                jobs = [job for job in jobs if job["state"] == state]
            return {"jobs": jobs[offset:offset + limit], "total": len(jobs),
                    "next_offset": offset + limit if offset + limit < len(jobs) else None}
        if operation == "logs":
            job = store.get(request.get("job_id"))
            stream = request.get("stream", "stdout")
            require(stream in {"stdout", "stderr"}, "stream must be stdout or stderr")
            offset = integer(request.get("offset", 0), 0, "offset")
            limit = integer(request.get("limit", 16384), 1, "limit")
            require(limit <= 1024 * 1024, "Log limit must be <= 1 MiB")
            path = Path(job[f"{stream}_path"])
            try:
                identity = None
                if not store.uses_legacy_outputs(job["id"]):
                    registration = self.home / "jobs" / job["id"] / "output-files.json"
                    identity = json.loads(registration.read_text())[stream]
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
                with os.fdopen(fd, "rb") as log:
                    info = os.fstat(log.fileno())
                    if not stat.S_ISREG(info.st_mode):
                        raise RmgrError("LOG_UNAVAILABLE", f"Output is not a regular file: {path}")
                    if identity is not None and (info.st_dev, info.st_ino) != (identity["device"], identity["inode"]):
                        raise RmgrError("LOG_UNAVAILABLE", f"Output file was replaced: {path}")
                    log.seek(offset)
                    data = log.read(limit)
                    more = bool(log.read(1))
                available = True
            except FileNotFoundError:
                data, more = b"", False
                available = False
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise RmgrError("LOG_UNAVAILABLE", f"Cannot read {path}: {exc}") from exc
            return {"job_id": job["id"], "stream": stream, "offset": offset,
                    "next_offset": offset + len(data), "more": more,
                    "text": data.decode("utf-8", errors="replace"), "state": job["state"],
                    "path": str(path), "available": available}
        raise RmgrError("INVALID_REQUEST", "Unknown operation")


async def connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, api: API) -> None:
    try:
        peer = writer.get_extra_info("socket")
        _, uid, _ = struct.unpack("3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        require(uid == os.getuid(), "Only the daemon's user may connect")
        line = await asyncio.wait_for(reader.readline(), timeout=5)
        require(bool(line) and len(line) <= MAX_REQUEST, "Missing or oversized request")
        request = json.loads(line)
        response = {"ok": True, "result": api.dispatch(request)}
    except RmgrError as exc:
        response = {"ok": False, "error": {"code": exc.code, "message": str(exc)}}
    except (ValueError, TypeError, asyncio.TimeoutError) as exc:
        response = {"ok": False, "error": {"code": "INVALID_REQUEST", "message": str(exc)}}
    except Exception:
        LOG.exception("API request failed")
        response = {"ok": False, "error": {"code": "INTERNAL_ERROR", "message": "See daemon journal"}}
    try:
        writer.write(json.dumps(response).encode() + b"\n")
        await asyncio.wait_for(writer.drain(), timeout=5)
    except (OSError, asyncio.TimeoutError):
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


async def serve(home: Path) -> None:
    prepare_home(home)
    lock = (home / "daemon.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock.close()
        raise RmgrError("ALREADY_RUNNING", "Another daemon owns this state directory") from exc
    path = home / "control.sock"
    store = None
    try:
        require(len(os.fsencode(path)) < 108, "State directory path is too long for a Unix socket")
        config = Config.load(home)
        backend = SystemdBackend(home)
        store = Store(home)
        scheduler = Scheduler(store, config, backend)
        scheduler.reconcile()
        evidence = home / "enforcement.json"
        if any(job["state"] in ACTIVE for job in store.all()):
            # A restart must not run a probe alongside an existing exclusive job.
            backend.ping()
            if not evidence.exists():
                raise RmgrError("RECOVERING", "Jobs are still running and enforcement evidence is missing; wait for them to finish")
            capabilities = json.loads(evidence.read_text())
        else:
            capabilities = backend.check(config)
            evidence.write_text(json.dumps(capabilities))
        api = API(scheduler, home, capabilities)
        path.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(lambda r, w: connection(r, w, api), path=path, limit=MAX_REQUEST)
        path.chmod(0o600)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, stop.set)
        LOG.info("Ready: %s", path)
        async with server:
            while not stop.is_set():
                scheduler.tick()
                if scheduler.error:
                    LOG.warning("Scheduler degraded: %s", scheduler.error)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass
    finally:
        # Separate job units remain alive; their allocations are recovered on restart.
        path.unlink(missing_ok=True)
        if store:
            store.close()
        lock.close()


def request(home: Path, payload: dict, timeout: float = 10) -> dict:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(home / "control.sock"))
            client.sendall(json.dumps(payload).encode() + b"\n")
            with client.makefile("rb") as response:
                line = response.readline(32 * MAX_REQUEST)
        reply = json.loads(line)
    except (OSError, ValueError) as exc:
        raise RmgrError("DAEMON_UNAVAILABLE", f"Cannot reach daemon: {exc}. Run rmgr service status / rmgr doctor") from exc
    if not reply.get("ok"):
        error = reply["error"]
        raise RmgrError(error["code"], error["message"])
    return reply["result"]
