"""A passing implement landing is refused when the plan it lands on did not move.

The check exists because nothing in the landing path advanced a plan's impl:
two plans sat at zero percent across five landed nodes each. Each test here
makes the guarded thing happen — an equal pair of figures — and reads the
refusal, rather than resting on the suite being green.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import _plan_html, _store, crew
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "proj"
PLAN = "plan-a"


def _write_plan(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{state['slug']}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")


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
    _write_plan(
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
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(root, *arguments)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt", "docs")
    _git(root, "commit", "-q", "-m", "test: seed repository")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _set_plan(repository: Path, **fields) -> None:
    """Patch the fixture plan's persisted state, then commit nothing more."""
    state, version = _store.read_plan(PROJECT, PLAN, repository, artifact_type="plan")
    state.update(fields)
    _store.write_plan(PROJECT, PLAN, state, version, repository, artifact_type="plan")


def _write_pointer(
    repository: Path,
    run_id: str,
    *,
    role: str = "implement",
    plan_impl_at_dispatch: float | None = None,
    plan: str = PLAN,
) -> None:
    record: dict = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repository),
        "worktree": str(repository),
        "launch": "in-harness",
        "role": role,
        "backend": "native",
        "created_at": "2026-09-18T08:00:00Z",
        "node": {
            "id": "node-a",
            "plan": plan,
            "section": "s2",
            "time_budget": "25m",
            "write_paths": [],
        },
    }
    if plan_impl_at_dispatch is not None:
        record["plan_impl_at_dispatch"] = plan_impl_at_dispatch
    _write_json(pointer_path(run_id), record)


def _promote(repository: Path, run_id: str, **kwargs):
    return crew.complete(run_id, root=repository, **kwargs)


def _ledger_row(repository: Path, run_id: str) -> dict:
    _data, _version = crew.ledger.load(PROJECT, root=repository)
    rows = crew.ledger.runs(PROJECT, root=repository)
    return next(row for row in rows if row.get("run_id") == run_id)


def test_a_passing_implement_landing_refuses_when_impl_did_not_move(
    repository: Path,
) -> None:
    _set_plan(
        repository,
        impl=0.5,
        section_declarations={"s2": "implementable", "s3": "implementable"},
    )
    run_id = "r-20260918T080000000000-node-a"
    _write_pointer(repository, run_id, plan_impl_at_dispatch=0.5)

    with pytest.raises(crew.CrewError) as refusal:
        _promote(
            repository,
            run_id,
            gate="passed",
            outcome="the guard landed",
        )

    message = str(refusal.value)
    assert "impl" in message
    assert "0.5" in message
    assert "s2" in message and "s3" in message
    assert "--no-impl-change" in message
    # The pointer survives a refusal, so the coordinator can retry it.
    assert pointer_path(run_id).exists()


def test_a_recorded_reason_waives_the_refusal_and_lands_on_the_ledger_row(
    repository: Path,
) -> None:
    _set_plan(repository, impl=0.5)
    run_id = "r-20260918T080100000000-node-a"
    _write_pointer(repository, run_id, plan_impl_at_dispatch=0.5)

    promoted = _promote(
        repository,
        run_id,
        gate="passed",
        outcome="the section's impl is moved by a sibling node",
        no_impl_change="the section's impl is moved by a sibling node",
    )

    assert promoted["impl_move"]["verdict"] == "waived"
    row = _ledger_row(repository, run_id)
    assert row["plan_impl_at_dispatch"] == 0.5
    assert row["impl_move"]["reason"] == "the section's impl is moved by a sibling node"


def test_an_impl_that_moved_promotes_without_a_reason(repository: Path) -> None:
    _set_plan(repository, impl=0.9)
    run_id = "r-20260918T080200000000-node-a"
    _write_pointer(repository, run_id, plan_impl_at_dispatch=0.5)

    promoted = _promote(repository, run_id, gate="passed", outcome="the plan advanced")

    assert promoted["impl_move"]["verdict"] == "moved"
    row = _ledger_row(repository, run_id)
    assert row["plan_impl_at_dispatch"] == 0.5
    assert row["impl_move"]["at_complete"] == 0.9


