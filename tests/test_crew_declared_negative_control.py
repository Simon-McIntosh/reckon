"""A node that writes a check declares the mutation that check fails against.

A guard, assertion or test whose author never named the mutation it must fail
against is a check that passed by not exercising anything: three such checks
were landed in one week, each green under its own gate and each found by an
independent reviewer. The trigger is the node's declared write paths containing
a test file, which a dispatcher can evaluate without reading prose. Each test
here makes the guarded thing happen — a missing declaration, an undeclared red
log, a declaration of none — and reads the refusal or the recorded verdict,
rather than resting on the suite being green.

Each test declares the mutation it fails against, and the red log that mutation
produced is kept beside the passing one under the node's report directory:
``dispatch_refusal.red.log``, ``promotion_missing_log.red.log`` and
``promotion_unnamed_mutation.red.log``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import _plan_html, crew
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "proj"
PLAN = "plan-a"

# Every promotion below exists to exercise the negative-control contract, and a
# run whose write paths reach a test file also owes an independent review. The
# fixtures carry no review record and no review is the contract under test, so
# each promotion states the reason it lands without one — the same declaration
# the CLI takes as `--waive-unreviewed-promotion`.
REVIEW_WAIVER = "the negative-control contract is the subject under test"

DISPATCH_CONFIG = {
    "default_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "25m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
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


@pytest.fixture()
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


# ── dispatch: a test path with no declaration is refused ────────────────────


def _dispatch_repository(tmp_path: Path, home: Path, *, project: str = PROJECT) -> Path:
    root = tmp_path / "repo"
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "fixture.html").write_text(
        "<!doctype html>\n<html><head>\n"
        f'<meta name="docs-project" content="{project}">\n'
        '<meta name="reckon-type" content="plan">\n'
        '<meta name="plan-slug" content="fixture">\n'
        '</head><body><h2 id="guard">Guard</h2></body></html>\n',
        encoding="utf-8",
    )
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "worker@example.invalid")
    _git(root, "config", "user.name", "Worker")
    _git(root, "add", "seed.txt", "docs/plans/fixture.html")
    _git(root, "commit", "-q", "-m", "chore: seed")
    (home / "mounts.json").write_text(
        json.dumps({project: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _dispatch_node(
    home: Path, *, write_paths: list[str], negative_control: str = ""
) -> crew.TaskNode:
    return crew.TaskNode(
        id="node-control",
        goal="land the guard and declare the mutation it fails against",
        plan="fixture",
        section="guard",
        spec_level="exact",
        done_when="pytest reports one passing negative-control dispatch case",
        write_paths=list(write_paths),
        time_budget="20m",
        manifest_path=str(home / "manifests" / "node-control.md"),
        negative_control=negative_control,
    )


def test_dispatch_refuses_a_test_path_with_no_declaration(
    config_home: Path, tmp_path: Path
) -> None:
    """The guarded thing happens: a test path is declared and nothing else."""
    repo = _dispatch_repository(tmp_path, config_home)

    with pytest.raises(crew.CrewError) as refusal:
        crew.dispatch(
            node=_dispatch_node(config_home, write_paths=["tests/test_guard.py"]),
            project=PROJECT,
            repo=repo,
            config=DISPATCH_CONFIG,
            session="session-negative-control",
            launcher=lambda *args, **kwargs: 4242,
        )

    message = str(refusal.value)
    assert "negative_control" in message
    assert "tests/test_guard.py" in message
    # Nothing was created: no worktree, no live pointer.
    assert not list(crew.list_live(project=PROJECT))


def test_dispatch_admits_a_test_path_that_declares_its_mutation(
    config_home: Path, tmp_path: Path
) -> None:
    """The same node with the declaration is admitted, and carries it forward."""
    repo = _dispatch_repository(tmp_path, config_home)
    node = _dispatch_node(
        config_home,
        write_paths=["tests/test_guard.py"],
        negative_control="removing the watcher liveness guard turns it red",
    )

    resolution = crew.plan_dispatch(
        node=node, project=PROJECT, repo=repo, config=DISPATCH_CONFIG
    )

    assert resolution.validation.ok, resolution.validation.findings
    assert resolution.node.negative_control == (
        "removing the watcher liveness guard turns it red"
    )


# ── promotion: a passing gate needs the declared red log ────────────────────


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
def repository(config_home: Path, tmp_path: Path) -> Path:
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
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "worker@example.invalid")
    _git(root, "config", "user.name", "Worker")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt", "docs")
    _git(root, "commit", "-q", "-m", "test: seed repository")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _write_pointer(
    repository: Path,
    run_id: str,
    *,
    write_paths: list[str],
    negative_control: str = "",
    manifest_path: Path | None = None,
) -> None:
    node: dict = {
        "id": "node-a",
        "plan": PLAN,
        "section": "s2",
        "time_budget": "25m",
        "write_paths": list(write_paths),
    }
    if negative_control:
        node["negative_control"] = negative_control
    record: dict = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repository),
        "worktree": str(repository),
        "launch": "in-harness",
        "role": "implement",
        "backend": "native",
        "created_at": "2026-09-18T08:00:00Z",
        "node": node,
    }
    if manifest_path is not None:
        record["manifest_path"] = str(manifest_path)
    _write_json(pointer_path(run_id), record)


def _write_manifest(path: Path, *, red_log: Path | None) -> None:
    lines = ["status: complete", "node: node-a", "commits: []"]
    if red_log is not None:
        lines.append(f"negative_control_log: {red_log}")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _promote(repository: Path, run_id: str, **kwargs):
    return crew.complete(run_id, root=repository, **kwargs)


def test_promotion_refuses_a_passing_gate_without_the_declared_red_log(
    repository: Path, tmp_path: Path
) -> None:
    """The guarded thing happens: a declaration with no red log delivered."""
    run_id = "r-20260919T090000000000-node-a"
    manifest = tmp_path / "manifest.md"
    _write_manifest(manifest, red_log=None)
    _write_pointer(
        repository,
        run_id,
        write_paths=["tests/test_guard.py"],
        negative_control="removing the guard turns the fixture red",
        manifest_path=manifest,
    )

    with pytest.raises(crew.CrewError) as refusal:
        _promote(
            repository,
            run_id,
            gate="passed",
            outcome="the guard landed",
            review_waiver=REVIEW_WAIVER,
        )

    message = str(refusal.value)
    assert "removing the guard turns the fixture red" in message
    assert "negative_control_log" in message
    assert pointer_path(run_id).exists()


def test_promotion_refuses_a_log_that_does_not_name_the_declared_mutation(
    repository: Path, tmp_path: Path
) -> None:
    """A failing log for another reason is not the negative control."""
    run_id = "r-20260919T090100000000-node-a"
    red = tmp_path / "other_failure.log"
    red.write_text("FAILED tests/test_other.py - AssertionError\n", encoding="utf-8")
    manifest = tmp_path / "manifest.md"
    _write_manifest(manifest, red_log=red)
    _write_pointer(
        repository,
        run_id,
        write_paths=["tests/test_guard.py"],
        negative_control="removing the guard turns the fixture red",
        manifest_path=manifest,
    )

    with pytest.raises(crew.CrewError) as refusal:
        _promote(
            repository,
            run_id,
            gate="passed",
            outcome="the guard landed",
            review_waiver=REVIEW_WAIVER,
        )

    assert "removing the guard turns the fixture red" in str(refusal.value)


def test_promotion_admits_a_red_log_that_names_the_declared_mutation(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-20260919T090200000000-node-a"
    mutation = "removing the guard turns the fixture red"
    red = tmp_path / "guard_removed.log"
    red.write_text(
        f"FAILED tests/test_guard.py - {mutation}\n1 failed, 1 passed\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "manifest.md"
    _write_manifest(manifest, red_log=red)
    _write_pointer(
        repository,
        run_id,
        write_paths=["tests/test_guard.py"],
        negative_control=mutation,
        manifest_path=manifest,
    )

    promoted = _promote(
        repository,
        run_id,
        gate="passed",
        outcome="the guard landed",
        review_waiver=REVIEW_WAIVER,
    )

    assert promoted["negative_control"]["verdict"] == "matched"
    assert promoted["negative_control"]["declaration"] == mutation
    row = crew.ledger.runs(PROJECT, root=repository)
    landed = next(item for item in row if item.get("run_id") == run_id)
    assert landed["negative_control"]["verdict"] == "matched"


def test_a_none_declaration_is_recorded_rather_than_refused(
    repository: Path, tmp_path: Path
) -> None:
    """The explicit escape: no applicable mutation, stated with its reason."""
    run_id = "r-20260919T090300000000-node-a"
    manifest = tmp_path / "manifest.md"
    _write_manifest(manifest, red_log=None)
    reason = "the identity must hold for every input, so no mutation applies"
    _write_pointer(
        repository,
        run_id,
        write_paths=["tests/test_guard.py"],
        negative_control=f"none: {reason}",
        manifest_path=manifest,
    )

    promoted = _promote(
        repository,
        run_id,
        gate="passed",
        outcome="the guard landed",
        review_waiver=REVIEW_WAIVER,
    )

    assert promoted["negative_control"]["verdict"] == "none-recorded"
    assert promoted["negative_control"]["reason"] == reason


def test_a_none_declaration_without_a_reason_is_refused(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-20260919T090400000000-node-a"
    manifest = tmp_path / "manifest.md"
    _write_manifest(manifest, red_log=None)
    _write_pointer(
        repository,
        run_id,
        write_paths=["tests/test_guard.py"],
        negative_control="none",
        manifest_path=manifest,
    )

    with pytest.raises(crew.CrewError) as refusal:
        _promote(
            repository,
            run_id,
            gate="passed",
            outcome="the guard landed",
            review_waiver=REVIEW_WAIVER,
        )

    assert "none" in str(refusal.value)


def test_a_node_writing_no_test_path_is_exempt(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-20260919T090500000000-node-a"
    manifest = tmp_path / "manifest.md"
    _write_manifest(manifest, red_log=None)
    _write_pointer(
        repository,
        run_id,
        write_paths=["reckon/crew/dispatch.py"],
        negative_control="declared but irrelevant",
        manifest_path=manifest,
    )

    promoted = _promote(
        repository,
        run_id,
        gate="passed",
        outcome="source only",
        review_waiver=REVIEW_WAIVER,
    )

    assert promoted["negative_control"]["verdict"] == "exempt"
    assert promoted["negative_control"]["reason"] == "node-writes-no-test-path"
