"""Immutable, hash-addressed run bundles for independent review and replay."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
import hashlib
import json
from pathlib import Path
import shutil
from typing import Any
from uuid import UUID, uuid4

from .domain import RunBundle


def _primitive(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, UUID)):
        return value.isoformat()
    if is_dataclass(value):
        return _primitive(asdict(value))
    if isinstance(value, dict):
        return {str(key): _primitive(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_primitive(item) for item in value]
    return value


class ArtifactStore:
    """Writes a bundle once, then atomically publishes it by run ID."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def write(self, bundle: RunBundle) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / str(bundle.run_id)
        if target.exists():
            raise FileExistsError(f"Run bundle already exists: {target}")

        staging = self.root / f".{bundle.run_id}.{uuid4().hex}.tmp"
        staging.mkdir()
        try:
            self._write_json(staging / "ticket.json", bundle.ticket)
            self._write_json(staging / "candidates.json", bundle.candidates)
            self._write_json(staging / "evidence.json", bundle.evidence)
            self._write_json(staging / "verdict.json", bundle.verdict)
            self._write_ndjson(staging / "timeline.ndjson", bundle.events)

            hashes = self._hash_artifacts(staging)
            manifest = {
                "schema_version": 1,
                "run_id": str(bundle.run_id),
                "created_at": bundle.created_at.isoformat(),
                "repo_commit": bundle.repo_commit,
                "prompt_version": bundle.prompt_version,
                "artifacts": hashes,
            }
            self._write_json(staging / "manifest.json", manifest)
            staging.replace(target)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return target

    @staticmethod
    def _write_json(path: Path, value: Any) -> None:
        path.write_text(
            json.dumps(_primitive(value), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _write_ndjson(path: Path, events: tuple[Any, ...]) -> None:
        with path.open("w", encoding="utf-8", newline="\n") as stream:
            for event in events:
                stream.write(json.dumps(_primitive(event), sort_keys=True) + "\n")

    @staticmethod
    def _hash_artifacts(directory: Path) -> dict[str, str]:
        hashes: dict[str, str] = {}
        for path in sorted(directory.iterdir()):
            if path.is_file():
                hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        return hashes
