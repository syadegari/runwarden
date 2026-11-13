"""Systemd is responsible for enforcement, job lifetime, and process-tree cleanup."""

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

from .models import Config, RmgrError


@dataclass(frozen=True)
class Observation:
    state: str
    reason: str = ""
    exit_code: int | None = None


def command(argv: list[str], timeout: float = 10) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, text=True, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RmgrError("BACKEND_UNAVAILABLE", str(exc)) from exc


def unit_name(job_id: str) -> str:
    return f"rmgr-job-{job_id}.service"


def gpu_capabilities(config: Config) -> dict:
    reservation = config.gpu_count == 1
    scheduling = (
        "GPU reservations are enabled: one managed GPU job at a time. "
        if reservation else
        "GPU reservations are disabled (gpu_count=0); set gpu_count=1 once GPU 0 is ready. "
    )
    return {
        "gpu_device_isolation": False,
        "gpu_reservation": reservation,
        "gpu_availability": "not_checked",
        "gpu_note": (
            scheduling
            + "GPU hardware and driver availability have not been tested. "
            "gpu_device_isolation=false means device access is not enforced, not that the GPU is inaccessible. "
            "Reservations and CUDA_VISIBLE_DEVICES rely on cooperating programs; "
            "programs outside Runwarden can still access the GPU."
        ),
    }


class SystemdBackend:
    def __init__(self, home: Path):
        self.home = home

    def ping(self) -> None:
        result = command(["systemctl", "--user", "show", "--property=Version"])
        if result.returncode:
            raise RmgrError("BACKEND_UNAVAILABLE", result.stderr.strip() or "Cannot reach user systemd manager")

    def launch(self, job: dict) -> None:
        spec = job["spec"]
        directory = self.home / "jobs" / job["id"]
        manifest = directory / "launch.json"
        outputs = {stream: job.get(f"{stream}_path", str(directory / f"{stream}.log"))
                   for stream in ("stdout", "stderr")}
        try:
            directory.mkdir(mode=0o700)
            manifest.write_text(json.dumps({"spec": spec, "cpu_ids": job["allocation"], "output_paths": outputs}))
        except OSError as exc:
            # No systemd command has run: this failure is known to precede execution.
            raise RmgrError("LAUNCH_PREPARATION_FAILED", str(exc)) from exc
        properties = {
            "Type": "exec",
            "ExitType": "cgroup",
            "RemainAfterExit": "yes",
            "KillMode": "control-group",
            "TimeoutStopSec": "5s",
            "RuntimeMaxSec": f"{spec['timeout_seconds']}s",
            "AllowedCPUs": " ".join(map(str, job["allocation"])),
            "MemoryMax": str(spec["memory_bytes"]),
            "MemorySwapMax": "0",
            "OOMPolicy": "kill",
            "TasksMax": "4096",
            "UMask": "0077",
        }
        argv = ["systemd-run", "--user", "--no-ask-password", "--quiet",
                "--expand-environment=no", f"--unit={unit_name(job['id'])}"]
        for key, value in properties.items():
            argv.extend(["--property", f"{key}={value}"])
        argv.extend(["--", sys.executable, "-I", str(Path(__file__).with_name("runner.py")), str(manifest)])
        result = command(argv)
        if result.returncode:
            raise RmgrError("LAUNCH_ERROR", result.stderr.strip() or "systemd-run failed")

    def observe(self, job: dict) -> Observation:
        result = command([
            "systemctl", "--user", "show", unit_name(job["id"]),
            "--property=LoadState,ActiveState,SubState,Result,ExecMainCode,ExecMainStatus,ControlGroup",
        ])
        properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if properties.get("LoadState") == "not-found":
            return Observation("interrupted", "Unit is missing; job will not be automatically replayed")
        if result.returncode or not properties.get("ActiveState"):
            raise RmgrError("BACKEND_UNAVAILABLE", result.stderr.strip() or "Cannot query systemd")
        state, substate = properties["ActiveState"], properties.get("SubState")
        if state not in {"failed", "inactive"} and not (state == "active" and substate == "exited"):
            return Observation("running")
        # Never release a reservation while descendants are still being terminated.
        group = properties.get("ControlGroup")
        if group:
            events = Path("/sys/fs/cgroup") / group.lstrip("/") / "cgroup.events"
            try:
                if "populated 1" in events.read_text().splitlines():
                    return Observation("running", "Waiting for remaining processes")
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise RmgrError("BACKEND_UNAVAILABLE", str(exc)) from exc
        outcome = properties.get("Result", "unknown")
        exit_code = int(properties.get("ExecMainStatus", "0"))
        if properties.get("ExecMainCode") in {"2", "3"}:
            exit_code = -exit_code
        error_path = self.home / "jobs" / job["id"] / "launch-error.txt"
        if error_path.exists():
            return Observation("failed", error_path.read_text()[:4096], exit_code)
        if outcome == "oom-kill":
            return Observation("oom", "Job exceeded its memory limit", exit_code)
        if outcome == "timeout":
            return Observation("timed_out", "Job exceeded its execution timeout", exit_code)
        if outcome == "success" and exit_code == 0:
            return Observation("succeeded", exit_code=0)
        return Observation("failed", f"systemd result: {outcome}", exit_code)

    def cancel(self, job: dict) -> None:
        result = command(["systemctl", "--user", "--no-block", "stop", unit_name(job["id"])])
        if result.returncode:
            raise RmgrError("BACKEND_UNAVAILABLE", result.stderr.strip())

    def forget(self, job: dict) -> None:
        # Called only after the terminal state is durable in SQLite.
        command(["systemctl", "--user", "stop", unit_name(job["id"])])
        command(["systemctl", "--user", "reset-failed", unit_name(job["id"])])

    def check(self, config: Config) -> dict:
        """Probe the real execution path, including limits, before accepting work."""
        job = {
            "id": uuid.uuid4().hex,
            "allocation": [config.cpu_ids[0]],
            "spec": {
                "argv": ["/usr/bin/true"], "cwd": str(self.home), "cores": 1,
                "memory_bytes": 64 * 1024**2, "gpu": False, "env": {}, "timeout_seconds": 5,
            },
        }
        try:
            self.launch(job)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                observation = self.observe(job)
                if observation.state != "running":
                    if observation.state != "succeeded":
                        raise RmgrError("ENFORCEMENT_UNAVAILABLE", observation.reason)
                    return {"cpu_cpuset": True, "memory_limit": True, "swap_disabled": True,
                            **gpu_capabilities(config)}
                time.sleep(0.05)
            raise RmgrError("BACKEND_UNAVAILABLE", "Enforcement probe timed out")
        finally:
            # A failed probe must not leave a lingering workload behind.
            try:
                self.cancel(job)
                self.forget(job)
            except RmgrError:
                pass
