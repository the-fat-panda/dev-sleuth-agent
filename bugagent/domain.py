"""Typed, serializable domain records for the evidence pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID, uuid4


class VerdictStatus(str, Enum):
    REPRODUCED = "REPRODUCED"
    NOT_REPRODUCED = "NOT_REPRODUCED"
    NEED_INFO = "NEED_INFO"
    INCONCLUSIVE = "INCONCLUSIVE"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class EventKind(str, Enum):
    INPUT_VALIDATED = "INPUT_VALIDATED"
    CANDIDATE_EVALUATED = "CANDIDATE_EVALUATED"
    REPLAY_EVALUATED = "REPLAY_EVALUATED"
    VERDICT_EMITTED = "VERDICT_EMITTED"


@dataclass(frozen=True, slots=True)
class Ticket:
    id: str
    title: str
    body: str
    repo_ref: str
    source_url: str | None = None
    expected_error: str | None = None
    version_hint: str | None = None

    def missing_information(self) -> tuple[str, ...]:
        missing: list[str] = []
        if not self.title.strip():
            missing.append("a concise bug title")
        if not self.body.strip():
            missing.append("the observed behavior and trigger")
        if not self.repo_ref.strip():
            missing.append("a pinned repository reference")
        return tuple(missing)


@dataclass(frozen=True, slots=True)
class CandidateTest:
    path: str
    content: str
    hypothesis: str
    expected_symptom: str
    public_api_claims: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExecutionEvidence:
    """Normalized result from one isolated candidate or replay execution."""

    attempt: int
    phase: str
    command: tuple[str, ...]
    exit_code: int | None
    timed_out: bool
    setup_valid: bool
    test_collected: bool
    test_failed: bool
    normalized_signature: str | None
    symptom_matches: bool
    relevant_frame_matches: bool
    uses_public_api: bool
    failure_origin: str | None
    environment_fingerprint: dict[str, str]
    stdout_sha256: str
    stderr_sha256: str

    def has_execution_disqualifier(self) -> bool:
        return (
            self.timed_out
            or not self.setup_valid
            or not self.test_collected
            or self.failure_origin == "generated_test"
        )


@dataclass(frozen=True, slots=True)
class Verdict:
    status: VerdictStatus
    confidence: Confidence
    evidence_score: int
    rationale: tuple[str, ...]
    blocking_questions: tuple[str, ...] = ()
    disqualifiers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RunEvent:
    kind: EventKind
    message: str
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RunBundle:
    ticket: Ticket
    repo_commit: str
    prompt_version: str
    candidates: tuple[CandidateTest, ...]
    evidence: tuple[ExecutionEvidence, ...]
    verdict: Verdict
    events: tuple[RunEvent, ...]
    run_id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
