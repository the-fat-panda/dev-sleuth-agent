"""Read-only repository context and strict generated-test validation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from bugagent.domain import CandidateTest, Ticket


class CandidateValidationError(ValueError):
    pass


_EXCLUDED_DIRECTORIES = {".git", ".bugagent", "__pycache__", ".venv", "venv", "node_modules"}
_SENSITIVE_PARTS = {".env", "credentials", "secrets", "secret", "private", "keys"}
_BANNED_TEST_PATTERNS = (
    r"\bsubprocess\b",
    r"\bos\.system\b",
    r"\bsocket\b",
    r"\brequests\b",
    r"\burllib\b",
    r"\bopen\s*\(",
)


@dataclass(frozen=True, slots=True)
class RepositoryContext:
    root: str
    files: tuple[str, ...]
    manifest: tuple[str, ...]
    snippets: tuple[tuple[str, str], ...]

    def as_prompt_text(self) -> str:
        manifest = "\n".join(self.manifest) or "(no standard Python manifest found)"
        files = "\n".join(self.files) or "(no Python files found)"
        snippets = "\n\n".join(f"### {path}\n{content}" for path, content in self.snippets)
        return f"Repository manifests:\n{manifest}\n\nPython files:\n{files}\n\nSafe source snippets:\n{snippets}"


class ReadOnlyRepository:
    """Provides bounded, secret-aware context without exposing arbitrary shell access."""

    def __init__(self, root: Path, *, max_file_bytes: int = 20_000, max_context_bytes: int = 50_000) -> None:
        self.root = root.resolve()
        self.max_file_bytes = max_file_bytes
        self.max_context_bytes = max_context_bytes
        if not self.root.is_dir():
            raise FileNotFoundError(f"Repository root does not exist: {self.root}")

    def list_python_files(self, *, limit: int = 80) -> tuple[str, ...]:
        paths: list[str] = []
        for path in sorted(self.root.rglob("*.py")):
            if self._is_excluded(path):
                continue
            paths.append(path.relative_to(self.root).as_posix())
            if len(paths) >= limit:
                break
        return tuple(paths)

    def read(self, relative_path: str) -> str:
        path = self._safe_path(relative_path)
        if path.stat().st_size > self.max_file_bytes:
            raise ValueError(f"Refusing to read oversized source file: {relative_path}")
        return path.read_text(encoding="utf-8", errors="replace")

    def search(self, term: str, *, limit: int = 20) -> tuple[str, ...]:
        if not term.strip() or len(term) > 120:
            raise ValueError("Search term must be between 1 and 120 characters.")
        matches: list[str] = []
        for relative_path in self.list_python_files():
            content = self.read(relative_path)
            for line_number, line in enumerate(content.splitlines(), start=1):
                if term.lower() in line.lower():
                    matches.append(f"{relative_path}:{line_number}:{line.strip()[:200]}")
                    if len(matches) >= limit:
                        return tuple(matches)
        return tuple(matches)

    def build_context(self, ticket: Ticket, *, max_snippets: int = 8) -> RepositoryContext:
        files = self.list_python_files()
        preferred_terms = _ticket_terms(ticket)
        ranked = sorted(
            files,
            key=lambda path: (
                -sum(term in path.lower() for term in preferred_terms),
                path.startswith("tests/"),
                path,
            ),
        )
        snippets: list[tuple[str, str]] = []
        used_bytes = 0
        for relative_path in ranked:
            if relative_path.startswith("tests/bugagent_generated/"):
                continue
            content = self.read(relative_path)
            encoded_size = len(content.encode("utf-8"))
            if used_bytes + encoded_size > self.max_context_bytes:
                continue
            snippets.append((relative_path, content))
            used_bytes += encoded_size
            if len(snippets) >= max_snippets:
                break

        manifests: list[str] = []
        for name in ("pyproject.toml", "pytest.ini", "requirements.txt"):
            path = self.root / name
            if path.is_file() and path.stat().st_size <= self.max_file_bytes:
                manifests.append(f"### {name}\n{path.read_text(encoding='utf-8', errors='replace')}")
        return RepositoryContext(str(self.root), files, tuple(manifests), tuple(snippets))

    def _safe_path(self, relative_path: str) -> Path:
        candidate = (self.root / relative_path).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as error:
            raise ValueError("Repository path escapes the selected worktree.") from error
        if not candidate.is_file() or self._is_excluded(candidate):
            raise ValueError(f"Repository file is not readable: {relative_path}")
        return candidate

    def _is_excluded(self, path: Path) -> bool:
        relative_parts = set(path.resolve().relative_to(self.root).parts)
        lowered = {part.lower() for part in relative_parts}
        return bool(lowered & _EXCLUDED_DIRECTORIES or lowered & _SENSITIVE_PARTS)


def validate_candidate(candidate: CandidateTest) -> CandidateTest:
    path = Path(candidate.path)
    if path.is_absolute() or path.parent != Path("tests") / "bugagent_generated":
        raise CandidateValidationError("Candidate path must be directly under tests/bugagent_generated.")
    if not path.name.startswith("test_") or path.suffix != ".py":
        raise CandidateValidationError("Candidate must be a Python pytest file named test_*.py.")
    if not candidate.content.strip() or len(candidate.content.encode("utf-8")) > 16_000:
        raise CandidateValidationError("Candidate test content must be between 1 and 16,000 bytes.")
    if not candidate.hypothesis.strip() or not candidate.expected_symptom.strip():
        raise CandidateValidationError("Candidate must state a hypothesis and expected symptom.")
    for pattern in _BANNED_TEST_PATTERNS:
        if re.search(pattern, candidate.content, flags=re.IGNORECASE):
            raise CandidateValidationError(f"Candidate contains a prohibited test operation: {pattern}")
    return candidate


def _ticket_terms(ticket: Ticket) -> tuple[str, ...]:
    tokens = re.findall(r"[a-zA-Z_]{3,}", f"{ticket.title} {ticket.body}".lower())
    return tuple(dict.fromkeys(tokens))
