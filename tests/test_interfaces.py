from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import fcntl
import os
import secrets
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from rmgr.backend import Observation, SystemdBackend
from rmgr.cli import main, memory_size, parser, submission
from rmgr.daemon import API
from rmgr.models import Config, GIB, RmgrError, prepare_home
from rmgr.runner import environment, parse_cpus, verify_limits
from rmgr.scheduler import Scheduler
from rmgr.service import unit_text
from rmgr.store import Store
from tests.test_scheduler import FakeBackend


class InterfaceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.home = Path(self.directory.name)
        prepare_home(self.home)
        self.store = Store(self.home)
        self.config = Config([0, 1], 4 * GIB)
        self.scheduler = Scheduler(self.store, self.config, FakeBackend())
        self.api = API(self.scheduler, self.home, {"cpu_cpuset": True})
        self.token = secrets.token_urlsafe(32)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def submit(self):
        return self.api.dispatch({"op": "submit", "request_id": "test", "control_token": self.token, "spec": {
            "argv": ["/usr/bin/true"], "cwd": str(self.home)}})

    def test_logs_have_bounded_byte_cursors(self):
        job = self.submit()
        directory = self.home / "jobs" / job["id"]
        directory.mkdir()
        (directory / "stdout.log").write_bytes(b"first\nsecond\n")
        first = self.api.dispatch({"op": "logs", "job_id": job["id"], "limit": 6})
        second = self.api.dispatch({"op": "logs", "job_id": job["id"], "offset": first["next_offset"]})
        self.assertEqual(first["text"], "first\n")
        self.assertTrue(first["more"])
        self.assertEqual(second["text"], "second\n")
        self.assertFalse(second["more"])
        with self.assertRaises(RmgrError):
            self.api.dispatch({"op": "logs", "job_id": job["id"], "stream": "../../config.json"})

    def test_health_reports_degradation_and_submission_is_rejected(self):
        self.assertEqual(self.api.dispatch({"op": "health"})["status"], "ready")
        self.scheduler.error = {"code": "BACKEND_UNAVAILABLE", "message": "Disconnected"}
        self.assertFalse(self.api.dispatch({"op": "health"})["accepting_jobs"])
        with self.assertRaises(RmgrError):
            self.submit()

    def test_health_checks_backend_even_when_queue_is_empty(self):
        self.scheduler.backend.unavailable = True
        health = self.api.dispatch({"op": "health"})
        self.assertEqual(health["status"], "degraded")
        self.assertEqual(health["error"]["code"], "BACKEND_UNAVAILABLE")

    def test_health_updates_legacy_gpu_evidence_without_claiming_hardware_access(self):
        cached = {"cpu_cpuset": True, "gpu_device_access": False, "gpu_reservation": False}
        for gpu_count in (0, 1):
            with self.subTest(gpu_count=gpu_count):
                self.scheduler.config = Config([0, 1], 4 * GIB, gpu_count)
                api = API(self.scheduler, self.home, cached)
                report = api.dispatch({"op": "health"})["enforcement"]
                self.assertNotIn("gpu_device_access", report)
                self.assertFalse(report["gpu_device_isolation"])
                self.assertEqual(report["gpu_reservation"], bool(gpu_count))
                self.assertEqual(report["gpu_availability"], "not_checked")
                self.assertIn("not that the GPU is inaccessible", report["gpu_note"])
                self.assertTrue(report["cpu_cpuset"])
        self.assertIn("gpu_device_access", cached)

    def test_doctor_explains_gpu_limits_after_successful_cpu_memory_probe(self):
        for gpu_count in (0, 1):
            with self.subTest(gpu_count=gpu_count):
                config = {"cpu_ids": [0], "memory_bytes": GIB, "gpu_count": gpu_count}
                (self.home / "config.json").write_text(json.dumps(config))
                with patch.object(SystemdBackend, "launch"), \
                     patch.object(SystemdBackend, "observe", return_value=Observation("succeeded")), \
                     patch.object(SystemdBackend, "cancel"), \
                     patch.object(SystemdBackend, "forget"), redirect_stdout(StringIO()) as output:
                    code = main(["--home", str(self.home), "doctor", "--json"])
                self.assertEqual(code, 0)
                report = json.loads(output.getvalue())["enforcement"]
                self.assertNotIn("gpu_device_access", report)
                self.assertFalse(report["gpu_device_isolation"])
                self.assertEqual(report["gpu_reservation"], bool(gpu_count))
                self.assertEqual(report["gpu_availability"], "not_checked")
                expected = "one managed GPU job" if gpu_count else "set gpu_count=1"
                self.assertIn(expected, report["gpu_note"])

    def test_list_pagination(self):
        self.submit()
        for number in range(3):
            self.scheduler.submit(str(number), {"argv": ["/usr/bin/true"], "cwd": str(self.home)}, secrets.token_urlsafe(32))
        first = self.api.dispatch({"op": "list", "limit": 2})
        second = self.api.dispatch({"op": "list", "limit": 2, "offset": first["next_offset"]})
        self.assertEqual(first["total"], 4)
        self.assertIsNone(second["next_offset"])
        self.assertFalse({j["id"] for j in first["jobs"]} & {j["id"] for j in second["jobs"]})

    def test_cli_resolves_conda_executable_without_following_venv_symlink(self):
        executable = self.home / "python"
        executable.symlink_to(sys.executable)
        args = parser().parse_args(["submit", "--receipt", str(self.home / "receipt.json"), "--cores", "2", "--memory", "2GiB", "--", str(executable), "-V"])
        spec = submission(args)
        self.assertEqual(spec["argv"][0], str(executable))
        self.assertEqual(spec["memory_bytes"], 2 * GIB)

    def test_cli_wait_distinguishes_wait_deadline_from_job_failure(self):
        for state, expected in (("running", 3), ("failed", 4), ("succeeded", 0)):
            with self.subTest(state=state), patch("rmgr.cli.request", return_value={"state": state}), redirect_stdout(StringIO()) as output:
                code = main(["wait", "a" * 32, "--timeout", "0", "--json"])
                self.assertEqual(code, expected)
                self.assertEqual(json.loads(output.getvalue())["finished"], state != "running")

    def test_cli_errors_include_submission_key_for_safe_retry(self):
        with patch("rmgr.cli.request", side_effect=RmgrError("DAEMON_UNAVAILABLE", "offline")), redirect_stderr(StringIO()) as output:
            code = main(["submit", "--receipt", str(self.home / "receipt.json"), "--request-id", "my-experiment", "--", "/usr/bin/true"])
        self.assertEqual(code, 2)
        self.assertIn("request_id=my-experiment", output.getvalue())

    def test_doctor_does_not_launch_a_probe_while_daemon_holds_lock(self):
        with (self.home / "daemon.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch("rmgr.cli.SystemdBackend") as backend, redirect_stdout(StringIO()) as output:
                code = main(["--home", str(self.home), "doctor", "--json"])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(output.getvalue())["error"]["code"], "BUSY")
            backend.assert_not_called()

    def test_runner_refuses_unenforced_limits(self):
        (self.home / "cpuset.cpus.effective").write_text("2-3,8\n")
        (self.home / "memory.max").write_text(str(GIB))
        (self.home / "memory.swap.max").write_text("0")
        verify_limits(self.home, [2, 3, 8], GIB)
        with self.assertRaisesRegex(RuntimeError, "CPU allocation"):
            verify_limits(self.home, [2, 3], GIB)
        (self.home / "memory.max").write_text("max")
        with self.assertRaisesRegex(RuntimeError, "memory limit"):
            verify_limits(self.home, [2, 3, 8], GIB)

    def test_runner_explains_missing_controllers_and_swap_support(self):
        limits = {"cpuset.cpus.effective": "0", "memory.max": str(GIB), "memory.swap.max": "0"}
        for missing, expected in (("cpuset.cpus.effective", "cpuset controller"),
                                  ("memory.max", "memory controller"),
                                  ("memory.swap.max", "swap accounting support")):
            with self.subTest(missing=missing):
                for filename, value in limits.items():
                    (self.home / filename).write_text(value)
                (self.home / missing).unlink()
                with self.assertRaisesRegex(RuntimeError, expected) as error:
                    verify_limits(self.home, [0], GIB)
                if missing != "memory.swap.max":
                    self.assertIn(f"user@{os.getuid()}.service", str(error.exception))
                    self.assertIn("Delegate=cpu cpuset memory pids", str(error.exception))

    def test_runner_distinguishes_missing_cgroup_from_missing_controller(self):
        with self.assertRaisesRegex(RuntimeError, "Job cgroup disappeared"):
            verify_limits(self.home / "gone", [0], GIB)

    def test_environment_does_not_inherit_agent_secrets_or_override_gpu_policy(self):
        spec = {"cores": 2, "gpu": False, "env": {"OMP_NUM_THREADS": "1", "CUDA_VISIBLE_DEVICES": "0"}}
        with patch.dict(os.environ, {"SECRET_FOR_TEST": "sensitive"}):
            env = environment(spec)
        self.assertNotIn("SECRET_FOR_TEST", env)
        self.assertEqual(env["OMP_NUM_THREADS"], "1")
        self.assertEqual(env["OPENBLAS_NUM_THREADS"], "2")
        self.assertEqual(env["CUDA_VISIBLE_DEVICES"], "")

    def test_unit_quotes_paths_without_shell_expansion(self):
        unit = unit_text(Path('/tmp/with space/$HOME/%i/"quote'))
        self.assertIn("$$HOME/%%i/\\\"quote", unit)
        self.assertIn("Restart=on-failure", unit)
        self.assertNotIn("sudo", unit)


