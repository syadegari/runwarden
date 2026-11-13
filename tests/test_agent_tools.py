"""Agent workflow and operator authorization checks using isolated state only."""

import asyncio
from contextlib import redirect_stderr, redirect_stdout
import errno
from io import StringIO
import json
import os
from pathlib import Path
import secrets
import sqlite3
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from rmgr.backend import SystemdBackend, Observation
from rmgr.cli import main, list_jobs
from rmgr.control import create_receipt
from rmgr.daemon import API, connection
from rmgr.models import Config, GIB, RmgrError, prepare_home
from rmgr.scheduler import Scheduler
from rmgr.store import Store
from rmgr.preview import PreviewAPI
from tests.test_scheduler import FakeBackend


class AgentToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rmgr-agents-")
        self.home = Path(self.temp.name)
        prepare_home(self.home)
        self.store = Store(self.home)
        self.backend = FakeBackend()
        self.scheduler = Scheduler(self.store, Config(list(range(16)), 47 * GIB), self.backend)
        self.api = API(self.scheduler, self.home, {})

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def submit(self, key="a", **spec):
        return self.scheduler.submit(key, {"argv": ["/usr/bin/true"], "cwd": str(self.home), **spec}, secrets.token_urlsafe(32))

    def cli(self, *args, peer_uid=None):
        def dispatch(home, payload, **kwargs):
            return self.api.dispatch(payload, peer_uid=peer_uid)
        with patch("rmgr.cli.request", side_effect=dispatch), redirect_stdout(StringIO()) as out, redirect_stderr(StringIO()) as err:
            code = main(["--home", str(self.home), *args])
        return code, out.getvalue(), err.getvalue()

    def legacy_log(self, job, data=b"0123456789"):
        with self.store.db:
            self.store.db.execute("UPDATE jobs SET stdout_path = NULL, stderr_path = NULL WHERE id = ?", (job["id"],))
        directory = self.home / "jobs" / job["id"]
        directory.mkdir()
        (directory / "stdout.log").write_bytes(data)
        (directory / "stderr.log").write_bytes(data)

    def test_launch_preparation_failure_is_explicit_and_queue_continues(self):
        first = self.submit("first")
        second = self.submit("second")
        launch = self.backend.launch
        def prepare(job):
            if job["id"] == first["id"]:
                raise RmgrError("LAUNCH_PREPARATION_FAILED", "read-only directory")
            launch(job)
        with patch.object(self.backend, "launch", side_effect=prepare):
            self.scheduler.tick()
        failed = self.store.get(first["id"])
        self.assertEqual(failed["state"], "failed")
        self.assertIsNone(failed["started"])
        self.assertIsNone(failed["allocation"])
        self.assertEqual(self.store.get(second["id"])["state"], "running")
        self.assertIsNone(self.scheduler.error)
        with patch("rmgr.backend.command") as command, patch.object(Path, "mkdir", side_effect=PermissionError("denied")):
            with self.assertRaises(RmgrError) as exc:
                SystemdBackend(self.home).launch(second)
        self.assertEqual(exc.exception.code, "LAUNCH_PREPARATION_FAILED")
        command.assert_not_called()

    def test_uncertain_storage_failure_keeps_reservations_without_replaying(self):
        job = self.submit(exclusive=True)
        later = self.submit("later")
        launch = self.backend.launch
        def lost(job):
            launch(job)
            raise OSError(errno.ENOSPC, "No space left")
        with patch.object(self.backend, "launch", side_effect=lost):
            self.scheduler.tick()
        self.assertEqual(self.scheduler.error["code"], "STORAGE_UNAVAILABLE")
        self.assertEqual(self.store.get(job["id"])["state"], "starting")
        self.scheduler.tick()
        self.assertEqual(self.store.get(job["id"])["state"], "running")
        self.assertEqual(self.store.get(later["id"])["state"], "queued")
        self.assertEqual(len(self.backend.launches), 1)
        with patch.object(self.store, "all", side_effect=sqlite3.OperationalError("disk full")):
            self.scheduler.tick()
        self.assertEqual(self.scheduler.error["code"], "STORAGE_UNAVAILABLE")

    def test_rejection_and_uncertain_response_have_different_recovery(self):
        receipt = self.home / "bad.json"
        code, out, err = self.cli("submit", "--receipt", str(receipt), "--cores", "17", "--", "/usr/bin/true")
        self.assertEqual((code, out), (2, ""))
        error = json.loads(err)["error"]
        self.assertEqual(error["acceptance"], "rejected")
        self.assertEqual(error["recovery"], "correct_request_use_new_receipt")
        with patch.object(self.api, "dispatch", side_effect=RmgrError("DAEMON_UNAVAILABLE", "lost response")):
            _, _, err = self.cli("retry", "--receipt", str(receipt))
        self.assertEqual(json.loads(err)["error"]["acceptance"], "unknown")
        self.assertEqual(json.loads(err)["error"]["recovery"], "retry_same_receipt")

    def test_spec_rejects_explicit_defaults_before_creating_receipts(self):
        spec = self.home / "spec.json"
        spec.write_text(json.dumps({"argv": ["/usr/bin/true"], "cwd": str(self.home)}))
        for options in (("--cores", "1"), ("--memory", "1G"), ("--timeout", "3600"), ("--gpu",), ("--cwd", str(self.home))):
            with self.subTest(options=options):
                receipt = self.home / "never-created.json"
                self.assertEqual(self.cli("submit", "--receipt", str(receipt), "--spec", str(spec), *options)[0], 2)
                self.assertFalse(receipt.exists())
        self.assertEqual(self.store.all(), [])

    def test_validate_is_read_only_and_reports_effective_environment(self):
        before = set(self.home.rglob("*"))
        code, output, error = self.cli("validate", "--cores", "8", "--env", "OMP_NUM_THREADS=4", "--", "/usr/bin/true")
        self.assertEqual((code, error), (0, ""))
        result = json.loads(output)
        self.assertFalse(result["reserved"])
        self.assertEqual(result["spec"]["cores"], 8)
        self.assertEqual(result["effective_environment"]["OMP_NUM_THREADS"], "4")
        self.assertEqual(result["effective_environment"]["OPENBLAS_NUM_THREADS"], "8")
        self.assertEqual(self.store.all(), [])
        self.assertEqual(before, set(self.home.rglob("*")))
        self.assertEqual(self.backend.launches, [])
        taken = self.home / "exists.log"
        taken.write_text("preserve")
        self.assertEqual(self.cli("validate", "--stdout", str(taken), "--", "/usr/bin/true")[0], 2)
        self.assertEqual(taken.read_text(), "preserve")

    def test_development_launcher_preserves_worktree_cwd(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "rmgr-dev"
        receipt = self.home / "worktree.receipt.json"
        result = subprocess.run([sys.executable, str(script), "--home", str(self.home / "no-daemon"),
                                 "submit", "--receipt", str(receipt), "--", "/usr/bin/true"],
                                cwd=self.home, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(receipt.read_text())["spec"]["cwd"], str(self.home))
        self.assertEqual(self.store.all(), [])

    def test_discovery_is_offline_and_includes_contracts(self):
        with patch("rmgr.cli.request") as request, redirect_stdout(StringIO()) as out:
            self.assertEqual(main(["describe", "submit", "--json"]), 0)
        request.assert_not_called()
        report = json.loads(out.getvalue())
        self.assertEqual(set(report["commands"]), {"submit"})
        self.assertFalse(report["commands"]["submit"]["read_only"])
        self.assertIn("--show-token", [flag for arg in report["commands"]["submit"]["arguments"] for flag in arg["flags"]])
        self.assertIn("cwd", report["job_spec"])
        health = self.api.dispatch({"op": "health"})
        self.assertEqual(health["protocol_version"], 2)
        self.assertIn("operator_cancel", health["features"])

    def test_lookup_never_submits_or_prints_token_and_show_token_is_opt_in(self):
        path = self.home / "receipt.json"
        receipt = create_receipt(path, self.home, {"argv": ["/usr/bin/true"], "cwd": str(self.home)})
        result = json.loads(self.cli("lookup", "--receipt", str(path))[1])
        self.assertFalse(result["found"])
        self.assertEqual(self.store.all(), [])
        result = json.loads(self.cli("retry", "--receipt", str(path))[1])
        self.assertNotIn("control_token", result)
        lookup = self.cli("lookup", "--receipt", str(path))[1]
        self.assertEqual(json.loads(lookup)["job"]["id"], result["id"])
        self.assertNotIn(receipt["control_token"], lookup)
        self.assertEqual(json.loads(self.cli("retry", "--receipt", str(path), "--show-token")[1])["control_token"], receipt["control_token"])
        self.assertEqual(len(self.store.all()), 1)

    def test_snapshot_survives_transitions_and_rejects_expired_or_changed_filters(self):
        jobs = [self.submit(str(i)) for i in range(3)]
        first = self.api.dispatch({"op": "list_snapshot", "states": ["queued"], "limit": 2})
        self.store.update(jobs[0]["id"], state="succeeded")
        self.submit("new")
        second = self.api.dispatch({"op": "list_snapshot", "snapshot_id": first["snapshot_id"], "offset": 2, "limit": 2})
        self.assertEqual([j["id"] for j in first["jobs"] + second["jobs"]], [j["id"] for j in jobs])
        self.assertEqual(second["total"], 3)
        with self.assertRaises(RmgrError):
            self.api.dispatch({"op": "list_snapshot", "snapshot_id": first["snapshot_id"], "states": ["running"]})
        self.api.snapshots.entries.clear()
        with self.assertRaises(RmgrError) as exc:
            self.api.dispatch({"op": "list_snapshot", "snapshot_id": first["snapshot_id"]})
        self.assertEqual(exc.exception.code, "SNAPSHOT_EXPIRED")

    def test_cli_snapshot_pagination_cwd_filter_and_status_consistency(self):
        other = self.home / "worktree"
        other.mkdir()
        a = self.submit("a")
        self.submit("b", cwd=str(other))
        self.submit("c")
        result = json.loads(self.cli("list", "--all", "--limit", "1", "--cwd", str(self.home))[1])
        self.assertEqual(len(result["jobs"]), 2)
        self.assertEqual(result["consistency"], "snapshot")
        self.assertEqual(self.cli("inspect", a["id"], "--cwd-only")[1].strip(), str(self.home))
        self.assertIn(str(other), self.cli("list", "--text", "--wide")[1])
        self.assertTrue(json.loads(self.cli("status", "--json")[1])["snapshot_consistent"])

    def test_structured_queue_reasons_match_reservations(self):
        first = self.submit("first", cores=8, memory_bytes=40 * GIB)
        second = self.submit("second", cores=8, memory_bytes=44 * GIB)
        third = self.submit("third")
        self.scheduler.tick()
        reason = self.store.get(second["id"])["reason_details"]
        self.assertEqual(reason["code"], "MEMORY_RESERVATION")
        self.assertEqual(reason["available_bytes"], 7 * GIB)
        self.assertEqual(reason["blocking_job_ids"], [first["id"]])
        self.assertEqual(self.store.get(third["id"])["reason_details"]["code"], "FIFO_BARRIER")

    def test_log_tail_and_follow_are_bounded_and_keep_offsets(self):
        job = self.submit()
        self.legacy_log(job)
        self.store.update(job["id"], state="succeeded")
        code, output, error = self.cli("logs", job["id"], "--tail", "4", "--limit", "2", "--follow", "--text")
        self.assertEqual((code, output, error), (0, "6789", ""))
        code, output, error = self.cli("logs", job["id"], "--tail", "4", "--limit", "2", "--follow", "--max-bytes", "3", "--text")
        self.assertEqual((code, output), (0, "678"))
        self.assertIn("next offset 9", error)
        self.store.update(job["id"], state="running")
        code, output, error = self.cli("logs", job["id"], "--offset", "10", "--follow", "--timeout", "0", "--text")
        self.assertEqual((code, output), (0, ""))
        self.assertIn("deadline", error)

    def test_old_daemon_does_not_silently_ignore_tail(self):
        with patch.object(self.api, "dispatch", return_value={"features": []}):
            code, _, error = self.cli("logs", "a" * 32, "--tail", "10")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(error)["error"]["code"], "UNSUPPORTED_FEATURE")

    def test_preview_advertises_read_only_and_rejects_mutations(self):
        preview = PreviewAPI(self.scheduler, self.home, {})
        health = preview.dispatch({"op": "health"})
        self.assertFalse(health["accepting_jobs"])
        self.assertNotIn("operator_cancel", health["features"])
        for op in ("submit", "cancel", "admin_cancel"):
            with self.assertRaises(RmgrError) as exc:
                preview.dispatch({"op": op}, peer_uid=0)
            self.assertEqual(exc.exception.code, "PREVIEW_READ_ONLY")
        self.assertEqual(self.store.all(), [])

    def test_operator_cancel_needs_peer_root_and_persists_audit_before_stop(self):
        job = self.submit(exclusive=True)
        self.scheduler.tick()
        payload = {"op": "admin_cancel", "job_id": job["id"], "reason": "operator test", "peer_uid": 0}
        for uid in (None, 1000):
            with self.assertRaises(RmgrError) as exc:
                self.api.dispatch(payload, peer_uid=uid)
            self.assertEqual(exc.exception.code, "OPERATOR_REQUIRED")
        self.assertEqual(self.store.operator_actions(job["id"]), [])
        with patch.object(self.backend, "cancel", side_effect=RmgrError("BACKEND_UNAVAILABLE", "bus down")):
            with self.assertRaises(RmgrError):
                self.api.dispatch(payload, peer_uid=0)
        self.assertEqual(self.store.get(job["id"])["state"], "cancelling")
        self.assertEqual(self.store.operator_actions(job["id"])[0]["reason"], "operator test")
        self.scheduler.tick()
        self.assertEqual(self.backend.stops, [job["id"]])
        self.assertTrue(self.scheduler.resources()["exclusive_running"])
        self.backend.units[job["id"]] = Observation("failed")
        self.scheduler.tick()
        finished = self.store.get(job["id"])
        self.assertEqual(finished["state"], "cancelled")
        self.assertIn("operator test", finished["reason"])
        self.assertEqual(finished["reason_details"]["peer_uid"], 0)
        self.assertEqual(len(self.api.dispatch({"op": "inspect", "job_id": job["id"]})["operator_actions"]), 1)

    def test_operator_cancel_queued_and_terminal_and_cli_without_sudo(self):
        job = self.submit()
        with patch("rmgr.cli.os.geteuid", return_value=1000):
            self.assertEqual(self.cli("admin-cancel", job["id"], "--reason", "test")[0], 2)
        with patch("rmgr.cli.os.geteuid", return_value=0):
            self.assertEqual(self.cli("admin-cancel", job["id"], "--reason", "test", peer_uid=0)[0], 0)
        self.assertEqual(self.store.get(job["id"])["state"], "cancelled")
        self.assertEqual(self.backend.stops, [])
        self.api.dispatch({"op": "admin_cancel", "job_id": job["id"], "reason": "already done"}, peer_uid=0)
        self.assertEqual(len(self.store.operator_actions(job["id"])), 2)

    def test_transport_takes_authority_from_socket_not_json(self):
        job = self.submit()
        async def exercise(uid, payload):
            class Peer:
                def getsockopt(self, *args):
                    return struct.pack("3i", 999, uid, uid)
            class Writer:
                data = b""
                def get_extra_info(self, name): return Peer()
                def write(self, data): self.data += data
                async def drain(self): pass
                def close(self): pass
                async def wait_closed(self): pass
            reader = asyncio.StreamReader()
            reader.feed_data(json.dumps(payload).encode() + b"\n")
            reader.feed_eof()
            writer = Writer()
            with patch("rmgr.daemon.os.getuid", return_value=1000):
                await connection(reader, writer, self.api)
            return json.loads(writer.data)
        payload = {"op": "admin_cancel", "job_id": job["id"], "reason": "root test", "peer_uid": 0}
        self.assertFalse(asyncio.run(exercise(1000, payload))["ok"])
        self.assertFalse(asyncio.run(exercise(2000, payload))["ok"])
        self.assertFalse(asyncio.run(exercise(0, {"op": "health"}))["ok"])
        self.assertTrue(asyncio.run(exercise(0, payload))["ok"])
        self.assertEqual(self.store.get(job["id"])["state"], "cancelled")


if __name__ == "__main__":
    unittest.main()
