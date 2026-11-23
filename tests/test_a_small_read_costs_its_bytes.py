"""A small read of a large file costs its bytes, not a filesystem block.

A default binary reader fills one ``st_blksize`` block before it returns the
first byte, and on GPFS that block is 4 MiB. A reader that wants the tail of a
default binary reader fills one ``st_blksize`` block before it returns the
first byte, and on GPFS that block is 4 MiB. A reader that wants the tail of a
worker's stream, a lock's record, or a bounded probe therefore paid 4 MiB per
open however few bytes it consumed.

This module finds those reads itself rather than trusting a list: it parses the
modules under ``reckon/crew/``, ``reckon/run_store.py`` and
``reckon/ledger.py``, and every binary read-capable open whose handle performs a
bounded read or a read after a seek must be unbuffered, or read positionally.
It also measures the behaviour: the instrument below simulates a file whose
filesystem reports a 4 MiB block, and the cases show that a buffered bounded
read asks the raw file for the whole block while the same read on an unbuffered
open asks for its own bytes.
"""

from __future__ import annotations

import ast
import contextlib
import io
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCAN_ROOT = REPO / "reckon" / "crew"
SCAN_FILES = (REPO / "reckon" / "run_store.py", REPO / "reckon" / "ledger.py")

PAGE = 4096
BLOCK = 4 * 1024 * 1024
TAIL_CHUNK = 64 * 1024

# A function called on a file handle with one of these names is a read or a
# reposition, which is what makes a write-only open out of scope.
READ_ATTRS = {"read", "readline", "readlines"}

# Opens through these modules are not plain files.
NON_FILE_OPENERS = {"tarfile", "zipfile", "gzip", "bz2", "lzma", "socket", "opener"}


@dataclass(frozen=True)
class OpenSite:
    """One binary read site: where it is, and how it opens its file."""

    path: Path
    line: int
    function: str
    mode: str
    buffering: int | None
    handle: str | None
    bounded: bool
    function_reads: bool
    seeks: bool
    passed_to: str | None

    def where(self) -> str:
        return f"{self.path.relative_to(REPO)}:{self.line} in {self.function}()"


def _scan_files() -> list[Path]:
    files = sorted(SCAN_ROOT.rglob("*.py"))
    files.extend(path for path in SCAN_FILES if path.is_file())
    return files


def _call_name(node: ast.Call) -> str | None:
    function = node.func
    if isinstance(function, ast.Name):
        return function.id
    if isinstance(function, ast.Attribute):
        return function.attr
    return None


def _enclosing(line: int, functions: list[ast.AST]) -> ast.AST | None:
    best: ast.AST | None = None
    for function in functions:
        end = getattr(function, "end_lineno", None) or function.lineno
        if function.lineno <= line <= end and (
            best is None or function.lineno > best.lineno
        ):
            best = function
    return best


def _open_mode(node: ast.Call) -> str | None:
    """The literal mode of an ``open`` call, or None when it is not literal."""
    if _call_name(node) != "open":
        return None
    if isinstance(node.func, ast.Attribute):
        owner = node.func.value
        if isinstance(owner, ast.Name) and owner.id in NON_FILE_OPENERS:
            return None
        candidates = node.args[:1]
    else:
        candidates = node.args[1:2]
    for keyword in node.keywords:
        if keyword.arg == "mode":
            candidates = (keyword.value,)
    for candidate in candidates:
        if isinstance(candidate, ast.Constant) and isinstance(candidate.value, str):
            return candidate.value
    return None


def _open_buffering(node: ast.Call) -> int | None:
    """The literal buffering argument, or None for the default."""
    candidates: list[ast.expr] = []
    if not isinstance(node.func, ast.Attribute) and len(node.args) >= 3:
        candidates.append(node.args[2])
    for keyword in node.keywords:
        if keyword.arg == "buffering":
            candidates.insert(0, keyword.value)
    for candidate in candidates:
        if isinstance(candidate, ast.Constant) and isinstance(candidate.value, int):
            return candidate.value
    return None


def _bound_name(node: ast.Call, tree: ast.AST) -> str | None:
    for candidate in ast.walk(tree):
        if isinstance(candidate, ast.With):
            for item in candidate.items:
                if item.context_expr is node and item.optional_vars is not None:
                    return ast.unparse(item.optional_vars)
        if isinstance(candidate, ast.Assign) and candidate.value is node:
            return ast.unparse(candidate.targets[0])
    return None


