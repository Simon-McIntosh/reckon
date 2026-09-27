"""impl is computed from the section records, and is not authorable on such a plan."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

import reckon._store as store_module
import reckon.mcp as mcp_module
import reckon.serve as serve_module
from reckon._plan_html import parse_meta, parse_plan, read_state

REAL_CONFIG_HOME = Path.home() / ".config" / "reckon"

DONE_4H = {"id": "first", "effort_hours": 4, "status": "done"}
IMPLEMENTABLE_6H = {"id": "second", "effort_hours": 6, "status": "implementable"}
IMPLEMENTABLE_10H = {"id": "third", "effort_hours": 10, "status": "implementable"}


def _config_home_entries() -> list[tuple[str, int]]:
    if not REAL_CONFIG_HOME.is_dir():
        return []
    return sorted(
        (entry.name, entry.stat().st_mtime_ns) for entry in REAL_CONFIG_HOME.iterdir()
    )


def _records_html(records: list[dict], stored_impl: str | None = None) -> str:
    declarations = json.dumps({row["id"]: row["status"] for row in records})
    body = []
    for record in records:
        body.append(f'<h2 id="{record["id"]}">The {record["id"]} work</h2>')
        body.append(
            f'<section data-reckon="section" data-id="{record["id"]}"'
            f' data-effort-hours="{record["effort_hours"]}"'
            ' data-capability-version="1.0" data-capability-class="general"'
            ' data-capability-reasoning="standard"'
            ' data-capability-verification="standard" data-capability-risk="low"'
            f' data-attempts="0" data-status="{record["status"]}" data-links=""></section>'
        )
    impl_meta = (
        f'<meta name="plan-impl" content="{stored_impl}">\n' if stored_impl else ""
    )
    return (
        '<!doctype html>\n<html lang="en"><head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="docs-project" content="sample">\n'
        '<meta name="reckon-type" content="plan">\n'
        '<meta name="plan-slug" content="computed">\n'
        '<meta name="plan-title" content="Computed impl">\n'
        f"{impl_meta}"
        f'<meta name="plan-section-declarations" content="{declarations}">\n'
        "</head><body><main>\n" + "\n".join(body) + "\n</main></body></html>\n"
    )


def _write_plan(docs: Path, slug: str, html: str) -> Path:
    path = docs / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return path


@pytest.fixture()
def project(tmp_path, monkeypatch):
    """A mounted project under a temporary config home, with the real one guarded."""
    before = _config_home_entries()
    home = tmp_path / "config"
    (home / "state").mkdir(parents=True)
    docs = tmp_path / "repo" / "docs"
    (docs / "plans").mkdir(parents=True)
    (home / "mounts.json").write_text(json.dumps({"sample": str(docs)}))
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(home / "state"))
    # A live run id would fence every write to that run's own checkout; a unit
    # test writes into its temporary project instead.
    monkeypatch.delenv("RECKON_RUN_ID", raising=False)
    serve_module._MOUNTS_FILE = home / "mounts.json"
    serve_module._STATE_ROOT = home / "state"
    serve_module._DISC_CACHE.clear()
    importlib.reload(store_module)
    importlib.reload(mcp_module)
    yield "sample", docs
    assert store_module._config_home() == home
    assert _config_home_entries() == before


def test_state_read_derives_impl_from_records_over_the_stored_meta(tmp_path):
    html = _records_html([DONE_4H, IMPLEMENTABLE_6H], stored_impl="0.9")
    path = _write_plan(tmp_path, "computed", html)

    state = read_state(html)
    assert state["impl"] == pytest.approx(0.4)
    assert state["impl_source"] == "computed"
    assert parse_plan(path)["impl"] == pytest.approx(0.4)
    assert parse_meta(path)["impl"] == pytest.approx(0.4)
    assert path.read_text(encoding="utf-8") == html


def test_adding_an_implementable_section_moves_impl_with_no_other_write(tmp_path):
    two = _records_html([DONE_4H, IMPLEMENTABLE_6H], stored_impl="0.9")
    three = _records_html(
        [DONE_4H, IMPLEMENTABLE_6H, IMPLEMENTABLE_10H], stored_impl="0.9"
    )
    assert read_state(two)["impl"] == pytest.approx(0.4)
    assert read_state(three)["impl"] == pytest.approx(0.2)
    path = _write_plan(tmp_path, "three", three)
    assert parse_meta(path)["impl"] == pytest.approx(0.2)
    # The stored figure on disk never moved; only the records did.
    assert 'name="plan-impl" content="0.9"' in path.read_text(encoding="utf-8")


def test_deferred_effort_sits_outside_the_denominator():
    records = [
        DONE_4H,
        IMPLEMENTABLE_6H,
        {"id": "later", "effort_hours": 90, "status": "deferred"},
    ]
    state = read_state(_records_html(records, stored_impl="0.9"))
    assert state["impl"] == pytest.approx(0.4)


def test_a_record_element_carrying_authored_prose_yields_no_computed_figure(tmp_path):
    """The fast path must refuse exactly what the parsed path refuses.

    A record element carrying authored prose is refused by the parsed read, so
    no reader may derive a figure from it: the fast path has to fall back to
    the authored value rather than report one the parser's own read denies.
    """
    html = _records_html([DONE_4H], stored_impl="0.9").replace(
        'data-links=""></section>', 'data-links="">Authored prose.</section>'
    )
    path = _write_plan(tmp_path, "computed", html)

    with pytest.raises(ValueError, match="must not contain authored prose"):
        read_state(html)
    with pytest.raises(ValueError, match="must not contain authored prose"):
        parse_plan(path)

    meta = parse_meta(path)
    assert meta.get("impl_source") != "computed"
    assert meta["impl"] == pytest.approx(0.9)


def test_state_mode_set_impl_is_refused_naming_the_computed_source(project):
    project_name, docs = project
    html = _records_html([DONE_4H, IMPLEMENTABLE_6H], stored_impl="0.9")
    path = _write_plan(docs, "computed", html)

    refusal = mcp_module._edit_plan_tool(
        project_name,
        "computed",
        expected_version=0,
        mode="state",
        ops=[{"op": "set", "path": "impl", "value": 0.7}],
    )

    assert refusal["ok"] is False
    assert refusal["error"] == "op_error"
    assert "computed from the section records" in refusal["detail"]
    assert "not authorable" in refusal["detail"]
    assert path.read_text(encoding="utf-8") == html


def test_plan_without_section_records_still_reads_its_authored_impl(tmp_path):
    html = _records_html([], stored_impl="0.9")
    path = _write_plan(tmp_path, "computed", html)

    state = read_state(html)
    assert state["impl"] == pytest.approx(0.9)
    assert state["impl_source"] == "authored"
    assert parse_plan(path)["impl"] == pytest.approx(0.9)
    assert parse_meta(path)["impl"] == pytest.approx(0.9)


def _documenting_prose_html(stored_impl: str) -> str:
    """A plan that quotes the record syntax and carries no record element.

    The quoted markup lives inside ``<code>``, so it is text a reader sees
    rather than a record the parser reads.
    """
    return (
        '<!doctype html>\n<html lang="en"><head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="docs-project" content="sample">\n'
        '<meta name="reckon-type" content="plan">\n'
        '<meta name="plan-slug" content="documenting">\n'
        '<meta name="plan-title" content="Documenting the record syntax">\n'
        '<meta name="plan-version" content="4">\n'
        f'<meta name="plan-impl" content="{stored_impl}">\n'
        "</head><body><main>\n"
        '<h2 id="contract">The record contract</h2>\n'
        "<p>A section record is written "
        '<code>&lt;section data-reckon="section" data-id="first" '
        'data-effort-hours="4" data-status="done"&gt;&lt;/section&gt;</code> '
        "beside its heading, so the id, the effort and the status are carried "
        "by the section element itself.</p>\n"
        "</main></body></html>\n"
    )


def test_a_plan_documenting_the_syntax_keeps_its_impl_writable(project):
    """A document quoting the record syntax does not hold its impl as records.

    The write gate asks the parsed state, not a text match: this plan names
    ``data-reckon="section"`` and the record attributes inside its own prose and
    carries no record element, so its impl stays authored and a set on it must
    land on disk and read back.
    """
    project_name, docs = project
    html = _documenting_prose_html("0.6")
    path = _write_plan(docs, "documenting", html)

    state = read_state(html)
    assert state["impl"] == pytest.approx(0.6)
    assert state["impl_source"] == "authored"
    assert parse_meta(path)["impl"] == pytest.approx(0.6)

    result = mcp_module._edit_plan_tool(
        project_name,
        "documenting",
        expected_version=4,
        mode="state",
        ops=[{"op": "set", "path": "impl", "value": 0.7}],
    )

    assert result["ok"] is True, result
    written = path.read_text(encoding="utf-8")
    assert written != html
    assert parse_meta(path)["impl"] == pytest.approx(0.7)
    assert read_state(written)["impl"] == pytest.approx(0.7)


def _impl_figures(node, slug: str, found: list[dict]) -> None:
    if isinstance(node, dict):
        if node.get("slug") == slug:
            found.append(node)
        for value in node.values():
            _impl_figures(value, slug, found)
    elif isinstance(node, list):
        for value in node:
            _impl_figures(value, slug, found)


def test_read_plan_and_roadmap_report_the_same_computed_impl(project):
    project_name, docs = project
    _write_plan(
        docs, "computed", _records_html([DONE_4H, IMPLEMENTABLE_6H], stored_impl="0.9")
    )

    read = mcp_module._read_plan(
        resource={"project": project_name, "type": "plan", "id": "computed"},
        view="raw",
    )
    assert read.get("ok") is not False, read
    assert read["data"]["impl"] == pytest.approx(0.4)

    raw = mcp_module._roadmap(project_name, view="raw")["data"]
    rows: list[dict] = []
    _impl_figures(raw, "computed", rows)
    progress = [
        row["progress_pct"] for row in rows if row.get("progress_pct") is not None
    ]
    assert progress, "roadmap reported no figure for the fixture plan"
    assert set(progress) == {40.0}
    assert read["data"]["impl"] * 100 == pytest.approx(progress[0])
