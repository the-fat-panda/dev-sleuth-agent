"""FastAPI service layer for asynchronous investigations."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
from threading import Lock
from typing import Callable, Iterator, Mapping
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from bugagent.agent import InvestigationOrchestrator, ResponsesInvestigationClient
from bugagent.agent.client import DEFAULT_MODEL
from bugagent.agent.repository import ReadOnlyRepository
from bugagent.artifacts import ArtifactStore, primitive
from bugagent.api_progress import InvestigationProgressReporter, ProgressInvestigationClient, ProgressSandbox
from bugagent.api_jobs import JobRegistry, JobSnapshot, JobState
from bugagent.api_repositories import (
    GitHubRepositorySource,
    RepositorySource,
    resolve_checkout,
    source_from_jira,
    validate_submission_source,
)
from bugagent.domain import RunBundle, Ticket, VerdictStatus
from bugagent.fix import (
    FixValidationError,
    PatchValidator,
    PublishedPullRequest,
    PullRequestPlan,
    PullRequestPublisher,
    ResponsesFixClient,
    propose_and_validate_fix,
    prepare_pull_request,
    write_pull_request_plan,
)
from bugagent.fix_jobs import FixJob, FixJobRegistry
from bugagent.github import GitHubCheckoutError, GitHubConfig, validate_git_available
from bugagent.jira import GitHubProjectSource, JiraCloudClient, JiraConfig, format_pull_request_comment
from bugagent.jira_api import attach_jira_routes
from bugagent.publish_jobs import PublicationJob, PublicationJobRegistry
from bugagent.sandbox import DockerSandbox, SandboxPolicy
from bugagent.web import RunStore
from bugagent.replay import _candidate_from_json


class APIConfigurationError(RuntimeError):
    """Required API runtime configuration is absent or unusable."""


@dataclass(frozen=True, slots=True)
class APIConfig:
    """Environment-derived settings for the HTTP service layer."""

    model: str
    sandbox_image: str
    max_attempts: int
    sandbox_timeout_seconds: int
    runs_root: Path
    jira: JiraConfig | None = None
    github: GitHubConfig | None = None

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> "APIConfig":
        source = os.environ if environment is None else environment
        image = source.get("BUGAGENT_SANDBOX_IMAGE", "").strip()
        if not image:
            raise APIConfigurationError("BUGAGENT_SANDBOX_IMAGE must be set to an immutable pytest image ID.")

        model = source.get("BUGAGENT_MODEL", DEFAULT_MODEL).strip()
        if not model:
            raise APIConfigurationError("BUGAGENT_MODEL must not be empty.")

        return cls(
            model=model,
            sandbox_image=image,
            max_attempts=_positive_integer(source, "BUGAGENT_MAX_ATTEMPTS", 3),
            sandbox_timeout_seconds=_positive_integer(source, "BUGAGENT_SANDBOX_TIMEOUT_SECONDS", 30),
            runs_root=Path(source.get("BUGAGENT_RUNS_ROOT", ".bugagent/runs")).expanduser().resolve(),
            jira=JiraConfig.from_environment(source),
            github=GitHubConfig.from_environment(source),
        )


def validate_runtime(config: APIConfig) -> None:
    """Fail startup before accepting work that cannot reach its prerequisites."""
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        raise APIConfigurationError("OPENAI_API_KEY is required to start the BugAgent API.")

    try:
        SandboxPolicy(image=config.sandbox_image, timeout_seconds=config.sandbox_timeout_seconds)
    except ValueError as error:
        raise APIConfigurationError(f"BUGAGENT_SANDBOX_IMAGE is not an immutable image ID: {error}") from error

    _run_docker_check(["docker", "info"], "Docker is unreachable")
    _run_docker_check(
        ["docker", "image", "inspect", config.sandbox_image],
        f"Configured pytest image is unavailable: {config.sandbox_image}",
    )
    validate_git_available(config.github)
    if config.jira:
        for project_key, source in config.jira.project_sources.items():
            try:
                validate_submission_source(source_from_jira(source), config.github)
            except (ValueError, GitHubCheckoutError) as error:
                raise APIConfigurationError(f"Configured Jira source for {project_key!r} is unavailable: {error}") from error


class TicketInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    body: str = Field(min_length=1)
    repo_ref: str = Field(min_length=1)
    expected_error: str | None = None

    def to_ticket(self) -> Ticket:
        return Ticket(
            id=self.id,
            title=self.title,
            body=self.body,
            repo_ref=self.repo_ref,
            expected_error=self.expected_error,
        )


class InvestigationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket: TicketInput
    repository: RepositorySource


class FixPreparationRequest(BaseModel):
    """A validated PR must target the same GitHub branch that was reproduced."""

    model_config = ConfigDict(extra="forbid")

    repository: GitHubRepositorySource
    base_branch: str = Field(min_length=1, max_length=255)


class PullRequestPublicationRequest(BaseModel):
    """The browser must make an explicit, deliberate publish confirmation."""

    model_config = ConfigDict(extra="forbid")

    confirm: bool


class AutomationModeRequest(BaseModel):
    """Explicit local-operator consent for autonomous Jira-to-draft-PR handling."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    confirm: bool = False


