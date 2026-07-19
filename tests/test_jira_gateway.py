from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from bugagent.jira_gateway import (
    GatewayConfigurationError,
    GatewayResponse,
    WEBHOOK_PATH,
    create_gateway,
    gateway_port,
    validate_upstream_url,
)


class JiraGatewayTests(unittest.TestCase):
    def test_relays_only_the_jira_webhook_path_and_required_headers(self) -> None:
        calls: list[tuple[str, bytes, str | None, str | None]] = []

        def forwarder(url: str, body: bytes, signature: str | None, content_type: str | None) -> GatewayResponse:
            calls.append((url, body, signature, content_type))
            return GatewayResponse(202, b'{"job_id":"job-1"}', "application/json")

        app = create_gateway(forwarder=forwarder)
        with TestClient(app) as client:
            response = client.post(
                WEBHOOK_PATH,
                content=b'{"webhookEvent":"jira:issue_created"}',
                headers={"X-Hub-Signature": "sha256=proof", "Content-Type": "application/json"},
            )
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.json(), {"job_id": "job-1"})
            self.assertEqual(client.get("/").status_code, 404)
            self.assertEqual(client.post("/investigations", json={}).status_code, 404)

        self.assertEqual(
            calls,
            [
                (
                    "http://127.0.0.1:8001/integrations/jira/webhook",
                    b'{"webhookEvent":"jira:issue_created"}',
                    "sha256=proof",
                    "application/json",
                )
            ],
        )

    def test_rejects_non_loopback_or_non_webhook_upstreams(self) -> None:
        for candidate in (
            "https://example.com/integrations/jira/webhook",
            "http://127.0.0.1:8001/investigations",
            "http://127.0.0.1:8001/integrations/jira/webhook?redirect=yes",
        ):
            with self.subTest(candidate=candidate):
                with self.assertRaises(GatewayConfigurationError):
                    validate_upstream_url(candidate)

    def test_gateway_port_defaults_and_rejects_invalid_values(self) -> None:
        self.assertEqual(gateway_port({}), 8002)
        with self.assertRaises(GatewayConfigurationError):
            gateway_port({"BUGAGENT_JIRA_WEBHOOK_GATEWAY_PORT": "0"})

