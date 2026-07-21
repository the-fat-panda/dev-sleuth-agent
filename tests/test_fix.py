from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from bugagent.domain import CandidateTest, Ticket
from bugagent.fix import (
    FixProposal,
    FixValidationError,
    PatchProposalError,
    PatchValidator,
    PullRequestPublisher,
    ResponsesFixClient,
    SuiteResult,
    propose_and_validate_fix,
    prepare_pull_request,
    write_pull_request_plan,
)
from bugagent.github import GitHubCheckoutError, GitHubConfig
from bugagent.agent.repository import ReadOnlyRepository
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


class _CountingStateAwareSandbox(_StateAwareSandbox):
    def __init__(self) -> None:
        self.calls = 0

    def run(self, repo_root: Path, candidate_path: Path) -> SandboxRun:
        self.calls += 1
        return super().run(repo_root, candidate_path)


class _SequencedFixClient:
    def __init__(self, proposals: tuple[FixProposal, ...]) -> None:
        self._proposals = proposals
        self.calls: list[tuple[str, ...]] = []

    def propose(
        self,
        ticket: Ticket,
        repository: ReadOnlyRepository,
        reproduction: CandidateTest,
        prior_feedback: tuple[str, ...] = (),
    ) -> FixProposal:
        self.calls.append(prior_feedback)
        return self._proposals[len(self.calls) - 1]


def _passing_suite(repo_root: Path) -> SuiteResult:
    return SuiteResult(("docker", "pytest"), 0, "2 passed", "", False)


def _regression() -> CandidateTest:
    return CandidateTest(
        path="tests/bugagent_generated/test_compute.py",
        content="from service import compute\n\ndef test_compute_keeps_value():\n    assert compute(3) == 3\n",
        hypothesis="The service changes a stable value.",
        expected_symptom="The returned value is one higher than requested.",
    )


