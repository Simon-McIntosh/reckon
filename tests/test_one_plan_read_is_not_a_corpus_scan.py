"""A one-plan read resolves that plan without walking the whole corpus.

``read_plan`` for one named plan used to find its document by globbing the
docs tree, and to derive the north-star diagnostic by composing the whole
project state, so the filesystem work of a single read grew with the number
of plans in the project. A named read now resolves its document at its
canonical path and takes the diagnostic from a result the store already
holds, so the recursive-glob count of one read is flat in corpus size while
its payload is unchanged.

The count is taken on a warm read — the second call after the diagnostic memo
is armed rather than removed, because the deliverable memoises the composition
a cold read still needs once. ``RECKON_TEST_REVERT_DIRECT_READ_RESOLUTION``
reverts the direct-path resolution so the read scans the corpus again; the
counts then diverge by corpus size and the equality assertion fails, which is
the node's negative control.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Self

from reckon import _store, resources
from reckon._plan_html import write_state

PLAN_COUNT_SMALL = 5
PLAN_COUNT_LARGE = 60
PROJECT = "sample"
NORTH_STAR = "reliable-delivery"
REVERT_ENV = "RECKON_TEST_REVERT_DIRECT_READ_RESOLUTION"

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

    Each plan also owns an evidence-fragment directory, so the tree carries one
    directory per plan. A recursive glob over this tree therefore costs more
    filesystem work in a larger project, which is what a corpus scan must stop
    paying for on a read about one plan.
    """

    docs = root / "docs"
    plans_dir = docs / "plans"
    plans_dir.mkdir(parents=True)
    for index in range(plan_count):
        slug = f"plan-{index:03d}"
        html = _BARE_PLAN.replace("<title>plan</title>", f"<title>{slug}</title>")
        (plans_dir / f"{slug}.html").write_text(
            write_state(
                html,
                {
                    "slug": slug,
                    "title": slug,
                    "status": "active",
                    "type": "plan",
                    "version": 0,
                    "north_star": NORTH_STAR,
                },
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
                "project": PROJECT,
                "data": {
                    "north_stars": [{"id": NORTH_STAR, "name": "Reliable delivery"}],
                    "projects": [
                        {"project": PROJECT, "north_stars": [{"id": NORTH_STAR}]}
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    return root


def _read(root: Path) -> tuple[dict, int]:
    return _store.read_plan(PROJECT, "plan-000", root=root)


def _warm_scandir_count(root: Path) -> int:
    """Return a read's scandir count with the diagnostic memo already armed."""

    _read(root)
    with _ScandirCounter() as counter:
        _read(root)
    return counter.count


def _disable_direct_resolution(monkeypatch) -> None:
    monkeypatch.setattr(resources, "_resolve_canonical", lambda *a, **k: None)


def test_one_plan_read_counts_flat_in_corpus_size(tmp_path: Path, monkeypatch) -> None:
    reverted = os.environ.get(REVERT_ENV) == "1"
    if reverted:
        _disable_direct_resolution(monkeypatch)

    small_root = _build_project(tmp_path / "small", PLAN_COUNT_SMALL)
    large_root = _build_project(tmp_path / "large", PLAN_COUNT_LARGE)

    if not reverted:
        # The payload is unchanged from the scan path: force the scan and
        # require the same result the direct path returns.
        fast_small, version_small = _read(small_root)
        fast_large, version_large = _read(large_root)
        with monkeypatch.context() as context:
            _disable_direct_resolution(context)
            scanned_small, scanned_version_small = _read(small_root)
            scanned_large, scanned_version_large = _read(large_root)
        assert (fast_small, version_small) == (scanned_small, scanned_version_small)
        assert (fast_large, version_large) == (scanned_large, scanned_version_large)

        # The counter must see a scan when one happens, or an equal pair of
        # zeros would prove nothing.
        with monkeypatch.context() as context:
            _disable_direct_resolution(context)
            forced_small = _warm_scandir_count(small_root)
            forced_large = _warm_scandir_count(large_root)
        assert forced_large > forced_small > 0

    small_count = _warm_scandir_count(small_root)
    large_count = _warm_scandir_count(large_root)
    assert small_count == large_count, (
        f"one-plan read scanned {small_count} times for {PLAN_COUNT_SMALL} plans "
        f"and {large_count} times for {PLAN_COUNT_LARGE} plans; a read about one "
        "plan must not walk the corpus"
    )
