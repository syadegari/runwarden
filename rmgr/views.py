"""Plain-text presentation of public daemon responses; no state or file access."""

from datetime import datetime
from pathlib import Path
import shlex
import time


def clean(value) -> str:
    """Keep command arguments and reasons from inserting terminal control codes."""
    value = str(value)
    return "".join(char if char.isprintable() else ascii(char)[1:-1] for char in value)


def memory(value: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024 or unit == "TiB":
            return f"{value:.2f}".rstrip("0").rstrip(".") + f" {unit}"
        value /= 1024


def duration(value) -> str:
    if value is None:
        return "—"
    seconds = max(0, int(value))
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return (f"{hours}h {minutes:02}m {seconds:02}s" if hours else
            f"{minutes}m {seconds:02}s" if minutes else f"{seconds}s")


def timestamp(value) -> str:
    return datetime.fromtimestamp(value).astimezone().isoformat(timespec="seconds") if value is not None else "—"


def runtime(job: dict, now: float) -> str:
    if job.get("started") is None:
        return "—"
    end = job.get("finished")
    return duration((now if end is None else end) - job["started"])


def shortened(value: str, width: int) -> str:
    return value if len(value) <= width else value[:width - 1] + "…"


def command(job: dict, wide: bool) -> str:
    argv = job["spec"]["argv"]
    if not wide:
        argv = [Path(argv[0]).name, *argv[1:]]
    value = clean(shlex.join(argv))
    return value if wide else shortened(value, 36)


def job_table(jobs: list[dict], wide: bool = False, now: float | None = None) -> str:
    if not jobs:
        return "No matching jobs."
    now = time.time() if now is None else now
    rows = [["JOB", "STATE", "CPUS", "MEMORY", "RUNTIME", "EXIT", "COMMAND", "REASON"]]
    if wide:
        rows[0].append("DIRECTORY")
    for job in jobs:
        reason = clean(job.get("reason") or "—")
        rows.append([job["id"], job["state"], str(job["spec"]["cores"]),
                     memory(job["spec"]["memory_bytes"]), runtime(job, now),
                     str(job["exit_code"]) if job.get("exit_code") is not None else "—",
                     command(job, wide), reason if wide else shortened(reason, 48)])
        if wide:
            rows[-1].append(clean(job["spec"]["cwd"]))
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip() for row in rows)


def resources_text(data: dict) -> str:
    total = data["physical_cores"]
    available = len(data["available_cpu_ids"])
    reserved_memory = data["memory_bytes"] - data["available_memory_bytes"]
    gpu = ("not configured" if not data["gpu_count"] else
           f"{0 if data['gpu_available'] else 1} / {data['gpu_count']} reserved · "
           f"{1 if data['gpu_available'] else 0} available (cooperative reservation)")
    return "\n".join([
        f"CPUs:   {total - available} / {total} reserved · {available} available",
        f"Memory: {memory(reserved_memory)} / {memory(data['memory_bytes'])} reserved · "
        f"{memory(data['available_memory_bytes'])} available",
        f"GPU:    {gpu}",
        f"Exclusive job running: {'yes' if data['exclusive_running'] else 'no'}",
    ])


def health_summary(data: dict) -> str:
    text = (f"Daemon: {clean(data['status'])} · "
            f"{'accepting jobs' if data['accepting_jobs'] else 'not accepting jobs'} · "
            f"version {clean(data['version'])}")
    if data.get("error"):
        text += f"\nError: {clean(data['error']['code'])}: {clean(data['error']['message'])}"
    return text


def health_text(data: dict) -> str:
    lines = [health_summary(data),
             f"Jobs: {data['running_jobs']} active (starting/running/cancelling) · {data['queued_jobs']} queued",
             "Enforcement:"]
    if "protocol_version" in data:
        lines.insert(1, f"Protocol: {data['protocol_version']} · features: {', '.join(data.get('features', []))}")
    for key, value in data.get("enforcement", {}).items():
        # Preserve old and new capability names without treating false as an error.
        label = "yes" if value is True else "no" if value is False else clean(value)
        lines.append(f"  {clean(key)}: {label}")
    return "\n".join(lines)


def inspect_text(result: dict, now: float) -> str:
    job, resources = result["job"], result["resources"]
    spec = job["spec"]
    lines = [f"Job:       {job['id']}", f"State:     {job['state']}",
             f"Reason:    {clean(job.get('reason') or '—')}", "",
             f"Requested: {spec['cores']} CPUs · {memory(spec['memory_bytes'])} memory",
             f"Available: {len(resources['available_cpu_ids'])} CPUs · {memory(resources['available_memory_bytes'])} memory (now)",
             f"Capacity:  {resources['physical_cores']} CPUs · {memory(resources['memory_bytes'])} memory",
             f"Allocated CPU IDs: {', '.join(map(str, job.get('allocation') or [])) or '—'}", "",
             f"Dependencies: {', '.join(spec['after_success']) or 'none'}",
             f"Exclusive:    {'yes' if spec['exclusive'] else 'no'}",
             f"GPU:          {'requested' if spec['gpu'] else 'not requested'}",
             f"Timeout:      {duration(spec['timeout_seconds'])}",
             f"Created:      {timestamp(job.get('created'))}",
             f"Started:      {timestamp(job.get('started'))}",
             f"Finished:     {timestamp(job.get('finished'))}",
             f"Runtime:      {runtime(job, now)}",
             f"Exit code:    {job['exit_code'] if job.get('exit_code') is not None else '—'}",
             f"Command:      {command(job, True)}",
             f"Directory:    {clean(spec['cwd'])}"]
    for stream in ("stdout", "stderr"):
        # Old daemons omit paths; the client supplies the legacy location.
        lines.append(f"{stream.capitalize() + ':':14}{clean(job[stream + '_path'])}")
    if job.get("reason_details"):
        lines.append(f"Reason code:  {clean(job['reason_details'].get('code'))}")
    for action in job.get("operator_actions", []):
        lines.append(f"Operator action: {timestamp(action['created'])} uid={action['peer_uid']} {clean(action['reason'])}")
    return "\n".join(lines)


def render(name: str, result: dict, *, wide: bool = False) -> str:
    now = time.time()
    if name == "resources":
        return resources_text(result)
    if name == "health":
        return health_text(result)
    if name == "inspect":
        return inspect_text(result, now)
    if name == "status":
        health = result["health"]
        return "\n".join([
            f"Observed: {timestamp(result['observed_at'])} ({'consistent snapshot' if result.get('snapshot_consistent') else 'separate live queries; legacy daemon'})",
            health_summary(health), resources_text(result["resources"]),
            f"Jobs: {health['running_jobs']} active (starting/running/cancelling) · {health['queued_jobs']} queued", "",
            job_table(result["jobs"], wide, now),
        ])
    if name == "list":
        text = job_table(result["jobs"], wide, now)
        text += f"\nShowing {len(result['jobs'])} of {result['total']} matching jobs."
        if result["next_offset"] is not None:
            text += f" More results: use --offset {result['next_offset']} with the same filters, or --all."
            if result.get("snapshot_id"):
                text += f" Continue this snapshot with --snapshot {result['snapshot_id']}."
        if result.get("consistency") == "best_effort_legacy":
            text += "\nLegacy daemon: results may change between queries/pages."
        return text
    raise ValueError(f"No text renderer for {name}")
