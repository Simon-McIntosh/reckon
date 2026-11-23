"""The admit service queues every section that may be queued.

The lead decided the queue must fill and drain unattended, so the service files
the queue itself rather than waiting for a coordinator's turn. These cases hold
it to the one read that says what may be queued: a reviewed plan's implementable
sections are filed in one pass and not again on the next, a section the read
holds is left beside the siblings it does not, a project that turns the setting
off gets none, a plan held before it had any request gets none until the hold is
released, and a request is filed under the plan's live coordinator or under the
service's own identity as the resolution answers.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import queue_edit, runs
from reckon.crew.admission import queueing
from reckon.crew.runs import pointer_path

PROJECT = "proj"
PLAN = "plan-a"
SECTIONS = ("s1", "s2", "s3")
COORDINATOR = "coordinator-live"
SERVICE = queueing.SERVICE_SESSION

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


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "--no-pager", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
    )


def _plan_html_text(slug: str, sections=SECTIONS) -> str:
    declarations = json.dumps(dict.fromkeys(sections, "implementable")).replace(
        '"', "&quot;"
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
        '<meta name="plan-status" content="active">\n'
        '<meta name="plan-version" content="3">\n'
        f'<meta name="plan-section-declarations" content="{declarations}">\n'
        f"{body}\n"
    )


def _seed_checkout(root: Path, plans: dict[str, str]) -> Path:
    scripts = root / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).resolve().parents[1] / "skills" / "reckon-build" / "scripts"
    (scripts / "worktree_fleet.py").write_text(
        (source / "worktree_fleet.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    plans_dir = root / "docs" / "plans"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    plans_dir.mkdir(parents=True, exist_ok=True)
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
        "--no-gpg-sign",
        "-q",
        "-m",
        "chore: seed checkout",
        "-m",
        "Seed the checkout the queue pass reads.",
    )
    return root


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_VELOCITY_CACHE", str(config_home / "cache" / "velocity"))
    root = _seed_checkout(tmp_path / "checkout", {PLAN: _plan_html_text(PLAN)})
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    queueing.forget_filled()
    return root


def _config(overrides: dict | None = None) -> dict:
    config = json.loads(json.dumps(CONFIG))
    config.update(overrides or {})
    return config


def _pass(root: Path, *, config: dict | None = None, store=None) -> dict:
    """One service pass over the mounted project, as the service runs it."""
    return queueing.PlanQueue().fill(
        config_for=lambda _project: config or _config(),
        projects={PROJECT: root / "docs"},
        **({"queue": store} if store is not None else {}),
    )


def _filed_requests() -> list[dict]:
    """The live requests this service filed, recognised by its own record."""
    return [
        pointer
        for pointer in runs.list_live(project=PROJECT)
        if (pointer.get("dispatch_options") or {}).get("queued_by") == SERVICE
        and str(pointer.get("phase") or "") in ("queued", "awaiting-upstream", "held")
    ]


def _sections_filed() -> list[str]:
    return sorted(
        str((pointer.get("node") or {}).get("section") or pointer.get("section") or "")
        for pointer in _filed_requests()
    )


def _live_pointer_for(plan: str, section: str, session: str) -> str:
    """A live request that makes ``session`` the plan's coordinator."""
    run_id = f"r-20260101T000000000000-{plan}-{section}"
    now = datetime.now(UTC).isoformat()
    runs._write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": session,
            "plan": plan,
            "section": section,
            "node": {"id": f"{plan}-{section}", "plan": plan, "section": section},
            "phase": "running",
            "created_at": now,
            "dispatched_at": now,
        },
    )
    return run_id


# --- cases -------------------------------------------------------------------


def test_a_reviewed_plan_is_queued_at_full_width_once(repository: Path) -> None:
    """One pass files every implementable section, and a second files none more."""
    first = _pass(repository)
    assert first["queued"] == 3, first
    assert _sections_filed() == ["s1", "s2", "s3"], _sections_filed()
    for pointer in _filed_requests():
        assert pointer["session"] == SERVICE, pointer
        assert (pointer.get("dispatch_options") or {}).get("queued_by") == SERVICE, (
            pointer
        )

    second = _pass(repository)
    assert second["queued"] == 0, second
    assert _sections_filed() == ["s1", "s2", "s3"], _sections_filed()


