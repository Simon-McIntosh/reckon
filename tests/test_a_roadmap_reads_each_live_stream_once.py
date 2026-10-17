"""A roadmap read parses the project watch stream once, and only its new bytes.

Both readers of a project's watch stream — the sprint liveness derivation and
the live-run classification — need the newest transition recorded per run.
Parsing the whole stream per reader per read is the cost this case bounds: with
a stream of ten thousand lines a single read must parse each line at most once,
however many readers consult it.

The stream is append-only and only grows, so a long-lived process keeps the byte
offset it parsed to and parses only the bytes appended since. A stream that was
truncated or replaced is parsed again from the start, because everything before
its old offset no longer describes it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import reckon.serve as serve_module
from reckon import metadata_index
from reckon._plan_html import write_state
from reckon.crew import runs
from tests.mcp_family_reload import reload_mcp_family

# A project name no test elsewhere uses, so the shared watch-stream memory and
# the live-pointer readers cannot pick up a peer's state.
PROJECT = "watch-stream-once-sample"
STREAM_LINES = 10_000


@pytest.fixture()
def mounted(tmp_path, monkeypatch):
    project = PROJECT
    docs = tmp_path / "repo" / "docs"
    docs.mkdir(parents=True)
    state_root = tmp_path / "state"
    state_root.mkdir()
    mounts = tmp_path / "mounts.json"
    mounts.write_text(json.dumps({project: str(docs)}))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config-home"))
    serve_module._MOUNTS_FILE = mounts
    serve_module._STATE_ROOT = state_root
    serve_module._DISC_CACHE.clear()
    serve_module._SIGNATURE_MEMO.clear()
    metadata_index.clear()
    # A fresh memory, so the first read of this project parses from the start.
    getattr(runs, "_WATCH_STREAM_MEMORY", {}).clear()
    reload_mcp_family()
    return project, docs


@pytest.fixture()
def parse_calls(monkeypatch) -> list[int]:
    """Count every parse_stream_line call for the duration of a test.

    The shared reader and the from-scratch reference both parse through this
    same name, so a counter here sees every parse either one performs.
    """
    count = [0]
    original = runs.parse_stream_line

    def counting(line: str):
        count[0] += 1
        return original(line)

    monkeypatch.setattr(runs, "parse_stream_line", counting)
    return count


def _write_plan(docs: Path, slug: str) -> Path:
    bare = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{slug}</title></head>"
        '<body><main class="plan-doc"></main></body></html>'
    )
    path = docs / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        write_state(bare, {"slug": slug, "title": slug, "status": "active"}),
        encoding="utf-8",
    )
    return path


def _stream_events(count: int, *, first: int = 0):
    for index in range(count):
        yield {
            "event": "transition",
            "project": PROJECT,
            "run_id": f"run-{first + index}",
            "session": "s-watch",
            "from_state": "starting",
            "to_state": "working",
            "recovery_classification": "working",
            "detail": "",
            "observed_at": "2026-10-17T00:00:00Z",
        }


def _write_stream(count: int, *, first: int = 0, append: bool = False) -> None:
    path = runs.watch_stream_path(PROJECT)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(
        f"{json.dumps(event, sort_keys=True)}\n"
        for event in _stream_events(count, first=first)
    )
    with path.open("a" if append else "w", encoding="utf-8") as handle:
        handle.write(payload)


def _read(project: str) -> dict:
    import reckon.mcp as mcp_module

    return mcp_module._roadmap(project, view=None)


def _unshared_latest(project: str) -> dict:
    """The newest classified event per run, parsed from scratch every call.

    This is the reference the shared reader must match: no memory, no offset, a
    whole parse of the stream on every call.
    """
    latest: dict[str, dict] = {}
    for event in runs.read_stream_events(runs.watch_stream_path(project)):
        if event.get("legacy"):
            continue
        run_id = str(event.get("run_id") or "")
        classification = str(
            event.get("recovery_classification") or event.get("to_state") or ""
        )
        if run_id and classification:
            latest[run_id] = event
    return latest


def test_a_roadmap_read_parses_the_watch_stream_once_at_most(
    mounted, parse_calls
) -> None:
    """One read parses at most one line per stream line, then only appends."""
    project, docs = mounted
    _write_plan(docs, "alpha")
    _write_stream(STREAM_LINES)

    # First read: a fresh memory, so each line is parsed once and no more.
    parse_calls[0] = 0
    first = _read(project)
    first_calls = parse_calls[0]
    assert first["completion"]["plans"] == 1
    assert first_calls <= STREAM_LINES, (
        f"one read parsed {first_calls} lines of a {STREAM_LINES}-line stream"
    )

    shared = getattr(runs, "watch_stream_latest_events", None)
    if shared is not None:
        # Every read returns what an unshared read returns.
        parse_calls[0] = 0
        reference = _unshared_latest(project)
        assert shared(project) == reference

    # Second read: ten appended lines are the ten parsed, none of the old ones.
    _write_stream(10, first=STREAM_LINES, append=True)
    parse_calls[0] = 0
    _read(project)
    assert parse_calls[0] == 10, parse_calls[0]
    if shared is not None:
        parse_calls[0] = 0
        assert shared(project) == _unshared_latest(project)

    # Third read: a truncated stream is parsed again from the start.
    _write_stream(5, first=99)
    parse_calls[0] = 0
    _read(project)
    assert parse_calls[0] == 5, parse_calls[0]
    if shared is not None:
        parse_calls[0] = 0
        assert shared(project) == _unshared_latest(project)
        assert len(shared(project)) == 5
