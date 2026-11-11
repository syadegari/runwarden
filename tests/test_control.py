"""Test the two-agent API boundary and loss/retry behavior, not just token helpers."""

from contextlib import closing, redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import secrets
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from rmgr.cli import main, parser
from rmgr.control import create_receipt, read_receipt, token_digest
from rmgr.daemon import API
from rmgr.manual import COMMAND_HELP, manual
from rmgr.models import Config, GIB, RmgrError, prepare_home
from rmgr.scheduler import Scheduler
from rmgr.store import Store
from tests.test_scheduler import FakeBackend


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.home = Path(self.directory.name)
        prepare_home(self.home)
        self.store = Store(self.home)
        self.backend = FakeBackend()
        self.scheduler = Scheduler(self.store, Config([0, 1], 4 * GIB), self.backend)
        self.api = API(self.scheduler, self.home, {})
        self.spec = {"argv": ["/usr/bin/true"], "cwd": str(self.home)}
        self.alice = secrets.token_urlsafe(32)
        self.bob = secrets.token_urlsafe(32)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def submit(self, key="alice-job", token=None):
        return self.api.dispatch({"op": "submit", "request_id": key, "spec": self.spec,
                                  "control_token": self.alice if token is None else token})

    def test_other_agent_cannot_cancel_queued_or_running_job(self):
        job = self.submit()
        other = self.submit("bob-job", self.bob)
        for state in ("queued", "running"):
            with self.subTest(state=state):
                for token in (None, "not-a-token", self.bob):
                    with self.assertRaises(RmgrError):
                        self.api.dispatch({"op": "cancel", "job_id": job["id"], "control_token": token})
                self.assertEqual(self.store.get(job["id"])["state"], state)
                self.assertEqual(self.backend.stops, [])
            self.scheduler.tick()
        self.api.dispatch({"op": "cancel", "job_id": job["id"], "control_token": self.alice})
        self.assertEqual(self.store.get(job["id"])["state"], "cancelling")
        self.assertEqual(self.store.get(other["id"])["state"], "running")
        self.assertEqual(self.backend.stops, [job["id"]])

    def test_retry_requires_original_token_and_does_not_coalesce_other_agent(self):
        job = self.submit()
        with self.assertRaisesRegex(RmgrError, "does not authorize"):
            self.submit(token=self.bob)
        self.assertEqual(self.submit()["id"], job["id"])
        self.assertEqual(len(self.store.all()), 1)

    def test_cancel_is_authorized_even_when_job_is_already_terminal(self):
        job = self.submit()
        self.scheduler.cancel(job["id"], self.alice)
        with self.assertRaises(RmgrError):
            self.scheduler.cancel(job["id"], self.bob)
        self.assertEqual(self.scheduler.cancel(job["id"], self.alice)["state"], "cancelled")

    def test_tokens_and_hashes_are_absent_from_public_and_runner_data(self):
        job = self.submit()
        self.scheduler.tick()
        responses = [self.api.dispatch({"op": op, "job_id": job["id"]})
                     for op in ("list", "inspect", "logs", "health", "resources")]
        responses.extend([job, self.backend.launches])
        serialized = json.dumps(responses)
        self.assertNotIn(self.alice, serialized)
        self.assertNotIn(token_digest(self.alice), serialized)
        self.assertNotIn("control_token_hash", serialized)
        self.assertNotIn("control_token", serialized)
        row = self.store.db.execute("SELECT control_token_hash FROM jobs").fetchone()
        self.assertEqual(row[0], token_digest(self.alice))
        self.store.close()
        self.store = Store(self.home)
        self.assertEqual(self.store.authorize(job["id"], self.alice)["id"], job["id"])

    def test_receipt_survives_lost_reply_and_retry_returns_original_job(self):
        receipt_path = self.home / "agent-a.receipt.json"
        accepted = []

        def lose_response(home, payload):
            # Client must persist the secret before the server can commit anything.
            receipt = read_receipt(receipt_path, self.home)
            self.assertEqual(payload["control_token"], receipt["control_token"])
            accepted.append(self.api.dispatch(payload))
            raise RmgrError("DAEMON_UNAVAILABLE", "Response lost after commit")

        with patch("rmgr.cli.request", side_effect=lose_response), redirect_stderr(StringIO()) as errors:
            code = main(["--home", str(self.home), "submit", "--receipt", str(receipt_path),
                         "--request-id", "lost-reply", "--", "/usr/bin/true"])
        self.assertEqual(code, 2)
        receipt = read_receipt(receipt_path, self.home)
        self.assertNotIn(receipt["control_token"], errors.getvalue())
        self.assertEqual(receipt_path.stat().st_mode & 0o777, 0o600)
        original_bytes = receipt_path.read_bytes()
        with patch("rmgr.cli.request", side_effect=lambda home, payload: self.api.dispatch(payload)), redirect_stdout(StringIO()) as output:
            code = main(["--home", str(self.home), "retry", "--receipt", str(receipt_path), "--json"])
        result = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(result["id"], accepted[0]["id"])
        self.assertEqual(result["control_token"], receipt["control_token"])
        self.assertEqual(len(self.store.all()), 1)
        self.assertEqual(receipt_path.read_bytes(), original_bytes)
        with patch("rmgr.cli.request", side_effect=lambda home, payload: self.api.dispatch(payload)), redirect_stdout(StringIO()):
            code = main(["--home", str(self.home), "cancel", result["id"], "--receipt", str(receipt_path)])
        self.assertEqual(code, 0)
        self.assertEqual(self.store.get(result["id"])["state"], "cancelled")

    def test_existing_or_unsafe_receipt_is_not_overwritten_or_sent(self):
        path = self.home / "receipt.json"
        create_receipt(path, self.home, self.spec)
        original = path.read_bytes()
        with patch("rmgr.cli.request") as send, redirect_stderr(StringIO()):
            code = main(["--home", str(self.home), "submit", "--receipt", str(path), "--", "/usr/bin/true"])
        self.assertEqual(code, 2)
        send.assert_not_called()
        self.assertEqual(path.read_bytes(), original)
        path.chmod(0o644)
        with self.assertRaisesRegex(RmgrError, "0600"):
            read_receipt(path, self.home)
        path.chmod(0o600)
        with self.assertRaisesRegex(RmgrError, "different state directory"):
            read_receipt(path, self.home / "different")
        link = self.home / "symlink.json"
        link.symlink_to(path)
        with self.assertRaises(OSError):
            read_receipt(link, self.home)

    def test_receipt_write_failure_prevents_submission(self):
        path = self.home / "not-durable.receipt.json"
        with patch("rmgr.control.os.fsync", side_effect=OSError("disk error")), patch("rmgr.cli.request") as send, redirect_stderr(StringIO()):
            code = main(["--home", str(self.home), "submit", "--receipt", str(path), "--", "/usr/bin/true"])
        self.assertEqual(code, 2)
        send.assert_not_called()

    def test_missing_token_cannot_submit_and_stdin_cancel_checks_token(self):
        with self.assertRaises(RmgrError):
            self.api.dispatch({"op": "submit", "request_id": "missing", "spec": self.spec})
        self.assertEqual(self.store.all(), [])
        job = self.submit()
        for token, expected in ((self.bob, 2), (self.alice, 0)):
            with patch("rmgr.cli.request", side_effect=lambda home, payload: self.api.dispatch(payload)), patch("sys.stdin", StringIO(token + "\n")), redirect_stdout(StringIO()) as output, redirect_stderr(StringIO()) as errors:
                code = main(["--home", str(self.home), "cancel", job["id"], "--token-stdin", "--json"])
            self.assertEqual(code, expected)
            self.assertNotIn(token, output.getvalue() + errors.getvalue())

    def test_legacy_schema_upgrade_preserves_history_without_granting_control(self):
        self.store.close()
        path = self.home / "jobs.sqlite3"
        path.unlink()
        with closing(sqlite3.connect(path)) as database:
            database.execute("""CREATE TABLE jobs (
                id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, spec TEXT NOT NULL,
                state TEXT NOT NULL, created REAL NOT NULL, started REAL, finished REAL,
                allocation TEXT, reason TEXT, exit_code INTEGER)""")
            database.execute("INSERT INTO jobs (id, request_id, spec, state, created) VALUES (?, ?, ?, ?, ?)",
                             ("a" * 32, "legacy", json.dumps(self.spec), "succeeded", 1.0))
            database.commit()
        self.store = Store(self.home)
        self.assertEqual(self.store.get("a" * 32)["state"], "succeeded")
        with self.assertRaises(RmgrError) as context:
            self.store.authorize("a" * 32, self.alice)
        self.assertEqual(context.exception.code, "LEGACY_JOB")


