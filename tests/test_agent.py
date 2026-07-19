from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from bugagent.agent import (
    CandidateValidationError,
    InvestigationOrchestrator,
    ReadOnlyRepository,
    ResponsesInvestigationClient,
    ScriptedInvestigationClient,
)
from bugagent.agent.client import DEFAULT_MODEL
from bugagent.demo import build_demo_bundle
from bugagent.domain import CandidateTest, Ticket, VerdictStatus
from bugagent.sandbox.docker import CommandResult, SandboxRun


class FakeSandbox:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, repo_root: Path, candidate_path: Path) -> SandboxRun:
        self.calls += 1
        result = CommandResult(("docker", "run"), 1, "", "E ZeroDivisionError: division by zero\nbanklib/account.py:6", False)
        preflight = CommandResult(("docker", "run"), 0, "1 test collected", "", False)
        return SandboxRun("sha256:" + "b" * 64, preflight, result, candidate_path.name)


class RepositoryTests(unittest.TestCase):
    def test_context_excludes_sensitive_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "package.py").write_text("def close(): pass\n", encoding="utf-8")
            (root / ".env").write_text("OPENAI_API_KEY=not-for-context", encoding="utf-8")
            context = ReadOnlyRepository(root).build_context(Ticket("T-1", "close bug", "fails", "repo@abc"))

        self.assertEqual(context.files, ("package.py",))
        self.assertNotIn("not-for-context", context.as_prompt_text())

    def test_rejects_unsafe_generated_test(self) -> None:
        from bugagent.agent.repository import validate_candidate

        candidate = CandidateTest(
            "tests/bugagent_generated/test_unsafe.py",
            "import subprocess\ndef test_x(): subprocess.run(['whoami'])\n",
            "x",
            "y",
        )
        with self.assertRaises(CandidateValidationError):
            validate_candidate(candidate)


class OrchestratorTests(unittest.TestCase):
    def test_scripted_client_and_sandbox_produce_verified_bundle(self) -> None:
        candidate = CandidateTest(
            "tests/bugagent_generated/test_close.py",
            "from banklib import Account\n\ndef test_close():\n    Account().close()\n",
            "empty close divides by zero",
            "ZeroDivisionError",
            ("Account.close",),
        )
        ticket = Ticket("T-2", "close fails", "close with no holds fails", "fixture@abc", expected_error="ZeroDivisionError")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "banklib").mkdir()
            (root / "banklib" / "account.py").write_text("class Account: pass\n", encoding="utf-8")
            bundle = InvestigationOrchestrator(ScriptedInvestigationClient((candidate,)), FakeSandbox()).investigate(
                ticket, root, "abc"
            )

        self.assertEqual(bundle.verdict.status, VerdictStatus.REPRODUCED)
        self.assertEqual(bundle.verdict.evidence_score, 100)
        self.assertEqual(len(bundle.evidence), 3)

    def test_unsafe_candidate_is_refused_without_starting_a_sandbox(self) -> None:
        unsafe = CandidateTest(
            "tests/bugagent_generated/test_unsafe.py",
            "import subprocess\n\ndef test_x(): subprocess.run(['whoami'])\n",
            "unsafe",
            "no result",
        )
        sandbox = FakeSandbox()
        ticket = Ticket("T-unsafe", "unsafe", "unsafe test proposal", "fixture@abc")
        with tempfile.TemporaryDirectory() as directory:
            bundle = InvestigationOrchestrator(ScriptedInvestigationClient((unsafe,)), sandbox).investigate(
                ticket, Path(directory), "fixture"
            )

        self.assertEqual(bundle.verdict.status, VerdictStatus.INCONCLUSIVE)
        self.assertEqual(bundle.verdict.evidence_score, 0)
        self.assertEqual(sandbox.calls, 0)


class ResponsesClientTests(unittest.TestCase):
    def test_sends_strict_schema_and_parses_candidate(self) -> None:
        observed: dict[str, object] = {}

        class Response:
            def read(self) -> bytes:
                return json.dumps(
                    {
                        "output_text": json.dumps(
                            {
                                "path": "tests/bugagent_generated/test_close.py",
                                "content": "def test_close(): pass\n",
                                "hypothesis": "close fails",
                                "expected_symptom": "ValueError",
                                "public_api_claims": ["Account.close"],
                            }
                        )
                    }
                ).encode("utf-8")

            def close(self) -> None:
                return None

        def sender(request, timeout):
            observed["payload"] = json.loads(request.data.decode("utf-8"))
            observed["timeout"] = timeout
            return Response()

        candidate = ResponsesInvestigationClient("test-key", request_sender=sender).propose(
            Ticket("T-3", "close fails", "bad close", "repo@abc"),
            ReadOnlyRepository(Path(__file__).resolve().parents[1]).build_context(
                Ticket("T-3", "close fails", "bad close", "repo@abc")
            ),
            (),
        )

        payload = observed["payload"]
        assert isinstance(payload, dict)
        self.assertEqual(payload["model"], DEFAULT_MODEL)
        self.assertFalse(payload["store"])
        self.assertTrue(payload["text"]["format"]["strict"])
        self.assertEqual(candidate.path, "tests/bugagent_generated/test_close.py")


if __name__ == "__main__":
    unittest.main()
