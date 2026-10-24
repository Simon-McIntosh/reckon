"""Tell whether the running server still runs the code on disk.

The served process imports reckon's Python once, at start, while the client's
JSX and CSS are compiled per request from the working tree. A checkout that
moves on while the process keeps running therefore serves current client code
against an older server, and nothing fails loudly: a route the client now
expects answers 404 and the client falls back to a slower path.

Most changes to the package never reach the server. The command line, crew
dispatch and the review machinery change many times an hour, and the served
process runs little of that code — measured on 1 October, one session ran 35 of
the package's 105 files, and a file it did run (crew/recovery.py) changed 19
times that day in functions the server never called. So a report compares
definitions, not files:

- every function and method the process has run since it started, recorded by
  :class:`ExecutedSource` on ``sys.monitoring``;
- every constant and class those functions read — by name, through the
  package's own imports, and transitively through the data definitions they
  read in turn;
- the module-level statements of a module first imported after start.

A definition counts as changed when its syntax tree changes; line shifts,
comments and docstrings leave it alone. A snapshot taken at start holds every
source file of the package (stat identity, hash and text), so the comparison
is always against the code the process loaded. The report is what the server
serves and what the client renders; the client never decides staleness itself.
"""

from __future__ import annotations

import ast
import contextlib
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from reckon._timestamps import parse_iso
from reckon.file_memo import file_signature

#: The command a reader is told to run; the service owns the restart.
RESTART_COMMAND = "reckon service restart"

#: How long one report is reused. Every open page polls, so without reuse each
#: poll would re-stat the whole package on a shared filesystem.
_REPORT_TTL_S = 30.0

#: The key under which a module's own top-level statements are compared, and
#: the key that compares a whole file.
_MODULE = "<module>"
_WHOLE_FILE = "*"

_Signature = tuple[int, int, int, int, int]


@dataclass(frozen=True)
class SourceSnapshot:
    """The package source as the process found it at start."""

    root: Path
    #: Package-relative posix path → (stat identity, sha256 of the bytes).
    files: dict[str, tuple[_Signature, str]] = field(default_factory=dict)
    taken_at: str = ""
    revision: str | None = None
    #: Package-relative posix path → the text the process started with.
    sources: dict[str, str] = field(default_factory=dict)


def package_root() -> Path:
    """Return the directory of the installed reckon package."""

    return Path(__file__).resolve().parent


def _source_files(root: Path) -> dict[str, Path]:
    return {
        path.relative_to(root).as_posix(): path
        for path in root.rglob("*.py")
        if "__pycache__" not in path.parts
    }


