from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bugagent.demo import build_demo_bundle
from bugagent.jira_api import attach_jira_routes
from bugagent.jira import (
    JiraConfig,
    JiraConfigurationError,
    JiraWebhookError,
    JiraWebhookRouter,
    format_investigation_comment,
    parse_issue_created,
    verify_webhook_signature,
)


class JiraTests(unittest.TestCase):
    def test_disabled_config_returns_none_and_partial_config_fails(self) -> None:
        self.assertIsNone(JiraConfig.from_environment({}))
        with self.assertRaisesRegex(JiraConfigurationError, "incomplete"):
            JiraConfig.from_environment({"BUGAGENT_JIRA_BASE_URL": "https://example.atlassian.net"})

    def test_signed_issue_created_webhook_is_parsed_and_de_duplicated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = _config(Path(directory))
            router = JiraWebhookRouter(config)
            payload = _webhook_payload()
            raw = json.dumps(payload).encode("utf-8")
            signature = _signature(config.webhook_secret, raw)

            issue, source = router.issue_created(signature, raw)
            self.assertEqual(issue.key, "SCRUM-7")
            self.assertEqual(issue.title, "Closing a new account fails")
            self.assertEqual(issue.body, "A customer says close fails on a fresh account.")
            self.assertEqual(source.repo_ref, "the-fat-panda/demo-fixture@main")

            calls: list[str] = []
            first, first_was_duplicate = router.submit_once(
                issue,
                source,
                lambda ticket, _: calls.append(ticket.id) or "job-1",
            )
            second, second_was_duplicate = router.submit_once(
                issue,
                source,
                lambda *_: "job-2",
            )

        self.assertEqual(first, "job-1")
        self.assertEqual(second, "job-1")
        self.assertFalse(first_was_duplicate)
        self.assertTrue(second_was_duplicate)
        self.assertEqual(calls, ["SCRUM-7"])

    def test_invalid_signature_and_unmapped_project_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            router = JiraWebhookRouter(_config(Path(directory)))
            raw = json.dumps(_webhook_payload()).encode("utf-8")
            with self.assertRaisesRegex(JiraWebhookError, "signature"):
                router.issue_created("sha256=wrong", raw)

            unknown = _webhook_payload()
            unknown["issue"]["fields"]["project"]["key"] = "OTHER"
            raw_unknown = json.dumps(unknown).encode("utf-8")
            with self.assertRaisesRegex(JiraWebhookError, "No repository source"):
                router.issue_created(_signature("test-secret", raw_unknown), raw_unknown)

    def test_comment_is_readable_for_a_reviewer(self) -> None:
        comment = format_investigation_comment(build_demo_bundle())
        self.assertIn("DevSleuthAgent investigation complete", comment)
        self.assertIn("Verdict: REPRODUCED", comment)
        self.assertIn("Candidate test:", comment)
        self.assertIn("Observed result:", comment)
        self.assertIn("What was tried:", comment)

    def test_parser_requires_created_event(self) -> None:
        payload = _webhook_payload()
        payload["webhookEvent"] = "jira:issue_updated"
        with self.assertRaisesRegex(JiraWebhookError, "jira:issue_created"):
            parse_issue_created(payload)

    def test_webhook_adapter_passes_jira_origin_to_the_service(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = _config(Path(directory))
            app = FastAPI()
            observed: dict[str, object] = {}
            attach_jira_routes(
                app,
                config,
                submit=lambda ticket, source, callback, issue: _capture_submission(
                    observed, ticket, source, callback, issue
                ),
                get_job=lambda job_id: {"job_id": job_id, "status": "queued"},
                emit_progress=lambda *_args, **_kwargs: None,
            )
            raw = json.dumps(_webhook_payload()).encode("utf-8")
            with TestClient(app) as client:
                response = client.post(
                    "/integrations/jira/webhook",
                    content=raw,
                    headers={"x-hub-signature": _signature(config.webhook_secret, raw)},
                )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["issue_key"], "SCRUM-7")
        self.assertEqual(observed["ticket_id"], "SCRUM-7")
        self.assertEqual(observed["issue_key"], "SCRUM-7")
        self.assertEqual(observed["issue_url"], "https://example.atlassian.net/rest/api/3/issue/10017")


def _config(repo: Path) -> JiraConfig:
    return JiraConfig.from_environment(
        {
            "BUGAGENT_JIRA_BASE_URL": "https://example.atlassian.net",
            "BUGAGENT_JIRA_EMAIL": "demo@example.com",
            "BUGAGENT_JIRA_API_TOKEN": "token",
            "BUGAGENT_JIRA_WEBHOOK_SECRET": "test-secret",
            "BUGAGENT_JIRA_PROJECT_SOURCES": json.dumps(
                {
                    "SCRUM": {
                        "repo_ref": "the-fat-panda/demo-fixture@main",
                        "path": str(repo),
                        "commit": "main",
                    }
                }
            ),
        }
    )


def _webhook_payload() -> dict[str, object]:
    return {
        "webhookEvent": "jira:issue_created",
        "issue": {
            "id": "10017",
            "key": "SCRUM-7",
            "self": "https://example.atlassian.net/rest/api/3/issue/10017",
            "fields": {
                "summary": "Closing a new account fails",
                "project": {"key": "SCRUM"},
                "description": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": "A customer says close fails on a fresh account."}],
                        }
                    ],
                },
            },
        },
    }


def _signature(secret: str, payload: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _capture_submission(observed, ticket, _source, _callback, issue):
    observed["ticket_id"] = ticket.id
    observed["issue_key"] = issue.key
    observed["issue_url"] = issue.source_url
    return "job-1"


if __name__ == "__main__":
    unittest.main()