class AutonomousPublishMode:
    """A restart-safe default-off runtime switch; the API itself remains loopback-only."""

    def __init__(self) -> None:
        self._enabled = False
        self._lock = Lock()

    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._enabled = enabled


InvestigationRunner = Callable[[Ticket, Path, str], RunBundle]
InvestigationRunnerFactory = Callable[["InvestigationProgressReporter"], InvestigationRunner]
RuntimeValidator = Callable[[APIConfig], None]
JobCompletionCallback = Callable[[str, RunBundle], None]
PullRequestPublisherRunner = Callable[[PullRequestPlan, GitHubConfig], PublishedPullRequest]


def create_app(
    config: APIConfig | None = None,
    *,
    startup_validator: RuntimeValidator = validate_runtime,
    investigation_runner: InvestigationRunner | None = None,
    pull_request_publisher: PullRequestPublisherRunner | None = None,
) -> FastAPI:
    """Create an API app without importing or changing the investigation core."""
    selected_config = config or APIConfig.from_environment()
    registry = JobRegistry()
    fix_registry = FixJobRegistry()
    publication_registry = PublicationJobRegistry()
    autonomous_mode = AutonomousPublishMode()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bugagent-investigation")
    fix_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bugagent-fix")
    publication_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bugagent-publication")
    runner_factory: InvestigationRunnerFactory
    if investigation_runner is None:
        runner_factory = lambda progress: _live_runner(selected_config, progress)
    else:
        runner_factory = lambda progress: investigation_runner
    publisher_runner = pull_request_publisher or (lambda plan, github: PullRequestPublisher(github).publish(plan))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        startup_validator(selected_config)
        try:
            yield
        finally:
            executor.shutdown(wait=False, cancel_futures=False)
            fix_executor.shutdown(wait=False, cancel_futures=False)
            publication_executor.shutdown(wait=False, cancel_futures=False)

    app = FastAPI(
        title="DevSleuthAgent Investigation API",
        version="0.1.0",
        description="Asynchronous ticket-to-evidence investigations over local source checkouts.",
        lifespan=lifespan,
    )
    app.state.config = selected_config
    app.state.jobs = registry
    app.state.fix_jobs = fix_registry
    app.state.publication_jobs = publication_registry
    app.state.autonomous_mode = autonomous_mode

    def enqueue(
        ticket: Ticket,
        repository: RepositorySource,
        on_completed: JobCompletionCallback | None = None,
        *,
        source: str = "manual",
        issue_key: str | None = None,
        issue_url: str | None = None,
    ) -> JobSnapshot:
        job = registry.create(
            ticket,
            source=source,
            issue_key=issue_key,
            issue_url=issue_url,
        )
        if source == "jira":
            registry.emit(
                job.job_id,
                "jira_intake",
                "completed",
                "Jira ticket accepted",
                detail={"issue_key": issue_key or ticket.id},
            )
        executor.submit(_run_job, registry, job.job_id, runner_factory, ticket, repository, selected_config.github, on_completed)
        return job

    @app.post("/investigations", status_code=status.HTTP_202_ACCEPTED)
    def start_investigation(request: InvestigationRequest) -> dict[str, object]:
        try:
            validate_submission_source(request.repository, selected_config.github)
        except (ValueError, GitHubCheckoutError) as error:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)) from error
        job = enqueue(request.ticket.to_ticket(), request.repository)
        return _job_response(job)

    @app.get("/investigations")
    def list_investigations(limit: int = Query(default=25, ge=1, le=100)) -> dict[str, object]:
        """List in-process work so webhook-started jobs are observable in the UI."""
        return {"jobs": [_job_response(job) for job in registry.list_recent(limit)]}

    @app.get("/investigations/{job_id}")
    def get_investigation(job_id: str) -> dict[str, object]:
        job = registry.get(job_id)
        if job is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Investigation job not found.")
        return _job_response(job)

    @app.get("/investigations/{job_id}/events")
    def stream_investigation_events(
        job_id: str,
        request: Request,
        after: int = Query(default=0, ge=0),
    ) -> StreamingResponse:
        if registry.get(job_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Investigation job not found.")
        cursor = _event_cursor(request, after)
        return StreamingResponse(
            _sse_events(registry, job_id, cursor),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/automation/yolo")
    def get_yolo_mode() -> dict[str, object]:
        capability = _yolo_capability(selected_config)
        return {"enabled": autonomous_mode.enabled(), **capability}

    @app.put("/automation/yolo")
    def set_yolo_mode(request: AutomationModeRequest) -> dict[str, object]:
        capability = _yolo_capability(selected_config)
        if request.enabled:
            if not request.confirm:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="Set confirm=true to enable YOLO mode for future Jira tickets.",
                )
            if not capability["available"]:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(capability["reason"]))
        autonomous_mode.set_enabled(request.enabled)
        return {"enabled": autonomous_mode.enabled(), **capability}

    @app.get("/runs")
    def list_runs() -> dict[str, object]:
        runs = RunStore(selected_config.runs_root).list_runs()
        return {"runs": primitive([_run_summary_with_fix_status(run, selected_config.runs_root) for run in runs])}

    @app.get("/runs/{run_id}")
    def get_run(run_id: str) -> object:
        run = RunStore(selected_config.runs_root).get_run(run_id)
        if run is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.")
        run["fix"] = _fix_status_for_run(selected_config.runs_root, run_id)
        return primitive(run)

    @app.post("/runs/{run_id}/fixes", status_code=status.HTTP_202_ACCEPTED)
    def prepare_fix(run_id: str, request: FixPreparationRequest) -> dict[str, object]:
        run = RunStore(selected_config.runs_root).get_run(run_id)
        if run is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.")
        if run["verdict"].get("status") != "REPRODUCED":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Only a REPRODUCED evidence bundle can start fix preparation.",
            )
        try:
            validate_submission_source(request.repository, selected_config.github)
            _validate_fix_request_source(run, request)
        except (ValueError, GitHubCheckoutError) as error:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)) from error
        job = fix_registry.create(run_id)
        fix_executor.submit(_run_fix_job, fix_registry, job.job_id, run, request, selected_config)
        return _fix_job_response(job)

    @app.get("/fixes/{job_id}")
    def get_fix_job(job_id: str) -> dict[str, object]:
        job = fix_registry.get(job_id)
        if job is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Fix preparation job not found.")
        return _fix_job_response(job)

    @app.get("/runs/{run_id}/fix-status")
    def get_run_fix_status(run_id: str) -> dict[str, object]:
        """Let an evidence view reattach to a YOLO-started fix job without a page refresh."""
        run = RunStore(selected_config.runs_root).get_run(run_id)
        if run is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.")
        if job := fix_registry.latest_for_run(run_id):
            return _fix_job_response(job)
        if plan := _prepared_plan_for_run(selected_config.runs_root, run_id):
            plan_id = plan.get("plan_id")
            if isinstance(plan_id, str):
                return {
                    "run_id": run_id,
                    "status": "done",
                    "plan_id": plan_id,
                    "plan_url": f"/pull-request-plans/{plan_id}",
                    "progress": {
                        "stage": "pr_plan",
                        "state": "completed",
                        "label": "Validated local PR plan ready",
                    },
                    "events": [],
                }
        if failed := _failed_fix_status_for_run(selected_config.runs_root, run_id):
            return failed
        return {"run_id": run_id, "status": "not_started"}

    @app.get("/pull-request-plans/{plan_id}")
    def get_pull_request_plan(plan_id: str) -> dict[str, object]:
        plan_path = _prepared_plan_path(selected_config.runs_root, plan_id)
        try:
            payload = json.loads(plan_path.read_text(encoding="utf-8"))
        except OSError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Prepared pull-request plan not found.") from error
        publication = _publication_for_plan(selected_config.runs_root, plan_id)
        if publication is not None:
            payload["publication"] = publication
        repository = payload.get("repository")
        if isinstance(repository, str):
            payload["publication_capability"] = _publication_capability(selected_config.github, repository)
        return payload

    @app.get("/pull-request-plans/{plan_id}/publication-status")
    def get_pull_request_plan_publication_status(plan_id: str) -> dict[str, object]:
        """Expose an autonomous publication handoff without making a GitHub write."""
        try:
            _read_pull_request_plan(selected_config.runs_root, plan_id)
        except (OSError, ValueError, FixValidationError, json.JSONDecodeError) as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        if publication := _publication_for_plan(selected_config.runs_root, plan_id):
            return {
                "plan_id": plan_id,
                "status": "done",
                "publication": publication,
            }
        if job := publication_registry.get_for_plan(plan_id):
            return _publication_job_response(job)
        return {"plan_id": plan_id, "status": "not_started"}

    @app.get("/runs/{run_id}/pull-request-plan")
    def get_run_pull_request_plan(run_id: str) -> dict[str, object]:
        plan = _prepared_plan_for_run(selected_config.runs_root, run_id)
        if plan is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No prepared pull-request plan for this run.")
        plan_id = plan.get("plan_id")
        if isinstance(plan_id, str) and (publication := _publication_for_plan(selected_config.runs_root, plan_id)) is not None:
            plan["publication"] = publication
        repository = plan.get("repository")
        if isinstance(repository, str):
            plan["publication_capability"] = _publication_capability(selected_config.github, repository)
        return plan

    @app.post("/pull-request-plans/{plan_id}/publish", status_code=status.HTTP_202_ACCEPTED)
    def publish_pull_request(plan_id: str, request: PullRequestPublicationRequest) -> dict[str, object]:
        if not request.confirm:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Set confirm=true to publish the validated draft pull request.",
            )
        try:
            plan = _read_pull_request_plan(selected_config.runs_root, plan_id)
        except (OSError, ValueError, FixValidationError, json.JSONDecodeError) as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        if _publication_for_plan(selected_config.runs_root, str(plan.plan_id)) is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This validated plan has already published a draft pull request.",
            )
        if selected_config.github is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="GitHub configuration is required to publish a pull request.")
        try:
            selected_config.github.require_publish_access(plan.repository)
        except GitHubCheckoutError as error:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)) from error
        run = RunStore(selected_config.runs_root).get_run(str(plan.run_id))
        if run is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="The evidence bundle for this validated plan is no longer available.")
        job, created = publication_registry.create(str(plan.run_id), str(plan.plan_id))
        if created:
            publication_executor.submit(
                _run_publication_job,
                publication_registry,
                job.job_id,
                plan,
                run,
                selected_config,
                publisher_runner,
            )
        return _publication_job_response(job)

    @app.get("/pull-request-publications/{job_id}")
    def get_pull_request_publication(job_id: str) -> dict[str, object]:
        job = publication_registry.get(job_id)
        if job is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pull-request publication job not found.")
        return _publication_job_response(job)

    @app.delete("/runs")
    def clear_runs() -> dict[str, int]:
        """Permanently remove completed bundles from the local demo history."""
        return {"deleted_run_count": _clear_stored_runs(selected_config.runs_root)}

    attach_jira_routes(
        app,
        selected_config.jira,
        submit=lambda ticket, source, callback, issue: enqueue(
            ticket,
            source_from_jira(source),
            callback,
            source="jira",
            issue_key=issue.key,
            issue_url=issue.source_url,
        ).job_id,
        get_job=lambda job_id: _job_response(job) if (job := registry.get(job_id)) else None,
        emit_progress=registry.emit,
        after_completion=lambda job_id, bundle, issue, source, yolo_requested: _continue_yolo_from_jira(
            registry,
            job_id,
            bundle,
            issue.key,
            source,
            selected_config,
            fix_registry,
            publication_registry,
            fix_executor,
            publication_executor,
            publisher_runner,
            yolo_requested,
        ),
        autonomous_requested=lambda _issue, _source: autonomous_mode.enabled(),
    )

    ui_root = Path(__file__).resolve().parents[1] / "ui"

    @app.get("/", include_in_schema=False)
    def application_root() -> RedirectResponse:
        return RedirectResponse(url="/app/")

    app.mount("/app", StaticFiles(directory=ui_root, html=True), name="bugagent-ui")

    return app


