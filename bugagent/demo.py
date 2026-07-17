"""A deterministic demo bundle used for the Phase 1 checkpoint."""

from __future__ import annotations

import hashlib

from .domain import CandidateTest, EventKind, ExecutionEvidence, RunBundle, RunEvent, Ticket
from .scoring import assess_evidence


def build_demo_bundle() -> RunBundle:
    ticket = Ticket(
        id="DEMO-42",
        title="Closing an account with no holds divides by zero",
        body="Calling close() on an account with no holds raises ZeroDivisionError.",
        repo_ref="demo-banklib@3f1b7c2",
        expected_error="ZeroDivisionError",
    )
    candidate = CandidateTest(
        path="tests/bugagent_generated/test_close_empty_account.py",
        content="def test_close_empty_account(account):\n    account.close()\n",
        hypothesis="close() divides a balance by the number of holds without guarding zero holds.",
        expected_symptom="ZeroDivisionError in banklib/account.py",
        public_api_claims=("Account.close",),
    )
    signature = "ZeroDivisionError|banklib/account.py|Account.close"
    candidate_evidence = _evidence(attempt=1, phase="CANDIDATE", signature=signature)
    replay_one = _evidence(attempt=1, phase="REPLAY", signature=signature)
    replay_two = _evidence(attempt=2, phase="REPLAY", signature=signature)
    assessment = assess_evidence(ticket, candidate_evidence, (replay_one, replay_two))

    events = (
        RunEvent(EventKind.INPUT_VALIDATED, "Ticket and pinned repository reference were validated."),
        RunEvent(EventKind.CANDIDATE_EVALUATED, "Candidate test failed with the reported symptom."),
        RunEvent(EventKind.REPLAY_EVALUATED, "Two clean replays matched the normalized signature."),
        RunEvent(
            EventKind.VERDICT_EMITTED,
            "Evidence threshold satisfied; emitted reproduced verdict.",
            detail={"score": assessment.verdict.evidence_score},
        ),
    )
    return RunBundle(
        ticket=ticket,
        repo_commit="3f1b7c2",
        prompt_version="phase-1-demo",
        candidates=(candidate,),
        evidence=(candidate_evidence, replay_one, replay_two),
        verdict=assessment.verdict,
        events=events,
    )


def _evidence(*, attempt: int, phase: str, signature: str) -> ExecutionEvidence:
    return ExecutionEvidence(
        attempt=attempt,
        phase=phase,
        command=("pytest", "-q", "tests/bugagent_generated/test_close_empty_account.py"),
        exit_code=1,
        timed_out=False,
        setup_valid=True,
        test_collected=True,
        test_failed=True,
        normalized_signature=signature,
        symptom_matches=True,
        relevant_frame_matches=True,
        uses_public_api=True,
        failure_origin="repository",
        environment_fingerprint={"python": "3.11", "pytest": "8.0", "image": "demo@sha256:fixed"},
        stdout_sha256=hashlib.sha256(b"failed test").hexdigest(),
        stderr_sha256=hashlib.sha256(b"ZeroDivisionError").hexdigest(),
    )
