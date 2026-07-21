"""Read-only repository context and strict generated-test validation."""

from __future__ import annotations

import ast
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
_TICKET_STOP_WORDS = {
    "about", "after", "again", "also", "and", "are", "but", "can", "could", "did", "does", "for", "from",
    "had", "has", "have", "into", "its", "just", "more", "not", "now", "one", "our", "out", "should", "that",
    "the", "their", "them", "then", "there", "they", "this", "too", "was", "were", "when", "with", "would", "you",
    "your", "customer", "says", "still",
}
# Contract-backed silent-output policies are intentionally narrow.  Their
# source contracts must be visible to the test writer whenever they exist,
# otherwise a correct public-API test has no basis for emitting the required
# independently-verifiable probe protocol.
_CONTRACT_SOURCE_PATHS = ("mercato/pricing/tax.py", "mercato/pricing/engine.py")


@dataclass(frozen=True, slots=True)
class ApiSurface:
    """One statically verified public API surface from repository source.

    This object is intentionally produced from ``ast`` rather than importing
    application modules.  Repository imports can have side effects and must
    never run merely to prepare a model prompt.
    """

    qualified_name: str
    path: str
    line: int
    signatures: tuple[str, ...]
    docstring: str | None = None

    def as_prompt_text(self) -> str:
        lines = [f"- {self.qualified_name} ({self.path}:{self.line})"]
        lines.extend(f"  {signature}" for signature in self.signatures)
        if self.docstring:
            lines.append(f"  Docstring: {self.docstring}")
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class UsageExample:
    """A bounded existing test function that demonstrates a real call path."""

    path: str
    line: int
    name: str
    content: str

    def as_prompt_text(self) -> str:
        return f"### {self.path}:{self.line} ({self.name})\n{self.content}"


