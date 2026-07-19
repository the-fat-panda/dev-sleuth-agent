"""Investigation clients: deterministic for tests and optional OpenAI Responses API."""

from __future__ import annotations

from dataclasses import asdict
import json
import os
from typing import Any, Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from bugagent.domain import CandidateTest, MinorValue, SilentOutputProof, TextValue, Ticket

from .repository import RepositoryContext

DEFAULT_MODEL = "gpt-5.6-terra"


class InvestigationClientError(RuntimeError):
    pass


class InvestigationClient(Protocol):
    def propose(
        self,
        ticket: Ticket,
        repository: RepositoryContext,
        prior_feedback: tuple[str, ...],
    ) -> CandidateTest: ...


class ScriptedInvestigationClient:
    """Deterministic client used by checkpoint fixtures and integration tests."""

    def __init__(self, candidates: tuple[CandidateTest, ...]) -> None:
        self._candidates = candidates
        self.calls: list[tuple[Ticket, RepositoryContext, tuple[str, ...]]] = []

    def propose(
        self,
        ticket: Ticket,
        repository: RepositoryContext,
        prior_feedback: tuple[str, ...],
    ) -> CandidateTest:
        self.calls.append((ticket, repository, prior_feedback))
        index = len(self.calls) - 1
        if index >= len(self._candidates):
            raise InvestigationClientError("No scripted candidate remains for this attempt.")
        return self._candidates[index]


class ResponsesInvestigationClient:
    """Minimal strict-schema Responses API client without a runtime SDK dependency."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        request_sender: Callable[[Request, float], Any] | None = None,
    ) -> None:
        if not api_key.strip():
            raise InvestigationClientError("OPENAI_API_KEY is required for live investigation.")
        self._api_key = api_key
        self.model = model
        self._request_sender = request_sender or _send_request

    @classmethod
    def from_environment(cls, *, model: str = DEFAULT_MODEL) -> "ResponsesInvestigationClient":
        return cls(os.environ.get("OPENAI_API_KEY", ""), model=model)

    def propose(
        self,
        ticket: Ticket,
        repository: RepositoryContext,
        prior_feedback: tuple[str, ...],
    ) -> CandidateTest:
        payload = {
            "model": self.model,
            "store": False,
            "input": [
                {"role": "system", "content": _SYSTEM_INSTRUCTIONS},
                {
                    "role": "user",
                    "content": _user_prompt(ticket, repository, prior_feedback),
                },
            ],
            "text": {"format": _candidate_schema()},
        }
        request = Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            response = self._request_sender(request, 90)
            raw = response.read().decode("utf-8")
            response.close()
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:500]
            raise InvestigationClientError(f"OpenAI request failed with HTTP {error.code}: {detail}") from error
        except URLError as error:
            raise InvestigationClientError(f"OpenAI request could not be completed: {error.reason}") from error

        try:
            candidate_data = json.loads(_extract_output_text(json.loads(raw)))
            return CandidateTest(
                path=candidate_data["path"],
                content=candidate_data["content"],
                hypothesis=candidate_data["hypothesis"],
                expected_symptom=candidate_data["expected_symptom"],
                public_api_claims=tuple(candidate_data["public_api_claims"]),
                silent_output=_silent_output_from_data(candidate_data["silent_output"]),
            )
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise InvestigationClientError("OpenAI response did not contain a valid candidate-test object.") from error


def _send_request(request: Request, timeout: float) -> Any:
    return urlopen(request, timeout=timeout)


def _extract_output_text(response: dict[str, Any]) -> str:
    if isinstance(response.get("output_text"), str):
        return response["output_text"]
    for item in response.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                return content["text"]
    raise InvestigationClientError("OpenAI response had no output text.")


def _candidate_schema() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "name": "bugagent_candidate_test",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "hypothesis": {"type": "string"},
                "expected_symptom": {"type": "string"},
                "public_api_claims": {"type": "array", "items": {"type": "string"}},
                "silent_output": {
                    "type": ["object", "null"],
                    "additionalProperties": False,
                    "properties": {
                        "policy_id": {"type": "string"},
                        "contract_path": {"type": "string"},
                        "contract_anchor": {"type": "string"},
                        "input_values": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {"name": {"type": "string"}, "value": {"type": "string"}},
                                "required": ["name", "value"],
                            },
                        },
                        "expected_values": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {"name": {"type": "string"}, "minor": {"type": "integer"}},
                                "required": ["name", "minor"],
                            },
                        },
                        "observed_fields": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": [
                        "policy_id",
                        "contract_path",
                        "contract_anchor",
                        "input_values",
                        "expected_values",
                        "observed_fields",
                    ],
                },
            },
            "required": ["path", "content", "hypothesis", "expected_symptom", "public_api_claims", "silent_output"],
        },
    }


_SYSTEM_INSTRUCTIONS = """You investigate a Python bug report and propose exactly one pytest regression test.
Use only public repository APIs when possible. Do not modify application code. Do not use subprocesses,
networking, filesystem access, or environment variables in the test. Put the test directly under
tests/bugagent_generated/. State the expected product failure, not a setup error. If prior feedback
shows a setup failure, refine the test; do not claim reproduction without sandbox evidence.

For a crash or exception bug, set silent_output to null. For a silent wrong-value bug, set silent_output
to a grounded proof only when repository source explicitly states the expected business rule. Use
policy_id "tax_after_discounts_v1" only when the repository's mercato/pricing/tax.py contains the exact
post-discount tax convention. Cite that exact sentence as contract_anchor. Supply integer minor-unit
inputs subtotal_minor, discount_minor, shipping_minor, decimal tax_rate, and currency; expected_values
must contain tax_minor and total_minor. Your test must configure the applicable public TaxPolicy rate,
assign quote from a public .quote() call, then print exactly one marker before assertions:
print("BUGAGENT_OBSERVATION " + json.dumps({"tax_minor": quote.tax.minor, "total_minor": quote.total.minor}, sort_keys=True))
It must assert quote.tax.minor and quote.total.minor against the grounded integer expected values. Do not
use a silent_output proof if you cannot meet every one of these requirements; return null instead."""


def _user_prompt(ticket: Ticket, repository: RepositoryContext, prior_feedback: tuple[str, ...]) -> str:
    feedback = "\n".join(f"- {item}" for item in prior_feedback) or "(none; this is the first attempt)"
    ticket_json = json.dumps(asdict(ticket), indent=2, sort_keys=True)
    return f"Ticket:\n{ticket_json}\n\n{repository.as_prompt_text()}\n\nPrior sandbox feedback:\n{feedback}"


def _silent_output_from_data(value: object) -> SilentOutputProof | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError("silent_output must be an object or null")
    input_values = tuple(
        TextValue(name=item["name"], value=item["value"]) for item in value["input_values"]
    )
    expected_values = tuple(
        MinorValue(name=item["name"], minor=item["minor"]) for item in value["expected_values"]
    )
    return SilentOutputProof(
        policy_id=value["policy_id"],
        contract_path=value["contract_path"],
        contract_anchor=value["contract_anchor"],
        input_values=input_values,
        expected_values=expected_values,
        observed_fields=tuple(value["observed_fields"]),
    )
