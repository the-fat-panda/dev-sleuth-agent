"""Jira Cloud boundary for DevSleuthAgent's HTTP service layer.

This module contains only transport, webhook, and presentation concerns. It
does not import or alter the investigation engine, sandbox protocol, or
evidence rubric.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import hmac
import json
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from bugagent.domain import RunBundle, Ticket
from bugagent.github import GitHubCheckoutError, GitHubSource


class JiraConfigurationError(RuntimeError):
    """Jira configuration is incomplete or unsafe to use."""


class JiraWebhookError(ValueError):
    """An inbound Jira webhook cannot be authenticated or understood."""


class JiraAPIError(RuntimeError):
    """Jira Cloud rejected or could not receive an outbound request."""


@dataclass(frozen=True, slots=True)
class LocalProjectSource:
    """A local checkout mapping, kept for development and offline use."""

    repo_ref: str
    path: Path
    commit: str


@dataclass(frozen=True, slots=True)
class GitHubProjectSource:
    """A Jira project mapping to an allow-listed GitHub repository source."""

    repo_ref: str
    source: GitHubSource


ProjectSource = LocalProjectSource | GitHubProjectSource


@dataclass(frozen=True, slots=True)
class JiraConfig:
    base_url: str
    email: str
    api_token: str
    webhook_secret: str
    project_sources: Mapping[str, ProjectSource]

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> "JiraConfig | None":
        keys = (
            "BUGAGENT_JIRA_BASE_URL",
            "BUGAGENT_JIRA_EMAIL",
            "BUGAGENT_JIRA_API_TOKEN",
            "BUGAGENT_JIRA_WEBHOOK_SECRET",
            "BUGAGENT_JIRA_PROJECT_SOURCES",
        )
        values = {key: environment.get(key, "").strip() for key in keys}
        configured = [key for key, value in values.items() if value]
        if not configured:
            return None
        missing = [key for key, value in values.items() if not value]
        if missing:
            raise JiraConfigurationError(f"Jira integration is incomplete; set {', '.join(missing)}.")
        base_url = values["BUGAGENT_JIRA_BASE_URL"].rstrip("/")
        if not base_url.startswith("https://"):
            raise JiraConfigurationError("BUGAGENT_JIRA_BASE_URL must use HTTPS.")
        return cls(
            base_url=base_url,
            email=values["BUGAGENT_JIRA_EMAIL"],
            api_token=values["BUGAGENT_JIRA_API_TOKEN"],
            webhook_secret=values["BUGAGENT_JIRA_WEBHOOK_SECRET"],
            project_sources=_project_sources(values["BUGAGENT_JIRA_PROJECT_SOURCES"]),
        )


@dataclass(frozen=True, slots=True)
class JiraIssue:
    key: str
    issue_id: str
    project_key: str
    title: str
    body: str
    source_url: str | None

    def to_ticket(self, source: ProjectSource) -> Ticket:
        return Ticket(
            id=self.key,
            title=self.title,
            body=self.body,
            repo_ref=source.repo_ref,
            source_url=self.source_url,
        )


class JiraCommentClient(Protocol):
    def post_comment(self, issue_key: str, text: str) -> None:
        """Publish a plain-text DevSleuthAgent result as an ADF Jira comment."""


class JiraCloudClient:
    """Small stdlib client for the single Jira write operation we need today."""

    def __init__(self, config: JiraConfig) -> None:
        self._base_url = config.base_url
        credential = f"{config.email}:{config.api_token}".encode("utf-8")
        self._authorization = "Basic " + base64.b64encode(credential).decode("ascii")

    def post_comment(self, issue_key: str, text: str) -> None:
        body = json.dumps({"body": _adf_document(text)}).encode("utf-8")
        request = Request(
            f"{self._base_url}/rest/api/3/issue/{quote(issue_key, safe='')}/comment",
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Authorization": self._authorization,
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=10) as response:
                if response.status not in {200, 201}:
                    raise JiraAPIError(f"Jira returned HTTP {response.status} while posting a comment.")
        except HTTPError as error:
            detail = error.read(512).decode("utf-8", errors="replace")
            raise JiraAPIError(f"Jira returned HTTP {error.code} while posting a comment: {detail}") from error
        except URLError as error:
            raise JiraAPIError(f"Jira could not be reached: {error.reason}") from error


class JiraWebhookRouter:
    """Verifies incoming Jira Cloud webhooks and de-duplicates issue-created delivery."""

    def __init__(self, config: JiraConfig) -> None:
        self._config = config
        self._issue_jobs: dict[str, str] = {}
        self._lock = Lock()

    def issue_created(self, signature: str | None, payload: bytes) -> tuple[JiraIssue, LocalProjectSource]:
        if not verify_webhook_signature(self._config.webhook_secret, signature, payload):
            raise JiraWebhookError("Webhook signature did not match the configured Jira secret.")
        try:
            raw = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise JiraWebhookError("Webhook body must be UTF-8 JSON.") from error
        issue = parse_issue_created(raw)
        source = self._config.project_sources.get(issue.project_key)
        if source is None:
            raise JiraWebhookError(f"No repository source is configured for Jira project {issue.project_key!r}.")
        return issue, source

    def submit_once(
        self,
        issue: JiraIssue,
        source: LocalProjectSource,
        submit: Callable[[Ticket, LocalProjectSource], str],
    ) -> tuple[str, bool]:
        """Return an existing job for a retried delivery or enqueue exactly once."""
        with self._lock:
            existing = self._issue_jobs.get(issue.issue_id)
            if existing is not None:
                return existing, True
            job_id = submit(issue.to_ticket(source), source)
            self._issue_jobs[issue.issue_id] = job_id
            return job_id, False


def verify_webhook_signature(secret: str, header: str | None, payload: bytes) -> bool:
    """Validate Jira Cloud's `X-Hub-Signature: sha256=<hex>` header."""
    if not header or "=" not in header:
        return False
    method, received = header.split("=", maxsplit=1)
    if method.lower() != "sha256" or not received:
        return False
    expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received)


