"""Promotion returns a bounded present-tense reading of its project fleet."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from reckon import _plan_html, crew
from reckon.crew import promotion, recovery
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "beat-project"
PLAN = "beat-plan"
QUOTA_OBSERVED_AT = "2030-01-02T03:04:05Z"
FLEET_OBSERVED_AT = "2030-01-02T03:04:06Z"


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path, home: Path) -> Path:
    root = tmp_path / "repository"
    state = root / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    plan_path = root / "docs" / "plans" / f"{PLAN}.html"
    plan_path.parent.mkdir(parents=True)
    document = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    plan_path.write_text(
        _plan_html.write_state(
            document,
            {
                "type": "plan",
                "slug": PLAN,
                "title": "Fleet beat plan",
                "status": "active",
                "version": 0,
                "comments": {},
            },
        ),
        encoding="utf-8",
    )
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.name", "Test User"),
        ("config", "user.email", "test@example.invalid"),
        ("add", "docs"),
        ("commit", "-q", "-m", "test: seed repository"),
    ):
        _git(root, *arguments)
    return root


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    return _repository(tmp_path, home)


def _write_manifest(path: Path, status: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"status: {status}\n", encoding="utf-8")
    return path


def _write_pointer(
    repository: Path,
    run_id: str,
    *,
    backend: str,
    status: str | None = None,
) -> None:
    manifest = (
        _write_manifest(repository / "manifests" / f"{run_id}.md", status)
        if status is not None
        else repository / "manifests" / f"{run_id}.md"
    )
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "base_sha": _git(repository, "rev-parse", "HEAD"),
            "launch": "in-harness",
            "role": "implement",
            "backend": backend,
            "created_at": "2030-01-02T03:00:00Z",
            "manifest_path": str(manifest),
            "node": {
                "id": f"node-{run_id}",
                "plan": PLAN,
                "section": "fleet",
                "time_budget": "20m",
                "write_paths": [],
            },
        },
    )


def _promote(repository: Path, run_id: str = "r-landing") -> dict[str, Any]:
    _write_pointer(repository, run_id, backend="landing")
    return crew.complete(
        run_id,
        gate="passed",
        completed_at=QUOTA_OBSERVED_AT,
        root=repository,
    )


def _stored_row(repository: Path, run_id: str) -> dict[str, Any]:
    data = json.loads(
        (repository / "docs" / "state" / PROJECT / "crew.json").read_text(
            encoding="utf-8"
        )
    )
    rows = [row for row in data["data"]["runs"] if row["run_id"] == run_id]
    assert len(rows) == 1
    return rows[0]


def _all_values(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [str(key) for key in value] + [
            item for nested in value.values() for item in _all_values(nested)
        ]
    if isinstance(value, list):
        return [item for nested in value for item in _all_values(nested)]
    return [str(value)]


def test_promotion_carries_a_bounded_fleet_reading_without_changing_the_ledger(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fleet = [
        ("r-working", "alpha", None),
        ("r-blocked", "alpha", "blocked"),
        ("r-delivered", "beta", "complete"),
    ]
    for run_id, backend, status in fleet:
        _write_pointer(repository, run_id, backend=backend, status=status)
    monkeypatch.setattr(promotion, "_utc_now", lambda: FLEET_OBSERVED_AT)

    result = _promote(repository)
    beat = result["fleet_state"]
    stored = _stored_row(repository, "r-landing")

    assert beat["fleet_state"] == "measured"
    assert beat["observed_at"] == FLEET_OBSERVED_AT
    assert result["lane_receipt"]["observed_at"] == QUOTA_OBSERVED_AT
    assert beat["observed_at"] != result["lane_receipt"]["observed_at"]
    assert beat["live_runs"] == len(fleet)
    assert beat["unreconciled_runs"] == len(fleet)
    assert beat["occupied_lanes"] == len({backend for _run, backend, _status in fleet})
    assert beat["actionable_runs"] == 1
    assert beat["actionable_classifications"] == ["blocked"]
    assert "fleet_state" not in result["record"]
    assert "fleet_state" not in stored
    assert stored == result["record"]

    ledger_path = repository / "docs" / "state" / PROJECT / "crew.json"
    ledger_before_read = ledger_path.read_bytes()
    assert promotion._fleet_state_reading(PROJECT)["fleet_state"] == "measured"
    assert ledger_path.read_bytes() == ledger_before_read

    forbidden = ("recommended", "preferred", "ready", "suggested")
    assert not any(
        term in value.casefold() for value in _all_values(beat) for term in forbidden
    )


def test_the_fleet_reading_has_a_constant_shape_for_small_and_large_fleets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reading_for(count: int) -> dict[str, Any]:
        pointers = [
            {
                "run_id": f"r-{index}",
                "project": PROJECT,
                "phase": "working",
                "backend": "alpha",
            }
            for index in range(count)
        ]
        monkeypatch.setattr(promotion, "list_live", lambda *, project: pointers)
        monkeypatch.setattr(
            promotion,
            "drain",
            lambda project: {"unreconciled_runs": len(pointers)},
        )
        monkeypatch.setattr(
            recovery,
            "classify_pointer",
            lambda pointer: {"recovery_classification": "running"},
        )
        return promotion._fleet_state_reading(PROJECT)

    one = reading_for(1)
    many = reading_for(25)

    assert set(one) == set(many)
    assert one["live_runs"] == 1
    assert many["live_runs"] == 25
    assert one["unreconciled_runs"] == 1
    assert many["unreconciled_runs"] == 25
    assert {
        key: len(value) for key, value in one.items() if isinstance(value, list)
    } == {key: len(value) for key, value in many.items() if isinstance(value, list)}


def test_an_unavailable_fleet_reading_does_not_block_promotion(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreadable(*, project: str) -> list[dict[str, Any]]:
        raise OSError("fleet pointers are temporarily unreadable")

    monkeypatch.setattr(promotion, "list_live", unreadable)

    result = _promote(repository, "r-unmeasured")
    stored = _stored_row(repository, "r-unmeasured")

    assert result["pointer_removed"] is True
    assert result["record"]["gate"] == "passed"
    assert result["fleet_state"] == {
        "fleet_state": "unmeasured",
        "observed_at": result["fleet_state"]["observed_at"],
        "unmeasured": {"fleet_state": "unavailable"},
    }
    assert "fleet_state" not in stored