def main() -> None:
    """Run the API server after loading required environment configuration."""
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=8001)


def _live_runner(config: APIConfig, progress: "InvestigationProgressReporter") -> InvestigationRunner:
    def run(ticket: Ticket, repo_root: Path, repo_commit: str) -> RunBundle:
        bundle = InvestigationOrchestrator(
            ProgressInvestigationClient(ResponsesInvestigationClient.from_environment(model=config.model), progress),
            ProgressSandbox(
                DockerSandbox(SandboxPolicy(image=config.sandbox_image, timeout_seconds=config.sandbox_timeout_seconds)),
                progress,
            ),
            max_attempts=config.max_attempts,
            prompt_version=f"live-{config.model}-v1",
        ).investigate(ticket, repo_root, repo_commit)
        progress.begin_verdict()
        ArtifactStore(config.runs_root).write(bundle)
        return bundle

    return run


def _run_job(
    registry: JobRegistry,
    job_id: str,
    runner_factory: InvestigationRunnerFactory,
    ticket: Ticket,
    repository: RepositorySource,
    github: GitHubConfig | None,
    on_completed: JobCompletionCallback | None = None,
) -> None:
    registry.mark_running(job_id)
    progress = InvestigationProgressReporter(registry, job_id)
    try:
        runner = runner_factory(progress)
        if isinstance(repository, GitHubRepositorySource):
            registry.emit(
                job_id,
                "github_checkout",
                "started",
                "Cloning GitHub repository",
                detail={"repository": repository.repository, "ref": repository.ref},
            )
        with resolve_checkout(repository, github) as resolved:
            if isinstance(repository, GitHubRepositorySource):
                registry.emit(
                    job_id,
                    "github_checkout",
                    "completed",
                    "GitHub checkout pinned",
                    detail={"repository": repository.repository, "commit": resolved.commit},
                )
            bundle = runner(ticket, resolved.root, resolved.commit)
    except Exception as error:
        registry.mark_failed(job_id, error)
        return
    if on_completed is not None:
        try:
            on_completed(job_id, bundle)
        except Exception as error:  # A completed investigation must not be hidden by an integration callback.
            registry.emit(
                job_id,
                "automation",
                "failed",
                "Post-investigation automation stopped",
                detail={"error": f"{type(error).__name__}: {error}"},
            )
    registry.mark_done(job_id, bundle)


