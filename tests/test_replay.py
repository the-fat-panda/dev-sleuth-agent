from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest

from bugagent.artifacts import ArtifactStore
from bugagent.domain import CandidateTest, Confidence, ExecutionEvidence, RunBundle, Ticket, Verdict, VerdictStatus
from bugagent.replay import BundleIntegrityError, load_verified_bundle, replay_bundle
from bugagent.sandbox.docker import CommandResult, SandboxRun


class MatchingSandbox:
    def run(self, repo_root: Path, candidate_path: Path) -> SandboxRun:
        self.asserted_candidate = candidate_path
        preflight = CommandResult(("docker", "run"), 0, "1 test collected", "", False)
        execution = CommandResult(
            ("docker", "run"),
            1,
            "", 
            "E ZeroDivisionError: division by zero\nbanklib/account.py:6",
            False,
        )
        return SandboxRun("sha256:" + "a" * 64, preflight, execution, candidate_path.name)


def _bundle() -> RunBundle:
    candidate = CandidateTest(
        "tests/bugagent_generated/test_close.py",
        "from banklib import Account\n\ndef test_close():\n    Account().close()\n",
        "close divides by zero",
        "ZeroDivisionError",
        ("Account.close",),
    )
    evidence = ExecutionEvidence(
        attempt=1,
        phase="CANDIDATE",
        command=("docker", "run"),
        exit_code=1,
        timed_out=False,
        setup_valid=True,
        test_collected=True,
        test_failed=True,
        normalized_signature="ZeroDivisionError|account.py|division by zero",
        symptom_matches=True,
        relevant_frame_matches=True,
        uses_public_api=True,
        failure_origin="repository",
        environment_fingerprint={"sandbox_image": "sha256:" + "a" * 64},
        stdout_sha256=hashlib.sha256(b"").hexdigest(),
        stderr_sha256=hashlib.sha256(b"trace").hexdigest(),
    )
    return RunBundle(
        Ticket("REPLAY-1", "close fails", "close() divides by zero", "fixture@abc"),
        "fixture",
        "tests",
        (candidate,),
        (evidence,),
        Verdict(VerdictStatus.REPRODUCED, Confidence.HIGH, 100, ("verified",)),
        (),
    )


class ReplayTests(unittest.TestCase):
    def test_replays_signed_candidate_twice_in_an_isolated_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "banklib").mkdir()
            (source / "banklib" / "account.py").write_text("class Account: pass\n", encoding="utf-8")
            bundle_path = ArtifactStore(root / "bundles").write(_bundle())

            report = replay_bundle(bundle_path, source, sandbox=MatchingSandbox())

        self.assertTrue(report.passed)
        self.assertEqual(report.observed_signatures, ("ZeroDivisionError|account.py|division by zero",) * 2)
        self.assertIsNone(report.source_ref_matches)
        self.assertFalse((source / "tests" / "bugagent_generated" / "test_close.py").exists())

    def test_rejects_bundle_with_tampered_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle_path = ArtifactStore(root / "bundles").write(_bundle())
            (bundle_path / "candidates.json").write_text("[]\n", encoding="utf-8")

            with self.assertRaises(BundleIntegrityError):
                load_verified_bundle(bundle_path)


if __name__ == "__main__":
    unittest.main()
