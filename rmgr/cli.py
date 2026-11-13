"""CLI with stable JSON results and opt-in human-readable inspection views."""

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
from .views import render
from .protocol import describe, MAX_LOG_BYTES


def memory_size(value: str) -> int:
    match = re.fullmatch(r"(\d+)([KMGT]?)(?:I?B)?", value.upper())
    if not match:
        raise argparse.ArgumentTypeError("Use bytes or an integer with K, M, G, or T suffix")
    return int(match[1]) * 1024 ** ("KMGT".index(match[2]) + 1 if match[2] else 0)


def job_arguments(command):
    # None distinguishes omitted options from explicit defaults beside --spec.
    command.add_argument("--spec", type=Path, help="JSON specification; cannot combine with explicit job options")
    command.add_argument("--cwd", type=Path, help="Execution directory (default: current directory)")
    command.add_argument("--stdout", help="New stdout file, relative to job cwd or absolute")
    command.add_argument("--stderr", help="New stderr file, relative to job cwd or absolute")
    command.add_argument("--cores", type=int, help="Physical core reservation (default: 1)")
    command.add_argument("--memory", type=memory_size, help="Memory reservation (default: 1G; minimum 64M)")
    command.add_argument("--gpu", action="store_true", default=None)
    command.add_argument("--exclusive", action="store_true", default=None)
    command.add_argument("--timeout", type=float, help="Job walltime limit in seconds (default: 3600)")
    command.add_argument("--after-success", action="append")
    command.add_argument("--env", action="append", metavar="KEY=VALUE")
    command.add_argument("argv", nargs=argparse.REMAINDER)


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
    for name in ("status", "list", "inspect", "resources", "health", "logs"):
        parsers[name].add_argument("--text", action="store_true", help="Human-readable output")
    for name in ("status", "list"):
        parsers[name].add_argument("--wide", action="store_true", help="Show full command lines and reasons in text output")
    parsers["status"].add_argument("--watch", type=float, metavar="SECONDS", help="Refresh repeatedly; Ctrl+C stops monitoring")
    parsers["manual"].add_argument("topic", nargs="?", default="overview", help="overview, instructions, jobs, ownership, limits, operations, or all")
    parsers["service"].add_argument("action", choices=["render", "install", "status", "start", "stop", "restart"])
    parsers["describe"].add_argument("topic", nargs="?", help="Optional command to describe")
    job_arguments(parsers["validate"])
    parsers["lookup"].add_argument("--receipt", type=Path, required=True)
    parsers["admin-cancel"].add_argument("job_id")
    parsers["admin-cancel"].add_argument("--reason", required=True, help="Required audit explanation (up to 1000 characters)")
    submit = parsers["submit"]
    job_arguments(submit)
    submit.add_argument("--request-id", default=None)
    submit.add_argument("--receipt", type=Path, required=True, help="New private receipt file; saved before submission and never overwritten")
    submit.add_argument("--id-only", action="store_true")
    submit.add_argument("--show-token", action="store_true", help="Explicitly include the private control token in output")
    parsers["retry"].add_argument("--receipt", type=Path, required=True, help="Resend exactly this saved submission")
    parsers["retry"].add_argument("--id-only", action="store_true")
    parsers["retry"].add_argument("--show-token", action="store_true", help="Explicitly include the private control token in output")
    for name in ("inspect", "cancel", "logs", "wait"):
        parsers[name].add_argument("job_id")
    parsers["inspect"].add_argument("--cwd-only", action="store_true", help="Print only the recorded execution directory")
    cancellation = parsers["cancel"].add_mutually_exclusive_group(required=True)
    cancellation.add_argument("--receipt", type=Path, help="Private receipt for the job being cancelled")
    cancellation.add_argument("--token-stdin", action="store_true", help="Read a raw control token from stdin instead of command-line arguments")
    filters = parsers["list"].add_mutually_exclusive_group()
    filters.add_argument("--state", action="append", choices=sorted(ACTIVE | TERMINAL | {"queued"}), help="Select a state; repeat to combine states")
    filters.add_argument("--active", action="store_true", help="Queued, starting, running, or cancelling jobs")
    filters.add_argument("--finished", action="store_true", help="All terminal jobs, including blocked and interrupted")
    parsers["list"].add_argument("--all", action="store_true", help="Fetch all matching jobs from the offset, across pages")
    parsers["list"].add_argument("--cwd", type=Path, help="Filter by exact execution directory")
    parsers["list"].add_argument("--snapshot", help="Continue a previously returned snapshot ID (new daemon required)")
    parsers["list"].add_argument("--offset", type=int, default=0)
    parsers["list"].add_argument("--limit", type=int, default=100)
    parsers["logs"].add_argument("--stream", choices=["stdout", "stderr"], default="stdout")
    parsers["logs"].add_argument("--offset", type=int, default=0)
    parsers["logs"].add_argument("--limit", type=int, default=16384)
    parsers["logs"].add_argument("--tail", type=int, metavar="BYTES", help="Read the last BYTES (1–1048576); new daemon required")
    parsers["logs"].add_argument("--follow", action="store_true", help="Follow output with time and byte limits")
    parsers["logs"].add_argument("--timeout", type=float, default=30, help="Follow deadline in seconds (default: 30)")
    parsers["logs"].add_argument("--interval", type=float, default=1, help="Follow polling interval in seconds (default: 1)")
    parsers["logs"].add_argument("--max-bytes", type=int, default=MAX_LOG_BYTES, help="Maximum bytes read during follow (default: 1 MiB)")
    parsers["wait"].add_argument("--timeout", type=float, default=30)
    return root


