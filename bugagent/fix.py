"""Bounded fix validation and explicitly gated GitHub pull-request publishing.

This module is deliberately separate from the investigation engine.  It never
edits the checkout used for reproduction: every candidate patch is applied in a
temporary copy, and publishing is disabled unless an operator opts in through
environment configuration and invokes :meth:`PullRequestPublisher.publish`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import difflib
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


class PatchProposalError(FixValidationError):
    """The model response is not a structurally safe unified-diff proposal."""


class PatchApplicationError(FixValidationError):
    """A structurally valid patch cannot be applied to the pinned checkout."""


class FixVerificationError(FixValidationError):
    """A patch applied but did not satisfy a required sandbox validation gate."""


class PullRequestPublishError(RuntimeError):
    """GitHub did not accept a deliberately requested pull-request publish."""


@dataclass(frozen=True, slots=True)
class FixEdit:
    """One line-addressed replacement in an existing safe source file."""

    path: str
    start_line: int
    end_line: int
    replacement_content: str


@dataclass(frozen=True, slots=True)
class FixProposal:
    """One model-proposed source patch; it is untrusted until validation succeeds."""

    summary: str
    rationale: str
    patch: str
    edits: tuple[FixEdit, ...] = ()


class FixClient(Protocol):
    def propose(
        self,
        ticket: Ticket,
        repository: ReadOnlyRepository,
        reproduction: CandidateTest,
        prior_feedback: tuple[str, ...] = (),
    ) -> FixProposal: ...


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

    def propose(
        self,
        ticket: Ticket,
        repository: ReadOnlyRepository,
        reproduction: CandidateTest,
        prior_feedback: tuple[str, ...] = (),
    ) -> FixProposal:
        payload = {
            "model": self.model,
            "store": False,
            "input": [
                {"role": "system", "content": _FIX_SYSTEM_INSTRUCTIONS},
                {"role": "user", "content": _fix_prompt(ticket, repository, reproduction, prior_feedback)},
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
            edits = _fix_edits_from_data(value["edits"])
            proposal = FixProposal(
                summary=str(value["summary"]),
                rationale=str(value["rationale"]),
                patch=_patch_from_structured_edits(repository, edits),
                edits=edits,
            )
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise PatchProposalError("OpenAI response did not contain a valid structured source-edit proposal.") from error
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

            # A malformed or stale diff must never consume a sandbox attempt.
            # `git apply --check` is non-mutating and runs in this disposable
            # checkout, so it is safe to give its exact failure back to the
            # model for one bounded correction attempt.
            _emit_progress(progress, "patch_preflight", "started", "Checking that the source patch applies cleanly")
            _preflight_patch(worktree, proposal.patch)
            _emit_progress(progress, "patch_preflight", "completed", "Source patch is structurally valid and applies cleanly")

            _emit_progress(progress, "regression_before", "started", "Confirming the reproduced regression fails before the patch")
            before = self._sandbox.run(worktree, candidate_path)
            if not _is_clean_failure(before):
                raise FixValidationError("The reproduced regression test did not fail cleanly before applying the patch.")
            _emit_progress(progress, "regression_before", "completed", "Regression failure confirmed before the patch")

            _emit_progress(progress, "patch_apply", "started", "Applying the proposed source-only patch")
            _apply_patch(worktree, proposal.patch, preflighted=True)
            changed_files = _changed_files(worktree)
            _validate_changed_files(changed_files)
            _emit_progress(progress, "patch_apply", "completed", "Source-only patch applied in disposable checkout")

            _emit_progress(progress, "regression_after", "started", "Confirming the regression passes after the patch")
            after = self._sandbox.run(worktree, candidate_path)
            if not _is_clean_pass(after):
                message = "The regression test did not pass cleanly after applying the patch."
                _emit_progress(progress, "regression_after", "failed", message)
                raise FixVerificationError(_validation_failure_detail(message, after))
            _emit_progress(progress, "regression_after", "completed", "Regression passes after the patch")

            _emit_progress(progress, "suite_validation", "started", "Running the repository test suite in the restricted sandbox")
            suite = self._suite_runner(worktree)
            if not suite.passed:
                message = "The selected repository test suite did not pass after applying the patch."
                _emit_progress(progress, "suite_validation", "failed", message)
                raise FixVerificationError(_suite_failure_detail(message, suite))
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


MAX_FIX_ATTEMPTS = 3


def propose_and_validate_fix(
    client: FixClient,
    validator: PatchValidator,
    *,
    ticket: Ticket,
    repository: ReadOnlyRepository,
    repo_root: Path,
    base_commit: str,
    reproduction: CandidateTest,
    progress: ValidationProgress | None = None,
    max_attempts: int = MAX_FIX_ATTEMPTS,
) -> ValidatedFix:
    """Generate a fix with bounded, evidence-backed correction attempts.

    Only proposal parsing, preflight application, and post-patch validation
    failures are retried.  Each attempt begins from the same pinned checkout;
    no patch is ever applied to the source repository or published remotely.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least one.")

    feedback: list[str] = []
    for attempt in range(1, max_attempts + 1):
        label = "Generating a minimal source-only patch" if attempt == 1 else "Generating a corrected source-only patch"
        _emit_progress(progress, "patch_generation", "started", label)
        proposal: FixProposal | None = None
        try:
            proposal = client.propose(ticket, repository, reproduction, tuple(feedback))
            _emit_progress(progress, "patch_generation", "completed", "Source-only patch proposed")
            return validator.validate(
                repo_root,
                base_commit=base_commit,
                proposal=proposal,
                reproduction=reproduction,
                progress=progress,
            )
        except (PatchProposalError, PatchApplicationError, FixVerificationError) as error:
            if attempt >= max_attempts:
                raise
            # Preserve the bounded sandbox result and previous source edit so
            # the next proposal can correct a semantic miss instead of blindly
            # repeating it.  Every value comes from the disposable checkout.
            feedback.append(_proposal_correction_feedback(proposal, error) if proposal is not None else str(error))
            retry_label = (
                "Patch did not meet sandbox validation; requesting a different minimal source edit"
                if isinstance(error, FixVerificationError)
                else "Source edit was rejected before sandbox validation; requesting a corrected line-addressed edit"
            )
            _emit_progress(
                progress,
                "patch_generation",
                "retrying",
                retry_label,
            )

    raise AssertionError("bounded fix attempts exited without a proposal result")


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
        raise PatchProposalError("Fix proposal must include a summary and rationale.")
    if not proposal.patch.strip() or len(proposal.patch.encode("utf-8")) > 100_000:
        raise PatchProposalError("Fix patch must be between 1 and 100,000 bytes.")
    if "diff --git " not in proposal.patch:
        raise PatchProposalError("Fix proposal must be a unified Git diff.")
    _validate_patch_headers(proposal.patch, error_type=PatchProposalError)


