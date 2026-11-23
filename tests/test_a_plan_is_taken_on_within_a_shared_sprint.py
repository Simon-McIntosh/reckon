"""A session takes on an unowned plan only inside a sprint it works in.

A plan's coordinator is derived from the session that dispatched its most
recent run, so taking an unowned plan on is dispatching it. The lead's rule is
that this is allowed, but not as a free-for-all: the session must already
coordinate a plan in the same sprint. A session that does not is told so on
every section rather than queueing work it has no standing to take, and a
session that does take a plan on announces it to the project's other sessions
through one row on the watch stream. The admission service queues across every
sprint the project holds, so the rule does not constrain it.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import dispatch_batch, plan_coordinator, recovery_watch, runs
from reckon.crew.runs import pointer_path

PROJECT = "proj"
SESSION = "coordinator-a"
OTHER_SESSION = "coordinator-b"

#: The sprint the session's own plan sits in, shared with the plan it may take.
SHARED_SPRINT = "S1"
#: A sprint the session coordinates nothing in.
FOREIGN_SPRINT = "S2"

CONFIG = {
    "default_backend": "clive",
    "local_backend": "clive",
    "backends": {
        "clive": {
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

SECTIONS = ("s1", "s2", "s3")


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "--no-pager", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
    )


def _plan_html_text(slug: str, sprint: str, sections=SECTIONS) -> str:
    quote = '"'
    declarations = json.dumps(dict.fromkeys(sections, "implementable")).replace(
        quote, "&quot;"
    )
    body = "".join(
        f'<h2 id="{name}">Section {name}</h2>'
        f"<p>Build {name}.</p>"
        f"<p><strong>Done when</strong> the {name} check passes.</p>"
        for name in sections
    )
    return (
        "<!doctype html>\n"
        f'<meta name="docs-project" content="{PROJECT}">\n'
        '<meta name="reckon-type" content="plan">\n'
        f'<meta name="plan-slug" content="{slug}">\n'
        f'<meta name="plan-sprint" content="{sprint}">\n'
        '<meta name="plan-status" content="active">\n'
        '<meta name="plan-version" content="3">\n'
        f'<meta name="plan-section-declarations" content="{declarations}">\n'
        f"{body}\n"
    )


def _seed_checkout(root: Path, plans: dict[str, str]) -> Path:
    """A checkout the dispatch gates accept, with the plans it is handed."""
    scripts = root / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).parents[1] / "skills" / "reckon-build" / "scripts"
    (scripts / "worktree_fleet.py").write_text(
        (source / "worktree_fleet.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    plans_dir = root / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "docs" / "state" / PROJECT / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n",
        encoding="utf-8",
    )
    for slug, text in plans.items():
        (plans_dir / f"{slug}.html").write_text(text, encoding="utf-8")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "worker@example.invalid")
    _git(root, "config", "user.name", "Worker")
    _git(root, "add", "--", "docs", "skills")
    _git(
        root,
        "commit",
        "-q",
        "-m",
        "chore: seed checkout",
        "-m",
        "Seed the checkout the dispatch gates read.",
    )
    return root


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_VELOCITY_CACHE", str(config_home / "cache" / "velocity"))
    root = _seed_checkout(
        tmp_path / "checkout",
        {
            # The plan the session already coordinates, and the two unowned
            # plans: one in the same sprint, one in a sprint it does not work.
            "lead-plan": _plan_html_text("lead-plan", SHARED_SPRINT),
            "peer-plan": _plan_html_text("peer-plan", SHARED_SPRINT),
            "same-sprint": _plan_html_text("same-sprint", SHARED_SPRINT),
            "other-sprint": _plan_html_text("other-sprint", FOREIGN_SPRINT),
        },
    )
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _config_for(_project: str) -> dict:
    return json.loads(json.dumps(CONFIG))


def _dispatch(repository: Path, plan: str, session: str = SESSION) -> dict:
    return dispatch_batch.queue_plan(
        PROJECT,
        plan,
        session=session,
        config_for=_config_for,
        repo_root=repository,
    )


def _seed_run(plan: str, session: str, *, classification: str = "queued") -> str:
    run_id = f"r-20260101T000000000000-{plan}-s1-{session}"
    now = datetime.now(UTC).isoformat()
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "session": session,
        "plan": plan,
        "section": "s1",
        "node": {"id": f"{plan}-s1", "plan": plan, "section": "s1"},
        "phase": "queued",
        "classification": classification,
        "created_at": now,
        "dispatched_at": now,
        "queued_at": now,
    }
    runs._write_json(pointer_path(run_id), pointer)
    return run_id


def _coordinator_of(repository: Path, plan: str) -> tuple[bool, str | None]:
    resolution = plan_coordinator.resolve_plan_coordinators(PROJECT, root=repository)
    row = (resolution.get("plans") or {}).get(plan) or {}
    return bool(row.get("tier") == "coordinator"), row.get("coordinator")


def _watch_rows(plan: str) -> list[dict]:
    path = runs.watch_stream_path(PROJECT)
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and json.loads(line).get("plan") == plan
    ]


def test_a_session_takes_on_an_unowned_plan_of_its_own_sprint(repository: Path) -> None:
    """An unowned plan in the session's sprint queues, and the session is named."""
    _seed_run("lead-plan", SESSION)

    result = _dispatch(repository, "same-sprint")

    assert result["queued"] == len(SECTIONS), result
    for row in result["results"]:
        assert row["status"] in ("queued", "awaiting-upstream"), row

    tiered, coordinator = _coordinator_of(repository, "same-sprint")
    assert tiered, "the dispatch made the session the plan's coordinator"
    assert coordinator == SESSION, coordinator