def _continue_yolo_from_jira(
    registry: JobRegistry,
    job_id: str,
    bundle: RunBundle,
    issue_key: str,
    source: object,
    config: APIConfig,
    fix_registry: FixJobRegistry,
    publication_registry: PublicationJobRegistry,
    fix_executor: ThreadPoolExecutor,
    publication_executor: ThreadPoolExecutor,
    publisher: PullRequestPublisherRunner,
    yolo_requested: bool,
) -> None:
    """Queue the remainder of a Jira workflow only after explicit YOLO-mode consent."""
    if not yolo_requested:
        registry.emit(job_id, "yolo", "skipped", "YOLO was off when this Jira ticket was accepted")
        return
    if bundle.verdict.status != VerdictStatus.REPRODUCED:
        registry.emit(job_id, "yolo", "skipped", "YOLO stopped because the ticket was not reproduced")
        return
    if not isinstance(source, GitHubProjectSource) or config.github is None:
        registry.emit(job_id, "yolo", "failed", "YOLO could not find a publishable GitHub mapping")
        _post_yolo_failure_comment(config, issue_key, "repository mapping", "This Jira project is not mapped to a publishable GitHub repository.")
        return
    repository = source_from_jira(source)
    if not isinstance(repository, GitHubRepositorySource):
        registry.emit(job_id, "yolo", "failed", "YOLO could not create a GitHub source from the Jira mapping")
        _post_yolo_failure_comment(config, issue_key, "repository mapping", "The mapped repository source cannot create a GitHub draft pull request.")
        return
    try:
        config.github.require_publish_access(repository.repository)
    except GitHubCheckoutError as error:
        registry.emit(job_id, "yolo", "failed", "YOLO GitHub publication setup needs review", detail={"error": str(error)})
        _post_yolo_failure_comment(config, issue_key, "GitHub publication setup", str(error))
        return
    run = RunStore(config.runs_root).get_run(str(bundle.run_id))
    if run is None:
        registry.emit(job_id, "yolo", "failed", "YOLO could not load the completed evidence bundle")
        _post_yolo_failure_comment(config, issue_key, "fix preparation", "The completed evidence bundle could not be loaded.")
        return
    try:
        registry.emit(job_id, "yolo", "started", "YOLO is validating a fix before it can open a draft PR")
        request = FixPreparationRequest(repository=repository, base_branch=repository.ref)
        job = fix_registry.create(str(bundle.run_id), autonomous=True)
        fix_executor.submit(
            _run_fix_job,
            fix_registry,
            job.job_id,
            run,
            request,
            config,
            lambda plan: _queue_yolo_publication(
                plan,
                run,
                issue_key,
                config,
                publication_registry,
                publication_executor,
                publisher,
            ),
            lambda error: _post_yolo_failure_comment(config, issue_key, "fix validation", str(error)),
        )
        registry.emit(
            job_id,
            "yolo",
            "completed",
            "YOLO fix validation queued; GitHub publication will follow only after validation passes",
            detail={"fix_job_id": job.job_id},
        )
    except Exception as error:
        registry.emit(
            job_id,
            "yolo",
            "failed",
            "YOLO could not start fix validation",
            detail={"error": f"{type(error).__name__}: {error}"},
        )
        _post_yolo_failure_comment(config, issue_key, "fix preparation", f"{type(error).__name__}: {error}")


