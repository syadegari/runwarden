"""Bounded, expiring read snapshots. No database writes and no job reservations."""

import json
import secrets
import time

from .models import RmgrError, integer, require
from .protocol import MAX_PAGE_SIZE


class Snapshots:
    ttl = 120
    max_bytes = 32 * 1024 * 1024
    max_entries = 16

    def __init__(self):
        self.entries = {}

    def page(self, request, capture):
        limit = integer(request.get("limit", MAX_PAGE_SIZE), 1, "limit")
        offset = integer(request.get("offset", 0), 0, "offset")
        require(limit <= MAX_PAGE_SIZE, f"limit must be <= {MAX_PAGE_SIZE}")
        now = time.monotonic()
        self.entries = {key: item for key, item in self.entries.items() if now < item["expires"]}
        key = request.get("snapshot_id")
        if key is None:
            jobs = capture()
            rows = [json.dumps(job).encode() for job in jobs]
            size = sum(map(len, rows))
            if size > self.max_bytes:
                raise RmgrError("SNAPSHOT_TOO_LARGE", "Narrow the states or cwd filter; snapshot exceeds 32 MiB")
            while self.entries and (len(self.entries) >= self.max_entries or
                                    sum(item["size"] for item in self.entries.values()) + size > self.max_bytes):
                del self.entries[next(iter(self.entries))]
            key = secrets.token_urlsafe(24)
            self.entries[key] = {"rows": rows, "size": size, "expires": now + self.ttl,
                                 "captured_at": time.time(), "states": request.get("states"), "cwd": request.get("cwd")}
        if not isinstance(key, str) or key not in self.entries:
            raise RmgrError("SNAPSHOT_EXPIRED", "Snapshot expired, was evicted, or daemon restarted; start a new listing")
        item = self.entries[key]
        for field in ("states", "cwd"):
            require(field not in request or request[field] == item[field], "Snapshot filters cannot change between pages")
        rows = item["rows"]
        end = offset + limit
        return {"jobs": [json.loads(row) for row in rows[offset:end]], "total": len(rows),
                "next_offset": end if end < len(rows) else None, "snapshot_id": key,
                "captured_at": item["captured_at"], "expires_in_seconds": max(0, item["expires"] - now),
                "consistency": "snapshot"}