def test_the_other_sessions_receive_the_row(repository: Path) -> None:
    """A second live session in the project reads the one take-on row."""
    _seed_run("lead-plan", SESSION)
    _seed_run("peer-plan", OTHER_SESSION, classification="waiting")

    _dispatch(repository, "same-sprint")

    rows = _watch_rows("same-sprint")
    assert len(rows) == 1, f"one take-on row, not {rows!r}"
    row = rows[0]
    assert row["kind"] == dispatch_batch.PLAN_TAKEN_ON_EVENT, row
    assert row["took_by"] == SESSION, row

    # The row is a broadcast, so the follower that will read it applies the
    # project-wide partition to the *other* session's pane too.
    own, _peers = recovery_watch._partition_session_rows(rows, OTHER_SESSION)
    assert rows[0] in own, "another live session reads the take-on row"


def test_a_plan_in_a_sprint_the_session_does_not_work_in_is_skipped(
    repository: Path,
) -> None:
    """An unowned plan outside the session's sprints queues nothing."""
    _seed_run("lead-plan", SESSION)

    result = _dispatch(repository, "other-sprint")

    assert result["queued"] == 0, result
    for row in result["results"]:
        assert row["status"] == "skipped", row
        assert row["reason"] == dispatch_batch.QUEUE_ANSWER_SPRINT, row
        assert row["queue_answer"] == dispatch_batch.QUEUE_ANSWER_SPRINT, row

    assert _watch_rows("other-sprint") == [], "no take-on row for a refused plan"
    live = list(runs.list_live(project=PROJECT))
    assert not [
        pointer
        for pointer in live
        if str((pointer.get("node") or {}).get("plan") or "") == "other-sprint"
    ], "a skipped plan has no request"


def test_the_service_identity_is_not_held_by_the_sprint_rule(repository: Path) -> None:
    """The admission service queues an unowned plan in any sprint."""
    _seed_run("lead-plan", SESSION)

    result = _dispatch(
        repository, "other-sprint", session=dispatch_batch.ADMISSION_SERVICE_SESSION
    )

    assert result["queued"] == len(SECTIONS), result
    for row in result["results"]:
        assert row["status"] in ("queued", "awaiting-upstream"), row
