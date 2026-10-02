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
import contextlib
import hashlib
import io
import json
import os
import subprocess
import tarfile
import time
import tokenize
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

WINDOW_LINES = 6
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
DEFINITIONS = (*FUNCTIONS, ast.ClassDef)

# The corpus fingerprint cache is written outside every repository, so the scan
# of an unchanged file reads its windows instead of re-parsing it. A cache entry
# is keyed by the file's path and content digest, so a file whose bytes change
# misses and is parsed again; an entry a newer shape of this module would not
# write is discarded rather than read as current. Each entry records when it was
# last used. Because every content a file ever had keeps its entry, and several
# worktrees at different revisions share one cache root, the cache is pruned to
# the least recently used once it exceeds a cap derived from the number of
# corpus files it knows about (see ``_cap_for``).
#
# The cap counts a cached path only while a recent scan read it: an entry also
# records the scan ordinal at which its path was last part of a scan's corpus,
# and a path no scan has read for ``_STALE_PATH_SCANS`` scans stops counting, so
# the least-recently-used prune evicts it. Without that expiry a path removed or
# renamed out of the corpus keeps its own entry in the count, pinning the cap
# open so neither it nor a superseded revision of a live path is ever evicted,
# and corpus.json grows with every path ever scanned rather than with the
# current corpus.
_CLONE_CACHE_VERSION = 2
_CORPUS_CACHES: dict[Path, dict[str, Any]] = {}

# How many scans a cached path may go unread before it stops counting toward the
# cap and becomes eligible for eviction. The span is in scans rather than in
# seconds so it is exercised without sleeping; it is large enough that the
# handful of scans between two full scans of one corpus — in which a partial
# scan reads only a subset — never ages a still-present path out, and small
# enough that a path removed or renamed out of the corpus is reclaimed within a
# few scans.
_STALE_PATH_SCANS = 5


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


class _WindowProvider(Protocol):
    """A provider of six-line token windows for one parsed function.

    ``_Function`` (parsed this scan) and ``_CachedFunction`` (rebuilt from the
    cache) both satisfy this shape, and the corpus index and the head list hold
    either kind interchangeably. Naming the shape once lets a type checker see
    that an interface mismatch between the two providers is a defect rather than
    letting ``Any`` hide it.
    """

    path: str
    name: str
    line: int

    def ref(self) -> dict[str, Any]: ...

    def windows(self) -> list[tuple[str, int]]: ...


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


@dataclass(frozen=True)
class _CachedFunction:
    """A corpus function rebuilt from its cached windows, without its tokens.

    An unchanged file is never re-parsed: its windows come from the digest-keyed
    cache and are exactly what ``_Function.windows`` would have produced. Only
    the windows and the function's reference are held, because a corpus function
    is compared by fingerprint and named in the report, never token-matched
    against a base (only a changed file is, and a changed file is parsed again).
    """

    path: str
    name: str
    line: int
    cached_windows: tuple[tuple[str, int], ...]

    def ref(self) -> dict[str, Any]:
        return {"path": self.path, "line": self.line, "name": self.name}

    def windows(self) -> list[tuple[str, int]]:
        return list(self.cached_windows)


def _cache_key(path: str, source: str) -> str:
    """The digest a cached file's windows are stored under.

    The path is folded in with the content so two files carrying identical bytes
    at different paths cannot share an entry, which would report one path's
    function as the other's.
    """
    return _digest(path + "\0" + source)


def _function_record(function: _Function) -> dict[str, Any]:
    return {
        "path": function.path,
        "name": function.name,
        "line": function.line,
        "windows": [[fingerprint, line] for fingerprint, line in function.windows()],
    }


def _function_from_record(record: Mapping[str, Any]) -> _CachedFunction:
    return _CachedFunction(
        path=str(record["path"]),
        name=str(record["name"]),
        line=int(record["line"]),
        cached_windows=tuple(
            (str(fingerprint), int(line)) for fingerprint, line in record["windows"]
        ),
    )


def _entry_path(entry: Any) -> str | None:
    """The corpus path a cache entry describes, if it records one.

    An entry written by this module carries its path directly; older entries
    carry only their functions, so the first function's path stands in. A file
    defining nothing records no path and is counted only through the files the
    scan itself reads.
    """
    if isinstance(entry, Mapping):
        path = entry.get("path")
        if isinstance(path, str):
            return path
        functions = entry.get("functions")
        if isinstance(functions, list) and functions:
            first = functions[0]
            if isinstance(first, Mapping) and isinstance(first.get("path"), str):
                return first["path"]
    return None


