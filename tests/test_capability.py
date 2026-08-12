from __future__ import annotations

import json
from pathlib import Path

import reckon.mcp as mcp_module
from reckon import _plan_html
from reckon._schema import IndexData, PlanState, gen_json_schema
from reckon._store import OpError, apply_ops
from reckon.capability import (
    CAPABILITY_SCHEMA_VERSION,
    capability_request,
    from_legacy_tier,
    match_worker,
)
import pytest


def _worker(
    ident: str,
    capability_class: str,
    *,
    family: str = "local",
    cost: int = 1,
    requirements: dict[str, str] | None = None,
) -> dict:
    return {
        "id": ident,
        "family": family,
        "cost": cost,
        "general_purpose": True,
        "capability": capability_request(
            capability_class,
            requirements=requirements,
        ),
    }


def test_legacy_capability_mapping_is_deterministic():
    expected = {
        "haiku": "routine",
        "sonnet": "general",
        "opus": "orchestrator",
    }
    for legacy, capability_class in expected.items():
        request, diagnostic = from_legacy_tier(legacy)
        assert request == {
            "version": CAPABILITY_SCHEMA_VERSION,
            "class": capability_class,
            "requirements": {},
        }
        assert "legacy tier" in diagnostic


def test_matcher_reports_absent_workers_without_weakening_request():
    request = capability_request("general")
    result = match_worker(request, [], selected_worker_id="requested")
    assert result.worker is None
    assert result.escalation_required
    assert result.fallback == "selected-worker-unavailable"
    assert result.requested == request


def test_matcher_validates_the_explicit_worker_independent_of_advertised_order():
    request = capability_request("routine")
    workers = [_worker("zeta", "general"), _worker("alpha", "general")]
    assert (
        match_worker(request, workers, selected_worker_id="zeta").worker["id"] == "zeta"
    )
    assert (
        match_worker(
            request,
            list(reversed(workers)),
            selected_worker_id="zeta",
        ).worker["id"]
        == "zeta"
    )


def test_matcher_does_not_infer_a_worker_from_family():
    workers = [
        _worker("preferred-routine", "routine", family="preferred"),
        _worker("cross-family-general", "general", family="available"),
    ]
    result = match_worker(
        capability_request("general"),
        workers,
        selected_worker_id="cross-family-general",
    )
    assert result.worker["id"] == "cross-family-general"
    assert not result.escalation_required
    assert result.fallback is None


def test_matcher_does_not_substitute_an_unrequested_worker():
    workers = [_worker("available", "orchestrator")]
    result = match_worker(
        capability_request("general"),
        workers,
        selected_worker_id="missing",
    )
    assert result.worker is None
    assert result.escalation_required
    assert result.fallback == "selected-worker-unavailable"


def test_elevated_risk_retains_orchestrator_and_strict_verification():
    orchestrator = _worker(
        "lead",
        "orchestrator",
        requirements={"risk": "critical", "verification": "strict"},
    )
    workers = [
        _worker(
            "general-worker",
            "general",
            requirements={"risk": "critical", "verification": "strict"},
        ),
        orchestrator,
    ]
    result = match_worker(
        capability_request("routine", requirements={"risk": "elevated"}),
        workers,
        selected_worker_id="lead",
    )
    assert result.worker["id"] == "lead"
    assert result.requested["class"] == "orchestrator"
    assert result.requested["requirements"]["verification"] == "strict"


def test_insufficient_explicit_worker_requires_rerouting():
    workers = [
        _worker("routine-worker", "routine"),
        _worker("general-worker", "general"),
    ]
    result = match_worker(
        capability_request("orchestrator", requirements={"verification": "strict"}),
        workers,
        selected_worker_id="general-worker",
    )
    assert result.worker["id"] == "general-worker"
    assert result.escalation_required
    assert result.fallback == "selected-worker-insufficient"


