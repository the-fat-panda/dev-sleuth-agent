"""Independent verifier for immutable BugAgent evidence bundles."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Protocol

from bugagent.agent.orchestrator import candidate_worktree
from bugagent.domain import CandidateTest
from bugagent.sandbox import DockerSandbox, SandboxPolicy
from bugagent.sandbox.docker import SandboxRun


class BundleIntegrityError(ValueError):
    """An evidence bundle is incomplete or does not match its manifest."""


class ReplaySandbox(Protocol):
    def run(self, repo_root: Path, candidate_path: Path) -> SandboxRun: ...


@dataclass(frozen=True, slots=True)
class ReplayReport:
    run_id: str
    candidate_path: str
    expected_signature: str | None
    observed_signatures: tuple[str | None, ...]
    source_ref: str
    source_ref_matches: bool | None
    image: str
    passed: bool
    reasons: tuple[str, ...]

    def as_json(self) -> dict[str, Any]:
        return asdict(self)


def replay_bundle(
    bundle_directory: Path,
    repo_root: Path,
    *,
    image: str | None = None,
    sandbox: ReplaySandbox | None = None,
) -> ReplayReport:
    """Re-run the first generated test twice without modifying the supplied checkout."""
    bundle = load_verified_bundle(bundle_directory)
    candidates = bundle["candidates"]
    if not candidates:
        raise BundleIntegrityError("Evidence bundle contains no generated candidate test.")

    candidate = _candidate_from_json(candidates[0])
    expected_signature = _candidate_signature(bundle["evidence"])
    selected_image = image or _bundle_image(bundle["evidence"])
    if not selected_image:
        raise BundleIntegrityError("No immutable sandbox image was supplied or recorded in this bundle.")
    runner = sandbox or DockerSandbox(SandboxPolicy(image=selected_image, timeout_seconds=30))

    with candidate_worktree(repo_root, candidate) as worktree:
        first = runner.run(worktree, worktree / candidate.path)
        second = runner.run(worktree, worktree / candidate.path)

    observed = (first.normalized_signature(), second.normalized_signature())
    reasons: list[str] = []
    if not all(run.setup_valid and run.test_collected and run.test_failed and not run.timed_out for run in (first, second)):
        reasons.append("At least one replay did not produce a clean collected test failure.")
    if observed[0] != observed[1]:
        reasons.append("The two fresh replays produced different normalized signatures.")
    if expected_signature and any(signature != expected_signature for signature in observed):
        reasons.append("A replay signature did not match the signed candidate evidence.")

    expected_ref = str(bundle["manifest"]["repo_commit"])
    source_ref_matches = _source_ref_matches(repo_root, expected_ref)
    if source_ref_matches is False:
        reasons.append("The supplied repository HEAD does not match the bundle's recorded commit.")

    return ReplayReport(
        run_id=str(bundle["manifest"]["run_id"]),
        candidate_path=candidate.path,
        expected_signature=expected_signature,
        observed_signatures=observed,
        source_ref=expected_ref,
        source_ref_matches=source_ref_matches,
        image=selected_image,
        passed=not reasons,
        reasons=tuple(reasons),
    )


def load_verified_bundle(bundle_directory: Path) -> dict[str, Any]:
    """Load JSON artifacts only after their manifest hashes have been verified."""
    root = bundle_directory.resolve()
    if not root.is_dir():
        raise BundleIntegrityError(f"Evidence bundle directory does not exist: {root}")
    manifest = _read_json(root / "manifest.json")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise BundleIntegrityError("Manifest does not contain artifact hashes.")
    required = {"ticket.json", "candidates.json", "evidence.json", "verdict.json", "timeline.ndjson"}
    if set(artifacts) != required:
        raise BundleIntegrityError("Manifest artifact set is incomplete or unexpected.")
    for name, expected_hash in artifacts.items():
        path = root / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
            raise BundleIntegrityError(f"Artifact hash mismatch: {name}")
    return {
        "manifest": manifest,
        "ticket": _read_json(root / "ticket.json"),
        "candidates": _read_json(root / "candidates.json"),
        "evidence": _read_json(root / "evidence.json"),
        "verdict": _read_json(root / "verdict.json"),
    }


def _candidate_from_json(value: dict[str, Any]) -> CandidateTest:
    try:
        return CandidateTest(
            path=value["path"],
            content=value["content"],
            hypothesis=value["hypothesis"],
            expected_symptom=value["expected_symptom"],
            public_api_claims=tuple(value.get("public_api_claims", [])),
        )
    except (KeyError, TypeError) as error:
        raise BundleIntegrityError("Candidate artifact has an invalid schema.") from error


def _candidate_signature(evidence: list[dict[str, Any]]) -> str | None:
    for item in evidence:
        if item.get("phase") == "CANDIDATE":
            return item.get("normalized_signature")
    return None


def _bundle_image(evidence: list[dict[str, Any]]) -> str | None:
    for item in evidence:
        if item.get("phase") == "CANDIDATE":
            fingerprint = item.get("environment_fingerprint", {})
            return fingerprint.get("sandbox_image") or fingerprint.get("image")
    return None


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BundleIntegrityError(f"Invalid JSON artifact: {path.name}") from error


def _source_ref_matches(repo_root: Path, expected_ref: str) -> bool | None:
    """A fixture may not be a Git checkout; return None rather than fabricate a match."""
    if not re.fullmatch(r"[0-9a-fA-F]{7,64}", expected_ref):
        return None
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root.resolve()), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    head = completed.stdout.strip()
    return head == expected_ref or head.startswith(expected_ref)