def _fix_edits_from_data(value: object) -> tuple[FixEdit, ...]:
    if not isinstance(value, list) or not value or len(value) > 3:
        raise PatchProposalError("Fix proposal must contain between one and three structured source edits.")
    edits: list[FixEdit] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise PatchProposalError("Each structured source edit must be an object.")
        try:
            path = item["path"]
            start_line = item["start_line"]
            end_line = item["end_line"]
            replacement_content = item["replacement_content"]
        except KeyError as error:
            raise PatchProposalError("Each structured source edit needs path, start_line, end_line, and replacement_content.") from error
        if not isinstance(path, str) or not isinstance(start_line, int) or not isinstance(end_line, int) or not isinstance(replacement_content, str):
            raise PatchProposalError("Structured source-edit fields must be a path, integer line range, and replacement string.")
        if not path or start_line < 1 or end_line < start_line:
            raise PatchProposalError("Structured source edits require a non-empty path and a valid 1-based line range.")
        if end_line - start_line >= 40 or len(replacement_content.encode("utf-8")) > 20_000:
            raise PatchProposalError("Structured source edit exceeds its bounded line-range or content limit.")
        edits.append(FixEdit(path, start_line, end_line, replacement_content))
    if len({edit.path for edit in edits}) != len(edits):
        raise PatchProposalError("Structured source edits may modify each file at most once.")
    return tuple(edits)


