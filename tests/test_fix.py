from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from bugagent.domain import CandidateTest, Ticket
from bugagent.fix import (
    FixProposal,
    FixValidationError,
    PatchValidator,
    PullRequestPublisher,
    SuiteResult,
    prepare_pull_request,
    write_pull_request_plan,
)
from bugagent.github import GitHubCheckoutError, GitHubConfig
from bugagent.sandbox import SandboxPolicy
from bugagent.sandbox.docker import CommandResult, SandboxRun


class _StateAwareSandbox:
    def run(self, repo_root: Path, candidate_path: Path) -> SandboxRun:
        is_fixed = "return value" in (repo_root / "service.py").read_text(encoding="utf-8") and "+ 1" not in (
            repo_root / "service.py"
        ).read_text(encoding="utf-8")
        execution = CommandResult(
            command=("pytest", str(candidate_path)),
            exit_code=0 if is_fixed else 1,
            stdout="" if is_fixed else "AssertionError: expected a stable value\n",
            stderr="",
            timed_out=False,
        )
        return SandboxRun(
            image="sha256:" + "a" * 64,
            preflight=CommandResult(("pytest", "--collect-only"), 0, "", "", False),
            execution=execution,
            candidate_path=candidate_path.as_posix(),
        )


def _passing_suite(repo_root: Path) -> SuiteResult:
    return SuiteResult(("docker", "pytest"), 0, "2 passed", "", False)


class FixValidationTests(unittest.TestCase):
    def test_validated_fix_never_modifies_the_original_checkout_and_creates_a_local_pr_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            (root / "service.py").write_text("def compute(value):\n    return value + 1\n", encoding="utf-8")
            regression = CandidateTest(
                path="tests/bugagent_generated/test_compute.py",
                content="from service import compute\n\ndef test_compute_keeps_value():\n    assert compute(3) == 3\n",
                hypothesis="The service changes a stable value.",
                expected_symptom="The returned value is one higher than requested.",
            )
            proposal = FixProposal(
                summary="Keep the requested value unchanged.",
                rationale="The verified regression shows an unwanted increment.",
                patch="""diff --git a/service.py b/service.py
--- a/service.py
+++ b/service.py
@@ -1,2 +1,2 @@
 def compute(value):
-    return value + 1
+    return value
""",
            )
            validator = PatchValidator(
                SandboxPolicy(image="sha256:" + "a" * 64),
                sandbox=_StateAwareSandbox(),
                suite_runner=_passing_suite,
            )

            validated = validator.validate(root, base_commit="a" * 40, proposal=proposal, reproduction=regression)
            ticket = Ticket("SCRUM-42", "Stable values are incremented", "A returned value changes unexpectedly.", "owner/repo@main")
            plan = prepare_pull_request(
                validated,
                run_id=uuid4(),
                ticket=ticket,
                repository="owner/repo",
                base_branch="main",
            )
            destination = Path(directory) / "prepared-pr.json"
            write_pull_request_plan(plan, destination)

            self.assertEqual((root / "service.py").read_text(encoding="utf-8"), "def compute(value):\n    return value + 1\n")
            self.assertEqual(validated.changed_files, ("service.py",))
            self.assertEqual(validated.post_patch_exit_code, 0)
            self.assertTrue(validated.suite.passed)
            self.assertTrue(plan.head_branch.startswith("devsleuth/fix-scrum-42-"))
            self.assertTrue(destination.is_file())
            self.assertIn("Regression passed after patch", plan.body)

    def test_prohibited_patch_path_is_rejected_before_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            (root / "service.py").write_text("def compute(value):\n    return value + 1\n", encoding="utf-8")
            regression = CandidateTest(
                path="tests/bugagent_generated/test_compute.py",
                content="def test_placeholder():\n    assert False\n",
                hypothesis="A check fails.",
                expected_symptom="A check fails.",
            )
            proposal = FixProposal(
                summary="Unsafe edit",
                rationale="Unsafe edit",
                patch="""diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml
--- a/.github/workflows/ci.yml
+++ b/.github/workflows/ci.yml
@@ -1 +1 @@
-old
+new
""",
            )
            validator = PatchValidator(
                SandboxPolicy(image="sha256:" + "a" * 64),
                sandbox=_StateAwareSandbox(),
                suite_runner=_passing_suite,
            )

            with self.assertRaisesRegex(FixValidationError, "prohibited path"):
                validator.validate(root, base_commit="a" * 40, proposal=proposal, reproduction=regression)

    def test_publisher_cannot_mutate_github_without_explicit_publish_enablement(self) -> None:
        config = GitHubConfig(frozenset({"owner/repo"}), token="token", publish_enabled=False)
        plan = prepare_pull_request(
            _validated_fix(),
            run_id=uuid4(),
            ticket=Ticket("SCRUM-8", "A fix", "A bug", "owner/repo@main"),
            repository="owner/repo",
            base_branch="main",
        )

        with self.assertRaisesRegex(GitHubCheckoutError, "publishing is disabled"):
            PullRequestPublisher(config).publish(plan)


def _validated_fix():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "repo"
        root.mkdir()
        (root / "service.py").write_text("def compute(value):\n    return value + 1\n", encoding="utf-8")
        regression = CandidateTest(
            path="tests/bugagent_generated/test_compute.py",
            content="from service import compute\n\ndef test_compute_keeps_value():\n    assert compute(3) == 3\n",
            hypothesis="The service changes a stable value.",
            expected_symptom="The returned value is one higher than requested.",
        )
        proposal = FixProposal(
            summary="Keep the requested value unchanged.",
            rationale="The verified regression shows an unwanted increment.",
            patch="""diff --git a/service.py b/service.py
--- a/service.py
+++ b/service.py
@@ -1,2 +1,2 @@
 def compute(value):
-    return value + 1
+    return value
""",
        )
        return PatchValidator(
            SandboxPolicy(image="sha256:" + "a" * 64),
            sandbox=_StateAwareSandbox(),
            suite_runner=_passing_suite,
        ).validate(root, base_commit="a" * 40, proposal=proposal, reproduction=regression)
