"""A finished upstream releases its waiters, whatever shape the finish takes.

A dependent request is parked until its upstream is finished. Finishing is not
one event: a plan can be spent without being closed, a section can be landed
while a review of it is still live, and a future input can be named before the
upstream that writes it has run. Each case builds its own temporary repository
and crew home under a temporary directory and reads nothing outside it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from reckon import crew as crew_package
from reckon import ledger
from reckon.crew import queue_admission, recovery_vocabulary, runs
from reckon.crew.node import TaskNode

PROJECT = "proj"
SESSION = "coordinator-a"
UPSTREAM_PLAN = "plan-a"
DOWNSTREAM_PLAN = "plan-b"
UPSTREAM = "s1"
WAITER = "s2"
WAITER_RUN = "r-waiter"
UPSTREAM_RUN = "b-upstream"
PROMOTED_RUN = "r-promoted"
REVIEW_RUN = "r-plan-review"
METERED_LANE = "codex-lane"

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
        },
        METERED_LANE: {
            "launch": "cli",
            "command": "codex",
            "model": "metered-model",
            "effort": "medium",
            "sandbox": "worktree-full",
            "time_budget": "25m",
        },
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _attribute_json(value: object) -> str:
    return json.dumps(value).replace('"', "&quot;")


def _meta(name: str, value: str) -> str:
    return f'<meta name="{name}" content="{value}">\n'


def _plan_html(
    slug: str,
    declarations: dict[str, str],
    *,
    depends_on: str = "",
    waits: dict[str, list[str]] | None = None,
) -> str:
    parts = [
        "<!doctype html>\n",
        _meta("docs-project", PROJECT),
        _meta("reckon-type", "plan"),
        _meta("plan-slug", slug),
        _meta("plan-status", "active"),
        _meta("plan-section-declarations", _attribute_json(declarations)),
        _meta("plan-section-depends-on", _attribute_json(waits or {})),
    ]
    if depends_on:
        parts.append(_meta("plan-depends-on", depends_on))
    for index, section in enumerate(declarations, start=1):
        parts.append(f'<h2 id="{section}">§{index}</h2>\n')
    return "".join(parts)


def _write_plan(
    repository: Path,
    slug: str,
    declarations: dict[str, str],
    *,
    depends_on: str = "",
    waits: dict[str, list[str]] | None = None,
) -> Path:
    path = repository / "docs" / "plans" / f"{slug}.html"
    path.write_text(
        _plan_html(slug, declarations, depends_on=depends_on, waits=waits),
        encoding="utf-8",
    )
    return path


def _declare(repository: Path, slug: str, section: str) -> None:
    """Mark one section done in a plan, and commit it as a landing does."""
    path = repository / "docs" / "plans" / f"{slug}.html"
    text = path.read_text(encoding="utf-8")
    match = re.search(r'name="plan-section-declarations" content="([^"]*)"', text)
    assert match is not None, "the fixture always writes a declarations meta"
    raw = match.group(1)
    declarations = json.loads(raw.replace("&quot;", '"').replace("&amp;", "&"))
    declarations[section] = "done"
    path.write_text(
        text.replace(raw, _attribute_json(declarations), 1),
        encoding="utf-8",
    )
    _git(repository, "add", "--", str(path.relative_to(repository)))
    _git(repository, "commit", "-q", "-m", f"chore: land {section}")


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A checkout, a config home and a ledger, all this fixture's own."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for variable, leaf in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_STATE_HOME", "state"),
    ):
        (home / leaf).mkdir()
        monkeypatch.setenv(variable, str(home / leaf))
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    repository = tmp_path / "checkout"
    (repository / "docs").mkdir(parents=True)
    monkeypatch.setenv("RECKON_STATE_ROOT", str(repository / "docs" / "state"))
    state = repository / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps({"project": PROJECT, "section": 0, "data": {"_version": 0}}) + "\n",
        encoding="utf-8",
    )
    (repository / "docs" / "plans").mkdir(parents=True)
    _write_plan(
        repository,
        UPSTREAM_PLAN,
        {UPSTREAM: "implementable", WAITER: "implementable", "s3": "implementable"},
        waits={WAITER: [f"{UPSTREAM_PLAN}#{UPSTREAM}"]},
    )
    _write_plan(
        repository,
        DOWNSTREAM_PLAN,
        {"t1": "implementable"},
        depends_on=UPSTREAM_PLAN,
    )
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.email", "worker@example.invalid")
    _git(repository, "config", "user.name", "Worker")
    _git(repository, "add", "--", "docs")
    _git(repository, "commit", "-q", "-m", "chore: seed checkout")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(config_home / "mounts.json"))
    from reckon.crew.dispatch_sections import project_mount_repository

    resolved = project_mount_repository(PROJECT)
    assert resolved is not None, "the fixture registers its own checkout"
    assert Path(resolved).resolve() == repository.resolve(), (
        f"the guard resolves {resolved}, not the fixture checkout {repository}"
    )
    ledger_path = Path(ledger.ledger_path(PROJECT)).resolve()
    assert ledger_path.is_relative_to(repository.resolve()), (
        f"the ledger {ledger_path} lies outside the fixture's checkout"
    )
    return repository


def _config_for():
    config = json.loads(json.dumps(CONFIG))

    def resolve(pointer):
        return config

    return resolve


def _launcher(launched: list):
    def launch(plan, *, log_path, stderr_path, prompt_path):
        launched.append(Path(str(prompt_path)).name)
        return os.getpid()

    return launch


def _phase_of(plan: str, section: str) -> str:
    wanted = ledger.normalize_section(section)
    for pointer in runs.list_live(project=PROJECT):
        node = dict(pointer.get("node") or {})
        if str(node.get("plan")) != plan:
            continue
        if ledger.normalize_section(node.get("section")) != wanted:
            continue
        return str(pointer.get("phase") or "")
    return ""


def _pointer(run_id: str) -> dict:
    return runs.read_pointer(run_id)


def _node(repository: Path, node_id: str, plan: str, section: str, **extra) -> TaskNode:
    return TaskNode(
        id=node_id,
        goal=f"build {plan} {section}",
        plan=plan,
        section=section,
        done_when=f"pytest reports passed for {section}",
        manifest_path=str(repository / f"{node_id}-manifest.md"),
        write_paths=[f"{section}.txt"],
        time_budget="20m",
        spec_level="exact",
        **extra,
    )


def _queue(
    repository: Path,
    nodes: list[TaskNode],
    run_ids: dict[str, str],
    *,
    backend_name: str = "",
) -> dict:
    return queue_admission.queue_requests(
        nodes,
        project=PROJECT,
        session=SESSION,
        repo_root=repository,
        config=CONFIG,
        backend_name=backend_name,
        run_ids=run_ids,
    )


def _release(refs: list[str]) -> dict:
    queue_admission._RELEASE_TICK_LAST["at"] = 0.0
    return queue_admission._release_waiters(refs, project=PROJECT)


def _admit(launched: list) -> None:
    queue_admission.admit_once(config_for=_config_for(), launcher=_launcher(launched))


def _delivered_run(
    repository: Path,
    run_id: str,
    *,
    plan: str,
    section: str,
    role: str,
    landed: bool,
) -> str:
    """Record a delivered run of one section, landed or still off HEAD."""
    if landed:
        commit = _git(repository, "rev-parse", "HEAD")
    else:
        _git(repository, "checkout", "-q", "-b", f"delivery-{run_id}")
        path = repository / f"{run_id}.txt"
        path.write_text(f"{run_id}\n", encoding="utf-8")
        _git(repository, "add", "--", path.name)
        _git(repository, "commit", "-q", "-m", f"feat: deliver {run_id}")
        commit = _git(repository, "rev-parse", "HEAD")
        _git(repository, "checkout", "-q", "main")
    manifest = runs.run_dir(run_id) / runs.DEFAULT_MANIFEST_NAME
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"status: complete\ncommits: {commit}\nmanifest-version: 1\n",
        encoding="utf-8",
    )
    runs._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": SESSION,
            "phase": "delivered",
            "role": role,
            "repo": str(repository),
            "manifest_path": str(manifest),
            "node": {"id": f"n-{run_id}", "plan": plan, "section": section},
        },
    )
    return commit


def test_a_plan_whose_sections_are_all_done_releases_its_waiter(
    repo: Path,
) -> None:
    """A spent plan is a finished upstream, whether or not it was closed."""
    stored = _queue(
        repo,
        [_node(repo, "n-waiter", DOWNSTREAM_PLAN, "t1")],
        {"n-waiter": WAITER_RUN},
    )
    assert stored[WAITER_RUN]["phase"] == queue_admission.AWAITING_UPSTREAM_PHASE
    assert stored[WAITER_RUN]["waits"] == [UPSTREAM_PLAN]

    for section in (UPSTREAM, WAITER, "s3"):
        _declare(repo, UPSTREAM_PLAN, section)
    plan_text = (repo / "docs" / "plans" / f"{UPSTREAM_PLAN}.html").read_text(
        encoding="utf-8"
    )
    assert 'content="active"' in plan_text, "the upstream plan is still active"

    released = _release([UPSTREAM_PLAN])
    assert released["promoted"] == [WAITER_RUN], released
    assert _phase_of(DOWNSTREAM_PLAN, "t1") == queue_admission.QUEUED_PHASE


def test_a_plan_with_an_open_section_keeps_its_waiter_parked(repo: Path) -> None:
    """The discriminator: the same dependency, one section still open."""
    _queue(
        repo,
        [_node(repo, "n-waiter", DOWNSTREAM_PLAN, "t1")],
        {"n-waiter": WAITER_RUN},
    )
    _declare(repo, UPSTREAM_PLAN, UPSTREAM)
    _declare(repo, UPSTREAM_PLAN, WAITER)

    released = _release([UPSTREAM_PLAN])
    assert released["promoted"] == [], released
    assert _phase_of(DOWNSTREAM_PLAN, "t1") == (queue_admission.AWAITING_UPSTREAM_PHASE)


def test_a_future_input_is_queued_and_checked_at_its_release(repo: Path) -> None:
    """A path the upstream is to create is named now and read at release."""
    stored = _queue(
        repo,
        [
            _node(
                repo, "n-waiter", UPSTREAM_PLAN, WAITER, inputs=["upstream-output.txt"]
            )
        ],
        {"n-waiter": WAITER_RUN},
    )
    assert stored[WAITER_RUN]["phase"] == queue_admission.AWAITING_UPSTREAM_PHASE
    assert not (repo / "upstream-output.txt").exists()

    _declare(repo, UPSTREAM_PLAN, UPSTREAM)
    released = _release([f"{UPSTREAM_PLAN}#{UPSTREAM}"])
    assert released["promoted"] == [WAITER_RUN], released

    launched: list = []
    _admit(launched)
    assert launched == [], "the input check refuses the launch"
    failure = str(_pointer(WAITER_RUN).get("failure") or "")
    assert "upstream-output.txt" in failure, failure


def test_an_input_the_upstream_wrote_launches_the_released_request(repo: Path) -> None:
    """The discriminator: the same request once its input exists."""
    _queue(
        repo,
        [
            _node(
                repo, "n-waiter", UPSTREAM_PLAN, WAITER, inputs=["upstream-output.txt"]
            )
        ],
        {"n-waiter": WAITER_RUN},
    )
    (repo / "upstream-output.txt").write_text(
        "written by the upstream\n", encoding="utf-8"
    )
    _declare(repo, UPSTREAM_PLAN, UPSTREAM)
    _release([f"{UPSTREAM_PLAN}#{UPSTREAM}"])

    launched: list = []
    _admit(launched)
    assert len(launched) == 1, launched
    pointer = _pointer(WAITER_RUN)
    phase = str(pointer.get("phase") or "")
    assert phase not in queue_admission.WAITING_UPSTREAM_PHASES, pointer
    assert phase != queue_admission.QUEUED_PHASE, pointer
    assert _phase_of(UPSTREAM_PLAN, WAITER) == phase, pointer
    assert str(pointer.get("failure") or "") == ""


def test_a_metered_request_launches_when_its_upstream_lands(repo: Path) -> None:
    """The wait is stored on the lane; landing releases it under that lane."""
    stored = _queue(
        repo,
        [_node(repo, "n-waiter", UPSTREAM_PLAN, WAITER)],
        {"n-waiter": WAITER_RUN},
        backend_name=METERED_LANE,
    )
    assert stored[WAITER_RUN]["phase"] == queue_admission.AWAITING_UPSTREAM_PHASE

    _declare(repo, UPSTREAM_PLAN, UPSTREAM)
    _release([f"{UPSTREAM_PLAN}#{UPSTREAM}"])

    launched: list = []
    _admit(launched)
    assert len(launched) == 1, launched
    pointer = _pointer(WAITER_RUN)
    assert pointer["backend"] == METERED_LANE, pointer["backend"]
    phase = str(pointer.get("phase") or "")
    assert phase not in queue_admission.WAITING_UPSTREAM_PHASES, phase
    assert phase != queue_admission.QUEUED_PHASE, phase
    assert dict(pointer.get("node") or {}).get("plan") == UPSTREAM_PLAN, pointer


def test_a_live_review_pointer_does_not_hold_a_landed_section(repo: Path) -> None:
    """A reviewer's pointer is not a run of the section it reviews."""
    _queue(
        repo,
        [_node(repo, "n-waiter", UPSTREAM_PLAN, WAITER)],
        {"n-waiter": WAITER_RUN},
    )
    _declare(repo, UPSTREAM_PLAN, UPSTREAM)
    _delivered_run(
        repo,
        UPSTREAM_RUN,
        plan=UPSTREAM_PLAN,
        section=UPSTREAM,
        role=recovery_vocabulary.REVIEW_ROLE,
        landed=True,
    )

    released = _release([f"{UPSTREAM_PLAN}#{UPSTREAM}"])
    assert released["promoted"] == [WAITER_RUN], released
    assert _phase_of(UPSTREAM_PLAN, WAITER) == queue_admission.QUEUED_PHASE


