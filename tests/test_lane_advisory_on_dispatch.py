"""A dispatch carries its lane's trajectory unasked, and refuses nothing with it."""

from __future__ import annotations

import copy
import importlib
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from reckon import _plan_html, crew
from reckon import cli as cli_module
from reckon.crew.runs import _write_json, pointer_path
from tests import test_dispatch_names_its_backend as existing_backend_tests

dispatch_module = importlib.import_module("reckon.crew.dispatch")

pytest_plugins = ("tests.test_dispatch_names_its_backend",)

ROLE = "implement"
SPEC_LEVEL = "exact"


def _run(
    *,
    lane: str,
    plan: str,
    index: int,
    input_tokens: float,
    coordinator_tokens: float,
) -> dict:
    return {
        "plan": plan,
        "role": ROLE,
        "spec_level": SPEC_LEVEL,
        "backend": lane,
        "input_tokens": input_tokens,
        "changed_lines": {"added": 10, "removed": 0},
        "gate": "passed",
        "completed_at_source": "terminal_event",
        "node_definition": {
            "write_paths": [f"src/{plan}-{index}.py"],
            "coordinator": {
                "authoring_turn": {"tokens": {"input_tokens": coordinator_tokens}}
            },
        },
    }


def _cheap_lane_runs(lane: str, *, count: int = 12, cost: float = 200.0) -> list[dict]:
    """One run per plan, so no run is re-touched and the lane reads zero rework."""
    return [
        _run(
            lane=lane,
            plan=f"plan-{lane}-{index}",
            index=index,
            input_tokens=cost,
            coordinator_tokens=50.0,
        )
        for index in range(count)
    ]


def _dear_lane_runs(lane: str, *, count: int = 12, cost: float = 1000.0) -> list[dict]:
    """Every run on one plan sharing one path, so all but the last are reworked."""
    return [
        {
            **_run(
                lane=lane,
                plan=f"plan-{lane}",
                index=index,
                input_tokens=cost,
                coordinator_tokens=50.0,
            ),
            "node_definition": {
                "write_paths": ["src/shared.py"],
                "coordinator": {"authoring_turn": {"tokens": {"input_tokens": 50.0}}},
            },
        }
        for index in range(count)
    ]


def _observation(
    monkeypatch: pytest.MonkeyPatch,
    *,
    utilisation_pct: float | None = 46.0,
    burn_multiple: float | None = 5.7,
    projected_in_seconds: int | None = 300,
    resets_seconds: int = 6 * 24 * 3600,
    calls: list[dict] | None = None,
) -> None:
    """Fix the lane at one spending trajectory, counting reads of it."""
    now = datetime.now(UTC)
    state = {
        "headroom": "known",
        "utilisation_pct": utilisation_pct,
        "burn_multiple": burn_multiple,
        "projected_exhaustion_at": (
            None
            if projected_in_seconds is None
            else (now + timedelta(seconds=projected_in_seconds)).isoformat()
        ),
        "resets_at": (now + timedelta(seconds=resets_seconds)).isoformat(),
        "seconds_until_reset": resets_seconds,
        "observed_at": now.isoformat(),
    }

    def _read(*_args, **_kwargs):
        if calls is not None:
            calls.append(state)
        return state

    monkeypatch.setattr(dispatch_module, "_dispatch_lane_observation", _read)


def _invoke(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    node: str,
    lane: str = "alpha",
    runs: list[dict] | None = None,
    live: bool = False,
):
    config = copy.deepcopy(existing_backend_tests.CONFIG)
    monkeypatch.setattr(
        cli_module, "_resolved_flight", lambda *_args, **_kwargs: config
    )
    monkeypatch.setattr(
        cli_module, "_model_availability_refusal", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        dispatch_module,
        "_lane_advisory_ledger_runs",
        lambda *_args, **_kwargs: list(runs or []),
    )
    arguments = existing_backend_tests._arguments(repo, node=node, dry_run=not live)
    if live:
        # A real dispatch arms the follower unless the caller waives it; the
        # written pointer is the deliverable a dry run never produces.
        arguments.append("--no-watch")
    result = CliRunner().invoke(
        cli_module.main,
        [*arguments, "--backend", lane],
    )
    return existing_backend_tests._payload(result), result