def submission(args) -> dict:
    if args.spec:
        explicit = [name for name in ("cwd", "stdout", "stderr", "cores", "memory", "gpu", "exclusive", "timeout", "after_success", "env") if getattr(args, name) is not None]
        if args.argv or explicit:
            raise RmgrError("INVALID_REQUEST", "--spec cannot be combined with command arguments or explicit job options: " + ", ".join(explicit))
        spec = json.loads(args.spec.read_text())
        if not isinstance(spec, dict):
            raise RmgrError("INVALID_REQUEST", "Job specification must be a JSON object")
        return spec
    argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    if not argv:
        raise RmgrError("INVALID_REQUEST", "Supply a command after -- or a --spec file")
    executable = shutil.which(argv[0])
    if executable is None:
        raise RmgrError("INVALID_REQUEST", f"Executable not found: {argv[0]}")
    # Preserve virtualenv symlinks: resolving them can select the wrong Python environment.
    argv[0] = os.path.abspath(executable)
    env = {}
    for entry in args.env or []:
        key, separator, value = entry.partition("=")
        if not separator:
            raise RmgrError("INVALID_REQUEST", "--env expects KEY=VALUE")
        env[key] = value
    spec = {"argv": argv, "cwd": str((args.cwd or Path.cwd()).resolve()), "cores": args.cores if args.cores is not None else 1,
            "memory_bytes": args.memory if args.memory is not None else 1024**3, "gpu": bool(args.gpu), "exclusive": bool(args.exclusive),
            "timeout_seconds": args.timeout if args.timeout is not None else 3600, "after_success": args.after_success or [], "env": env}
    for stream in ("stdout", "stderr"):
        if getattr(args, stream) is not None:
            spec[stream] = getattr(args, stream)
    return spec


def unsupported(exc):
    return exc.code == "INVALID_REQUEST" and str(exc) == "Unknown operation"


def require_feature(home, feature):
    if feature not in request(home, {"op": "health"}).get("features", []):
        raise RmgrError("UNSUPPORTED_FEATURE", f"The connected daemon does not advertise {feature}; this needs a planned daemon upgrade")


def list_jobs(home: Path, states=None, *, offset=0, limit=100, all_pages=False, cwd=None, snapshot=None) -> dict:
    if offset < 0 or not 1 <= limit <= 100:
        raise RmgrError("INVALID_REQUEST", "offset must be >= 0 and limit must be between 1 and 100")
    payload = {"op": "list_snapshot", "offset": offset, "limit": limit}
    if snapshot:
        payload["snapshot_id"] = snapshot
    if states:
        payload["states"] = sorted(set(states))
    if cwd is not None:
        payload["cwd"] = str(cwd)
    try:
        result = request(home, payload)
    except RmgrError as exc:
        if not unsupported(exc):
            raise
        if snapshot:
            raise RmgrError("UNSUPPORTED_FEATURE", "This daemon does not support listing snapshots") from exc
        result = legacy_list_jobs(home, states, offset=0 if cwd else offset, limit=limit, all_pages=all_pages or cwd is not None)
        if cwd is not None:
            jobs = [j for j in result["jobs"] if j["spec"]["cwd"] == str(cwd)]
            end = len(jobs) if all_pages else offset + limit
            result = {"jobs": jobs[offset:end], "total": len(jobs), "next_offset": end if end < len(jobs) else None}
        return {**result, "consistency": "best_effort_legacy"}
    jobs = result["jobs"][:]
    while all_pages and result["next_offset"] is not None:
        next_offset = result["next_offset"]
        if next_offset <= payload["offset"]:
            raise RmgrError("INVALID_RESPONSE", "List pagination did not advance")
        payload = {"op": "list_snapshot", "snapshot_id": result["snapshot_id"], "offset": next_offset, "limit": limit}
        result = request(home, payload)
        jobs.extend(result["jobs"])
    return {**result, "jobs": jobs}


