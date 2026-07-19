"""Small local CLI for the deterministic evidence-core checkpoint."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .agent import InvestigationOrchestrator, ResponsesInvestigationClient
from .agent.client import DEFAULT_MODEL
from .artifacts import ArtifactStore
from .demo import build_demo_bundle
from .domain import Ticket
from .replay import BundleIntegrityError, replay_bundle
from .sandbox import DockerSandbox, SandboxPolicy


def main() -> None:
    parser = argparse.ArgumentParser(description="BugAgent evidence-core tools")
    subcommands = parser.add_subparsers(dest="command", required=True)
    demo = subcommands.add_parser("demo", help="write a deterministic reproduced evidence bundle")
    demo.add_argument("--output", type=Path, default=Path(".bugagent") / "runs")
    replay = subcommands.add_parser("replay", help="verify a signed bundle in two fresh sandboxes")
    replay.add_argument("--bundle", type=Path, required=True, help="Path to one immutable evidence bundle")
    replay.add_argument("--repo", type=Path, required=True, help="Pinned source checkout to replay against")
    replay.add_argument("--image", help="Immutable local image ID; defaults to the image recorded in the bundle")
    investigate = subcommands.add_parser("investigate", help="run the real model and sandbox against a ticket")
    ticket_source = investigate.add_mutually_exclusive_group(required=True)
    ticket_source.add_argument("--ticket-file", type=Path, help="JSON object matching the Ticket fields")
    ticket_source.add_argument("--title", help="Vague human-readable ticket title")
    investigate.add_argument("--ticket-id", help="Ticket ID when ticket details are supplied as arguments")
    investigate.add_argument("--body", help="Ticket body when ticket details are supplied as arguments")
    investigate.add_argument("--repo-ref", help="Pinned repository reference when ticket details are supplied as arguments")
    investigate.add_argument("--expected-error", help="Optional expected exception from a detailed ticket")
    investigate.add_argument("--repo", type=Path, required=True, help="Pinned local source checkout to investigate")
    investigate.add_argument("--commit", required=True, help="Full Git SHA or immutable source label for the supplied checkout")
    investigate.add_argument("--image", required=True, help="Immutable local Docker image ID")
    investigate.add_argument("--model", default=DEFAULT_MODEL, help="Responses API model ID")
    investigate.add_argument("--max-attempts", type=int, default=3)
    investigate.add_argument("--output", type=Path, default=Path(".bugagent") / "runs")
    args = parser.parse_args()

    if args.command == "demo":
        bundle = build_demo_bundle()
        artifact_path = ArtifactStore(args.output).write(bundle)
        print(
            json.dumps(
                {
                    "run_id": str(bundle.run_id),
                    "status": bundle.verdict.status.value,
                    "evidence_score": bundle.verdict.evidence_score,
                    "artifact_path": str(artifact_path),
                },
                indent=2,
            )
        )
    elif args.command == "replay":
        try:
            report = replay_bundle(args.bundle, args.repo, image=args.image)
        except (BundleIntegrityError, ValueError) as error:
            parser.error(str(error))
        print(json.dumps(report.as_json(), indent=2))
        if not report.passed:
            raise SystemExit(1)
    elif args.command == "investigate":
        ticket = _ticket_from_args(args, parser)
        bundle = InvestigationOrchestrator(
            ResponsesInvestigationClient.from_environment(model=args.model),
            DockerSandbox(SandboxPolicy(image=args.image, timeout_seconds=30)),
            max_attempts=args.max_attempts,
            prompt_version=f"live-{args.model}-v1",
        ).investigate(ticket, args.repo, args.commit)
        artifact_path = ArtifactStore(args.output).write(bundle)
        print(json.dumps(_investigation_summary(bundle, artifact_path), indent=2))


def _ticket_from_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> Ticket:
    if args.ticket_file:
        try:
            payload = json.loads(args.ticket_file.read_text(encoding="utf-8"))
            return Ticket(**payload)
        except (OSError, TypeError, json.JSONDecodeError) as error:
            parser.error(f"Invalid ticket file: {error}")
    if not args.ticket_id or not args.body or not args.repo_ref:
        parser.error("--ticket-id, --body, and --repo-ref are required with --title.")
    return Ticket(args.ticket_id, args.title, args.body, args.repo_ref, expected_error=args.expected_error)


def _investigation_summary(bundle, artifact_path: Path) -> dict[str, object]:
    return {
        "run_id": str(bundle.run_id),
        "status": bundle.verdict.status.value,
        "evidence_score": bundle.verdict.evidence_score,
        "rationale": bundle.verdict.rationale,
        "disqualifiers": bundle.verdict.disqualifiers,
        "blocking_questions": bundle.verdict.blocking_questions,
        "artifact_path": str(artifact_path),
        "candidates": [
            {
                "path": candidate.path,
                "content": candidate.content,
                "hypothesis": candidate.hypothesis,
                "expected_symptom": candidate.expected_symptom,
            }
            for candidate in bundle.candidates
        ],
    }
