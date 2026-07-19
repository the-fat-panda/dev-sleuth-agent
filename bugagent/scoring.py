"""Deterministic evidence assessment. No model self-confidence is used here."""

from __future__ import annotations

from dataclasses import dataclass

from .domain import Confidence, ExecutionEvidence, Ticket, Verdict, VerdictStatus
from .silent_output import same_silent_output


@dataclass(frozen=True, slots=True)
class EvidenceAssessment:
    verdict: Verdict
    replay_count: int


def assess_ticket(ticket: Ticket) -> Verdict | None:
    """Return a NEED_INFO verdict only when required investigation inputs are absent."""
    missing = ticket.missing_information()
    if not missing:
        return None
    return Verdict(
        status=VerdictStatus.NEED_INFO,
        confidence=Confidence.HIGH,
        evidence_score=0,
        rationale=("The investigation cannot start until required ticket facts are supplied.",),
        blocking_questions=missing,
    )


def assess_evidence(
    ticket: Ticket,
    candidate: ExecutionEvidence,
    replays: tuple[ExecutionEvidence, ...] = (),
) -> EvidenceAssessment:
    """Apply a transparent 100-point reproduction rubric.

    A positive verdict requires a valid candidate and two clean replay runs with
    the same normalized signature. The score is evidence-derived and stable.
    """
    input_verdict = assess_ticket(ticket)
    if input_verdict:
        return EvidenceAssessment(input_verdict, replay_count=len(replays))

    disqualifiers = _disqualifiers(candidate, replays)
    if disqualifiers:
        status = (
            VerdictStatus.NOT_REPRODUCED
            if not candidate.setup_valid or not candidate.test_collected or not candidate.test_failed
            else VerdictStatus.INCONCLUSIVE
        )
        return EvidenceAssessment(
            Verdict(
                status=status,
                confidence=Confidence.HIGH if status == VerdictStatus.NOT_REPRODUCED else Confidence.MEDIUM,
                evidence_score=0,
                rationale=("The observed failure cannot be treated as a product-bug reproduction.",),
                disqualifiers=tuple(disqualifiers),
            ),
            replay_count=len(replays),
        )

    if _verified_silent_output(candidate):
        return _assess_verified_silent_output(candidate, replays)

    score = 0
    rationale: list[str] = []
    if candidate.setup_valid and candidate.test_collected:
        score += 20
        rationale.append("Test collection and environment setup were valid (+20).")
    if candidate.test_failed:
        score += 20
        rationale.append("Candidate test failed after collection (+20).")
    if candidate.symptom_matches:
        score += 25
        rationale.append("Observed symptom matches the ticket (+25).")
    if candidate.relevant_frame_matches:
        score += 20
        rationale.append("Relevant repository code path matches the hypothesis (+20).")
    if candidate.uses_public_api:
        score += 5
        rationale.append("Candidate exercises the claimed public API (+5).")

    clean_replays = _matching_clean_replays(candidate, replays)
    if clean_replays >= 2:
        score += 10
        rationale.append("Two clean replays produced the same normalized signature (+10).")
        return EvidenceAssessment(
            Verdict(
                status=VerdictStatus.REPRODUCED,
                confidence=Confidence.HIGH,
                evidence_score=score,
                rationale=tuple(rationale),
            ),
            replay_count=len(replays),
        )

    if score >= 85:
        rationale.append("Candidate is promising, but two clean matching replays are still required.")
        status = VerdictStatus.INCONCLUSIVE
        confidence = Confidence.MEDIUM
    else:
        rationale.append("The required evidence threshold was not met.")
        status = VerdictStatus.NOT_REPRODUCED
        confidence = Confidence.MEDIUM

    return EvidenceAssessment(
        Verdict(status=status, confidence=confidence, evidence_score=score, rationale=tuple(rationale)),
        replay_count=len(replays),
    )


def _matching_clean_replays(candidate: ExecutionEvidence, replays: tuple[ExecutionEvidence, ...]) -> int:
    if _verified_silent_output(candidate):
        return sum(
            replay.setup_valid
            and replay.test_collected
            and replay.test_failed
            and not replay.timed_out
            and same_silent_output(candidate.silent_output, replay.silent_output)
            for replay in replays
        )
    return sum(
        replay.setup_valid
        and replay.test_collected
        and replay.test_failed
        and not replay.timed_out
        and replay.failure_origin != "generated_test"
        and replay.normalized_signature == candidate.normalized_signature
        for replay in replays
    )


def _disqualifiers(candidate: ExecutionEvidence, replays: tuple[ExecutionEvidence, ...]) -> list[str]:
    reasons: list[str] = []
    if candidate.timed_out:
        reasons.append("Candidate execution timed out.")
    if not candidate.setup_valid:
        reasons.append("Candidate environment setup was invalid.")
    if not candidate.test_collected:
        reasons.append("Candidate test did not collect.")
    if not candidate.test_failed:
        reasons.append("Candidate test did not fail.")
    if candidate.failure_origin == "generated_test" and not _verified_silent_output(candidate):
        reasons.append("Failure originated solely in generated test code.")
    if candidate.silent_output and not candidate.silent_output.probe_verified:
        reasons.append(
            "Silent-output claim was not independently verified: "
            f"{candidate.silent_output.verification_error or 'missing grounded evidence.'}"
        )
    if _verified_silent_output(candidate) and not candidate.uses_public_api:
        reasons.append("Silent-output probe did not claim public API use.")
    if len(replays) >= 2 and _matching_clean_replays(candidate, replays) < 2:
        reasons.append(
            "Clean replays did not reproduce the same grounded value mismatch."
            if _verified_silent_output(candidate)
            else "Clean replays did not agree on the normalized failure signature."
        )
    return reasons


def _verified_silent_output(evidence: ExecutionEvidence) -> bool:
    return bool(evidence.silent_output and evidence.silent_output.probe_verified)


def _assess_verified_silent_output(
    candidate: ExecutionEvidence,
    replays: tuple[ExecutionEvidence, ...],
) -> EvidenceAssessment:
    """Score an observed product value against a separately grounded repository oracle."""
    score = 0
    rationale: list[str] = []
    if candidate.setup_valid and candidate.test_collected:
        score += 20
        rationale.append("Test collection and environment setup were valid (+20).")
    if candidate.test_failed:
        score += 10
        rationale.append("The controlled probe assertion failed after collection (+10).")
    if candidate.uses_public_api:
        score += 15
        rationale.append("Probe directly observed the claimed public API output (+15).")
    score += 25
    rationale.append(
        "Expected values were derived by a deterministic oracle from a pinned repository contract (+25)."
    )
    score += 20
    rationale.append("Observed product values differ from the grounded expected values (+20).")

    clean_replays = _matching_clean_replays(candidate, replays)
    if clean_replays >= 2:
        score += 10
        rationale.append("Two clean replays reproduced the same grounded value mismatch (+10).")
        return EvidenceAssessment(
            Verdict(
                status=VerdictStatus.REPRODUCED,
                confidence=Confidence.HIGH,
                evidence_score=score,
                rationale=tuple(rationale),
            ),
            replay_count=len(replays),
        )

    rationale.append("The grounded mismatch is promising, but two matching clean replays are still required.")
    return EvidenceAssessment(
        Verdict(
            status=VerdictStatus.INCONCLUSIVE,
            confidence=Confidence.MEDIUM,
            evidence_score=score,
            rationale=tuple(rationale),
        ),
        replay_count=len(replays),
    )
