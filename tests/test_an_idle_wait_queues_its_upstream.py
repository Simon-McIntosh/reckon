"""An idle wait queues the section it waits on.

A request stored waiting on its upstream is classed by what could release it.
When nothing is building that upstream, the wait is work a coordinator already
chose to queue, one step further upstream, so the admission pass files the
upstream section's request through the whole-plan dispatch and the rule follows
what that request waits on. A wait is only reported to its owner when the queue
read refuses the upstream, and the duty then names that refusal rather than
telling the owner to queue work the pass cannot queue.
"""

from __future__ import annotations

import json
import subprocess
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import flight
from reckon.crew import obligations as obligations_module
from reckon.crew import queue_admission, runs
from reckon.crew.node import TaskNode

PROJECT = "proj"
SESSION = "coordinator-a"
WAITER_PLAN = "plan-wait"
CHAIN = ("plan-a", "plan-b", "plan-c")
UPSTREAM_PLAN = CHAIN[-1]

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


def _config(overrides: dict | None = None) -> dict:
    config = json.loads(json.dumps(CONFIG))
    config.update(overrides or {})
    return config


def _run(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "--no-pager", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
    )


def _plan_html(slug: str, *, waits: dict[str, list[str]] | None = None) -> str:
    """One plan declaring a single implementable section."""
    declares = json.dumps({"s1": "implementable"}).replace('"', "&quot;")
    depends = json.dumps(waits).replace('"', "&quot;") if waits else "{}"
    return (
        "<!doctype html>\n"
        f'<meta name="docs-project" content="{PROJECT}">\n'
        '<meta name="reckon-type" content="plan">\n'
        f'<meta name="plan-slug" content="{slug}">\n'
        '<meta name="plan-status" content="active">\n'
        '<meta name="plan-version" content="2">\n'
        f'<meta name="plan-section-declarations" content="{declares}">\n'
        f'<meta name="plan-section-depends-on" content="{depends}">\n'
        '<h2 id="s1">§1</h2>\n'
        "<p>Build s1.</p>\n"
        "<p><strong>Done when</strong> the s1 check passes.</p>\n"
    )


def _write_plan(repository: Path, slug: str, text: str) -> Path:
    path = repository / "docs" / "plans" / f"{slug}.html"
    path.write_text(text, encoding="utf-8")
    return path