def _queue_yolo_publication(
    plan: PullRequestPlan,
    run: dict[str, object],
    issue_key: str,
    config: APIConfig,
    registry: PublicationJobRegistry,
    executor: ThreadPoolExecutor,
    publisher: PullRequestPublisherRunner,
) -> None:
    job, created = registry.create(str(plan.run_id), str(plan.plan_id))
    if not created:
        return
    executor.submit(
        _run_publication_job,
        registry,
        job.job_id,
        plan,
        run,
        config,
        publisher,
        lambda error: _post_yolo_failure_comment(config, issue_key, "draft pull-request publication", str(error)),
    )


def _post_yolo_failure_comment(config: APIConfig, issue_key: str, stage: str, detail: str) -> None:
    """Leave Jira with the automation outcome when a later autonomous step cannot continue."""
    if config.jira is None:
        return
    message = "\n".join(
        (
            "DevSleuthAgent autonomous workflow stopped",
            "",
            f"Stage: {stage}",
            f"Result: {detail}",
            "No draft pull request was created by this run.",
        )
    )
    try:
        JiraCloudClient(config.jira).post_comment(issue_key, message)
    except Exception:
        # The primary job already retains the failure; never hide it behind a secondary comment error.
        return


def _run_fix_job(
    registry: FixJobRegistry,
    job_id: str,
    run: dict[str, object],
    request: FixPreparationRequest,
    config: APIConfig,
    on_completed: Callable[[PullRequestPlan], None] | None = None,
    on_failed: Callable[[Exception], None] | None = None,
) -> None:
    """Generate and validate a patch off the event loop; never publish it."""
    registry.mark_running(job_id)
    try:
        if config.github is None:
            raise GitHubCheckoutError("GitHub configuration is required for fix preparation.")
        ticket_json = run["ticket"]
        candidates_json = run["candidates"]
        manifest = run["manifest"]
        if not isinstance(ticket_json, dict) or not isinstance(candidates_json, list) or not candidates_json or not isinstance(manifest, dict):
            raise ValueError("Stored bundle is missing ticket, candidate, or manifest data.")
        ticket = Ticket(**ticket_json)
        candidate = _candidate_from_json(candidates_json[0])
        base_commit = str(manifest["repo_commit"])
        run_id = UUID(str(manifest["run_id"]))

        registry.emit(job_id, "repository_checkout", "started", "Cloning the reproduced GitHub branch")
        with resolve_checkout(request.repository, config.github) as resolved:
            if resolved.commit != base_commit:
                raise ValueError(
                    "The target branch no longer matches the reproduced commit; investigate and validate again before preparing a fix."
                )
            registry.emit(job_id, "repository_checkout", "completed", "Reproduced GitHub commit pinned")
            validated = propose_and_validate_fix(
                ResponsesFixClient.from_environment(model=config.model),
                PatchValidator(SandboxPolicy(image=config.sandbox_image, timeout_seconds=config.sandbox_timeout_seconds)),
                ticket=ticket,
                repository=ReadOnlyRepository(resolved.root),
                repo_root=resolved.root,
                base_commit=base_commit,
                reproduction=candidate,
                progress=lambda stage, state, label: registry.emit(job_id, stage, state, label),
            )
        registry.emit(job_id, "pr_plan", "started", "Writing the validated local PR plan")
        plan = prepare_pull_request(
            validated,
            run_id=run_id,
            ticket=ticket,
            repository=request.repository.repository,
            base_branch=request.base_branch,
        )
        destination = _prepared_plan_path(config.runs_root, str(plan.plan_id))
        write_pull_request_plan(plan, destination)
    except Exception as error:
        registry.mark_failed(job_id, error)
        if failed_job := registry.get(job_id):
            try:
                _write_failed_fix_status(config.runs_root, _fix_job_response(failed_job))
            except OSError:
                # The in-memory failure remains available for this process even
                # if a local disk write is unavailable.
                pass
        if on_failed is not None:
            on_failed(error)
        return
    registry.mark_done(job_id, plan_id=str(plan.plan_id), plan_path=str(destination))
    if on_completed is not None:
        on_completed(plan)