def test_a_section_the_read_holds_is_left_while_its_siblings_are_filed(
    repository: Path,
) -> None:
    """A section a coordinator held gets no request; the other two do."""
    held = _live_pointer_for(PLAN, "s1", "coordinator-other")
    record = runs.read_pointer(held)
    record["queue_hold"] = {
        "prior_phase": record["phase"],
        "started_at": datetime.now(UTC).isoformat(),
        "reason": "waiting on the lead's answer",
    }
    record["phase"] = "held"
    runs._write_json(pointer_path(held), record)

    report = _pass(repository)
    assert report["queued"] == 2, report
    assert _sections_filed() == ["s2", "s3"], _sections_filed()


def test_a_project_with_the_setting_off_gets_none(repository: Path) -> None:
    """The one flight setting a project switches the service's queueing off with."""
    report = _pass(repository, config=_config({"service_queues_plans": False}))
    assert report["queued"] == 0, report
    assert _filed_requests() == [], _filed_requests()


def test_a_held_plan_gets_none_and_gets_them_after_the_hold_is_released(
    repository: Path,
) -> None:
    """A plan held before it had a request is covered until the hold is released."""
    queue_edit.hold(
        plan=PLAN, project=PROJECT, reason="the lead has not decided the shape"
    )
    # Read back what was stored rather than what the call was handed, so the
    # guard fails if the hold never reaches the file the service reads.
    stored = queue_edit.plan_hold(PROJECT, PLAN)
    assert stored is not None, stored
    assert "not decided" in stored["reason"], stored
    assert stored["session"] == "", stored

    assert _pass(repository)["queued"] == 0
    assert _filed_requests() == [], _filed_requests()

    queue_edit.release(plan=PLAN, project=PROJECT, reason="the shape is settled")
    assert queue_edit.plan_hold(PROJECT, PLAN) is None

    assert _pass(repository)["queued"] == 3
    assert _sections_filed() == ["s1", "s2", "s3"], _sections_filed()


def test_a_pass_after_one_plan_moves_refiles_that_plan_alone(repository: Path) -> None:
    """The identity is per plan, so an untouched sibling is not parsed again."""
    queue = queueing.PlanQueue()
    queue.fill(projects={PROJECT: repository / "docs"})
    added = repository / "docs" / "plans" / "plan-b.html"
    added.write_text(_plan_html_text("plan-b"), encoding="utf-8")

    report = queue.fill(projects={PROJECT: repository / "docs"})
    examined = [
        entry["plan"]
        for entry in report["projects"][0]["plans"]
        if not entry.get("reason")
    ]
    assert examined == ["plan-b"], report


def test_a_pass_naming_no_configuration_resolves_each_projects_own(
    repository: Path,
) -> None:
    """The service's own pass supplies no callable and still files requests."""
    report = queueing.PlanQueue().fill(projects={PROJECT: repository / "docs"})
    assert [entry.get("error") for entry in report["projects"]] == [None], report
    assert report["queued"] == 3, report
    assert _sections_filed() == ["s1", "s2", "s3"], _sections_filed()


def test_a_plan_named_alone_is_held_without_a_project(repository: Path) -> None:
    """The plan alone is enough: its project comes from the mounts."""
    queue_edit.hold(plan=PLAN, reason="the lead has not decided the shape")
    assert queue_edit.plan_hold(PROJECT, PLAN) is not None
    assert _pass(repository)["queued"] == 0

    queue_edit.release(plan=PLAN, reason="the shape is settled")
    assert queue_edit.plan_hold(PROJECT, PLAN) is None
    assert _pass(repository)["queued"] == 3


def test_a_live_coordinators_plan_is_filed_under_its_session(
    repository: Path,
) -> None:
    """The requests name the coordinator the resolution finds behind the plan."""
    _live_pointer_for(PLAN, "s3", COORDINATOR)
    report = _pass(repository)
    assert report["queued"] == 2, report
    filed = _filed_requests()
    assert filed, report
    for pointer in filed:
        assert pointer["session"] == COORDINATOR, pointer
        assert (pointer.get("dispatch_options") or {}).get("queued_by") == SERVICE, (
            pointer
        )