@dataclass(frozen=True, slots=True)
class RepositoryContext:
    root: str
    files: tuple[str, ...]
    manifest: tuple[str, ...]
    snippets: tuple[tuple[str, str], ...]
    api_surface: tuple[ApiSurface, ...] = ()
    usage_examples: tuple[UsageExample, ...] = ()

    def as_prompt_text(self) -> str:
        manifest = "\n".join(self.manifest) or "(no standard Python manifest found)"
        files = "\n".join(self.files) or "(no Python files found)"
        snippets = "\n\n".join(f"### {path}\n{content}" for path, content in self.snippets)
        api_surface = "\n\n".join(item.as_prompt_text() for item in self.api_surface) or "(no public Python API could be indexed)"
        usage_examples = "\n\n".join(item.as_prompt_text() for item in self.usage_examples) or "(no matching existing test usage found)"
        return (
            f"Repository manifests:\n{manifest}\n\n"
            f"Python files:\n{files}\n\n"
            f"Verified API surface (static source signatures; treat as ground truth):\n{api_surface}\n\n"
            f"Existing verified test usage (prefer these call patterns):\n{usage_examples}\n\n"
            f"Safe source snippets:\n{snippets}"
        )


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

    def build_context(
        self,
        ticket: Ticket,
        *,
        max_snippets: int = 8,
        max_api_bytes: int = 16_000,
        max_usage_bytes: int = 16_000,
    ) -> RepositoryContext:
        """Build bounded source context plus a deterministic API usage guide.

        The old context only ranked whole files by ticket words.  That left a
        test-writing model to infer constructor and keyword arguments from a
        partial file dump.  The guide below gives it exact static signatures
        and real existing test calls without executing any application code.
        """
        files = self.list_python_files()
        preferred_terms = _ticket_terms(ticket)
        api_surface = self._build_api_surface(files, preferred_terms, max_bytes=max_api_bytes)
        usage_examples = self._build_usage_examples(files, preferred_terms, api_surface, max_bytes=max_usage_bytes)
        source_rank: dict[str, int] = {}
        for index, item in enumerate(api_surface):
            source_rank.setdefault(item.path, index)
        contract_rank = {path: index for index, path in enumerate(_CONTRACT_SOURCE_PATHS) if path in files}
        ranked = sorted(
            files,
            key=lambda path: (
                0 if path in contract_rank else 1,
                contract_rank.get(path, len(contract_rank)),
                source_rank.get(path, len(api_surface)),
                -_term_score(path, preferred_terms),
                path.startswith("tests/"),
                path,
            ),
        )
        manifests: list[str] = []
        for name in ("pyproject.toml", "pytest.ini", "requirements.txt"):
            path = self.root / name
            if path.is_file() and path.stat().st_size <= self.max_file_bytes:
                manifests.append(f"### {name}\n{path.read_text(encoding='utf-8', errors='replace')}")

        # Keep the pre-existing total context cap meaningful after adding the
        # API guide.  A small reserve covers section headers and the file list.
        guide_bytes = sum(len(item.as_prompt_text().encode("utf-8")) for item in api_surface)
        guide_bytes += sum(len(item.as_prompt_text().encode("utf-8")) for item in usage_examples)
        guide_bytes += sum(len(item.encode("utf-8")) for item in manifests)
        guide_bytes += len("\n".join(files).encode("utf-8")) + 1_024
        snippet_budget = max(0, self.max_context_bytes - guide_bytes)

        snippets: list[tuple[str, str]] = []
        used_bytes = 0
        for relative_path in ranked:
            if relative_path.startswith("tests/bugagent_generated/"):
                continue
            try:
                content = self.read(relative_path)
            except ValueError:
                # The file list remains useful for orientation, but an
                # oversized file never consumes the bounded prompt budget.
                continue
            encoded_size = len(content.encode("utf-8"))
            if used_bytes + encoded_size > snippet_budget:
                continue
            snippets.append((relative_path, content))
            used_bytes += encoded_size
            if len(snippets) >= max_snippets:
                break

        return RepositoryContext(
            str(self.root),
            files,
            tuple(manifests),
            tuple(snippets),
            api_surface=api_surface,
            usage_examples=usage_examples,
        )

    def _build_api_surface(
        self,
        files: tuple[str, ...],
        terms: tuple[str, ...],
        *,
        max_bytes: int,
    ) -> tuple[ApiSurface, ...]:
        candidates: list[tuple[int, ApiSurface]] = []
        for relative_path in files:
            if relative_path.startswith("tests/"):
                continue
            try:
                content = self.read(relative_path)
            except ValueError:
                continue
            try:
                tree = ast.parse(content, filename=relative_path)
            except SyntaxError:
                continue
            module = _module_name(relative_path)
            for node in tree.body:
                item = _api_surface_from_node(node, module, relative_path)
                if item is None:
                    continue
                source = ast.get_source_segment(content, node) or ""
                score = _term_score("\n".join((item.qualified_name, source, item.docstring or "")), terms)
                candidates.append((score, item))

        ranked = sorted(candidates, key=lambda value: (-value[0], value[1].path, value[1].line, value[1].qualified_name))
        selected: list[ApiSurface] = []
        used_bytes = 0
        for _, item in ranked:
            rendered_size = len(item.as_prompt_text().encode("utf-8"))
            if rendered_size > max_bytes or used_bytes + rendered_size > max_bytes:
                continue
            selected.append(item)
            used_bytes += rendered_size
        return tuple(selected)

    def _build_usage_examples(
        self,
        files: tuple[str, ...],
        terms: tuple[str, ...],
        api_surface: tuple[ApiSurface, ...],
        *,
        max_bytes: int,
        max_examples: int = 8,
    ) -> tuple[UsageExample, ...]:
        candidates: list[tuple[int, UsageExample]] = []
        verified_type_names = {item.qualified_name.rsplit(".", maxsplit=1)[-1] for item in api_surface}
        for relative_path in files:
            if not relative_path.startswith("tests/") or relative_path.startswith("tests/bugagent_generated/"):
                continue
            try:
                content = self.read(relative_path)
            except ValueError:
                continue
            try:
                tree = ast.parse(content, filename=relative_path)
            except SyntaxError:
                continue
            imported_names = _imported_names(tree)
            api_import_score = len(imported_names & verified_type_names)
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not node.name.startswith("test_"):
                    continue
                example_content = ast.get_source_segment(content, node)
                if not example_content:
                    continue
                # Test names are concise intent labels.  Give an exact name
                # match priority over generic words repeated in broad end to
                # end order tests, then use the body as a tie breaker.
                name_score = _term_score(node.name, terms)
                path_score = _term_score(relative_path, terms)
                source_score = _term_score(example_content, terms)
                # A test without a ticket-term match can still be the only
                # usage demonstration in a small repository, but rank it last.
                calls = sum(isinstance(child, ast.Call) for child in ast.walk(node))
                score = api_import_score * 100_000 + name_score * 10_000 + path_score * 1_000 + source_score * 10 + min(calls, 20)
                candidates.append((score, UsageExample(relative_path, node.lineno, node.name, example_content)))

        ranked = sorted(candidates, key=lambda value: (-value[0], value[1].path, value[1].line))
        selected: list[UsageExample] = []
        used_bytes = 0
        for _, item in ranked:
            rendered_size = len(item.as_prompt_text().encode("utf-8"))
            if rendered_size > max_bytes or used_bytes + rendered_size > max_bytes:
                continue
            selected.append(item)
            used_bytes += rendered_size
            if len(selected) >= max_examples:
                break
        return tuple(selected)

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
    return tuple(dict.fromkeys(token for token in tokens if token not in _TICKET_STOP_WORDS))


