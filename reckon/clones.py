"""Report functions that duplicate another function under normalised tokens.

The instrument is the six-line normalised clone detector from the crew pattern
review (``docs/research/data/crew-pattern-review/code-depth/census.py``), ported
here so a package caller can run it without importing from ``docs/``. The study
script stays as the review's artifact.

The detector hashes every window of six consecutive token-bearing code lines,
ignoring comments, docstrings and layout while keeping the spelling of
identifiers and operators and collapsing string and number literals to their
token kinds. Two windows that hash alike are a copy. Promotion runs the detector
over the functions a run added or modified, against the whole tree at the
promoted revision, and reports each pair; it warns and never refuses, because a
private copy is a review signal rather than a landed-code defect.
"""

from __future__ import annotations

import ast
import collections
import hashlib
import io
import subprocess
import tarfile
import tokenize
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

WINDOW_LINES = 6
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
DEFINITIONS = (*FUNCTIONS, ast.ClassDef)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _docstring_spans(tree: ast.AST) -> set[int]:
    """Line numbers covered by module/class/function docstrings."""
    spans: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, *DEFINITIONS)) and node.body:
            first = node.body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                spans.update(range(first.lineno, first.end_lineno + 1))
    return spans


def _token_lines(source: str, tree: ast.AST) -> list[tuple[int, str]]:
    """Keep names/operators; canonicalise literals and ignore prose/layout."""
    ignored = _docstring_spans(tree)
    lines: dict[int, list[str]] = collections.defaultdict(list)
    skip = {
        tokenize.ENCODING,
        tokenize.ENDMARKER,
        tokenize.COMMENT,
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.NEWLINE,
        tokenize.NL,
    }
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type in skip or tok.start[0] in ignored:
            continue
        value = (
            tokenize.tok_name[tok.type]
            if tok.type in (tokenize.STRING, tokenize.NUMBER)
            else tok.string
        )
        lines[tok.start[0]].append(value)
    return [(line, " ".join(tokens)) for line, tokens in sorted(lines.items())]


def _nested_definition_lines(node: ast.AST) -> set[int]:
    """Lines belonging to a definition nested inside ``node``.

    A nested function's tokens are its own, not the enclosing function's, so the
    enclosing function's normalised body must not absorb them.
    """
    nested: set[int] = set()
    for child in ast.iter_child_nodes(node):
        for descendant in ast.walk(child):
            if isinstance(descendant, (ast.Module, *DEFINITIONS)):
                nested.update(range(descendant.lineno, descendant.end_lineno + 1))
    return nested


@dataclass(frozen=True)
class _Function:
    """One implementation and the token windows it owns."""

    path: str
    name: str
    line: int
    end_line: int
    tokens: tuple[tuple[int, str], ...]

    def ref(self) -> dict[str, Any]:
        return {"path": self.path, "line": self.line, "name": self.name}

    def windows(self) -> list[tuple[str, int]]:
        """Return ``(fingerprint, first_line)`` for each six-line window."""
        result: list[tuple[str, int]] = []
        for index in range(len(self.tokens) - WINDOW_LINES + 1):
            window = self.tokens[index : index + WINDOW_LINES]
            text = "\n".join(body for _line, body in window)
            result.append((_digest(text), window[0][0]))
        return result


def functions_in(source: str, path: str) -> list[_Function]:
    """Parse ``source`` and return every function definition it holds."""
    tree = ast.parse(source, filename=path)
    token_lines = _token_lines(source, tree)
    result: list[_Function] = []
    for node in ast.walk(tree):
        if not isinstance(node, FUNCTIONS) or node.end_lineno is None:
            continue
        nested = _nested_definition_lines(node)
        owned = tuple(
            (line, text)
            for line, text in token_lines
            if node.lineno <= line <= node.end_lineno and line not in nested
        )
        name = getattr(node, "name", "")
        result.append(
            _Function(
                path=path,
                name=name,
                line=node.lineno,
                end_line=node.end_lineno,
                tokens=owned,
            )
        )
    return result


def _index(functions: Iterable[_Function]) -> dict[str, list[_Function]]:
    """Map each window fingerprint to the functions that carry it."""
    index: dict[str, list[_Function]] = collections.defaultdict(list)
    for function in functions:
        for fingerprint, _line in function.windows():
            index[fingerprint].append(function)
    return index


