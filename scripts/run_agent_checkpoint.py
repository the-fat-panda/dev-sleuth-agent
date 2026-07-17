"""Run the complete Phase 3 scripted-agent → sandbox → proof-bundle checkpoint."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from bugagent.agent import InvestigationOrchestrator, ScriptedInvestigationClient
from bugagent.artifacts import ArtifactStore
from bugagent.domain import CandidateTest, Ticket, VerdictStatus
from bugagent.sandbox import DockerSandbox, SandboxPolicy


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True, help="Immutable local sha256 image ID")
    parser.add_argument("--output", type=Path, default=Path(".bugagent") / "checkpoint-3")
    arguments = parser.parse_args()

    workspace = Path(__file__).resolve().parents[1]
    fixture = workspace / "fixtures" / "sandbox_live"
    ticket = Ticket(
        id="FIXTURE-1",
        title="Closing an account with no holds divides by zero",
        body="Calling close() on an account with no holds raises ZeroDivisionError.",
        repo_ref="sandbox-live@fixture",
        expected_error="ZeroDivisionError",
    )
    candidate = CandidateTest(
        path="tests/bugagent_generated/test_close_empty_account.py",
        content=(
            "from banklib import Account\n\n\n"
            "def test_closing_empty_account_is_safe() -> None:\n"
            "    Account().close()\n"
        ),
        hypothesis="Account.close divides by the zero hold count.",
        expected_symptom="ZeroDivisionError in banklib/account.py",
        public_api_claims=("Account.close",),
    )
    agent = InvestigationOrchestrator(
        ScriptedInvestigationClient((candidate,)),
        DockerSandbox(SandboxPolicy(image=arguments.image, timeout_seconds=30)),
        prompt_version="checkpoint-3-scripted-v1",
    )
    bundle = agent.investigate(ticket, fixture, "fixture")
    artifact_path = ArtifactStore(arguments.output).write(bundle)
    passed = bundle.verdict.status == VerdictStatus.REPRODUCED and bundle.verdict.evidence_score == 100
    print(
        json.dumps(
            {
                "checkpoint": "phase-3-agent-to-proof",
                "passed": passed,
                "status": bundle.verdict.status.value,
                "score": bundle.verdict.evidence_score,
                "candidate_count": len(bundle.candidates),
                "execution_count": len(bundle.evidence),
                "artifact_path": str(artifact_path),
            },
            indent=2,
        )
    )
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
