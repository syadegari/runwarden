"""Shared discovery contracts for the client and daemon."""

from dataclasses import MISSING, fields
from pathlib import Path
import argparse

from . import __version__
from .models import ACTIVE, TERMINAL, JobSpec

PROTOCOL_VERSION = 2
FEATURES = ["validate", "lookup", "list_snapshot", "status_snapshot", "log_tail",
            "queue_details", "operator_cancel", "working_directory_outputs"]
MAX_SPEC_BYTES = 65536
MAX_LOG_BYTES = 1024 * 1024
MAX_PAGE_SIZE = 100


def runtime_capabilities():
    return {"protocol_version": PROTOCOL_VERSION, "features": list(FEATURES)}


def describe(parser, topic=None):
    """Read argument definitions rather than maintain a second CLI option catalog."""
    commands = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction)).choices
    if topic is not None and topic not in commands:
        from .models import RmgrError
        raise RmgrError("UNKNOWN_COMMAND", f"Unknown command: {topic}")

    def argument(action):
        default = action.default
        if isinstance(default, Path):
            default = str(default)
        return {"name": action.dest, "flags": action.option_strings, "required": action.required,
                "default": default if default != argparse.SUPPRESS else None,
                "type": getattr(action.type, "__name__", "boolean" if isinstance(action, argparse._StoreTrueAction) else "string"),
                "choices": list(action.choices) if action.choices is not None else None,
                "repeatable": isinstance(action, argparse._AppendAction), "nargs": action.nargs,
                "help": action.help}

    job_fields = ["id", "request_id", "spec", "state", "created", "started", "finished", "allocation",
                  "reason", "reason_details", "exit_code", "stdout_path", "stderr_path"]
    output_fields = {
        "submit": [*job_fields, "receipt"],
        "retry": [*job_fields, "receipt"],
        "list": ["jobs", "total", "next_offset", "snapshot_id", "captured_at", "consistency"],
        "status": ["observed_at", "health", "resources", "jobs", "snapshot_consistent"],
        "validate": ["valid", "spec", "effective_environment", "resources", "warnings", "reserved"],
        "lookup": ["found", "request_id", "job"],
        "logs": ["text", "offset", "next_offset", "more", "state", "path", "available"],
        "inspect": [*job_fields, "operator_actions"],
        "health": ["status", "version", "protocol_version", "features", "accepting_jobs", "error", "enforcement"],
        "wait": ["finished", "job"],
        "cancel": job_fields,
        "admin-cancel": [*job_fields, "operator_actions"],
        "resources": ["physical_cores", "cpu_ids", "available_cpu_ids", "memory_bytes", "available_memory_bytes", "gpu_count", "gpu_available", "exclusive_running"],
        "manual": ["version", "topic", "topics", "text"],
        "instructions": ["version", "topic", "topics", "text"],
        "doctor": ["config", "state_directory", "status", "enforcement", "error"],
        "init": ["config", "resources"],
        "daemon": ["stopped"],
        "service": ["unit (render)", "installed/message (install)", "systemd properties (status)", "action/ok (start/stop/restart)"],
        "describe": ["client_version", "catalog_version", "supported_protocol_version", "commands", "job_spec", "constraints", "exit_codes"],
    }
    catalog = {}
    for name, command in commands.items():
        if topic is not None and topic != name:
            continue
        operator = name in {"init", "daemon", "doctor", "service", "admin-cancel"}
        mutates = operator or name in {"submit", "retry", "cancel"}
        catalog[name] = {"description": command.description, "role": "operator" if operator else "agent",
                         "read_only": not mutates, "offline": name in {"instructions", "manual", "describe"},
                         "authorization": "root_peer_uid" if name == "admin-cancel" else "job_control_token" if name in {"retry", "cancel"} else "none" if name in {"describe", "instructions", "manual"} else "daemon_user",
                         "arguments": [argument(a) for a in command._actions if a.dest != "help"],
                         "exclusive_option_groups": [[a.dest for a in group._group_actions] for group in command._mutually_exclusive_groups],
                         "output_fields": output_fields.get(name, []),
                         "notes": "Service status/render are read-only; other service actions change host state." if name == "service" else None}
    schema = {}
    for field in fields(JobSpec):
        default = (field.default if field.default is not MISSING else
                   field.default_factory() if field.default_factory is not MISSING else None)
        schema[field.name] = {"type": str(field.type), "required": field.default is MISSING and field.default_factory is MISSING,
                              "default": default}
    return {"client_version": __version__, "catalog_version": 1, "supported_protocol_version": PROTOCOL_VERSION,
            "runtime_discovery": "rmgr health --json (missing features means legacy/unknown; do not infer from client version)",
            "global_arguments": [argument(a) for a in parser._actions if a.dest in {"home", "json"}],
            "commands": catalog, "job_spec": schema,
            "constraints": {"minimum_memory_bytes": 64 * 1024**2, "maximum_spec_bytes": MAX_SPEC_BYTES,
                            "maximum_page_size": MAX_PAGE_SIZE, "maximum_log_chunk_bytes": MAX_LOG_BYTES,
                            "timeout": "finite and positive", "cores": "physical cores; must fit configured capacity",
                            "spec_input": "--spec cannot be combined with explicit job options or argv",
                            "paths": "JSON argv[0] and cwd must be absolute; output paths are relative to cwd unless absolute"},
            "states": {"active": sorted(ACTIVE | {"queued"}), "terminal": sorted(TERMINAL)},
            "exit_codes": {"0": "command succeeded", "2": "request/local/service error or degraded readiness",
                           "3": "wait deadline; job unfinished", "4": "wait observed unsuccessful terminal job",
                           "130": "interrupted by user"},
            "error_fields": ["code", "message", "acceptance", "recovery", "request_id", "receipt"],
            "token_output": "Stored in private receipt; submit/retry print it only with --show-token",
            "examples": ["rmgr validate --cores 8 --memory 16G -- python -m fe_solver solve ...",
                         "rmgr submit --receipt ./run.receipt.json --cores 8 --memory 16G -- python -m fe_solver solve ...",
                         "rmgr lookup --receipt ./run.receipt.json --json", "rmgr logs JOB_ID --stream stderr --tail 4096 --text"]}