def _patch_from_structured_edits(repository: ReadOnlyRepository, edits: tuple[FixEdit, ...]) -> str:
    """Create canonical Git diff text from safe, model-selected line replacements.

    The model never supplies hunk headers or line counts.  It can only select
    a bounded 1-based line range from a numbered source excerpt in an existing
    Python file; this function derives the patch with :mod:`difflib` from the
    pinned checkout.
    """
    chunks: list[str] = []
    for edit in edits:
        _validate_patch_path(edit.path, error_type=PatchProposalError)
        if PurePosixPath(edit.path).suffix != ".py":
            raise PatchProposalError("Structured source edits may target existing Python source files only.")
        try:
            original = repository.read(edit.path)
        except (OSError, ValueError) as error:
            raise PatchProposalError(f"Structured source edit cannot read {edit.path!r}: {error}") from error
        original_lines = original.splitlines(keepends=True)
        if edit.end_line > len(original_lines):
            raise PatchProposalError(
                f"Structured source edit line range {edit.start_line}-{edit.end_line} exceeds {edit.path!r}, which has {len(original_lines)} lines."
            )
        replacement_lines = edit.replacement_content.splitlines(keepends=True)
        if len(replacement_lines) > 50:
            raise PatchProposalError("Structured source edit replacement exceeds the 50-line limit.")
        updated = "".join(
            original_lines[: edit.start_line - 1]
            + replacement_lines
            + original_lines[edit.end_line :]
        )
        if updated == original:
            raise PatchProposalError("Structured source edit must change the selected source line range.")
        diff = "".join(
            difflib.unified_diff(
                original_lines,
                updated.splitlines(keepends=True),
                fromfile=f"a/{edit.path}",
                tofile=f"b/{edit.path}",
            )
        )
        if not diff:
            raise PatchProposalError(f"Structured source edit did not produce a diff for {edit.path!r}.")
        chunks.append(f"diff --git a/{edit.path} b/{edit.path}\n{diff}")
    patch = "".join(chunks)
    if len(patch.encode("utf-8")) > 100_000:
        raise PatchProposalError("Generated source patch exceeds the 100,000 byte limit.")
    return patch


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


def _preflight_patch(worktree: Path, patch: str, *, config: GitHubConfig | None = None) -> None:
    """Confirm a patch can be parsed and applied without mutating the checkout."""
    _validate_patch_headers(patch, error_type=PatchProposalError)
    check_command = ["git", "-C", str(worktree), "apply", "--check", "--whitespace=error-all", "-"]
    try:
        _git_with_input(check_command, patch, config, "Patch did not apply cleanly")
    except FixValidationError as error:
        raise PatchApplicationError(str(error)) from error


def _apply_patch(
    worktree: Path,
    patch: str,
    *,
    config: GitHubConfig | None = None,
    preflighted: bool = False,
) -> None:
    if not preflighted:
        _preflight_patch(worktree, patch, config=config)
    apply_command = ["git", "-C", str(worktree), "apply", "--whitespace=error-all", "-"]
    _git_with_input(apply_command, patch, config, "Could not apply patch in disposable worktree")


def _validate_patch_headers(patch: str, *, error_type: type[FixValidationError] = FixValidationError) -> None:
    paths: list[str] = []
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            parts = line.split(maxsplit=3)
            if len(parts) != 4:
                raise error_type("Patch contains an invalid diff header.")
            paths.extend(part.removeprefix("a/").removeprefix("b/") for part in parts[2:])
    if not paths:
        raise error_type("Patch contains no file headers.")
    for path in paths:
        _validate_patch_path(path, error_type=error_type)


def _validate_patch_path(path: str, *, error_type: type[FixValidationError] = FixValidationError) -> None:
    pure = PurePosixPath(path)
    lowered = {part.lower() for part in pure.parts}
    forbidden = {".git", ".github", ".env", "secrets", "secret", "credentials", "keys"}
    if pure.is_absolute() or ".." in pure.parts or not path or lowered & forbidden:
        raise error_type(f"Patch targets a prohibited path: {path}")
    if pure.as_posix().startswith("tests/bugagent_generated/"):
        raise error_type("Fix patch may not modify the reproduced regression test.")


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


def _validation_failure_detail(message: str, run: SandboxRun) -> str:
    """Return a small, useful failure record for the next bounded proposal."""
    execution = run.execution
    if execution is None:
        detail = run.preflight.stderr or run.preflight.stdout or "The regression test did not execute."
    elif run.timed_out:
        detail = "The regression test timed out."
    else:
        detail = execution.stderr or execution.stdout or f"pytest exited with status {execution.exit_code}."
    return f"{message}\nSandbox feedback:\n{_bounded_feedback_text(detail)}"


