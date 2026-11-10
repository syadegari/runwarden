"""SQLite persistence; the daemon is the sole writer."""

import json
from pathlib import Path
import sqlite3
import time
import uuid

from .models import JobSpec, RmgrError, require
from .control import authorize, token_digest


class Store:
    def __init__(self, home: Path):
        self.db = sqlite3.connect(home / "jobs.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                request_id TEXT UNIQUE NOT NULL,
                spec TEXT NOT NULL,
                state TEXT NOT NULL,
                created REAL NOT NULL,
                started REAL,
                finished REAL,
                allocation TEXT,
                reason TEXT,
                exit_code INTEGER,
                control_token_hash TEXT
            )
        """)
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(jobs)")}
        if "control_token_hash" not in columns:
            # Existing history stays intact. Never invent control credentials for old jobs.
            self.db.execute("ALTER TABLE jobs ADD COLUMN control_token_hash TEXT")
        self.db.commit()

    @staticmethod
    def decode(row) -> dict:
        job = dict(row)
        job.pop("control_token_hash", None)
        job["spec"] = json.loads(job["spec"])
        job["allocation"] = json.loads(job["allocation"]) if job["allocation"] else None
        return job

    def get(self, job_id: str) -> dict:
        require(isinstance(job_id, str), "job_id must be a string")
        row = self.db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise RmgrError("NOT_FOUND", f"No job {job_id}")
        return self.decode(row)

    def all(self) -> list[dict]:
        return [self.decode(row) for row in self.db.execute("SELECT * FROM jobs ORDER BY created, rowid")]

    def submit(self, request_id: str, spec: JobSpec, control_token: str) -> dict:
        digest = token_digest(control_token)
        payload = json.dumps(spec.to_dict(), sort_keys=True)
        existing = self.db.execute("SELECT * FROM jobs WHERE request_id = ?", (request_id,)).fetchone()
        if existing is not None:
            authorize(existing["control_token_hash"], control_token)
            if existing["spec"] != payload:
                raise RmgrError("REQUEST_CONFLICT", "request_id already exists with a different specification")
            return self.decode(existing)
        # Dependencies must already exist. New jobs can only point backwards, so cycles are impossible.
        for dependency in spec.after_success:
            self.get(dependency)
        job_id = uuid.uuid4().hex
        with self.db:
            self.db.execute(
                "INSERT INTO jobs (id, request_id, spec, state, created, control_token_hash) VALUES (?, ?, ?, 'queued', ?, ?)",
                (job_id, request_id, payload, time.time(), digest),
            )
        return self.get(job_id)

    def authorize(self, job_id: str, control_token: object) -> dict:
        job = self.get(job_id)
        row = self.db.execute("SELECT control_token_hash FROM jobs WHERE id = ?", (job_id,)).fetchone()
        authorize(row["control_token_hash"], control_token)
        return job

    def update(self, job_id: str, **fields) -> None:
        allowed = {"state", "started", "finished", "allocation", "reason", "exit_code"}
        if not fields or not fields.keys() <= allowed:
            raise ValueError("Invalid job update")
        if "allocation" in fields:
            fields["allocation"] = json.dumps(fields["allocation"])
        columns = ", ".join(f"{name} = ?" for name in fields)
        with self.db:
            self.db.execute(f"UPDATE jobs SET {columns} WHERE id = ?", (*fields.values(), job_id))

    def close(self) -> None:
        self.db.close()