def test_a_run_dispatched_before_the_check_is_exempt_and_says_so(
    repository: Path,
) -> None:
    _set_plan(repository, impl=0.5)
    run_id = "r-20260918T080300000000-node-a"
    _write_pointer(repository, run_id)

    promoted = _promote(repository, run_id, gate="passed", outcome="legacy run lands")

    assert promoted["impl_move"]["verdict"] == "exempt"
    assert promoted["impl_move"]["reason"] == "no-impl-recorded-at-dispatch"
    assert "plan_impl_at_dispatch" not in _ledger_row(repository, run_id)


@pytest.mark.parametrize("role", ["review", "investigate", "cleanup"])
def test_non_implementing_roles_are_exempt(repository: Path, role: str) -> None:
    _set_plan(repository, impl=0.5)
    run_id = f"r-20260918T080400000000-{role}"
    _write_pointer(repository, run_id, role=role, plan_impl_at_dispatch=0.5)

    promoted = _promote(
        repository, run_id, gate="passed", outcome=f"{role} produced its report"
    )

    assert promoted["impl_move"]["verdict"] == "exempt"
    assert promoted["impl_move"]["reason"] == f"role-not-enforced:{role}"


def test_a_test_role_is_enforced(repository: Path) -> None:
    _set_plan(repository, impl=0.5)
    run_id = "r-20260918T080500000000-node-a"
    _write_pointer(repository, run_id, role="test", plan_impl_at_dispatch=0.5)

    with pytest.raises(crew.CrewError) as refusal:
        _promote(repository, run_id, gate="passed", outcome="the verifier passed")

    assert "--no-impl-change" in str(refusal.value)


def test_a_non_passing_gate_is_exempt(repository: Path) -> None:
    _set_plan(repository, impl=0.5)
    run_id = "r-20260918T080600000000-node-a"
    _write_pointer(repository, run_id, plan_impl_at_dispatch=0.5)

    promoted = _promote(
        repository,
        run_id,
        gate="failed",
        failure_classification="work-rejected",
        outcome="the work did not meet the goal",
    )

    assert promoted["impl_move"]["verdict"] == "exempt"
    assert promoted["impl_move"]["reason"] == "gate-not-passing"


@pytest.mark.parametrize("classification", ["negative-result", "correct-refusal"])
def test_negative_results_and_correct_refusals_are_exempt(
    repository: Path, classification: str
) -> None:
    _set_plan(repository, impl=0.5)
    run_id = f"r-20260918T080700000000-{classification}"
    _write_pointer(repository, run_id, plan_impl_at_dispatch=0.5)

    promoted = _promote(
        repository,
        run_id,
        gate="failed",
        failure_classification=classification,
        outcome=f"the gate {classification}",
    )

    assert promoted["impl_move"]["verdict"] == "exempt"
    assert promoted["impl_move"]["reason"] == f"failure-classification:{classification}"


def test_the_refusal_lists_no_sections_when_none_are_declared(
    repository: Path,
) -> None:
    """A plan with no declaration still refuses.

    The section list is a courtesy for the reader, not a precondition: a plan
    that has never been declared still must move when its work lands.
    """
    _set_plan(repository, impl=0.25)
    run_id = "r-20260918T080800000000-node-a"
    _write_pointer(repository, run_id, plan_impl_at_dispatch=0.25)

    with pytest.raises(crew.CrewError) as refusal:
        _promote(repository, run_id, gate="passed", outcome="the work landed")

    message = str(refusal.value)
    assert "0.25" in message
    assert "(none declared)" in message
    assert "--no-impl-change" in message


def test_the_dispatch_reader_reads_the_impl_the_record_will_carry(
    repository: Path,
) -> None:
    """The reader the dispatch record calls returns the plan's persisted impl."""
    from reckon.crew.dispatch import _plan_impl_at_dispatch

    _set_plan(repository, impl=0.75)

    assert _plan_impl_at_dispatch(PROJECT, PLAN, repository) == 0.75


def test_the_dispatch_reader_returns_none_for_an_unset_impl(repository: Path) -> None:
    """A plan carrying no impl reads as unset, never as a false zero."""
    from reckon.crew.dispatch import _plan_impl_at_dispatch

    assert _plan_impl_at_dispatch(PROJECT, PLAN, repository) is None