def _run_publication_job(
    registry: PublicationJobRegistry,
    job_id: str,
    plan: PullRequestPlan,
    run: dict[str, object],
    config: APIConfig,
    publisher: PullRequestPublisherRunner,
    on_failed: Callable[[Exception], None] | None = None,
) -> None:
    """Publish one already-validated plan after a separate explicit API confirmation."""
    registry.mark_running(job_id)
    try:
        if config.github is None:
            raise GitHubCheckoutError("GitHub configuration is required to publish a pull request.")
        registry.emit(
            job_id,
            "github_publish",
            "started",
            "Rechecking the pinned base commit, pushing the validated branch, and opening a draft PR",
        )
        published = publisher(plan, config.github)
        publication = _publication_record(plan, published)
        _write_publication_record(config.runs_root, plan, publication)
        registry.emit(job_id, "github_publish", "completed", "Draft pull request opened on GitHub")
        _post_jira_pull_request_comment(registry, job_id, run, plan, publication, config)
        _write_publication_record(config.runs_root, plan, publication)
    except Exception as error:
        registry.mark_failed(job_id, error)
        if on_failed is not None:
            on_failed(error)
        return
    registry.mark_done(job_id, publication)


def _post_jira_pull_request_comment(
    registry: PublicationJobRegistry,
    job_id: str,
    run: dict[str, object],
    plan: PullRequestPlan,
    publication: dict[str, object],
    config: APIConfig,
) -> None:
    ticket_json = run.get("ticket")
    if not isinstance(ticket_json, dict):
        publication["jira_comment"] = {"status": "not_applicable"}
        return
    ticket_id = ticket_json.get("id")
    if not isinstance(ticket_id, str) or not _is_configured_jira_ticket(config.jira, ticket_id):
        publication["jira_comment"] = {"status": "not_applicable"}
        return
    pull_request = publication["pull_request"]
    if not isinstance(pull_request, dict):
        raise ValueError("Published pull-request record is malformed.")
    try:
        registry.emit(job_id, "jira_comment", "started", "Posting the draft pull-request link to Jira")
        JiraCloudClient(config.jira).post_comment(
            ticket_id,
            format_pull_request_comment(
                ticket_id=ticket_id,
                run_id=str(plan.run_id),
                pull_request_url=str(pull_request["url"]),
                branch=str(pull_request["branch"]),
                commit=str(pull_request["commit"]),
            ),
        )
    except Exception as error:
        publication["jira_comment"] = {"status": "failed", "error": f"{type(error).__name__}: {error}"}
        registry.emit(job_id, "jira_comment", "failed", "Draft PR was opened, but the Jira comment could not be posted")
    else:
        publication["jira_comment"] = {"status": "posted", "issue_key": ticket_id}
        registry.emit(job_id, "jira_comment", "completed", "Draft PR link posted to Jira")


def _job_response(job: JobSnapshot) -> dict[str, object]:
    payload: dict[str, object] = {
        "job_id": job.job_id,
        "status": job.status.value,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
        "status_url": f"/investigations/{job.job_id}",
        "events_url": f"/investigations/{job.job_id}/events",
        "source": job.source,
        "ticket": {
            "id": job.ticket_id,
            "title": job.ticket_title,
            "repo_ref": job.repo_ref,
        },
    }
    if job.issue_key:
        payload["issue_key"] = job.issue_key
    if job.issue_url:
        payload["issue_url"] = job.issue_url
    if job.run_id:
        payload["run_id"] = job.run_id
        payload["run_url"] = f"/runs/{job.run_id}"
    if job.verdict:
        payload["verdict"] = job.verdict
    if job.error:
        payload["error"] = job.error
    return payload


def _fix_job_response(job: FixJob) -> dict[str, object]:
    payload: dict[str, object] = {
        "job_id": job.job_id,
        "run_id": job.run_id,
        "status": job.status.value,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
        "status_url": f"/fixes/{job.job_id}",
        "published": False,
        "autonomous": job.autonomous,
        "progress": {
            "stage": job.current_stage,
            "state": job.current_state,
            "label": job.current_label,
        },
        "events": [
            {
                "sequence": event.sequence,
                "stage": event.stage,
                "state": event.state,
                "label": event.label,
                "occurred_at": event.occurred_at.isoformat(),
            }
            for event in job.events
        ],
    }
    if job.plan_id:
        payload["plan_id"] = job.plan_id
        payload["plan_url"] = f"/pull-request-plans/{job.plan_id}"
    if job.error:
        payload["error"] = job.error
    return payload


