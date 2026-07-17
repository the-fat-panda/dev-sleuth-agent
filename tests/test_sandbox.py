from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest

from bugagent.sandbox import DockerSandbox, SandboxPolicy, SandboxPolicyError


IMAGE = "sha256:" + "a" * 64


class RecordingExecutor:
    def __init__(self, responses: list[subprocess.CompletedProcess[str] | Exception]) -> None:
        self.responses = responses
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class SandboxPolicyTests(unittest.TestCase):
    def test_rejects_mutable_image_tag(self) -> None:
        with self.assertRaises(SandboxPolicyError):
            SandboxPolicy(image="python:3.13-slim")

    def test_candidate_must_stay_in_generated_test_directory(self) -> None:
        policy = SandboxPolicy(image=IMAGE)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(SandboxPolicyError):
                policy.validate_candidate_path(root, Path("tests/test_existing.py"))

    def test_docker_command_has_required_restrictions(self) -> None:
        policy = SandboxPolicy(image=IMAGE)
        with tempfile.TemporaryDirectory() as directory:
            command = policy.docker_prefix(Path(directory))

        for flag in (
            "--pull=never",
            "--network=none",
            "--read-only",
            "--user=10001:10001",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
        ):
            self.assertIn(flag, command)
        self.assertIn(IMAGE, command)


class DockerSandboxTests(unittest.TestCase):
    def test_valid_collection_then_failure_is_recorded(self) -> None:
        executor = RecordingExecutor(
            [
                subprocess.CompletedProcess([], 0, "1 test collected", ""),
                subprocess.CompletedProcess(
                    [],
                    1,
                    "",
                    'E   ZeroDivisionError: division by zero\nFile "banklib/account.py", line 42',
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "tests" / "bugagent_generated" / "test_repro.py"
            path.parent.mkdir(parents=True)
            path.write_text("def test_repro(): pass\n", encoding="utf-8")
            run = DockerSandbox(SandboxPolicy(image=IMAGE), executor).run(root, path)

        self.assertTrue(run.setup_valid)
        self.assertTrue(run.test_collected)
        self.assertTrue(run.test_failed)
        self.assertFalse(run.timed_out)
        self.assertEqual(len(executor.commands), 2)
        self.assertIn("--collect-only", executor.commands[0])
        self.assertIn("--network=none", executor.commands[1])
        self.assertIn("--tb=long", executor.commands[1])
        self.assertEqual(run.normalized_signature(), "ZeroDivisionError|account.py|division by zero")

    def test_timeout_never_looks_like_a_reproduction(self) -> None:
        executor = RecordingExecutor([subprocess.TimeoutExpired(["docker"], 60)])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "tests" / "bugagent_generated" / "test_repro.py"
            path.parent.mkdir(parents=True)
            path.write_text("def test_repro(): pass\n", encoding="utf-8")
            run = DockerSandbox(SandboxPolicy(image=IMAGE), executor).run(root, path)

        self.assertTrue(run.timed_out)
        self.assertFalse(run.setup_valid)
        self.assertFalse(run.test_failed)

    def test_normalizer_understands_pytest_path_lines(self) -> None:
        from bugagent.sandbox.docker import normalize_failure_signature

        signature = normalize_failure_signature(
            "E   ZeroDivisionError: division by zero\n"
            "fixtures/tests/bugagent_generated/test_repro.py:6\n"
            "fixtures/banklib/account.py:6: ZeroDivisionError"
        )

        self.assertEqual(signature, "ZeroDivisionError|account.py|division by zero")


if __name__ == "__main__":
    unittest.main()
