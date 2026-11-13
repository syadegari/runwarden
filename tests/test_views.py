from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from io import StringIO
import json
import unittest
from unittest.mock import patch

from rmgr.cli import main
from rmgr.models import GIB, RmgrError, TERMINAL


class TextViewTests(unittest.TestCase):
    """Exercise the CLI against the old daemon protocol, including its 100-row cap."""

    def setUp(self):
        self.jobs = [self.job(1, "running"), self.job(2, "queued")]
        self.jobs[1]["spec"]["memory_bytes"] = 44 * GIB
        self.jobs[1]["reason"] = "Waiting for memory reservation"
        self.resources = {"physical_cores": 16, "cpu_ids": list(range(16)),
                          "available_cpu_ids": list(range(8, 16)), "memory_bytes": 47 * GIB,
                          "available_memory_bytes": 7 * GIB, "gpu_count": 0,
                          "gpu_available": False, "exclusive_running": False}
        self.health = {"status": "ready", "version": "0.2.0", "accepting_jobs": True,
                       "error": None, "running_jobs": 1, "queued_jobs": 1,
                       "enforcement": {"cpu_cpuset": True, "gpu_device_access": False}}
        self.calls = []

    @staticmethod
    def job(number, state):
        terminal = state in TERMINAL
        return {"id": f"{number:032x}", "created": number,
                "started": None if state in {"queued", "blocked"} else 100,
                "finished": 179 if terminal else None,
                "state": state, "reason": None, "exit_code": 0 if state == "succeeded" else None,
                "allocation": None if state == "queued" else list(range(8)),
                "spec": {"argv": ["/tmp/experiment/fill_search.sh", "argument with spaces"],
                         "cwd": "/tmp/experiment", "cores": 8, "memory_bytes": 40 * GIB,
                         "after_success": [], "exclusive": False, "gpu": False,
                         "timeout_seconds": 3600}}

    def request(self, home, payload):
        self.calls.append(dict(payload))
        op = payload["op"]
        if op in {"status", "list_snapshot"}:
            raise RmgrError("INVALID_REQUEST", "Unknown operation")
        if op == "health":
            return deepcopy(self.health)
        if op == "resources":
            return deepcopy(self.resources)
        if op == "inspect":
            return deepcopy(next(job for job in self.jobs if job["id"] == payload["job_id"]))
        if op == "list":
            self.assertLessEqual(payload["limit"], 100)
            self.assertIsInstance(payload.get("state", ""), str)
            jobs = [job for job in self.jobs if not payload.get("state") or job["state"] == payload["state"]]
            start, end = payload["offset"], payload["offset"] + payload["limit"]
            return deepcopy({"jobs": jobs[start:end], "total": len(jobs),
                             "next_offset": end if end < len(jobs) else None})
        self.fail(f"Unexpected operation: {op}")

    def run_cli(self, *argv, request=None):
        with patch("rmgr.cli.request", side_effect=request or self.request), \
                patch("rmgr.cli.time.time", return_value=180), \
                redirect_stdout(StringIO()) as stdout, redirect_stderr(StringIO()) as stderr:
            code = main(["--home", "/tmp/rmgr-view-test", *argv])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_status_explains_memory_reservation_without_an_upgrade(self):
        code, output, error = self.run_cli("status")
        self.assertEqual((code, error), (0, ""))
        for expected in ("8 / 16 reserved", "40 GiB / 47 GiB reserved", "7 GiB available",
                         "44 GiB", "Waiting for memory reservation", "not configured", self.jobs[0]["id"]):
            self.assertIn(expected, output)
        self.assertNotIn("control_token", output)
        self.assertEqual({call["op"] for call in self.calls}, {"status", "list_snapshot", "list", "resources", "health"})

    def test_status_json_contains_public_combined_data(self):
        code, output, _ = self.run_cli("status", "--json")
        result = json.loads(output)
        self.assertEqual(code, 0)
        self.assertEqual(result["jobs"], self.jobs)
        self.assertEqual(result["resources"], self.resources)
        self.assertIn("observed_at", result)

    def test_degraded_health_and_status_are_nonzero_and_explain_error(self):
        self.health.update(status="degraded", accepting_jobs=False,
                           error={"code": "BACKEND_UNAVAILABLE", "message": "Disconnected"})
        for args in (("health", "--text"), ("status",)):
            with self.subTest(args=args):
                code, output, _ = self.run_cli(*args)
                self.assertEqual(code, 2)
                self.assertIn("Disconnected", output)
                self.assertIn("not accepting jobs", output)

    def test_existing_json_commands_keep_their_shapes(self):
        for command, expected in (("health", self.health), ("resources", self.resources)):
            for flag in ([], ["--json"]):
                self.assertEqual(json.loads(self.run_cli(command, *flag)[1]), expected)
        self.assertEqual(json.loads(self.run_cli("inspect", self.jobs[0]["id"], "--json")[1]), self.jobs[0])
        result = json.loads(self.run_cli("list", "--state", "running", "--json")[1])
        self.assertEqual(result, {"jobs": self.jobs[:1], "total": 1, "next_offset": None, "consistency": "best_effort_legacy"})

    def test_all_pages_and_footer(self):
        self.jobs = [self.job(i, "running") for i in range(205)]
        output = self.run_cli("list", "--text")[1]
        self.assertIn("Showing 100 of 205", output)
        self.assertIn("--offset 100", output)
        result = json.loads(self.run_cli("list", "--all", "--offset", "3", "--limit", "17", "--json")[1])
        self.assertEqual(result["jobs"], self.jobs[3:])
        self.assertIsNone(result["next_offset"])

    def test_multiple_states_are_combined_before_pagination(self):
        self.jobs = [self.job(i, "running" if i % 2 else "queued") for i in range(250)]
        result = json.loads(self.run_cli("list", "--state", "running", "--state", "queued",
                                         "--offset", "99", "--limit", "3", "--json")[1])
        self.assertEqual(result["jobs"], self.jobs[99:102])
        self.assertEqual((result["total"], result["next_offset"]), (250, 102))
        status = json.loads(self.run_cli("status", "--json")[1])
        self.assertEqual(status["jobs"], self.jobs)

    def test_active_and_finished_filters_include_all_lifecycle_states(self):
        states = ["queued", "starting", "running", "cancelling", *sorted(TERMINAL)]
        self.jobs = [self.job(i, state) for i, state in enumerate(states)]
        for flag, expected in (("--active", states[:4]), ("--finished", states[4:])):
            result = json.loads(self.run_cli("list", flag, "--json")[1])
            self.assertEqual([job["state"] for job in result["jobs"]], expected)

    def test_empty_jobs_and_gpu_reservation(self):
        self.jobs = []
        self.assertIn("No matching jobs", self.run_cli("list", "--text")[1])
        self.resources.update(gpu_count=1, gpu_available=False)
        output = self.run_cli("resources", "--text")[1]
        self.assertIn("1 / 1 reserved", output)
        self.assertIn("cooperative reservation", output)

    def test_inspect_old_paths_and_new_paths_and_terminal_runtime(self):
        job = self.job(3, "succeeded")
        self.jobs = [job]
        output = self.run_cli("inspect", job["id"], "--text")[1]
        self.assertIn(f"/tmp/rmgr-view-test/jobs/{job['id']}/stdout.log", output)
        self.assertIn("1m 19s", output)
        self.assertIn("Exit code:    0", output)
        job.update(stdout_path="/tmp/custom.out", stderr_path="/tmp/custom.err")
        output = self.run_cli("inspect", job["id"], "--text")[1]
        self.assertIn("/tmp/custom.out", output)
        self.assertNotIn("/jobs/", output)

    def test_wide_shows_quoted_arguments_and_escapes_terminal_controls(self):
        self.jobs[0]["spec"]["argv"] = ["/tmp/long/path/program", "a b", "x" * 100, "\x1b[2J"]
        self.jobs[0]["reason"] = "bad\nreason" + "!" * 80
        narrow = self.run_cli("list", "--text")[1]
        wide = self.run_cli("list", "--text", "--wide")[1]
        self.assertNotIn("x" * 100, narrow)
        self.assertIn("/tmp/long/path/program 'a b'", wide)
        self.assertIn("x" * 100, wide)
        self.assertNotIn("\x1b", wide)
        self.assertIn("bad\\nreason", wide)

    def test_logs_stdout_is_only_output_and_notices_go_to_stderr(self):
        response = {"text": "exact output\nno final newline", "more": True, "next_offset": 123}
        code, output, error = self.run_cli("logs", "a" * 32, "--text", "--stream", "stderr",
                                           request=lambda *_: response)
        self.assertEqual((code, output), (0, response["text"]))
        self.assertIn("--offset 123", error)
        response.update(text="", more=False, available=False)
        _, output, error = self.run_cli("logs", "a" * 32, "--text", request=lambda *_: response)
        self.assertEqual(output, "")
        self.assertIn("not available", error)

    def test_watch_json_stream_and_clean_interrupt_without_terminal_escapes(self):
        with patch("rmgr.cli.time.sleep", side_effect=[None, KeyboardInterrupt]) as sleep:
            code, output, error = self.run_cli("status", "--watch", "2", "--json")
        self.assertEqual((code, error), (130, ""))
        self.assertEqual(len([json.loads(line) for line in output.splitlines()]), 2)
        sleep.assert_called_with(2)
        with patch("rmgr.cli.time.sleep", side_effect=KeyboardInterrupt):
            code, output, _ = self.run_cli("status", "--watch", "2")
        self.assertEqual(code, 130)
        self.assertNotIn("\x1b", output)

    def test_invalid_options_are_rejected_before_contacting_daemon(self):
        for args in (("status", "--watch", "0"), ("status", "--watch", "nan"),
                     ("status", "--watch", "inf"), ("status", "--text", "--json"),
                     ("--json", "list", "--text"), ("list", "--wide"),
                     ("list", "--active", "--state", "running")):
            with self.subTest(args=args), self.assertRaises(SystemExit) as error:
                self.run_cli(*args)
            self.assertEqual(error.exception.code, 2)
        self.assertEqual(self.calls, [])
        for args in (("--limit", "101"), ("--offset", "-1")):
            self.assertEqual(self.run_cli("list", *args)[0], 2)
        self.assertEqual(self.calls, [])

    def test_connection_error_remains_explicit(self):
        def unavailable(*args):
            raise RmgrError("DAEMON_UNAVAILABLE", "offline")
        code, output, error = self.run_cli("status", request=unavailable)
        self.assertEqual((code, output), (2, ""))
        self.assertEqual(json.loads(error)["error"]["code"], "DAEMON_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