def _suite_failure_detail(message: str, suite: SuiteResult) -> str:
    if suite.timed_out:
        detail = "The repository test suite timed out."
    else:
        detail = suite.stderr or suite.stdout or f"pytest exited with status {suite.exit_code}."
    return f"{message}\nSuite feedback:\n{_bounded_feedback_text(detail)}"


def _proposal_correction_feedback(proposal: FixProposal, error: FixValidationError) -> str:
    """Keep retry input specific while bounding model-visible prior output."""
    if proposal.edits:
        edits = "\n".join(
            f"- {edit.path}:{edit.start_line}-{edit.end_line} ->\n{_bounded_feedback_text(edit.replacement_content, 800)}"
            for edit in proposal.edits
        )
    else:
        edits = _bounded_feedback_text(proposal.patch, 2_000)
    return f"{error}\nPrevious proposed source edit(s):\n{edits}\nChoose a different minimal edit that satisfies the verified regression."


def _bounded_feedback_text(value: str, limit: int = 2_000) -> str:
    text = value.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n[feedback truncated]"


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
                "edits": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 3,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "path": {"type": "string"},
                            "start_line": {"type": "integer", "minimum": 1},
                            "end_line": {"type": "integer", "minimum": 1},
                            "replacement_content": {"type": "string"},
                        },
                        "required": ["path", "start_line", "end_line", "replacement_content"],
                    },
                },
            },
            "required": ["summary", "rationale", "edits"],
        },
    }


_FIX_SYSTEM_INSTRUCTIONS = """You propose exactly one minimal source-code fix for a verified Python regression.
Return structured source edits, never a unified diff or Markdown fence. Each edit targets one existing Python
source file and must supply the exact 1-based `start_line` and `end_line` from the numbered source excerpts,
plus replacement text for that range. The service validates the range against the pinned checkout and generates
the Git diff itself. Do not modify generated regression tests, CI configuration, dependencies, build files,
secrets, or GitHub files. Preserve public API compatibility. The generated patch will be applied to a disposable
checkout, then the existing verified regression and repository test suite must pass in restricted sandboxes. If
no safe minimal source fix is justified, return an out-of-range line number; it will be rejected rather than
published."""


def _fix_prompt(
    ticket: Ticket,
    repository: ReadOnlyRepository,
    reproduction: CandidateTest,
    prior_feedback: tuple[str, ...] = (),
) -> str:
    ticket_json = json.dumps(asdict(ticket), indent=2, sort_keys=True)
    feedback = "\n".join(f"- {item}" for item in prior_feedback) or "(none; this is the first patch attempt)"
    return "\n\n".join(
        (
            f"Ticket:\n{ticket_json}",
            "Verified regression test (already proven before patch):\n" + reproduction.content,
            "Prior structured-edit feedback (correct the stated line-range or validation error; do not repeat it):\n" + feedback,
            "Repository context:\n" + _fix_repository_context(repository, ticket),
        )
    )


def _fix_repository_context(repository: ReadOnlyRepository, ticket: Ticket) -> str:
    """Present source excerpts with stable, 1-based line numbers for edits."""
    context = repository.build_context(ticket)
    manifests = "\n".join(context.manifest) or "(no standard Python manifest found)"
    files = "\n".join(context.files) or "(no Python files found)"
    api_surface = "\n\n".join(item.as_prompt_text() for item in context.api_surface) or "(no public Python API could be indexed)"
    usage_examples = "\n\n".join(item.as_prompt_text() for item in context.usage_examples) or "(no matching existing test usage found)"
    snippets = "\n\n".join(
        f"### {path}\n" + "\n".join(f"{index:>4} | {line}" for index, line in enumerate(content.splitlines(), start=1))
        for path, content in context.snippets
    ) or "(no editable source excerpt available)"
    return (
        f"Repository manifests:\n{manifests}\n\n"
        f"Python files:\n{files}\n\n"
        f"Verified API surface (static source signatures; treat as ground truth):\n{api_surface}\n\n"
        f"Existing verified test usage (prefer these call patterns):\n{usage_examples}\n\n"
        f"Numbered editable source excerpts (use these exact line numbers):\n{snippets}"
    )