class BackendTests(unittest.TestCase):
    def test_launch_sets_limits_for_entire_process_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            prepare_home(home)
            job = {"id": "a" * 32, "allocation": [2, 4], "spec": {
                "memory_bytes": GIB, "timeout_seconds": 30, "argv": ["/usr/bin/true"]}}
            with patch("rmgr.backend.command", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
                SystemdBackend(home).launch(job)
            argv = run.call_args.args[0]
            for required in ("AllowedCPUs=2 4", f"MemoryMax={GIB}", "MemorySwapMax=0",
                             "ExitType=cgroup", "KillMode=control-group", "OOMPolicy=kill", "RuntimeMaxSec=30s"):
                self.assertIn(required, argv)
            self.assertIn("--expand-environment=no", argv)

    def test_terminal_classification(self):
        for result, expected in (("success", "succeeded"), ("oom-kill", "oom"), ("timeout", "timed_out"), ("exit-code", "failed")):
            with self.subTest(result=result), tempfile.TemporaryDirectory() as directory:
                output = f"LoadState=loaded\nActiveState=failed\nSubState=failed\nResult={result}\nExecMainStatus=0\nExecMainCode=1\nControlGroup=\n"
                with patch("rmgr.backend.command", return_value=subprocess.CompletedProcess([], 0, output, "")):
                    observation = SystemdBackend(Path(directory)).observe({"id": "a" * 32})
                self.assertEqual(observation.state, expected)

    def test_bus_failure_is_not_mistaken_for_missing_unit(self):
        with patch("rmgr.backend.command", return_value=subprocess.CompletedProcess([], 1, "", "Cannot connect")):
            with self.assertRaisesRegex(RmgrError, "Cannot connect"):
                SystemdBackend(Path("/tmp")).observe({"id": "a" * 32})


if __name__ == "__main__":
    unittest.main()