class ManualTests(unittest.TestCase):
    def test_instructions_print_offline_text_and_json_without_creating_state(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "absent-state"
            for flags, structured in ((["instructions"], False),
                                      (["instructions", "--json"], True),
                                      (["--json", "instructions"], True)):
                with self.subTest(flags=flags), patch("rmgr.cli.request") as network, \
                     patch("rmgr.cli.prepare_home") as prepare, \
                     redirect_stdout(StringIO()) as output, redirect_stderr(StringIO()) as errors:
                    self.assertEqual(main(["--home", str(home), *flags]), 0)
                    network.assert_not_called()
                    prepare.assert_not_called()
                    self.assertFalse(home.exists())
                    self.assertEqual(errors.getvalue(), "")
                    expected = manual("instructions")
                    if structured:
                        self.assertEqual(json.loads(output.getvalue()), expected)
                    else:
                        self.assertEqual(output.getvalue(), expected["text"] + "\n")
                    for phrase in ("public job ID", "control_token", "handoff", "rmgr cancel",
                                   "rmgr retry", "same Linux account"):
                        self.assertIn(phrase, expected["text"])

    def test_manual_is_offline_and_describes_ownership_and_limits(self):
        with patch("rmgr.cli.request") as network, redirect_stdout(StringIO()) as output:
            self.assertEqual(main(["manual", "all", "--json"]), 0)
        network.assert_not_called()
        result = json.loads(output.getvalue())
        for topic in ("overview", "instructions", "jobs", "ownership", "limits", "operations"):
            self.assertIn(topic, result["topics"])
        self.assertIn("same Linux account", result["text"])
        self.assertIn("rmgr retry", result["text"])
        self.assertIn("not guaranteed to start", result["text"])

    def test_help_lists_the_guide_and_every_command_has_a_description(self):
        help_text = parser().format_help()
        self.assertIn("rmgr manual", help_text)
        for command, description in COMMAND_HELP.items():
            self.assertIn(command, help_text)
            self.assertTrue(description)
        with self.assertRaises(RmgrError):
            manual("../secret")


if __name__ == "__main__":
    unittest.main()
