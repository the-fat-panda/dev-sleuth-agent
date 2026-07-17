"""Docker implementation of a no-network, disposable Python test sandbox."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import re
import subprocess
from typing import Callable, Protocol

from .policy import SandboxPolicy


class SandboxUnavailable(RuntimeError):
    """Docker cannot be reached or the configured immutable image is absent."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    command: tuple[str, ...]
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool


@dataclass(frozen=True, slots=True)
class SandboxRun:
    """Raw isolated execution facts; matching is performed by the evidence layer."""

    image: str
    preflight: CommandResult
    execution: CommandResult | None
    candidate_path: str

    @property
    def setup_valid(self) -> bool:
        return not self.preflight.timed_out and self.preflight.exit_code == 0

    @property
    def test_collected(self) -> bool:
        return self.setup_valid

    @property
    def test_failed(self) -> bool:
        return bool(self.execution and not self.execution.timed_out and self.execution.exit_code == 1)

    @property
    def timed_out(self) -> bool:
        return self.preflight.timed_out or bool(self.execution and self.execution.timed_out)

    def normalized_signature(self) -> str | None:
        if not self.execution:
            return None
        return normalize_failure_signature(self.execution.stdout + "\n" + self.execution.stderr)

    def stdout_sha256(self) -> str:
        payload = self.execution.stdout if self.execution else self.preflight.stdout
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def stderr_sha256(self) -> str:
        payload = self.execution.stderr if self.execution else self.preflight.stderr
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class CommandExecutor(Protocol):
    def __call__(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]: ...


class DockerSandbox:
    def __init__(self, policy: SandboxPolicy, executor: CommandExecutor | None = None) -> None:
        self.policy = policy
        self._executor = executor or _default_executor

    def run(self, repo_root: Path, candidate_path: Path) -> SandboxRun:
        resolved_candidate = self.policy.validate_candidate_path(repo_root, candidate_path)
        relative_candidate = resolved_candidate.relative_to(repo_root.resolve()).as_posix()

        preflight_command = self._pytest_command(repo_root, relative_candidate, collect_only=True)
        preflight = self._run_command(preflight_command)
        if preflight.exit_code != 0 or preflight.timed_out:
            return SandboxRun(self.policy.image, preflight, None, relative_candidate)

        execution_command = self._pytest_command(repo_root, relative_candidate, collect_only=False)
        execution = self._run_command(execution_command)
        return SandboxRun(self.policy.image, preflight, execution, relative_candidate)

    def _pytest_command(self, repo_root: Path, candidate_path: str, *, collect_only: bool) -> list[str]:
        command = self.policy.docker_prefix(repo_root)
        command.extend(["python", "-m", "pytest", "-q", "--tb=long", "-p", "no:cacheprovider"])
        if collect_only:
            command.append("--collect-only")
        command.append(candidate_path)
        return command

    def _run_command(self, command: list[str]) -> CommandResult:
        try:
            completed = self._executor(
                command,
                capture_output=True,
                text=True,
                timeout=self.policy.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            return CommandResult(tuple(command), None, _as_text(error.stdout), _as_text(error.stderr), True)
        except FileNotFoundError as error:
            raise SandboxUnavailable("Docker executable is not available.") from error
        except OSError as error:
            raise SandboxUnavailable(f"Docker invocation failed: {error}") from error

        return CommandResult(
            command=tuple(command),
            exit_code=completed.returncode,
            stdout=_cap_output(completed.stdout or "", self.policy.output_limit_bytes),
            stderr=_cap_output(completed.stderr or "", self.policy.output_limit_bytes),
            timed_out=False,
        )


def _default_executor(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, **kwargs)  # type: ignore[arg-type]


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


def _cap_output(value: str, limit: int) -> str:
    if len(value.encode("utf-8")) <= limit:
        return value
    return value.encode("utf-8")[:limit].decode("utf-8", errors="ignore") + "\n[output truncated]"


def normalize_failure_signature(output: str) -> str | None:
    """Extract a stable exception-and-frame signature without retaining raw paths."""
    exception = re.search(r"([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception|Failure)):\s*([^\n]+)", output)
    frames = re.findall(r"(?:File|at) [\"']?([^\"'\s]+\.py)[\"']?(?:, line \d+)?", output)
    if not frames:
        frames = re.findall(r"([A-Za-z0-9_./\\-]+\.py):\d+", output)
    if not exception:
        return None
    message = re.sub(r"\d+", "#", exception.group(2)).strip()
    frame_path = Path(frames[-1]).name if frames else "unknown"
    return f"{exception.group(1)}|{frame_path}|{message}"