def test_explicit_selection_ignores_cost_and_stable_id_tiebreakers():
    workers = [
        _worker("routine-worker", "routine", cost=0),
        _worker(
            "expensive",
            "orchestrator",
            cost=9,
            requirements={"verification": "strict"},
        ),
        _worker("zeta", "orchestrator", cost=1),
        _worker("alpha", "orchestrator", cost=1),
    ]
    result = match_worker(
        capability_request(
            "orchestrator",
            requirements={"verification": "strict"},
        ),
        workers,
        selected_worker_id="expensive",
    )
    assert result.worker["id"] == "expensive"
    assert not result.escalation_required
    assert result.fallback is None


def test_capability_applies_to_plan_followup_and_sprint_item_schema():
    request = capability_request(
        "general",
        requirements={"reasoning": "deep", "verification": "strict"},
    )
    state = PlanState.model_validate(
        {
            "project": "sample",
            "slug": "work",
            "title": "Work",
            "status": "active",
            "capability": request,
            "followups": [
                {
                    "id": "next",
                    "prompt": "dispatch",
                    "capability": request,
                }
            ],
        }
    ).validate_for_write()
    assert state.capability.capability_class == "general"
    assert state.followups[0].capability.requirements.verification == "strict"

    index = IndexData.model_validate(
        {
            "sprints": [
                {"id": "current", "items": [{"slug": "work", "capability": request}]}
            ]
        }
    )
    assert index.sprints[0].items[0].capability.requirements.reasoning == "deep"
    schema = gen_json_schema()
    assert schema["schemaVersion"] == "3.0"
    assert schema["properties"]["capability"]


def test_legacy_html_maps_on_read_and_explicit_edit_migrates():
    html = """<!doctype html>
<html><head>
<meta name="docs-project" content="sample">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="work">
<meta name="plan-title" content="Work">
<meta name="plan-status" content="active">
<meta name="plan-tier" content="sonnet">
<title>Work</title>
</head><body><main></main></body></html>"""
    state = _plan_html.read_state(html)
    assert state["capability"]["class"] == "general"
    assert state["compatibility_warnings"]

    state.pop("tier")
    state.pop("compatibility_warnings")
    rendered = _plan_html.write_state(html, state)
    assert "plan-tier" not in rendered
    assert 'name="plan-capability-version" content="1.0"' in rendered
    assert 'name="plan-capability-class" content="general"' in rendered
    assert not _plan_html.read_state(rendered).get("compatibility_warnings")


def _effort_html(*metas: str) -> str:
    return "<html><head>" + "".join(metas) + "</head><body></body></html>"


def test_explicit_hours_win_and_report_redundant_legacy_letter():
    state = _plan_html.read_state(
        _effort_html(
            '<meta name="plan-effort-hours" content="5.25">',
            '<meta name="plan-effort" content="XL">',
        )
    )

    assert state["effort_hours"] == 5.25
    assert state["effort_calibrated"] is True
    assert any("redundant" in warning for warning in state["compatibility_warnings"])


@pytest.mark.parametrize(
    ("letter", "hours"),
    [("S", 1.0), ("M", 2.0), ("L", 4.0), ("XL", 8.0)],
)
def test_legacy_letter_resolves_to_uncalibrated_worker_hours(letter, hours):
    state = _plan_html.read_state(
        _effort_html(f'<meta name="plan-effort" content="{letter}">')
    )

    assert state["effort_hours"] == hours
    assert state["effort_calibrated"] is False
    assert any("uncalibrated" in warning for warning in state["compatibility_warnings"])


def test_plan_without_either_effort_field_reads_unchanged():
    state = _plan_html.read_state(_effort_html())

    assert "effort" not in state
    assert "effort_hours" not in state
    assert "effort_calibrated" not in state
    assert "compatibility_warnings" not in state


def test_setting_worker_hours_is_supported_by_plan_ops():
    working = {
        "effort": "M",
        "effort_hours": 2.0,
        "effort_calibrated": False,
        "decisions": {},
        "followups": [],
    }

    warnings = apply_ops(
        working,
        [{"op": "set", "path": "effort_hours", "value": 2.75}],
        is_index=False,
    )

    assert working["effort_hours"] == 2.75
    assert working["effort_calibrated"] is True
    assert warnings == [
        "legacy effort letter is redundant because explicit worker-hours win"
    ]