def _api_surface_from_node(node: ast.stmt, module: str, path: str) -> ApiSurface | None:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
        return ApiSurface(
            qualified_name=f"{module}.{node.name}",
            path=path,
            line=node.lineno,
            signatures=(f"{node.name}{_signature(node.args)}",),
            docstring=_short_docstring(ast.get_docstring(node)),
        )
    if not isinstance(node, ast.ClassDef) or node.name.startswith("_"):
        return None

    signatures: list[str] = []
    for member in node.body:
        if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if member.name == "__init__":
            signatures.append(f"constructor: {node.name}{_signature(member.args, drop_first=True)}")
        elif not member.name.startswith("_"):
            signatures.append(f"{node.name}.{member.name}{_signature(member.args)}")
    if not signatures:
        return None
    return ApiSurface(
        qualified_name=f"{module}.{node.name}",
        path=path,
        line=node.lineno,
        signatures=tuple(signatures),
        docstring=_short_docstring(ast.get_docstring(node)),
    )


def _signature(arguments: ast.arguments, *, drop_first: bool = False) -> str:
    positional = list(arguments.posonlyargs) + list(arguments.args)
    defaults_at = len(positional) - len(arguments.defaults)
    values: list[str] = []
    for index, argument in enumerate(positional):
        if drop_first and index == 0:
            continue
        text = _argument_text(argument)
        if index >= defaults_at:
            text += f" = {_expression_text(arguments.defaults[index - defaults_at])}"
        values.append(text)
        if arguments.posonlyargs and index + 1 == len(arguments.posonlyargs):
            values.append("/")
    if arguments.vararg:
        values.append(f"*{_argument_text(arguments.vararg)}")
    elif arguments.kwonlyargs:
        values.append("*")
    for argument, default in zip(arguments.kwonlyargs, arguments.kw_defaults, strict=True):
        text = _argument_text(argument)
        if default is not None:
            text += f" = {_expression_text(default)}"
        values.append(text)
    if arguments.kwarg:
        values.append(f"**{_argument_text(arguments.kwarg)}")
    return f"({', '.join(values)})"


def _argument_text(argument: ast.arg) -> str:
    if argument.annotation is None:
        return argument.arg
    return f"{argument.arg}: {_expression_text(argument.annotation)}"


def _expression_text(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except (AttributeError, ValueError):  # pragma: no cover - defensive for malformed source ASTs
        return "..."


def _module_name(relative_path: str) -> str:
    path = Path(relative_path).with_suffix("")
    parts = list(path.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) or "module"


def _short_docstring(value: str | None, *, limit: int = 600) -> str | None:
    if not value:
        return None
    normalized = " ".join(value.split())
    return normalized[:limit] + ("..." if len(normalized) > limit else "")


def _term_score(value: str, terms: tuple[str, ...]) -> int:
    tokens = re.findall(r"[a-zA-Z0-9]+", value.replace("_", " ").lower())
    return sum(tokens.count(term) for term in terms)


def _imported_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    if not isinstance(tree, ast.Module):
        return names
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update((alias.asname or alias.name.split(".", maxsplit=1)[0]) for alias in node.names)
    return names