def _seen_at(entry: Any) -> int:
    """The scan ordinal at which ``entry``'s path was last part of a scan.

    Zero when the entry records none, which sorts it oldest. Entries written
    before this module counted scans read as never seen and so age out on the
    first prune.
    """
    if isinstance(entry, Mapping):
        seen = entry.get("seen")
        if isinstance(seen, int):
            return seen
    return 0


def _current_scan(cache: Mapping[str, Any]) -> int:
    """The ordinal of the scan about to run.

    One greater than the highest ordinal any entry records, so it survives the
    in-memory cache being cleared and the store re-read: the counter is carried
    by the entries themselves rather than by a separate field the document
    would have to version. A path read this scan therefore records a strictly
    newer ordinal than any path not read, and successive scans advance the
    ordinal even when every file is a cache hit.
    """
    return max((_seen_at(entry) for entry in cache.values()), default=0) + 1


def _fresh_cached_paths(cache: Mapping[str, Any], scan: int) -> set[str]:
    """Paths the cache holds that a scan read within the staleness span.

    A path counts toward the cap only while a recent scan read it. Once no scan
    has read it for ``_STALE_PATH_SCANS`` scans it stops counting, so the
    least-recently-used prune evicts its entries and a path removed or renamed
    out of the corpus does not pin the cap open for ever.
    """
    fresh: set[str] = set()
    for entry in cache.values():
        path = _entry_path(entry)
        if path is not None and scan - _seen_at(entry) < _STALE_PATH_SCANS:
            fresh.add(path)
    return fresh


def _cap_for(known_file_count: int) -> int:
    """The most cache entries a scan keeps.

    The cap is one entry per corpus file the scan knows about, because one
    revision's worth of fingerprints is all a scan can use: an entry is keyed by
    path and content digest, so two revisions of the same file are two entries,
    and every entry beyond the known file count is a revision the current scan
    did not read. The scan's own paths and the cached paths a recent scan read
    are counted together, so a scan that reads only part of the corpus does not
    shrink the cap to that part and evict the warm entries the next full scan
    would reuse. Deriving the cap from the file count rather than a fixed literal
    keeps it correct as the corpus grows, and holding at most one revision's
    worth bounds corpus.json no matter how many revisions or worktrees share the
    cache root. The floor of one keeps an empty corpus from disarming the prune.
    """
    return max(1, known_file_count)


def _used_at(entry: Any) -> float:
    """The time ``entry`` was last used, or a value that sorts it first."""
    if isinstance(entry, Mapping):
        used = entry.get("used")
        if isinstance(used, (int, float)):
            return float(used)
    return float("-inf")


def _evict_to_cap(cache: dict[str, Any], cap: int) -> bool:
    """Drop the least recently used entries until ``cache`` holds ``cap``.

    Returns whether anything was dropped, so the caller knows the cache it
    holds differs from the one on disk.
    """
    if len(cache) <= cap:
        return False
    ordered = sorted(cache.items(), key=lambda item: _used_at(item[1]), reverse=True)
    for key, _entry in ordered[cap:]:
        del cache[key]
    return True


def _cached_functions(
    source: str, path: str, cache: dict[str, Any], now: float, scan: int
) -> list[_WindowProvider]:
    """Return ``source``'s functions, reusing the cache for unchanged bytes."""
    key = _cache_key(path, source)
    record = cache.get(key)
    if isinstance(record, Mapping):
        functions = record.get("functions")
        if isinstance(functions, list):
            try:
                rebuilt = [_function_from_record(item) for item in functions]
            except (KeyError, TypeError, ValueError):
                rebuilt = None
            if rebuilt is not None:
                cache[key] = {
                    "used": now,
                    "path": path,
                    "seen": scan,
                    "functions": functions,
                }
                return rebuilt
    functions = functions_in(source, path)
    cache[key] = {
        "used": now,
        "path": path,
        "seen": scan,
        "functions": [_function_record(function) for function in functions],
    }
    return functions


