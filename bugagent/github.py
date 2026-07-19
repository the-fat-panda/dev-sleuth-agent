"""Allow-listed GitHub checkout support for the HTTP service layer.

The investigation engine still receives only a local, disposable checkout. This
adapter owns the separate provider concern of creating that checkout from a
GitHub repository and resolving the mutable branch ref to an immutable SHA.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
from typing import Iterator, Mapping


class GitHubConfigurationError(RuntimeError):
    """GitHub repository configuration cannot safely resolve a checkout."""


class GitHubCheckoutError(RuntimeError):
    """A GitHub checkout could not be created or pinned to a commit."""


_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,38})/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")


@dataclass(frozen=True, slots=True)
class GitHubConfig:
    """Explicit provider allow-list; a token is optional for public repositories."""

    allowed_repositories: frozenset[str]
    token: str | None = None

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> "GitHubConfig | None":
        raw_allowed = environment.get("BUGAGENT_GITHUB_ALLOWED_REPOSITORIES", "").strip()
        token = environment.get("BUGAGENT_GITHUB_TOKEN", "").strip() or None
        if not raw_allowed and not token:
            return None
        if not raw_allowed:
            raise GitHubConfigurationError(
                "BUGAGENT_GITHUB_ALLOWED_REPOSITORIES is required when BUGAGENT_GITHUB_TOKEN is configured."
            )
        repositories = frozenset(item.strip() for item in raw_allowed.split(",") if item.strip())
        if not repositories or any(not _REPOSITORY_PATTERN.fullmatch(item) for item in repositories):
            raise GitHubConfigurationError(
                "BUGAGENT_GITHUB_ALLOWED_REPOSITORIES must be a comma-separated owner/repository allow-list."
            )
        return cls(allowed_repositories=repositories, token=token)

    def require_allowed(self, repository: str) -> None:
        if not _REPOSITORY_PATTERN.fullmatch(repository):
            raise GitHubCheckoutError("GitHub repository must be in owner/repository form.")
        if repository not in self.allowed_repositories:
            raise GitHubCheckoutError(f"GitHub repository {repository!r} is not in BUGAGENT_GITHUB_ALLOWED_REPOSITORIES.")


@dataclass(frozen=True, slots=True)
class GitHubSource:
    repository: str
    ref: str

    def __post_init__(self) -> None:
        if not _REPOSITORY_PATTERN.fullmatch(self.repository):
            raise GitHubCheckoutError("GitHub repository must be in owner/repository form.")
        if not self.ref.strip() or len(self.ref) > 255 or any(character.isspace() for character in self.ref):
            raise GitHubCheckoutError("GitHub ref must be a non-empty branch, tag, or commit reference without whitespace.")


@dataclass(frozen=True, slots=True)
class ResolvedCheckout:
    root: Path
    commit: str


@contextmanager
def checkout(config: GitHubConfig, source: GitHubSource) -> Iterator[ResolvedCheckout]:
    """Clone one permitted ref into a temporary directory and resolve its exact SHA."""
    config.require_allowed(source.repository)
    with TemporaryDirectory(prefix="devsleuth-github-") as directory:
        root = Path(directory) / "repository"
        _run_git(
            ["git", "clone", "--depth", "1", "--branch", source.ref, _clone_url(source.repository), str(root)],
            config,
            "GitHub clone failed",
        )
        commit = _run_git(["git", "-C", str(root), "rev-parse", "HEAD"], config, "Could not resolve cloned commit").stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise GitHubCheckoutError("GitHub checkout did not resolve to a full commit SHA.")
        _run_git(["git", "-C", str(root), "status", "--porcelain"], config, "Could not verify cloned checkout")
        yield ResolvedCheckout(root=root, commit=commit)


def validate_git_available(config: GitHubConfig | None) -> None:
    """Avoid a startup surprise only when GitHub sources have actually been enabled."""
    if config is None:
        return
    _run_git(["git", "--version"], config, "Git is required for GitHub repository sources")


def _clone_url(repository: str) -> str:
    return f"https://github.com/{repository}.git"


def _run_git(command: list[str], config: GitHubConfig, message: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    if config.token:
        credential = base64.b64encode(f"x-access-token:{config.token}".encode("utf-8")).decode("ascii")
        environment["GIT_CONFIG_COUNT"] = "1"
        environment["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraheader"
        environment["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {credential}"
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False, env=environment)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GitHubCheckoutError(f"{message}: {error}") from error
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        suffix = f" ({detail})" if detail else ""
        raise GitHubCheckoutError(f"{message}.{suffix}")
    return completed
