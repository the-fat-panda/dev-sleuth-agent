"""Bounded ticket-to-proof investigation controller."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from typing import Protocol

from bugagent.domain import (
    CandidateTest,
    Confidence,
    EventKind,
    ExecutionEvidence,
    RunBundle,
    RunEvent,
    Ticket,
    Verdict,
    VerdictStatus,
)
from bugagent.sandbox.docker import SandboxRun
from bugagent.scoring import assess_evidence, assess_ticket

from .client import InvestigationClient
from .repository import CandidateValidationError, ReadOnlyRepository, validate_candidate


class Sandbox(Protocol):
    def run(self, repo_root: Path, candidate_path: Path) -> SandboxRun: ...


class InvestigationOrchestrator:
    def __init__(
        self,
        client: InvestigationClient,
        sandbox: Sandbox,
        *,
        max_attempts: int = 3,
        prompt_version: str = "investigation-v1",
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least one.")
        self.client = client
        self.sandbox = sandbox
        self.max_attempts = max_attempts
        self.prompt_version = prompt_version

    def investigate(self, ticket: Ticket, repo_root: Path, repo_commit: str) -> RunBundle:
        initial = assess_ticket(ticket)
        events: list[RunEvent] = [RunEvent(EventKind.INPUT_VALIDATED, "Ticket input was validated.")]
        if initial:
            events.append(RunEvent(EventKind.VERDICT_EMITTED, "Ticket is missing required investigation facts."))
            return RunBundle(ticket, repo_commit, self.prompt_version, (), (), initial, tuple(events))

        repository = ReadOnlyRepository(repo_root)
        context = repository.build_context(ticket)
        candidates: list[CandidateTest] = []
        evidence: list[ExecutionEvidence] = []
        feedback: list[str] = []
        last_verdict: Verdict | None = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                candidate = validate_candidate(self.client.propose(ticket, context, tuple(feedback)))
            except CandidateValidationError as error:
                verdict = Verdict(
                    VerdictStatus.INCONCLUSIVE,
                    Confidence.LOW,
                    0,
                    ("The proposed test was rejected before execution.",),
                    disqualifiers=(str(error),),
                )
                events.append(
                    RunEvent(
                        EventKind.CANDIDATE_EVALUATED,
                        f"Candidate {attempt} was rejected by the safety validator.",
                        detail={"reason": str(error)},
                    )
                )
                events.append(RunEvent(EventKind.VERDICT_EMITTED, "No unsafe candidate is ever executed."))
                return RunBundle(
                    ticket,
                    repo_commit,
                    self.prompt_version,
                    tuple(candidates),
                    tuple(evidence),
                    verdict,
                    tuple(events),
                )
            candidates.append(candidate)
            with candidate_worktree(repo_root, candidate) as worktree:
                test_path = worktree / candidate.path
                candidate_run = self.sandbox.run(worktree, test_path)
                candidate_evidence = _evidence_from_run(candidate_run, attempt, "CANDIDATE", ticket, candidate)
                evidence.append(candidate_evidence)

                replays: tuple[ExecutionEvidence, ...] = ()
                if candidate_run.test_failed and candidate_run.setup_valid and not candidate_run.timed_out:
                    replay_one = _evidence_from_run(
                        self.sandbox.run(worktree, test_path), attempt, "REPLAY", ticket, candidate
                    )
                    replay_two = _evidence_from_run(
                        self.sandbox.run(worktree, test_path), attempt + 1, "REPLAY", ticket, candidate
                    )
                    replays = (replay_one, replay_two)
                    evidence.extend(replays)
                    events.append(RunEvent(EventKind.REPLAY_EVALUATED, f"Attempt {attempt} completed two clean replays."))

            assessment = assess_evidence(ticket, candidate_evidence, replays)
            last_verdict = assessment.verdict
            events.append(
                RunEvent(
                    EventKind.CANDIDATE_EVALUATED,
                    f"Candidate {attempt} assessed as {assessment.verdict.status.value}.",
                    detail={
                        "score": assessment.verdict.evidence_score,
                        "disqualifiers": list(assessment.verdict.disqualifiers),
                    },
                )
            )
            if assessment.verdict.status == VerdictStatus.REPRODUCED:
                events.append(RunEvent(EventKind.VERDICT_EMITTED, "Matched evidence replayed twice in clean containers."))
                return RunBundle(
                    ticket,
                    repo_commit,
                    self.prompt_version,
                    tuple(candidates),
                    tuple(evidence),
                    assessment.verdict,
                    tuple(events),
                )
            feedback.append(_feedback_from_assessment(assessment.verdict, candidate_evidence))

        verdict = last_verdict or Verdict(
            VerdictStatus.INCONCLUSIVE,
            Confidence.LOW,
            0,
            ("No candidate test could be evaluated.",),
        )
        if verdict.status == VerdictStatus.INCONCLUSIVE:
            verdict = replace(
                verdict,
                rationale=verdict.rationale + ("Attempt budget exhausted before a verified reproduction.",),
            )
        events.append(RunEvent(EventKind.VERDICT_EMITTED, f"Attempt budget exhausted with {verdict.status.value}."))
        return RunBundle(ticket, repo_commit, self.prompt_version, tuple(candidates), tuple(evidence), verdict, tuple(events))


def candidate_worktree(repo_root: Path, candidate: CandidateTest):
    """Copy an input repository so generation never modifies the user's selected checkout."""
    class Worktree:
        def __enter__(self) -> Path:
            self._temporary = TemporaryDirectory(prefix="bugagent-")
            self.path = Path(self._temporary.name) / "repo"
            shutil.copytree(
                repo_root,
                self.path,
                ignore=shutil.ignore_patterns(".git", ".bugagent", "__pycache__", ".venv", "venv"),
            )
            target = self.path / candidate.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(candidate.content, encoding="utf-8", newline="\n")
            return self.path

        def __exit__(self, exc_type, exc_value, traceback) -> None:
            self._temporary.cleanup()

    return Worktree()