def _commit(repository: Path, message: str) -> None:
    _run(repository, "add", "--", "docs")
    _run(repository, "commit", "-q", "-m", message, "-m", "Fixtures the waits read.")


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_VELOCITY_CACHE", str(config_home / "cache" / "velocity"))

    root = tmp_path / "checkout"
    scripts = root / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).resolve().parents[1] / "skills" / "reckon-build" / "scripts"
    (scripts / "worktree_fleet.py").write_text(
        (source / "worktree_fleet.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    state = root / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n",
        encoding="utf-8",
    )
    (root / "docs" / "plans").mkdir(parents=True)
    _write_plan(root, WAITER_PLAN, _plan_html(WAITER_PLAN, waits={"s1": []}))
    for slug in CHAIN:
        _write_plan(root, slug, _plan_html(slug))
    _run(root, "init", "-q", "-b", "main")
    _run(root, "config", "user.email", "worker@example.invalid")
    _run(root, "config", "user.name", "Worker")
    _run(root, "add", "--", "docs", "skills")
    _run(root, "commit", "-q", "-m", "chore: seed the checkout", "-m", "Seed it.")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


@pytest.fixture()
def project_config(repository: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """The configuration every resolution in the checkout answers with."""
    holder = {"config": _config()}

    def resolve(*_arguments, **_keywords):
        return types.SimpleNamespace(config=holder["config"])

    monkeypatch.setattr(flight, "resolve", resolve)
    return holder


def _declare_waits(repository: Path, plan: str, waits: dict[str, list[str]]) -> None:
    _write_plan(repository, plan, _plan_html(plan, waits=waits))
    _commit(repository, f"chore: declare what {plan} waits on")


def _queue_waiter(repository: Path, run_id: str = "r-waiter") -> dict:
    return queue_admission.queue_requests(
        [
            TaskNode(
                id="waiter",
                goal="build the waiting section",
                plan=WAITER_PLAN,
                section="s1",
                done_when="the waiting check passes",
                write_paths=["s1.txt"],
                time_budget="20m",
                spec_level="exact",
            )
        ],
        project=PROJECT,
        session=SESSION,
        repo_root=repository,
        config=_config(),
        run_ids={"waiter": run_id},
    )


def _live() -> list[dict]:
    return runs.list_live(project=PROJECT)


def _pointer(run_id: str) -> dict:
    pointer = runs.read_pointer(run_id)
    assert isinstance(pointer, dict), run_id
    return pointer


def _requests_on(plan: str) -> list[dict]:
    return [
        pointer
        for pointer in _live()
        if str((pointer.get("node") or {}).get("plan") or pointer.get("plan")) == plan
    ]


def _age(run_id: str, hours: float = 100.0) -> dict:
    pointer = _pointer(run_id)
    stale = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
    pointer["created_at"] = stale
    pointer["waits_at"] = stale
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer


def _declare_done(repository: Path, plan: str) -> None:
    """Mark a plan's section landed, as its promotion does."""
    text = _plan_html(plan).replace(
        json.dumps({"s1": "implementable"}).replace('"', "&quot;"),
        json.dumps({"s1": "done"}).replace('"', "&quot;"),
    )
    _write_plan(repository, plan, text)
    _commit(repository, f"chore: land {plan}")


def _idle_duties() -> list[dict]:
    view = obligations_module.obligations(PROJECT, SESSION)
    return [row for row in view["obligations"] if row["kind"] == "wait-idle"]


def test_an_idle_wait_queues_its_upstream_and_is_released_when_it_lands(
    repository: Path, project_config: dict
) -> None:
    """One pass files the upstream section and leaves the waiter no duty."""
    _declare_waits(repository, WAITER_PLAN, {"s1": [f"{UPSTREAM_PLAN}#s1"]})
    _queue_waiter(repository)
    _age("r-waiter")

    queue_admission.advance_waiting_requests(_live())

    filed = _requests_on(UPSTREAM_PLAN)
    assert [str((row.get("node") or {}).get("section")) for row in filed] == ["s1"], (
        filed
    )
    assert str(filed[0].get("session") or "") == SESSION, filed[0]

    waiter = _pointer("r-waiter")
    assert waiter["wait_class"] == "progressing", waiter
    assert waiter["phase"] == "awaiting-upstream", waiter
    assert not waiter.get("idle_reported_at"), waiter
    assert _idle_duties() == [], _idle_duties()

    # The rule reaches the request it filed: the waiter is released when the
    # upstream section lands, exactly as any other upstream wait is.
    _declare_done(repository, UPSTREAM_PLAN)
    queue_admission.advance_waiting_requests(_live())

    assert _pointer("r-waiter")["phase"] == "queued", _pointer("r-waiter")


def test_a_chain_of_three_upstreams_is_queued_to_its_root_in_one_pass(
    repository: Path, project_config: dict
) -> None:
    """Each request's own wait is queued, so the chain reaches its root."""
    _declare_waits(repository, CHAIN[2], {"s1": [f"{CHAIN[1]}#s1"]})
    _declare_waits(repository, CHAIN[1], {"s1": [f"{CHAIN[0]}#s1"]})
    _declare_waits(repository, WAITER_PLAN, {"s1": [f"{CHAIN[2]}#s1"]})
    _queue_waiter(repository)

    queue_admission.advance_waiting_requests(_live())

    for plan in CHAIN:
        assert _requests_on(plan), plan
    waiter = _pointer("r-waiter")
    assert waiter["wait_class"] == "progressing", waiter
    # The middle request waits on the root, so the pass read the request it had
    # just filed rather than stopping at the first upstream.
    middle = _requests_on(CHAIN[1])[0]
    assert str(middle.get("wait_class") or "progressing") == "progressing", middle


def test_the_upstream_is_queued_with_the_services_own_queueing_off(
    repository: Path, project_config: dict
) -> None:
    """The wait resolution files the upstream whether or not the service does."""
    from reckon.crew.admission import queueing

    off = _config({"service_queues_plans": False})
    assert flight.service_queues_plans(off) is False
    report = queueing.PlanQueue().fill(
        config_for=lambda _project: off,
        projects={PROJECT: repository / "docs"},
    )
    assert report["queued"] == 0, report

    _declare_waits(repository, WAITER_PLAN, {"s1": [f"{UPSTREAM_PLAN}#s1"]})
    _queue_waiter(repository)
    project_config["config"] = off
    queue_admission.advance_waiting_requests(_live())

    assert _requests_on(UPSTREAM_PLAN), _live()


def test_an_upstream_whose_review_is_owed_is_not_queued_and_the_duty_names_it(
    repository: Path, project_config: dict
) -> None:
    """A refused upstream is reported with the read's own reason."""
    _declare_waits(repository, WAITER_PLAN, {"s1": [f"{UPSTREAM_PLAN}#s1"]})
    _queue_waiter(repository)
    _age("r-waiter")
    project_config["config"] = _config({"plan_review_gate": "enforce"})

    queue_admission.advance_waiting_requests(_live())

    assert _requests_on(UPSTREAM_PLAN) == [], _live()
    waiter = _pointer("r-waiter")
    assert waiter["wait_class"] == "idle", waiter
    assert waiter["idle_reported_at"], waiter
    duties = _idle_duties()
    assert [row["run_id"] for row in duties] == ["r-waiter"], duties
    text = f"{duties[0]['reason']} {duties[0]['next_command']}"
    assert "review owed" in text, text
    assert (
        "queue the upstream this request waits on" not in duties[0]["next_command"]
    ), duties[0]


def test_a_wait_blocked_on_an_unbuilt_section_of_a_live_plan_is_queued(
    repository: Path, project_config: dict
) -> None:
    """A phase blocked on an upstream nobody builds in a live plan resolves by queueing it."""
    _declare_waits(repository, WAITER_PLAN, {"s1": [f"{UPSTREAM_PLAN}#s1"]})
    _queue_waiter(repository)
    pointer = _pointer("r-waiter")
    pointer["phase"] = "blocked-upstream"
    runs._write_json(runs.pointer_path("r-waiter"), pointer)

    queue_admission.advance_waiting_requests(_live())

    assert _requests_on(UPSTREAM_PLAN), _live()
    assert _pointer("r-waiter")["wait_class"] == "progressing", _pointer("r-waiter")
