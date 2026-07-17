"""Small local CLI for the deterministic evidence-core checkpoint."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .artifacts import ArtifactStore
from .demo import build_demo_bundle
from .replay import BundleIntegrityError, replay_bundle


def main() -> None:
    parser = argparse.ArgumentParser(description="BugAgent evidence-core tools")
    subcommands = parser.add_subparsers(dest="command", required=True)
    demo = subcommands.add_parser("demo", help="write a deterministic reproduced evidence bundle")
    demo.add_argument("--output", type=Path, default=Path(".bugagent") / "runs")
    replay = subcommands.add_parser("replay", help="verify a signed bundle in two fresh sandboxes")
    replay.add_argument("--bundle", type=Path, required=True, help="Path to one immutable evidence bundle")
    replay.add_argument("--repo", type=Path, required=True, help="Pinned source checkout to replay against")
    replay.add_argument("--image", help="Immutable local image ID; defaults to the image recorded in the bundle")
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