def _publication_job_response(job: PublicationJob) -> dict[str, object]:
    payload: dict[str, object] = {
        "job_id": job.job_id,
        "run_id": job.run_id,
        "plan_id": job.plan_id,
        "status": job.status.value,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
        "status_url": f"/pull-request-publications/{job.job_id}",
        "progress": {
            "stage": job.current_stage,
            "state": job.current_state,
            "label": job.current_label,
        },
        "events": [
            {
                "sequence": event.sequence,
                "stage": event.stage,
                "state": event.state,
                "label": event.label,
                "occurred_at": event.occurred_at.isoformat(),
            }
            for event in job.events
        ],
    }
    if job.publication is not None:
        payload["publication"] = job.publication
    if job.error:
        payload["error"] = job.error
    return payload


def _validate_fix_request_source(run: dict[str, object], request: FixPreparationRequest) -> None:
    ticket = run.get("ticket")
    if not isinstance(ticket, dict) or not isinstance(ticket.get("repo_ref"), str):
        raise ValueError("Stored bundle does not identify its reproduced GitHub source.")
    expected_ref = f"{request.repository.repository}@{request.base_branch}"
    if request.repository.ref != request.base_branch:
        raise ValueError("repository.ref and base_branch must be the same immutable validation branch.")
    if ticket["repo_ref"] != expected_ref:
        raise ValueError("Fix preparation must target the same GitHub repository and branch that was reproduced.")


def _prepared_plan_path(runs_root: Path, plan_id: str) -> Path:
    try:
        canonical_id = str(UUID(plan_id))
    except ValueError as error:
        raise ValueError("Prepared pull-request plan ID is invalid.") from error
    root = (runs_root.parent / "prepared-prs").resolve()
    target = (root / f"{canonical_id}.json").resolve()
    try:
        target.relative_to(root)
    except ValueError as error:
        raise ValueError("Prepared pull-request plan path escapes its configured root.") from error
    return target


def _failed_fix_status_path(runs_root: Path, run_id: str) -> Path:
    try:
        canonical_run_id = str(UUID(run_id))
    except ValueError as error:
        raise ValueError("Fix status run ID is invalid.") from error
    root = (runs_root.parent / "fix-failures").resolve()
    target = (root / f"{canonical_run_id}.json").resolve()
    try:
        target.relative_to(root)
    except ValueError as error:
        raise ValueError("Fix status path escapes its configured root.") from error
    return target


