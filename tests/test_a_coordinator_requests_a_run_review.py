"""A coordinator can compose the code review of one named delivered run.

The review reflex is the only composer of a delivered run's code review, so a
run it will not review — a head a resume left behind, or a previous round still
owing an answer — sat unreviewed with no command that could compose the one
review the reflex would not. These cases drive the coordinator's verb against a
real git history and a real review store: the command composes the review the
reflex composes, at the head the run carries now; a second composition for the
same head is refused because the first review stands; a run whose previous
round still carries an undisposed blocking finding is refused naming that
finding and the command that lifts it; and the review-missing obligation for
that run names the same cause rather than an overdue figure.

The base revision has no ``--run`` subject on the review verb, so the first
case fails there with no such option.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli
from reckon.crew import review as review_module
from reckon.crew import runs

dispatch_module = importlib.import_module("reckon.crew.dispatch")
obligations_module = importlib.import_module("reckon.crew.obligations")
recovery_review_dispatch = importlib.import_module(
    "reckon.crew.recovery_review_dispatch"
)

PROJECT = "coordinator-requests-a-run-review"
SESSION = "coordinator-fixture"
BLOCKING = review_module.BLOCKING_FINDING_SEVERITY

LOCAL_BACKEND = "clive"

CONFIG = {
    "default_backend": LOCAL_BACKEND,
    "local_backend": LOCAL_BACKEND,
    "backends": {
        LOCAL_BACKEND: {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "25m",
        }
    },
    "roles": {"implement": {}, "review": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}

FINDING = "the fit ignores the converter's behaviour at DC, so the model is wrong"


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _commit(repository: Path, paths: dict[str, str], message: str) -> str:
    for name, body in paths.items():
        target = repository / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    subprocess.run(
        ["git", "add", *paths], cwd=repository, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", message],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    return _git(repository, "rev-parse", "HEAD")


@pytest.fixture()
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A repository, an isolated crew home and a delivered implement run."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    # A run review follows the reviewed run onto the local lane, so the flight
    # the command resolves without an explicit config must name one. The
    # shipped defaults define the in-harness backend and no local lane.
    flight_file = config_home / "flight.yaml"
    flight_file.write_text(
        "version: 1\n"
        "default_backend: clive\n"
        "local_backend: clive\n"
        "backends:\n"
        "  clive:\n"
        "    launch: cli\n"
        "    command: codex\n"
        "    model: some-model\n"
        "    effort: high\n"
        "    sandbox: worktree-full\n"
        "    session_reuse: true\n"
        "    time_budget: 25m\n"
        "roles:\n"
        "  implement: {}\n"
        "  review: {}\n"
        "fences:\n"
        "  time_budget: 25m\n"
        "  needs_help_after_failures: 2\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("RECKON_FLIGHT_CONFIG", str(flight_file))
    repo = tmp_path / "repo"
    repo.mkdir()
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(repo, *arguments)
    _commit(repo, {"seed.txt": "seed\n"}, "chore: seed")
    delivered = _commit(
        repo,
        {
            "pkg/mod.py": "VALUE = 1\n",
            "docs/plans/fixture-plan.html": (
                '<meta name="docs-project" content="' + PROJECT + '">'
                '<meta name="reckon-type" content="plan">'
                '<meta name="plan-slug" content="fixture-plan">'
                '<meta name="plan-status" content="active">'
                '<h2 id="s1">The delivered change</h2>'
            ),
        },
        "feat: the delivered change",
    )
    (config_home / "mounts.json").write_text(
        '{"' + PROJECT + '": "' + str(repo / "docs") + '"}'
    )
    monkeypatch.setattr(
        review_module,
        "review_store_root",
        lambda base_dir=None: config_home / "reviews",
    )
    monkeypatch.setattr(
        obligations_module, "_utc_now", lambda: datetime(2026, 11, 22, tzinfo=UTC)
    )
    return {
        "tmp": tmp_path,
        "config_home": config_home,
        "repo": repo,
        "delivered": delivered,
    }


def _write_run(world: dict, *, run_id: str, head: str) -> dict:
    """One complete, unpromoted implement run whose worktree is the repo."""
    repo = world["repo"]
    manifest = world["config_home"] / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {run_id}\nstatus: complete\ncommits: [{head}]\n", encoding="utf-8"
    )
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "repo": str(repo),
        "worktree": str(repo),
        "process_alive": False,
        "launch": "in-harness",
        "role": "implement",
        "manifest_path": str(manifest),
        "node": {
            "id": run_id,
            "plan": "fixture-plan",
            "section": "s1",
            "write_paths": ["pkg/mod.py"],
        },
    }
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer


def _store_review(
    run_id: str, *, base: str, head: str, finding: str | None = None
) -> dict:
    """Store a review of ``run_id`` at the revision it read."""
    emitted = [
        f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
    ]
    if finding is not None:
        emitted.append(f"FINDING pkg/mod.py:1 {BLOCKING}: {finding}")
    return review_module.store_review(
        {
            **review_module.parse_review("\n".join(emitted)),
            "project": PROJECT,
            "reviewed_run_id": run_id,
            "review_run_id": f"review-of-{run_id}",
            "reviewed_base_sha": base,
            "reviewed_head_sha": head,
        }
    )


def _drive(world: dict, compose, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Run one composition inside a follower claim, capturing its launch."""
    repo = world["repo"]
    worktrees = world["tmp"] / "worktrees"
    captured: dict = {}

    def prepare(_repo, session, worker, base):
        path = worktrees / str(session) / str(worker)
        path.mkdir(parents=True, exist_ok=True)
        captured["worker"] = worker
        captured["path"] = str(path)
        return {
            "path": str(path),
            "base": base,
            "base_sha": _git(repo, "rev-parse", "HEAD"),
        }

    monkeypatch.setattr(dispatch_module, "_create_worktree", prepare)
    from reckon import crew
    from reckon.crew import recovery

    try:
        with runs.follower_claim(PROJECT, SESSION, delivery="stream"):
            report = compose(lambda *a, **k: os.getpid())
    finally:
        if crew.watch_state(PROJECT)["watcher_live"]:
            recovery.unwatch(PROJECT)
    return {**report, "_captured": captured}


