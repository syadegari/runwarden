"""Output ownership, migration, and real redirection without a systemd dependency."""

import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from rmgr.backend import SystemdBackend
from rmgr.cli import parser, submission
from rmgr.control import token_digest
from rmgr.daemon import API
from rmgr.models import Config, GIB, JobSpec, RmgrError, prepare_home
from rmgr.scheduler import Scheduler
from rmgr.store import Store
from tests.test_scheduler import FakeBackend


class OutputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home, self.cwd = self.root / "state", self.root / "experiment"
        prepare_home(self.home)
        self.cwd.mkdir()
        self.config = Config([min(os.sched_getaffinity(0))], GIB)
        self.store = Store(self.home)
        self.scheduler = Scheduler(self.store, self.config, FakeBackend())
        self.api = API(self.scheduler, self.home, {})
        self.token = secrets.token_urlsafe(32)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def submit(self, **overrides):
        spec = {"argv": [sys.executable, "-c", "import sys; print('hello'); print('warning', file=sys.stderr)"],
                "cwd": str(self.cwd)}
        spec.update(overrides)
        return self.scheduler.submit(secrets.token_hex(16), spec, self.token)

    def launch(self, job, legacy_manifest=False):
        job = {**job, "allocation": self.config.cpu_ids}
        with patch("rmgr.backend.command", return_value=subprocess.CompletedProcess([], 0, "", "")):
            SystemdBackend(self.home).launch(job)
        manifest = self.home / "jobs" / job["id"] / "launch.json"
        if legacy_manifest:
            data = json.loads(manifest.read_text())
            del data["output_paths"]
            manifest.write_text(json.dumps(data))
        # Exercise real open/dup2/exec in a child. Enforcement has its own live
        # integration tests; only that host dependency is mocked here.
        code = """
import sys
from pathlib import Path
from unittest.mock import patch
from rmgr.runner import run
with patch('rmgr.runner.current_cgroup'), patch('rmgr.runner.verify_limits'):
    raise SystemExit(run(Path(sys.argv[1])))
"""
        return subprocess.run([sys.executable, "-c", code, str(manifest)], capture_output=True, text=True, timeout=10)

    def test_default_outputs_are_unique_local_private_and_queryable_after_reopen(self):
        first, second = self.submit(), self.submit()
        self.assertNotEqual(first["stdout_path"], second["stdout_path"])
        for job in (first, second):
            result = self.launch(job)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            for stream, expected in (("stdout", "hello\n"), ("stderr", "warning\n")):
                path = Path(job[f"{stream}_path"])
                self.assertEqual(path.parent, self.cwd)
                self.assertEqual(path.read_text(), expected)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertFalse((self.home / "jobs" / job["id"] / f"{stream}.log").exists())
        self.store.close()
        self.store = Store(self.home)
        self.scheduler = Scheduler(self.store, self.config, FakeBackend())
        self.api = API(self.scheduler, self.home, {})
        self.assertEqual(self.store.get(first["id"])["stdout_path"], first["stdout_path"])
        chunk = self.api.dispatch({"op": "logs", "job_id": first["id"], "limit": 2})
        self.assertTrue(chunk["available"])
        self.assertEqual(chunk["text"], "he")
        self.assertTrue(chunk["more"])
        rest = self.api.dispatch({"op": "logs", "job_id": first["id"], "offset": chunk["next_offset"]})
        self.assertEqual(rest["text"], "llo\n")

    def test_cli_relative_outputs_use_job_cwd_and_support_absolute_paths(self):
        nested = self.cwd / "outputs"
        nested.mkdir()
        err = self.root / "absolute stderr.log"
        args = parser().parse_args(["submit", "--receipt", str(self.root / "receipt"),
                                    "--cwd", str(self.cwd), "--stdout", "outputs/out.log",
                                    "--stderr", str(err), "--", sys.executable, "-c",
                                    "import sys; print('hello'); print('warning', file=sys.stderr)"])
        job = self.scheduler.submit("cli", submission(args), self.token)
        self.assertEqual(job["stdout_path"], str(nested / "out.log"))
        self.assertEqual(job["stderr_path"], str(err))
        before = self.api.dispatch({"op": "logs", "job_id": job["id"]})
        self.assertFalse(before["available"])
        self.assertEqual(self.launch(job).returncode, 0)
        err_reply = self.api.dispatch({"op": "logs", "job_id": job["id"], "stream": "stderr"})
        self.assertEqual(err_reply["text"], "warning\n")
        err.unlink()
        missing = self.api.dispatch({"op": "logs", "job_id": job["id"], "stream": "stderr"})
        self.assertFalse(missing["available"])
        self.assertEqual(missing["path"], str(err))

    def test_collision_fails_without_overwriting_or_exposing_another_jobs_output(self):
        first = self.submit(stdout="shared.out", stderr="shared.err")
        second = self.submit(stdout="shared.out", stderr="shared.err")
        self.assertEqual(self.launch(first).returncode, 0)
        result = self.launch(second)
        self.assertEqual(result.returncode, 125)
        self.assertIn("File exists", result.stderr)
        self.assertEqual(Path(first["stdout_path"]).read_text(), "hello\n")
        reply = self.api.dispatch({"op": "logs", "job_id": second["id"]})
        self.assertFalse(reply["available"])
        self.assertEqual(reply["text"], "")
        error = self.home / "jobs" / second["id"] / "launch-error.txt"
        self.assertIn("File exists", error.read_text())

    def test_missing_parent_and_existing_stderr_prevent_execution(self):
        marker = self.cwd / "executed"
        argv = [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"]
        missing = self.submit(stdout="missing/out", argv=argv)
        self.assertEqual(self.launch(missing).returncode, 125)
        self.assertFalse(marker.exists())
        existing = self.cwd / "existing.err"
        existing.write_text("original")
        blocked = self.submit(stderr="existing.err", argv=argv)
        self.assertEqual(self.launch(blocked).returncode, 125)
        self.assertFalse(marker.exists())
        self.assertEqual(existing.read_text(), "original")

    def test_launcher_errors_are_central_not_in_program_stderr(self):
        job = self.submit(argv=[str(self.cwd / "missing-program")])
        result = self.launch(job)
        self.assertEqual(result.returncode, 125)
        self.assertIn("refusing to launch", result.stderr)
        self.assertEqual(Path(job["stderr_path"]).read_text(), "")
        self.assertTrue((self.home / "jobs" / job["id"] / "launch-error.txt").exists())

    def test_reader_rejects_replaced_files_symlinks_and_fifos_without_blocking(self):
        for replacement in ("regular", "symlink", "fifo"):
            with self.subTest(replacement=replacement):
                job = self.submit()
                self.assertEqual(self.launch(job).returncode, 0)
                path = Path(job["stdout_path"])
                # Retain the old inode to avoid inode reuse by the filesystem.
                path.rename(path.with_suffix(".saved"))
                if replacement == "regular":
                    path.write_text("other data")
                elif replacement == "symlink":
                    path.symlink_to(path.with_suffix(".saved"))
                else:
                    os.mkfifo(path)
                with self.assertRaises(RmgrError) as error:
                    self.api.dispatch({"op": "logs", "job_id": job["id"]})
                self.assertEqual(error.exception.code, "LOG_UNAVAILABLE")

    def test_output_validation_and_retry_do_not_revalidate_finished_jobs(self):
        for overrides in ({"stdout": ""}, {"stderr": 42}, {"stdout": "a\x00b"},
                          {"stdout": "x", "stderr": "./x"}):
            with self.subTest(overrides=overrides), self.assertRaises(RmgrError):
                self.submit(**overrides)
        job = self.submit(stdout="run.out")
        self.assertEqual(self.launch(job).returncode, 0)
        self.store.update(job["id"], state="succeeded", exit_code=0)
        self.cwd.rename(self.root / "moved-experiment")
        self.scheduler.config = Config([], 0)
        self.scheduler.backend.unavailable = True
        request = {"op": "submit", "request_id": job["request_id"], "spec": job["spec"], "control_token": self.token}
        self.assertEqual(self.api.dispatch(request)["id"], job["id"])
        self.scheduler.error = {"code": "BACKEND_UNAVAILABLE", "message": "offline"}
        self.assertEqual(self.api.dispatch(request)["id"], job["id"])
        for change in ({"control_token": secrets.token_urlsafe(32)},
                       {"spec": {**job["spec"], "stdout": "other.out"}}):
            with self.assertRaises(RmgrError):
                self.api.dispatch({**request, **change})

    def test_pre_03_schema_logs_and_receipts_survive_migration(self):
        self.store.close()
        self.home = self.root / "old-state"
        prepare_home(self.home)
        spec = JobSpec(["/usr/bin/true"], str(self.cwd)).to_dict()
        self.assertNotIn("stdout", spec)
        job_id = "b" * 32
        with sqlite3.connect(self.home / "jobs.sqlite3") as db:
            db.execute("""CREATE TABLE jobs (id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL,
                spec TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL, started REAL,
                finished REAL, allocation TEXT, reason TEXT, exit_code INTEGER, control_token_hash TEXT)""")
            db.execute("INSERT INTO jobs (id, request_id, spec, state, created, control_token_hash) VALUES (?, ?, ?, ?, ?, ?)",
                       (job_id, "legacy", json.dumps(spec, sort_keys=True), "succeeded", 1.0, token_digest(self.token)))
        directory = self.home / "jobs" / job_id
        directory.mkdir()
        (directory / "stdout.log").write_text("historical output\n")
        self.store = Store(self.home)
        self.scheduler = Scheduler(self.store, self.config, FakeBackend())
        self.api = API(self.scheduler, self.home, {})
        job = self.scheduler.submit("legacy", spec, self.token)
        self.assertEqual(job["stdout_path"], str(directory / "stdout.log"))
        self.assertEqual(self.api.dispatch({"op": "logs", "job_id": job_id})["text"], "historical output\n")
        self.assertEqual(len(self.store.all()), 1)

    def test_runner_accepts_pre_upgrade_manifest(self):
        job = self.submit()
        result = self.launch(job, legacy_manifest=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        directory = self.home / "jobs" / job["id"]
        self.assertEqual((directory / "stdout.log").read_text(), "hello\n")
        self.assertEqual((directory / "stderr.log").read_text(), "warning\n")
        self.assertFalse(Path(job["stdout_path"]).exists())


if __name__ == "__main__":
    unittest.main()
