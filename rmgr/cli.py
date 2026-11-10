"""Small CLI, with the same structured results for humans and agent clients."""

import argparse
import asyncio
from dataclasses import asdict
import fcntl
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import sys
import time

from . import __version__
from .backend import SystemdBackend
from .control import create_receipt, read_receipt
from .daemon import request, serve
from .manual import COMMAND_HELP, manual
from .models import ACTIVE, Config, RmgrError, TERMINAL, prepare_home
from .service import service
from .store import Store


def memory_size(value: str) -> int:
    match = re.fullmatch(r"(\d+)([KMGT]?)(?:I?B)?", value.upper())
    if not match:
        raise argparse.ArgumentTypeError("Use bytes or an integer with K, M, G, or T suffix")
    return int(match[1]) * 1024 ** ("KMGT".index(match[2]) + 1 if match[2] else 0)


def parser() -> argparse.ArgumentParser:
    default_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "rmgr"
    root = argparse.ArgumentParser(prog="rmgr", description="Local resource queue. Start with 'rmgr manual' for usage and limitations.")
    root.add_argument("--version", action="version", version=f"rmgr {__version__}")
    root.add_argument("--home", type=Path, default=default_home, help="Private daemon state directory")
    root.add_argument("--json", action="store_true", help="Compact machine-readable output")
    commands = root.add_subparsers(dest="command", required=True)
    parsers = {}
    for name, description in COMMAND_HELP.items():
        parsers[name] = commands.add_parser(name, help=description, description=description)
        parsers[name].add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    parsers["manual"].add_argument("topic", nargs="?", default="overview", help="overview, jobs, ownership, limits, operations, or all")
    parsers["service"].add_argument("action", choices=["render", "install", "status", "start", "stop", "restart"])
    submit = parsers["submit"]
    submit.add_argument("--spec", type=Path, help="JSON job specification")
    submit.add_argument("--request-id", default=None)
    submit.add_argument("--receipt", type=Path, required=True, help="New private receipt file; saved before submission and never overwritten")
    submit.add_argument("--id-only", action="store_true")
    submit.add_argument("--cwd", type=Path, default=Path.cwd())
    submit.add_argument("--cores", type=int, default=1)
    submit.add_argument("--memory", type=memory_size, default=1024**3)
    submit.add_argument("--gpu", action="store_true")
    submit.add_argument("--exclusive", action="store_true")
    submit.add_argument("--timeout", type=float, default=3600)
    submit.add_argument("--after-success", action="append", default=[])
    submit.add_argument("--env", action="append", default=[], metavar="KEY=VALUE")
    submit.add_argument("argv", nargs=argparse.REMAINDER)
    parsers["retry"].add_argument("--receipt", type=Path, required=True, help="Resend exactly this saved submission")
    parsers["retry"].add_argument("--id-only", action="store_true")
    for name in ("inspect", "cancel", "logs", "wait"):
        parsers[name].add_argument("job_id")
    cancellation = parsers["cancel"].add_mutually_exclusive_group(required=True)
    cancellation.add_argument("--receipt", type=Path, help="Private receipt for the job being cancelled")
    cancellation.add_argument("--token-stdin", action="store_true", help="Read a raw control token from stdin instead of command-line arguments")
    parsers["list"].add_argument("--state")
    parsers["list"].add_argument("--offset", type=int, default=0)
    parsers["list"].add_argument("--limit", type=int, default=100)
    parsers["logs"].add_argument("--stream", choices=["stdout", "stderr"], default="stdout")
    parsers["logs"].add_argument("--offset", type=int, default=0)
    parsers["logs"].add_argument("--limit", type=int, default=16384)
    parsers["wait"].add_argument("--timeout", type=float, default=30)
    return root


def submission(args) -> dict:
    if args.spec:
        if args.argv:
            raise RmgrError("INVALID_REQUEST", "Use either --spec or command arguments")
        return json.loads(args.spec.read_text())
    argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    if not argv:
        raise RmgrError("INVALID_REQUEST", "Supply a command after -- or a --spec file")
    executable = shutil.which(argv[0])
    if executable is None:
        raise RmgrError("INVALID_REQUEST", f"Executable not found: {argv[0]}")
    # Preserve virtualenv symlinks: resolving them can select the wrong Python environment.
    argv[0] = os.path.abspath(executable)
    env = {}
    for entry in args.env:
        key, separator, value = entry.partition("=")
        if not separator:
            raise RmgrError("INVALID_REQUEST", "--env expects KEY=VALUE")
        env[key] = value
    return {"argv": argv, "cwd": str(args.cwd.resolve()), "cores": args.cores,
            "memory_bytes": args.memory, "gpu": args.gpu, "exclusive": args.exclusive,
            "timeout_seconds": args.timeout, "after_success": args.after_success, "env": env}


