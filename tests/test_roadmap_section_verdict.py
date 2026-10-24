from __future__ import annotations

import pytest

from reckon.crew.plan_review import _readiness_by_slug, project_section_readiness
from reckon.roadmap import build_roadmap
from tests.roadmap_fixtures import plan_row


def _row(*, waiting: dict | None = None, upstream: dict | None = None) -> dict:
    consumer = plan_row(
        "consumer",
        section_declarations={"s1": "implementable", "s2": "implementable"},
    )
    consumer.update(waiting or {})
    inventory = [consumer]
    if upstream is not None:
        inventory.insert(0, upstream)
    report = build_roadmap("sample", inventory, [])
    return next(row for row in report["pending_work"] if row["slug"] == "consumer")


@pytest.mark.parametrize(
    ("waiting", "upstream", "held_section", "expected_kind"),
    [
        (
            {"depends_on": ["upstream#s1"]},
            plan_row("upstream", section_declarations={"s1": "implementable"}),
            "s1",
            "dependency",
        ),
        (
            {"section_depends_on": {"s2": ["upstream#s1"]}},
            plan_row("upstream", section_declarations={"s1": "implementable"}),
            "s2",
            "dependency",
        ),
        (
            {
                "decisions": [{"key": "choice", "title": "Choose", "choice": ""}],
                "decision_sections": {"choice": ["s2"]},
            },
            None,
            "s2",
            "decision",
        ),
        (
            {"gates": [{"id": "measure", "status": "open", "gated_sections": ["s2"]}]},
            None,
            "s2",
            "gate",
        ),
        (
            {
                "gates": [
                    {
                        "id": "measure",
                        "status": "open",
                        "gated_sections": ["s1", "s2"],
                    }
                ]
            },
            None,
            "s2",
            "gate",
        ),
        (
            {"depends_on": ["upstream"]},
            plan_row("upstream", section_declarations={"s1": "implementable"}),
            "s2",
            "upstream",
        ),
    ],
)
def test_section_verdict_includes_every_hold(
    waiting: dict, upstream: dict | None, held_section: str, expected_kind: str
) -> None:
    row = _row(waiting=waiting, upstream=upstream)
    verdict = row["section_verdicts"][held_section]

    assert verdict["ready"] is False
    assert verdict["blockers"]
    assert any(expected_kind in str(blocker).lower() for blocker in verdict["blockers"])
    for item in row.get("section_readiness", []):
        assert item["ready"] is row["section_verdicts"][item["section"]]["ready"]


def test_unheld_section_stays_ready_beside_scoped_decision() -> None:
    row = _row(
        waiting={
            "decisions": [{"key": "choice", "title": "Choose", "choice": ""}],
            "decision_sections": {"choice": ["s2"]},
        }
    )

    assert row["section_verdicts"]["s1"] == {
        "section": "s1",
        "ready": True,
        "blockers": [],
    }
    assert row["section_verdicts"]["s2"]["ready"] is False


def test_unheld_sections_are_ready() -> None:
    row = _row()

    assert all(verdict["ready"] for verdict in row["section_verdicts"].values())
    assert set(row["section_verdicts"]) == {"s1", "s2"}


def test_project_read_reuses_section_verdicts(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from reckon import mcp_discovery

    inventory = [
        plan_row(
            "consumer",
            section_declarations={"s1": "implementable", "s2": "implementable"},
            gates=[{"id": "measure", "status": "open", "gated_sections": ["s2"]}],
        )
    ]
    monkeypatch.setattr(
        mcp_discovery,
        "_discover_project",
        lambda project, root: {
            "inventory": inventory,
            "sprints": [],
            "active_sprint_id": None,
        },
    )
    monkeypatch.setattr(mcp_discovery, "_inventory_row", lambda item: item)

    result = project_section_readiness("sample", tmp_path)

    assert result["consumer"]["sections"]["s2"]["ready"] is False
    assert result["consumer"]["sections"]["s1"]["ready"] is True
    assert _readiness_by_slug("sample", tmp_path) == {"consumer": "ready"}
