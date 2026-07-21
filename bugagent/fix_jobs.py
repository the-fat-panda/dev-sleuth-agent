"""Process-local state for asynchronous fix preparation jobs."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from threading import Lock
from uuid import uuid4


class FixJobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class FixProgressEvent:
    sequence: int
    stage: str
    state: str
    label: str
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class FixJob:
    job_id: str
    run_id: str
    status: FixJobState
    created_at: datetime
    updated_at: datetime
    autonomous: bool = False
    plan_id: str | None = None
    plan_path: str | None = None
    error: str | None = None
    current_stage: str = "queued"
    current_state: str = "queued"
    current_label: str = "Fix preparation queued"
    events: tuple[FixProgressEvent, ...] = ()


class FixJobRegistry:
    """Minimal isolated registry; restart-safe plan files are written separately."""

    def __init__(self) -> None:
        self._jobs: dict[str, FixJob] = {}
        self._lock = Lock()

    def create(self, run_id: str, *, autonomous: bool = False) -> FixJob:
        now = _now()
        event = FixProgressEvent(1, "queued", "queued", "Fix preparation queued", now)
        job = FixJob(str(uuid4()), run_id, FixJobState.QUEUED, now, now, autonomous=autonomous, events=(event,))
        with self._lock:
            self._jobs[job.job_id] = job
        return job

    def get(self, job_id: str) -> FixJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def latest_for_run(self, run_id: str) -> FixJob | None:
        """Return the newest in-process job for a run so a refreshed UI can reattach."""
        with self._lock:
            jobs = [job for job in self._jobs.values() if job.run_id == run_id]
            return max(jobs, key=lambda job: job.created_at, default=None)

    def mark_running(self, job_id: str) -> None:
        self._replace(job_id, status=FixJobState.RUNNING, error=None)
        self.emit(job_id, "job", "running", "Preparing verified fix")

    def mark_done(self, job_id: str, *, plan_id: str, plan_path: str) -> None:
        self._replace(job_id, status=FixJobState.DONE, plan_id=plan_id, plan_path=plan_path, error=None)
        self.emit(job_id, "pr_plan", "completed", "Validated local PR plan ready")

    def mark_failed(self, job_id: str, error: Exception) -> None:
        message = f"{type(error).__name__}: {error}"
        self._replace(job_id, status=FixJobState.FAILED, error=message)
        self.emit(job_id, "job", "failed", "Fix preparation could not validate")

    def emit(self, job_id: str, stage: str, state: str, label: str) -> None:
        now = _now()
        with self._lock:
            current = self._jobs.get(job_id)
            if current is None:
                return
            event = FixProgressEvent(len(current.events) + 1, stage, state, label, now)
            self._jobs[job_id] = replace(
                current,
                updated_at=now,
                current_stage=stage,
                current_state=state,
                current_label=label,
                events=current.events + (event,),
            )

    def _replace(self, job_id: str, **changes: object) -> None:
        with self._lock:
            current = self._jobs.get(job_id)
            if current is None:
                return
            self._jobs[job_id] = replace(current, updated_at=_now(), **changes)


def _now() -> datetime:
    return datetime.now(timezone.utc)
