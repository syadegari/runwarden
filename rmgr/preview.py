"""Development preview with synthetic jobs; never starts workloads or calls systemd.

Run with python3 -m rmgr.preview. The printed --home selects this disposable API.
"""

import asyncio
import json
from pathlib import Path
import signal
import tempfile
import time

from .control import create_receipt
from .daemon import API, MAX_REQUEST, connection
from .models import Config, GIB, RmgrError, prepare_home
from .scheduler import Scheduler
from .store import Store


class PreviewBackend:
    def ping(self):
        pass

    def launch(self, *args):
        raise RuntimeError("Preview must never launch jobs")

    cancel = forget = observe = launch


class PreviewAPI(API):
    def dispatch(self, request, **kwargs):
        if request.get("op") in {"submit", "cancel", "admin_cancel"}:
            raise RmgrError("PREVIEW_READ_ONLY", "Preview contains synthetic jobs and never executes or cancels workloads")
        result = super().dispatch(request, **kwargs)
        if request.get("op") == "health":
            result.update(accepting_jobs=False, preview=True)
            result["features"] = [feature for feature in result["features"] if feature != "operator_cancel"]
        return result


def seed(home, store, scheduler):
    jobs = []
    for number, state in enumerate(("running", "queued", "failed"), 1):
        cwd = home / f"worktree-{number}"
        cwd.mkdir()
        spec = {"argv": ["/usr/bin/true"], "cwd": str(cwd), "cores": 8,
                "memory_bytes": (44 if state == "queued" else 40) * GIB}
        receipt_path = home / f"run-{number}.receipt.json"
        receipt = create_receipt(receipt_path, home, spec)
        job = scheduler.submit(receipt["request_id"], spec, receipt["control_token"])
        now = time.time()
        store.update(job["id"], state=state, allocation=list(range(8)) if state == "running" else None,
                     started=None if state == "queued" else now - 79, finished=now if state == "failed" else None,
                     exit_code=255 if state == "failed" else None,
                     reason="Waiting for memory reservation" if state == "queued" else "Synthetic application failure" if state == "failed" else None,
                     reason_details={"code": "MEMORY_RESERVATION", "requested_bytes": 44 * GIB, "available_bytes": 7 * GIB,
                                     "blocking_job_ids": [jobs[0]["id"]]} if state == "queued" else None)
        directory = home / "jobs" / job["id"]
        directory.mkdir()
        identities = {}
        if state != "queued":
            for stream in ("stdout", "stderr"):
                path = Path(job[f"{stream}_path"])
                path.write_text(f"Synthetic {stream} for preview job {number}\n")
                path.chmod(0o600)
                info = path.stat()
                identities[stream] = {"device": info.st_dev, "inode": info.st_ino}
            (directory / "output-files.json").write_text(json.dumps(identities))
        jobs.append(job)
    return jobs


async def run(home):
    prepare_home(home)
    store = Store(home)
    scheduler = Scheduler(store, Config(list(range(16)), 47 * GIB), PreviewBackend())
    jobs = seed(home, store, scheduler)
    api = PreviewAPI(scheduler, home, {"preview": True, "note": "Synthetic data; enforcement is not tested"})
    path = home / "control.sock"
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    try:
        server = await asyncio.start_unix_server(lambda r, w: connection(r, w, api), path=path, limit=MAX_REQUEST)
        path.chmod(0o600)
        print("Read-only preview: synthetic jobs, no systemd, no real workloads.", flush=True)
        print(f"python3 -m rmgr.cli --home {home} status", flush=True)
        print(f"python3 -m rmgr.cli --home {home} lookup --receipt {home}/run-1.receipt.json", flush=True)
        print(f"python3 -m rmgr.cli --home {home} logs {jobs[2]['id']} --stream stderr --tail 4096 --text", flush=True)
        print("Ctrl+C stops the preview and removes its temporary state.", flush=True)
        async with server:
            await stop.wait()
    finally:
        store.close()


def main():
    try:
        with tempfile.TemporaryDirectory(prefix="rmgr-preview-") as directory:
            asyncio.run(run(Path(directory)))
    except OSError as exc:
        print(f"Preview could not start: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