def legacy_list_jobs(home: Path, states=None, *, offset=0, limit=100, all_pages=False) -> dict:
    """Combine legacy single-state queries on the client; no daemon upgrade needed."""
    if offset < 0 or not 1 <= limit <= 100:
        raise RmgrError("INVALID_REQUEST", "offset must be >= 0 and limit must be between 1 and 100")
    states = sorted(set(states or []))
    if len(states) > 1:
        jobs = {}
        for state in states:
            for job in legacy_list_jobs(home, [state], all_pages=True)["jobs"]:
                jobs[job["id"]] = job
        # A job can change state between queries. Deduplicate it and present one row.
        ordered = sorted(jobs.values(), key=lambda job: (job["created"], job["id"]))
        end = len(ordered) if all_pages else offset + limit
        return {"jobs": ordered[offset:end], "total": len(ordered),
                "next_offset": end if end < len(ordered) else None}
    payload = {"op": "list", "offset": offset, "limit": limit}
    if states:
        payload["state"] = states[0]
    result = request(home, payload)
    if not all_pages:
        return result
    jobs = {job["id"]: job for job in result["jobs"]}
    while result["next_offset"] is not None:
        next_offset = result["next_offset"]
        if next_offset <= payload["offset"]:
            raise RmgrError("INVALID_RESPONSE", "List pagination did not advance")
        payload = {**payload, "offset": next_offset}
        result = request(home, payload)
        jobs.update((job["id"], job) for job in result["jobs"])
    return {"jobs": list(jobs.values()), "total": result["total"], "next_offset": None}


def execute(args) -> tuple[dict, int]:
    home = args.home.expanduser().resolve()
    name = args.command
    if name == "describe":
        return describe(parser(), args.topic), 0
    if name == "instructions":
        return manual("instructions"), 0
    if name == "manual":
        return manual(args.topic), 0
    if name == "list":
        states = (ACTIVE | {"queued"} if args.active else TERMINAL if args.finished else args.state)
        return list_jobs(home, states, offset=args.offset, limit=args.limit, all_pages=args.all,
                         cwd=args.cwd.resolve() if args.cwd is not None else None, snapshot=args.snapshot), 0
    if name == "status":
        try:
            result = request(home, {"op": "status"})
            return result, 2 if result["health"]["status"] != "ready" else 0
        except RmgrError as exc:
            if not unsupported(exc):
                raise
        health = request(home, {"op": "health"})
        resources = request(home, {"op": "resources"})
        jobs = list_jobs(home, ACTIVE | {"queued"}, all_pages=True)
        return {"observed_at": time.time(), "health": health, "resources": resources,
                "jobs": jobs["jobs"], "snapshot_consistent": False}, 2 if health["status"] != "ready" else 0
    if name == "validate":
        spec = submission(args)
        require_feature(home, "validate")
        return request(home, {"op": "validate", "spec": spec}), 0
    if name == "lookup":
        receipt = read_receipt(args.receipt.expanduser().absolute(), home)
        try:
            return request(home, {"op": "lookup", "request_id": receipt["request_id"]}), 0
        except RmgrError as exc:
            if not unsupported(exc):
                raise
        # Legacy lookup is public and read-only too; never call retry as a fallback.
        jobs = legacy_list_jobs(home, all_pages=True)["jobs"]
        job = next((j for j in jobs if j["request_id"] == receipt["request_id"]), None)
        return {"found": job is not None, "request_id": receipt["request_id"], "job": job}, 0
    if name == "admin-cancel":
        if os.geteuid() != 0:
            raise RmgrError("OPERATOR_REQUIRED", "Use sudo with an explicit --home to the user's daemon; no job key is needed")
        if not args.reason.strip() or len(args.reason) > 1000:
            raise RmgrError("INVALID_REQUEST", "Operator reason must contain 1–1000 characters")
        try:
            return request(home, {"op": "admin_cancel", "job_id": args.job_id, "reason": args.reason}), 0
        except RmgrError as exc:
            if unsupported(exc):
                raise RmgrError("UNSUPPORTED_FEATURE", "Operator cancellation requires the upgraded daemon") from exc
            raise
    if name == "inspect" and args.text and not args.cwd_only:
        job = request(home, {"op": "inspect", "job_id": args.job_id})
        job = dict(job)
        for stream in ("stdout", "stderr"):
            job.setdefault(f"{stream}_path", str(home / "jobs" / job["id"] / f"{stream}.log"))
        return {"job": job, "resources": request(home, {"op": "resources"})}, 0
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
            result = {**job, "receipt": str(path)}
            if args.show_token:
                result["control_token"] = receipt["control_token"]
            return result, 0
        except RmgrError as exc:
            rejected = exc.code in {"INVALID_REQUEST", "NOT_FOUND", "FORBIDDEN", "CONTROL_TOKEN_REQUIRED", "REQUEST_CONFLICT", "LEGACY_JOB"}
            retry = not rejected
            recovery = "retry_same_receipt" if retry else "correct_request_use_new_receipt"
            advice = "Use rmgr retry --receipt PATH" if retry else "Correct the request and use a new receipt; retry preserves this rejected specification"
            raise RmgrError(exc.code, f"{exc}; submission request_id={receipt['request_id']}; receipt={path}. {advice}",
                            acceptance="rejected" if rejected else "unknown", recovery=recovery,
                            request_id=receipt["request_id"], receipt=str(path)) from exc
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
    if name == "logs" and args.tail is not None:
        require_feature(home, "log_tail")
        payload["tail_bytes"] = args.tail
    if name == "logs" and getattr(args, "follow_deadline", None) is not None:
        result = request(home, payload, timeout=max(0.1, min(10, args.follow_deadline - time.monotonic())))
    else:
        result = request(home, payload)
    return result, 2 if name == "health" and result["status"] != "ready" else 0


