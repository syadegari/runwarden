"""User-service installation; no sudo and no system-wide file changes."""

import os
from pathlib import Path
import sys

from .backend import command
from .models import RmgrError


def quote_unit_argument(value: str) -> str:
    # systemd's ExecStart syntax is not shell syntax. Disable specifier/env expansion.
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$").replace("\n", "\\n").replace("\r", "\\r") + '"'


def unit_text(home: Path) -> str:
    args = [sys.executable, "-I", "-m", "rmgr", "--home", str(home), "daemon"]
    return "\n".join([
        "# Managed by rmgr service install", "[Unit]", "Description=Local resource manager",
        "", "[Service]", "Type=exec", "ExecStart=" + " ".join(map(quote_unit_argument, args)),
        "Restart=on-failure", "RestartSec=3", "UMask=0077", "", "[Install]",
        "WantedBy=default.target", "",
    ])


def service(action: str, home: Path) -> dict:
    if action == "render":
        return {"unit": unit_text(home)}
    if action == "install":
        installed = command([sys.executable, "-I", "-c", "import rmgr"])
        if installed.returncode:
            raise RmgrError("NOT_INSTALLED", "Install the package with pipx or in a virtualenv before installing its service")
        config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        directory = config_home / "systemd" / "user"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "rmgr.service"
        if path.exists() and not path.read_text().startswith("# Managed by rmgr service install\n"):
            raise RmgrError("SERVICE_CONFLICT", f"Refusing to overwrite {path}")
        path.write_text(unit_text(home))
        for args in (["daemon-reload"], ["enable", "--now", "rmgr.service"]):
            result = command(["systemctl", "--user", *args])
            if result.returncode:
                raise RmgrError("SERVICE_ERROR", result.stderr.strip())
        return {"installed": str(path), "message": "Service enabled; run rmgr health to check readiness"}
    if action == "status":
        result = command(["systemctl", "--user", "show", "rmgr.service",
                          "--property=LoadState,ActiveState,SubState,UnitFileState,Result"])
        if result.returncode:
            raise RmgrError("SERVICE_ERROR", result.stderr.strip())
        return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    result = command(["systemctl", "--user", action, "rmgr.service"])
    if result.returncode:
        raise RmgrError("SERVICE_ERROR", result.stderr.strip())
    return {"action": action, "ok": True}
