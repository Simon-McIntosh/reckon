"""A project's pending-plan limit warns at creation; it never refuses.

The limit is declared in the project configuration beside
``schedule_horizon_sprints``; *pending* is the velocity plan census's own
closed definition (not shipped/done/superseded/abandoned, not archived). A
create at or beyond the limit succeeds and its success response carries a
``warning`` naming the limit, the pending count and the three pending plans
nearest to closing, ranked by impl, highest first. A create within the limit,
or in a project declaring no limit, carries no warning. No plan comment is
written in any case.

Every case synthesises a project under ``tmp_path`` and reads and writes only
there.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import mcp as mcp_module


def _plan_html(slug: str, status: str, impl: str) -> str:
    return (
        '<!doctype html><html lang="en"><head>'
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{slug}">'
        f'<meta name="plan-status" content="{status}">'
        f'<meta name="plan-impl" content="{impl}">'
        f"<title>{slug}</title></head>"
        "<body><main class=\"plan-doc\"></main></body></html>"
    )


def _write_project(root: Path, *, limit: int | None) -> None:
    """A synthetic project whose config declares (or omits) the pending limit."""

    (root / "docs" / "plans").mkdir(parents=True, exist_ok=True)
    state = root / "docs" / "state" / "sample"
    state.mkdir(parents=True, exist_ok=True)
    manifest = {"project": "sample", "schedule_horizon_sprints": 3}
    if limit is not None:
        manifest["pending_plan_limit"] = limit
    envelope = {
        "project": "sample",
        "doc": "index",
        "data": {"projects": [manifest], "sprints": []},
    }
    (state / "index.json").write_text(json.dumps(envelope), encoding="utf-8")


def _write_plan(root: Path, slug: str, status: str, impl: str) -> None:
    (root / "docs" / "plans" / f"{slug}.html").write_text(
        _plan_html(slug, status, impl), encoding="utf-8"
    )


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A project at its limit of 3, holding four pending plans."""

    root = tmp_path / "checkout"
    _write_project(root, limit=3)
    _write_plan(root, "alpha", "active", "0.9")
    _write_plan(root, "bravo", "active", "0.6")
    _write_plan(root, "charlie", "active", "0.4")
    _write_plan(root, "delta", "active", "0.1")
    return root


def _create(root: Path, slug: str):
    ops = [{"op": "set", "path": "standalone", "value": "one-file fix"}]
    return mcp_module._edit_plan(
        "sample",
        slug,
        ops,
        expected_version=0,
        create=True,
        checkout_path=str(root),
        doc_type="plan",
    )


def _comments(root: Path, slug: str) -> list[dict]:
    read = mcp_module._read_plan(
        "sample",
        slug,
        checkout_path=str(root),
        doc_type="plan",
    )
    return read["data"]["comments"].get("_top", [])


def test_create_at_the_limit_succeeds_and_warns_naming_the_three_nearest(
    checkout: Path,
):
    result = _create(checkout, "gamma")

    assert result["ok"] is True
    assert result["created"] is True
    assert (checkout / "docs" / "plans" / "gamma.html").exists()
    assert "warning" in result
    warning = result["warning"]
    assert "limit of 3" in warning
    assert "4 pending plans" in warning
    # Ranked by impl, highest first — the three nearest, delta omitted.
    assert "alpha (impl 0.9)" in warning
    assert "bravo (impl 0.6)" in warning
    assert "charlie (impl 0.4)" in warning
    assert "delta" not in warning
    # No comment is written on the created plan.
    assert _comments(checkout, "gamma") == []


def test_create_under_the_limit_carries_no_warning(tmp_path: Path):
    root = tmp_path / "checkout"
    _write_project(root, limit=5)
    _write_plan(root, "alpha", "active", "0.9")
    _write_plan(root, "bravo", "active", "0.6")

    result = _create(root, "gamma")

    assert result["ok"] is True
    assert (root / "docs" / "plans" / "gamma.html").exists()
    assert "warning" not in result
    assert _comments(root, "gamma") == []


def test_a_project_declaring_no_limit_carries_no_warning(tmp_path: Path):
    root = tmp_path / "checkout"
    _write_project(root, limit=None)
    _write_plan(root, "alpha", "active", "0.9")
    _write_plan(root, "bravo", "active", "0.6")

    result = _create(root, "gamma")

    assert result["ok"] is True
    assert "warning" not in result
    assert _comments(root, "gamma") == []