def _git(root: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = completed.stdout.strip()
    return output if completed.returncode == 0 and output else None


def take_snapshot(root: Path | None = None) -> SourceSnapshot:
    """Record every source file under ``root`` (default: this package)."""

    root = Path(root) if root is not None else package_root()
    files: dict[str, tuple[_Signature, str]] = {}
    sources: dict[str, str] = {}
    for relative, path in _source_files(root).items():
        try:
            signature = file_signature(path)
            data = path.read_bytes()
        except OSError:
            continue
        files[relative] = (signature, hashlib.sha256(data).hexdigest())
        sources[relative] = data.decode("utf-8", errors="replace")
    return SourceSnapshot(
        root=root,
        files=files,
        taken_at=datetime.now(UTC).isoformat(timespec="seconds"),
        revision=_git(root, "rev-parse", "HEAD"),
        sources=sources,
    )


_HASH_MEMO: dict[tuple[str, _Signature], str] = {}
_INDEX_MEMO: dict[tuple[str, str], _ModuleIndex | None] = {}
_DEFINITION_MEMO: dict[tuple[str, _Signature, tuple[str, ...]], dict[str, str]] = {}
_REPORTS: dict[int, tuple[float, dict]] = {}
_LOCK = threading.Lock()


def forget_reports() -> None:
    """Drop every reused report, hash and parse, as a fresh process holds none."""

    with _LOCK:
        _REPORTS.clear()
        _HASH_MEMO.clear()
        _INDEX_MEMO.clear()
        _DEFINITION_MEMO.clear()


def _current_hash(path: Path, signature: _Signature) -> str:
    key = (str(path), signature)
    with _LOCK:
        known = _HASH_MEMO.get(key)
    if known is not None:
        return known
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with _LOCK:
        _HASH_MEMO[key] = digest
    return digest


def _moved_files(snapshot: SourceSnapshot) -> tuple[list[str], list[str], list[str]]:
    """Return the snapshot files whose bytes changed, those removed, and new ones."""

    current = _source_files(snapshot.root)
    changed: list[str] = []
    removed: list[str] = []
    for relative, (signature, digest) in snapshot.files.items():
        path = current.get(relative)
        if path is None:
            removed.append(relative)
            continue
        try:
            now = file_signature(path)
            if now != signature and _current_hash(path, now) != digest:
                changed.append(relative)
        except OSError:
            removed.append(relative)
    added = [relative for relative in current if relative not in snapshot.files]
    return sorted(changed), sorted(removed), sorted(added)


# ── Definition index ───────────────────────────────────────────────────────


@dataclass
class _ModuleIndex:
    """One module's top-level definitions, as far as dependencies need them."""

    #: Top-level name → the statements that bind it, in source order.
    bindings: dict[str, list[ast.stmt]] = field(default_factory=dict)
    functions: set[str] = field(default_factory=set)
    classes: dict[str, ast.ClassDef] = field(default_factory=dict)
    #: Local name → (absolute module, attribute or None for a module import).
    imports: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    #: The top-level statements that are neither a def nor a class.
    statements: list[ast.stmt] = field(default_factory=list)
    tree: ast.Module | None = None


def _module_name(package: str, relative: str) -> str:
    parts = relative.removesuffix(".py").split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join([package, *parts])


def _module_file(snapshot: SourceSnapshot, module: str) -> str | None:
    package = snapshot.root.name
    if module != package and not module.startswith(package + "."):
        return None
    parts = module.split(".")[1:]
    stem = "/".join(parts)
    for candidate in (f"{stem}.py", f"{stem}/__init__.py" if stem else "__init__.py"):
        if candidate in snapshot.files:
            return candidate
    return None


def _import_targets(
    node: ast.Import | ast.ImportFrom, module: str, is_package: bool
) -> Iterable[tuple[str, str, str | None]]:
    """Yield (local name, absolute module, attribute or None) for an import."""

    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.asname:
                yield alias.asname, alias.name, None
        return
    if node.level:
        base = module.split(".")
        if not is_package:
            base = base[:-1]
        base = base[: len(base) - (node.level - 1)]
        source = ".".join(base + ([node.module] if node.module else []))
    else:
        source = node.module or ""
    for alias in node.names:
        yield alias.asname or alias.name, source, alias.name


def _bound_names(statement: ast.stmt) -> Iterable[str]:
    if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        yield statement.name
    elif isinstance(statement, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        targets = (
            statement.targets
            if isinstance(statement, ast.Assign)
            else [statement.target]
        )
        for target in targets:
            for node in ast.walk(target):
                if isinstance(node, ast.Name):
                    yield node.id
    elif isinstance(statement, (ast.If, ast.Try, ast.With)):
        for child in ast.iter_child_nodes(statement):
            if isinstance(child, ast.stmt):
                yield from _bound_names(child)
        for handler in getattr(statement, "handlers", []):
            for child in handler.body:
                yield from _bound_names(child)


def _index(snapshot: SourceSnapshot, relative: str, text: str) -> _ModuleIndex | None:
    key = (relative, hashlib.sha256(text.encode()).hexdigest())
    with _LOCK:
        if key in _INDEX_MEMO:
            return _INDEX_MEMO[key]
    try:
        tree = ast.parse(text)
    except SyntaxError:
        index = None
    else:
        index = _ModuleIndex(tree=tree)
        module = _module_name(snapshot.root.name, relative)
        is_package = relative.endswith("__init__.py")
        for statement in tree.body:
            for node in (
                ast.walk(statement)
                if isinstance(statement, (ast.If, ast.Try, ast.With))
                else [statement]
            ):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    for name, source, attribute in _import_targets(
                        node, module, is_package
                    ):
                        index.imports[name] = (source, attribute)
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                index.functions.add(statement.name)
            elif isinstance(statement, ast.ClassDef):
                index.classes[statement.name] = statement
            else:
                index.statements.append(statement)
            for name in _bound_names(statement):
                index.bindings.setdefault(name, []).append(statement)
    with _LOCK:
        _INDEX_MEMO[key] = index
    return index


class _StripDocstrings(ast.NodeTransformer):
    """Remove docstrings, which document behaviour without changing it."""

    def _strip(self, node):
        self.generic_visit(node)
        body = getattr(node, "body", None)
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
        return node

    def visit_Module(self, node):
        return self._strip(node)

    def visit_FunctionDef(self, node):
        return self._strip(node)

    def visit_AsyncFunctionDef(self, node):
        return self._strip(node)

    def visit_ClassDef(self, node):
        return self._strip(node)


def _fingerprint(nodes: list[ast.AST]) -> str:
    stripped = [_StripDocstrings().visit(copy.deepcopy(node)) for node in nodes]
    # ast.dump leaves out positions, so moving a definition is not a change.
    return "\n".join(ast.dump(node) for node in stripped)


def _definition(index: _ModuleIndex | None, key: str) -> str | None:
    """Return a definition's fingerprint, or None when the module lacks it."""

    if index is None or index.tree is None:
        return None
    if key == _WHOLE_FILE:
        return _fingerprint([index.tree])
    if key == _MODULE:
        return _fingerprint(index.statements)
    if "." in key:
        class_name, member = key.split(".", 1)
        cls = index.classes.get(class_name)
        if cls is None:
            return None
        if member == _MODULE:
            # The class without its methods: bases, decorators, class attributes.
            shell = copy.deepcopy(cls)
            shell.body = [
                node
                for node in shell.body
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            ] or [ast.Pass()]
            return _fingerprint([shell])
        for node in cls.body:
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == member
            ):
                return _fingerprint([node])
        # A member that is not a method (a nested class): compare the class.
        return _fingerprint([cls])
    statements = index.bindings.get(key)
    return _fingerprint(statements) if statements else None


def _definition_nodes(index: _ModuleIndex, key: str) -> list[ast.AST]:
    if key == _WHOLE_FILE:
        return [index.tree] if index.tree else []
    if key == _MODULE:
        return list(index.statements)
    if "." in key:
        class_name, member = key.split(".", 1)
        cls = index.classes.get(class_name)
        if cls is None:
            return []
        if member == _MODULE:
            return (
                [
                    node
                    for node in cls.body
                    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                ]
                + list(cls.bases)
                + list(cls.decorator_list)
            )
        methods = [
            node
            for node in cls.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == member
        ]
        return methods or [cls]
    return list(index.bindings.get(key, []))


def _key_for(qualname: str, index: _ModuleIndex | None) -> list[str]:
    """Map a code object's qualified name to the definitions it belongs to."""

    head = qualname.split(".<locals>.", 1)[0]
    if head.startswith("<") or index is None:
        return [_MODULE]
    parts = head.split(".")
    if parts[0] in index.classes and len(parts) > 1:
        return [f"{parts[0]}.{parts[1]}", f"{parts[0]}.{_MODULE}"]
    return [parts[0]]


def _used_definitions(
    snapshot: SourceSnapshot, executed: Mapping[str, Iterable[str]]
) -> dict[str, set[str]]:
    """Return file → definition keys the process ran or read, from its own code."""

    used: dict[str, set[str]] = {}
    queue: list[tuple[str, str]] = []

    def add(relative: str, key: str) -> None:
        keys = used.setdefault(relative, set())
        if key not in keys:
            keys.add(key)
            queue.append((relative, key))

    def index_of(relative: str) -> _ModuleIndex | None:
        text = snapshot.sources.get(relative)
        return _index(snapshot, relative, text) if text is not None else None

    def depend(module: str, name: str, seen: frozenset = frozenset()) -> None:
        relative = _module_file(snapshot, module)
        if relative is None or (relative, name) in seen:
            return
        index = index_of(relative)
        if index is None:
            return
        if name in index.imports:  # a re-export: follow it to its definition
            source, attribute = index.imports[name]
            if attribute is not None:
                depend(source, attribute, seen | {(relative, name)})
            return
        if name in index.functions:
            return  # a function counts only once the process has run it
        if name in index.bindings:
            add(relative, name)

    for relative, qualnames in executed.items():
        if relative not in snapshot.files:
            continue
        index = index_of(relative)
        for qualname in qualnames:
            for key in (
                [_WHOLE_FILE] if qualname == _WHOLE_FILE else _key_for(qualname, index)
            ):
                add(relative, key)

    while queue:
        relative, key = queue.pop()
        index = index_of(relative)
        if index is None or key == _WHOLE_FILE:
            continue
        nodes = _definition_nodes(index, key)
        module = _module_name(snapshot.root.name, relative)
        is_package = relative.endswith("__init__.py")
        local_imports: dict[str, tuple[str, str | None]] = {}
        names: set[str] = set()
        attributes: set[tuple[str, str]] = set()
        for top in nodes:
            for node in ast.walk(top):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    for name, source, attribute in _import_targets(
                        node, module, is_package
                    ):
                        local_imports[name] = (source, attribute)
                elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                    names.add(node.id)
                elif isinstance(node, ast.Attribute) and isinstance(
                    node.value, ast.Name
                ):
                    attributes.add((node.value.id, node.attr))
        imports = {**index.imports, **local_imports}
        for name in names:
            if name in imports:
                source, attribute = imports[name]
                if (
                    attribute is not None
                    and _module_file(snapshot, f"{source}.{attribute}") is None
                ):
                    depend(source, attribute)
            elif name in index.bindings and name not in index.functions:
                add(relative, name)
        for alias, attribute in attributes:
            target = imports.get(alias)
            if target is None:
                continue
            source, member = target
            submodule = source if member is None else f"{source}.{member}"
            if _module_file(snapshot, submodule) is not None:
                depend(submodule, attribute)
    return used


def definition_fingerprints(
    snapshot: SourceSnapshot, executed: Mapping[str, Iterable[str]]
) -> dict[str, dict[str, object]]:
    """Record the definitions reached by a derivation and their file revisions."""

    result: dict[str, dict[str, object]] = {}
    for relative, keys in _used_definitions(snapshot, executed).items():
        source = snapshot.sources.get(relative)
        known = snapshot.files.get(relative)
        if source is None or known is None:
            continue
        index = _index(snapshot, relative, source)
        result[relative] = {
            "revision": list(known[0]),
            "definitions": {
                key: hashlib.sha256(
                    (_definition(index, key) or "").encode()
                ).hexdigest()
                for key in sorted(keys)
            },
        }
    return result


def definitions_unchanged(root: Path, recorded: Mapping[str, object]) -> bool:
    """Check only reached definitions in files whose revision has moved."""

    if not recorded:
        return False
    for relative, entry in recorded.items():
        if (
            not isinstance(relative, str)
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or not relative.endswith(".py")
            or not isinstance(entry, dict)
        ):
            return False
        expected = entry.get("definitions")
        revision = entry.get("revision")
        if (
            not isinstance(expected, dict)
            or not expected
            or any(
                not isinstance(key, str)
                or not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                for key, digest in expected.items()
            )
            or not isinstance(revision, list)
            or len(revision) != 5
            or any(not isinstance(part, int) for part in revision)
        ):
            return False
        path = root / relative
        try:
            current_revision = file_signature(path)
        except OSError:
            return False
        if list(current_revision) == revision:
            continue
        keys = tuple(sorted(expected))
        memo_key = (str(path), current_revision, keys)
        with _LOCK:
            actual = _DEFINITION_MEMO.get(memo_key)
        if actual is None:
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
                if file_signature(path) != current_revision:
                    return False
            except OSError:
                return False
            snapshot = SourceSnapshot(root=root)
            index = _index(snapshot, relative, source)
            actual = {
                key: hashlib.sha256(
                    (_definition(index, key) or "").encode()
                ).hexdigest()
                for key in keys
            }
            with _LOCK:
                _DEFINITION_MEMO[memo_key] = actual
        if actual != expected:
            return False
    return True


# ── Reports ────────────────────────────────────────────────────────────────


def _normalise(
    executed: Mapping[str, Iterable[str]] | Iterable[str],
) -> dict[str, set[str]]:
    if isinstance(executed, Mapping):
        return {relative: set(names) for relative, names in executed.items()}
    return {relative: {_WHOLE_FILE} for relative in executed}


def _compare(
    snapshot: SourceSnapshot,
    executed: Mapping[str, Iterable[str]] | Iterable[str] | None,
) -> dict:
    moved, removed, added = _moved_files(snapshot)
    definitions: list[str] = []
    if executed is None:
        scope = "package"
        changed, gone, new = moved, removed, added
    else:
        scope = "executed"
        changed, gone, new = [], [], []
        if moved or removed:
            used = _used_definitions(snapshot, _normalise(executed))
            for relative in sorted(set(moved) | set(removed)):
                keys = used.get(relative)
                if not keys:
                    continue
                before = _index(snapshot, relative, snapshot.sources.get(relative, ""))
                path = snapshot.root / relative
                after = (
                    _index(
                        snapshot,
                        relative,
                        path.read_text(encoding="utf-8", errors="replace"),
                    )
                    if relative in moved
                    else None
                )
                differing = sorted(
                    key
                    for key in keys
                    if _definition(before, key) != _definition(after, key)
                )
                if differing:
                    (gone if relative in removed else changed).append(relative)
                    definitions.extend(f"{relative}:{key}" for key in differing)
    stale = bool(changed or gone or new)

    disk_revision = _git(snapshot.root, "rev-parse", "HEAD")
    commits_behind: int | None = None
    if stale and (changed or gone) and snapshot.revision and disk_revision:
        # Only the commits that touched a file this report counts.
        counted = _git(
            snapshot.root,
            "rev-list",
            "--count",
            f"{snapshot.revision}..{disk_revision}",
            "--",
            *(changed + gone),
        )
        commits_behind = int(counted) if counted and counted.isdigit() else None
    return {
        "stale": stale,
        "summary": _summary(sorted(changed + gone + new), snapshot.taken_at, scope)
        if stale
        else None,
        "scope": scope,
        "started_at": snapshot.taken_at,
        "revision": snapshot.revision,
        "disk_revision": disk_revision,
        "commits_behind": commits_behind,
        "changed": changed,
        "added": new,
        "removed": gone,
        "definitions": definitions,
        "restart_command": RESTART_COMMAND,
    }


def _summary(files: list[str], started_at: str, scope: str) -> str:
    """One sentence a reader can act on, for every surface that shows drift."""

    parsed_start = parse_iso(started_at)
    if parsed_start is None:
        started = started_at or "an unknown time"
    else:
        started = parsed_start.strftime("%Y-%m-%d %H:%M UTC")
    count = len(files)
    named = ", ".join(files[:3]) + (f" and {count - 3} more" if count > 3 else "")
    where = (
        f"code it runs changed in {count} file{'' if count == 1 else 's'}"
        if scope == "executed"
        else f"{count} package file{'' if count == 1 else 's'} changed"
    )
    return (
        f"The server is behind the code on disk: {where} since it started at "
        f"{started} ({named}). Restart it to serve the current code."
    )


def drift(
    snapshot: SourceSnapshot,
    *,
    executed: Mapping[str, Iterable[str]] | Iterable[str] | None = None,
    max_age_s: float = 0.0,
) -> dict:
    """Compare ``snapshot`` with the package on disk.

    ``executed`` maps a package-relative file to the qualified names of the
    code the process ran from it (a bare collection of files compares those
    files whole). Without it, every file of the package counts. ``max_age_s``
    reuses a report that young for the same snapshot, which is what the server
    passes so a page poll does not re-stat the package each time.
    """

    key = id(snapshot)
    now = time.monotonic()
    if max_age_s > 0:
        with _LOCK:
            reused = _REPORTS.get(key)
        if reused is not None and now - reused[0] < max_age_s:
            return dict(reused[1])
    report = _compare(snapshot, executed)
    with _LOCK:
        _REPORTS[key] = (now, report)
    return dict(report)


def served_report(
    snapshot: SourceSnapshot | None,
    executed: Mapping[str, Iterable[str]] | Iterable[str] | None = None,
) -> dict | None:
    """Return the report a server serves, reusing one for a short window."""

    if snapshot is None:
        return None
    return drift(snapshot, executed=executed, max_age_s=_REPORT_TTL_S)


# ── The moved-checkout decision ────────────────────────────────────────────
#
# Two servers must answer one question about an image whose code has moved: may
# the moved tree be loaded, and so may the call be answered on its current code?
# The HTTP server answers by re-executing, the MCP server by running the call
# again on the current code; both must reach the same verdict about the tree, or
# one serves code the other refuses. The verdict is derived here, once, from the
# snapshot the process loaded and the checkout's own code identity.


@dataclass(frozen=True)
class MovedCheckout:
    """Whether the code an image loaded differs from the checkout, and if it loads.

    ``moved`` says the checkout's code is not the code this image runs, which is
    what makes a failed call worth answering on current code. ``trusted`` says
    the moved tree may be loaded at all -- clean under the package against
    ``HEAD`` and importable -- so a tree mid-edit is never loaded over a running
    image. ``running_stamp`` and ``disk_stamp`` are the two identities the
    verdict compares, kept so a reader can report the change it saw.
    """

    moved: bool
    trusted: bool
    detail: str
    running_stamp: str
    disk_stamp: str
    changed_files: tuple[str, ...] = ()

    @property
    def may_load(self) -> bool:
        """True only for a moved tree a reader is allowed to load."""
        return self.moved and self.trusted


def code_fingerprints(
    snapshot: SourceSnapshot, report: Mapping[str, object]
) -> tuple[str, str]:
    """Fingerprint exactly the source files the executed-code report counted.

    Two digests, one over the bytes the process loaded and one over the bytes on
    disk, so ``running == disk`` means the report's change was a timestamp
    rather than a content move and the image still serves what it run.
    """

    names = sorted(set(report["changed"] + report["added"] + report["removed"]))
    running = hashlib.sha256()
    disk = hashlib.sha256()
    for name in names:
        known = snapshot.files.get(name)
        before = known[1] if known is not None else "missing"
        try:
            after = hashlib.sha256((snapshot.root / name).read_bytes()).hexdigest()
        except OSError:
            after = "missing"
        running.update(f"{name}:{before}\n".encode())
        disk.update(f"{name}:{after}\n".encode())
    return running.hexdigest(), disk.hexdigest()


def served_code_moved(
    snapshot: SourceSnapshot, report: Mapping[str, object]
) -> MovedCheckout:
    """The HTTP server's decision: does the report's change reach the image?

    A report counts a file whose identity moved, which a touch alone moves. The
    fingerprints tell a touch from a content change, and only a content change
    makes the image stale. The HTTP server loads the checkout it serves, so it
    does not consult the tree's trust itself.
    """

    running, disk = code_fingerprints(snapshot, report)
    return MovedCheckout(
        moved=running != disk,
        trusted=False,
        detail="",
        running_stamp=running,
        disk_stamp=disk,
        changed_files=tuple(report["changed"] + report["added"] + report["removed"]),
    )


def moved_checkout(*, root: Path, running_stamp: str) -> MovedCheckout:
    """The MCP server's decision: has the checkout's code moved, and may it load?

    ``running_stamp`` is the code identity the server recorded when it imported
    the package, and ``root`` the checkout that supplied it. The verdict is the
    trust-checked one, so a caller re-running a call on current code does so only
    onto a tree the followers would themselves load.

    The identity is read the way the running stamp was: ``source_code_identity``
    identifies a package directory and derives the checkout from the directory
    above it, so it is handed the package inside ``root``. Handed the checkout
    itself it would identify the directory above the checkout and never match,
    which reads every checkout as moved.
    """

    from reckon import checkout_trust

    package_dir = Path(root) / "reckon"
    if not package_dir.is_dir():
        package_dir = Path(root)
    current = checkout_trust.source_code_identity(package_dir)
    if current == running_stamp:
        return MovedCheckout(
            moved=False,
            trusted=False,
            detail="",
            running_stamp=running_stamp,
            disk_stamp=current,
        )
    verdict = checkout_trust.moved_checkout_is_trustworthy(root)
    return MovedCheckout(
        moved=True,
        trusted=verdict.trusted,
        detail=verdict.detail,
        running_stamp=running_stamp,
        disk_stamp=current,
    )


class ExecutedSource:
    """Record which of a package's functions the process has run.

    One ``sys.monitoring`` callback fires the first time each code object
    starts and returns DISABLE, so the cost is one call per function for the
    life of the process and nothing after. It sees every thread. Tracking
    starts after the process has finished importing, so the command line that
    launched the server is not counted: it ran before tracking began and never
    runs again. Constants and classes do not run; they are reached from the
    functions that read them (see :func:`_used_definitions`).
    """

    #: Tool ids not reserved by ``sys.monitoring`` (0 debugger, 1 coverage,
    #: 2 profiler, 5 optimizer). A process with both taken reports on the whole
    #: package instead.
    _TOOL_IDS = (4, 3)

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        # The interpreter records the path a module was imported by, which
        # may or may not be the resolved one; either names this package.
        self._prefixes = tuple(
            dict.fromkeys([str(self.root) + os.sep, str(self.root.resolve()) + os.sep])
        )
        self._seen: set[tuple[str, str]] = set()
        # Held on the instance so the callback touches no module global: a
        # callback still registered while the interpreter shuts down finds the
        # sys module already torn down.
        self._disable = sys.monitoring.DISABLE
        self.tool: int | None = None

    def start(self) -> bool:
        monitoring = sys.monitoring
        for tool in self._TOOL_IDS:
            if monitoring.get_tool(tool) is None:
                monitoring.use_tool_id(tool, "reckon-served-code")
                self.tool = tool
                break
        else:
            return False
        monitoring.register_callback(
            self.tool, monitoring.events.PY_START, self._started
        )
        monitoring.set_events(self.tool, monitoring.events.PY_START)
        return True

    def _started(self, code, instruction_offset):
        if code.co_filename.startswith(self._prefixes):
            self._seen.add((code.co_filename, code.co_qualname))
        return self._disable

    def _relative(self, filename: str) -> str | None:
        for prefix in self._prefixes:
            if filename.startswith(prefix):
                return filename[len(prefix) :].replace(os.sep, "/")
        return None

    def definitions(self) -> dict[str, set[str]]:
        """Return package-relative file → qualified names the process has run."""

        found: dict[str, set[str]] = {}
        for filename, qualname in list(self._seen):
            relative = self._relative(filename)
            if relative is not None:
                found.setdefault(relative, set()).add(qualname)
        return found

    def relative_paths(self) -> set[str]:
        """Return the package-relative posix paths the process has run."""

        return set(self.definitions())

    def stop(self) -> None:
        if self.tool is None:
            return
        monitoring = sys.monitoring
        monitoring.set_events(self.tool, 0)
        monitoring.register_callback(self.tool, monitoring.events.PY_START, None)
        monitoring.free_tool_id(self.tool)
        self.tool = None


# ── Reach graph ────────────────────────────────────────────────────────────
#
# A node's gate proves its code works when called; it does not prove anything
# calls it. This resolves, for one revision's package tree, which modules
# and module-level definitions are reachable from the production entry points —
# the console scripts in ``pyproject.toml``, the MCP server, the HTTP server and
# every module launched by ``python -m`` from reckon's own source. It reads the
# tree with ``ast`` and never imports the package, so a module that cannot be
# imported safely is still measured.

#: Modules that are production entry points by name. ``reckon.cli`` mounts the
#: console script's Click verbs; ``reckon.cli_entry`` carries them; ``reckon.mcp``
#: is the MCP server and its tool functions; ``reckon.serve`` is the HTTP server
#: and its handlers.
_ROOT_MODULES = ("reckon.cli", "reckon.cli_entry", "reckon.mcp", "reckon.serve")

#: Hook modules are launched by the harness through ``python -m``.
_HOOK_PREFIX = "reckon.hooks."

#: A ``python -m reckon.<...>`` invocation in a source string is a production
#: entry point, whether in code, a docstring or a usage message.
_LAUNCH_RE = re.compile(r"""-m["'\s,]+(reckon[\w.]*)""")

#: Decorators that register a definition with the framework that will call it,
#: so a definition carrying one runs as soon as its module is imported.
_REGISTRATION_MARKERS = (
    ".command",
    ".group",
    ".tool",
    ".route",
    ".register",
    ".handler",
    ".resource",
    ".prompt",
    ".hook",
)


@dataclass(frozen=True)
class Reach:
    """The production modules and definitions reached from the entry points."""

    #: Module names (``reckon.crew.runs``) reached through imports.
    modules: frozenset[str]
    #: ``<module>:<qualified name>`` for every module-level definition reached.
    definitions: frozenset[str]
    #: The entry points the walk started from.
    roots: frozenset[str]


#: Directories that never name the reviewed repository's import package.
_NON_PACKAGE_DIRS = frozenset({"tests", "docs", "scripts"})


class PackageNotFoundError(ValueError):
    """A reviewed repository declares no single import package to read."""


def _tracked_init_dirs(repo, revision) -> set[str]:
    """Every directory holding a tracked ``__init__.py`` at ``revision``."""

    listing = _git(repo, "ls-tree", "-r", "--name-only", revision or "HEAD") or ""
    return {
        path.rsplit("/", 1)[0]
        for path in listing.splitlines()
        if path.endswith("__init__.py") and "/" in path
    }


def _project_name_at(repo, revision) -> str | None:
    """The ``[project] name`` in ``pyproject.toml`` at ``revision``, normalised."""

    import tomllib

    from reckon.velocity import read_blobs

    spec = (revision, "pyproject.toml")
    blob = read_blobs(repo, [spec]).get(spec)
    if not blob:
        return None
    try:
        data = tomllib.loads(blob.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        return None
    name = data.get("project", {}).get("name")
    return name.replace("-", "_") if isinstance(name, str) and name else None


def _package_candidates(directories: Iterable[str]) -> list[str]:
    """Top-level (or ``src/`` child) directories that could be the package."""

    found: list[str] = []
    for directory in sorted(set(directories)):
        parts = directory.split("/")
        if (len(parts) == 1 and parts[0] not in _NON_PACKAGE_DIRS) or (
            len(parts) == 2 and parts[0] == "src" and parts[1] not in _NON_PACKAGE_DIRS
        ):
            found.append(directory)
    return found


def _candidate_import_name(candidate: str) -> str:
    """The import name of a candidate package directory (``src/acme`` → ``acme``)."""

    return candidate.rsplit("/", 1)[-1]


def _holds(candidate: str, path: str) -> bool:
    """Whether a named path is held by the candidate package directory.

    A path is held when it lies under the candidate's directory
    (``nova/equilibrium/topology.py``, ``src/acme/core.py``), or when its first
    dotted component is the candidate's import name
    (``nova.equilibrium.topology``). A token naming something else — a
    package-relative path such as ``crew/runs.py`` — is held by no candidate.
    """

    text = str(path).replace("\\", "/").strip().rstrip("/")
    if not text:
        return False
    if text == candidate or text.startswith(candidate + "/"):
        return True
    return text.split(".", 1)[0] == _candidate_import_name(candidate)


def _package_from_paths(candidates: Iterable[str], paths: Iterable[str]) -> str | None:
    """The one candidate holding every named path a candidate holds, or None.

    A named path held by no candidate is ignored, so a package-relative token
    leaves the choice to the declared name. When every held path is held by the
    same candidate, that candidate is the package; held paths pointing at two
    or more candidates are refused, naming them.
    """

    holders: list[str] = []
    for path in paths:
        for candidate in candidates:
            if _holds(candidate, path) and candidate not in holders:
                holders.append(candidate)
    if not holders:
        return None
    if len(holders) == 1:
        return holders[0]
    named = ", ".join(repr(candidate) for candidate in sorted(holders))
    raise PackageNotFoundError(f"no single import package to review: {named}")


def package_directory(repo, revision, paths: Iterable[str] = ()) -> str:
    """The reviewed repository's own import package, as a repo-relative path.

    The named paths the review carries settle the choice when the repository
    declares several candidates: the package is the candidate that holds every
    named path any candidate holds (``nova/equilibrium/topology.py`` and
    ``nova.equilibrium.topology`` both name ``nova``), and paths held by two or
    more candidates are refused by name. A path held by no candidate changes
    nothing. Otherwise the package is the project's declared name when a
    directory of that name (or ``src/<name>``) holds a tracked ``__init__.py``,
    then the single top-level package directory (or ``src/`` child) that does.
    A repository that declares none, or several, is refused by name rather than
    guessed at.
    """

    directories = _tracked_init_dirs(repo, revision)
    chosen = _package_from_paths(_package_candidates(directories), paths)
    if chosen is not None:
        return chosen
    name = _project_name_at(repo, revision)
    if name is not None:
        for candidate in (name, f"src/{name}"):
            if candidate in directories:
                return candidate
    candidates = _package_candidates(directories)
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        named = ", ".join(repr(candidate) for candidate in candidates)
        raise PackageNotFoundError(f"no single import package to review: {named}")
    raise PackageNotFoundError(
        "no import package to review: no tracked directory holds an __init__.py"
    )


def dotted_token(path: str, package: str) -> str:
    """A module name or file path token as a dotted name, under ``package``.

    A leading ``<package>/`` path prefix is dropped, a ``.py`` suffix is
    stripped, and path separators become dots, so ``reckon/crew/runs.py`` and
    ``crew/runs.py`` both read ``crew.runs``. This is the one place a review's
    path normalisation happens.
    """

    text = path.replace("\\", "/").strip().rstrip("/")
    text = text.removeprefix(f"{package}/")
    return text.removesuffix(".py").replace("/", ".")


def as_module(name: str, modules: Iterable[str], package: str) -> str | None:
    """The snapshot module a module name or path token names, or None.

    A token is tried as given, then under ``package``; trailing components are
    dropped until a known module is found, so a directory resolves to its
    nearest enclosing module.
    """

    known = modules if isinstance(modules, (frozenset, set)) else frozenset(modules)
    if name in known:
        return name
    candidate = dotted_token(name, package)
    for probe in (candidate, f"{package}.{candidate}"):
        parts = probe.split(".")
        while parts:
            joined = ".".join(parts)
            if joined in known:
                return joined
            parts.pop()
    return None


def _package_snapshot(repo, revision, paths: Iterable[str] = ()) -> SourceSnapshot:
    """Read the reviewed package's tree into a snapshot keyed package-relative."""

    from reckon.velocity import read_sources

    directory = package_directory(repo, revision, paths)
    sources: dict[str, str] = {}
    files: dict[str, tuple[_Signature, str]] = {}
    for path, text in read_sources(repo, revision, prefix=directory).items():
        relative = path.removeprefix(directory + "/")
        sources[relative] = text.decode("utf-8-sig")
        files[relative] = ((0, 0, 0, 0, 0), "")
    return SourceSnapshot(
        root=Path(directory),
        files=files,
        sources=sources,
        revision=revision or None,
    )


def _edges(tree, module: str, is_package: bool, known: frozenset[str]) -> set[str]:
    """The package modules one ``tree`` imports, resolved to known modules.

    Both ``import a.b`` and ``from a import b`` are edges; a relative import is
    resolved against the file's package. A name imported from a package resolves
    to the submodule it names when that submodule exists, and to the package
    otherwise. An import that resolves to no module in the tree is ignored.
    """

    found: set[str] = set()

    def resolve(name: str) -> str | None:
        parts = name.split(".")
        while parts:
            candidate = ".".join(parts)
            if candidate in known:
                return candidate
            parts.pop()
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                target = resolve(alias.name)
                if target is not None:
                    found.add(target)
        elif isinstance(node, ast.ImportFrom):
            for _local, source, attribute in _import_targets(node, module, is_package):
                if not source:
                    continue
                if attribute is not None:
                    submodule = resolve(f"{source}.{attribute}")
                    if submodule is not None:
                        found.add(submodule)
                        continue
                target = resolve(source)
                if target is not None:
                    found.add(target)
    return found


def _console_script_modules(repo) -> dict[str, str]:
    """Map each console script's module to the callable it names, from pyproject."""

    import tomllib

    try:
        data = tomllib.loads((Path(repo) / "pyproject.toml").read_text("utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    scripts = data.get("project", {}).get("scripts", {})
    modules: dict[str, str] = {}
    for target in scripts.values():
        if isinstance(target, str) and ":" in target:
            module, _, callable_name = target.partition(":")
            modules[module.strip()] = callable_name.strip()
    return modules


def _guard_mains(tree) -> set[str]:
    """The bare names called under an ``if __name__ == "__main__"`` guard."""

    mains: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and "__name__" in ast.unparse(node.test):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
                    mains.add(sub.func.id)
    return mains


def _registered(node) -> bool:
    """Whether a definition carries a registration decorator."""

    joined = " ".join(ast.unparse(d) for d in node.decorator_list).lower()
    return any(marker in joined for marker in _REGISTRATION_MARKERS)


def _identifiers(tree) -> set[str]:
    """Every bare name and attribute name the tree reads."""

    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


def _imported_names(tree) -> set[str]:
    """Every name imported by ``from ... import name`` in the tree."""

    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.name)
    return names


def _entry_definitions(trees, module_of, roots, console_scripts) -> dict[str, set[str]]:
    """Entry callables per module: console targets and ``__main__`` guard calls."""

    entries: dict[str, set[str]] = {}
    for module, callable_name in console_scripts.items():
        relative = module_of.get(module)
        if relative is not None:
            entries.setdefault(relative, set()).add(callable_name)
    for module in roots:
        relative = module_of.get(module)
        if relative is None:
            continue
        entries.setdefault(relative, set()).update(_guard_mains(trees[relative]))
    return entries


def _reach(repo, revision) -> Reach:
    snapshot = _package_snapshot(repo, revision)
    module_of = {
        _module_name(snapshot.root.name, relative): relative
        for relative in snapshot.sources
    }
    known = frozenset(module_of)
    trees = {
        relative: _index(snapshot, relative, text).tree
        for relative, text in snapshot.sources.items()
    }

    console_scripts = _console_script_modules(repo)
    roots: set[str] = {m for m in _ROOT_MODULES if m in known}
    roots |= {m for m in known if m.startswith(_HOOK_PREFIX)}
    roots |= {m for m in console_scripts if m in known}
    for text in snapshot.sources.values():
        for hit in _LAUNCH_RE.findall(text):
            parts = hit.split(".")
            while parts:
                candidate = ".".join(parts)
                if candidate in known and candidate != "reckon":
                    roots.add(candidate)
                    break
                parts.pop()

    edges = {
        module: _edges(trees[relative], module, relative.endswith("__init__.py"), known)
        for module, relative in module_of.items()
    }

    reached_modules: set[str] = set(roots)
    queue = list(roots)
    while queue:
        module = queue.pop()
        for target in edges.get(module, ()):
            if target not in reached_modules:
                reached_modules.add(target)
                queue.append(target)
        # Importing ``a.b`` runs ``a``'s ``__init__`` too, so a package is
        # reached when any of its submodules is.
        parts = module.split(".")
        while len(parts) > 1:
            parts.pop()
            parent = ".".join(parts)
            if parent in known and parent not in reached_modules:
                reached_modules.add(parent)
                queue.append(parent)

    entries = _entry_definitions(trees, module_of, roots, console_scripts)

    from reckon.interface_counts import definitions as enumerate_definitions

    definitions: set[str] = set()
    for module in sorted(reached_modules):
        tree = trees[module_of[module]]
        identifiers = _identifiers(tree)
        imported = _imported_names(tree)
        entry = entries.get(module_of[module], set())
        for node, qualified, _public, nested in enumerate_definitions(tree):
            if nested or "." in qualified:
                continue
            if (
                qualified in entry
                or _registered(node)
                or node.name in identifiers
                or node.name in imported
            ):
                definitions.add(f"{module}:{qualified}")

    return Reach(
        modules=frozenset(reached_modules),
        definitions=frozenset(definitions),
        roots=frozenset(roots),
    )


_REACH_MEMO: dict[tuple[str, str], Reach] = {}


def reached(repo, revision) -> Reach:
    """The production modules and definitions reachable at ``revision``.

    The entry points are the console scripts in ``pyproject.toml`` and the Click
    verbs they mount, the MCP tool functions, the ``serve.py`` handlers, and
    every module launched by ``python -m`` from reckon's own source. Reach is the
    transitive closure over imports; a module-level definition is reached when it
    is an entry point, carries a registration decorator, or its name is read or
    imported by reached code. The result is a pure function of the revision's
    tree, so it is memoised by ``(repo, revision)``.
    """

    key = (str(Path(repo).resolve()), revision or "")
    cached = _REACH_MEMO.get(key)
    if cached is not None:
        return cached
    result = _reach(repo, revision)
    _REACH_MEMO[key] = result
    return result


def package_modules(repo, revision, paths: Iterable[str] = ()) -> frozenset[str]:
    """Every module name under the reviewed package at ``revision``.

    ``paths`` are the review's named paths, which settle the package choice
    when the repository declares several candidates (see
    :func:`package_directory`).
    """

    snapshot = _package_snapshot(repo, revision, paths)
    return frozenset(
        _module_name(snapshot.root.name, relative) for relative in snapshot.sources
    )


def neighbourhood(repo, revision, paths) -> frozenset[str]:
    """The production modules one import away from ``paths``, in either direction.

    ``paths`` are module names or package-relative file paths. The result
    holds every module that imports one of the named modules and every module one
    of them imports, excluding the named modules themselves. The named paths
    also settle the package choice when the repository declares several
    candidates (see :func:`package_directory`).
    """

    snapshot = _package_snapshot(repo, revision, paths)
    module_of = {
        _module_name(snapshot.root.name, relative): relative
        for relative in snapshot.sources
    }
    known = frozenset(module_of)
    package = snapshot.root.name

    named = {
        module
        for module in (as_module(path, known, package) for path in paths)
        if module is not None
    }
    if not named:
        return frozenset()

    edges = {
        module: _edges(
            ast.parse(snapshot.sources[relative]),
            module,
            relative.endswith("__init__.py"),
            known,
        )
        for module, relative in module_of.items()
    }
    adjacent: set[str] = set()
    for module in named:
        adjacent |= edges.get(module, set())
        adjacent |= {src for src, targets in edges.items() if module in targets}
    return frozenset(adjacent - named)


def signature_compatible(
    registered: Mapping[str, bool],
    params: object,
    required: object,
) -> bool:
    """Whether the current code accepts every parameter a client registered.

    ``registered`` maps each parameter the client's schema holds to whether the
    client marked it required. A change is compatible when every registered
    parameter is still accepted, no registered optional parameter has become
    required, and no parameter the client does not hold is required. A client
    whose schema still covers every call the tool accepts needs no reconnect;
    one that lost a parameter, or gained a client-invisible required one, has
    calls it can make that the current code would refuse.
    """
    current = (
        {str(item) for item in params}
        if isinstance(params, (list, tuple, set, frozenset))
        else set()
    )
    current_required = (
        {str(item) for item in required}
        if isinstance(required, (list, tuple, set, frozenset))
        else set()
    )
    for name, was_required in registered.items():
        if name not in current:
            return False
        if not was_required and name in current_required:
            return False
    return all(name in registered for name in current_required)


def probe_reply(
    params: Mapping[str, list[str]],
    required: Mapping[str, list[str]],
    registered: Mapping[str, Mapping[str, bool]] | None,
) -> dict:
    """The probe answer: each tool's parameters, their required subset, a verdict.

    ``params`` is each tool's parameter names and ``required`` names the subset
    that carries no default — both keyed by tool. The compatibility verdict,
    keyed by tool, is present only when the request carried the caller's own
    registered parameters, so a probe from a parent that predates the verdict
    gets the answer it already understands and compares it itself.
    """
    reply: dict = {"params": params, "required": required}
    if registered:
        reply["compatible"] = {
            name: signature_compatible(
                registered.get(name) or {}, params.get(name), required.get(name)
            )
            for name in registered
        }
    return reply


# ── The persistent current-code child ──────────────────────────────────────
#
# A stdio MCP server cannot replace the image it runs the way the HTTP server
# re-executes: the client holds the session it initialised, and a fresh process
# has not received it. So the tree's current code runs behind the front in one
# persistent child per code stamp. The child is started when the guard first
# sees a stamp it does not hold, answers a readiness probe with every tool's
# parameter names and runs no tool, and then takes one request per line until
# the stamp moves again or its input pipe closes. Warming is by the probe rather
# than by a caller's call, so a call made while the child is still cold is
# served by the running image and never waits on the child.

#: The child's program. It imports the tree, so it holds the current code; the
#: probe reports the parameter names of every registered tool without running
#: one, and each later request runs at most one tool. The loop ends at EOF on
#: its stdin, which closing the server's end of the pipe delivers.
SERVED_CHILD_PROGRAM = r"""
import asyncio, json, sys

root = sys.argv[1]
sys.path.insert(0, root)

import reckon.mcp as reckon_mcp
from reckon import served_code as served_code_module


def structured(result):
    if isinstance(result, tuple):
        for item in result:
            if isinstance(item, dict):
                return item
        result = result[0]
    if isinstance(result, list):
        for block in result:
            text = getattr(block, "text", None)
            if text:
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return {"text": text}
    return {"unstructured": repr(result)}


def tool_params():
    return {
        item.name: sorted(item.fn_metadata.arg_model.model_fields)
        for item in reckon_mcp.mcp._tool_manager.list_tools()
    }


def tool_required():
    return {
        item.name: sorted(
            field_name
            for field_name, field in item.fn_metadata.arg_model.model_fields.items()
            if field.is_required()
        )
        for item in reckon_mcp.mcp._tool_manager.list_tools()
    }


def run_tool(name, arguments):
    out = {"reached": False}
    try:
        tool = next(
            item
            for item in reckon_mcp.mcp._tool_manager.list_tools()
            if item.name == name
        )
        out["params"] = sorted(tool.fn_metadata.arg_model.model_fields)
        # A failure raised after this point is the tool's own answer; one
        # raised before it belongs to no code the caller could retry on.
        out["reached"] = True
        inner = getattr(getattr(tool, "fn", None), "_stale_guard_inner", None)
        if inner is None:
            result = structured(asyncio.run(reckon_mcp.mcp.call_tool(name, arguments)))
        else:
            result = asyncio.run(inner(**arguments))
            if not isinstance(result, dict):
                result = structured(result)
        out["answer"] = result
    except BaseException as exc:  # noqa: BLE001 - any failure is the child's answer
        reason = f"{type(exc).__name__}: {exc}"
        return {
            "reached": False,
            "answer": {"ok": False, "error": "crew_error", "detail": reason},
        }
    return out


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        message = json.loads(line)
    except ValueError:
        continue
    ident = message.get("id")
    if message.get("op") == "probe":
        reply = {
            "id": ident,
            "op": "probe",
            **served_code_module.probe_reply(
                tool_params(), tool_required(), message.get("registered")
            ),
        }
    else:
        reply = {"id": ident, "op": "call"}
        reply.update(run_tool(message.get("tool"), message.get("kwargs") or {}))
    sys.stdout.write(json.dumps(reply) + "\n")
    sys.stdout.flush()
"""


#: The file a served child's stderr is kept in, named beside the code it serves
#: so a reader can find a death's cause without being told a path.
CHILD_STDERR_NAME = ".reckon-served-child.stderr.log"


def child_stderr_path(root: Path) -> Path:
    """Where a served child's stderr is kept for a reader after it dies.

    One file per served tree, rewritten on every spawn, so it holds the most
    recent child's stderr rather than the accumulation of every death. A child
    that floods it still bounds the file to one child's output.
    """
    return Path(root) / CHILD_STDERR_NAME


def spawn_served_child(root: Path) -> subprocess.Popen:
    """Launch one persistent child holding the tree's current code.

    The child inherits this process's environment so its interpreter and site
    packages resolve the same way, and reads requests from a pipe the caller
    writes and closes. Its stderr goes to :func:`child_stderr_path`, truncated
    here, so a child that dies before answering leaves its cause on disk.
    """

    environment = dict(os.environ)
    environment.setdefault("PYTHONUNBUFFERED", "1")
    stderr = open(child_stderr_path(root), "wb")  # noqa: SIM115 - closed below
    try:
        return subprocess.Popen(
            [sys.executable, "-c", SERVED_CHILD_PROGRAM, str(root)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=True,
            bufsize=1,
            env=environment,
        )
    finally:
        stderr.close()


def parse_child_reply(line: str) -> dict | None:
    """One reply line, or None when it carries no whole request id."""
    try:
        message = json.loads(line)
    except ValueError:
        return None
    if not isinstance(message, dict) or "id" not in message:
        return None
    return message


class ServedChild:
    """A persistent interpreter serving requests on one code stamp.

    Every request carries an id and every reply echoes it, so a reply that
    arrives after its caller gave up is discarded rather than misread as the
    caller's own. A reader thread is the only reader of the child's stdout,
    which is what lets a caller bound its own wait while the pipe stays drained.
    """

    def __init__(self, process: subprocess.Popen, *, timeout_s: float) -> None:
        self._process = process
        self._timeout_s = timeout_s
        self._next_id = 0
        self._probe_id: int | None = None
        self._warm: dict | None = None
        self._replies: dict[int, dict] = {}
        self._condition = threading.Condition()
        self._closed = False
        self._write_lock = threading.Lock()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        stream = self._process.stdout
        try:
            if stream is not None:
                for line in stream:
                    message = parse_child_reply(line)
                    if message is None:
                        continue
                    with self._condition:
                        self._replies[message["id"]] = message
                        self._condition.notify_all()
        except (OSError, ValueError):
            pass
        finally:
            with self._condition:
                self._closed = True
                self._condition.notify_all()

    def _send(self, op: str, **payload: object) -> int | None:
        with self._write_lock:
            stream = self._process.stdin
            if stream is None:
                return None
            self._next_id += 1
            ident = self._next_id
            try:
                stream.write(json.dumps({"id": ident, "op": op, **payload}) + "\n")
                stream.flush()
            except (OSError, ValueError):
                return None
            return ident

    def _await(self, ident: int | None, timeout_s: float) -> dict | None:
        if ident is None:
            return None
        deadline = time.monotonic() + timeout_s
        with self._condition:
            while ident not in self._replies:
                if self._closed:
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)
            return self._replies.pop(ident)

    def _take(self, ident: int | None) -> dict | None:
        if ident is None:
            return None
        with self._condition:
            return self._replies.pop(ident, None)

    def probe(self, registered: Mapping[str, Mapping[str, bool]] | None = None) -> None:
        """Ask the child to report every tool's parameters, without waiting.

        ``registered`` is the caller's own parameter map, each parameter with
        whether the caller marked it required, so the child can answer whether
        the change is compatible without the caller re-deciding it.
        """
        self._probe_id = self._send("probe", registered=registered)

    def warm_answer(self) -> dict | None:
        """The probe's answer once it has arrived; None while it is cold.

        The answer is kept once read, so every later call reads the same one
        rather than consuming it and reporting a warm child as cold.
        """
        if self._warm is not None:
            return self._warm
        answer = self._take(self._probe_id)
        if isinstance(answer, dict):
            self._warm = answer
        return answer

    def run(self, tool: str, kwargs: Mapping[str, object]) -> dict | None:
        """Run one tool in the child, bounded; None means it made no answer."""
        ident = self._send("call", tool=tool, kwargs=dict(kwargs))
        return self._await(ident, self._timeout_s)

    @property
    def alive(self) -> bool:
        """Whether the child is still running."""
        return self._process.poll() is None

    def stop(self) -> None:
        """Close the child's input pipe, which is its instruction to end."""
        with self._write_lock:
            stream = self._process.stdin
            if stream is not None:
                with contextlib.suppress(OSError, ValueError):
                    stream.close()
                self._process.stdin = None
        with contextlib.suppress(subprocess.TimeoutExpired):
            self._process.wait(timeout=5)
            return
        self._process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            self._process.wait(timeout=5)


class CurrentCodeServer:
    """One persistent child per code stamp, behind the stdio front.

    The child is started when the guard first sees a stamp it does not hold,
    and is stopped and replaced when the stamp moves again, so a session pays
    one import per commit rather than one per call. Warming is by the readiness
    probe: until the probe has answered the child is cold and no call is routed
    to it, which is what keeps a cold child from making a caller wait.

    A child that dies is replaced, but only up to ``death_bound`` consecutive
    deaths within one stamp: past that the server stops respawning and lets the
    running image answer every call until the stamp moves, so a child that dies
    right after every spawn costs a bounded number of imports rather than one
    per call.
    """

    def __init__(
        self,
        root: Path,
        *,
        spawn: Callable[[Path], subprocess.Popen] | None = None,
        timeout_s: float = 120.0,
        death_bound: int = 3,
    ) -> None:
        self._root = Path(root)
        self._spawn_child = spawn or spawn_served_child
        self._timeout_s = timeout_s
        self._death_bound = max(1, int(death_bound))
        self._deaths = 0
        self._backed_off = False
        self._stamp: str | None = None
        self._child: ServedChild | None = None
        self._spawns = 0
        self._lock = threading.Lock()

    def _child_for(
        self,
        stamp: str,
        registered: Mapping[str, Mapping[str, bool]] | None = None,
    ) -> ServedChild | None:
        """The live child for ``stamp``, starting one while within the bound.

        A child that has died is not the child for this stamp however well its
        stamp matches: reusing it makes every routed call take the may-have-
        landed timeout until the stamp happens to move again. A dead child is
        dropped and counted; one replacement is started while the consecutive
        deaths are under the bound, and past it the server backs off and starts
        none until the stamp moves. A replacement is cold until its own probe
        answers, so calls are served on the running image meanwhile.
        """
        with self._lock:
            if self._stamp is not None and self._stamp != stamp:
                if self._child is not None:
                    self._child.stop()
                    self._child = None
                self._deaths = 0
                self._backed_off = False
            if self._child is not None and not self._child.alive:
                self._child.stop()
                self._child = None
                self._deaths += 1
                if self._deaths >= self._death_bound:
                    self._backed_off = True
            if self._backed_off:
                self._stamp = stamp
                return None
            if self._child is not None:
                return self._child
            self._stamp = stamp
            try:
                process = self._spawn_child(self._root)
            except (OSError, subprocess.SubprocessError):
                self._child = None
                return None
            if process is None:
                self._child = None
                return None
            self._spawns += 1
            child = ServedChild(process, timeout_s=self._timeout_s)
            child.probe(registered)
            self._child = child
            return child

    @property
    def death_bound(self) -> int:
        """The most consecutive child deaths one stamp may spawn replacements for."""
        return self._death_bound

    @property
    def backed_off(self) -> bool:
        """Whether the server has stopped respawning until the stamp moves."""
        return self._backed_off

    def current_stamp(self) -> str | None:
        """The stamp the live child was started for, or None when there is none."""
        return self._stamp

    def spawns(self) -> int:
        """How many children this server has started, for a caller to count."""
        return self._spawns

    def warm(
        self,
        stamp: str,
        registered: Mapping[str, Mapping[str, bool]] | None = None,
    ) -> dict | None:
        """Start the child for ``stamp`` and return its probe answer if it has
        arrived. Never waits: a cold child answers nothing and serves no call.

        ``registered`` is handed to a freshly started child's probe, so the
        child decides compatibility itself. An already-running child keeps the
        verdict from the request that started it.
        """
        child = self._child_for(stamp, registered)
        if child is None:
            return None
        answer = child.warm_answer()
        return answer if isinstance(answer, dict) else None

    def run(self, stamp: str, tool: str, kwargs: Mapping[str, object]) -> dict | None:
        """Run one tool in the child for ``stamp``; None when it made no answer."""
        child = self._child_for(stamp)
        if child is None:
            return None
        return child.run(tool, kwargs)

    def stop(self) -> None:
        with self._lock:
            if self._child is not None:
                child = self._child
                self._child = None
                self._stamp = None
                child.stop()
