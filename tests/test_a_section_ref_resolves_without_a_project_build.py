"""A section ref resolves its target plan without building the project index.

A plan read that serves its section-scoped refs answers every ref from the
project's metadata index, and building that index on a cold store parses every
covered document. Each ref therefore resolves from the persisted index when it
covers the dependency, and a dependency the store does not cover is parsed from
its own document, so the read parses at most the plan being read and the plans
its refs name.

The count is taken through a patch on the document parser, ``read_state``, and
counts *documents* rather than calls, because a document reached twice is
memoised. ``RECKON_TEST_REVERT_SECTION_REF_INDEX`` restores the base revision's
whole-project index read for the refs; the first case then parses every plan in
the store and fails, which is this node's negative control.
"""

from __future__ import annotations

import json
import os
import re
from html import escape
from pathlib import Path
from typing import Any, Self

import pytest

import reckon._plan_html as plan_html
import reckon.mcp as mcp_module
from reckon import metadata_index
from tests.mcp_family_reload import reload_mcp_family

PROJECT = "alpha"
OWNER = "owner"
TARGETS = ("first-target", "second-target", "third-target")
FILLERS = tuple(f"filler-{index:02d}" for index in range(16))
REVERT_ENV = "RECKON_TEST_REVERT_SECTION_REF_INDEX"

#: Three sections of the owner, each waiting on one of the three targets.
WAITS = {
    "s1": [f"{TARGETS[0]}#s1"],
    "s2": [f"{TARGETS[1]}#s2"],
    "s3": [f"{TARGETS[2]}#s1"],
}

_SLUG_IN_TEXT = re.compile(r'name="plan-slug"\s+content="([^"]+)"')


def _plan_meta(name: str, value: str) -> str:
    return f'<meta name="{name}" content="{escape(value, quote=True)}">'


def _write_plan(
    docs: Path,
    slug: str,
    *,
    sections: dict[str, str],
    section_depends_on: dict[str, list[str]] | None = None,
) -> Path:
    """One active plan whose sections carry the given declarations."""

    head = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8">',
        f'<meta name="docs-project" content="{PROJECT}">',
        '<meta name="reckon-type" content="plan">',
        _plan_meta("plan-slug", slug),
        _plan_meta("plan-title", slug),
        '<meta name="plan-status" content="active">',
        '<meta name="plan-effort" content="M">',
        _plan_meta(
            "plan-section-declarations",
            json.dumps(sections, separators=(",", ":")),
        ),
    ]
    if section_depends_on:
        head.append(
            _plan_meta(
                "plan-section-depends-on",
                json.dumps(section_depends_on, separators=(",", ":")),
            )
        )
    head.append(
        f'<meta name="plan-followups" content="[]"><title>{slug}</title></head>'
    )
    body = ['<body><main class="plan-doc">']
    for name, declaration in sorted(sections.items()):
        body.append(
            f'<h2 id="{name}">Section</h2>'
            '<section data-reckon="section"'
            f' data-id="{name}"'
            ' data-effort-hours="2"'
            ' data-capability-version="1.0" data-capability-class="general"'
            ' data-capability-reasoning="standard"'
            ' data-capability-verification="standard"'
            ' data-capability-risk="low"'
            f' data-status="{declaration}" data-links=""></section>'
        )
    body.append("</main></body></html>")
    path = docs / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(head) + "".join(body), encoding="utf-8")
    return path


