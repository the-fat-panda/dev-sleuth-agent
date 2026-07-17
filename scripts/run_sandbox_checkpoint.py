"""Run the Phase 2 clean-container checkpoint against the frozen local fixture."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from bugagent.sandbox import DockerSandbox, SandboxPolicy


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True, help="Immutable local sha256 image ID")
    arguments = parser.parse_args()

    repository_root = Path(__file__).resolve().parents[1] / "fixtures" / "sandbox_live"
    test_path = repository_root / "tests" / "bugagent_generated" / "test_close_empty_account.py"
    sandbox = DockerSandbox(SandboxPolicy(image=arguments.image, timeout_seconds=30))
    runs = [sandbox.run(repository_root, test_path), sandbox.run(repository_root, test_path)]

    signatures = [run.normalized_signature() for run in runs]
    passed = all(run.setup_valid and run.test_failed and not run.timed_out for run in runs)
    passed = passed and signatures[0] is not None and signatures[0] == signatures[1]
    print(
        json.dumps(
            {
                "checkpoint": "phase-2-clean-replay",
                "passed": passed,
                "runs": len(runs),
                "signatures": signatures,
                "image": arguments.image,
            },
            indent=2,
        )
    )
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
