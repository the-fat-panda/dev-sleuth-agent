"""API-layer adapters that observe investigation progress without changing the core."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from bugagent.agent import ResponsesInvestigationClient
from bugagent.domain import Ticket
from bugagent.sandbox import DockerSandbox


class ProgressEventSink(Protocol):
    def emit(
        self,
        job_id: str,
        stage: str,
        state: str,
        label: str,
        *,
        attempt: int | None = None,
        detail: dict[str, object] | None = None,
    ) -> None: ...


class InvestigationProgressReporter:
    """Reports stage boundaries by observing the client and sandbox adapters."""

    _LABELS = {
        "form_hypothesis": "Forming hypothesis",
        "candidate_sandbox": "Running candidate in sandbox",
        "replay_1": "Replaying in a fresh sandbox",
        "replay_2": "Confirming the replay",
        "verdict": "Producing verdict",
    }

    def __init__(self, registry: ProgressEventSink, job_id: str) -> None:
        self._registry = registry
        self._job_id = job_id
        self._attempt = 0
        self._sandbox_calls = 0
        self._verdict_started = False

    def begin_hypothesis(self) -> int:
        self._attempt += 1
        self._sandbox_calls = 0
        self._emit("form_hypothesis", "started")
        return self._attempt

    def complete_hypothesis(self) -> None:
        self._emit("form_hypothesis", "completed")

    def fail_hypothesis(self, error: Exception) -> None:
        self._emit("form_hypothesis", "failed", detail=_error_detail(error))

    def next_sandbox_stage(self) -> str:
        self._sandbox_calls += 1
        return {
            1: "candidate_sandbox",
            2: "replay_1",
            3: "replay_2",
        }.get(self._sandbox_calls, "candidate_sandbox")

    def begin_stage(self, stage: str) -> None:
        self._emit(stage, "started")

    def complete_stage(self, stage: str) -> None:
        self._emit(stage, "completed")

    def fail_stage(self, stage: str, error: Exception) -> None:
        self._emit(stage, "failed", detail=_error_detail(error))

    def begin_verdict(self) -> None:
        if self._verdict_started:
            return
        self._verdict_started = True
        self._emit("verdict", "started")

    def _emit(self, stage: str, state: str, *, detail: dict[str, object] | None = None) -> None:
        self._registry.emit(
            self._job_id,
            stage,
            state,
            self._LABELS[stage],
            attempt=self._attempt or None,
            detail=detail,
        )


class ProgressInvestigationClient:
    """Client adapter that observes hypothesis formation without altering it."""

    def __init__(self, delegate: ResponsesInvestigationClient, progress: InvestigationProgressReporter) -> None:
        self._delegate = delegate
        self._progress = progress

    def propose(self, ticket: Ticket, repository: object, prior_feedback: tuple[str, ...]):
        self._progress.begin_hypothesis()
        try:
            candidate = self._delegate.propose(ticket, repository, prior_feedback)  # type: ignore[arg-type]
        except Exception as error:
            self._progress.fail_hypothesis(error)
            raise
        self._progress.complete_hypothesis()
        return candidate


class ProgressSandbox:
    """Sandbox adapter that observes candidate and replay executions."""

    def __init__(self, delegate: DockerSandbox, progress: InvestigationProgressReporter) -> None:
        self._delegate = delegate
        self._progress = progress

    def run(self, repo_root: Path, candidate_path: Path):
        stage = self._progress.next_sandbox_stage()
        self._progress.begin_stage(stage)
        try:
            result = self._delegate.run(repo_root, candidate_path)
        except Exception as error:
            self._progress.fail_stage(stage, error)
            raise
        self._progress.complete_stage(stage)
        if stage == "replay_2":
            self._progress.begin_verdict()
        return result


def _error_detail(error: Exception) -> dict[str, object]:
    return {"error": f"{type(error).__name__}: {error}"}
