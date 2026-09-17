"""A promotion that is going to refuse leaves the plan exactly as it found it.

The landing comment is written to the plan HTML, and the plan version bumped,
before ``ledger.build_record`` runs its own gate-evidence validation. A
promotion refused for missing gate evidence has therefore already mutated the
plan the caller is retrying against: the retry finds a comment on disk and is
refused for a reason that has nothing to do with the missing evidence, and the
first attempt's narrative pins the wording of every later attempt's outcome.

No validation performed after the comment needs the comment to exist, so the
measure is ordering rather than a new guard: validate before writing. A fixture
repository and a fixture crew home are synthesised per test, and the real plan
and crew directories are asserted untouched, because an isolated read does not
prove an isolated write.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from reckon import _plan_html, _store, crew, ledger
from reckon.crew import promotion
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "refused-promotion-fixture"
PLAN = "promotion-target"
RUN_IDS = (
    "r-20260917T120000000001-gate-evidence-refused",
    "r-20260917T120000000002-verdict-refused",
    "r-20260917T120000000003-retry-succeeds",
    "r-20260917T120000000004-well-formed",
    "r-20260917T120000000005-idempotent",
    "r-20260917T120000000006-worker-authored",
)

# The shape a passing gate check takes when the caller supplies it: every field
# the gate-evidence guard requires, so the same call succeeds once it is given.
GATE_CHECK: dict[str, Any] = {
    "command": "pytest -q tests/test_refused_promotion_leaves_no_trace.py",
    "exit_status": 0,
    "log_path": "/tmp/refused-promotion-gate.log",
    "log_digest": "sha256:0",
}


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write_plan(root: Path, comments: dict[str, list[dict]] | None = None) -> Path:
    path = root / "docs" / "plans" / f"{PLAN}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    state = {
        "type": "plan",
        "slug": PLAN,
        "title": "Promotion target",
        "status": "active",
        "version": 0,
        "comments": comments or {},
    }
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def real_stores_are_not_fixture_targets() -> None:
    """No fixture may reach this checkout's own plan or the real crew home."""
    checkout = Path(__file__).resolve().parents[1]
    real_plan = checkout / "docs" / "plans" / f"{PLAN}.html"
    real_state = checkout / "docs" / "state" / PROJECT
    real_live = Path.home() / ".config" / "reckon" / "crew" / "live"
    real_pointers = [real_live / f"{run_id}.json" for run_id in RUN_IDS]
    assert not real_plan.exists()
    assert not real_state.exists()
    assert not any(path.exists() for path in real_pointers)
    yield
    assert not real_plan.exists()
    assert not real_state.exists()
    assert not any(path.exists() for path in real_pointers)


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_hook = tmp_path / "config"
    config_hook.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_hook))
    root = tmp_path / "repo"
    _write_plan(root)
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "docs"),
        ("commit", "-q", "-m", "test: seed repository"),
    ):
        _git(root, *arguments)
    (config_hook / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _pointer(
    repository: Path, run_id: str, *, write_paths: list[str] | None = None
) -> None:
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "launch": "in-harness",
            "role": "implement",
            "member": "worker-a",
            "backend": "native",
            "created_at": "2026-09-17T11:00:00Z",
            "manifest_path": "/durable/manifest.md",
            "node": {
                "id": "promotion-target",
                "plan": PLAN,
                "section": "§2",
                "time_budget": "25m",
                "write_paths": write_paths or [],
            },
        },
    )


def _plan_state(repository: Path) -> tuple[dict[str, Any], int, bytes]:
    plan_file = repository / "docs" / "plans" / f"{PLAN}.html"
    state, version = _store.read_plan(PROJECT, PLAN, repository, artifact_type="plan")
    return state, version, plan_file.read_bytes()


def _promote(
    repository: Path, run_id: str, outcome: str, **extra: Any
) -> dict[str, Any]:
    return crew.complete(
        run_id,
        gate=extra.pop("gate", "passed"),
        outcome=outcome,
        root=repository,
        **extra,
    )


# ── The falsifier: a refusal writes nothing ─────────────────────────────────


def test_a_gate_evidence_refusal_leaves_the_plan_byte_identical(
    repository: Path,
) -> None:
    """The named case: a passing gate with no check is refused, plan untouched."""
    run_id = RUN_IDS[0]
    _pointer(repository, run_id)
    _state, version_before, before = _plan_state(repository)

    with pytest.raises(ledger.LedgerError) as error:
        _promote(
            repository,
            run_id,
            "the gate evidence was not among the submitted fields",
            require_gate_check=True,
        )

    assert "requires the check that produced it" in str(error.value)
    _state, version_after, after = _plan_state(repository)
    assert after == before
    assert version_after == version_before
    # A refused promotion consumes nothing: the pointer survives so the caller
    # may retry it unchanged.
    assert pointer_path(run_id).exists()


def test_another_refusal_at_the_same_point_also_leaves_no_trace(
    repository: Path,
) -> None:
    """The range: the ordering holds for every refusal the record build raises.

    The gate-evidence guard is one check ``ledger.build_record`` performs and an
    off-vocabulary gate verdict is another. ``promotion.complete`` rejects the
    verdict before the pointer lock, so it is reached here through the locked
    body directly — which is the guard that matters, because a repair that
    hoisted only the gate-evidence check would leave the rest mutating the plan.
    """
    run_id = RUN_IDS[1]
    _pointer(repository, run_id)
    _state, version_before, before = _plan_state(repository)

    with pytest.raises(ledger.LedgerError) as error:
        crew._complete_locked(
            run_id,
            gate="nearly-passed",
            outcome="an off-vocabulary verdict",
            root=repository,
            gate_check=GATE_CHECK,
            require_gate_check=True,
        )

    assert "is not one of" in str(error.value)
    _state, version_after, after = _plan_state(repository)
    assert version_after == version_before
    assert after == before
    assert pointer_path(run_id).exists()


