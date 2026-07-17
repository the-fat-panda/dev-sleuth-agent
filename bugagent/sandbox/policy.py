"""Pure policy construction for hardened Docker sandbox invocations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class SandboxPolicyError(ValueError):
    """Raised before any container is started when a safety invariant is broken."""


@dataclass(frozen=True, slots=True)
class SandboxPolicy:
    """Security controls for running untrusted tests in Docker.

    Images must be immutable. Runs explicitly disable Docker's image pulling;
    image provisioning is a separate, reviewable build step.
    """

    image: str
    timeout_seconds: int = 60
    memory_limit: str = "512m"
    cpu_limit: str = "1.0"
    pid_limit: int = 128
    output_limit_bytes: int = 262_144

    def __post_init__(self) -> None:
        if not _is_immutable_image_reference(self.image):
            raise SandboxPolicyError("Sandbox image must be a local image ID or immutable sha256 digest.")
        if self.timeout_seconds <= 0:
            raise SandboxPolicyError("Sandbox timeout must be greater than zero.")
        if self.pid_limit <= 0:
            raise SandboxPolicyError("Sandbox PID limit must be greater than zero.")

    def validate_candidate_path(self, repo_root: Path, candidate_path: Path) -> Path:
        root = repo_root.resolve()
        if candidate_path.is_absolute():
            resolved = candidate_path.resolve()
        else:
            resolved = (root / candidate_path).resolve()
        try:
            relative = resolved.relative_to(root)
        except ValueError as error:
            raise SandboxPolicyError("Candidate test must remain inside the repository worktree.") from error

        required_prefix = Path("tests") / "bugagent_generated"
        if relative.parent != required_prefix or relative.suffix != ".py":
            raise SandboxPolicyError("Candidate tests may only be Python files directly under tests/bugagent_generated.")
        return resolved

    def docker_prefix(self, repo_root: Path) -> list[str]:
        """Return Docker arguments before the test command and image reference."""
        worktree = repo_root.resolve()
        return [
            "docker",
            "run",
            "--rm",
            "--pull=never",
            "--network=none",
            "--read-only",
            "--user=10001:10001",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            f"--pids-limit={self.pid_limit}",
            f"--memory={self.memory_limit}",
            f"--cpus={self.cpu_limit}",
            "--tmpfs=/tmp:rw,noexec,nosuid,size=64m",
            "--env=HOME=/tmp",
            "--env=TMPDIR=/tmp",
            "--env=PYTHONDONTWRITEBYTECODE=1",
            f"--mount=type=bind,source={worktree},target=/workspace,readonly",
            "--workdir=/workspace",
            self.image,
        ]


def _is_immutable_image_reference(image: str) -> bool:
    return image.startswith("sha256:") or "@sha256:" in image
