"""A standing review is recognised by the record it was told to write.

A review of a run identifies the run it reviews in the paths the dispatch
composes from that run, so a review launched under any node id still names the
run it is scoring. Recognition keyed on the minted node id alone loses such a
review: the source run keeps reading review-missing while a review of it is
alive, and its coordinator is steered toward a duplicate review on a lane it
has already withdrawn.

The lane half is argued with its discriminating negative: an owning backend the
flight configuration has removed from review routing must not be the lane the
printed command names, because the printed command is the one a coordinator
retypes.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import recovery, runs

obligations_module = importlib.import_module("reckon.crew.obligations")

PROJECT = "review-path-fixture"
SESSION = "coordinator-fixture"
OBSERVED_AT = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)

# The lane the owning run recorded, the locally served lane a fallback would
# otherwise land on, and a third lane a fallback may reach past both.
OWNING_BACKEND = "clive"
LOCAL_BACKEND = "alpha"
OTHER_BACKEND = "beta"


def _backend(command: str) -> dict:
    return {
        "launch": "cli",
        "command": command,
        "model": "some-model",
        "effort": "high",
        "sandbox": "worktree-full",
        "session_reuse": True,
        "time_budget": "25m",
    }


def _lane_config(*excluded: str) -> dict:
    """The fleet, optionally with backends removed from review routing."""
    config: dict = {
        "default_backend": LOCAL_BACKEND,
        "local_backend": LOCAL_BACKEND,
        "backends": {
            LOCAL_BACKEND: _backend("codex"),
            OTHER_BACKEND: _backend("claude"),
            OWNING_BACKEND: _backend("claude"),
        },
        "roles": {"implement": {}, "review": {}},
        "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
    }
    if excluded:
        config[recovery.REVIEW_EXCLUDED_BACKENDS_KEY] = list(excluded)
    return config


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed review-path fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    monkeypatch.setattr(obligations_module, "_utc_now", lambda: OBSERVED_AT)
    return root


def _write_pointer(run_id: str, record: dict) -> None:
    runs._write_json(runs.pointer_path(run_id), record)


def _scoring_pointer(repository: Path, run_id: str, *, backend: str = "") -> dict:
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "process_alive": False,
        "repo": str(repository),
        "worktree": str(repository),
        "node": {
            "id": "source-node",
            "plan": "fixture-plan",
            "section": "fixture-section",
            "time_budget": "20m",
            "write_paths": ["seed.txt"],
        },
    }
    if backend:
        record["backend"] = backend
    _write_pointer(run_id, record)
    return record


def _row(run_id: str, classification: str) -> dict:
    return {
        "run_id": run_id,
        "session": SESSION,
        "plan": "fixture-plan",
        "node": "source-node",
        "classification": classification,
        "recovery_classification": classification,
        "terminal_age_seconds": 120,
        "next_action": "reckon crew dispatch --node review-of-source-node",
    }


def test_a_review_under_any_node_id_is_in_flight_by_its_record_path(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A review the node-id lookup cannot name is still the review of this run."""
    source = "run-source"
    review = "run-review-elsewhere"
    _scoring_pointer(repository, source)
    record = runs.read_pointer(source)

    expected = recovery._review_dispatch_fields(record)["write_paths"]
    # The head-keyed path is composed only when the run's head resolves, and the
    # obligations reader keys its currency check on it, so its presence is the
    # control that this fixture exercises the whole path rather than the legacy
    # spelling alone.
    assert len(expected) == 2, expected

    _write_pointer(
        review,
        {
            "run_id": review,
            "project": PROJECT,
            "session": SESSION,
            "process_alive": True,
            "role": "review",
            "node": {
                # Deliberately not ``review-of-source-node``: the measured
                # defect is a review launched under a coordinator's own id.
                "id": "re-review-of-source-node",
                "plan": "fixture-plan",
                "section": "fixture-section",
                "write_paths": list(expected),
            },
        },
    )

    assert recovery._review_in_flight(record) == review

    monkeypatch.setattr(
        obligations_module,
        "_classified_rows",
        lambda _project: [_row(source, "scoring")],
    )
    monkeypatch.setattr(
        runs, "drain", lambda *_args, **_kwargs: {"unreconciled_runs": 1}
    )
    result = obligations_module.obligations(PROJECT, SESSION)
    kinds = [item["kind"] for item in result["obligations"]]
    assert "review-missing" not in kinds


def test_the_printed_command_names_a_lane_the_exclusion_allows(
    repository: Path,
) -> None:
    """The retypeable command consults the exclusion the reflex already honours."""
    run_id = "r-lane"
    _scoring_pointer(repository, run_id, backend=OWNING_BACKEND)
    record = runs.read_pointer(run_id)

    # Control: with nothing excluded the owning lane is the preference, so a
    # fallback has not quietly become the default.
    unprejudiced = recovery._review_dispatch_argv(record, config=_lane_config())
    assert unprejudiced[unprejudiced.index("--backend") + 1] == OWNING_BACKEND

    # The owning lane excluded: the command names the local lane instead.
    owning_excluded = recovery._review_dispatch_argv(
        record, config=_lane_config(OWNING_BACKEND)
    )
    assert OWNING_BACKEND not in owning_excluded
    assert "--local" in owning_excluded

    # Owning and local excluded: a named fallback lane is printed, not the
    # local spelling and never an excluded backend.
    both_excluded = recovery._review_dispatch_argv(
        record, config=_lane_config(OWNING_BACKEND, LOCAL_BACKEND)
    )
    assert both_excluded[both_excluded.index("--backend") + 1] == OTHER_BACKEND
    assert "--local" not in both_excluded

    action = recovery._review_dispatch_action(
        record, config=_lane_config(OWNING_BACKEND, LOCAL_BACKEND)
    )
    assert "--backend " + OTHER_BACKEND in action
    assert OWNING_BACKEND not in action