def _cache_root(cache_root: str | Path | None = None) -> Path:
    """The directory the corpus fingerprint cache lives under.

    Outside every repository, so a cache write never dirties a checkout. A
    caller that isolated its configuration through ``RECKON_HOME`` also isolated
    its cache. ``RECKON_CLONE_CACHE`` names the location outright when set.
    """
    if cache_root is not None:
        return Path(cache_root)
    configured = os.environ.get("RECKON_CLONE_CACHE")
    if configured:
        return Path(configured).expanduser()
    cache_home = os.environ.get("XDG_CACHE_HOME")
    if cache_home:
        return Path(cache_home) / "reckon" / "clones"
    reckon_home = os.environ.get("RECKON_HOME")
    if reckon_home:
        return Path(reckon_home) / "cache" / "clones"
    return Path.home() / ".cache" / "reckon" / "clones"


def _load_corpus_cache(root: Path) -> dict[str, Any]:
    try:
        entry = json.loads((root / "corpus.json").read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(entry, dict) or entry.get("version") != _CLONE_CACHE_VERSION:
        return {}
    files = entry.get("files")
    if not isinstance(files, dict):
        return {}
    return {
        key: value
        for key, value in files.items()
        if isinstance(value, dict) and isinstance(value.get("functions"), list)
    }


def _store_corpus_cache(root: Path, files: dict[str, Any]) -> None:
    from reckon._store import write_json_atomically

    write_json_atomically(
        root / "corpus.json",
        {"version": _CLONE_CACHE_VERSION, "files": files},
        fsync=False,
        indent=None,
    )


def _shared_cache(root: Path) -> dict[str, Any]:
    cache = _CORPUS_CACHES.get(root)
    if cache is None:
        cache = _load_corpus_cache(root)
        _CORPUS_CACHES[root] = cache
    return cache


def _index(
    functions: Iterable[_WindowProvider],
) -> dict[str, list[_WindowProvider]]:
    """Map each window fingerprint to the functions that carry it."""
    index: dict[str, list[_WindowProvider]] = collections.defaultdict(list)
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
    cache_root: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Report changed functions whose windows duplicate another function.

    ``head_sources`` and ``base_sources`` map repository-relative paths to
    source text. A function is changed when its path is in ``changed_paths`` and
    is either absent from the base or has different owned tokens there; a path
    the base does not carry (an added file) marks all its functions changed.
    Only functions under ``corpus_prefixes`` form the corpus a changed function
    can match against, and a changed function never matches itself.

    A changed file is parsed from the head bytes it is given; every other file
    under ``corpus_prefixes`` is read from a cache keyed by its path and content
    digest, so an unchanged file is never re-parsed. A file outside the corpus
    that did not change contributes nothing and is not read at all.

    Each cache entry records when it was last used, and the cache is pruned to
    the least recently used once it holds more than one entry per known corpus
    file — the paths this scan read and the cached paths a recent scan read — so
    the entry a scan just read survives, a partial scan does not shrink the cap
    to the subset it read, and an entry for a superseded revision is dropped. A
    cached path no scan has read for ``_STALE_PATH_SCANS`` scans stops counting
    toward the cap, so an entry for a path removed or renamed out of the corpus
    is evicted rather than pinning the cap open for ever. That bound is what
    keeps ``corpus.json`` from growing with every revision or departed path that
    shares the cache root.
    """
    prefixes = tuple(corpus_prefixes)
    base_sources = dict(base_sources or {})
    changed = frozenset(changed_paths)
    root = _cache_root(cache_root)
    cache = _shared_cache(root)
    now = time.time()
    scan = _current_scan(cache)
    corpus_paths = [
        path
        for path in head_sources
        if path.endswith(".py") and path.startswith(prefixes)
    ]
    head_functions: list[_WindowProvider] = []
    dirty = False
    for path, source in head_sources.items():
        if not path.endswith(".py"):
            continue
        if path not in changed and not path.startswith(prefixes):
            continue
        try:
            if path in changed:
                parsed = functions_in(source, path)
                head_functions.extend(parsed)
                if path.startswith(prefixes):
                    cache[_cache_key(path, source)] = {
                        "used": now,
                        "path": path,
                        "seen": scan,
                        "functions": [
                            _function_record(function) for function in parsed
                        ],
                    }
                    dirty = True
            else:
                head_functions.extend(
                    _cached_functions(source, path, cache, now, scan)
                )
                dirty = True
        except (SyntaxError, ValueError):
            continue
    known_paths = set(corpus_paths) | _fresh_cached_paths(cache, scan)
    evicted = _evict_to_cap(cache, _cap_for(len(known_paths)))
    if dirty or evicted:
        with contextlib.suppress(OSError):
            # The cache is a speed-up, not a dependency: a directory that
            # cannot be written leaves the scan reading every file, which still
            # reports the same matches.
            _store_corpus_cache(root, cache)
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
