"""Dependency-free local evidence dashboard for judge and developer review."""

from __future__ import annotations

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlparse


class RunStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def list_runs(self) -> list[dict[str, Any]]:
        if not self.root.is_dir():
            return []
        runs: list[dict[str, Any]] = []
        for manifest_path in sorted(self.root.glob("*/manifest.json"), reverse=True):
            try:
                manifest = _read_json(manifest_path)
                verdict = _read_json(manifest_path.parent / "verdict.json")
            except (OSError, json.JSONDecodeError):
                continue
            runs.append(
                {
                    "run_id": manifest["run_id"],
                    "created_at": manifest["created_at"],
                    "repo_commit": manifest["repo_commit"],
                    "status": verdict["status"],
                    "score": verdict["evidence_score"],
                }
            )
        return runs

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        if not _safe_run_id(run_id):
            return None
        directory = self.root / run_id
        try:
            directory.resolve().relative_to(self.root)
        except ValueError:
            return None
        required = {
            "manifest": "manifest.json",
            "ticket": "ticket.json",
            "candidates": "candidates.json",
            "evidence": "evidence.json",
            "verdict": "verdict.json",
        }
        try:
            payload = {key: _read_json(directory / filename) for key, filename in required.items()}
            payload["events"] = _read_ndjson(directory / "timeline.ndjson")
        except (OSError, json.JSONDecodeError):
            return None
        return payload


def make_handler(store: RunStore, static_root: Path) -> type[BaseHTTPRequestHandler]:
    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/api/runs":
                self._json({"runs": store.list_runs()})
                return
            if parsed.path.startswith("/api/runs/"):
                run_id = parsed.path.removeprefix("/api/runs/")
                run = store.get_run(run_id)
                if run is None:
                    self.send_error(HTTPStatus.NOT_FOUND, "Run not found")
                    return
                self._json(run)
                return
            self._static(parsed.path)

        def _json(self, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _static(self, request_path: str) -> None:
            relative = "index.html" if request_path in {"", "/"} else unquote(request_path).lstrip("/")
            pure_path = PurePosixPath(relative)
            if pure_path.is_absolute() or ".." in pure_path.parts:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            target = (static_root / Path(*pure_path.parts)).resolve()
            try:
                target.relative_to(static_root.resolve())
            except ValueError:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if not target.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            content_type = {".html": "text/html", ".css": "text/css", ".js": "text/javascript"}.get(
                target.suffix, "application/octet-stream"
            )
            content = target.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, format: str, *args: Any) -> None:
            return None

    return DashboardHandler


def serve(*, runs_root: Path, host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    static_root = Path(__file__).resolve().parents[1] / "web"
    return ThreadingHTTPServer((host, port), make_handler(RunStore(runs_root), static_root))


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the BugAgent evidence dashboard")
    parser.add_argument("--runs-root", type=Path, default=Path(".bugagent") / "checkpoint-3")
    parser.add_argument("--port", type=int, default=8000)
    arguments = parser.parse_args()
    server = serve(runs_root=arguments.runs_root, port=arguments.port)
    print(f"BugAgent dashboard: http://127.0.0.1:{arguments.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _safe_run_id(value: str) -> bool:
    return bool(value) and all(character.isalnum() or character == "-" for character in value)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_ndjson(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


if __name__ == "__main__":
    main()