def _valid_proposal() -> FixProposal:
    return FixProposal(
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


class FixValidationTests(unittest.TestCase):
    def test_structured_model_edits_generate_a_canonical_applicable_diff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            (root / "service.py").write_text("def compute(value):\n    return value + 1\n", encoding="utf-8")
            observed: dict[str, object] = {}

            class Response:
                def read(self) -> bytes:
                    return json.dumps(
                        {
                            "output_text": json.dumps(
                                {
                                    "summary": "Keep the requested value unchanged.",
                                    "rationale": "The verified regression shows an unwanted increment.",
                                    "edits": [
                                        {
                                            "path": "service.py",
                                            "start_line": 2,
                                            "end_line": 2,
                                            "replacement_content": "    return value\n",
                                        }
                                    ],
                                }
                            )
                        }
                    ).encode("utf-8")

                def close(self) -> None:
                    return None

            def sender(request, timeout):
                observed["payload"] = json.loads(request.data.decode("utf-8"))
                return Response()

            proposal = ResponsesFixClient("test-key", request_sender=sender).propose(
                Ticket("SCRUM-45", "Stable values are incremented", "A returned value changes unexpectedly.", "owner/repo@main"),
                ReadOnlyRepository(root),
                _regression(),
            )
            validated = PatchValidator(
                SandboxPolicy(image="sha256:" + "a" * 64),
                sandbox=_StateAwareSandbox(),
                suite_runner=_passing_suite,
            ).validate(root, base_commit="a" * 40, proposal=proposal, reproduction=_regression())

        payload = observed["payload"]
        assert isinstance(payload, dict)
        schema = payload["text"]["format"]["schema"]
        self.assertIn("edits", schema["properties"])
        self.assertNotIn("patch", schema["properties"])
        self.assertIn("Numbered editable source excerpts", payload["input"][1]["content"])
        self.assertIn("   2 |     return value + 1", payload["input"][1]["content"])
        self.assertIn("diff --git a/service.py b/service.py", proposal.patch)
        self.assertIn("@@", proposal.patch)
        self.assertEqual(validated.changed_files, ("service.py",))

    def test_structured_edit_must_name_a_valid_source_line_range(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            (root / "service.py").write_text("def compute(value):\n    return value + 1\n", encoding="utf-8")

            class Response:
                def read(self) -> bytes:
                    return json.dumps(
                        {
                            "output_text": json.dumps(
                                {
                                    "summary": "Invalid stale edit.",
                                    "rationale": "The expected fragment is deliberately absent.",
                                    "edits": [
                                        {
                                            "path": "service.py",
                                            "start_line": 3,
                                            "end_line": 3,
                                            "replacement_content": "    return value\n",
                                        }
                                    ],
                                }
                            )
                        }
                    ).encode("utf-8")

                def close(self) -> None:
                    return None

            with self.assertRaisesRegex(PatchProposalError, "line range 3-3 exceeds.*has 2 lines"):
                ResponsesFixClient("test-key", request_sender=lambda _request, _timeout: Response()).propose(
                    Ticket("SCRUM-46", "Stable values are incremented", "A returned value changes unexpectedly.", "owner/repo@main"),
                    ReadOnlyRepository(root),
                    _regression(),
                )

    def test_malformed_diff_is_retried_with_exact_apply_error_before_any_sandbox_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            (root / "service.py").write_text("def compute(value):\n    return value + 1\n", encoding="utf-8")
            regression = _regression()
            malformed = FixProposal(
                summary="Malformed patch for retry control.",
                rationale="The diff headers are followed by invalid patch content.",
                patch="""diff --git a/service.py b/service.py
--- a/service.py
+++ b/service.py
this is not a unified-diff hunk
""",
            )
            client = _SequencedFixClient((malformed, _valid_proposal()))
            sandbox = _CountingStateAwareSandbox()

            validated = propose_and_validate_fix(
                client,
                PatchValidator(SandboxPolicy(image="sha256:" + "a" * 64), sandbox=sandbox, suite_runner=_passing_suite),
                ticket=Ticket("SCRUM-43", "Stable values are incremented", "A returned value changes unexpectedly.", "owner/repo@main"),
                repository=ReadOnlyRepository(root),
                repo_root=root,
                base_commit="a" * 40,
                reproduction=regression,
            )

        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls[0], ())
        self.assertIn("Patch did not apply cleanly", client.calls[1][0])
        self.assertIn("error:", client.calls[1][0])
        self.assertEqual(sandbox.calls, 2, "The malformed first diff must not run the regression sandbox.")
        self.assertEqual(validated.changed_files, ("service.py",))

    def test_semantic_validation_failure_is_retried_with_sandbox_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            (root / "service.py").write_text("def compute(value):\n    return value + 1\n", encoding="utf-8")
            ineffective = FixProposal(
                summary="Change the implementation without solving the verified case.",
                rationale="This patch is intentionally valid but does not satisfy the regression.",
                patch="""diff --git a/service.py b/service.py
--- a/service.py
+++ b/service.py
@@ -1,2 +1,2 @@
 def compute(value):
-    return value + 1
+    return value + 1  # unchanged behavior
""",
            )
            client = _SequencedFixClient((ineffective, _valid_proposal()))
            sandbox = _CountingStateAwareSandbox()

            validated = propose_and_validate_fix(
                client,
                PatchValidator(
                    SandboxPolicy(image="sha256:" + "a" * 64),
                    sandbox=sandbox,
                    suite_runner=_passing_suite,
                ),
                ticket=Ticket("SCRUM-44", "Stable values are incremented", "A returned value changes unexpectedly.", "owner/repo@main"),
                repository=ReadOnlyRepository(root),
                repo_root=root,
                base_commit="a" * 40,
                reproduction=_regression(),
                max_attempts=2,
            )

        self.assertEqual(len(client.calls), 2)
        self.assertIn("regression test did not pass", client.calls[1][0])
        self.assertIn("return value + 1  # unchanged behavior", client.calls[1][0])
        self.assertEqual(sandbox.calls, 4)
        self.assertEqual(validated.changed_files, ("service.py",))

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
