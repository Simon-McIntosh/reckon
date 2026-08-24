"""Promotion records narrative and measurements at their durable homes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import _plan_html, _store, crew, ledger
from reckon.crew.runs import _write_json, pointer_path


PROJECT = "proj"
PLAN = "plan-a"


def _write_resource(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{state['slug']}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    _write_resource(
        root / "docs" / "plans" / f"{PLAN}.html",
        {
            "type": "plan",
            "slug": PLAN,
            "title": "Plan A",
            "status": "active",
            "version": 0,
            "comments": {},
        },
    )
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def test_promotion_splits_narrative_from_run_measurements(repository: Path) -> None:
    run_id = "r-20260824T190800000000-node-a"
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "launch": "in-harness",
            "role": "implement",
            "member": "worker-a",
            "backend": "native",
            "created_at": "2026-08-24T19:08:00Z",
            "manifest_path": "/durable/manifest.md",
            "node": {
                "id": "node-a",
                "plan": PLAN,
                "section": "§2",
                "time_budget": "35m",
                "write_paths": [],
            },
        },
    )

    narrative = "The shared write boundary now refuses an unclaimed closure."
    promoted = crew.complete(
        run_id,
        gate="passed",
        outcome=narrative,
        tests_added=23,
        completed_at="2026-08-24T19:10:17Z",
        root=repository,
    )

    plan, _version = _store.read_plan(
        PROJECT, PLAN, repository, artifact_type="plan"
    )
    comment = plan["comments"]["s2"][0]
    assert promoted["plan_comment"] == {
        "recorded": True,
        "comment_id": comment["id"],
        "section": "s2",
        "already_recorded": False,
    }
    assert narrative in comment["body"]
    assert "23" not in comment["body"]
    assert "137" not in comment["body"]

    run = ledger.load(PROJECT, repository)[0]["runs"][0]
    assert run["tests_added"] == 23
    assert run["wall_seconds"] == 137
    assert run["gate"] == "passed"
    assert run["outcome"] == ""
    assert narrative not in json.dumps(run)
    assert not (repository / "docs" / "evidence").exists()


def test_terminal_write_requires_a_back_linking_evidence_record(
    repository: Path,
) -> None:
    plan, version = _store.read_plan(
        PROJECT, PLAN, repository, artifact_type="plan"
    )
    with pytest.raises(_store.OpError) as excinfo:
        _store.write_plan(
            PROJECT,
            PLAN,
            {**plan, "status": "done"},
            version,
            repository,
            artifact_type="plan",
        )

    detail = str(excinfo.value)
    assert f"docs/evidence/archive/{PLAN}-landed.html" in detail
    assert f'plan-evidence-for" content="{PLAN}' in detail
    with pytest.raises(_store.OpError, match=f"{PLAN}-landed.html"):
        _store.validate_landing_patch(
            {**plan, "status": "done"}, {"status": "done"}
        )
    refused, refused_version = _store.read_plan(
        PROJECT, PLAN, repository, artifact_type="plan"
    )
    assert refused["status"] == "active"
    assert refused_version == version

    evidence_path = (
        repository / "docs" / "evidence" / "archive" / f"{PLAN}-landed.html"
    )
    _write_resource(
        evidence_path,
        {
            "type": "evidence",
            "slug": f"{PLAN}-landed",
            "title": "Plan A execution evidence",
            "evidence_for": [PLAN],
            "version": 0,
        },
    )
    assert ledger.evidence_records_for_plan(PROJECT, PLAN, repository) == [
        evidence_path
    ]

    new_version = _store.write_plan(
        PROJECT,
        PLAN,
        {**refused, "status": "done"},
        refused_version,
        repository,
        artifact_type="plan",
    )
    terminal, stored_version = _store.read_plan(
        PROJECT, PLAN, repository, artifact_type="plan"
    )
    assert new_version == stored_version == refused_version + 1
    assert terminal["status"] == "done"