def _write_failed_fix_status(runs_root: Path, status: dict[str, object]) -> Path:
    run_id = status.get("run_id")
    if not isinstance(run_id, str) or status.get("status") != "failed":
        raise ValueError("Only a failed fix-job response can be persisted.")
    destination = _failed_fix_status_path(runs_root, run_id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(destination)
    return destination


def _failed_fix_status_for_run(runs_root: Path, run_id: str) -> dict[str, object] | None:
    try:
        destination = _failed_fix_status_path(runs_root, run_id)
        canonical_run_id = str(UUID(run_id))
    except ValueError:
        return None
    try:
        payload = json.loads(destination.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("run_id") != canonical_run_id or payload.get("status") != "failed":
        return None
    return payload


def _read_pull_request_plan(runs_root: Path, plan_id: str) -> PullRequestPlan:
    path = _prepared_plan_path(runs_root, plan_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise FileNotFoundError("Prepared pull-request plan not found.") from error
    if not isinstance(payload, dict):
        raise FixValidationError("Prepared pull-request plan is invalid.")
    return PullRequestPlan.from_json(payload)


def _publication_record_path(runs_root: Path, plan_id: str) -> Path:
    try:
        canonical_id = str(UUID(plan_id))
    except ValueError as error:
        raise ValueError("Prepared pull-request plan ID is invalid.") from error
    root = (runs_root.parent / "published-prs").resolve()
    target = (root / f"{canonical_id}.json").resolve()
    try:
        target.relative_to(root)
    except ValueError as error:
        raise ValueError("Published pull-request record path escapes its configured root.") from error
    return target


def _publication_for_plan(runs_root: Path, plan_id: str) -> dict[str, object] | None:
    try:
        path = _publication_record_path(runs_root, plan_id)
    except ValueError:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _publication_record(plan: PullRequestPlan, published: PublishedPullRequest) -> dict[str, object]:
    return {
        "plan_id": str(plan.plan_id),
        "run_id": str(plan.run_id),
        "repository": published.repository,
        "published_at": datetime.now(timezone.utc).isoformat(),
        "pull_request": {
            "number": published.number,
            "url": published.url,
            "branch": published.branch,
            "commit": published.commit,
            "draft": True,
        },
        "jira_comment": {"status": "pending"},
    }


def _write_publication_record(runs_root: Path, plan: PullRequestPlan, publication: dict[str, object]) -> Path:
    destination = _publication_record_path(runs_root, str(plan.plan_id))
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps(publication, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(destination)
    return destination


def _is_configured_jira_ticket(config: JiraConfig | None, ticket_id: str) -> bool:
    project_key, separator, sequence = ticket_id.partition("-")
    return bool(config is not None and separator and sequence and project_key in config.project_sources)


def _publication_capability(config: GitHubConfig | None, repository: str) -> dict[str, object]:
    if config is None:
        return {"available": False, "reason": "GitHub publishing is not configured for this service."}
    try:
        config.require_publish_access(repository)
    except GitHubCheckoutError as error:
        return {"available": False, "reason": str(error)}
    return {"available": True}


def _yolo_capability(config: APIConfig) -> dict[str, object]:
    if config.jira is None:
        return {"available": False, "reason": "Configure Jira intake before enabling YOLO mode."}
    if config.github is None:
        return {"available": False, "reason": "Configure a GitHub write token and draft-PR publishing before enabling YOLO mode."}
    github_sources = [source for source in config.jira.project_sources.values() if isinstance(source, GitHubProjectSource)]
    if not github_sources:
        return {"available": False, "reason": "Map at least one Jira project to an allow-listed GitHub repository before enabling YOLO mode."}
    for source in github_sources:
        try:
            config.github.require_publish_access(source.source.repository)
        except GitHubCheckoutError as error:
            return {"available": False, "reason": str(error)}
    return {"available": True}


def _prepared_plan_for_run(runs_root: Path, run_id: str) -> dict[str, object] | None:
    try:
        canonical_run_id = str(UUID(run_id))
    except ValueError:
        return None
    root = (runs_root.parent / "prepared-prs").resolve()
    if not root.is_dir():
        return None
    for path in sorted(root.glob("*.json"), reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("run_id") == canonical_run_id:
            return payload
    return None


def _run_summary_with_fix_status(summary: dict[str, object], runs_root: Path) -> dict[str, object]:
    enriched = dict(summary)
    run_id = summary.get("run_id")
    if isinstance(run_id, str):
        enriched["fix"] = _fix_status_for_run(runs_root, run_id)
    return enriched


def _fix_status_for_run(runs_root: Path, run_id: str) -> dict[str, object] | None:
    """Return the highest persisted post-reproduction state without overstating deployment."""
    plan = _prepared_plan_for_run(runs_root, run_id)
    if plan is not None:
        plan_id = plan.get("plan_id")
        if isinstance(plan_id, str) and (publication := _publication_for_plan(runs_root, plan_id)) is not None:
            pull_request = publication.get("pull_request")
            response: dict[str, object] = {
                "status": "DRAFT_PR_OPEN",
                "label": "DRAFT PR OPEN",
                "plan_id": plan_id,
                "published": True,
                "jira_comment": publication.get("jira_comment"),
            }
            if isinstance(pull_request, dict):
                response["pull_request"] = pull_request
            return response
        return {
            "status": "FIX_VALIDATED",
            "label": "FIX VALIDATED",
            "plan_id": str(plan_id) if isinstance(plan_id, str) else None,
            "published": False,
        }
    if failed := _failed_fix_status_for_run(runs_root, run_id):
        return {
            "status": "FIX_NEEDS_REVIEW",
            "label": "FIX NEEDS REVIEW",
            "published": False,
            "error": failed.get("error"),
        }
    return None


def _sse_events(registry: JobRegistry, job_id: str, after_sequence: int) -> Iterator[str]:
    cursor = after_sequence
    while True:
        snapshot, events = registry.wait_for_events(job_id, cursor, timeout_seconds=15)
        if snapshot is None:
            return
        for event in events:
            cursor = event.sequence
            payload = json.dumps(event.as_json(), separators=(",", ":"))
            yield f"id: {event.sequence}\nevent: progress\ndata: {payload}\n\n"
        if snapshot.status in {JobState.DONE, JobState.FAILED}:
            return
        if not events:
            yield ": keepalive\n\n"


def _event_cursor(request: Request, fallback: int) -> int:
    raw_value = request.headers.get("last-event-id")
    if raw_value is None:
        return fallback
    try:
        return max(int(raw_value), fallback)
    except ValueError:
        return fallback


def _clear_stored_runs(root: Path) -> int:
    """Delete only canonical UUID bundle directories directly within the configured root."""
    resolved_root = root.resolve()
    if not resolved_root.is_dir():
        return 0

    deleted = 0
    for directory in resolved_root.iterdir():
        if directory.is_symlink() or not directory.is_dir() or not _is_canonical_run_directory(directory.name):
            continue
        if not (directory / "manifest.json").is_file():
            continue
        if directory.resolve().parent != resolved_root:
            continue
        shutil.rmtree(directory)
        deleted += 1
    return deleted


def _is_canonical_run_directory(name: str) -> bool:
    try:
        return str(UUID(name)) == name
    except ValueError:
        return False


def _positive_integer(source: Mapping[str, str], name: str, default: int) -> int:
    raw = source.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as error:
        raise APIConfigurationError(f"{name} must be a positive integer.") from error
    if value < 1:
        raise APIConfigurationError(f"{name} must be a positive integer.")
    return value


def _run_docker_check(command: list[str], message: str) -> None:
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise APIConfigurationError(f"{message}: {error}") from error
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        suffix = f" ({detail})" if detail else ""
        raise APIConfigurationError(f"{message}.{suffix}")




if __name__ == "__main__":
    main()
