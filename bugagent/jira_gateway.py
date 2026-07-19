"""Narrow public ingress for signed Jira webhooks.

The investigation API is intentionally bound to loopback and currently has no
general API authentication.  A public tunnel therefore targets this gateway,
not the API itself.  The gateway accepts one POST path, forwards only the
payload and Jira signature to the loopback API, and returns 404 everywhere
else.  Signature validation remains in :mod:`bugagent.jira_api` so there is a
single source of truth for webhook authentication.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import os
from typing import Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from fastapi import FastAPI, HTTPException, Request as FastAPIRequest, Response, status


WEBHOOK_PATH = "/integrations/jira/webhook"
DEFAULT_UPSTREAM_URL = f"http://127.0.0.1:8001{WEBHOOK_PATH}"
DEFAULT_PORT = 8002
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


class GatewayConfigurationError(RuntimeError):
    """The gateway's loopback-only upstream configuration is invalid."""


@dataclass(frozen=True, slots=True)
class GatewayResponse:
    """The small response surface returned by the loopback API."""

    status_code: int
    body: bytes
    content_type: str


Forwarder = Callable[[str, bytes, str | None, str | None], GatewayResponse]


def create_gateway(
    upstream_url: str = DEFAULT_UPSTREAM_URL,
    *,
    forwarder: Forwarder | None = None,
) -> FastAPI:
    """Create a public-safe gateway that exposes only the signed Jira route."""
    validated_upstream = validate_upstream_url(upstream_url)
    selected_forwarder = forwarder or forward_webhook
    app = FastAPI(
        title="DevSleuthAgent Jira webhook gateway",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.post(WEBHOOK_PATH)
    async def relay_webhook(request: FastAPIRequest) -> Response:
        try:
            result = await asyncio.to_thread(
                selected_forwarder,
                validated_upstream,
                await request.body(),
                request.headers.get("x-hub-signature"),
                request.headers.get("content-type"),
            )
        except URLError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"DevSleuthAgent webhook API is unavailable: {error.reason}",
            ) from error
        return Response(content=result.body, status_code=result.status_code, media_type=result.content_type)

    return app


def forward_webhook(
    upstream_url: str,
    payload: bytes,
    signature: str | None,
    content_type: str | None,
) -> GatewayResponse:
    """Forward only the headers required by the authenticated Jira endpoint."""
    headers = {"content-type": content_type or "application/json"}
    if signature:
        headers["x-hub-signature"] = signature
    request = Request(upstream_url, data=payload, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=15) as response:
            return GatewayResponse(
                status_code=response.status,
                body=response.read(),
                content_type=response.headers.get_content_type(),
            )
    except HTTPError as error:
        return GatewayResponse(
            status_code=error.code,
            body=error.read(),
            content_type=error.headers.get_content_type() if error.headers else "application/json",
        )


def validate_upstream_url(value: str) -> str:
    """Permit only the exact loopback Jira endpoint; never create an open proxy."""
    parsed = urlparse(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in _LOOPBACK_HOSTS
        or parsed.path != WEBHOOK_PATH
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise GatewayConfigurationError(
            f"BUGAGENT_JIRA_WEBHOOK_UPSTREAM must be a loopback HTTP URL ending in {WEBHOOK_PATH}."
        )
    return value


def gateway_port(environment: Mapping[str, str] | None = None) -> int:
    """Load the private gateway port without adding configuration to the engine."""
    source = os.environ if environment is None else environment
    raw = source.get("BUGAGENT_JIRA_WEBHOOK_GATEWAY_PORT", str(DEFAULT_PORT)).strip()
    try:
        port = int(raw)
    except ValueError as error:
        raise GatewayConfigurationError("BUGAGENT_JIRA_WEBHOOK_GATEWAY_PORT must be an integer.") from error
    if not 1 <= port <= 65535:
        raise GatewayConfigurationError("BUGAGENT_JIRA_WEBHOOK_GATEWAY_PORT must be between 1 and 65535.")
    return port


def main() -> None:
    """Run the webhook-only loopback listener for a public HTTPS tunnel."""
    import uvicorn

    upstream = os.environ.get("BUGAGENT_JIRA_WEBHOOK_UPSTREAM", DEFAULT_UPSTREAM_URL)
    uvicorn.run(create_gateway(upstream), host="127.0.0.1", port=gateway_port())


if __name__ == "__main__":
    main()