def test_emitted_advisory_carries_the_four_figures_and_names_a_cheaper_lane(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _observation(monkeypatch, projected_in_seconds=300)
    runs = _dear_lane_runs("alpha") + _cheap_lane_runs("clive")
    payload, result = _invoke(dispatch_repo, monkeypatch, node="emitted", runs=runs)

    assert result.exit_code == 0
    advisory = payload["lane_advisory"]
    assert advisory["state"] == "emitted"
    assert advisory["utilisation_pct"] == 46.0
    assert advisory["burn_multiple"] == 5.7
    assert advisory["projected_exhaustion_at"] is not None
    assert advisory["resets_at"] is not None
    assert advisory["precedes_horizon"] is True
    assert advisory["cheaper_lane"]["lane"] == "clive"
    assert advisory["cheaper_lane"]["state"] == "measured"


def test_advisory_is_emitted_unasked_and_adds_no_second_probe(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict] = []
    _observation(monkeypatch, projected_in_seconds=300, calls=calls)
    payload, result = _invoke(
        dispatch_repo,
        monkeypatch,
        node="unasked",
        runs=_dear_lane_runs("alpha") + _cheap_lane_runs("clive"),
    )

    assert result.exit_code == 0
    assert payload["lane_advisory"]["state"] == "emitted"
    # The observation is read once, as before the advisory existed: the
    # trajectory rides the reading the dispatch already takes.
    assert len(calls) == 1


def test_resolved_backend_is_identical_with_and_without_the_advisory(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = _dear_lane_runs("alpha") + _cheap_lane_runs("clive")
    _observation(monkeypatch, projected_in_seconds=300)
    at_risk, first = _invoke(dispatch_repo, monkeypatch, node="at-risk", runs=runs)
    _observation(monkeypatch, projected_in_seconds=3000)
    later, second = _invoke(dispatch_repo, monkeypatch, node="after-horizon", runs=runs)
    _observation(monkeypatch, projected_in_seconds=None)
    unmeasured, third = _invoke(
        dispatch_repo, monkeypatch, node="no-projection", runs=runs
    )

    assert [first.exit_code, second.exit_code, third.exit_code] == [0, 0, 0]
    assert at_risk["lane_advisory"]["state"] == "emitted"
    assert later["lane_advisory"]["state"] == "quiet"
    assert unmeasured["lane_advisory"]["state"] == "quiet"
    assert at_risk["backend"] == later["backend"] == unmeasured["backend"] == "alpha"
    assert (
        at_risk["agent"]["backend"]
        == later["agent"]["backend"]
        == unmeasured["agent"]["backend"]
        == "alpha"
    )


def test_a_projection_after_the_horizon_says_so_rather_than_going_silent(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _observation(monkeypatch, projected_in_seconds=3000)
    payload, result = _invoke(dispatch_repo, monkeypatch, node="after-horizon")

    assert result.exit_code == 0
    advisory = payload["lane_advisory"]
    assert advisory["state"] == "quiet"
    assert advisory["precedes_horizon"] is False
    assert "after this node's" in advisory["detail"]
    assert advisory["cheaper_lane"]["state"] == "not_evaluated"


def test_an_unmetered_lane_states_it_has_no_window_to_exhaust(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _observation(monkeypatch, projected_in_seconds=300)
    payload, result = _invoke(
        dispatch_repo, monkeypatch, node="local-lane", lane="clive"
    )

    assert result.exit_code == 0
    advisory = payload["lane_advisory"]
    assert advisory["metered"] is False
    assert advisory["state"] == "quiet"
    assert "unmetered" in advisory["detail"]


def test_an_absent_ledger_names_no_lane_and_does_not_refuse(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _observation(monkeypatch, projected_in_seconds=300)
    payload, result = _invoke(dispatch_repo, monkeypatch, node="no-ledger", runs=[])

    assert result.exit_code == 0
    advisory = payload["lane_advisory"]
    assert advisory["state"] == "emitted"
    assert advisory["cheaper_lane"]["lane"] is None
    assert advisory["cheaper_lane"]["state"] == "insufficient_evidence"


def test_too_few_runs_states_the_shortfall_instead_of_naming_a_lane() -> None:
    runs = [
        _run(
            lane="alpha",
            plan="p",
            index=0,
            input_tokens=1000.0,
            coordinator_tokens=50.0,
        ),
        _run(
            lane="clive", plan="q", index=1, input_tokens=10.0, coordinator_tokens=10.0
        ),
    ]
    clause = dispatch_module._lane_advisory_cheaper_lane(
        runs,
        resolved_lane="alpha",
        role=ROLE,
        spec_level=SPEC_LEVEL,
        configured_lanes=["alpha", "clive"],
    )
    assert clause["lane"] is None
    assert clause["state"] == "insufficient_evidence"
    assert "1 usable run(s)" in clause["detail"]
    assert "10 needed" in clause["detail"]


def test_no_lane_cheaper_is_stated_when_the_resolved_lane_already_wins() -> None:
    runs = _cheap_lane_runs("alpha") + _dear_lane_runs("clive")
    clause = dispatch_module._lane_advisory_cheaper_lane(
        runs,
        resolved_lane="alpha",
        role=ROLE,
        spec_level=SPEC_LEVEL,
        configured_lanes=["alpha", "clive"],
    )
    assert clause["lane"] is None
    assert clause["state"] == "none_cheaper"
    assert "no configured lane" in clause["detail"]


def test_a_cheaper_lane_is_named_from_its_rework_charged_cost() -> None:
    runs = _dear_lane_runs("alpha") + _cheap_lane_runs("clive")
    clause = dispatch_module._lane_advisory_cheaper_lane(
        runs,
        resolved_lane="alpha",
        role=ROLE,
        spec_level=SPEC_LEVEL,
        configured_lanes=["alpha", "clive"],
    )
    assert clause["state"] == "measured"
    assert clause["lane"] == "clive"
    assert clause["cost_per_durable_node"] < clause["resolved_cost_per_durable_node"]
    assert clause["samples"] == 12


def test_an_emitted_advisory_reaches_the_written_pointer(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _observation(monkeypatch, projected_in_seconds=300)
    runs = _dear_lane_runs("alpha") + _cheap_lane_runs("clive")
    payload, result = _invoke(
        dispatch_repo, monkeypatch, node="pointer-carries", runs=runs, live=True
    )

    assert result.exit_code == 0
    # Read the record the run actually wrote rather than the payload the CLI
    # echoed: the record assembly is where the advisory was dropped, and a
    # payload assembled from a different object cannot show that.
    pointer = crew.read_pointer(payload["run_id"])
    advisory = pointer["lane_advisory"]
    assert advisory["state"] == "emitted"
    assert advisory["utilisation_pct"] == 46.0
    assert advisory["burn_multiple"] == 5.7
    assert advisory["projected_exhaustion_at"] is not None
    assert advisory["resets_at"] is not None
    assert advisory["cheaper_lane"]["lane"] == "clive"
    assert pointer["lane_declaration"]["resolved_backend"] == "alpha"


def test_an_absent_advisory_writes_no_key_rather_than_a_null(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _observation(monkeypatch, projected_in_seconds=300)
    real_plan_dispatch = dispatch_module.plan_dispatch

    def _without_advisory(**kwargs):
        resolution = real_plan_dispatch(**kwargs)
        resolution.lane_advisory = None
        return resolution

    monkeypatch.setattr(dispatch_module, "plan_dispatch", _without_advisory)
    payload, result = _invoke(dispatch_repo, monkeypatch, node="no-advisory", live=True)

    assert result.exit_code == 0
    pointer = crew.read_pointer(payload["run_id"])
    assert "lane_advisory" not in pointer
    # The run wrote its record: the neighbouring lane fields are present, so
    # the absence belongs to the advisory rather than to a record that never
    # landed. A null here would read as a lane that was assessed. It is a
    # null the reader cannot tell from a measured absence.
    assert pointer["lane_declaration"]["resolved_backend"] == "alpha"
    assert pointer["lane_reading"] is not None


def test_the_refusal_renders_the_count_that_fired_it_not_the_lane_total() -> None:
    # Twelve usable runs, but only three carry a paired worker-and-coordinator
    # reading, and the charged median is drawn from those three. Counting the
    # nine unpaired runs toward the floor would show "12 usable run(s), 10
    # needed" beside a refusal citing ten -- a count that already meets the
    # floor. The rendered count must be the one that fired the clause.
    charged = _cheap_lane_runs("alpha", count=3)
    uncharged = [
        _run(
            lane="alpha",
            plan=f"plan-uncharged-{index}",
            index=index,
            input_tokens=None,
            coordinator_tokens=None,
        )
        for index in range(9)
    ]
    for run in uncharged:
        run.pop("input_tokens")
        run["node_definition"].pop("coordinator")

    evidence = dispatch_module._lane_advisory_costs(
        [*charged, *uncharged], role=ROLE, spec_level=SPEC_LEVEL
    )
    assert evidence["alpha"]["samples"] == 12
    assert evidence["alpha"]["input_samples"] == 3

    clause = dispatch_module._lane_advisory_cheaper_lane(
        [*charged, *uncharged],
        resolved_lane="alpha",
        role=ROLE,
        spec_level=SPEC_LEVEL,
        configured_lanes=["alpha", "clive"],
    )
    assert clause["state"] == "insufficient_evidence"
    assert "3 usable run(s)" in clause["detail"]
    assert "10 needed" in clause["detail"]
    assert "12 usable run(s)" not in clause["detail"]


# ── The advisory must also reach the committed ledger row, which is the
# surface a later reader has once the live pointer has been deleted by the
# promotion that made the run evidence.

PROMOTION_PROJECT = "advisory-project"
PROMOTION_PLAN = "advisory-plan"
PROMOTED_AT = "2030-01-02T03:04:05Z"


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture()
def ledger_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthesised project whose ledger is writable, never a real checkout."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    root = tmp_path / "repo"
    (root / "docs" / "state" / PROMOTION_PROJECT).mkdir(parents=True)
    plan_path = root / "docs" / "plans" / f"{PROMOTION_PLAN}.html"
    plan_path.parent.mkdir(parents=True)
    plan = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROMOTION_PROJECT}">'
        f"<title>{PROMOTION_PLAN} title</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    plan_path.write_text(
        _plan_html.write_state(
            plan,
            {
                "type": "plan",
                "slug": PROMOTION_PLAN,
                "title": "Advisory plan",
                "status": "active",
                "version": 0,
                "comments": {},
            },
        ),
        encoding="utf-8",
    )
    (config_home / "mounts.json").write_text(
        json.dumps({PROMOTION_PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "docs"),
        ("commit", "-q", "-m", "test: seed repository"),
    ):
        _git(root, *arguments)
    return root


def _advisory(*, cheaper_lane: str | None) -> dict[str, Any]:
    """One emitted advisory, shaped as the dispatch writes it."""
    return {
        "state": "emitted",
        "detail": "alpha sits near its horizon",
        "backend": "alpha",
        "metered": True,
        "utilisation_pct": 46.0,
        "burn_multiple": 5.7,
        "projected_exhaustion_at": "2030-01-02T03:30:00Z",
        "resets_at": "2030-01-08T03:00:00Z",
        "seconds_until_reset": 6 * 24 * 3600,
        "observed_at": "2030-01-02T03:20:00Z",
        "horizon_seconds": 1800,
        "horizon_ends_at": "2030-01-02T03:45:00Z",
        "precedes_horizon": True,
        "cheaper_lane": {"lane": cheaper_lane, "state": "measured", "detail": ""},
    }


def _declaration() -> dict[str, Any]:
    return {
        "backend": "alpha",
        "resolved_backend": "alpha",
        "headroom": "known",
        "metered": True,
        "utilisation_pct": 46.0,
        "observed_at": "2030-01-02T03:20:00Z",
        "read_at": "2030-01-02T03:20:00Z",
    }


def _reading() -> dict[str, Any]:
    return {
        "state": "known",
        "headroom": 22,
        "running": 19,
        "waiting": 0,
        "observed_at": "2030-01-02T03:20:00Z",
    }


def _write_pointer(
    repository: Path,
    run_id: str,
    *,
    backend: str,
    advisory: dict[str, Any] | None,
) -> None:
    record: dict[str, Any] = {
        "run_id": run_id,
        "project": PROMOTION_PROJECT,
        "repo": str(repository),
        "worktree": str(repository),
        "base_sha": _git(repository, "rev-parse", "HEAD"),
        "launch": "in-harness",
        "role": "implement",
        "backend": backend,
        "created_at": "2030-01-02T03:00:00Z",
        "lane_declaration": _declaration(),
        "lane_reading": _reading(),
        "node": {
            "id": f"advisory-{run_id}",
            "plan": PROMOTION_PLAN,
            "section": "§3",
            "time_budget": "20m",
            "write_paths": [],
        },
    }
    # Written only when it exists, matching the dispatch record: a pointer with
    # a null advisory would read as a lane assessed and found quiet.
    if advisory is not None:
        record["lane_advisory"] = advisory
    _write_json(pointer_path(run_id), record)


def _promote(
    repository: Path,
    run_id: str,
    *,
    backend: str,
    advisory: dict[str, Any] | None,
) -> dict[str, Any]:
    _write_pointer(repository, run_id, backend=backend, advisory=advisory)
    crew.complete(
        run_id,
        gate="passed",
        completed_at=PROMOTED_AT,
        root=repository,
    )
    return _committed_row(repository, run_id)


def _committed_row(repository: Path, run_id: str) -> dict[str, Any]:
    """Read the row off disk, never the pointer the promotion consumed."""
    ledger_path = repository / "docs" / "state" / PROMOTION_PROJECT / "crew.json"
    data = json.loads(ledger_path.read_text(encoding="utf-8"))
    rows = [row for row in data["data"]["runs"] if row["run_id"] == run_id]
    assert len(rows) == 1
    return rows[0]


def test_a_promoted_row_carries_the_advisory_and_the_lane_fields(
    ledger_repository: Path,
) -> None:
    run_id = "r-promoted-carries"
    row = _promote(
        ledger_repository,
        run_id,
        backend="alpha",
        advisory=_advisory(cheaper_lane="clive"),
    )

    advised = row["lane_advisory"]["cheaper_lane"]["lane"]
    assert advised == "clive"
    assert row["lane_advisory"]["state"] == "emitted"
    assert row["lane_declaration"] == _declaration()
    assert row["lane_reading"] == _reading()
    # The run became evidence: the pointer that held these is deleted, so the
    # row is now the only place they are readable.
    assert not pointer_path(run_id).exists()


def test_advice_taken_and_advice_declined_are_told_apart_from_the_rows(
    ledger_repository: Path,
) -> None:
    took = _promote(
        ledger_repository,
        "r-took-the-advice",
        backend="clive",
        advisory=_advisory(cheaper_lane="clive"),
    )
    declined = _promote(
        ledger_repository,
        "r-declined-the-advice",
        backend="alpha",
        advisory=_advisory(cheaper_lane="clive"),
    )

    def _advised_lane(row: dict[str, Any]) -> str | None:
        return row["lane_advisory"]["cheaper_lane"]["lane"]

    # Both rows are read from the committed ledger alone, and the question the
    # row carries is whether the lane chosen equals the one advised.
    assert took["backend"] == _advised_lane(took)
    assert declined["backend"] != _advised_lane(declined)
    assert _advised_lane(took) == _advised_lane(declined) == "clive"
    assert took["backend"] == "clive" and declined["backend"] == "alpha"


def test_a_run_with_no_advisory_promotes_with_no_advisory_key(
    ledger_repository: Path,
) -> None:
    row = _promote(
        ledger_repository,
        "r-no-advisory",
        backend="alpha",
        advisory=None,
    )

    # A key holding null would read as a lane measured and found quiet; the
    # absence must be truly absent.
    assert "lane_advisory" not in row
    # The row landed, so the absence belongs to the advisory rather than to a
    # record that was never written.
    assert row["lane_declaration"] == _declaration()
    assert row["lane_reading"] == _reading()