def test_a_delivered_implementation_run_still_holds_its_section(repo: Path) -> None:
    """The discriminator: an implementation run's delivery does hold."""
    _queue(
        repo,
        [_node(repo, "n-waiter", UPSTREAM_PLAN, WAITER)],
        {"n-waiter": WAITER_RUN},
    )
    _declare(repo, UPSTREAM_PLAN, UPSTREAM)
    _delivered_run(
        repo,
        UPSTREAM_RUN,
        plan=UPSTREAM_PLAN,
        section=UPSTREAM,
        role="implement",
        landed=False,
    )

    released = _release([f"{UPSTREAM_PLAN}#{UPSTREAM}"])
    assert released["promoted"] == [], released
    assert _phase_of(UPSTREAM_PLAN, WAITER) == (queue_admission.AWAITING_UPSTREAM_PHASE)


def test_a_promoted_implementation_row_carries_the_plan_slug(repo: Path) -> None:
    """The row a landing writes names the plan the run built."""
    run_id = PROMOTED_RUN
    base = _git(repo, "rev-parse", "HEAD")
    path = repo / "s1.txt"
    path.write_text("s1\n", encoding="utf-8")
    _git(repo, "add", "--", path.name)
    _git(repo, "commit", "-q", "-m", "feat: deliver s1")
    head = _git(repo, "rev-parse", "HEAD")
    manifest = runs.run_dir(run_id) / runs.DEFAULT_MANIFEST_NAME
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: deliver-s1\n"
        "status: complete\n"
        f"commits: {head}\n"
        "changed_paths: s1.txt\n"
        "tests: pytest tests/test_a_finished_upstream_releases_its_waiters.py\n"
        "artifacts: none\n"
        "evidence_inputs: none\n"
        "blockers: none\n"
        f"orientation_base_sha: {base}\n",
        encoding="utf-8",
    )
    runs._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repo),
            "worktree": str(repo),
            "base_sha": base,
            "launch": "in-harness",
            "role": "implement",
            "manifest_path": str(manifest),
            "node": {
                "id": "deliver-s1",
                "plan": UPSTREAM_PLAN,
                "section": UPSTREAM,
                "write_paths": ["s1.txt"],
            },
        },
    )

    crew_package.complete(
        run_id,
        gate="not-run",
        outcome="this case reads the plan slug the row carries, not a gate",
        root=repo,
    )

    rows, _version = ledger.load(PROJECT)
    row = next(entry for entry in rows["runs"] if str(entry.get("run_id")) == run_id)
    assert row["plan"] == UPSTREAM_PLAN, row["plan"]


