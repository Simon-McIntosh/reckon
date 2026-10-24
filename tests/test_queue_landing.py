"""The admit loop lands accepted deliveries and leaves refusals visible."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import ledger
from reckon.crew import obligations, queue_admission, queue_landing, runs
from reckon.crew import review as review_module
from reckon.crew.recovery_review_dispatch import _dispatch_text_review_for_run
from tests.helpers.git import git as fixture_git
from tests.helpers.landing_repository import (
    PLAN as LANDING_PLAN,
)
from tests.helpers.landing_repository import (
    PROJECT as LANDING_PROJECT,
)
from tests.helpers.landing_repository import _write_origin_and_checkout

PROJECT = "landing-fixture"
SESSION = "owner"
RUN = "r-delivered-implementation"
DISPATCH_CONFIG = {
    "default_backend": "local",
    "local_backend": "local",
    "backends": {
        "local": {
            "launch": "cli",
            "command": "codex",
            "model": "local-model",
            "effort": "medium",
            "sandbox": "worktree-full",
            "time_budget": "25m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture()
def delivery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str | Path]:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "worker@example.invalid")
    _git(repo, "config", "user.name", "Worker")
    plan = repo / "docs" / "plans" / "delivery-plan.html"
    plan.parent.mkdir(parents=True)
    plan.write_text(
        '<!doctype html><meta name="docs-project" content="landing-fixture">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="delivery-plan">'
        '<meta name="plan-status" content="active">'
        '<meta name="plan-section-declarations" '
        'content="{&quot;s2&quot;: &quot;implementable&quot;}">'
        '<h2 id="s2">Downstream</h2>\n',
        encoding="utf-8",
    )
    state = repo / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}),
        encoding="utf-8",
    )
    scripts = repo / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).parents[1] / "skills" / "reckon-build" / "scripts"
    (scripts / "worktree_fleet.py").write_text(
        (source / "worktree_fleet.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    (repo / "marker.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "marker.txt", "docs", "skills")
    _git(repo, "commit", "-q", "-m", "chore: seed fixture")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "marker.txt").write_text("delivered\n", encoding="utf-8")
    _git(repo, "add", "marker.txt")
    _git(repo, "commit", "-q", "-m", "feat: deliver marker")
    head = _git(repo, "rev-parse", "HEAD")
    manifest = runs.run_dir(RUN) / runs.DEFAULT_MANIFEST_NAME
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        f"node: implementation\nstatus: complete\ncommits: [{head}]\n",
        encoding="utf-8",
    )
    runs._write_json(
        runs.pointer_path(RUN),
        {
            "run_id": RUN,
            "project": PROJECT,
            "session": SESSION,
            "repo": str(repo),
            "worktree": str(repo),
            "process_alive": False,
            "launch": "in-harness",
            "role": "implement",
            "manifest_path": str(manifest),
            "node": {"id": "implementation", "write_paths": ["marker.txt"]},
        },
    )
    return {"repo": repo, "base": base, "head": head}


def _review(*, base: str, head: str, score: int = 20) -> None:
    parsed = review_module.parse_review(
        "\n".join(
            f"SCORE {dimension}: {score}"
            for dimension in review_module.REVIEW_DIMENSIONS
        )
    )
    parsed.update(
        {
            "project": PROJECT,
            "reviewed_run_id": RUN,
            "review_run_id": "review-of-delivery",
            "reviewed_base_sha": base,
            "reviewed_head_sha": head,
        }
    )
    review_module.store_review(parsed)


def test_accepted_delivery_lands_before_its_downstream_is_admitted(
    delivery: dict[str, str | Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _review(base=str(delivery["base"]), head=str(delivery["head"]))
    downstream = "r-downstream"
    runs._write_json(
        runs.pointer_path(downstream),
        {
            "run_id": downstream,
            "project": PROJECT,
            "session": SESSION,
            "repo": str(delivery["repo"]),
            "phase": queue_admission.AWAITING_UPSTREAM_PHASE,
            "waits": [f"run:{RUN}"],
            "wait_projects": {RUN: PROJECT},
            "backend": "local",
            "queued_at": datetime.now(UTC).isoformat(),
            "node": {
                "id": "downstream",
                "goal": "launch after delivery",
                "plan": "delivery-plan",
                "section": "s2",
                "role": "implement",
                "done_when": "pytest reports passed for downstream launch",
                "time_budget": "20m",
                "spec_level": "exact",
                "write_paths": ["downstream.txt"],
            },
        },
    )
    events: list[str] = []

    def land(run_id: str) -> tuple[bool, str]:
        assert run_id == RUN
        events.append("land")
        revision = str(delivery["head"])
        _git(Path(delivery["repo"]), "merge-base", "--is-ancestor", revision, "HEAD")
        row = ledger.run_path(PROJECT, run_id)
        row.parent.mkdir(parents=True, exist_ok=True)
        row.write_text(
            json.dumps({"run_id": run_id, "promoted_revision": revision}),
            encoding="utf-8",
        )
        runs.pointer_path(run_id).unlink()
        return True, ""

    def launch(_plan, *, log_path, stderr_path, prompt_path) -> int:
        assert not runs.pointer_path(RUN).exists()
        assert Path(prompt_path).exists()
        events.append("downstream-launched")
        return os.getpid()

    monkeypatch.setattr(queue_landing, "_land", land)
    monkeypatch.setattr(
        queue_admission, "_dispatch_fleet_gate", lambda: {"state": "open"}
    )
    monkeypatch.setattr(
        queue_admission, "_session_allowance_read", lambda *a, **k: (1, "fixture")
    )
    monkeypatch.setattr(queue_admission, "_lane_gate_hold", lambda *a, **k: None)
    monkeypatch.setattr(queue_admission, "_join_admitted_plan_review", lambda *a: "")
    monkeypatch.setattr(queue_admission, "_record_pass_verdicts", lambda *a, **k: None)
    result = queue_admission.admit_until_stopped(
        once=True, launcher=launch, config_for=lambda _pointer: DISPATCH_CONFIG
    )

    assert events == ["land", "downstream-launched"]
    assert result["landing"]["landed"] == [RUN]
    assert result["launched"] == [downstream]
    assert result["promoted_upstream"] == [downstream]


def test_review_of_older_head_cannot_land(
    delivery: dict[str, str | Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _review(base=str(delivery["base"]), head=str(delivery["base"]))
    monkeypatch.setattr(
        queue_landing, "_land", lambda run_id: pytest.fail("stale review landed")
    )

    assert queue_landing.land_once()["landed"] == []
    assert runs.pointer_path(RUN).exists()


def test_below_floor_review_raises_an_owner_obligation(
    delivery: dict[str, str | Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _review(base=str(delivery["base"]), head=str(delivery["head"]), score=15)
    monkeypatch.setattr(
        queue_landing, "_land", lambda run_id: pytest.fail("low review landed")
    )

    first = queue_landing.land_once()
    second = queue_landing.land_once()

    assert first["landed"] == []
    assert first["obligations"] == [
        {
            "run_id": RUN,
            "session": SESSION,
            "guard": "review acceptance floor or findings",
        }
    ]
    assert second["obligations"] == []
    assert runs.read_pointer(RUN)["admission_landing"]["status"] == "refused"
    duties = obligations.obligations(PROJECT, SESSION)["obligations"]
    assert any(item["run_id"] == RUN for item in duties)
    assert any(
        item["kind"] == "landing-refused" and "floor" in str(item.get("reason") or "")
        for item in duties
        if item["run_id"] == RUN
    ), duties


def test_refused_land_names_its_guard_and_is_never_retried(
    delivery: dict[str, str | Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _review(base=str(delivery["base"]), head=str(delivery["head"]))
    calls: list[str] = []

    def refuse(run_id: str) -> tuple[bool, str]:
        calls.append(run_id)
        return False, "index.lock stayed held"

    monkeypatch.setattr(queue_landing, "_land", refuse)
    first = queue_landing.land_once()
    second = queue_landing.land_once()

    assert calls == [RUN]
    assert first["obligations"][0]["guard"] == "index.lock stayed held"
    assert second["landed"] == []
    assert runs.pointer_path(RUN).exists()
    assert (
        runs.read_pointer(RUN)["admission_landing"]["guard"] == "index.lock stayed held"
    )
    duties = obligations.obligations(PROJECT, SESSION)["obligations"]
    assert any(
        item["kind"] == "landing-refused"
        and "index.lock stayed held" in item.get("reason", "")
        for item in duties
        if item["run_id"] == RUN
    ), duties


def test_an_interrupted_attempt_becomes_an_owner_duty_without_retry(
    delivery: dict[str, str | Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _review(base=str(delivery["base"]), head=str(delivery["head"]))
    runs._mutate_pointer(
        RUN,
        lambda pointer: {
            **pointer,
            "admission_landing": {
                "head": str(delivery["head"]),
                "guard": "crew land",
                "status": "attempting",
            },
        },
    )
    monkeypatch.setattr(
        queue_landing, "_land", lambda run_id: pytest.fail("interrupted land retried")
    )

    result = queue_landing.land_once()

    assert result["landed"] == []
    assert result["obligations"][0]["run_id"] == RUN
    assert "interrupted" in result["obligations"][0]["guard"]
    assert runs.read_pointer(RUN)["admission_landing"]["status"] == "refused"
    duties = obligations.obligations(PROJECT, SESSION)["obligations"]
    assert any(item["kind"] == "landing-refused" for item in duties)


def test_an_unchanged_head_is_not_reviewed_a_second_time(
    delivery: dict[str, str | Path],
) -> None:
    _review(base=str(delivery["base"]), head=str(delivery["head"]))

    result = _dispatch_text_review_for_run(runs.read_pointer(RUN), dry_run=True)

    assert result["dispatched"] is False
    assert "not awaiting review" in result["reason"]


def test_land_invokes_the_shared_path_through_the_scheduler_shim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def run(command, **options):
        captured.update(command=command, options=options)
        return subprocess.CompletedProcess(command, 0, json.dumps({"ok": True}), "")

    monkeypatch.setattr(queue_landing, "_run_srun", run)
    assert queue_landing._land(RUN) == (True, "")
    command = captured["command"]
    assert Path(command[0]).name == "srun"
    assert command[1:3] == ["--overlap", "--ntasks=1"]
    assert command[-7:] == [
        "crew",
        "complete",
        "--run",
        RUN,
        "--gate",
        "passed",
        "--land",
    ]
    assert captured["options"]["env"]["PYTHONPATH"].split(":")[0] == str(
        Path(queue_landing.__file__).resolve().parents[2]
    )


def test_accepted_delivery_uses_the_real_crew_land_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    origin, repository = _write_origin_and_checkout(tmp_path)
    (home / "mounts.json").write_text(
        json.dumps({LANDING_PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    base = fixture_git(repository, "rev-parse", "HEAD")
    worktree = tmp_path / "worker-tree"
    fixture_git(repository, "worktree", "add", "--detach", str(worktree), "HEAD")
    (worktree / "reviewed.txt").write_text("accepted delivery\n", encoding="utf-8")
    fixture_git(worktree, "add", "reviewed.txt")
    fixture_git(worktree, "commit", "-q", "-m", "feat: deliver accepted marker")
    head = fixture_git(worktree, "rev-parse", "HEAD")
    gate_log = tmp_path / "gate.log"
    gate_log.write_text(
        f"# revision: {head}\n# tree: {worktree}\n# command: true\nEXIT=0\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "manifest.md"
    manifest.write_text(
        f"node: implementation\nstatus: complete\ncommits: [{head}]\n"
        f"changed_paths: [reviewed.txt]\ntests: true\n"
        f"test_logs: [{gate_log}]\n",
        encoding="utf-8",
    )
    run_id = "r-20261024T090000000000-accepted-delivery"
    runs._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": LANDING_PROJECT,
            "session": "owner",
            "repo": str(repository),
            "worktree": str(worktree),
            "process_alive": False,
            "launch": "in-harness",
            "role": "implement",
            "backend": "native",
            "created_at": "2026-10-24T09:00:00Z",
            "base_sha": base,
            "manifest_path": str(manifest),
            "node": {
                "id": "implementation",
                "plan": LANDING_PLAN,
                "section": "s1",
                "write_paths": ["reviewed.txt"],
            },
        },
    )
    review = review_module.parse_review(
        "\n".join(
            f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
        )
    )
    review.update(
        {
            "project": LANDING_PROJECT,
            "reviewed_run_id": run_id,
            "review_run_id": "review-of-accepted-delivery",
            "reviewed_base_sha": base,
            "reviewed_head_sha": head,
        }
    )
    review_module.store_review(review)

    def run_in_shim(command, **_options):
        assert Path(command[0]).name == "srun"
        assert command[1:3] == ["--overlap", "--ntasks=1"]
        result = CliRunner().invoke(cli_module.main, command[6:])
        return subprocess.CompletedProcess(command, result.exit_code, result.output, "")

    monkeypatch.setattr(queue_landing, "_run_srun", run_in_shim)
    result = queue_landing.land_once()

    assert result["landed"] == [run_id], result
    assert fixture_git(repository, "show", "main:reviewed.txt") == "accepted delivery"
    assert fixture_git(origin, "rev-parse", "main") == fixture_git(
        repository, "rev-parse", "main"
    )
    assert not runs.pointer_path(run_id).exists()
