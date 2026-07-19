"""Run the real GPT-5.6 Terra investigation against the local banklib fixture."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from bugagent.agent import InvestigationOrchestrator, ResponsesInvestigationClient
from bugagent.agent.client import DEFAULT_MODEL
from bugagent.artifacts import ArtifactStore
from bugagent.domain import RunBundle, Ticket
from bugagent.sandbox import DockerSandbox, SandboxPolicy
from bugagent.sandbox.docker import SandboxRun


class RecordingSandbox:
    """Records raw sandbox output for this live proof without changing the sandbox Protocol."""

    def __init__(self, delegate: DockerSandbox) -> None:
        self._delegate = delegate
        self.runs: list[SandboxRun] = []

    def run(self, repo_root: Path, candidate_path: Path) -> SandboxRun:
        run = self._delegate.run(repo_root, candidate_path)
        self.runs.append(run)
        return run


def main() -> None:
    parser = argparse.ArgumentParser(description="Run BugAgent's real model investigation on banklib.")
    parser.add_argument("--image", required=True, help="Immutable local Docker image ID")
    parser.add_argument("--output", type=Path, default=Path(".bugagent") / "live-investigations")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()

    workspace = Path(__file__).resolve().parents[1]
    fixture = workspace / "fixtures" / "sandbox_live"
    ticket = Ticket(
        id="LIVE-BANKLIB-1",
        title="Fresh records fail during the normal close action",
        body=(
            "A customer reports that completing the normal close action on a newly created record crashes. "
            "It seems to happen before the record has any activity. Please investigate a regression test."
        ),
        repo_ref="sandbox-live@fixture",
    )
    sandbox = RecordingSandbox(DockerSandbox(SandboxPolicy(image=args.image, timeout_seconds=30)))
    orchestrator = InvestigationOrchestrator(
        ResponsesInvestigationClient.from_environment(model=args.model),
        sandbox,
        prompt_version="live-gpt-5.6-terra-v1",
    )
    bundle = orchestrator.investigate(ticket, fixture, "fixture")
    artifact_path = ArtifactStore(args.output).write(bundle)
    _print_report(bundle, sandbox.runs, artifact_path)


def _print_report(bundle: RunBundle, runs: list[SandboxRun], artifact_path: Path) -> None:
    print("LIVE_INVESTIGATION_REPORT_BEGIN")
    print(f"VERDICT: {bundle.verdict.status.value}")
    print(f"EVIDENCE_SCORE: {bundle.verdict.evidence_score}/100")
    print(f"ARTIFACT_PATH: {artifact_path}")
    print("SCORE_BREAKDOWN:")
    for reason in bundle.verdict.rationale:
        print(f"- {reason}")
    for reason in bundle.verdict.disqualifiers:
        print(f"- DISQUALIFIER: {reason}")
    for question in bundle.verdict.blocking_questions:
        print(f"- BLOCKING_QUESTION: {question}")

    for index, candidate in enumerate(bundle.candidates, start=1):
        print(f"CANDIDATE_{index}_BEGIN")
        print(json.dumps(_candidate_payload(candidate), indent=2))
        print(f"CANDIDATE_{index}_END")

    for index, run in enumerate(runs, start=1):
        print(f"SANDBOX_RUN_{index}_BEGIN")
        print(f"NORMALIZED_SIGNATURE: {run.normalized_signature()}")
        print(f"SETUP_VALID: {run.setup_valid}")
        print(f"TEST_FAILED: {run.test_failed}")
        print(f"TIMED_OUT: {run.timed_out}")
        print("COLLECTION_STDOUT:")
        print(run.preflight.stdout)
        print("COLLECTION_STDERR:")
        print(run.preflight.stderr)
        if run.execution:
            print("EXECUTION_STDOUT:")
            print(run.execution.stdout)
            print("EXECUTION_STDERR:")
            print(run.execution.stderr)
        print(f"SANDBOX_RUN_{index}_END")
    print("LIVE_INVESTIGATION_REPORT_END")


def _candidate_payload(candidate: Any) -> dict[str, Any]:
    return {
        "path": candidate.path,
        "content": candidate.content,
        "hypothesis": candidate.hypothesis,
        "expected_symptom": candidate.expected_symptom,
        "public_api_claims": candidate.public_api_claims,
    }


if __name__ == "__main__":
    main()