def _handle_calls(tree: ast.AST, handle: str) -> list[ast.Call]:
    base = handle.split(".", maxsplit=1)[0].split("[", maxsplit=1)[0]
    calls: list[ast.Call] = []
    for candidate in ast.walk(tree):
        if not isinstance(candidate, ast.Call):
            continue
        function = candidate.func
        if not isinstance(function, ast.Attribute):
            continue
        if isinstance(function.value, ast.Name) and function.value.id == base:
            calls.append(candidate)
    return calls


def _is_bounded_call(call: ast.Call) -> bool:
    name = _call_name(call)
    if name == "read":
        return bool(call.args)
    return name in {"readline", "readlines"}


def _passed_argument(scope: ast.AST, handle: str) -> str | None:
    base = handle.split(".", maxsplit=1)[0].split("[", maxsplit=1)[0]
    for candidate in ast.walk(scope):
        if not isinstance(candidate, ast.Call):
            continue
        arguments = list(candidate.args) + [kw.value for kw in candidate.keywords]
        for argument in arguments:
            text = ast.unparse(argument)
            name = _call_name(candidate)
            if name and (text == handle or text.split(".")[0] == base):
                return name
    return None


def discover() -> list[OpenSite]:
    """Every binary read-capable open in the scanned modules, with its usage."""
    sites: list[OpenSite] = []
    for path in _scan_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        functions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            mode = _open_mode(node)
            if not mode or "b" not in mode:
                continue
            if "r" not in mode and "+" not in mode:
                continue
            scope = _enclosing(node.lineno, functions) or tree
            handle = _bound_name(node, tree)
            calls = _handle_calls(scope, handle) if handle else []
            names = {_call_name(call) for call in calls}
            sites.append(
                OpenSite(
                    path=path,
                    line=node.lineno,
                    function=getattr(
                        _enclosing(node.lineno, functions), "name", "<module>"
                    ),
                    mode=mode,
                    buffering=_open_buffering(node),
                    handle=handle,
                    bounded=any(_is_bounded_call(call) for call in calls),
                    function_reads=bool(names & READ_ATTRS),
                    seeks="seek" in names,
                    passed_to=_passed_argument(scope, handle) if handle else None,
                )
            )
    return sites


def in_scope() -> list[OpenSite]:
    """Sites whose read is bounded, or happens after a seek.

    A site is in scope only when the handle is read at all: an open that seeks
    and then writes is a write path, and the block buffer it may carry costs no
    read.
    """
    return [
        site
        for site in discover()
        if site.bounded or (site.seeks and site.function_reads)
    ]


def reader_helpers() -> dict[str, bool]:
    """Functions that read a parameter, and whether they read positionally."""
    helpers: dict[str, bool] = {}
    for path in _scan_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            parameters = {
                argument.arg for argument in node.args.args + node.args.kwonlyargs
            }
            reads = False
            positional = False
            for candidate in ast.walk(node):
                if not isinstance(candidate, ast.Call):
                    continue
                name = _call_name(candidate)
                function = candidate.func
                if (
                    isinstance(function, ast.Attribute)
                    and isinstance(function.value, ast.Name)
                    and function.value.id in parameters
                    and (name in READ_ATTRS or name == "seek")
                ):
                    reads = True
                if name in {"pread", "preadv"}:
                    positional = True
                    reads = True
            if reads:
                helpers[node.name] = positional
    return helpers


@contextlib.contextmanager
def four_mib_block_opens(monkeypatch: pytest.MonkeyPatch, log: list[int]):
    """Make binary ``Path.open`` behave like a file on a 4 MiB-block filesystem.

    A default buffered open gets a reader whose block size is 4 MiB and whose
    raw reads are recorded; an unbuffered open returns a raw file whose reads
    are recorded directly, at exactly the size asked for.
    """

    class TracingFileIO(io.FileIO):
        def read(self, size: int = -1) -> bytes:
            log.append(size)
            return super().read(size)

        def readline(self, size: int = -1) -> bytes:
            log.append(size)
            return super().readline(size)

        def readinto(self, buffer) -> int:
            log.append(len(buffer))
            return super().readinto(buffer)

    real_open = Path.open

    def fake_open(self: Path, mode: str = "r", buffering: int = -1, *args, **kwargs):
        if "b" in mode and ("r" in mode or "+" in mode):
            raw_mode = mode.replace("b", "")
            if buffering == 0:
                return TracingFileIO(self, raw_mode)
            return io.BufferedReader(TracingFileIO(self, raw_mode), buffer_size=BLOCK)
        return real_open(self, mode, buffering, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fake_open)
    yield