def _live_review_ids(reviewed_run_id: str) -> list[str]:
    """Every live run whose pointer names this run as the one it reviews."""
    return [
        str(pointer.get("run_id") or "")
        for pointer in runs.list_live(project=PROJECT)
        if str(pointer.get("reviewed_run_id") or "") == reviewed_run_id
    ]


def _payload(outcome) -> dict:
    text = outcome.stdout.strip()
    try:
        return json.loads(text)
    except ValueError:
        return json.loads(text.splitlines()[-1])


def test_the_command_composes_a_review_of_a_delivered_run_at_its_head(
    world: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One command composes the run's review; the reflex composes no second."""
    run_id = "r-delivered"
    pointer = _write_run(world, run_id=run_id, head=world["delivered"])

    outcome = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "review",
            "--project",
            PROJECT,
            "--run",
            run_id,
            "--dry-run",
        ],
    )
    assert outcome.exit_code == 0, outcome.output
    preview = _payload(outcome)
    assert preview.get("dry_run") is True, preview
    # The preview names the review node keyed by the run and the head it read,
    # so the head composed for is the run's own head and not the checkout's.
    argv = " ".join(preview["argv"])
    assert f"{run_id}-{world['delivered'][:12]}" in argv, argv

    # The command's own composition, driven through the real dispatch.
    report = _drive(
        world,
        lambda launcher: review_module.compose_run_review(
            run_id, config=CONFIG, launcher=launcher
        ),
        monkeypatch,
    )
    assert report["dispatched"] is True, report.get("reason")
    assert report["_captured"].get("worker"), report

    # The reflex asks the same question of the same record: the review the
    # command composed stands for the head, so no second one is composed.
    second = _drive(
        world,
        lambda launcher: recovery_review_dispatch.dispatch_review_for_run(
            runs.read_pointer(run_id), config=CONFIG, launcher=launcher
        ),
        monkeypatch,
    )
    assert second["dispatched"] is False, second
    assert "already in flight" in str(second.get("reason") or ""), second
    assert pointer["run_id"] == run_id


def test_a_run_whose_review_owes_a_blocking_finding_is_refused(
    world: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The command refuses over the finding, naming it and the dispose command."""
    run_id = "r-undisposed"
    _write_run(world, run_id=run_id, head=world["delivered"])
    _store_review(
        run_id, base=world["delivered"], head=world["delivered"], finding=FINDING
    )

    report = review_module.compose_run_review(run_id, config=CONFIG)

    assert report["dispatched"] is False, report
    assert report.get("refused") is True, report
    reason = str(report["reason"])
    assert "blocking finding(s) with no disposition" in reason, reason
    assert "pkg/mod.py:1" in reason, reason
    assert "crew dispose" in reason, reason
    assert _live_review_ids(run_id) == [], _live_review_ids(run_id)


def test_the_review_missing_duty_names_the_undisposed_finding(
    world: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The duty names the cause, not the overdue figure, when the reflex must refuse."""
    run_id = "r-moved-head"
    _write_run(world, run_id=run_id, head=world["delivered"])
    _store_review(
        run_id, base=world["delivered"], head=world["delivered"], finding=FINDING
    )
    moved = _commit(world["repo"], {"pkg/mod.py": "VALUE = 2\n"}, "fix: a later head")

    report = obligations_module.obligations(PROJECT, SESSION)
    duty = next(
        item for item in report["obligations"] if str(item.get("run_id")) == run_id
    )

    assert duty["kind"] == "review-missing", duty
    reason = str(duty.get("reason") or "")
    assert "blocking finding" in reason, reason
    assert "overdue by" not in reason, reason
    command = str(duty.get("next_command") or "")
    assert "crew dispose" in command, command
    # The head the duty speaks for is the one the run carries now, not the
    # revision its stored review read.
    assert str(duty.get("head") or "") == moved, duty
