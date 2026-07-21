"""Bounded fix validation and explicitly gated GitHub pull-request publishing.

This module is deliberately separate from the investigation engine.  It never
edits the checkout used for reproduction: every candidate patch is applied in a
temporary copy, and publishing is disabled unless an operator opts in through
environment configuration and invokes :meth:`PullRequestPublisher.publish`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory
from typing import Any, Callable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

from bugagent.agent.client import DEFAULT_MODEL, InvestigationClientError, _extract_output_text, _send_request
from bugagent.agent.repository import ReadOnlyRepository, validate_candidate
from bugagent.domain import CandidateTest, Ticket
from bugagent.github import GitHubCheckoutError, GitHubConfig, GitHubSource, _run_git, checkout
from bugagent.sandbox import DockerSandbox, SandboxPolicy
from bugagent.sandbox.docker import SandboxRun


class FixValidationError(RuntimeError):
    """A patch cannot be safely treated as a validated fix."""


class PullRequestPublishError(RuntimeError):
    """GitHub did not accept a deliberately requested pull-request publish."""


@dataclass(frozen=True, slots=True)
class FixProposal:
    """One model-proposed source patch; it is untrusted until validation succeeds."""

    summary: str
    rationale: str
    patch: str


class FixClient(Protocol):
    def propose(self, ticket: Ticket, repository: ReadOnlyRepository, reproduction: CandidateTest) -> FixProposal: ...


class ResponsesFixClient:
    """Strict-schema Responses client for a single unified-diff source fix."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        request_sender: Callable[[Request, float], Any] | None = None,
    ) -> None:
        if not api_key.strip():
            raise InvestigationClientError("OPENAI_API_KEY is required for live fix preparation.")
        self._api_key = api_key
        self.model = model
        self._request_sender = request_sender or _send_request

    @classmethod
    def from_environment(cls, *, model: str = DEFAULT_MODEL) -> "ResponsesFixClient":
        return cls(os.environ.get("OPENAI_API_KEY", ""), model=model)

    def propose(self, ticket: Ticket, repository: ReadOnlyRepository, reproduction: CandidateTest) -> FixProposal:
        payload = {
            "model": self.model,
            "store": False,
            "input": [
                {"role": "system", "content": _FIX_SYSTEM_INSTRUCTIONS},
                {"role": "user", "content": _fix_prompt(ticket, repository, reproduction)},
            ],
            "text": {"format": _fix_schema()},
        }
        request = Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            response = self._request_sender(request, 90)
            raw = response.read().decode("utf-8")
            response.close()
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:500]
            raise InvestigationClientError(f"OpenAI fix request failed with HTTP {error.code}: {detail}") from error
        except URLError as error:
            raise InvestigationClientError(f"OpenAI fix request could not be completed: {error.reason}") from error

        try:
            value = json.loads(_extract_output_text(json.loads(raw)))
            proposal = FixProposal(summary=value["summary"], rationale=value["rationale"], patch=value["patch"])
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise InvestigationClientError("OpenAI response did not contain a valid fix proposal.") from error
        _validate_proposal(proposal)
        return proposal


@dataclass(frozen=True, slots=True)
class SuiteResult:
    command: tuple[str, ...]
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class PatchSandbox(Protocol):
    def run(self, repo_root: Path, candidate_path: Path) -> SandboxRun: ...


SuiteRunner = Callable[[Path], SuiteResult]
ValidationProgress = Callable[[str, str, str], None]


@dataclass(frozen=True, slots=True)
class ValidatedFix:
    """A patch that passed the before/after regression and selected suite gates."""

    proposal: FixProposal
    base_commit: str
    changed_files: tuple[str, ...]
    regression_path: str
    regression_content: str
    pre_patch_signature: str | None
    post_patch_exit_code: int | None
    suite: SuiteResult