def test_a_promoted_plan_review_row_names_the_plan_it_reviewed(repo: Path) -> None:
    """A plan review's node id names its plan, so its row carries it."""
    run_id = REVIEW_RUN
    base = _git(repo, "rev-parse", "HEAD")
    manifest = runs.run_dir(run_id) / runs.DEFAULT_MANIFEST_NAME
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: plan-review\n"
        "status: complete\n"
        f"commits: {base}\n"
        "changed_paths: none\n"
        "tests: none\n"
        "artifacts: none\n"
        "evidence_inputs: none\n"
        "blockers: none\n"
        f"orientation_base_sha: {base}\n",
        encoding="utf-8",
    )
    runs._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repo),
            "worktree": str(repo),
            "base_sha": base,
            "launch": "in-harness",
            "role": recovery_vocabulary.REVIEW_ROLE,
            "subject": "plan",
            "plan_slug": UPSTREAM_PLAN,
            "manifest_path": str(manifest),
            "node": {
                "id": f"{recovery_vocabulary.PLAN_REVIEW_NODE_PREFIX}{UPSTREAM_PLAN}",
                "plan": "",
                "section": "",
                "role": recovery_vocabulary.REVIEW_ROLE,
            },
        },
    )

    crew_package.complete(
        run_id,
        gate="not-run",
        outcome="this case reads the plan slug the row carries",
        root=repo,
    )

    rows, _version = ledger.load(PROJECT)
    row = next(entry for entry in rows["runs"] if str(entry.get("run_id")) == run_id)
    assert row["plan"] == UPSTREAM_PLAN, row["plan"]
