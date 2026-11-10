"""Run inside a job's cgroup, verify enforcement, then replace this process.

This file intentionally uses only the standard library and no relative imports:
systemd invokes it by absolute path with the manager's Python interpreter.
"""

import json
import os
from pathlib import Path
import sys


def parse_cpus(value: str) -> set[int]:
    cpus = set()
    for item in value.strip().split(","):
        if not item:
            continue
        first, separator, last = item.partition("-")
        cpus.update(range(int(first), int(last) + 1) if separator else [int(first)])
    return cpus


def current_cgroup() -> Path:
    for line in Path("/proc/self/cgroup").read_text().splitlines():
        if line.startswith("0::"):
            return Path("/sys/fs/cgroup") / line[3:].lstrip("/")
    raise RuntimeError("A unified cgroup v2 hierarchy is required")


def verify_limits(cgroup: Path, cpus: list[int], memory: int) -> None:
    effective = parse_cpus((cgroup / "cpuset.cpus.effective").read_text())
    if effective != set(cpus):
        raise RuntimeError(f"CPU allocation mismatch: expected {cpus}, got {sorted(effective)}")
    limit = (cgroup / "memory.max").read_text().strip()
    if limit == "max" or int(limit) > memory:
        raise RuntimeError("Requested memory limit is not enforced")
    if (cgroup / "memory.swap.max").read_text().strip() != "0":
        raise RuntimeError("Job swap must be disabled")


def environment(spec: dict) -> dict[str, str]:
    # Do not copy the submitting agent's entire environment or persist its secrets.
    env = {key: os.environ[key] for key in ("HOME", "USER", "LOGNAME", "LANG") if key in os.environ}
    env["PATH"] = "/usr/local/bin:/usr/bin:/bin"
    for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        env[key] = str(spec["cores"])
    env["PYTHONUNBUFFERED"] = "1"
    env.update(spec["env"])
    # Device visibility is cooperative, not device-access security.
    env["CUDA_VISIBLE_DEVICES"] = "0" if spec["gpu"] else ""
    return env


def run(manifest_path: Path) -> int:
    manifest = json.loads(manifest_path.read_text())
    directory = manifest_path.parent
    for stream, fd in (("stdout", 1), ("stderr", 2)):
        with (directory / f"{stream}.log").open("ab", buffering=0) as output:
            os.dup2(output.fileno(), fd)
    try:
        spec = manifest["spec"]
        verify_limits(current_cgroup(), manifest["cpu_ids"], spec["memory_bytes"])
        os.sched_setaffinity(0, manifest["cpu_ids"])
        os.chdir(spec["cwd"])
        env = environment(spec)
        os.execve(spec["argv"][0], spec["argv"], env)
    except Exception as exc:
        (directory / "launch-error.txt").write_text(str(exc))
        print(f"rmgr: refusing to launch job: {exc}", file=sys.stderr, flush=True)
        return 125


if __name__ == "__main__":
    raise SystemExit(run(Path(sys.argv[1])))