@dataclass(frozen=True, slots=True)
class PullRequestPlan:
    """Persistable publish input.  Constructing it does not contact GitHub."""

    plan_id: UUID
    run_id: UUID
    repository: str
    base_branch: str
    base_commit: str
    head_branch: str
    title: str
    body: str
    patch: str
    regression_path: str
    regression_content: str
    created_at: datetime

    def as_json(self) -> dict[str, Any]:
        return _primitive(asdict(self))

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> "PullRequestPlan":
        try:
            return cls(
                plan_id=UUID(str(value["plan_id"])),
                run_id=UUID(str(value["run_id"])),
                repository=str(value["repository"]),
                base_branch=str(value["base_branch"]),
                base_commit=str(value["base_commit"]),
                head_branch=str(value["head_branch"]),
                title=str(value["title"]),
                body=str(value["body"]),
                patch=str(value["patch"]),
                regression_path=str(value["regression_path"]),
                regression_content=str(value["regression_content"]),
                created_at=datetime.fromisoformat(str(value["created_at"])),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise FixValidationError("Prepared pull-request plan is invalid.") from error


@dataclass(frozen=True, slots=True)
class PublishedPullRequest:
    repository: str
    number: int
    url: str
    branch: str
    commit: str


class PatchValidator:
    """Validate a source-only patch in a disposable worktree and restricted sandbox."""

    def __init__(
        self,
        policy: SandboxPolicy,
        *,
        sandbox: PatchSandbox | None = None,
        suite_runner: SuiteRunner | None = None,
    ) -> None:
        self._policy = policy
        self._sandbox = sandbox or DockerSandbox(policy)
        self._suite_runner = suite_runner or _sandbox_suite_runner(policy)

    def validate(
        self,
        repo_root: Path,
        *,
        base_commit: str,
        proposal: FixProposal,
        reproduction: CandidateTest,
        progress: ValidationProgress | None = None,
    ) -> ValidatedFix:
        _validate_proposal(proposal)
        regression = validate_candidate(reproduction)
        source = repo_root.resolve()
        if not source.is_dir():
            raise FixValidationError(f"Repository root does not exist: {source}")

        with TemporaryDirectory(prefix="devsleuth-fix-") as directory:
            worktree = Path(directory) / "repository"
            shutil.copytree(source, worktree, ignore=shutil.ignore_patterns(".git", ".bugagent", "__pycache__", ".pytest_cache"))
            candidate_path = worktree / regression.path
            candidate_path.parent.mkdir(parents=True, exist_ok=True)
            candidate_path.write_text(regression.content, encoding="utf-8", newline="\n")
            _initialize_local_repository(worktree)

            _emit_progress(progress, "regression_before", "started", "Confirming the reproduced regression fails before the patch")
            before = self._sandbox.run(worktree, candidate_path)
            if not _is_clean_failure(before):
                raise FixValidationError("The reproduced regression test did not fail cleanly before applying the patch.")
            _emit_progress(progress, "regression_before", "completed", "Regression failure confirmed before the patch")

            _emit_progress(progress, "patch_apply", "started", "Applying the proposed source-only patch")
            _apply_patch(worktree, proposal.patch)
            changed_files = _changed_files(worktree)
            _validate_changed_files(changed_files)
            _emit_progress(progress, "patch_apply", "completed", "Source-only patch applied in disposable checkout")

            _emit_progress(progress, "regression_after", "started", "Confirming the regression passes after the patch")
            after = self._sandbox.run(worktree, candidate_path)
            if not _is_clean_pass(after):
                raise FixValidationError("The regression test did not pass cleanly after applying the patch.")
            _emit_progress(progress, "regression_after", "completed", "Regression passes after the patch")

            _emit_progress(progress, "suite_validation", "started", "Running the repository test suite in the restricted sandbox")
            suite = self._suite_runner(worktree)
            if not suite.passed:
                raise FixValidationError("The selected repository test suite did not pass after applying the patch.")
            _emit_progress(progress, "suite_validation", "completed", "Repository test suite passed in the restricted sandbox")

        return ValidatedFix(
            proposal=proposal,
            base_commit=base_commit,
            changed_files=changed_files,
            regression_path=regression.path,
            regression_content=regression.content,
            pre_patch_signature=before.normalized_signature(),
            post_patch_exit_code=after.execution.exit_code if after.execution else None,
            suite=suite,
        )


def prepare_pull_request(
    validated: ValidatedFix,
    *,
    run_id: UUID,
    ticket: Ticket,
    repository: str,
    base_branch: str,
) -> PullRequestPlan:
    """Create a reviewable plan only after every validation gate has passed."""
    _validate_repository_and_ref(repository, base_branch)
    if not validated.suite.passed or validated.post_patch_exit_code != 0:
        raise FixValidationError("A pull-request plan requires a fully validated fix.")
    short_ticket = _slug(ticket.id, fallback="ticket")
    head_branch = f"devsleuth/fix-{short_ticket}-{str(run_id)[:8]}"
    title = f"fix: {ticket.title.strip()[:72]}"
    body = "\n".join(
        (
            "## DevSleuthAgent verified fix",
            "",
            f"Ticket: {ticket.source_url or ticket.id}",
            f"Evidence run: `{run_id}`",
            f"Reproduced commit: `{validated.base_commit}`",
            "",
            "### Validation",
            f"- [x] Regression failed before patch: `{validated.pre_patch_signature or 'recorded failure'}`",
            "- [x] Regression passed after patch in the restricted sandbox",
            "- [x] Repository test suite passed in the restricted sandbox",
            "",
            "### Proposed change",
            validated.proposal.summary.strip(),
            "",
            "This pull request was prepared from an evidence-verified reproduction. Review the diff before merge.",
        )
    )
    return PullRequestPlan(
        plan_id=uuid4(),
        run_id=run_id,
        repository=repository,
        base_branch=base_branch,
        base_commit=validated.base_commit,
        head_branch=head_branch,
        title=title,
        body=body,
        patch=validated.proposal.patch,
        regression_path=validated.regression_path,
        regression_content=validated.regression_content,
        created_at=datetime.now(timezone.utc),
    )


class PullRequestPublisher:
    """Publish a validated plan only after deliberate configuration and method-call approval."""

    def __init__(self, config: GitHubConfig, *, request_sender: Callable[[Request, float], Any] | None = None) -> None:
        self._config = config
        self._request_sender = request_sender or _send_request

    def publish(self, plan: PullRequestPlan) -> PublishedPullRequest:
        self._config.require_publish_access(plan.repository)
        _validate_repository_and_ref(plan.repository, plan.base_branch)
        _validate_branch(plan.head_branch)
        regression = CandidateTest(
            path=plan.regression_path,
            content=plan.regression_content,
            hypothesis="Validated regression test retained with the source fix.",
            expected_symptom="The verified reproduction is corrected.",
        )
        validate_candidate(regression)

        with checkout(self._config, GitHubSource(plan.repository, plan.base_branch)) as resolved:
            if resolved.commit != plan.base_commit:
                raise PullRequestPublishError(
                    "The base branch changed after validation; prepare and validate the fix again before publishing."
                )
            _run_git(["git", "-C", str(resolved.root), "switch", "-c", plan.head_branch], self._config, "Could not create PR branch")
            target = resolved.root / plan.regression_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(plan.regression_content, encoding="utf-8", newline="\n")
            _apply_patch(resolved.root, plan.patch, config=self._config)
            _run_git(["git", "-C", str(resolved.root), "diff", "--check"], self._config, "Patch whitespace check failed")
            _run_git(["git", "-C", str(resolved.root), "add", "--all"], self._config, "Could not stage validated PR changes")
            _run_git(
                [
                    "git",
                    "-C",
                    str(resolved.root),
                    "-c",
                    "user.name=DevSleuthAgent",
                    "-c",
                    "user.email=devsleuth-agent@users.noreply.github.com",
                    "commit",
                    "-m",
                    plan.title,
                ],
                self._config,
                "Could not create validated PR commit",
            )
            commit = _run_git(["git", "-C", str(resolved.root), "rev-parse", "HEAD"], self._config, "Could not resolve PR commit").stdout.strip()
            _run_git(
                ["git", "-C", str(resolved.root), "push", "origin", f"HEAD:refs/heads/{plan.head_branch}"],
                self._config,
                "Could not push validated PR branch",
            )

        payload = {"title": plan.title, "head": plan.head_branch, "base": plan.base_branch, "body": plan.body, "draft": True}
        request = Request(
            f"https://api.github.com/repos/{plan.repository}/pulls",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._config.token}",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            method="POST",
        )
        try:
            response = self._request_sender(request, 30)
            raw = response.read().decode("utf-8")
            response.close()
            result = json.loads(raw)
            return PublishedPullRequest(
                repository=plan.repository,
                number=int(result["number"]),
                url=str(result["html_url"]),
                branch=plan.head_branch,
                commit=commit,
            )
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:500]
            raise PullRequestPublishError(f"GitHub PR creation failed with HTTP {error.code}: {detail}") from error
        except (URLError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise PullRequestPublishError(f"GitHub PR creation could not be completed: {error}") from error


def write_pull_request_plan(plan: PullRequestPlan, output: Path) -> Path:
    """Persist a local plan for an operator to review or explicitly publish later."""
    destination = output.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"Prepared pull-request plan already exists: {destination}")
    destination.write_text(json.dumps(plan.as_json(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return destination


def _validate_proposal(proposal: FixProposal) -> None:
    if not proposal.summary.strip() or not proposal.rationale.strip():
        raise FixValidationError("Fix proposal must include a summary and rationale.")
    if not proposal.patch.strip() or len(proposal.patch.encode("utf-8")) > 100_000:
        raise FixValidationError("Fix patch must be between 1 and 100,000 bytes.")
    if "diff --git " not in proposal.patch:
        raise FixValidationError("Fix proposal must be a unified Git diff.")


def _initialize_local_repository(worktree: Path) -> None:
    _local_git(["git", "init", "--quiet", str(worktree)], "Could not initialize disposable validation repository")
    _local_git(["git", "-C", str(worktree), "add", "--all"], "Could not stage validation baseline")
    _local_git(
        [
            "git",
            "-C",
            str(worktree),
            "-c",
            "user.name=DevSleuthAgent",
            "-c",
            "user.email=devsleuth-agent@users.noreply.github.com",
            "commit",
            "--quiet",
            "-m",
            "validation baseline",
        ],
        "Could not commit validation baseline",
    )


def _apply_patch(worktree: Path, patch: str, *, config: GitHubConfig | None = None) -> None:
    _validate_patch_headers(patch)
    check_command = ["git", "-C", str(worktree), "apply", "--check", "--whitespace=error-all", "-"]
    _git_with_input(check_command, patch, config, "Patch did not apply cleanly")
    apply_command = ["git", "-C", str(worktree), "apply", "--whitespace=error-all", "-"]
    _git_with_input(apply_command, patch, config, "Could not apply patch in disposable worktree")


def _validate_patch_headers(patch: str) -> None:
    paths: list[str] = []
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            parts = line.split(maxsplit=3)
            if len(parts) != 4:
                raise FixValidationError("Patch contains an invalid diff header.")
            paths.extend(part.removeprefix("a/").removeprefix("b/") for part in parts[2:])
    if not paths:
        raise FixValidationError("Patch contains no file headers.")
    for path in paths:
        _validate_patch_path(path)


def _validate_patch_path(path: str) -> None:
    pure = PurePosixPath(path)
    lowered = {part.lower() for part in pure.parts}
    forbidden = {".git", ".github", ".env", "secrets", "secret", "credentials", "keys"}
    if pure.is_absolute() or ".." in pure.parts or not path or lowered & forbidden:
        raise FixValidationError(f"Patch targets a prohibited path: {path}")
    if pure.as_posix().startswith("tests/bugagent_generated/"):
        raise FixValidationError("Fix patch may not modify the reproduced regression test.")


def _changed_files(worktree: Path) -> tuple[str, ...]:
    completed = _local_git(["git", "-C", str(worktree), "diff", "--name-only", "--diff-filter=ACMR", "HEAD"], "Could not inspect patch files")
    changed = tuple(line.strip() for line in completed.stdout.splitlines() if line.strip())
    if not changed:
        raise FixValidationError("Fix patch did not change any repository file.")
    return changed


def _validate_changed_files(paths: tuple[str, ...]) -> None:
    for path in paths:
        _validate_patch_path(path)


def _is_clean_failure(run: SandboxRun) -> bool:
    return run.setup_valid and run.test_collected and run.test_failed and not run.timed_out


def _is_clean_pass(run: SandboxRun) -> bool:
    return bool(run.setup_valid and run.test_collected and run.execution and run.execution.exit_code == 0 and not run.timed_out)


def _emit_progress(progress: ValidationProgress | None, stage: str, state: str, label: str) -> None:
    if progress is not None:
        progress(stage, state, label)


def _sandbox_suite_runner(policy: SandboxPolicy) -> SuiteRunner:
    def run(worktree: Path) -> SuiteResult:
        command = policy.docker_prefix(worktree)
        command.extend(["python", "-m", "pytest", "-q", "--tb=short", "-p", "no:cacheprovider"])
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=policy.timeout_seconds, check=False)
        except subprocess.TimeoutExpired as error:
            return SuiteResult(tuple(command), None, _text(error.stdout), _text(error.stderr), True)
        except OSError as error:
            raise FixValidationError(f"Could not run repository suite in Docker: {error}") from error
        return SuiteResult(tuple(command), completed.returncode, completed.stdout or "", completed.stderr or "", False)

    return run


def _local_git(command: list[str], message: str) -> subprocess.CompletedProcess[str]:
    return _git_with_input(command, None, None, message)


def _git_with_input(command: list[str], data: str | None, config: GitHubConfig | None, message: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    if config and config.token:
        import base64

        credential = base64.b64encode(f"x-access-token:{config.token}".encode("utf-8")).decode("ascii")
        environment["GIT_CONFIG_COUNT"] = "1"
        environment["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraheader"
        environment["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {credential}"
    try:
        completed = subprocess.run(command, input=data, capture_output=True, text=True, timeout=60, check=False, env=environment)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise FixValidationError(f"{message}: {error}") from error
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        suffix = f" ({detail})" if detail else ""
        raise FixValidationError(f"{message}.{suffix}")
    return completed


def _validate_repository_and_ref(repository: str, ref: str) -> None:
    GitHubSource(repository, ref)


def _validate_branch(branch: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{2,120}", branch) or ".." in branch or branch.endswith("/"):
        raise FixValidationError("Pull-request branch name is invalid.")


def _slug(value: str, *, fallback: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return normalized[:40] or fallback


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


def _primitive(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _primitive(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_primitive(item) for item in value]
    return value


def _fix_schema() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "name": "devsleuth_fix_proposal",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "summary": {"type": "string"},
                "rationale": {"type": "string"},
                "patch": {"type": "string"},
            },
            "required": ["summary", "rationale", "patch"],
        },
    }


_FIX_SYSTEM_INSTRUCTIONS = """You propose exactly one minimal source-code fix for a verified Python regression.
Return a unified Git diff only; do not include Markdown fences. Do not modify the generated regression test,
CI configuration, dependencies, build files, secrets, or GitHub files. Preserve public API compatibility.
The patch will be applied to a disposable checkout, then the existing verified regression must pass and the
repository test suite must pass in a restricted sandbox. If a safe minimal source fix is not justified by
the supplied evidence and source, return an empty patch; it will be rejected rather than published."""


def _fix_prompt(ticket: Ticket, repository: ReadOnlyRepository, reproduction: CandidateTest) -> str:
    ticket_json = json.dumps(asdict(ticket), indent=2, sort_keys=True)
    return "\n\n".join(
        (
            f"Ticket:\n{ticket_json}",
            "Verified regression test (already proven before patch):\n" + reproduction.content,
            "Repository context:\n" + repository.build_context(ticket).as_prompt_text(),
        )
    )
