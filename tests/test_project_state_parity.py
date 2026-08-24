"""Marker-resolved, field-level project-state preservation tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from reckon import _plan_html
from reckon.project_state import (
    ProjectStateError,
    migrate_project_state,
    read_resource,
    write_resource,
)
from reckon.project_state_parity import (
    EXCLUDED_OBSERVATIONS,
    NESTED_FIELDS,
    ORGANISATIONAL_FIELDS,
    RELATIONAL_FIELDS,
    compare_project_state,
    main,
    resolve_frozen_snapshot,
)
from reckon.tags import normalise_tag

LONG_RATIONALE = (
    "ITER & NOVA retain <entity-bearing> rationale text exactly; "
    + "the preservation comparison must not truncate this sentence. " * 18
    + "TAIL-SENTINEL"
)
PLAN_TAG_SPELLINGS = ["Standard_Names", "Plasma Control"]
SPRINT_TAG_SPELLINGS = ["Fleet Migration", "Shared_State"]
PLAN_TAGS = [normalise_tag(tag) for tag in PLAN_TAG_SPELLINGS]
SPRINT_TAGS = [normalise_tag(tag) for tag in SPRINT_TAG_SPELLINGS]


def _render_plan(slug: str, *, relations: bool) -> str:
    bare = (
        "<!doctype html><html><head>"
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{slug}">'
        f"<title>{slug}</title></head><body><main></main></body></html>"
    )
    state = {
        "type": "plan",
        "slug": slug,
        "title": slug.title(),
        "status": "active",
        "sprint": "current",
        "milestone": "launch",
        "north_star": "reliable-state",
        "tier": "opus",
        "decisions": {
            "storage": {
                "title": "Where is state stored?",
                "context": "Preserve context.",
                "choices": ["typed", "aggregate"],
                "option_labels": {"typed": "Typed", "aggregate": "Aggregate"},
                "choice": "typed",
                "rationale": LONG_RATIONALE,
                "by": "owner",
                "when": "2026-08-24",
            }
        },
        "followups": [
            {
                "id": "followup-preserve",
                "status": "open",
                "written_by": "owner",
                "written_at": "2026-08-24",
                "recommends_skill": "/reckon-ship sample",
                "title": "Preserve fields",
                "body": "Compare the exact values.",
                "prompt": "/reckon-ship sample",
            }
        ],
        "comments": {
            "storage": [
                {
                    "id": "comment-preserve",
                    "who": "reviewer",
                    "when": "2026-08-24",
                    "quote": "exact",
                    "body": "Keep this comment.",
                }
            ]
        },
        "questions": [
            {
                "id": "question-preserve",
                "section": "storage",
                "opened_by": "owner",
                "opened_at": "2026-08-24",
                "body": "Is the comparison exact?",
                "resolution": None,
                "resolved_at": None,
                "resolved_by": None,
            }
        ],
        "research": [
            {
                "id": "research-preserve",
                "type": "measurement",
                "title": "Corpus measurement",
                "source": "local",
                "added_by": "owner",
                "when": "2026-08-24",
                "url": None,
            }
        ],
    }
    if relations:
        state.update(
            {
                "tags": PLAN_TAG_SPELLINGS,
                "depends_on": ["foundation"],
                "blocks": ["consumer"],
                "informs": ["design"],
                "evidence_for": ["claim"],
                "verifies": ["contract#preservation"],
                "supersedes": ["earlier-design"],
            }
        )
    return _plan_html.write_state(bare, state)


def _legacy_index(docs: Path, *, include_plan_inventory: bool) -> Path:
    plan_states = []
    for slug in ("foundation", "contract"):
        path = docs / "plans" / f"{slug}.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            _render_plan(slug, relations=slug == "contract"), encoding="utf-8"
        )
        plan_states.append(_plan_html.read_state(path.read_text(encoding="utf-8")))

    data = {
        "_version": 7,
        "active_sprint_id": "current",
        "projects": [{"project": "sample", "owner": "owner"}],
        "sprints": [
            {
                "id": "current",
                "theme": "Preservation",
                "status": "active",
                "tags": SPRINT_TAGS,
                "items": [
                    {
                        "slug": "contract",
                        "blocked_by": ["resolved-external"],
                        "milestone": "launch",
                        "tier": "opus",
                        "north_star": "reliable-state",
                        "why_now": "State must remain attributable.",
                        "done_when": "Every authored value is identical.",
                    },
                    {
                        "slug": "foundation",
                        "milestone": "launch",
                        "tier": "opus",
                        "north_star": "reliable-state",
                        "why_now": "The dependency is ready.",
                        "done_when": "The supporting plan remains present.",
                    },
                ],
            }
        ],
        "milestones": [
            {
                "id": "launch",
                "name": "Launch",
                "status": "active",
                "depends_on": [],
            }
        ],
        "blockers": [
            {
                "id": "resolved-external",
                "summary": "External condition",
                "owner": "operator",
                "next": "Resolve the condition",
            }
        ],
        "timeline": [{"when": "2026-08-24", "who": "owner", "what": "Started"}],
    }
    if include_plan_inventory:
        data["inventory"] = plan_states
    path = docs / "state" / "sample" / "index.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "updated": "2026-08-24T00:00:00",
                "project": "sample",
                "doc": "index",
                "data": data,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _migrated_corpus(
    tmp_path: Path, *, include_plan_inventory: bool = True
) -> tuple[Path, dict[Path, bytes]]:
    docs = tmp_path / "docs"
    docs.mkdir()
    _legacy_index(docs, include_plan_inventory=include_plan_inventory)
    plan_bytes = {
        path: path.read_bytes() for path in sorted((docs / "plans").glob("*.html"))
    }
    migrate_project_state(docs, "sample")
    return docs, plan_bytes


def test_migration_preserves_every_plan_byte_and_every_evidenced_field(tmp_path: Path):
    docs, plan_bytes = _migrated_corpus(tmp_path)

    assert {path: path.read_bytes() for path in plan_bytes} == plan_bytes
    report = compare_project_state(docs)

    assert report["ok"] is True
    assert report["totals"]["mismatches"] == 0
    assert report["totals"]["out_of_corpus"] == 0
    assert report["excluded_observations"] == list(EXCLUDED_OBSERVATIONS)
    for field in (
        *RELATIONAL_FIELDS,
        *ORGANISATIONAL_FIELDS,
        "sprint_items",
        "why_now",
        "done_when",
        *NESTED_FIELDS,
    ):
        evidence = report["fields"][field]
        assert evidence["status"] == "matched", field
        assert evidence["compared"] > 0, field
        assert evidence["matched"] == evidence["compared"], field
    assert report["fields"]["tags"]["compared"] == 2
    assert report["fields"]["tags"]["matched"] == 2

    newcomer = docs / "plans" / "newcomer.html"
    newcomer.write_text(_render_plan("newcomer", relations=True), encoding="utf-8")
    augmented = compare_project_state(docs)
    for field in RELATIONAL_FIELDS:
        evidence = augmented["fields"][field]
        assert evidence["status"] == "matched"
        assert evidence["compared"] == 1
        assert evidence["matched"] == 1
        assert evidence["additional"] == 1

    sprint, version = read_resource(docs, "sample", "sprint", "current")
    sprint["items"][0].pop("blocked_by")
    write_resource(docs, "sample", "sprint", "current", sprint, version)
    drifted = compare_project_state(docs)
    sprint_items = drifted["fields"]["sprint_items"]
    assert drifted["ok"] is True
    assert drifted["totals"]["current_state_drift"] == 1
    assert sprint_items["status"] == "preserved-with-current-state-drift"
    assert sprint_items["compared"] == 2
    assert sprint_items["matched"] == 1
    assert sprint_items["current_state_drift"] == 1
    assert sprint_items["drift_details"] == [
        {
            "path": "sprint:current/item:contract",
            "field": "blocked_by",
            "before": ["resolved-external"],
            "after": "<missing>",
        }
    ]

    path = docs / "plans" / "contract.html"
    current = _plan_html.read_state(path.read_text(encoding="utf-8"))
    current["decisions"]["storage"]["rationale"] = LONG_RATIONALE[:-1] + "!"
    path.write_text(
        _plan_html.write_state(path.read_text(encoding="utf-8"), current),
        encoding="utf-8",
    )
    changed = compare_project_state(docs)
    decision_mismatch = changed["fields"]["decisions"]["mismatches"][0]
    assert decision_mismatch["before"]["storage"]["rationale"] == LONG_RATIONALE
    assert decision_mismatch["after"]["storage"]["rationale"].endswith("!")


def test_stored_tag_removal_and_reordering_are_mismatches(tmp_path: Path):
    docs, _ = _migrated_corpus(tmp_path)
    plan_state = _plan_html.read_state(
        (docs / "plans" / "contract.html").read_text(encoding="utf-8")
    )
    assert plan_state["tags"] == PLAN_TAGS

    sprint, version = read_resource(docs, "sample", "sprint", "current")
    assert sprint["tags"] == SPRINT_TAGS
    sprint["tags"] = SPRINT_TAG_SPELLINGS
    write_resource(docs, "sample", "sprint", "current", sprint, version)
    canonicalised = compare_project_state(docs)["fields"]["tags"]
    assert canonicalised["status"] == "matched"
    assert canonicalised["compared"] == 2
    assert canonicalised["matched"] == 2

    sprint, version = read_resource(docs, "sample", "sprint", "current")
    sprint["tags"] = SPRINT_TAGS[:-1]
    write_resource(docs, "sample", "sprint", "current", sprint, version)
    removed = compare_project_state(docs)["fields"]["tags"]
    assert removed["status"] == "mismatch"
    assert removed["additional"] == 0
    assert removed["mismatches"] == [
        {
            "path": "sprints[current].tags",
            "before": SPRINT_TAGS,
            "after": SPRINT_TAGS[:-1],
        }
    ]

    sprint, version = read_resource(docs, "sample", "sprint", "current")
    sprint["tags"] = list(reversed(SPRINT_TAGS))
    write_resource(docs, "sample", "sprint", "current", sprint, version)
    reordered = compare_project_state(docs)["fields"]["tags"]
    assert reordered["status"] == "mismatch"
    assert reordered["additional"] == 0
    assert reordered["mismatches"] == [
        {
            "path": "sprints[current].tags",
            "before": SPRINT_TAGS,
            "after": list(reversed(SPRINT_TAGS)),
        }
    ]


def test_missing_historical_plan_state_is_out_of_corpus(tmp_path: Path):
    docs, _ = _migrated_corpus(tmp_path, include_plan_inventory=False)

    report = compare_project_state(docs)

    assert report["ok"] is True
    for field in (*RELATIONAL_FIELDS, *NESTED_FIELDS):
        evidence = report["fields"][field]
        assert evidence["status"] == "out-of-corpus"
        assert evidence["compared"] == 0
        assert evidence["matched"] == 0
        assert evidence["mismatches"] == []
    assert report["fields"]["sprint"]["compared"] == 2
    assert report["fields"]["sprint_items"]["compared"] == 2


def test_exact_plan_field_change_is_reported_with_path(tmp_path: Path):
    docs, _ = _migrated_corpus(tmp_path)
    path = docs / "plans" / "contract.html"
    current = _plan_html.read_state(path.read_text(encoding="utf-8"))
    path.write_text(
        _plan_html.write_state(
            path.read_text(encoding="utf-8"),
            {**current, "informs": ["changed-design"]},
        ),
        encoding="utf-8",
    )

    report = compare_project_state(docs)

    assert report["ok"] is False
    assert report["totals"]["mismatches"] == 1
    assert report["fields"]["informs"] == {
        "status": "mismatch",
        "compared": 1,
        "matched": 0,
        "additional": 0,
        "current_state_drift": 0,
        "drift_details": [],
        "mismatches": [
            {
                "path": "plan:contract",
                "before": ["design"],
                "after": ["changed-design"],
            }
        ],
    }


def test_snapshot_resolution_rejects_hash_mismatch_and_path_escape(tmp_path: Path):
    docs, _ = _migrated_corpus(tmp_path)
    frozen = resolve_frozen_snapshot(docs)
    frozen.snapshot_path.write_bytes(b"changed")

    with pytest.raises(ProjectStateError, match="snapshot hash mismatch"):
        resolve_frozen_snapshot(docs)

    marker = json.loads(frozen.marker_path.read_text(encoding="utf-8"))
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    marker["snapshot"] = "../outside.json"
    marker["snapshot_sha256"] = hashlib.sha256(outside.read_bytes()).hexdigest()
    frozen.marker_path.write_text(json.dumps(marker), encoding="utf-8")
    with pytest.raises(ProjectStateError, match="escapes docs directory"):
        resolve_frozen_snapshot(docs)


def test_module_entrypoint_reports_json_and_uses_marker_project(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    docs, _ = _migrated_corpus(tmp_path)

    exit_code = main([str(docs), "--indent", "0"])
    report = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert report["project"] == "sample"
    assert report["ok"] is True
