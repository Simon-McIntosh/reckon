"""A summary read of one plan lists its inventory without walking the corpus.

A one-plan summary read answers with the plan's own state, and with the
blocking the project's inventory derives for it — the explicit and held
references a sprint item names. That inventory and the plan's provenance were
both taken by walking the docs tree and rglobbing every HTML file, so the
filesystem work grew with the number of plans even though the read concerns
one.

The summary read now takes the inventory rows from the persisted metadata
index — read when the tree's shape is unchanged by the directories' stat
identity, so the walk runs only to rebuild a stale index — and resolves the one
selected document at its canonical path rather than building the whole resource
map to pick one entry out of it.

The count is taken on a warm read, after the index is built once and persisted.
``RECKON_TEST_REVERT_SUMMARY_INDEX`` routes the inventory read back through the
corpus walk; the 60-plan count then exceeds the 5-plan count and the equality
assertion fails, which is the node's negative control.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Self

from reckon import mcp, metadata_index, serve
from reckon._plan_html import write_state

PLAN_COUNT_SMALL = 5
PLAN_COUNT_LARGE = 60
PROJECT = "sample"
REVERT_ENV = "RECKON_TEST_REVERT_SUMMARY_INDEX"

_BARE_PLAN = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    '<meta name="docs-project" content="sample">'
    "<title>plan</title></head>"
    '<body><main class="plan-doc"></main></body></html>'
)


class _ScandirCounter:
    """Count every ``os.scandir`` call made while the block runs."""

    def __enter__(self) -> Self:
        self.count = 0
        self._real = os.scandir

        def counting(path: object = ".") -> object:
            self.count += 1
            return self._real(path)

        os.scandir = counting  # type: ignore[assignment]
        return self

    def __exit__(self, *exc: object) -> None:
        os.scandir = self._real  # type: ignore[assignment]


def _build_project(root: Path, plan_count: int) -> Path:
    """Lay out a project with ``plan_count`` plans and their evidence dirs.

    Each plan owns an evidence-fragment directory so the tree carries one
    directory per plan: a recursive glob over it costs more filesystem work in
    a larger project, which is what a read about one plan must stop paying for.
    The first plan depends on the second, and a sprint item blocks the first
    with an explicit reference, so the inventory's derived blocking that the
    summary embeds is non-empty and the payload comparison is meaningful.
    """

    docs = root / "docs"
    plans_dir = docs / "plans"
    plans_dir.mkdir(parents=True)
    for index in range(plan_count):
        slug = f"plan-{index:03d}"
        state: dict[str, object] = {
            "slug": slug,
            "title": slug,
            "status": "active",
            "type": "plan",
            "version": 0,
        }
        if index == 0:
            state["depends_on"] = ["plan-001"]
        (plans_dir / f"{slug}.html").write_text(
            write_state(
                _BARE_PLAN.replace("<title>plan</title>", f"<title>{slug}</title>"),
                state,
            ),
            encoding="utf-8",
        )
        fragment_dir = docs / "evidence" / "fragments" / slug
        fragment_dir.mkdir(parents=True)
        (fragment_dir / "landing.html").write_text(
            "<html><body><p>landed</p></body></html>", encoding="utf-8"
        )
    state_dir = docs / "state" / PROJECT
    state_dir.mkdir(parents=True)
    (state_dir / "index.json").write_text(
        json.dumps(
            {
                "version": 1,
                "data": {
                    "sprints": [
                        {
                            "id": "S1",
                            "theme": "Index-backed reads",
                            "status": "active",
                            "items": [
                                {"slug": "plan-000", "blocked_by": ["explicit-1"]}
                            ],
                        }
                    ],
                    "milestones": [],
                    "blockers": [],
                    "timeline": [],
                    "active_sprint_id": "S1",
                    "north_stars": [],
                },
            }
        ),
        encoding="utf-8",
    )
    return root


def _read(root: Path, slug: str = "plan-000") -> dict:
    return mcp._read_plan(PROJECT, slug, checkout_path=str(root), view="summary")


def _warm_scandir_count(root: Path, slug: str = "plan-000") -> tuple[dict, int]:
    """Return ``(payload, scandir_count)`` for a warm read of one plan."""

    _read(root, slug)
    with _ScandirCounter() as counter:
        payload = _read(root, slug)
    return payload, counter.count


def _force_walk(monkeypatch) -> None:
    """Route the inventory read back through the corpus walk."""

    monkeypatch.setattr(
        mcp,
        "_index_discovery",
        lambda project, root=None: mcp._discover_project(project, root),
    )


def test_summary_view_scandir_is_flat_in_corpus_size(tmp_path, monkeypatch) -> None:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    serve._DISC_CACHE.clear()
    metadata_index.clear()

    reverted = os.environ.get(REVERT_ENV) == "1"

    small_root = _build_project(tmp_path / "small", PLAN_COUNT_SMALL)
    large_root = _build_project(tmp_path / "large", PLAN_COUNT_LARGE)

    if reverted:
        _force_walk(monkeypatch)

    # The payload is unchanged from the walk path: force the inventory walk and
    # require the same result the index-backed read returns.
    if not reverted:
        fast_small = _read(small_root)
        with monkeypatch.context() as context:
            _force_walk(context)
            scanned_small = _read(small_root)
        assert fast_small == scanned_small

        # The counter must see a scan when one happens, or an equal pair of
        # zeros would prove nothing.
        with monkeypatch.context() as context:
            _force_walk(context)
            _payload, forced_small = _warm_scandir_count(small_root)
            _payload, forced_large = _warm_scandir_count(large_root)
        assert forced_large > forced_small > 0

    _payload, small_count = _warm_scandir_count(small_root)
    _large_payload, large_count = _warm_scandir_count(large_root)
    assert small_count == large_count, (
        f"summary view scanned {small_count} times for {PLAN_COUNT_SMALL} plans "
        f"and {large_count} times for {PLAN_COUNT_LARGE} plans; a read about one "
        "plan must not walk the corpus"
    )
