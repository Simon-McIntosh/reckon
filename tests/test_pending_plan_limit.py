"""A project holds bounded open work: a plan beyond its pending-plan limit is refused.

The limit is declared in the project configuration beside
``schedule_horizon_sprints``; *pending* is the velocity plan census's own
closed definition (not shipped/done/superseded/abandoned, not archived). The
refusal names the pending plans nearest to closing and the override that lets a
create through, and the override's reason is recorded on the plan it opened.

Every case synthesises a project under ``tmp_path`` and reads and writes only
there.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import mcp as mcp_module


def _plan_html(slug: str, status: str, impl: str, *, closable: bool = False) -> str:
    followups = ""
    if closable:
        followups = (
            '<section data-reckon="followups" id="followups" class="r-followups">'
            f'<article class="r-fu" data-id="f-{slug}" data-status="resolved">'
            f'<h4 class="r-fu-title">close {slug}</h4>'
            '<div class="r-fu-body"><p>the chain ends here</p></div>'
            f'<pre class="r-fu-prompt">/reckon-build {slug} §9</pre>'
            '<p class="r-fu-outcome">done — no followup</p></article></section>'
        )
    return (
        '<!doctype html><html lang="en"><head>'
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{slug}">'
        f'<meta name="plan-status" content="{status}">'
        f'<meta name="plan-impl" content="{impl}">'
        f"<title>{slug}</title></head>"
        f'<body><main class="plan-doc">{followups}</main></body></html>'
    )


def _write_evidence(root: Path, slug: str) -> None:
    """A typed evidence record, so a plan can reach a terminal status."""

    archive = root / "docs" / "evidence" / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    (archive / f"{slug}-landed.html").write_text(
        '<!doctype html><html lang="en"><head>'
        '<meta name="reckon-type" content="evidence">'
        f'<meta name="plan-evidence-for" content="{slug}">'
        f"<title>{slug} landed</title></head>"
        "<body><main></main></body></html>",
        encoding="utf-8",
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


def _write_plan(
    root: Path, slug: str, status: str, impl: str, *, closable: bool = False
) -> None:
    (root / "docs" / "plans" / f"{slug}.html").write_text(
        _plan_html(slug, status, impl, closable=closable), encoding="utf-8"
    )


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A project at its limit of 2, holding two pending plans."""

    root = tmp_path / "checkout"
    _write_project(root, limit=2)
    _write_plan(root, "alpha", "active", "0.8")
    # beta is pending yet landable, so one test can close it through the write
    # path and watch the census free a slot.
    _write_plan(root, "beta", "active", "0.3", closable=True)
    _write_evidence(root, "beta")
    return root


def _create(root: Path, slug: str, **kwargs):
    ops = kwargs.pop(
        "ops", [{"op": "set", "path": "standalone", "value": "one-file fix"}]
    )
    return mcp_module._edit_plan(
        "sample",
        slug,
        ops,
        expected_version=0,
        create=True,
        checkout_path=str(root),
        doc_type="plan",
        **kwargs,
    )


def test_third_create_is_refused_naming_the_plans_nearest_to_closing(checkout: Path):
    result = _create(checkout, "gamma")

    assert result["ok"] is False
    assert result["error"] == "pending_plan_limit"
    assert result["limit"] == 2
    assert result["pending"] == 2
    # Ranked by impl, highest first.
    assert [row["slug"] for row in result["nearest_to_closing"]] == ["alpha", "beta"]
    assert "limit of 2" in result["detail"]
    assert "alpha (impl 0.8)" in result["detail"]
    assert "override_wip_limit" in result["detail"]
    # A refused create leaves no trace.
    assert not (checkout / "docs" / "plans" / "gamma.html").exists()


def test_override_lets_the_create_through_and_records_the_reason(checkout: Path):
    reason = "lead approved opening this plan against the cap on 2026-09-29"

    created = _create(checkout, "gamma", override_wip_limit=reason)

    assert created["ok"] is True
    assert (checkout / "docs" / "plans" / "gamma.html").exists()
    read = mcp_module._read_plan(
        "sample", "gamma", checkout_path=str(checkout), doc_type="plan"
    )
    bodies = [comment["body"] for comment in read["data"]["comments"].get("_top", [])]
    assert any(reason in body for body in bodies), bodies


def test_closing_a_plan_frees_a_slot(checkout: Path):
    current = mcp_module._read_plan(
        "sample", "beta", checkout_path=str(checkout), doc_type="plan"
    )
    closed = mcp_module._edit_plan(
        "sample",
        "beta",
        [{"op": "set", "path": "status", "value": "done"}],
        expected_version=current["version"],
        checkout_path=str(checkout),
        doc_type="plan",
    )
    assert closed["ok"] is True

    result = _create(checkout, "gamma")

    assert result["ok"] is True
    assert (checkout / "docs" / "plans" / "gamma.html").exists()


def test_a_project_without_a_declared_limit_is_never_refused(tmp_path: Path):
    root = tmp_path / "checkout"
    _write_project(root, limit=None)
    _write_plan(root, "alpha", "active", "0.8")
    _write_plan(root, "beta", "active", "0.3")

    result = _create(root, "gamma")

    assert result["ok"] is True


def _comments(root: Path, slug: str) -> list[dict]:
    read = mcp_module._read_plan(
        "sample", slug, checkout_path=str(root), doc_type="plan"
    )
    return read["data"]["comments"].get("_top", [])


def test_an_override_below_the_limit_writes_no_comment(tmp_path: Path):
    # limit 2, one pending plan: the override lifts nothing, so it is silent.
    root = tmp_path / "checkout"
    _write_project(root, limit=2)
    _write_plan(root, "alpha", "active", "0.8")

    created = _create(root, "gamma", override_wip_limit="opened early")

    assert created["ok"] is True
    assert _comments(root, "gamma") == []


def test_an_override_on_an_uncapped_project_writes_no_comment(tmp_path: Path):
    root = tmp_path / "checkout"
    _write_project(root, limit=None)
    _write_plan(root, "alpha", "active", "0.8")

    created = _create(root, "gamma", override_wip_limit="opened early")

    assert created["ok"] is True
    # No comment at all — in particular none naming a null limit.
    assert _comments(root, "gamma") == []
