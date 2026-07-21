"""Process-local progress for explicit draft pull-request publication jobs."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from threading import Lock
from uuid import uuid4


class PublicationJobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class PublicationProgressEvent:
    sequence: int
    stage: str
    state: str
    label: str
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class PublicationJob:
    job_id: str
    run_id: str
    plan_id: str
    status: PublicationJobState
    created_at: datetime
    updated_at: datetime
    error: str | None = None
    publication: dict[str, object] | None = None
    current_stage: str = "queued"
    current_state: str = "queued"
    current_label: str = "Draft pull-request publication queued"
    events: tuple[PublicationProgressEvent, ...] = ()


class PublicationJobRegistry:
    """Keep explicit publication jobs observable without persisting credentials."""

    def __init__(self) -> None:
        self._jobs: dict[str, PublicationJob] = {}
        self._jobs_by_plan: dict[str, str] = {}
        self._lock = Lock()

    def create(self, run_id: str, plan_id: str) -> tuple[PublicationJob, bool]:
        with self._lock:
            existing_id = self._jobs_by_plan.get(plan_id)
            if existing_id and (existing := self._jobs.get(existing_id)) is not None:
                return existing, False
            now = _now()
            event = PublicationProgressEvent(1, "queued", "queued", "Draft pull-request publication queued", now)
            job = PublicationJob(str(uuid4()), run_id, plan_id, PublicationJobState.QUEUED, now, now, events=(event,))
            self._jobs[job.job_id] = job
            self._jobs_by_plan[plan_id] = job.job_id
            return job, True

    def get(self, job_id: str) -> PublicationJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def mark_running(self, job_id: str) -> None:
        self._replace(job_id, status=PublicationJobState.RUNNING, error=None)
        self.emit(job_id, "job", "running", "Publishing validated draft pull request")

    def mark_done(self, job_id: str, publication: dict[str, object]) -> None:
        self._replace(job_id, status=PublicationJobState.DONE, publication=publication, error=None)
        self.emit(job_id, "publication", "completed", "Draft pull request opened")

    def mark_failed(self, job_id: str, error: Exception) -> None:
        message = f"{type(error).__name__}: {error}"
        self._replace(job_id, status=PublicationJobState.FAILED, error=message)
        self.emit(job_id, "job", "failed", "Draft pull-request publication failed")

    def emit(self, job_id: str, stage: str, state: str, label: str) -> None:
        now = _now()
        with self._lock:
            current = self._jobs.get(job_id)
            if current is None:
                return
            event = PublicationProgressEvent(len(current.events) + 1, stage, state, label, now)
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