def test_capability_edit_removes_legacy_field_with_warning():
    working = {
        "tier": "sonnet",
        "decisions": {},
        "followups": [],
        "questions": [],
        "research": [],
        "comments": {},
    }
    warnings = apply_ops(
        working,
        [
            {
                "op": "set",
                "path": "capability",
                "value": capability_request("general"),
            }
        ],
        is_index=False,
    )
    assert "tier" not in working
    assert working["capability"]["class"] == "general"
    assert warnings == ["legacy tier removed because capability was set explicitly"]


def test_normal_plan_write_migrates_mapped_legacy_capability():
    working = {
        "project": "sample",
        "type": "plan",
        "slug": "work",
        "title": "Work",
        "status": "active",
        "tier": "sonnet",
        "capability": capability_request("general"),
        "decisions": {},
        "followups": [],
        "questions": [],
        "research": [],
        "comments": {},
        "compatibility_warnings": ["legacy input"],
    }
    assert mcp_module._validate_working("work", working) is None
    assert "tier" not in working
    assert "compatibility_warnings" not in working


def test_normal_index_write_migrates_legacy_sprint_capability():
    working = {
        "sprints": [
            {
                "id": "current",
                "items": [{"slug": "work", "tier": "sonnet"}],
            }
        ]
    }
    assert mcp_module._validate_working("index", working) is None
    item = working["sprints"][0]["items"][0]
    assert item["capability"]["class"] == "general"
    assert "tier" not in item


@pytest.mark.parametrize(
    ("target", "item"),
    [
        (
            "followups",
            {
                "id": "next",
                "written_by": "worker",
                "written_at": "today",
                "title": "Continue",
                "body": "Work",
                "prompt": "dispatch",
                "tier": "sonnet",
            },
        ),
        ("sprints.current.items", {"slug": "work", "tier": "sonnet"}),
    ],
)
def test_new_mcp_items_reject_legacy_tier(target, item):
    working = (
        {"sprints": [{"id": "current", "items": []}]}
        if target.startswith("sprints.")
        else {"followups": []}
    )
    with pytest.raises(OpError, match="must use capability"):
        apply_ops(
            working,
            [{"op": "append", "target": target, "item": item}],
            is_index=target.startswith("sprints."),
        )


def test_index_read_maps_legacy_sprint_item(monkeypatch):
    monkeypatch.setattr(
        mcp_module,
        "read_plan",
        lambda project, slug, root=None: (
            {
                "sprints": [
                    {
                        "id": "current",
                        "items": [{"slug": "work", "tier": "sonnet"}],
                    }
                ]
            },
            4,
        ),
    )
    result = mcp_module._read_plan("sample", "index")
    item = result["data"]["sprints"][0]["items"][0]
    assert item["capability"]["class"] == "general"
    assert result["data"]["compatibility_warnings"]


def test_audit_reports_legacy_capability_diagnostic(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "work.html").write_text(
        """<!doctype html><html><head>
<meta name="docs-project" content="sample">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="work">
<meta name="plan-title" content="Work">
<meta name="plan-status" content="active">
<meta name="plan-tier" content="sonnet">
<title>Work</title></head><body><main></main></body></html>"""
    )
    result = mcp_module._audit("sample", checkout_path=str(tmp_path))
    assert any(
        finding["code"] == "legacy-capability-tier" for finding in result["findings"]
    )


def test_static_ui_uses_capability_fields():
    root = Path(__file__).resolve().parent.parent
    loader = (root / "docs/ui/state-loader.js").read_text()
    state_api = (root / "docs/_shared/state.js").read_text()
    schema = json.loads((root / "docs/_shared/plan.schema.json").read_text())
    assert "mapLegacyCapability" in loader
    assert "getCapability" in state_api
    assert "getTier" not in state_api
    assert "capability" in schema["properties"]
