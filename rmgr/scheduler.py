"""Admission and state transitions, independent of the execution backend."""

import json
import sqlite3
import time

from .models import ACTIVE, TERMINAL, Config, JobSpec, RmgrError, require, text
from .store import Store
from .protocol import MAX_SPEC_BYTES


class Scheduler:
    def __init__(self, store: Store, config: Config, backend):
        self.store = store
        self.config = config
        self.backend = backend
        self.error = None

    def submit(self, request_id: str, specification: dict, control_token: str) -> dict:
        text(request_id, "request_id")
        require(len(request_id) <= 200, "request_id is too long")
        existing = self.store.retry(request_id, specification, control_token)
        if existing is not None:
            return existing
        spec = JobSpec.from_dict(specification, self.config)
        require(len(json.dumps(spec.to_dict())) <= MAX_SPEC_BYTES, "Job specification must be <= 64 KiB")
        return self.store.submit(request_id, spec, control_token)

    def operator_cancel(self, job_id: str, reason: str) -> dict:
        text(reason, "reason")
        require(bool(reason.strip()) and len(reason) <= 1000, "Operator reason must contain 1–1000 characters")
        job = self.store.operator_cancel(job_id, reason)
        if job["state"] == "cancelling":
            self.backend.cancel(job)
        return self.store.get(job_id)

    def cancel(self, job_id: str, control_token: object) -> dict:
        job = self.store.authorize(job_id, control_token)
        if job["state"] == "queued":
            self.store.update(job_id, state="cancelled", finished=time.time(), reason="Cancelled before launch", reason_details=None)
        elif job["state"] in ACTIVE:
            # Persist intent first. Restart recovery will repeat a stop if necessary.
            self.store.update(job_id, state="cancelling", reason="Cancellation requested", reason_details=None)
            self.backend.cancel(job)
        return self.store.get(job_id)

    def reconcile(self) -> None:
        for job in self.store.all():
            if job["state"] not in ACTIVE:
                continue
            observation = self.backend.observe(job)
            if observation.state == "running":
                if job["state"] == "cancelling":
                    self.backend.cancel(job)
                elif job["state"] == "starting":
                    self.store.update(job["id"], state="running", reason=None)
                continue
            state = "cancelled" if job["state"] == "cancelling" else observation.state
            self.store.update(job["id"], state=state, finished=time.time(),
                              reason=job["reason"] if state == "cancelled" else observation.reason,
                              reason_details=job.get("reason_details") if state == "cancelled" else None,
                              exit_code=observation.exit_code)
            self.backend.forget(job)

    def tick(self) -> None:
        try:
            self.reconcile()
            self.admit()
            self.error = None
        except RmgrError as exc:
            # Unknown backend state means resources stay reserved and admission stops.
            self.error = exc.to_dict()
        except (OSError, sqlite3.Error) as exc:
            # Persisted allocations remain authoritative; a later tick reconciles
            # them before admission. Never turn storage uncertainty into a replay.
            self.error = {"code": "STORAGE_UNAVAILABLE", "message": str(exc)}

    def admit(self) -> None:
        jobs = self.store.all()
        by_id = {job["id"]: job for job in jobs}
        active = [job for job in jobs if job["state"] in ACTIVE]
        used_cpus = {cpu for job in active for cpu in job["allocation"]}
        available = [cpu for cpu in self.config.cpu_ids if cpu not in used_cpus]
        memory = self.config.memory_bytes - sum(job["spec"]["memory_bytes"] for job in active)
        gpu_busy = any(job["spec"]["gpu"] for job in active)
        exclusive_busy = any(job["spec"]["exclusive"] for job in active)
        barrier = None
        for job in jobs:
            if job["state"] != "queued":
                continue
            spec = job["spec"]
            dependencies = [by_id[dep] for dep in spec["after_success"]]
            if any(dep["state"] in TERMINAL - {"succeeded"} for dep in dependencies):
                self.store.update(job["id"], state="blocked", finished=time.time(), reason="A dependency did not succeed",
                                  reason_details={"code": "DEPENDENCY_FAILED", "blocking_job_ids": [d["id"] for d in dependencies if d["state"] in TERMINAL - {"succeeded"}]})
                by_id[job["id"]] = self.store.get(job["id"])
                continue
            if any(dep["state"] != "succeeded" for dep in dependencies):
                self.store.update(job["id"], reason="Waiting for successful dependencies", reason_details={"code": "DEPENDENCIES", "blocking_job_ids": [d["id"] for d in dependencies if d["state"] != "succeeded"]})
                continue
            if barrier:
                self.store.update(job["id"], reason=f"Waiting behind job {barrier}", reason_details={"code": "FIFO_BARRIER", "blocking_job_ids": [barrier]})
                continue
            reason = None
            details = {"blocking_job_ids": [j["id"] for j in active]}
            if exclusive_busy:
                reason = "An exclusive job is running"
                details.update(code="EXCLUSIVE_RUNNING", blocking_job_ids=[j["id"] for j in active if j["spec"]["exclusive"]])
            elif spec["exclusive"] and active:
                reason = "Waiting for running jobs to drain for exclusive execution"
                details.update(code="EXCLUSIVE_DRAIN")
            elif len(available) < spec["cores"]:
                reason = "Waiting for physical cores"
                details.update(code="CPU_RESERVATION", requested_cores=spec["cores"], available_cores=len(available))
            elif memory < spec["memory_bytes"]:
                reason = "Waiting for memory reservation"
                details.update(code="MEMORY_RESERVATION", requested_bytes=spec["memory_bytes"], available_bytes=memory)
            elif spec["gpu"] and gpu_busy:
                reason = "Waiting for GPU reservation"
                details.update(code="GPU_RESERVATION", blocking_job_ids=[j["id"] for j in active if j["spec"]["gpu"]])
            if reason:
                # FIFO among ready jobs avoids starvation without needing runtime prediction.
                barrier = job["id"]
                self.store.update(job["id"], reason=reason, reason_details=details)
                continue
            allocation = available[:spec["cores"]]
            self.store.update(job["id"], state="starting", allocation=allocation,
                              started=time.time(), reason=None, reason_details=None)
            job = self.store.get(job["id"])
            # On ambiguous launch errors leave STARTING durable. Reconciliation determines
            # whether a unit exists; never blindly replay the command after a crash.
            try:
                self.backend.launch(job)
            except RmgrError as exc:
                if exc.code != "LAUNCH_PREPARATION_FAILED":
                    raise
                self.store.update(job["id"], state="failed", finished=time.time(), started=None,
                                  allocation=None, reason=str(exc), reason_details={"code": exc.code})
                continue
            self.store.update(job["id"], state="running")
            available = available[spec["cores"]:]
            memory -= spec["memory_bytes"]
            gpu_busy = gpu_busy or spec["gpu"]
            exclusive_busy = spec["exclusive"]
            active.append(job)

    def resources(self) -> dict:
        active = [job for job in self.store.all() if job["state"] in ACTIVE]
        used = {cpu for job in active for cpu in job["allocation"]}
        return {
            "physical_cores": len(self.config.cpu_ids),
            "cpu_ids": self.config.cpu_ids,
            "available_cpu_ids": [cpu for cpu in self.config.cpu_ids if cpu not in used],
            "memory_bytes": self.config.memory_bytes,
            "available_memory_bytes": self.config.memory_bytes - sum(j["spec"]["memory_bytes"] for j in active),
            "gpu_count": self.config.gpu_count,
            "gpu_available": bool(self.config.gpu_count and not any(j["spec"]["gpu"] for j in active)),
            "exclusive_running": any(j["spec"]["exclusive"] for j in active),
        }
