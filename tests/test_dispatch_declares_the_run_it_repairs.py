"""A dispatch may declare the promoted run it repairs, and its landing inherits it.

A corrective run promotes without an operator flag because the plan movement it
inherits belongs to the run it corrects. Until now that inheritance was read
only from the record's ``attempt_kind`` — a resume or a redispatch — so a node
dispatched fresh to repair an already-promoted run carried no such marker and
its landing was refused for an unmoved impl unless the coordinator passed
``--no-impl-change`` by hand. This file names the ``--repairs`` declaration that
fills that gap: the target must be a promoted run of the project's own ledger,
the declaration is recorded on the new run's record, and promotion reads it
through the same corrective-attempt exemption ``attempt_kind`` uses.

Each case makes the guarded thing happen — the refusal the declaration lifts,
and the declaration that lifts it — rather than resting on the suite being
green.
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

DISPATCH_CONFIG = {
    "default_backend": "native",
    "backends": {
        "native": {
            "launch": "in-harness",
            "model": "embedded-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "time_budget": "20m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _write_plan(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{state['slug']}</title>"
        '</head><body><main class="plan-doc">'
        '<h2 id="s2">&sect;2 &mdash; Section two</h2>'
        "</main></body></html>\n"
    )
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")


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
    state, version = _store.read_plan(PROJECT, PLAN, repository, artifact_type="plan")
    state.update(fields)
    _store.write_plan(PROJECT, PLAN, state, version, repository, artifact_type="plan")


def _write_pointer(
    repository: Path,
    run_id: str,
    *,
    role: str = "implement",
    plan_impl_at_dispatch: float | None = None,
    project: str = PROJECT,
) -> None:
    record: dict = {
        "run_id": run_id,
        "project": project,
        "repo": str(repository),
        "worktree": str(repository),
        "launch": "in-harness",
        "role": role,
        "backend": "native",
        "created_at": "2026-09-18T08:00:00Z",
        "node": {
            "id": "node-a",
            "plan": PLAN,
            "section": "s2",
            "time_budget": "25m",
            "write_paths": [],
        },
    }
    if plan_impl_at_dispatch is not None:
        record["plan_impl_at_dispatch"] = plan_impl_at_dispatch
    _write_json(pointer_path(run_id), record)


def _provision_fleet_script(repository: Path) -> None:
    scripts = repository / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    source = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (scripts / "worktree_fleet.py").write_text(
        source.read_text(encoding="utf-8"), encoding="utf-8"
    )
    _git(repository, "add", "skills")
    _git(repository, "commit", "-q", "-m", "test: provision the fleet script")


def _dispatch_one(
    repository: Path, tmp_path: Path, node_id: str, *, repairs: str = ""
) -> dict:
    node = crew.TaskNode(
        id=node_id,
        goal="repair the run the dispatch names",
        plan=PLAN,
        section="s2",
        spec_level="guided",
        done_when="the section's tests pass and the plan records the advance",
        write_paths=["package/target.py"],
        time_budget="20m",
        manifest_path=str(tmp_path / f"{node_id}.md"),
    )
    return crew.dispatch(
        node=node,
        project=PROJECT,
        repo=repository,
        config=DISPATCH_CONFIG,
        session="dispatch-session",
        launcher=lambda *args, **kwargs: 42001,
        check_budget=False,
        repairs=repairs,
    )


def _promoted_run_that_moved_the_plan(repository: Path, run_id: str) -> dict:
    """Promote a predecessor whose landing advanced the plan's impl."""
    _write_pointer(repository, run_id, plan_impl_at_dispatch=0.4)
    _set_plan(repository, impl=0.6)
    return crew.complete(
        run_id, root=repository, gate="passed", outcome="the original node landed"
    )


def test_a_repairs_dispatch_records_the_field_and_promotes_exempt(
    repository: Path, tmp_path: Path
) -> None:
    """The declared repair lands without an operator impl flag.

    Both halves are read off one run: the dispatch records the repaired run id
    on the record it returns and on the pointer it writes, and the promotion of
    that same run is exempt because the plan did not move a second time. The
    plan genuinely did not move under the repair — the predecessor's landing is
    what advanced it — so removing the exemption would refuse this run, as the
    companion case below shows for a dispatch that declares no repair.
    """
    _provision_fleet_script(repository)
    _set_plan(repository, impl=0.4)
    _git(repository, "add", "docs")
    _git(repository, "commit", "-q", "-m", "test: the plan carries an impl")

    predecessor = "r-20260918T090000000000-node-original"
    moved = _promoted_run_that_moved_the_plan(repository, predecessor)
    assert moved["impl_move"]["verdict"] == "moved"

    record = _dispatch_one(repository, tmp_path, "repair-node", repairs=predecessor)
    assert record["repairs"] == predecessor
    assert crew.read_pointer(record["run_id"])["repairs"] == predecessor

    promoted = crew.complete(
        record["run_id"],
        root=repository,
        gate="passed",
        outcome="the repair corrected the original landing",
        no_commit="the dispatched node never ran; the repair declaration is the "
        "subject",
    )

    assert promoted["impl_move"]["verdict"] == "exempt"
    assert promoted["impl_move"]["reason"] == f"corrective-run:repairs:{predecessor}"


def test_a_repairs_dispatch_naming_an_unknown_run_is_refused(
    repository: Path, tmp_path: Path
) -> None:
    """A run id in no ledger and no pointer is refused before launch."""
    _provision_fleet_script(repository)

    with pytest.raises(crew.CrewError) as refusal:
        _dispatch_one(
            repository,
            tmp_path,
            "repairs-unknown",
            repairs="r-20260918T091000000000-nowhere",
        )

    assert "names no run in project" in str(refusal.value)


def test_a_repairs_target_not_yet_promoted_is_refused(
    repository: Path, tmp_path: Path
) -> None:
    """A run still in flight is named as such rather than as unknown."""
    _provision_fleet_script(repository)
    live = "r-20260918T091500000000-still-running"
    _write_pointer(repository, live)

    with pytest.raises(crew.CrewError) as refusal:
        _dispatch_one(repository, tmp_path, "repairs-inflight", repairs=live)

    assert "is not yet promoted" in str(refusal.value)


def test_a_repairs_target_from_another_project_is_refused(
    repository: Path, tmp_path: Path
) -> None:
    """A live run owned by another project is refused as that project's."""
    _provision_fleet_script(repository)
    foreign = "r-20260918T091700000000-other-project-run"
    _write_pointer(repository, foreign, project="other")

    with pytest.raises(crew.CrewError) as refusal:
        _dispatch_one(repository, tmp_path, "repairs-foreign", repairs=foreign)

    assert "belongs to project" in str(refusal.value)
    assert "'other'" in str(refusal.value)


def test_a_dispatch_with_no_repairs_is_still_refused_when_the_impl_did_not_move(
    repository: Path,
) -> None:
    """The exemption is the declaration, not the fixture: without it, refusal.

    The plan does not move under this run, so the promotion is refused and
    names the flag that waives it. This is the behaviour the ``--repairs``
    declaration replaces for a repair, and it is unchanged for everything else.
    """
    _set_plan(repository, impl=0.5)
    run_id = "r-20260918T092000000000-plain-node"
    _write_pointer(repository, run_id, plan_impl_at_dispatch=0.5)

    with pytest.raises(crew.CrewError) as refusal:
        crew.complete(run_id, root=repository, gate="passed", outcome="the work landed")

    assert "--no-impl-change" in str(refusal.value)
