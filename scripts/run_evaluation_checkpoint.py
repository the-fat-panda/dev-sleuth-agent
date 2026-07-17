"""Execute the frozen baseline controls and write a machine-readable result."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from bugagent.agent import InvestigationOrchestrator, ScriptedInvestigationClient
from bugagent.domain import CandidateTest, Ticket
from bugagent.sandbox import DockerSandbox, SandboxPolicy


def main() -> None:
    parser = argparse.ArgumentParser(description="Run BugAgent's frozen release-gate baseline.")
    parser.add_argument("--image", required=True, help="Immutable local sha256 image ID")
    parser.add_argument("--output", type=Path, default=Path(".bugagent") / "evaluation" / "baseline.json")
    args = parser.parse_args()

    workspace = Path(__file__).resolve().parents[1]
    fixture = workspace / "fixtures" / "sandbox_live"
    definitions = json.loads((workspace / "evals" / "frozen-cases.json").read_text(encoding="utf-8"))
    sandbox = DockerSandbox(SandboxPolicy(image=args.image, timeout_seconds=30))

    reproduced = InvestigationOrchestrator(
        ScriptedInvestigationClient((_reproduction_candidate(),)), sandbox, prompt_version="frozen-baseline-v1"
    ).investigate(_reproduction_ticket(), fixture, "fixture")
    need_info = InvestigationOrchestrator(
        ScriptedInvestigationClient(()), sandbox, prompt_version="frozen-baseline-v1"
    ).investigate(Ticket("MISSING-CONTEXT-1", "Close bug", "close() fails", ""), fixture, "fixture")
    unsafe = InvestigationOrchestrator(
        ScriptedInvestigationClient((_unsafe_candidate(),)), sandbox, prompt_version="frozen-baseline-v1"
    ).investigate(Ticket("UNSAFE-CANDIDATE-1", "Unsafe proposal", "verify refusal", "fixture@safe"), fixture, "fixture")

    results = [
        _result(definitions["cases"][0], reproduced.verdict.status.value, reproduced.verdict.evidence_score, len(reproduced.evidence)),
        _result(definitions["cases"][1], need_info.verdict.status.value, need_info.verdict.evidence_score, len(need_info.evidence)),
        _result(definitions["cases"][2], unsafe.verdict.status.value, unsafe.verdict.evidence_score, len(unsafe.evidence)),
    ]
    payload = {
        "evaluation_version": definitions["evaluation_version"],
        "scope": definitions["scope"],
        "results": results,
        "summary": {
            "passed": all(item["passed"] for item in results),
            "case_count": len(results),
            "verified_positive_count": sum(item["actual_verdict"] == "REPRODUCED" for item in results),
            "safe_refusal_count": sum(item["case_id"] == "UNSAFE-CANDIDATE-1" and item["passed"] for item in results),
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))
    if not payload["summary"]["passed"]:
        raise SystemExit(1)


def _result(case: dict[str, str], actual: str, score: int, execution_count: int) -> dict[str, object]:
    return {
        "case_id": case["id"],
        "expected_verdict": case["expected_verdict"],
        "actual_verdict": actual,
        "evidence_score": score,
        "execution_count": execution_count,
        "passed": actual == case["expected_verdict"],
    }


def _reproduction_ticket() -> Ticket:
    return Ticket(
        id="SANDBOX-REPRO-1",
        title="Closing an account with no holds divides by zero",
        body="Calling close() on an account with no holds raises ZeroDivisionError.",
        repo_ref="sandbox-live@fixture",
        expected_error="ZeroDivisionError",
    )


def _reproduction_candidate() -> CandidateTest:
    return CandidateTest(
        path="tests/bugagent_generated/test_close_empty_account.py",
        content="from banklib import Account\n\n\ndef test_closing_empty_account_is_safe() -> None:\n    Account().close()\n",
        hypothesis="Account.close divides by the zero hold count.",
        expected_symptom="ZeroDivisionError in banklib/account.py",
        public_api_claims=("Account.close",),
    )


def _unsafe_candidate() -> CandidateTest:
    return CandidateTest(
        path="tests/bugagent_generated/test_unsafe.py",
        content="import subprocess\n\n\ndef test_attempts_a_subprocess() -> None:\n    subprocess.run(['whoami'])\n",
        hypothesis="A subprocess should never be run by generated test code.",
        expected_symptom="safe refusal",
    )


if __name__ == "__main__":
    main()