def _evidence_from_run(
    run: SandboxRun,
    attempt: int,
    phase: str,
    ticket: Ticket,
    candidate: CandidateTest,
) -> ExecutionEvidence:
    signature = run.normalized_signature()
    symptom_matches = bool(
        signature
        and (
            (ticket.expected_error and ticket.expected_error.lower() in signature.lower())
            or candidate.expected_symptom.lower().split(" ")[0] in signature.lower()
        )
    )
    frame = signature.split("|")[1] if signature and "|" in signature else "unknown"
    relevant_frame_matches = frame not in {"unknown", Path(candidate.path).name}
    return ExecutionEvidence(
        attempt=attempt,
        phase=phase,
        command=run.execution.command if run.execution else run.preflight.command,
        exit_code=run.execution.exit_code if run.execution else run.preflight.exit_code,
        timed_out=run.timed_out,
        setup_valid=run.setup_valid,
        test_collected=run.test_collected,
        test_failed=run.test_failed,
        normalized_signature=signature,
        symptom_matches=symptom_matches,
        relevant_frame_matches=relevant_frame_matches,
        uses_public_api=bool(candidate.public_api_claims),
        failure_origin="repository" if relevant_frame_matches else "generated_test" if run.test_failed else None,
        environment_fingerprint={"sandbox_image": run.image},
        stdout_sha256=run.stdout_sha256(),
        stderr_sha256=run.stderr_sha256(),
    )


def _feedback_from_assessment(verdict: Verdict, evidence: ExecutionEvidence) -> str:
    reason = "; ".join(verdict.disqualifiers or verdict.rationale)
    signature = evidence.normalized_signature or "no normalized failure signature"
    return f"Previous candidate verdict={verdict.status.value}; signature={signature}; reason={reason}"
