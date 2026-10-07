"""A pick that asks for a node's hours reads the node's plan, not the docs tree.

The estimate a node carries is either the node's own figure, or the figure the
plan it names declares, or nothing. Resolving which required walking every
HTML file below the docs root to build a staleness stamp before the plan was
even looked up, so every pick paid a whole-tree scan on a shared filesystem.
The resolve now reads the plan and the directory listings a resolve touches,
and caches the figure on their stamps, so a repeated ask for an unchanged plan
reads no directory at all.
"""

from __future__ import annotations

import math
import os
import re
from pathlib import Path

import pytest

from reckon import _plan_html, resources
from reckon.crew.node import TaskNode
from reckon.crew import routing


PLAN_TEMPLATE = (
    '<meta name="reckon-type" content="plan">'
    '<meta name="plan-slug" content="{slug}">'
    "{effort}"
)
_EFFORT_RE = re.compile(r'plan-effort-hours" content="(?P<hours>[^"]+)"')


def _plan_text(slug: str, hours: float | None) -> str:
    effort = (
        f'<meta name="plan-effort-hours" content="{hours}">'
        if hours is not None
        else ""
    )
    return PLAN_TEMPLATE.format(slug=slug, effort=effort)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _node(plan: str, hours: float | None = None) -> TaskNode:
    return TaskNode(
        id="work",
        goal="Estimate the node",
        plan=plan,
        role="implement",
        spec_level="guided",
        done_when="The estimate is returned",
        estimated_hours=hours,
    )


@pytest.fixture
def docs_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A docs tree whose plans live in a typed root, a nested legacy path and
    the archive, beside an evidence fragment, an infra file and an index."""

    monkeypatch.setenv("RECKON_PICK_CACHE", str(tmp_path / "pick-cache"))
    repo = tmp_path / "repo"
    docs = repo / "docs"
    _write(docs / "plans" / "alpha.html", _plan_text("alpha", 3))
    _write(docs / "plans" / "gamma.html", _plan_text("gamma", None))
    _write(docs / "plans" / "archive" / "retired.html", _plan_text("retired", 9))
    _write(docs / "legacy" / "nested" / "beta.html", _plan_text("beta", 4))
    # A fragment naming a live plan slug and an effort of its own must not be
    # resolved as that plan.
    _write(
        docs / "evidence" / "fragments" / "alpha" / "landed.html",
        _plan_text("alpha", 42),
    )
    _write(docs / "index.html", _plan_text("index", 7))
    _write(docs / "_shared" / "shell.html", _plan_text("shell", 8))
    return repo


def _expected_hours(repo: Path, slug: str) -> float | None:
    """The hours the fixture declares for a live, non-archived plan slug."""

    for path in (repo / "docs").rglob("*.html"):
        relative = path.relative_to(repo / "docs")
        if "archive" in relative.parts or "fragments" in relative.parts:
            continue
        meta = _plan_html.parse_meta(path)
        if meta.get("slug") != slug:
            continue
        match = _EFFORT_RE.search(path.read_text())
        if match is None:
            return None
        value = float(match.group("hours"))
        return value if math.isfinite(value) and value > 0 else None
    return None


def _walk_estimate(repo: Path, project: str, node: TaskNode) -> tuple[float | None, str]:
    """The figure the whole-tree resolve reaches, computed without the cache."""

    try:
        node_hours = float(node.estimated_hours)
    except (TypeError, ValueError):
        node_hours = 0.0
    if math.isfinite(node_hours) and node_hours > 0:
        return node_hours, "node"
    if not node.plan.strip():
        return None, "unavailable"
    resource = resources.resolve_resource(
        repo / "docs", project, node.plan, "plan", include_archived=False
    )
    if resource is None:
        return None, "unavailable"
    value = _plan_html.parse_meta(resource.path).get("effort_hours")
    try:
        hours = float(value)
    except (TypeError, ValueError):
        return None, "unavailable"
    return (
        (hours, "plan-fallback")
        if math.isfinite(hours) and hours > 0
        else (None, "unavailable")
    )


@pytest.mark.parametrize(
    ("plan", "node_hours"),
    [
        ("alpha", None),
        ("beta", None),
        ("gamma", None),
        ("retired", None),
        ("missing", None),
        ("index", None),
        ("", None),
        ("alpha", 0.5),
    ],
)
def test_the_estimate_matches_the_whole_tree_resolve(
    docs_tree: Path, plan: str, node_hours: float | None
) -> None:
    node = _node(plan, node_hours)
    assert routing._estimated_hours(docs_tree, "sample", node) == _walk_estimate(
        docs_tree, "sample", node
    )


def test_a_live_plan_reports_its_declared_hours(docs_tree: Path) -> None:
    result = routing._estimated_hours(docs_tree, "sample", _node("alpha"))
    assert result == (3.0, "plan-fallback")
    assert result[0] == _expected_hours(docs_tree, "alpha")


def test_a_nested_legacy_plan_is_found_without_a_typed_root(docs_tree: Path) -> None:
    assert routing._estimated_hours(docs_tree, "sample", _node("beta")) == (
        4.0,
        "plan-fallback",
    )


def test_the_node_estimate_wins_over_the_plan(docs_tree: Path) -> None:
    assert routing._estimated_hours(docs_tree, "sample", _node("alpha", 0.5)) == (
        0.5,
        "node",
    )


@pytest.mark.parametrize("plan", ["gamma", "retired", "missing", "", "index", "shell"])
def test_a_planless_estimate_is_unavailable(docs_tree: Path, plan: str) -> None:
    assert routing._estimated_hours(docs_tree, "sample", _node(plan)) == (
        None,
        "unavailable",
    )


def test_the_fragment_sharing_a_plan_slug_is_not_the_plan(docs_tree: Path) -> None:
    """A landing fragment names its plan's slug; only the plan declares hours."""

    assert routing._estimated_hours(docs_tree, "sample", _node("alpha")) == (
        3.0,
        "plan-fallback",
    )


def test_a_second_call_for_a_plan_walks_no_directory(
    docs_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_scandir = os.scandir
    calls: list[object] = []

    def counting_scandir(*args, **kwargs):
        calls.append(args[0] if args else kwargs.get("path"))
        return real_scandir(*args, **kwargs)

    monkeypatch.setattr(os, "scandir", counting_scandir)
    routing._estimated_hours(docs_tree, "sample", _node("alpha"))
    assert calls, "the first resolve must read the docs tree"
    calls.clear()

    assert routing._estimated_hours(docs_tree, "sample", _node("alpha")) == (
        3.0,
        "plan-fallback",
    )
    assert calls == [], f"second resolve walked directories: {calls}"


def test_editing_the_plan_changes_the_returned_hours(docs_tree: Path) -> None:
    assert routing._estimated_hours(docs_tree, "sample", _node("alpha")) == (
        3.0,
        "plan-fallback",
    )
    plan = docs_tree / "docs" / "plans" / "alpha.html"
    plan.write_text(_plan_text("alpha", 5))
    assert routing._estimated_hours(docs_tree, "sample", _node("alpha")) == (
        5.0,
        "plan-fallback",
    )