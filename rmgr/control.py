"""Per-job bearer tokens and durable client receipts.

Tokens authorize API mutations. They do not isolate clients that have unrestricted
access to the same Linux account. Receipts belong to callers, not job manifests.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import uuid

from .models import RmgrError


def token_digest(token: object) -> str:
    if not isinstance(token, str) or re.fullmatch(r"[A-Za-z0-9_-]{43}", token) is None:
        raise RmgrError("CONTROL_TOKEN_REQUIRED", "A valid job control token is required; use the submission receipt")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def authorize(stored_digest: str | None, token: object) -> None:
    if stored_digest is None:
        raise RmgrError("LEGACY_JOB", "This pre-token job has no control token; it requires operator handling")
    candidate = token_digest(token)
    if not secrets.compare_digest(stored_digest, candidate):
        raise RmgrError("FORBIDDEN", "The supplied control token does not authorize this job")


def create_receipt(path: Path, home: Path, spec: dict, request_id: str | None = None) -> dict:
    receipt = {"receipt_version": 1, "daemon_home": str(home), "request_id": request_id or uuid.uuid4().hex,
               "control_token": secrets.token_urlsafe(32), "spec": spec}
    data = (json.dumps(receipt, indent=2) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError as exc:
        raise RmgrError("RECEIPT_EXISTS", "Receipt already exists; use rmgr retry --receipt PATH or choose a new path") from exc
    with os.fdopen(fd, "wb") as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    # Make the new directory entry durable before any submission can reach the daemon.
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return receipt


def read_receipt(path: Path, home: Path) -> dict:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RmgrError("INVALID_RECEIPT", "Receipt must be a regular file owned by this user with permissions 0600")
        data = source.read(1024 * 1024 + 1)
    try:
        if len(data) > 1024 * 1024:
            raise ValueError("oversized")
        receipt = json.loads(data)
        if not isinstance(receipt, dict) or receipt.get("receipt_version") != 1:
            raise ValueError("version")
        if not isinstance(receipt.get("spec"), dict) or not isinstance(receipt.get("request_id"), str):
            raise ValueError("fields")
    except (ValueError, UnicodeError) as exc:
        raise RmgrError("INVALID_RECEIPT", "Invalid or unsupported receipt file") from exc
    if receipt.get("daemon_home") != str(home):
        raise RmgrError("WRONG_DAEMON", "Receipt belongs to a different state directory; use the original --home")
    token_digest(receipt.get("control_token"))
    return receipt
