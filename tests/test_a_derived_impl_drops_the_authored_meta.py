"""A derived impl drops an authored plan-impl meta on a full-state write.

A plan that declares its sections derives its impl from those declarations, so
the serialiser emits no ``plan-impl`` meta: a derived figure is the server's and
must never be stored as the authored one. It also rewrites only the names it is
given, so an authored ``plan-impl`` already on disk survives every write and
reads as the plan's figure for good — the audit then reports the file and the
server disagreeing, and no supported write can correct it.

The four full-state writers pass the opt-in that removes it: the store's write
path, its text-replacement path, the plan POST handler, and the tag backfill.
The default serialise is unchanged, so a round-trip or a write that carries no
content change moves no bytes.
"""

from __future__ import annotations

import http.client
import json
import threading
from copy import deepcopy
from pathlib import Path

import pytest

from reckon import _store, doccheck
from reckon._plan_html import read_state, write_state
from reckon.mcp_edit_plan import _edit_plan
from reckon.tags import backfill_tags_from_preimage

CAPABILITY = {
    "version": "1.0",
    "class": "general",
    "requirements": {
        "reasoning": "standard",
        "verification": "strict",
        "risk": "low",
    },
}

#: s1 recorded done and s2 merely declared: the records do not cover every
#: declaration, so the figure is derived from the declarations.
DECLARATIONS = {"s1": "done", "s2": "implementable"}

#: What the two declarations derive from one done record.
DERIVED_IMPL = 0.5

#: An authored figure far enough from the derived one to be reported.
STALE_IMPL = "0.67"


def _record(section_id: str, status: str) -> dict:
    return {
        "id": section_id,
        "effort_hours": 1.0,
        "capability": deepcopy(CAPABILITY),
        "attempts": 0,
        "status": status,
        "links": [],
    }


def _bare(headings: list[tuple[str, str]]) -> str:
    body = "".join(
        f'<h2 id="{sid}">{title}</h2>\n<p>{title} body.</p>\n'
        for sid, title in headings
    )
    return (
        '<!doctype html>\n<html lang="en"><head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="docs-project" content="sample">\n'
        '<meta name="reckon-type" content="plan">\n'
        '</head>\n<body><main class="plan-doc">\n'
        f"{body}"
        "</main></body></html>\n"
    )


def _derived_text(slug: str = "derived", stale_impl: str | None = STALE_IMPL) -> str:
    """A plan whose impl is derived, plus the authored meta it disagrees with.

    The 0.67 line is planted where a serialise that still emitted it would put
    it — immediately before the lifecycle block — so the document is canonical
    apart from that one line and a round-trip is byte-identical.
    """
    text = write_state(
        _bare([("s1", "The s1 work"), ("s2", "The s2 work")]),
        {
            "project": "sample",
            "type": "plan",
            "slug": slug,
            "title": "Derived impl",
            "status": "active",
            "modified": "2026-11-09",
            "version": 1,
            "section_declarations": deepcopy(DECLARATIONS),
            "sections": [_record("s1", "done")],
        },
    )
    if stale_impl is None:
        return text
    marker = '<meta name="plan-version"'
    assert marker in text
    return text.replace(
        marker, f'<meta name="plan-impl" content="{stale_impl}">\n{marker}', 1
    )


def _authored_text(slug: str = "authored") -> str:
    """A plan with no declarations, so its authored impl stands."""
    return write_state(
        _bare([("s1", "The s1 work")]),
        {
            "project": "sample",
            "type": "plan",
            "slug": slug,
            "title": "Authored impl",
            "status": "active",
            "modified": "2026-11-09",
            "version": 1,
            "impl": 0.3,
        },
    )


def _plant(tmp_path: Path, text: str, slug: str, relative: str | None = None) -> Path:
    path = tmp_path / "repo" / "docs" / (relative or f"plans/{slug}.html")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """Point the store at a temp docs tree and return (docs_dir, project)."""
    project = "sample"
    docs_dir = tmp_path / "repo" / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    state_root = tmp_path / "state"
    state_root.mkdir()
    mounts = tmp_path / "mounts.json"
    mounts.write_text(json.dumps({project: str(docs_dir)}), encoding="utf-8")
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    (tmp_path / "config").mkdir()
    return docs_dir, project


# ── the defect the opt-in repairs ───────────────────────────────────────────


def test_the_stale_meta_is_reported_before_any_write(tmp_path: Path) -> None:
    """Reproduction: the file and the server disagree, and nothing corrects it."""
    path = _plant(tmp_path, _derived_text(), "derived")

    state = read_state(path.read_text(encoding="utf-8"))
    assert state["impl_source"] == "declarations"
    assert state["impl"] == pytest.approx(DERIVED_IMPL)

    codes = {finding.code for finding in doccheck.audit_file(path)}
    assert "impl-meta-stale" in codes

    round_tripped = write_state(
        path.read_text(encoding="utf-8"), read_state(path.read_text(encoding="utf-8"))
    )
    assert 'name="plan-impl" content="0.67"' in round_tripped, (
        "the default serialise leaves the authored meta where it found it"
    )


# ── (a) a state write drops it ──────────────────────────────────────────────