def _big_stream(path: Path) -> Path:
    row = b'{"type": "transition", "run_id": "r-1", "pad": "' + b"x" * 64 + b'"}\n'
    with path.open("wb") as stream:
        for _ in range(150_000):
            stream.write(row)
    return path


def test_a_tail_read_of_a_stream_costs_its_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tail read must not fill a whole filesystem block."""
    from reckon.crew import runs

    stream = _big_stream(tmp_path / "stream.events")
    log: list[int] = []
    with four_mib_block_opens(monkeypatch, log):
        boundary = runs.line_boundary(stream, chunk=TAIL_CHUNK)
    assert log, "the instrument saw no read at all"
    assert boundary > 0, "the tail scan found no line"
    oversized = [size for size in log if size > TAIL_CHUNK + PAGE]
    assert not oversized, (
        "a bounded tail read asked the file for "
        f"{oversized} bytes, up to the {BLOCK}-byte block size"
    )


def test_a_bounded_range_read_costs_its_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The station store's range read asks for its limit, not a block."""
    from reckon.crew import station_store

    store = tmp_path / "store.log"
    store.write_bytes(b"y" * (2 * BLOCK))
    log: list[int] = []
    with four_mib_block_opens(monkeypatch, log):
        payload = station_store._read_range(store, BLOCK + 7, 64)
    assert payload == b"y" * 64
    assert log, "the instrument saw no read at all"
    assert max(log) <= 64 + PAGE, f"the range read asked for {max(log)} bytes"


def test_the_instrument_sees_a_buffered_small_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control: the same read on a default buffered open costs a block."""
    target = tmp_path / "large"
    target.write_bytes(b"z" * (BLOCK + PAGE))
    buffered: list[int] = []
    with four_mib_block_opens(monkeypatch, buffered), target.open("rb") as stream:
        stream.read(28)
    unbuffered: list[int] = []
    with (
        four_mib_block_opens(monkeypatch, unbuffered),
        target.open("rb", buffering=0) as stream,
    ):
        stream.read(28)
    assert buffered[-1] == BLOCK, (
        f"the instrument did not reproduce a 4 MiB read: {buffered}"
    )
    assert unbuffered[-1] == 28, f"an unbuffered read asked for {unbuffered}"


def test_every_bounded_or_seek_read_is_unbuffered() -> None:
    """The scan finds the reads itself; each one passes or is named."""
    helpers = reader_helpers()
    sites = in_scope()
    assert len(sites) >= 12, (
        f"the scan found too few reads: {[site.where() for site in sites]}"
    )
    known = {(str(site.path.relative_to(REPO)), site.function) for site in sites}
    for expected in (
        ("reckon/crew/runs.py", "line_boundary"),
        ("reckon/crew/runs.py", "_iter_stream_lines_backwards"),
        ("reckon/crew/station_store.py", "_read_range"),
        ("reckon/crew/directory.py", "_last_assistant_request"),
        ("reckon/crew/recovery_wait.py", "_appended_lines"),
        ("reckon/crew/paid_lanes.py", "_read_rollout_event"),
        ("reckon/run_store.py", "store_stamp"),
    ):
        assert expected in known, f"the scan did not see {expected}"

    offenders: list[str] = []
    for site in sites:
        if site.buffering == 0:
            continue
        if site.passed_to is not None and helpers.get(site.passed_to):
            continue
        offenders.append(f"{site.where()} mode={site.mode} buffering={site.buffering}")
    assert not offenders, (
        "a bounded or post-seek binary read opens buffered, so it fills a "
        f"filesystem block before its bytes: {offenders}"
    )


def test_the_shared_lock_record_read_is_positional() -> None:
    """A helper that reads a handle must read it positionally, or its open is
    unbuffered; the shared lock-record reader is the case that cannot be fixed
    by its callers, because a lock handle is opened by many of them."""
    helpers = reader_helpers()
    assert "_read_watch_record" in helpers, (
        f"the shared lock-record reader was not found: {sorted(helpers)}"
    )
    assert helpers["_read_watch_record"] is True, (
        "the shared lock-record reader reads its handle through the block "
        "buffer, so every lock holder pays a filesystem block"
    )
