"""An unmeasured fleet reading names the exception that produced it."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from reckon import _plan_html, crew, ledger
from reckon.crew import promotion, promotion_release, recovery
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "cause-project"
PLAN = "cause-plan"
COMPLETED_AT = "2030-01-02T03:04:05Z"


class _PointerStepError(RuntimeError):
    """A distinctive failure a reader can recognise by its type name."""


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
                "title": "Cause plan",
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


def _write_pointer(repository: Path, run_id: str) -> None:
    manifest = repository / "manifests" / f"{run_id}.md"
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
            "backend": "landing",
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


def _raise_from_the_pointer_step(
    message: str, monkeypatch: pytest.MonkeyPatch
) -> _PointerStepError:
    """Make the per-pointer step raise, so the reading cannot be composed.

    The reading derives its unreconciled count from the closure drain's
    per-pointer step, so forcing that step to raise is what drives the reading
    to unmeasured. A benign classification stub keeps the raise on the step the
    test names rather than on the pointer classifier that runs first.
    """
    failure = _PointerStepError(message)
    monkeypatch.setattr(
        promotion_release,
        "list_live",
        lambda *, project: [
            {
                "run_id": "r-1",
                "project": PROJECT,
                "phase": "working",
                "backend": "alpha",
            }
        ],
    )
    monkeypatch.setattr(
        recovery,
        "classify_pointer",
        lambda pointer: {"recovery_classification": "running"},
    )

    def raising(pointer: Any) -> dict[str, Any]:
        raise failure

    monkeypatch.setattr(promotion_release, "_drain_row", raising)
    return failure


def _raise_from_the_pointer_listing(
    message: str, monkeypatch: pytest.MonkeyPatch
) -> _PointerStepError:
    """Make the pointer listing raise, so no pointer is read before the failure.

    This is the pre-pointer path: the reading fails before it reaches the
    per-pointer step, and it must still name the exception rather than fall back
    to a bare absence.
    """
    failure = _PointerStepError(message)

    def raising(*, project: str) -> list[dict[str, Any]]:
        raise failure

    monkeypatch.setattr(promotion_release, "list_live", raising)
    return failure


def test_an_unmeasured_reading_names_the_exception_type_and_message(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failure = _raise_from_the_pointer_step("live_runs absent from the row", monkeypatch)

    reading = promotion._fleet_state_reading(PROJECT)

    assert reading["fleet_state"] == "unmeasured"
    assert reading["unmeasured"]["fleet_state"] == "unavailable"
    assert set(reading["unmeasured"]) == {"fleet_state", "cause"}
    assert reading["unmeasured"]["cause"] == (f"{type(failure).__name__}: {failure}")
    assert type(failure).__name__ in reading["unmeasured"]["cause"]
    assert str(failure) in reading["unmeasured"]["cause"]


def test_a_failure_before_the_pointers_are_read_still_names_its_cause(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failure = _raise_from_the_pointer_listing(
        "fleet pointers are temporarily unreadable", monkeypatch
    )

    reading = promotion._fleet_state_reading(PROJECT)

    assert reading["fleet_state"] == "unmeasured"
    assert reading["unmeasured"]["fleet_state"] == "unavailable"
    assert set(reading["unmeasured"]) == {"fleet_state", "cause"}
    assert reading["unmeasured"]["cause"] == f"{type(failure).__name__}: {failure}"


def test_the_named_cause_is_bounded_in_length(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _raise_from_the_pointer_step("x" * 5_000, monkeypatch)

    cause = promotion._fleet_state_reading(PROJECT)["unmeasured"]["cause"]

    assert len(cause) == promotion._FLEET_UNMEASURED_CAUSE_LIMIT
    assert cause.startswith(f"{_PointerStepError.__name__}: x")


def test_an_unmeasured_reading_still_lets_the_promotion_complete(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_pointer(repository, "r-unmeasured")
    failure = _raise_from_the_pointer_step(
        "the pointer step could not be composed", monkeypatch
    )

    result = crew.complete(
        "r-unmeasured",
        gate="passed",
        completed_at=COMPLETED_AT,
        root=repository,
    )

    assert result["pointer_removed"] is True
    assert result["record"]["gate"] == "passed"
    assert result["fleet_state"]["fleet_state"] == "unmeasured"
    assert result["fleet_state"]["unmeasured"]["cause"] == (
        f"{type(failure).__name__}: {failure}"
    )
    stored = json.loads(
        ledger.run_path(PROJECT, "r-unmeasured", repository).read_text(encoding="utf-8")
    )
    assert stored["run_id"] == "r-unmeasured"
    assert "fleet_state" not in stored
