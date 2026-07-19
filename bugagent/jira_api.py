"""FastAPI routes that adapt authenticated Jira events to investigation jobs."""

from __future__ import annotations

from typing import Callable

from fastapi import FastAPI, HTTPException, Request, status

from bugagent.domain import RunBundle, Ticket
from bugagent.jira import (
    JiraCloudClient,
    JiraConfig,
    JiraIssue,
    ProjectSource,
    JiraWebhookError,
    JiraWebhookRouter,
    format_investigation_comment,
)

SubmitInvestigation = Callable[
    [Ticket, ProjectSource, Callable[[str, RunBundle], None] | None, JiraIssue],
    str,
]
GetJobResponse = Callable[[str], dict[str, object] | None]
EmitProgress = Callable[..., None]


def attach_jira_routes(
    app: FastAPI,
    config: JiraConfig | None,
    *,
    submit: SubmitInvestigation,
    get_job: GetJobResponse,
    emit_progress: EmitProgress,
) -> None:
    """Add an optional, signed Jira Cloud boundary without coupling it to the engine."""
    router = JiraWebhookRouter(config) if config else None
    client = JiraCloudClient(config) if config else None
    app.state.jira_configured = config is not None

    @app.get("/integrations/jira/status")
    def jira_status() -> dict[str, object]:
        if config is None:
            return {"configured": False}
        return {
            "configured": True,
            "base_url": config.base_url,
            "project_keys": sorted(config.project_sources),
            "webhook_url": "/integrations/jira/webhook",
        }

    @app.post("/integrations/jira/webhook", status_code=status.HTTP_202_ACCEPTED)
    async def receive_jira_webhook(request: Request) -> dict[str, object]:
        if router is None or client is None:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Jira integration is not configured.")
        try:
            issue, source = router.issue_created(request.headers.get("x-hub-signature"), await request.body())
        except JiraWebhookError as error:
            code = status.HTTP_401_UNAUTHORIZED if "signature" in str(error).lower() else status.HTTP_422_UNPROCESSABLE_CONTENT
            raise HTTPException(status_code=code, detail=str(error)) from error
        job_id, duplicate = router.submit_once(
            issue,
            source,
            lambda ticket, mapped_source: submit(
                ticket,
                mapped_source,
                _jira_completion_publisher(client, issue.key, emit_progress),
                issue,
            ),
        )
        payload = get_job(job_id)
        if payload is None:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Jira job was not registered.")
        payload.update(
            {
                "source": "jira",
                "issue_key": issue.key,
                "duplicate_delivery": duplicate,
            }
        )
        return payload


def _jira_completion_publisher(
    client: JiraCloudClient,
    issue_key: str,
    emit_progress: EmitProgress,
) -> Callable[[str, RunBundle], None]:
    def publish(job_id: str, bundle: RunBundle) -> None:
        emit_progress(job_id, "jira_comment", "started", "Posting evidence to Jira")
        try:
            client.post_comment(issue_key, format_investigation_comment(bundle))
        except Exception as error:
            emit_progress(
                job_id,
                "jira_comment",
                "failed",
                "Jira evidence comment could not be posted",
                detail={"error": f"{type(error).__name__}: {error}"},
            )
            return
        emit_progress(job_id, "jira_comment", "completed", "Evidence comment posted to Jira", detail={"issue_key": issue_key})

    return publish
