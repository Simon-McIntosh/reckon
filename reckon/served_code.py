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
import copy
import hashlib
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterable, Mapping
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
_REPORTS: dict[int, tuple[float, dict]] = {}
_LOCK = threading.Lock()


def forget_reports() -> None:
    """Drop every reused report, hash and parse, as a fresh process holds none."""

    with _LOCK:
        _REPORTS.clear()
        _HASH_MEMO.clear()
        _INDEX_MEMO.clear()


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