def test_a_state_write_drops_the_authored_meta(tmp_path: Path, store) -> None:
    _, project = store
    path = _plant(tmp_path, _derived_text(), "derived")

    _store.patch_plan(project, "derived", {"title": "Renamed"}, expected_version=1)

    text = path.read_text(encoding="utf-8")
    assert 'name="plan-impl"' not in text
    assert read_state(text)["impl"] == pytest.approx(DERIVED_IMPL)
    assert "impl-meta-stale" not in {f.code for f in doccheck.audit_file(path)}


# ── (b) a write that changes nothing moves nothing ──────────────────────────


def test_a_no_change_patch_leaves_the_file_untouched(
    tmp_path: Path, store, monkeypatch
) -> None:
    _, project = store
    text = _derived_text()
    path = _plant(tmp_path, text, "derived")
    before = path.stat()
    writes: list[Path] = []
    real_write = _store.write_atomically

    def spying_write(target, writer, **keywords):
        writes.append(Path(target))
        return real_write(target, writer, **keywords)

    monkeypatch.setattr(_store, "write_atomically", spying_write)
    # A crew run refuses a plan write that would land outside its own worktree,
    # so the refusal would answer for the store and the store would never be
    # asked. The subject here is the store's guard, not the run's.
    monkeypatch.delenv("RECKON_RUN_ID", raising=False)

    response = _edit_plan(
        project,
        "derived",
        [{"op": "set", "path": "status", "value": "active"}],
        expected_version=1,
    )
    # Identical text is what a write of identical content leaves behind, so the
    # text alone cannot tell a skipped write from a performed one. The writer was
    # never reached, and the file it would have replaced is the same file.
    after = path.stat()
    assert path not in writes, (
        "a patch carrying no content change must not write the file"
    )
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ), "the file was replaced under a patch that changed nothing"
    assert path.read_text(encoding="utf-8") == text
    assert response.get("ok") is True, response
    assert response.get("new_version") == 1, response


def test_the_default_serialise_round_trips_byte_for_byte() -> None:
    text = _derived_text()

    assert write_state(text, read_state(text)) == text


# ── (c) an authored impl keeps its meta ─────────────────────────────────────


def test_an_authored_impl_keeps_its_meta_through_a_state_write(
    tmp_path: Path, store
) -> None:
    _, project = store
    path = _plant(tmp_path, _authored_text(), "authored")

    _store.patch_plan(project, "authored", {"title": "Renamed"}, expected_version=1)

    text = path.read_text(encoding="utf-8")
    assert 'name="plan-impl" content="0.3"' in text
    assert read_state(text)["impl_source"] == "authored"


# ── (d) the plan POST handler drops it ──────────────────────────────────────


def test_the_plan_post_handler_drops_the_authored_meta(
    tmp_path: Path, store, monkeypatch
) -> None:
    import reckon.serve as serve_module

    _, project = store
    path = _plant(tmp_path, _derived_text(), "derived")
    monkeypatch.setattr(
        serve_module, "_MOUNTS_FILE", Path(store[0].parent.parent / "mounts.json")
    )
    monkeypatch.setattr(serve_module, "_STATE_ROOT", tmp_path / "state")
    serve_module._DISC_CACHE.clear()

    state = read_state(path.read_text(encoding="utf-8"))

    server = serve_module.ThreadingHTTPServer(("127.0.0.1", 0), serve_module.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=20)
    try:
        connection.request(
            "POST",
            f"/plan/{project}/derived",
            body=json.dumps({"title": "Renamed"}),
            headers={
                "Content-Type": "application/json",
                "If-Match": str(state["version"]),
            },
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
    finally:
        connection.close()
        server.shutdown()

    assert response.status == 200, payload
    assert 'name="plan-impl"' not in path.read_text(encoding="utf-8")


# ── (e) a text replacement drops it ─────────────────────────────────────────


def test_a_text_replacement_drops_the_authored_meta(tmp_path: Path, store) -> None:
    _, project = store
    text = _derived_text()
    path = _plant(tmp_path, text, "derived")

    _store.replace_plan_text(
        project,
        "derived",
        old_html="<p>The s1 work body.</p>",
        new_html="<p>The s1 work body, edited.</p>",
        expected_version=1,
    )

    written = path.read_text(encoding="utf-8")
    assert "edited" in written
    assert 'name="plan-impl"' not in written


# ── (f) a tag backfill drops it ─────────────────────────────────────────────


def test_a_tag_backfill_drops_the_authored_meta(tmp_path: Path) -> None:
    docs = tmp_path / "corpus" / "docs"
    path = docs / "plans" / "Topic" / "derived.html"
    path.parent.mkdir(parents=True)
    path.write_text(_derived_text(), encoding="utf-8")
    preimage = tmp_path / "layout-moves-preimage.txt"
    preimage.write_text(
        "  plans/Topic/derived.html -> plans/derived.html\n", encoding="utf-8"
    )

    backfill_tags_from_preimage(docs, preimage)

    assert path.is_file(), "the backfill rewrites the source in place"
    assert 'name="plan-impl"' not in path.read_text(encoding="utf-8")
