"""Offline documentation shipped with the executable's exact release."""

from importlib.resources import files

from . import __version__
from .models import RmgrError


COMMAND_HELP = {
    "describe": "Discover command arguments, constraints, and contracts offline as JSON",
    "validate": "Validate an effective job specification without creating a receipt or reserving resources",
    "lookup": "Find an existing job from a receipt without submitting or exposing its token",
    "admin-cancel": "Operator-only cancellation without a job key; requires a root socket peer and an audit reason",
    "instructions": "Print agent onboarding instructions to stdout; no daemon required",
    "manual": "Read the offline usage guide; start with 'rmgr manual'",
    "init": "Create configuration without replacing existing settings",
    "daemon": "Run the scheduler in the foreground (operator command)",
    "doctor": "Probe enforcement while the daemon and jobs are stopped",
    "health": "Check daemon, database, and backend readiness",
    "resources": "Show configured capacity and current reservations",
    "status": "Show a human-readable overview of capacity and active jobs",
    "submit": "Save a private receipt and enqueue a command or JSON specification",
    "retry": "Resubmit a saved receipt safely after a lost response",
    "list": "List public job information with bounded pagination",
    "inspect": "Read one job's specification, allocation, and state",
    "cancel": "Cancel a job using its control token from a receipt or stdin",
    "logs": "Read a bounded chunk of stdout or stderr",
    "wait": "Wait for a job until completion or a client-side deadline",
    "service": "Install or manage the shared user service (operator command)",
}


def manual(topic: str = "overview") -> dict:
    source = files("rmgr").joinpath("manual.md").read_text(encoding="utf-8")
    sections = {}
    for section in source.split("\n## ")[1:]:
        name, _, body = section.partition("\n")
        sections[name.strip()] = body.strip()
    if topic != "all" and topic not in sections:
        raise RmgrError("UNKNOWN_TOPIC", f"Unknown manual topic; choose from {', '.join(sections)}, all")
    return {"version": __version__, "topic": topic, "topics": [*sections, "all"],
            "text": source.strip() if topic == "all" else sections[topic]}