def main(argv=None) -> int:
    cli = parser()
    args = cli.parse_args(argv)
    if args.command == "admin-cancel" and not any(a == "--home" or a.startswith("--home=") for a in (sys.argv[1:] if argv is None else argv)):
        cli.error("admin-cancel requires explicit --home to the user's daemon state directory")
    as_text = getattr(args, "text", False) or (args.command == "status" and not args.json)
    if getattr(args, "text", False) and args.json:
        cli.error("--text and --json cannot be combined")
    if getattr(args, "wide", False) and not as_text:
        cli.error("--wide requires text output")
    watch = getattr(args, "watch", None)
    if watch is not None and (not math.isfinite(watch) or watch <= 0):
        cli.error("--watch must be finite and positive")
    if getattr(args, "cwd_only", False) and args.json:
        cli.error("--cwd-only and --json cannot be combined")
    if getattr(args, "show_token", False) and args.id_only:
        cli.error("--show-token and --id-only cannot be combined")
    follow = args.command == "logs" and args.follow
    if args.command == "logs":
        if args.tail is not None and (not 1 <= args.tail <= MAX_LOG_BYTES or args.offset != 0):
            cli.error("--tail must be 1–1048576 bytes and cannot be combined with a nonzero --offset")
        if not 1 <= args.limit <= MAX_LOG_BYTES or args.offset < 0:
            cli.error("--limit must be 1–1048576 bytes and --offset nonnegative")
        if not math.isfinite(args.timeout) or args.timeout < 0 or not math.isfinite(args.interval) or args.interval <= 0 or args.max_bytes <= 0:
            cli.error("Follow timeout must be finite and nonnegative; interval finite and positive; max-bytes positive")
    if follow:
        args.follow_deadline = time.monotonic() + args.timeout
        remaining_bytes = args.max_bytes
    try:
        while True:
            if follow:
                args.limit = min(args.limit, remaining_bytes)
            result, code = execute(args)
            if watch is not None and as_text and sys.stdout.isatty():
                print("\033[2J\033[H", end="")
            if args.command == "inspect" and args.cwd_only:
                print(result["spec"]["cwd"])
            elif args.command in {"submit", "retry"} and args.id_only:
                print(result["id"])
            elif args.command in {"manual", "instructions"} and not args.json:
                print(result["text"])
            elif args.command == "service" and args.action == "render" and not args.json:
                print(result["unit"], end="")
            elif as_text and args.command == "logs":
                print(result["text"], end="")
                if result.get("available") is False:
                    print("Output file is not available yet or has been removed.", file=sys.stderr)
                if result["more"] and not follow:
                    print(f"More output available: use --offset {result['next_offset']} with the same stream.", file=sys.stderr)
            elif as_text:
                print(render(args.command, result, wide=getattr(args, "wide", False)))
            else:
                print(json.dumps(result, indent=None if args.json or follow else 2))
            if follow:
                sys.stdout.flush()
                remaining_bytes -= result["next_offset"] - result["offset"]
                args.offset, args.tail = result["next_offset"], None
                if result["state"] in TERMINAL and not result["more"]:
                    return code
                remaining_time = args.follow_deadline - time.monotonic()
                if remaining_bytes <= 0 or remaining_time <= 0:
                    print(f"Follow stopped at {'byte limit' if remaining_bytes <= 0 else 'deadline'}; next offset {args.offset}.", file=sys.stderr)
                    return code
                time.sleep(0 if result["more"] else min(args.interval, remaining_time))
                continue
            if watch is None:
                return code
            sys.stdout.flush()
            time.sleep(watch)
    except KeyboardInterrupt:
        return 130
    except (RmgrError, OSError, ValueError) as exc:
        error = exc.to_dict() if isinstance(exc, RmgrError) else {"code": "LOCAL_ERROR", "message": str(exc)}
        print(json.dumps({"error": error}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