@pytest.fixture()
def cold_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Twenty plans in one project and a persisted index that does not exist.

    The store directory is a temporary config home, so the persisted index
    starts absent: a read that wants the project's index must build it, and
    that build is what parses every document.
    """

    docs = tmp_path / "repo" / "docs"
    docs.mkdir(parents=True)
    state_root = tmp_path / "state"
    state_root.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    mounts = tmp_path / "mounts.json"
    mounts.write_text(json.dumps({PROJECT: str(docs)}), encoding="utf-8")

    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))
    monkeypatch.setenv("RECKON_HOME", str(home))

    import reckon.serve as serve_module

    serve_module._MOUNTS_FILE = mounts
    serve_module._STATE_ROOT = state_root
    serve_module._DISC_CACHE.clear()
    metadata_index.clear()
    reload_mcp_family()

    for slug in TARGETS:
        _write_plan(docs, slug, sections={"s1": "done", "s2": "implementable"})
    for slug in FILLERS:
        _write_plan(docs, slug, sections={"s1": "implementable"})
    _write_plan(
        docs,
        OWNER,
        sections=dict.fromkeys(WAITS, "implementable"),
        section_depends_on=WAITS,
    )
    yield docs
    metadata_index.clear()


class _DocumentParseCounter:
    """Count the distinct documents parsed through ``read_state``.

    A document parsed more than once is memoised, so the assertions use
    ``parsed`` — the set of parsed documents — because a read's cost is the
    set of documents it parses, not the number of calls it makes.
    """

    def __enter__(self) -> Self:
        self.documents: list[str] = []
        self.calls = 0
        self._real = plan_html.read_state

        def counting(html_text: str) -> dict:
            self.calls += 1
            match = _SLUG_IN_TEXT.search(html_text or "")
            self.documents.append(match.group(1) if match else "")
            return self._real(html_text)

        plan_html.read_state = counting  # type: ignore[assignment]
        return self

    def __exit__(self, *exc: object) -> None:
        plan_html.read_state = self._real  # type: ignore[assignment]

    @property
    def parsed(self) -> set[str]:
        return {slug for slug in self.documents if slug}


def _base_indexed_plan_states(
    project: str, checkout_path: str | None
) -> dict[str, dict[str, Any]]:
    """The base revision's ref lookup: build the project index, then map slugs.

    Reproduced here rather than called, because the negative control has to
    restore the behaviour this node removed. It builds the whole index on a
    cold store, which is the cost the first case measures.
    """

    docs_dir = mcp_module._docs_dir_for_project(project, checkout_path)
    if docs_dir is None:
        return {}
    live: dict[str, dict[str, Any]] = {}
    archived: dict[str, dict[str, Any]] = {}
    for entry in metadata_index.discovery_state(docs_dir, project).values():
        if not isinstance(entry, dict):
            continue
        state = entry.get("state")
        meta = entry.get("meta")
        if not isinstance(state, dict) or not isinstance(meta, dict):
            continue
        slug = str(meta.get("slug") or state.get("slug") or "")
        if not slug:
            continue
        bucket = archived if meta.get("archived") else live
        bucket.setdefault(slug, dict(state))
    return {**archived, **live}


def _section_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in result.get("deps", []) if row.get("source_section")]


def _detail_read() -> dict[str, Any]:
    return mcp_module._read_plan(PROJECT, OWNER, view="detail")


def _ref_rows_read() -> dict[str, Any]:
    """The read whose ``deps`` are the section-scoped dependency rows.

    The detail view composes this read for its data and deps, then reports the
    blocked subset as ``section_blocking``; the rows themselves are here.
    """

    return mcp_module._read_plan(PROJECT, OWNER)


def test_a_section_ref_read_parses_only_the_plans_it_names(cold_store: Path) -> None:
    if os.environ.get(REVERT_ENV) == "1":
        # The negative control: the base revision's lookup builds the index,
        # so the same assertions see every plan in the store parsed.
        real = mcp_module._indexed_plan_states
        mcp_module._indexed_plan_states = _base_indexed_plan_states  # type: ignore[assignment]
        try:
            _assert_named_plans_only()
        finally:
            mcp_module._indexed_plan_states = real  # type: ignore[assignment]
        return
    _assert_named_plans_only()


def _assert_named_plans_only() -> None:
    with _DocumentParseCounter() as parses:
        detail = _detail_read()
        rows_result = _ref_rows_read()

    parsed = parses.parsed
    named = {OWNER, *TARGETS}
    # The owner is parsed by its own read, and the three refs resolve from
    # their own documents; nothing else in the twenty-plan store is touched.
    assert OWNER in parsed, f"the read parsed no plan at all: {parses.documents}"
    assert parsed <= named, (
        f"a section ref read parsed plans it does not name: {sorted(parsed - named)}"
    )
    # The upper bound is the deliverable; the lower bound is the control that
    # every ref actually resolved, so a read that skipped the refs cannot pass
    # by parsing nothing.
    assert len(parsed) <= 4, sorted(parsed)
    assert set(TARGETS) <= parsed, sorted(parsed)

    rows = _section_rows(rows_result)
    assert len(rows) == 3, rows
    expected_refs = {ref for refs in WAITS.values() for ref in refs}
    assert {row["ref"] for row in rows} == expected_refs
    assert {row["source_section"] for row in rows} == set(WAITS)
    assert {row["status"] for row in rows} == {"done", "implementable"}

    # The detail view reports the blocked subset of those same rows, so the
    # refs reached the view this node's bound is stated for.
    blocking = detail.get("section_blocking") or []
    assert [entry["section"] for entry in blocking] == ["s2"], blocking
    assert blocking[0]["waits_on"][0]["ref"] == f"{TARGETS[1]}#s2"


def test_dependency_rows_equal_a_full_index_build(cold_store: Path) -> None:
    cold_rows = _section_rows(_ref_rows_read())

    metadata_index.clear()
    rows = metadata_index.index_rows(cold_store, PROJECT, revalidate=True)
    assert len(rows) == 20, len(rows)

    warm_rows = _section_rows(_ref_rows_read())
    assert warm_rows == cold_rows, (cold_rows, warm_rows)


def test_a_ref_absent_from_the_persisted_index_resolves_by_one_parse(
    cold_store: Path,
) -> None:
    metadata_index.index_rows(cold_store, PROJECT, revalidate=True)

    # A plan written after the store was built is one the store cannot answer
    # for; the in-memory caches are dropped so the store is the only answer.
    late_slug = "late-target"
    _write_plan(cold_store, late_slug, sections={"s1": "done", "s2": "implementable"})
    metadata_index.clear()

    with _DocumentParseCounter() as parses:
        late_state = mcp_module._indexed_plan_state(PROJECT, late_slug, None)
    assert late_state is not None, "the ref into an unindexed plan resolved to nothing"
    assert parses.parsed == {late_slug}, (
        f"resolving an unindexed ref parsed {sorted(parses.parsed)}"
    )
    assert late_state.get("status") == "active", late_state.get("status")
    assert late_state.get("section_declarations", {}).get("s2") == "implementable"

    # A plan the store does cover is answered without parsing anything, which
    # is what makes the unindexed case exactly one parse rather than that plus
    # a project index.
    with _DocumentParseCounter() as covered_parses:
        covered_state = mcp_module._indexed_plan_state(PROJECT, TARGETS[0], None)
    assert covered_state is not None
    assert covered_parses.parsed == set(), (
        f"an indexed ref parsed {sorted(covered_parses.parsed)}"
    )


def test_an_unresolved_ref_is_looked_up_once(cold_store: Path) -> None:
    """A ref that resolves to nothing is cached as absent, not retried.

    The mapping's promise is that each slug is resolved at most once per read.
    An absence is an answer, so a second lookup of the same missing plan must
    not repeat the parse-or-store work the first one already did.
    """

    states = mcp_module._PersistedPlanStates(PROJECT, None)
    calls: list[str] = []
    real = states._resolve

    def counting(slug: str) -> dict[str, Any] | None:
        calls.append(slug)
        return real(slug)

    states._resolve = counting  # type: ignore[method-assign]
    assert states.get("no-such-plan") is None
    assert states.get("no-such-plan") is None
    assert calls == ["no-such-plan"], calls


def test_the_state_mapping_reports_the_plans_it_holds(cold_store: Path) -> None:
    """The mapping reports its resolved plans through the dict interface.

    Every lookup the ref resolution makes goes through ``get``, but a later
    caller that iterates the mapping must see the plans it resolved rather than
    an empty mapping that only implements ``get``.
    """

    metadata_index.index_rows(cold_store, PROJECT, revalidate=True)
    states = mcp_module._PersistedPlanStates(PROJECT, None)
    state = states.get(TARGETS[0])
    assert state is not None
    assert len(states) == 1, len(states)
    assert list(states) == [TARGETS[0]], list(states)
    assert TARGETS[0] in states
    assert states[TARGETS[0]] is state
    assert dict(states.items()) == {TARGETS[0]: state}


def test_the_negative_control_restores_the_index_build(
    cold_store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The revert switch really does make the first case's read build the index.

    Without this, a negative control that silently stopped working would let
    the first case pass for the wrong reason: its assertions would never see
    the cost they exist to bound.
    """

    monkeypatch.setattr(mcp_module, "_indexed_plan_states", _base_indexed_plan_states)
    with _DocumentParseCounter() as parses:
        _detail_read()
    assert len(parses.parsed) == 20, sorted(parses.parsed)
