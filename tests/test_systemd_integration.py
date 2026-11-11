"""Opt-in host checks: RMGR_SYSTEMD_TESTS=1 python -m unittest tests.test_systemd_integration -v.

Uses at most two physical cores, 256 MiB per job, short timeouts, and private
temporary state. Does not install services or change system configuration.
"""

import json
import os
import secrets
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from rmgr.backend import SystemdBackend
from rmgr.daemon import API
from rmgr.models import ACTIVE, Config, TERMINAL, physical_cpus, prepare_home
from rmgr.scheduler import Scheduler
from rmgr.store import Store


@unittest.skipUnless(os.environ.get("RMGR_SYSTEMD_TESTS") == "1", "Opt-in: requires a working user systemd manager and delegated cgroups")
class SystemdIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="rmgr-host-test-")
        self.home = Path(self.directory.name)
        prepare_home(self.home)
        self.config = Config(physical_cpus()[:2], 512 * 1024**2)
        self.backend = SystemdBackend(self.home)
        self.backend.check(self.config)
        self.store = Store(self.home)
        self.scheduler = Scheduler(self.store, self.config, self.backend)
        self.tokens = {}

    def tearDown(self):
        try:
            for job in self.store.all():
                if job["state"] in ACTIVE:
                    self.backend.cancel(job)
                self.backend.forget(job)
        finally:
            self.store.close()
            self.directory.cleanup()

    def submit(self, code, **options):
        spec = {"argv": [sys.executable, "-c", code], "cwd": str(self.home),
                "memory_bytes": 128 * 1024**2, "timeout_seconds": 10}
        spec.update(options)
        token = secrets.token_urlsafe(32)
        job = self.scheduler.submit(str(time.monotonic_ns()), spec, token)
        self.tokens[job["id"]] = token
        return job

    def wait(self, job, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.scheduler.tick()
            self.assertIsNone(self.scheduler.error)
            current = self.store.get(job["id"])
            if current["state"] in TERMINAL:
                return current
            time.sleep(0.05)
        self.fail(f"Job did not finish: {self.store.get(job['id'])}")

    def test_affinity_and_persistent_completion(self):
        job = self.submit("import os,json; print(json.dumps(sorted(os.sched_getaffinity(0))))")
        self.scheduler.tick()
        # Replace the scheduler and reopen SQLite while its job is independently owned by systemd.
        self.store.close()
        self.store = Store(self.home)
        self.scheduler = Scheduler(self.store, self.config, self.backend)
        completed = self.wait(job)
        self.assertEqual(completed["state"], "succeeded")
        output = Path(completed["stdout_path"]).read_text()
        self.assertEqual(json.loads(output), completed["allocation"])

    def test_memory_and_walltime_enforcement(self):
        oom = self.submit("x = bytearray(256 * 1024 * 1024)")
        self.assertEqual(self.wait(oom)["state"], "oom")
        timeout = self.submit("import time; time.sleep(60)", timeout_seconds=0.5)
        self.assertEqual(self.wait(timeout)["state"], "timed_out")

    def test_custom_outputs_and_launcher_diagnostics_are_separate(self):
        cwd = self.home / "experiment"
        cwd.mkdir()
        job = self.submit("import sys; print('result'); print('warning', file=sys.stderr)",
                          cwd=str(cwd), stdout="result.out", stderr=str(cwd / "result.err"))
        completed = self.wait(job)
        self.assertEqual(completed["state"], "succeeded")
        self.assertEqual((cwd / "result.out").read_text(), "result\n")
        self.assertEqual((cwd / "result.err").read_text(), "warning\n")
        self.assertFalse((self.home / "jobs" / job["id"] / "stdout.log").exists())
        api = API(self.scheduler, self.home, {})
        self.assertEqual(api.dispatch({"op": "logs", "job_id": job["id"]})["text"], "result\n")

        failed = self.submit("", cwd=str(cwd), argv=[str(cwd / "missing-executable")])
        failure = self.wait(failed)
        self.assertEqual(failure["state"], "failed")
        self.assertEqual(failure["exit_code"], 125)
        self.assertIn("missing-executable", failure["reason"])
        self.assertEqual(Path(failure["stderr_path"]).read_text(), "")

    def test_output_collision_does_not_block_the_scheduler(self):
        a = self.submit("print('original')", stdout="shared.out")
        self.assertEqual(self.wait(a)["state"], "succeeded")
        b = self.submit("print('replacement')", stdout="shared.out")
        c = self.submit("print('independent')")
        self.assertEqual(self.wait(b)["state"], "failed")
        self.assertEqual(self.wait(c)["state"], "succeeded")
        self.assertEqual((self.home / "shared.out").read_text(), "original\n")
        api = API(self.scheduler, self.home, {})
        self.assertFalse(api.dispatch({"op": "logs", "job_id": b["id"]})["available"])

    def test_legacy_queued_job_keeps_central_outputs(self):
        job = self.submit("print('legacy')")
        with self.store.db:
            self.store.db.execute("UPDATE jobs SET stdout_path = NULL, stderr_path = NULL WHERE id = ?", (job["id"],))
        completed = self.wait(job)
        self.assertEqual(completed["state"], "succeeded")
        self.assertEqual(completed["stdout_path"], str(self.home / "jobs" / job["id"] / "stdout.log"))
        self.assertEqual(Path(completed["stdout_path"]).read_text(), "legacy\n")

    def test_cancellation_terminates_descendants(self):
        marker = self.home / "child.pid"
        code = (
            "import pathlib,subprocess,sys,time; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
            f"pathlib.Path({str(marker)!r}).write_text(str(p.pid)); "
            "time.sleep(60)"
        )
        job = self.submit(code)
        self.scheduler.tick()
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(marker.exists())
        child = int(marker.read_text())
        self.scheduler.cancel(job["id"], self.tokens[job["id"]])
        self.assertEqual(self.wait(job)["state"], "cancelled")
        stat = Path(f"/proc/{child}/stat")
        if stat.exists():
            self.assertEqual(stat.read_text().split(") ", 1)[1].split()[0], "Z")

    def test_disjoint_cpu_jobs_can_overlap(self):
        if len(self.config.cpu_ids) < 2:
            self.skipTest("Needs two physical cores")
        a = self.submit("import time; time.sleep(2)")
        b = self.submit("import time; time.sleep(2)")
        self.scheduler.tick()
        first, second = self.store.get(a["id"]), self.store.get(b["id"])
        self.assertEqual((first["state"], second["state"]), ("running", "running"))
        self.assertFalse(set(first["allocation"]) & set(second["allocation"]))
        self.assertEqual(self.wait(a)["state"], "succeeded")
        self.assertEqual(self.wait(b)["state"], "succeeded")


if __name__ == "__main__":
    unittest.main()