def test_the_retry_after_a_refusal_succeeds_with_no_intervening_change(
    repository: Path,
) -> None:
    """The cascade the ordering exists to prevent, in one test.

    The refused attempt leaves the plan, the tree and the pointer exactly as it
    found them, so the immediately following retry lands with no commit, no
    cleanup and no wording pinned by the failed attempt's own narrative.
    """
    run_id = RUN_IDS[2]
    _state, version_before, before = _plan_state(repository)
    head_before = _git(repository, "rev-parse", "HEAD")
    _pointer(repository, run_id)

    with pytest.raises(ledger.LedgerError):
        _promote(
            repository,
            run_id,
            "the record that lands on the retry",
            require_gate_check=True,
        )

    _state, version_after_refusal, after_refusal = _plan_state(repository)
    assert after_refusal == before
    assert version_after_refusal == version_before
    assert _git(repository, "rev-parse", "HEAD") == head_before

    _promote(
        repository,
        run_id,
        "the record that lands on the retry",
        require_gate_check=True,
        gate_check=GATE_CHECK,
    )

    state, version_after, after = _plan_state(repository)
    assert version_after == version_before + 1
    assert after != before
    comments = state["comments"]["s2"]
    assert len(comments) == 1
    assert "the record that lands on the retry" in comments[0]["body"]
    rows = ledger.runs(PROJECT, root=repository)
    assert [row["run_id"] for row in rows] == [run_id]
    # The comment carries the narrative, so the row carries it empty rather
    # than twice — the coupling that once forced the write-before-validate
    # order, preserved by handing the decision to the row after the fact.
    assert rows[0]["outcome"] == ""


# ── Positive controls: the ordinary paths still write exactly once ──────────


def test_a_well_formed_promotion_writes_one_comment_and_bumps_once(
    repository: Path,
) -> None:
    run_id = RUN_IDS[3]
    _pointer(repository, run_id)
    _state, version_before, before = _plan_state(repository)

    _promote(
        repository,
        run_id,
        "a well-formed promotion lands its narrative",
        require_gate_check=True,
        gate_check=GATE_CHECK,
    )

    state, version_after, after = _plan_state(repository)
    assert version_after == version_before + 1
    assert after != before
    assert len(state["comments"]["s2"]) == 1
    assert "a well-formed promotion lands its narrative" in state["comments"]["s2"][0]["body"]


def test_a_second_promotion_of_a_landed_run_writes_nothing(
    repository: Path,
) -> None:
    """The idempotent re-promotion path reports already_recorded, writing nothing.

    A run whose ledger row is already committed is promoted again with the same
    narrative — the shape a coordinator hits when it is unsure whether the first
    call landed. The plan must not gain a second comment and its version must
    not move.
    """
    run_id = RUN_IDS[4]
    _pointer(repository, run_id)
    _promote(
        repository,
        run_id,
        "a landing that is then promoted a second time",
        require_gate_check=True,
        gate_check=GATE_CHECK,
    )
    _state, version_after_first, after_first = _plan_state(repository)
    # A landing promotion deletes the pointer; restore it to re-promote the run
    # whose row is already committed.
    _pointer(repository, run_id)

    result = _promote(
        repository,
        run_id,
        "a landing that is then promoted a second time",
        require_gate_check=True,
        gate_check=GATE_CHECK,
    )

    assert result["already_promoted"] is True
    assert result["plan_comment"]["already_recorded"] is True
    _state, version_after_second, after_second = _plan_state(repository)
    assert after_second == after_first
    assert version_after_second == version_after_first


def test_a_worker_authored_landing_record_is_not_duplicated(
    repository: Path,
) -> None:
    """The writer's own record wins, and the narrative promotion declined to
    write survives on the ledger row instead of being dropped."""
    run_id = RUN_IDS[5]
    comment_id = f"c-run-{run_id}"
    worker_comment = {
        "id": comment_id,
        "who": "worker-a",
        "when": "2026-09-17T11:00:00Z",
        "body": "<p>the worker records its own landing</p>",
    }
    _write_plan(repository, {"s2": [worker_comment]})
    _git(repository, "add", "docs")
    _git(repository, "commit", "-q", "-m", "docs: record the worker landing")
    worker_commit = _git(repository, "rev-parse", "HEAD")
    plan_relative = f"docs/plans/{PLAN}.html"
    _pointer(repository, run_id, write_paths=[plan_relative])
    _state, version_before, before = _plan_state(repository)

    _promote(
        repository,
        run_id,
        "the coordinator narrative the worker already recorded",
        commits=(worker_commit,),
        require_gate_check=True,
        gate_check=GATE_CHECK,
    )

    state, version_after, after = _plan_state(repository)
    assert after == before
    assert version_after == version_before
    recorded = state["comments"]["s2"]
    assert len(recorded) == 1
    assert recorded[0]["id"] == comment_id
    assert recorded[0]["who"] == "worker-a"
    assert recorded[0]["body"] == "<p>the worker records its own landing</p>"
    rows = ledger.runs(PROJECT, root=repository)
    assert rows[0]["outcome"] == "the coordinator narrative the worker already recorded"