def parse_issue_created(payload: object) -> JiraIssue:
    """Extract the bounded ticket fields needed by the unchanged investigation core."""
    if not isinstance(payload, dict) or payload.get("webhookEvent") != "jira:issue_created":
        raise JiraWebhookError("Only jira:issue_created webhooks are accepted.")
    issue = payload.get("issue")
    if not isinstance(issue, dict):
        raise JiraWebhookError("Webhook does not contain an issue object.")
    fields = issue.get("fields")
    if not isinstance(fields, dict):
        raise JiraWebhookError("Webhook issue does not contain fields.")
    project = fields.get("project")
    if not isinstance(project, dict):
        raise JiraWebhookError("Webhook issue does not contain a project.")
    key = _required_string(issue.get("key"), "issue key")
    issue_id = _required_string(issue.get("id"), "issue id")
    project_key = _required_string(project.get("key"), "project key")
    title = _required_string(fields.get("summary"), "issue summary")
    body = _adf_text(fields.get("description")).strip()
    if not body:
        body = title
    source_url = issue.get("self") if isinstance(issue.get("self"), str) else None
    return JiraIssue(key, issue_id, project_key, title, body, source_url)


def format_investigation_comment(bundle: RunBundle) -> str:
    """Keep Jira comments concise while including a reviewer-useful evidence trail."""
    verdict = bundle.verdict
    lines = [
        "DevSleuthAgent investigation complete",
        "",
        f"Verdict: {verdict.status.value} ({verdict.evidence_score}/100, {verdict.confidence.value} confidence)",
        f"Evidence run: {bundle.run_id}",
    ]
    if bundle.candidates:
        candidate = bundle.candidates[-1]
        lines.extend(
            [
                f"Candidate test: {candidate.path}",
                f"Expected symptom: {candidate.expected_symptom}",
            ]
        )
    if bundle.evidence:
        observed = next((item for item in bundle.evidence if item.normalized_signature), None)
        if observed and observed.normalized_signature:
            lines.append(f"Observed result: {observed.normalized_signature}")
    if verdict.rationale:
        lines.extend(["", "What was tried:"])
        lines.extend(f"- {item}" for item in verdict.rationale)
    if verdict.blocking_questions:
        lines.extend(["", "Information needed:"])
        lines.extend(f"- {item}" for item in verdict.blocking_questions)
    return "\n".join(lines)


def _project_sources(raw: str) -> dict[str, ProjectSource]:
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as error:
        raise JiraConfigurationError("BUGAGENT_JIRA_PROJECT_SOURCES must be a JSON object.") from error
    if not isinstance(decoded, dict) or not decoded:
        raise JiraConfigurationError("BUGAGENT_JIRA_PROJECT_SOURCES must map one or more project keys to sources.")
    sources: dict[str, ProjectSource] = {}
    for project_key, item in decoded.items():
        if not isinstance(project_key, str) or not project_key.strip() or not isinstance(item, dict):
            raise JiraConfigurationError("Each Jira project source must have a non-empty project key and object value.")
        try:
            repo_ref = _required_string(item.get("repo_ref"), f"repository reference for {project_key}")
            kind = item.get("kind", "local_path")
            if kind == "local_path":
                path = Path(_required_string(item.get("path"), f"path for {project_key}")).expanduser().resolve()
                commit = _required_string(item.get("commit"), f"commit for {project_key}")
                sources[project_key] = LocalProjectSource(repo_ref=repo_ref, path=path, commit=commit)
            elif kind == "github":
                repository = _required_string(item.get("repository"), f"GitHub repository for {project_key}")
                ref = _required_string(item.get("ref"), f"GitHub ref for {project_key}")
                sources[project_key] = GitHubProjectSource(repo_ref=repo_ref, source=GitHubSource(repository, ref))
            else:
                raise JiraConfigurationError(f"Unsupported Jira project source kind {kind!r} for {project_key}.")
        except (JiraWebhookError, GitHubCheckoutError) as error:
            raise JiraConfigurationError(str(error)) from error
    return sources


def _required_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise JiraWebhookError(f"Webhook is missing {name}.")
    return value.strip()


def _adf_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return ""
    fragments: list[str] = []

    def visit(node: object) -> None:
        if not isinstance(node, dict):
            return
        text = node.get("text")
        if isinstance(text, str):
            fragments.append(text)
        content = node.get("content")
        if isinstance(content, list):
            for child in content:
                visit(child)
            if node.get("type") in {"paragraph", "heading", "listItem"}:
                fragments.append("\n")

    visit(value)
    return "".join(fragments)


def _adf_document(text: str) -> dict[str, object]:
    paragraphs = [line for line in text.splitlines() if line] or [""]
    return {
        "version": 1,
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": line}]}
            for line in paragraphs
        ],
    }