def _changed_base_names(
    base_sources: Mapping[str, str], changed_paths: Iterable[str]
) -> dict[str, dict[str, tuple[tuple[int, str], ...]]]:
    """Index base implementations by path and qualified-ish name."""
    result: dict[str, dict[str, tuple[tuple[int, str], ...]]] = {}
    for path in changed_paths:
        source = base_sources.get(path)
        if source is None:
            continue
        try:
            functions = functions_in(source, path)
        except (SyntaxError, ValueError):
            continue
        result[path] = {function.name: function.tokens for function in functions}
    return result


def clone_matches(
    head_sources: Mapping[str, str],
    *,
    changed_paths: Iterable[str],
    base_sources: Mapping[str, str] | None = None,
    corpus_prefixes: Iterable[str] = ("reckon/", "tests/"),
) -> list[dict[str, Any]]:
    """Report changed functions whose windows duplicate another function.

    ``head_sources`` and ``base_sources`` map repository-relative paths to
    source text. A function is changed when its path is in ``changed_paths`` and
    is either absent from the base or has different owned tokens there; a path
    the base does not carry (an added file) marks all its functions changed.
    Only functions under ``corpus_prefixes`` form the corpus a changed function
    can match against, and a changed function never matches itself.
    """
    prefixes = tuple(corpus_prefixes)
    base_sources = dict(base_sources or {})
    head_functions: list[_Function] = []
    for path, source in head_sources.items():
        if not path.endswith(".py"):
            continue
        try:
            head_functions.extend(functions_in(source, path))
        except (SyntaxError, ValueError):
            continue
    corpus = [f for f in head_functions if f.path.startswith(prefixes)]
    index = _index(corpus)
    changed_names = _changed_base_names(base_sources, changed_paths)
    matches: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str, int]] = set()
    for function in head_functions:
        if function.path not in set(changed_paths):
            continue
        base_named = changed_names.get(function.path)
        if base_named is not None and base_named.get(function.name) == function.tokens:
            continue
        for fingerprint, window_line in function.windows():
            for other in index.get(fingerprint, ()):
                if other is function:
                    continue
                key = (
                    function.path,
                    function.line,
                    other.path,
                    other.line,
                )
                if key in seen:
                    continue
                seen.add(key)
                matches.append(
                    {
                        "run_function": function.ref(),
                        "existing_function": other.ref(),
                        "window_line": window_line,
                    }
                )
    matches.sort(
        key=lambda match: (
            match["run_function"]["path"],
            match["run_function"]["line"],
            match["existing_function"]["path"],
            match["existing_function"]["line"],
        )
    )
    return matches


def _git(tree: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(tree), *arguments],
        capture_output=True,
        check=False,
    )


def revised_python(tree: Path, revision: str) -> dict[str, str]:
    """Every tracked ``.py`` file at ``revision``, keyed by repository path.

    The tree is read from git's object store rather than the working tree, so
    the measurement is anchored to the revision the caller named and cannot see
    an edit made in the meantime.
    """
    archive = _git(tree, "archive", "--format=tar", revision)
    if archive.returncode:
        return {}
    sources: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        for member in tar.getmembers():
            if not member.isfile() or not member.name.endswith(".py"):
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            sources[member.name] = handle.read().decode("utf-8-sig", errors="replace")
    return sources


def changed_paths(tree: Path, base_sha: str, tip: str) -> list[str]:
    """The paths ``tip`` changes against ``base_sha``, or empty when unreadable."""
    if not base_sha or not tip:
        return []
    result = _git(tree, "diff", "--name-only", base_sha, tip)
    if result.returncode:
        return []
    return [line for line in result.stdout.decode().splitlines() if line.strip()]


def promotion_clone_matches(
    tree: Path,
    *,
    base_sha: str,
    tip: str,
) -> list[dict[str, Any]] | None:
    """Report clone matches for the functions the run added or modified.

    ``None`` means the measurement could not be produced — an unreadable base or
    tip, or an unreadable revision archive. A list, empty or not, is a reading.
    """
    if not base_sha or not tip:
        return None
    base = revised_python(tree, base_sha)
    head = revised_python(tree, tip)
    if not head or not base:
        return None
    changed = [
        path for path in changed_paths(tree, base_sha, tip) if path.endswith(".py")
    ]
    try:
        return clone_matches(
            head,
            changed_paths=changed,
            base_sources=base,
        )
    except (OSError, ValueError):
        return None
