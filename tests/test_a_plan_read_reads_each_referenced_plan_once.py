"""A plan read reads each referenced plan once, from the metadata index.

A plan whose sections wait on refs used to resolve every ref with its own full
plan read of the target, so one read of a plan waiting on six refs across two
plans read each target three times. A referenced plan's row needs only what its
parse carries, which the metadata index already holds, so the read now answers
every ref from one index lookup per referenced plan and reads no target in
full.

The counts are taken on one read of the waiting plan. ``read_plan`` is wrapped
on the full plan read path, so a target read in full is counted wherever it is
made, and the index lookup is wrapped so one lookup per referenced plan is
asserted rather than assumed. The same fixture also resolves through the base
revision's per-ref full read, and the rows must be identical — the saving may
not change an answer.

``RECKON_TEST_REVERT_INDEXED_REF_READ`` restores the per-ref full read, which
is the node's negative control: the same assertions then see six full reads
and fail.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from html import escape
from pathlib import Path
from typing import Any, Self

import pytest

import reckon.mcp as mcp_module
from tests.mcp_family_reload import reload_mcp_family

PROJECT = "alpha"
OWNER = "waiting-plan"
FIRST = "first-target"
SECOND = "second-target"
REVERT_ENV = "RECKON_TEST_REVERT_INDEXED_REF_READ"

#: Three refs into each referenced plan, held by six sections of the owner.
WAITS = {
    "s1": [f"{FIRST}#s1"],
    "s2": [f"{FIRST}#s2"],
    "s3": [f"{FIRST}#s3"],
    "s4": [f"{SECOND}#s1"],
    "s5": [f"{SECOND}#s2"],
    "s6": [f"{SECOND}#s3"],
}


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
    head.append(f"<title>{slug}</title></head>")
    body = ['<body><main class="plan-doc">']
    for name, declaration in sorted(sections.items()):
        body.append(
            # A target plan's sections are authored the same way; only the
            # waiting plan carries the depends-on meta.
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
def referenced_plans(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A waiting plan naming six refs across two referenced plans."""

    docs = tmp_path / "repo" / "docs"
    docs.mkdir(parents=True)
    state_root = tmp_path / "state"
    state_root.mkdir()
    mounts = tmp_path / "mounts.json"
    mounts.write_text(json.dumps({PROJECT: str(docs)}), encoding="utf-8")
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))

    import reckon.serve as serve_module

    serve_module._MOUNTS_FILE = mounts
    serve_module._STATE_ROOT = state_root
    serve_module._DISC_CACHE.clear()
    reload_mcp_family()

    _write_plan(docs, FIRST, sections={"s1": "done", "s2": "done", "s3": "done"})
    _write_plan(
        docs,
        SECOND,
        sections={"s1": "done", "s2": "implementable", "s3": "done"},
    )
    _write_plan(
        docs,
        OWNER,
        sections=dict.fromkeys(WAITS, "implementable"),
        section_depends_on=WAITS,
    )
    return PROJECT


class _FullReadCounter:
    """Count full plan reads by target slug for the duration of the block."""

    def __enter__(self) -> Self:
        self.calls: list[tuple[str, str]] = []
        self._real = mcp_module.read_plan

        def counting(project: str, slug: str, *args: Any, **kwargs: Any):
            self.calls.append((project, slug))
            return self._real(project, slug, *args, **kwargs)

        mcp_module.read_plan = counting  # type: ignore[assignment]
        return self

    def __exit__(self, *exc: object) -> None:
        mcp_module.read_plan = self._real  # type: ignore[assignment]

    def referenced(self) -> list[tuple[str, str]]:
        return [call for call in self.calls if call[1] in (FIRST, SECOND)]


class _IndexLookupCounter:
    """Count the metadata-index reads that answer the refs."""

    def __enter__(self) -> Self:
        self.projects: list[str] = []
        self._real = mcp_module._indexed_plan_states

        def counting(
            project: str, checkout_path: str | None
        ) -> dict[str, dict[str, Any]]:
            self.projects.append(project)
            return self._real(project, checkout_path)

        mcp_module._indexed_plan_states = counting  # type: ignore[assignment]
        return self

    def __exit__(self, *exc: object) -> None:
        mcp_module._indexed_plan_states = self._real  # type: ignore[assignment]


@contextmanager
def _per_ref_full_read_lookup() -> Iterator[None]:
    """Resolve each ref with its own full plan read, as the base revision did."""

    real_lookup = mcp_module._indexed_plan_state

    def lookup(
        target_project: str,
        slug: str,
        checkout_path: str | None,
        state_cache: dict[Any, dict[str, dict[str, Any]]] | None = None,
    ) -> dict[str, Any] | None:
        data, _version = mcp_module.read_plan(target_project, slug, checkout_path)
        return data or None

    mcp_module._indexed_plan_state = lookup  # type: ignore[assignment]
    try:
        yield
    finally:
        mcp_module._indexed_plan_state = real_lookup  # type: ignore[assignment]


def _section_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in result.get("deps", []) if row.get("source_section")]


def _read(project: str) -> dict[str, Any]:
    return mcp_module._read_plan(project, OWNER)


def test_a_plan_read_indexes_each_referenced_plan_once(referenced_plans: str) -> None:
    if os.environ.get(REVERT_ENV) == "1":
        # The negative control: restore the per-ref full read and let the same
        # assertions observe six target reads.
        with _per_ref_full_read_lookup():
            _run_assertions(referenced_plans)
        return
    _run_assertions(referenced_plans)


def _run_assertions(project: str) -> None:
    with _FullReadCounter() as reads, _IndexLookupCounter() as lookups:
        result = _read(project)

    # The counter sees the waiting plan's own read, so an empty target count
    # cannot mean the wrapper missed every read.
    assert (project, OWNER) in reads.calls

    assert reads.referenced() == [], (
        f"resolving the refs read referenced plans in full: {reads.referenced()}"
    )

    # At most one index read per referenced plan: a project's index answers
    # every ref into it, so the count cannot grow with the number of refs. The
    # lower bound proves the index was actually consulted rather than skipped.
    assert 0 < len(lookups.projects) <= len({FIRST, SECOND}), lookups.projects

    rows = _section_rows(result)
    assert len(rows) == 6, result
    assert {row["ref"] for row in rows} == {
        ref for refs in WAITS.values() for ref in refs
    }
    assert {row["source_section"] for row in rows} == set(WAITS)
    assert {row["status"] for row in rows} == {"done", "implementable"}

    # The answer is the base revision's answer: the per-ref full read returns
    # the same rows and the same plan data, so reading from the index changed
    # the cost, not the state.
    with _per_ref_full_read_lookup():
        base_result = _read(project)
    assert rows == _section_rows(base_result)
    assert result["data"] == base_result["data"]
