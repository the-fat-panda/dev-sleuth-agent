"""Repository-source request models and checkout resolution for the HTTP API."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field

from bugagent.github import GitHubCheckoutError, GitHubConfig, GitHubSource, ResolvedCheckout, checkout
from bugagent.jira import GitHubProjectSource, LocalProjectSource, ProjectSource


class LocalRepositorySource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["local_path"] = "local_path"
    path: str = Field(min_length=1)
    commit: str = Field(min_length=1)


class GitHubRepositorySource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["github"] = "github"
    repository: str = Field(min_length=3, max_length=140)
    ref: str = Field(min_length=1, max_length=255)

    def to_source(self) -> GitHubSource:
        return GitHubSource(self.repository, self.ref)


RepositorySource = Annotated[LocalRepositorySource | GitHubRepositorySource, Field(discriminator="kind")]


@dataclass(frozen=True, slots=True)
class LocalCheckout:
    root: Path
    commit: str


def validate_submission_source(source: RepositorySource, github: GitHubConfig | None) -> None:
    """Fast request-time validation; cloning remains a background-worker operation."""
    if isinstance(source, LocalRepositorySource):
        path = Path(source.path).expanduser().resolve()
        if not path.is_dir():
            raise ValueError("repository.path must name an existing local directory.")
        return
    if github is None:
        raise ValueError("GitHub repository sources are not configured.")
    github.require_allowed(source.repository)


@contextmanager
def resolve_checkout(source: RepositorySource, github: GitHubConfig | None) -> Iterator[LocalCheckout | ResolvedCheckout]:
    """Yield the local checkout that the engine already expects."""
    if isinstance(source, LocalRepositorySource):
        path = Path(source.path).expanduser().resolve()
        if not path.is_dir():
            raise ValueError("repository.path must name an existing local directory.")
        yield LocalCheckout(root=path, commit=source.commit)
        return
    if github is None:
        raise GitHubCheckoutError("GitHub repository sources are not configured.")
    with checkout(github, source.to_source()) as resolved:
        yield resolved


def source_from_jira(source: ProjectSource) -> RepositorySource:
    if isinstance(source, LocalProjectSource):
        return LocalRepositorySource(path=str(source.path), commit=source.commit)
    if isinstance(source, GitHubProjectSource):
        return GitHubRepositorySource(repository=source.source.repository, ref=source.source.ref)
    raise TypeError(f"Unsupported Jira project source: {type(source)!r}")
