"""Scheduling invariants and restart behavior without requiring systemd."""

from pathlib import Path
import secrets
import tempfile
import unittest

from rmgr.backend import Observation
from rmgr.models import Config, GIB, RmgrError, prepare_home
from rmgr.scheduler import Scheduler
from rmgr.store import Store


class FakeBackend:
    def __init__(self):
        self.units = {}
        self.launches = []
        self.stops = []
        self.unavailable = False
        self.fail_after_launch = False

    def ping(self):
        if self.unavailable:
            raise RmgrError("BACKEND_UNAVAILABLE", "Bus unavailable")

    def launch(self, job):
        self.units[job["id"]] = Observation("running")
        self.launches.append(job)
        if self.fail_after_launch:
            raise RmgrError("BACKEND_UNAVAILABLE", "Lost reply after creating the unit")

    def observe(self, job):
        if self.unavailable:
            raise RmgrError("BACKEND_UNAVAILABLE", "Bus unavailable")
        return self.units.get(job["id"], Observation("interrupted"))

    def cancel(self, job):
        self.stops.append(job["id"])

    def forget(self, job):
        self.units.pop(job["id"], None)


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.home = Path(self.directory.name)
        prepare_home(self.home)
        self.config = Config(list(range(16)), 48 * GIB, 1)
        self.store = Store(self.home)
        self.backend = FakeBackend()
        self.scheduler = Scheduler(self.store, self.config, self.backend)
        self.tokens = {}

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def submit(self, key, **overrides):
        spec = {"argv": ["/usr/bin/true"], "cwd": str(self.home)}
        spec.update(overrides)
        token = self.tokens.setdefault(key, secrets.token_urlsafe(32))
        return self.scheduler.submit(key, spec, token)

    def state(self, job):
        return self.store.get(job["id"])["state"]

    def finish(self, job, state="succeeded"):
        self.backend.units[job["id"]] = Observation(state, exit_code=0 if state == "succeeded" else 1)

    def test_two_eight_core_jobs_fit_and_sixteen_waits(self):
        a, b = self.submit("a", cores=8), self.submit("b", cores=8)
        c = self.submit("c", cores=16)
        self.scheduler.tick()
        self.assertEqual([self.state(j) for j in (a, b, c)], ["running", "running", "queued"])
        allocations = [self.store.get(j["id"])["allocation"] for j in (a, b)]
        self.assertFalse(set(allocations[0]) & set(allocations[1]))
        self.finish(a)
        self.scheduler.tick()
        self.assertEqual(self.state(c), "queued")
        self.finish(b)
        self.scheduler.tick()
        self.assertEqual(self.state(c), "running")

    def test_exclusive_job_drains_queue_and_runs_alone(self):
        first = self.submit("first", cores=4)
        self.scheduler.tick()
        exclusive = self.submit("exclusive", cores=8, exclusive=True)
        later = self.submit("later", cores=1)
        self.scheduler.tick()
        self.assertEqual(self.state(later), "queued")
        self.finish(first)
        self.scheduler.tick()
        self.assertEqual(self.state(exclusive), "running")
        self.assertEqual(self.state(later), "queued")
        self.finish(exclusive)
        self.scheduler.tick()
        self.assertEqual(self.state(later), "running")

    def test_memory_and_gpu_are_independent_reservations(self):
        a = self.submit("a", memory_bytes=40 * GIB, gpu=True)
        b = self.submit("b", memory_bytes=16 * GIB)
        c = self.submit("c", gpu=True)
        self.scheduler.tick()
        self.assertEqual(self.state(b), "queued")
        self.assertEqual(self.state(c), "queued")
        self.finish(a)
        self.scheduler.tick()
        self.assertEqual(self.state(b), "running")
        self.assertEqual(self.state(c), "running")

    def test_gpu_jobs_serialize_even_with_spare_cpu_and_memory(self):
        a, b = self.submit("a", gpu=True), self.submit("b", gpu=True)
        self.scheduler.tick()
        self.assertEqual(self.state(b), "queued")
        self.assertIn("GPU", self.store.get(b["id"])["reason"])
        self.finish(a)
        self.scheduler.tick()
        self.assertEqual(self.state(b), "running")

    def test_dependencies_fan_out_after_success(self):
        a = self.submit("a")
        b = self.submit("b", after_success=[a["id"]], cores=8)
        c = self.submit("c", after_success=[a["id"]], cores=8)
        self.scheduler.tick()
        self.assertEqual(self.state(b), "queued")
        self.finish(a)
        self.scheduler.tick()
        self.assertEqual([self.state(j) for j in (b, c)], ["running", "running"])

    def test_failed_dependency_propagates_without_launching_descendants(self):
        a = self.submit("a")
        b = self.submit("b", after_success=[a["id"]])
        c = self.submit("c", after_success=[b["id"]])
        self.scheduler.tick()
        self.finish(a, "oom")
        self.scheduler.tick()
        self.assertEqual([self.state(j) for j in (a, b, c)], ["oom", "blocked", "blocked"])
        self.assertEqual(len(self.backend.launches), 1)

    def test_idempotency_survives_database_reopen(self):
        a = self.submit("unique", cores=8)
        self.store.close()
        self.store = Store(self.home)
        self.scheduler = Scheduler(self.store, self.config, self.backend)
        self.assertEqual(self.submit("unique", cores=8)["id"], a["id"])
        with self.assertRaisesRegex(RmgrError, "different specification"):
            self.submit("unique", cores=4)

    def test_ambiguous_launch_and_restart_do_not_duplicate_jobs(self):
        a = self.submit("a", exclusive=True)
        b = self.submit("b")
        self.backend.fail_after_launch = True
        self.scheduler.tick()
        self.assertEqual(self.state(a), "starting")
        self.assertIsNotNone(self.scheduler.error)
        self.backend.fail_after_launch = False
        self.scheduler = Scheduler(self.store, self.config, self.backend)
        self.scheduler.tick()
        self.assertEqual(self.state(a), "running")
        self.assertEqual(self.state(b), "queued")
        self.assertEqual(len(self.backend.launches), 1)

    def test_unknown_backend_state_holds_resources_and_stops_admission(self):
        a = self.submit("a")
        self.scheduler.tick()
        b = self.submit("b")
        self.backend.unavailable = True
        self.scheduler.tick()
        self.assertEqual(self.state(a), "running")
        self.assertEqual(self.state(b), "queued")
        self.assertIsNotNone(self.scheduler.error)

    def test_cancel_waits_for_process_tree_to_stop(self):
        a, b = self.submit("a", cores=16), self.submit("b")
        self.scheduler.tick()
        self.scheduler.cancel(a["id"], self.tokens["a"])
        self.scheduler.tick()
        self.assertEqual(self.state(a), "cancelling")
        self.assertEqual(self.state(b), "queued")
        self.finish(a, "failed")
        self.scheduler.tick()
        self.assertEqual(self.state(a), "cancelled")
        self.assertEqual(self.state(b), "running")

    def test_restart_missing_unit_is_interrupted_not_replayed(self):
        a = self.submit("a")
        self.scheduler.tick()
        self.backend.units.clear()
        self.scheduler = Scheduler(self.store, self.config, self.backend)
        self.scheduler.tick()
        self.assertEqual(self.state(a), "interrupted")
        self.assertEqual(len(self.backend.launches), 1)

    def test_cancel_queued_dependency_blocks_dependents(self):
        a = self.submit("a")
        b = self.submit("b", after_success=[a["id"]])
        self.scheduler.cancel(a["id"], self.tokens["a"])
        self.scheduler.tick()
        self.assertEqual(self.state(b), "blocked")
        self.assertEqual(self.backend.launches, [])

    def test_rejects_unsatisfiable_and_malformed_requests(self):
        for overrides in ({"cores": 17}, {"cores": True}, {"memory_bytes": 49 * GIB},
                          {"gpu": 1}, {"timeout_seconds": float("nan")}, {"unknown": 1},
                          {"after_success": ["a" * 32]}, {"env": {"BAD=KEY": "x"}}):
            with self.subTest(overrides=overrides), self.assertRaises(RmgrError):
                self.submit("invalid", **overrides)
        self.assertEqual(self.store.all(), [])


if __name__ == "__main__":
    unittest.main()
