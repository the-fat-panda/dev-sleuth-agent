"""Make one live ResponsesInvestigationClient request and print its raw API response.

This is intentionally a probe, not an investigation command. It uses the real
client request path and never writes to a repository or starts a sandbox.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any
from urllib.request import Request, urlopen

from bugagent.agent import ResponsesInvestigationClient
from bugagent.agent.client import DEFAULT_MODEL
from bugagent.agent.repository import RepositoryContext
from bugagent.domain import Ticket


class _CapturedResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def close(self) -> None:
        return None


def _capture_raw_response(request: Request, timeout: float) -> _CapturedResponse:
    response = urlopen(request, timeout=timeout)
    try:
        body = response.read()
    finally:
        response.close()
    print("RAW_RESPONSE_BEGIN")
    print(body.decode("utf-8", errors="replace"))
    print("RAW_RESPONSE_END")
    return _CapturedResponse(body)


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe BugAgent's live Responses API client.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Model ID to probe")
    args = parser.parse_args()

    ticket = Ticket(
        id="LIVE-PROBE-1",
        title="A public operation fails for a newly created object",
        body="Create one safe pytest regression test from the supplied public API context.",
        repo_ref="live-probe@local",
    )
    context = RepositoryContext(
        root="live-probe",
        files=("library.py",),
        manifest=(),
        snippets=(("library.py", "class Account:\n    def close(self) -> None:\n        pass\n"),),
    )
    client = ResponsesInvestigationClient(
        os.environ.get("OPENAI_API_KEY", ""),
        model=args.model,
        request_sender=_capture_raw_response,
    )
    candidate = client.propose(ticket, context, ())
    print("PARSED_CANDIDATE_BEGIN")
    print(
        json.dumps(
            {
                "path": candidate.path,
                "content": candidate.content,
                "hypothesis": candidate.hypothesis,
                "expected_symptom": candidate.expected_symptom,
                "public_api_claims": candidate.public_api_claims,
            },
            indent=2,
        )
    )
    print("PARSED_CANDIDATE_END")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"LIVE_MODEL_CHECK_FAILED: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1)
