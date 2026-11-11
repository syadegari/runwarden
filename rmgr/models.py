"""Validated configuration and job requests. Resource amounts are reservations."""

from dataclasses import asdict, dataclass, field
import json
import math
import os
from pathlib import Path
import re


GIB = 1024**3
ACTIVE = {"starting", "running", "cancelling"}
TERMINAL = {"succeeded", "failed", "cancelled", "timed_out", "oom", "blocked", "interrupted"}


class RmgrError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RmgrError("INVALID_REQUEST", message)


def integer(value: object, minimum: int, name: str) -> int:
    require(type(value) is int and value >= minimum, f"{name} must be an integer >= {minimum}")
    return value


def text(value: object, name: str) -> str:
    require(isinstance(value, str) and bool(value) and "\x00" not in value, f"Invalid {name}")
    return value


def physical_cpus() -> list[int]:
    """Choose one available logical CPU per physical core, preserving topology."""
    cores = {}
    for cpu in sorted(os.sched_getaffinity(0)):
        topology = Path(f"/sys/devices/system/cpu/cpu{cpu}/topology")
        try:
            key = (int((topology / "physical_package_id").read_text()),
                   int((topology / "core_id").read_text()))
        except OSError as exc:
            raise RmgrError("TOPOLOGY_UNAVAILABLE", str(exc)) from exc
        cores.setdefault(key, cpu)
    return list(cores.values())


def memory_total() -> int:
    return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")


@dataclass(frozen=True)
class Config:
    cpu_ids: list[int]
    memory_bytes: int
    gpu_count: int = 0

    @classmethod
    def default(cls):
        return cls(physical_cpus(), int(memory_total() * 0.75))

    @classmethod
    def from_dict(cls, data: dict):
        require(isinstance(data, dict), "Configuration must be an object")
        require(set(data) <= {"cpu_ids", "memory_bytes", "gpu_count"}, "Unknown configuration field")
        cpus = data.get("cpu_ids")
        require(isinstance(cpus, list) and bool(cpus), "cpu_ids must be a nonempty list")
        for cpu in cpus:
            integer(cpu, 0, "CPU ID")
        require(len(set(cpus)) == len(cpus), "CPU IDs must be unique")
        require(set(cpus) <= set(physical_cpus()), "Choose one discovered representative per physical core")
        memory = integer(data.get("memory_bytes"), 64 * 1024**2, "memory_bytes")
        require(memory <= memory_total(), "Memory budget exceeds physical RAM")
        gpu_count = integer(data.get("gpu_count", 0), 0, "gpu_count")
        require(gpu_count <= 1, "This version supports at most one GPU")
        return cls(cpus, memory, gpu_count)

    @classmethod
    def load(cls, home: Path):
        path = home / "config.json"
        if not path.exists():
            return cls.default()
        try:
            return cls.from_dict(json.loads(path.read_text()))
        except (ValueError, TypeError) as exc:
            raise RmgrError("INVALID_CONFIG", str(exc)) from exc


@dataclass(frozen=True)
class JobSpec:
    argv: list[str]
    cwd: str
    cores: int = 1
    memory_bytes: int = GIB
    gpu: bool = False
    exclusive: bool = False
    timeout_seconds: float = 3600
    after_success: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    stdout: str | None = None
    stderr: str | None = None

    @classmethod
    def from_dict(cls, data: dict, config: Config):
        require(isinstance(data, dict), "Job specification must be an object")
        require(set(data) <= set(cls.__dataclass_fields__), "Unknown job specification field")
        argv = data.get("argv")
        require(isinstance(argv, list) and bool(argv), "argv must be a nonempty string array")
        for arg in argv:
            require(isinstance(arg, str) and "\x00" not in arg, "Invalid argv entry")
        text(argv[0], "executable")
        require(Path(argv[0]).is_absolute(), "Use an absolute executable path")
        cwd = text(data.get("cwd"), "cwd")
        require(Path(cwd).is_absolute() and Path(cwd).is_dir(), "cwd must be an existing absolute directory")
        cores = integer(data.get("cores", 1), 1, "cores")
        memory = integer(data.get("memory_bytes", GIB), 64 * 1024**2, "memory_bytes")
        require(cores <= len(config.cpu_ids), "Requested cores exceed configured capacity")
        require(memory <= config.memory_bytes, "Requested memory exceeds configured capacity")
        gpu, exclusive = data.get("gpu", False), data.get("exclusive", False)
        require(type(gpu) is bool and type(exclusive) is bool, "gpu and exclusive must be booleans")
        require(not gpu or config.gpu_count == 1, "No GPU configured")
        timeout = data.get("timeout_seconds", 3600)
        require(type(timeout) in (int, float) and math.isfinite(timeout) and timeout > 0,
                "timeout_seconds must be finite and positive")
        deps = data.get("after_success", [])
        require(isinstance(deps, list), "after_success must be an array of job IDs")
        for dep in deps:
            require(isinstance(dep, str) and re.fullmatch(r"[a-f0-9]{32}", dep) is not None,
                    "Invalid dependency ID")
        require(len(set(deps)) == len(deps), "Duplicate dependency")
        env = data.get("env", {})
        require(isinstance(env, dict), "env must be an object")
        for key, value in env.items():
            require(isinstance(key, str) and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", key) is not None,
                    "Invalid environment key")
            require(isinstance(value, str) and "\x00" not in value, "Invalid environment value")
        outputs = {}
        for stream in ("stdout", "stderr"):
            value = data.get(stream)
            if value is not None:
                text(value, stream)
                outputs[stream] = os.path.abspath(Path(cwd) / value)
        require(len(set(outputs.values())) == len(outputs), "stdout and stderr must use different paths")
        return cls(argv, cwd, cores, memory, gpu, exclusive, timeout, deps, env,
                   data.get("stdout"), data.get("stderr"))

    def to_dict(self) -> dict:
        # Preserve canonical pre-0.3 specs so existing receipts remain retryable.
        return {key: value for key, value in asdict(self).items()
                if key not in {"stdout", "stderr"} or value is not None}


def prepare_home(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    require(home.is_dir() and home.stat().st_uid == os.getuid(), "State directory must belong to this user")
    require(home.stat().st_mode & 0o077 == 0, "State directory must have permissions 0700")
    (home / "jobs").mkdir(exist_ok=True, mode=0o700)
