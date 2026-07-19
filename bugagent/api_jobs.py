"""Process-local job state for the HTTP investigation service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from threading import Condition
from uuid import uuid4

from bugagent.artifacts import primitive
from bugagent.domain import RunBundle, Ticket


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class JobSnapshot:
    job_id: str
    status: JobState
    created_at: datetime
    updated_at: datetime
    run_id: str | None = None
    verdict: dict[str, object] | None = None
    error: str | None = None
    source: str = "manual"
    ticket_id: str = ""
    ticket_title: str = ""
    repo_ref: str = ""
    issue_key: str | None = None
    issue_url: str | None = None


@dataclass(frozen=True, slots=True)
class JobProgressEvent:
    sequence: int
    stage: str
    state: str
    label: str
    occurred_at: datetime
    attempt: int | None = None
    detail: dict[str, object] | None = None

    def as_json(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "sequence": self.sequence,
            "stage": self.stage,
            "state": self.state,
            "label": self.label,
            "occurred_at": self.occurred_at.isoformat(),
        }
        if self.attempt is not None:
            payload["attempt"] = self.attempt
        if self.detail:
            payload["detail"] = self.detail
        return payload


class JobRegistry:
    """Small thread-safe, process-local registry for background investigation state."""

    def __init__(self) -> None:
        self._jobs: dict[str, JobSnapshot] = {}
        self._events: dict[str, list[JobProgressEvent]] = {}
        self._condition = Condition()

    def create(
        self,
        ticket: Ticket | None = None,
        *,
        source: str = "manual",
        issue_key: str | None = None,
        issue_url: str | None = None,
    ) -> JobSnapshot:
        now = _now()
        job = JobSnapshot(
            job_id=str(uuid4()),
            status=JobState.QUEUED,
            created_at=now,
            updated_at=now,
            source=source,
            ticket_id=ticket.id if ticket else "",
            ticket_title=ticket.title if ticket else "",
            repo_ref=ticket.repo_ref if ticket else "",
            issue_key=issue_key,
            issue_url=issue_url,
        )
        with self._condition:
            self._jobs[job.job_id] = job
            self._events[job.job_id] = []
            self._emit_locked(job.job_id, "job", JobState.QUEUED.value, "Investigation queued")
        return job

    def get(self, job_id: str) -> JobSnapshot | None:
        with self._condition:
            return self._jobs.get(job_id)

    def list_recent(self, limit: int) -> tuple[JobSnapshot, ...]:
        with self._condition:
            return tuple(sorted(self._jobs.values(), key=lambda job: job.created_at, reverse=True)[:limit])

    def mark_running(self, job_id: str) -> None:
        self._replace(job_id, status=JobState.RUNNING)
        self.emit(job_id, "job", JobState.RUNNING.value, "Investigation started")

    def mark_done(self, job_id: str, bundle: RunBundle) -> None:
        serialized_verdict = primitive(bundle.verdict)
        assert isinstance(serialized_verdict, dict)
        verdict = {
            "status": serialized_verdict["status"],
            "score": serialized_verdict["evidence_score"],
            "rationale": serialized_verdict["rationale"],
        }
        self._replace(job_id, status=JobState.DONE, run_id=str(bundle.run_id), verdict=verdict, error=None)
        self.emit(
            job_id,
            "verdict",
            "completed",
            "Verdict ready",
            detail={"run_id": str(bundle.run_id), "status": verdict["status"], "score": verdict["score"]},
        )

    def mark_failed(self, job_id: str, error: Exception) -> None:
        message = f"{type(error).__name__}: {error}"
        self._replace(job_id, status=JobState.FAILED, error=message)
        self.emit(job_id, "verdict", "failed", "Investigation could not finish", detail={"error": message})

    def emit(
        self,
        job_id: str,
        stage: str,
        state: str,
        label: str,
        *,
        attempt: int | None = None,
        detail: dict[str, object] | None = None,
    ) -> None:
        with self._condition:
            self._emit_locked(job_id, stage, state, label, attempt=attempt, detail=detail)

    def wait_for_events(
        self, job_id: str, after_sequence: int, timeout_seconds: float
    ) -> tuple[JobSnapshot | None, tuple[JobProgressEvent, ...]]:
        with self._condition:
            current = self._jobs.get(job_id)
            if current is None:
                return None, ()
            events = self._events.get(job_id, [])
            if not any(event.sequence > after_sequence for event in events) and current.status not in {
                JobState.DONE,
                JobState.FAILED,
            }:
                self._condition.wait(timeout_seconds)
                current = self._jobs.get(job_id)
                events = self._events.get(job_id, [])
            return current, tuple(event for event in events if event.sequence > after_sequence)

    def _replace(self, job_id: str, **changes: object) -> None:
        with self._condition:
            current = self._jobs.get(job_id)
            if current is None:
                return
            values = {
                "job_id": current.job_id,
                "status": current.status,
                "created_at": current.created_at,
                "updated_at": _now(),
                "run_id": current.run_id,
                "verdict": current.verdict,
                "error": current.error,
                "source": current.source,
                "ticket_id": current.ticket_id,
                "ticket_title": current.ticket_title,
                "repo_ref": current.repo_ref,
                "issue_key": current.issue_key,
                "issue_url": current.issue_url,
            }
            values.update(changes)
            self._jobs[job_id] = JobSnapshot(**values)
            self._condition.notify_all()

    def _emit_locked(
        self,
        job_id: str,
        stage: str,
        state: str,
        label: str,
        *,
        attempt: int | None = None,
        detail: dict[str, object] | None = None,
    ) -> None:
        events = self._events.get(job_id)
        if events is None:
            return
        events.append(
            JobProgressEvent(
                sequence=len(events) + 1,
                stage=stage,
                state=state,
                label=label,
                occurred_at=_now(),
                attempt=attempt,
                detail=detail,
            )
        )
        self._condition.notify_all()


def _now() -> datetime:
    return datetime.now(timezone.utc)
