"""One read walks the resource tree once, whatever flags its callers ask for.

A read opens ``resource_scan_scope`` and resolves many slugs inside it; the
callers ask for different ``include_archived`` and ``include_legacy``
combinations, and the scope serves one tree walk with the widest flags, from
which each caller's view is filtered. The count of walks is the property under
test, so the walk itself is wrapped and counted, and each caller's view is
checked against the resources its own flags select.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import reckon.resources as resources
from reckon.resources import iter_resources, resource_scan_scope

PROJECT = "sample"


def _doc(root: Path, relative: str, *, slug: str, archived: bool = False) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{slug}">'
        f'<meta name="plan-archived" content="{"1" if archived else "0"}">'
        f"<title>{slug}</title></head><body><main><h1>{slug}</h1></main></body></html>"
    )
    return path


@pytest.fixture
def docs_tree(tmp_path: Path) -> Path:
    """A tree holding a live plan, an archived plan, and a legacy flat plan."""
    docs = tmp_path / "docs"
    _doc(docs, "plans/live-plan.html", slug="live-plan")
    _doc(docs, "plans/archive/old-plan.html", slug="old-plan", archived=True)
    _doc(docs, "flat-plan.html", slug="flat-plan")
    return docs


def _slugs(resources_: list) -> set[str]:
    return {resource.slug for resource in resources_}


@pytest.fixture
def counted_walks(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Record every call to the tree walk, delegating to the real one."""
    calls: list[dict] = []
    real = resources._scan_resources

    def counting(docs_dir, project, **flags):
        calls.append(flags)
        return real(docs_dir, project, **flags)

    monkeypatch.setattr(resources, "_scan_resources", counting)
    return calls


def test_one_read_walks_the_tree_once_across_flag_combinations(
    docs_tree: Path, counted_walks: list[dict]
) -> None:
    with resource_scan_scope():
        everything = iter_resources(docs_tree, PROJECT, ignore_invalid=True)
        live_only = iter_resources(
            docs_tree,
            PROJECT,
            include_archived=False,
            include_legacy=False,
            ignore_invalid=True,
        )
        no_archived = iter_resources(
            docs_tree,
            PROJECT,
            include_archived=False,
            include_legacy=True,
            ignore_invalid=True,
        )
        no_legacy = iter_resources(
            docs_tree,
            PROJECT,
            include_archived=True,
            include_legacy=False,
            ignore_invalid=True,
        )

    assert len(counted_walks) == 1, counted_walks
    only_walk = counted_walks[0]
    assert only_walk["include_archived"] is True
    assert only_walk["include_legacy"] is True
    assert only_walk["ignore_invalid"] is True

    assert _slugs(everything) == {"live-plan", "old-plan", "flat-plan"}
    assert _slugs(live_only) == {"live-plan"}
    assert _slugs(no_archived) == {"live-plan", "flat-plan"}
    assert _slugs(no_legacy) == {"live-plan", "old-plan"}


def test_each_caller_receives_a_fresh_copy(
    docs_tree: Path, counted_walks: list[dict]
) -> None:
    with resource_scan_scope():
        first = iter_resources(docs_tree, PROJECT, ignore_invalid=True)
        second = iter_resources(docs_tree, PROJECT, ignore_invalid=True)

    assert first == second
    assert first is not second
    assert len(counted_walks) == 1


def test_outside_a_scope_each_call_scans(
    docs_tree: Path, counted_walks: list[dict]
) -> None:
    iter_resources(docs_tree, PROJECT, include_archived=False, ignore_invalid=True)
    iter_resources(docs_tree, PROJECT, include_archived=True, ignore_invalid=True)

    assert len(counted_walks) == 2


def test_a_strict_caller_outside_a_scope_keeps_its_scan(
    docs_tree: Path, counted_walks: list[dict]
) -> None:
    result = iter_resources(docs_tree, PROJECT, include_archived=True)

    assert _slugs(result) == {"live-plan", "old-plan", "flat-plan"}
    assert counted_walks == [
        {"include_archived": True, "include_legacy": True, "ignore_invalid": False}
    ]