def execute(args) -> tuple[dict, int]:
    home = args.home.expanduser().resolve()
    name = args.command
    if name == "manual":
        return manual(args.topic), 0
    if name == "init":
        prepare_home(home)
        path = home / "config.json"
        if not path.exists():
            path.write_text(json.dumps(asdict(Config.default()), indent=2) + "\n")
        return {"config": str(path), "resources": asdict(Config.load(home))}, 0
    if name == "daemon":
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        asyncio.run(serve(home))
        return {"stopped": True}, 0
    if name == "service":
        return service(args.action, home), 0
    if name == "doctor":
        prepare_home(home)
        config = Config.load(home)
        result = {"config": asdict(config), "state_directory": str(home)}
        try:
            with (home / "daemon.lock").open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise RmgrError("BUSY", "Daemon is running; use health. Run the enforcement probe after jobs finish and the service stops") from exc
                backend = SystemdBackend(home)
                store = Store(home)
                try:
                    for job in store.all():
                        if job["state"] in ACTIVE and backend.observe(job).state == "running":
                            raise RmgrError("BUSY", "A managed job is still running; probing could disturb its measurement")
                finally:
                    store.close()
                result["enforcement"] = backend.check(config)
            result["status"] = "ready"
            return result, 0
        except RmgrError as exc:
            result.update(status="unavailable", error={"code": exc.code, "message": str(exc)})
            return result, 2
    if name in {"submit", "retry"}:
        path = args.receipt.expanduser().absolute()
        if name == "submit":
            receipt = create_receipt(path, home, submission(args), args.request_id)
        else:
            receipt = read_receipt(path, home)
        try:
            job = request(home, {"op": "submit", "request_id": receipt["request_id"],
                                 "spec": receipt["spec"], "control_token": receipt["control_token"]})
            return {**job, "control_token": receipt["control_token"], "receipt": str(path)}, 0
        except RmgrError as exc:
            raise RmgrError(exc.code, f"{exc}; submission request_id={receipt['request_id']}; receipt={path}. Use rmgr retry --receipt PATH") from exc
    if name == "cancel":
        if args.receipt:
            token = read_receipt(args.receipt.expanduser().absolute(), home)["control_token"]
        else:
            token = sys.stdin.readline(512).strip()
        return request(home, {"op": "cancel", "job_id": args.job_id, "control_token": token}), 0
    if name == "wait":
        if not math.isfinite(args.timeout) or args.timeout < 0:
            raise RmgrError("INVALID_REQUEST", "Wait timeout must be finite and nonnegative")
        deadline = time.monotonic() + args.timeout
        while True:
            job = request(home, {"op": "inspect", "job_id": args.job_id}, timeout=max(0.1, min(10, deadline - time.monotonic())))
            if job["state"] in TERMINAL:
                return {"finished": True, "job": job}, 0 if job["state"] == "succeeded" else 4
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"finished": False, "job": job}, 3
            time.sleep(min(0.5, remaining))
    payload = {"op": name}
    for field in ("job_id", "state", "offset", "limit", "stream"):
        if hasattr(args, field):
            payload[field] = getattr(args, field)
    result = request(home, payload)
    return result, 2 if name == "health" and result["status"] != "ready" else 0


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        result, code = execute(args)
        if args.command in {"submit", "retry"} and args.id_only:
            print(result["id"])
        elif args.command == "manual" and not args.json:
            print(result["text"])
        elif args.command == "service" and args.action == "render" and not args.json:
            print(result["unit"], end="")
        else:
            print(json.dumps(result, indent=None if args.json else 2))
        return code
    except (RmgrError, OSError, ValueError) as exc:
        error = {"code": exc.code if isinstance(exc, RmgrError) else "LOCAL_ERROR", "message": str(exc)}
        print(json.dumps({"error": error}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
