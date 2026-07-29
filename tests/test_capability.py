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
    result = match_worker(request, [])
    assert result.worker is None
    assert result.escalation_required
    assert result.fallback == "inline-no-advertised-worker"
    assert result.requested == request


def test_matcher_is_deterministic_for_equal_workers():
    request = capability_request("routine")
    workers = [_worker("zeta", "general"), _worker("alpha", "general")]
    assert match_worker(request, workers).worker["id"] == "alpha"
    assert match_worker(request, list(reversed(workers))).worker["id"] == "alpha"


def test_one_below_prefers_immediately_lower_capability():
    orchestrator = _worker("lead", "orchestrator")
    workers = [
        orchestrator,
        _worker("routine-worker", "routine"),
        _worker("general-worker", "general"),
    ]
    result = match_worker(
        capability_request("routine"),
        workers,
        orchestrator=orchestrator,
        policy="one-below",
    )
    assert result.worker["id"] == "general-worker"
    assert result.requested["class"] == "general"


def test_one_below_inherits_orchestrator_when_no_lower_worker_exists():
    orchestrator = _worker("lead", "orchestrator")
    result = match_worker(
        capability_request("routine"),
        [orchestrator],
        orchestrator=orchestrator,
        policy="one-below",
    )
    assert result.worker["id"] == "lead"
    assert result.reasoning_adjustment == "decrease-one-supported-level"


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
        orchestrator=orchestrator,
        policy="one-below",
    )
    assert result.worker["id"] == "lead"
    assert result.requested["class"] == "orchestrator"
    assert result.requested["requirements"]["verification"] == "strict"


def test_unsatisfied_request_uses_strongest_worker_with_escalation_signal():
    workers = [
        _worker("routine-worker", "routine"),
        _worker("general-worker", "general"),
    ]
    result = match_worker(
        capability_request("orchestrator", requirements={"verification": "strict"}),
        workers,
    )
    assert result.worker["id"] == "general-worker"
    assert result.escalation_required
    assert result.fallback == "strongest-advertised-worker"


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
