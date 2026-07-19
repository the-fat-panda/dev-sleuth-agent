"""FastAPI service layer for asynchronous investigations."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Callable, Iterator, Mapping
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from bugagent.agent import InvestigationOrchestrator, ResponsesInvestigationClient
from bugagent.agent.client import DEFAULT_MODEL
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
from bugagent.domain import RunBundle, Ticket
from bugagent.github import GitHubCheckoutError, GitHubConfig, validate_git_available
from bugagent.jira import JiraConfig
from bugagent.jira_api import attach_jira_routes
from bugagent.sandbox import DockerSandbox, SandboxPolicy
from bugagent.web import RunStore


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


InvestigationRunner = Callable[[Ticket, Path, str], RunBundle]
InvestigationRunnerFactory = Callable[["InvestigationProgressReporter"], InvestigationRunner]
RuntimeValidator = Callable[[APIConfig], None]
JobCompletionCallback = Callable[[str, RunBundle], None]


def create_app(
    config: APIConfig | None = None,
    *,
    startup_validator: RuntimeValidator = validate_runtime,
    investigation_runner: InvestigationRunner | None = None,
) -> FastAPI:
    """Create an API app without importing or changing the investigation core."""
    selected_config = config or APIConfig.from_environment()
    registry = JobRegistry()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bugagent-investigation")
    runner_factory: InvestigationRunnerFactory
    if investigation_runner is None:
        runner_factory = lambda progress: _live_runner(selected_config, progress)
    else:
        runner_factory = lambda progress: investigation_runner

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        startup_validator(selected_config)
        try:
            yield
        finally:
            executor.shutdown(wait=False, cancel_futures=False)

    app = FastAPI(
        title="DevSleuthAgent Investigation API",
        version="0.1.0",
        description="Asynchronous ticket-to-evidence investigations over local source checkouts.",
        lifespan=lifespan,
    )
    app.state.config = selected_config
    app.state.jobs = registry

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

    @app.get("/runs")
    def list_runs() -> dict[str, object]:
        return {"runs": primitive(RunStore(selected_config.runs_root).list_runs())}

    @app.get("/runs/{run_id}")
    def get_run(run_id: str) -> object:
        run = RunStore(selected_config.runs_root).get_run(run_id)
        if run is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.")
        return primitive(run)

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
    registry.mark_done(job_id, bundle)
    if on_completed is not None:
        on_completed(job_id, bundle)


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
