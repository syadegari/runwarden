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
import tomllib
import unittest
from unittest.mock import patch

from rmgr.backend import Observation, SystemdBackend
from rmgr import __version__
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
        # Exercise byte cursors for an existing pre-0.3 central log.
        with self.store.db:
            self.store.db.execute("UPDATE jobs SET stdout_path = NULL, stderr_path = NULL WHERE id = ?", (job["id"],))
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
        with self.assertRaises(RmgrError) as error:
            self.submit()
        self.assertEqual(error.exception.code, "BACKEND_UNAVAILABLE")
        self.assertEqual(self.store.all(), [])

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

    def test_health_wait_prints_only_final_readiness(self):
        ready = {"status": "ready", "accepting_jobs": True}
        responses = [RmgrError("DAEMON_UNAVAILABLE", "starting"), {"status": "degraded"}, ready]
        with patch("rmgr.cli.request", side_effect=responses) as request, \
             patch("rmgr.cli.time.sleep"), redirect_stdout(StringIO()) as output, \
             redirect_stderr(StringIO()) as errors:
            self.assertEqual(main(["--home", str(self.home), "health", "--wait", "30", "--json"]), 0)
        self.assertEqual(json.loads(output.getvalue()), ready)
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(request.call_count, 3)
        for call in request.call_args_list:
            self.assertEqual(call.args, (self.home, {"op": "health"}))
            self.assertGreater(call.kwargs["timeout"], 0)
            self.assertLessEqual(call.kwargs["timeout"], 10)

    def test_health_wait_deadline_keeps_last_failure(self):
        for response in (RmgrError("DAEMON_UNAVAILABLE", "still offline"), {"status": "degraded"}):
            with self.subTest(response=response):
                now = [0.0]
                def advance(seconds):
                    now[0] += seconds
                kwargs = {"side_effect": response} if isinstance(response, Exception) else {"return_value": response}
                with patch("rmgr.cli.request", **kwargs), \
                     patch("rmgr.cli.time.monotonic", side_effect=lambda: now[0]), \
                     patch("rmgr.cli.time.sleep", side_effect=advance), \
                     redirect_stdout(StringIO()) as output, redirect_stderr(StringIO()) as errors:
                    self.assertEqual(main(["health", "--wait", "1", "--json"]), 2)
                self.assertEqual(now[0], 1)
                if isinstance(response, Exception):
                    self.assertEqual(output.getvalue(), "")
                    self.assertEqual(json.loads(errors.getvalue())["error"]["message"], "still offline")
                else:
                    self.assertEqual(json.loads(output.getvalue()), response)
                    self.assertEqual(errors.getvalue(), "")

    def test_health_wait_does_not_replace_diagnostics_with_a_deadline_request(self):
        degraded = {"status": "degraded", "error": {"code": "BACKEND_UNAVAILABLE", "message": "missing cpuset"}}
        for oversleep in (0, 0.1):
            with self.subTest(oversleep=oversleep):
                now = [0.0]
                starts = []

                def request(home, payload, *, timeout):
                    starts.append(now[0])
                    self.assertLessEqual(timeout, 1 - now[0])
                    now[0] += min(0.01, timeout)
                    if timeout < 0.01:
                        raise RmgrError("DAEMON_UNAVAILABLE", "socket timed out")
                    return degraded

                def sleep(seconds):
                    now[0] += seconds + oversleep

                with patch("rmgr.cli.request", side_effect=request), \
                     patch("rmgr.cli.time.monotonic", side_effect=lambda: now[0]), \
                     patch("rmgr.cli.time.sleep", side_effect=sleep), \
                     redirect_stdout(StringIO()) as output, redirect_stderr(StringIO()) as errors:
                    self.assertEqual(main(["health", "--wait", "1", "--json"]), 2)
                self.assertEqual(json.loads(output.getvalue()), degraded)
                self.assertEqual(errors.getvalue(), "")
                self.assertEqual(len(starts), 2)
                self.assertTrue(all(start < 1 for start in starts))

    def test_health_wait_handles_expiry_before_first_request(self):
        with patch("rmgr.cli.time.monotonic", side_effect=[0, 1]), \
             patch("rmgr.cli.request") as request, redirect_stderr(StringIO()) as errors:
            self.assertEqual(main(["health", "--wait", "0.001", "--json"]), 2)
        request.assert_not_called()
        self.assertEqual(json.loads(errors.getvalue())["error"]["code"], "DAEMON_UNAVAILABLE")

    def test_health_wait_does_not_retry_unrelated_errors_or_change_one_shot(self):
        for argv, error in ((["health", "--wait", "30"], RmgrError("FORBIDDEN", "denied")),
                            (["health"], RmgrError("DAEMON_UNAVAILABLE", "offline"))):
            with self.subTest(argv=argv), patch("rmgr.cli.request", side_effect=error) as request, \
                 patch("rmgr.cli.time.sleep") as sleep, redirect_stderr(StringIO()):
                self.assertEqual(main(argv), 2)
                request.assert_called_once()
                sleep.assert_not_called()

    def test_health_wait_rejects_invalid_durations(self):
        for duration in ("0", "-1", "nan", "inf"):
            with self.subTest(duration=duration), redirect_stderr(StringIO()), \
                 patch("rmgr.cli.request") as request, self.assertRaises(SystemExit) as error:
                main(["health", "--wait", duration])
            self.assertEqual(error.exception.code, 2)
            request.assert_not_called()

    def test_doctor_failure_points_to_offline_host_setup(self):
        (self.home / "config.json").write_text(json.dumps({"cpu_ids": [0], "memory_bytes": GIB, "gpu_count": 0}))
        with patch.object(SystemdBackend, "check", side_effect=RmgrError("ENFORCEMENT_UNAVAILABLE", "missing cpuset")), \
             redirect_stdout(StringIO()) as output:
            self.assertEqual(main(["--home", str(self.home), "doctor", "--json"]), 2)
        report = json.loads(output.getvalue())
        self.assertEqual(report["error"]["code"], "ENFORCEMENT_UNAVAILABLE")
        self.assertIn("rmgr manual host-setup", report["hint"])
        with patch("rmgr.cli.request") as request, redirect_stdout(StringIO()) as guide:
            self.assertEqual(main(["manual", "host-setup"]), 0)
        request.assert_not_called()
        self.assertIn("Delegate=cpu cpuset memory pids", guide.getvalue())
        self.assertIn("rmgr health --wait 30 --json", guide.getvalue())

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

    def test_package_metadata_matches_runtime_version(self):
        project = Path(__file__).resolve().parents[1] / "pyproject.toml"
        self.assertEqual(tomllib.loads(project.read_text())["project"]["version"], __version__)


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
