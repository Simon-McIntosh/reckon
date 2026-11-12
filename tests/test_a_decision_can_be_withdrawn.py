"""An unlocked decision can be withdrawn through edit_plan, reason and date kept.

Taking a decision out of a plan had no tool path: ``lock`` records an answer
and text mode refuses a replacement that overlaps the decisions region, so the
block was removed by a direct hand edit. ``withdraw`` is that path. It writes
in one versioned call, keeps the reason and the date in the plan's own record,
and records no choice — a withdrawal is not a decision anyone made. The wait it
caused leaves the roadmap's blockers with it, and a locked decision, whose
choice IS someone's answer, is refused rather than quietly discarded.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon._plan_html import parse_plan, read_state_file, write_state
from reckon.roadmap import build_roadmap
from tests.mcp_family_reload import reload_mcp_family

PROJECT = "withdraw-project"
SLUG = "withdraw-case"

REASON = (
    "the transport question governed an interface that has been removed, so "
    "nothing is left for it to decide"
)


@pytest.fixture()
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    state_root = tmp_path / "state"
    state_root.mkdir()
    mounts_file = tmp_path / "mounts.json"
    mounts_file.write_text(json.dumps({PROJECT: str(docs_dir)}))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))

    import reckon.serve as serve_mod

    serve_mod._MOUNTS_FILE = mounts_file
    serve_mod._STATE_ROOT = state_root
    mcp_module = reload_mcp_family()
    return docs_dir, mcp_module


def _bare_plan(slug: str, state: dict) -> str:
    base = dict(state)
    base.setdefault("slug", slug)
    base.setdefault("title", slug.title())
    base.setdefault("type", "plan")
    base.setdefault("status", "active")
    bare = (
        '<!doctype html>\n<html lang="en">\n<head><meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{slug}</title></head>\n"
        '<body><main class="plan-doc">'
        '<h2 id="s1">§1 — work</h2>'
        "</main></body>\n</html>\n"
    )
    return write_state(bare, base)


def _write_plan(docs_dir: Path, decisions: dict) -> Path:
    path = docs_dir / "plans" / f"{SLUG}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _bare_plan(
            SLUG,
            {
                "version": 0,
                "section_declarations": {"s1": "implementable"},
                "decisions": decisions,
            },
        ),
        encoding="utf-8",
    )
    return path


def _unlocked() -> dict:
    return {
        "transport": {
            "title": "Which transport carries the payload?",
            "context": "The consumer chose its own wire format.",
            "choices": ["grpc", "rest"],
        }
    }


def _mixed() -> dict:
    decisions = _unlocked()
    decisions["naming"] = {
        "title": "Which naming scheme?",
        "choices": ["snake", "camel"],
        "choice": "snake",
        "rationale": "the rest of the corpus is snake_case",
        "when": "2026-01-02T03:04:05+00:00",
        "by": "Simon McIntosh",
    }
    return decisions


def _roadmap_row(docs_dir: Path) -> dict:
    parsed = parse_plan(docs_dir / "plans" / f"{SLUG}.html")
    report = build_roadmap(PROJECT, [parsed], [], docs_dir=docs_dir, review={})
    return next(row for row in report["pending_work"] if row["slug"] == SLUG)


def _withdraw(mcp_module, expected_version: int, **fields) -> dict:
    op = {"op": "withdraw", **fields}
    return mcp_module._edit_plan(PROJECT, SLUG, [op], expected_version)


def test_withdrawing_an_unlocked_decision_clears_its_wait(setup) -> None:
    docs_dir, mcp_module = setup
    path = _write_plan(docs_dir, _unlocked())

    before = _roadmap_row(docs_dir)
    assert [row["id"] for row in before["decision_blockers"]] == ["transport"]
    assert before["readiness"] == "blocked"
    assert parse_plan(path)["dec_open"] == 1
    assert parse_plan(path)["version"] == 0

    result = _withdraw(
        mcp_module,
        0,
        key="transport",
        reason=REASON,
        by="Simon McIntosh",
    )

    assert result["ok"] is True, result
    assert result["new_version"] == 1
    assert parse_plan(path)["version"] == 1, "the withdrawal is one versioned write"
    assert parse_plan(path)["dec_open"] == 0, "the decision is no longer open"

    after = _roadmap_row(docs_dir)
    assert after["decision_blockers"] == []
    assert after["readiness"] == "ready"
    # The record survives the wait: the withdrawal is reported with its reason
    # rather than the decision disappearing from the plan.
    withdrawn = [row for row in after["deferred_decisions"] if row["id"] == "transport"]
    assert withdrawn and withdrawn[0]["rationale"] == REASON


def test_a_withdrawal_keeps_its_reason_and_date(setup) -> None:
    docs_dir, mcp_module = setup
    path = _write_plan(docs_dir, _unlocked())

    started = datetime.now(tz=UTC)
    result = _withdraw(
        mcp_module,
        0,
        key="transport",
        reason=REASON,
        by="Simon McIntosh",
    )
    assert result["ok"] is True, result

    decision = read_state_file(path)["decisions"]["transport"]
    assert decision["rationale"] == REASON
    assert decision["by"] == "Simon McIntosh"
    assert decision["choice"] == "", "a withdrawal records no choice"
    assert decision["sections"] == [], "a withdrawn decision holds no section"

    recorded = decision["when"]
    assert recorded, "the withdrawal records the date it was made"
    stamp = datetime.fromisoformat(recorded)
    assert started - timedelta(minutes=1) <= stamp <= datetime.now(tz=UTC)


def test_a_locked_decision_cannot_be_withdrawn(setup) -> None:
    docs_dir, mcp_module = setup
    path = _write_plan(docs_dir, _mixed())

    result = _withdraw(mcp_module, 0, key="naming", reason=REASON)

    assert result["ok"] is False, result
    assert result["error"] == "op_error"
    assert "naming" in result["detail"], result
    assert "locked" in result["detail"], result
    state = read_state_file(path)
    assert state["version"] == 0, "a refused withdrawal writes nothing"
    assert state["decisions"]["naming"]["choice"] == "snake"
    assert state["decisions"]["naming"]["rationale"] == (
        "the rest of the corpus is snake_case"
    )


def test_a_withdrawal_with_no_reason_is_refused(setup) -> None:
    docs_dir, mcp_module = setup
    path = _write_plan(docs_dir, _unlocked())

    result = _withdraw(mcp_module, 0, key="transport", by="Simon McIntosh")

    assert result["ok"] is False, result
    assert result["error"] == "op_error"
    assert "transport" in result["detail"], result
    assert "reason" in result["detail"], result
    state = read_state_file(path)
    assert state["version"] == 0, "a refused withdrawal writes nothing"
    assert state["decisions"]["transport"]["rationale"] == ""
    assert parse_plan(path)["dec_open"] == 1


def test_a_withdrawal_naming_a_missing_decision_is_refused(setup) -> None:
    docs_dir, mcp_module = setup
    path = _write_plan(docs_dir, _unlocked())

    result = _withdraw(mcp_module, 0, key="absent", reason=REASON)

    assert result["ok"] is False, result
    assert result["error"] == "op_error"
    assert "absent" in result["detail"], result
    assert read_state_file(path)["version"] == 0


def test_a_withdrawn_decision_cannot_be_accepted_back(setup) -> None:
    docs_dir, mcp_module = setup
    decisions = _unlocked()
    decisions["transport"]["recommended"] = "grpc"
    decisions["transport"]["recommended_by"] = "Simon McIntosh"
    _write_plan(docs_dir, decisions)

    withdrawn = _withdraw(mcp_module, 0, key="transport", reason=REASON)
    assert withdrawn["ok"] is True, withdrawn

    result = mcp_module._edit_plan(
        PROJECT, SLUG, [{"op": "accept", "key": "transport"}], 1
    )

    assert result["ok"] is False, result
    assert "recommendation" in result["detail"], result
    path = docs_dir / "plans" / f"{SLUG}.html"
    assert read_state_file(path)["decisions"]["transport"]["choice"] == ""
