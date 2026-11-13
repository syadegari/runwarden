"""A private Unix-socket API. One request and one response per connection."""

import asyncio
import fcntl
import json
import logging
import os
from pathlib import Path
import signal
import socket
import sqlite3
import stat
import struct
import time

from . import __version__
from .backend import SystemdBackend, gpu_capabilities
from .models import ACTIVE, TERMINAL, Config, JobSpec, RmgrError, integer, prepare_home, require
from .protocol import MAX_LOG_BYTES, MAX_SPEC_BYTES, runtime_capabilities
from .runner import environment
from .snapshots import Snapshots
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
        self.snapshots = Snapshots()

    def dispatch(self, request: dict, *, peer_uid=None) -> dict:
        require(isinstance(request, dict), "Request must be an object")
        operation = request.get("op")
        store = self.scheduler.store
        if operation == "admin_cancel":
            # This value is supplied by connection(), NEVER by request JSON.
            if peer_uid != 0:
                raise RmgrError("OPERATOR_REQUIRED", "Operator cancellation requires a root Unix-socket peer (sudo)")
            job = self.scheduler.operator_cancel(request.get("job_id"), request.get("reason"))
            return {**job, "operator_actions": store.operator_actions(job["id"])}
        if operation == "lookup":
            job = store.lookup(request.get("request_id"))
            return {"found": job is not None, "request_id": request.get("request_id"), "job": job}
        if operation == "validate":
            spec = JobSpec.from_dict(request.get("spec"), self.scheduler.config)
            require(len(json.dumps(spec.to_dict())) <= MAX_SPEC_BYTES, "Job specification must be <= 64 KiB")
            require(Path(spec.argv[0]).is_file() and os.access(spec.argv[0], os.X_OK), "Executable must exist and be executable")
            warnings = []
            for dep in spec.after_success:
                job = store.get(dep)
                if job["state"] in TERMINAL - {"succeeded"}:
                    warnings.append(f"Dependency {dep} did not succeed; this job would be blocked")
            for stream in ("stdout", "stderr"):
                value = getattr(spec, stream)
                if value is not None:
                    path = Path(spec.cwd) / value
                    require(not os.path.lexists(path), f"{stream} target already exists: {path}")
                    require(path.parent.is_dir() and os.access(path.parent, os.W_OK), f"{stream} parent must exist and be writable")
                else:
                    require(os.access(spec.cwd, os.W_OK), "cwd must be writable for default output files")
            if self.scheduler.error:
                warnings.append("Scheduler is degraded; new submission is currently unavailable")
            return {"valid": True, "spec": spec.to_dict(), "effective_environment": environment(spec.to_dict()),
                    "resources": self.scheduler.resources(), "warnings": warnings,
                    "reserved": False, "note": "Checks current configuration and paths only; does not guarantee admission or future launch"}
        if operation == "list_snapshot":
            def capture():
                states = request.get("states")
                require(states is None or (isinstance(states, list) and all(isinstance(s, str) and s in ACTIVE | TERMINAL | {"queued"} for s in states)), "Invalid states filter")
                cwd = request.get("cwd")
                require(cwd is None or (isinstance(cwd, str) and Path(cwd).is_absolute()), "cwd filter must be absolute")
                return [j for j in store.all() if (not states or j["state"] in states) and (cwd is None or j["spec"]["cwd"] == cwd)]
            return self.snapshots.page(request, capture)
        if operation == "status":
            # No await or concurrent writer: health, resources, jobs come from one turn.
            health = self.dispatch({"op": "health"})
            return {"observed_at": time.time(), "health": health, "resources": self.scheduler.resources(),
                    "jobs": [j for j in store.all() if j["state"] in ACTIVE | {"queued"}], "snapshot_consistent": True}
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
                    "enforcement": self.capabilities, **runtime_capabilities()}
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
            job = store.get(request.get("job_id"))
            return {**job, "operator_actions": store.operator_actions(job["id"])}
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
            require(limit <= MAX_LOG_BYTES, "Log limit must be <= 1 MiB")
            tail = request.get("tail_bytes")
            if tail is not None:
                integer(tail, 1, "tail_bytes")
                require(tail <= MAX_LOG_BYTES and offset == 0, "tail_bytes must be <= 1 MiB and cannot be combined with a nonzero offset")
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
                    if tail is not None:
                        offset = max(0, info.st_size - tail)
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
        line = await asyncio.wait_for(reader.readline(), timeout=5)
        require(bool(line) and len(line) <= MAX_REQUEST, "Missing or oversized request")
        request = json.loads(line)
        require(isinstance(request, dict), "Request must be an object")
        require(uid == os.getuid() or (uid == 0 and request.get("op") == "admin_cancel"), "Only the daemon user may connect; root may only perform operator cancellation")
        response = {"ok": True, "result": api.dispatch(request, peer_uid=uid)}
    except RmgrError as exc:
        response = {"ok": False, "error": exc.to_dict()}
    except (OSError, sqlite3.Error) as exc:
        response = {"ok": False, "error": {"code": "STORAGE_UNAVAILABLE", "message": str(exc)}}
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
        if not isinstance(reply, dict) or type(reply.get("ok")) is not bool:
            raise ValueError("Malformed daemon response")
        if reply["ok"] and not isinstance(reply.get("result"), dict):
            raise ValueError("Malformed daemon result")
        if not reply["ok"] and (not isinstance(reply.get("error"), dict) or not all(isinstance(reply["error"].get(k), str) for k in ("code", "message"))):
            raise ValueError("Malformed daemon error")
    except (OSError, ValueError) as exc:
        raise RmgrError("DAEMON_UNAVAILABLE", f"Cannot reach daemon: {exc}. Run rmgr service status / rmgr doctor") from exc
    if not reply.get("ok"):
        error = reply["error"]
        raise RmgrError(error["code"], error["message"], **{k: v for k, v in error.items() if k not in {"code", "message"}})
    return reply["result"]
