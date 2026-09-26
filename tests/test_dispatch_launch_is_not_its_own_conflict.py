"""A launching dispatch holds its paths from the run id, not from its launch.

Every arbitration surface reads live pointers: a peer dispatch's admission
check, the review reflex deciding whether a review is already in flight, an
operator reading the fleet. A dispatch mints its run id at entry and wrote its
pointer only once the worktree was cut, the prompt composed and the peer
channels wired — so for that whole span it held its write paths invisibly. A
second dispatch arriving inside the span read no claim, took the same paths and
launched a duplicate worker, and the refusal that followed named the claim the
launching dispatch had made for itself.

Two halves are asserted here, and the first is the one that fails without the
fix. The first: while one dispatch is composing its launch, a second dispatch
of the same paths meets that dispatch's claim and is refused, naming it. The
second: the claim a dispatch makes is its own pointer — one live pointer per
launch, superseded by the complete record, and gone along with the worktree and
run directory when the launch refuses. A positive control drives the ordinary
launch and shows the reads every absence assertion depends on do see a present
run, so an absence is a measurement rather than a blind read.

The real pointer directory under the user's config home is asserted untouched
across every case, because an isolated read does not prove an isolated write.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from reckon import crew
from reckon.crew.runs import list_live, pointer_path

dispatch_module = importlib.import_module("reckon.crew.dispatch")

PROJECT = "launch-claim-fixture"
NODE_ID = "node-claiming"
SESSION = "session-launch-claim"
REAL_LIVE = Path.home() / ".config" / "reckon" / "crew" / "live"

# A CLI backend, so a dispatch composes a real launch and writes a live
# pointer rather than handing back an in-harness directive. Every launcher is
# supplied by the test, so no command named here is ever executed.
CONFIG: dict[str, Any] = {
    "default_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": False,
            "time_budget": "25m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _plan_document() -> str:
    return (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">'
        '<meta name="plan-title" content="fixture">'
        '<meta name="plan-status" content="active">'
        '<meta name="plan-impl" content="0">'
        '<meta name="plan-version" content="0">'
        '</head><body><h2 id="s5">A launch holds its own claim</h2></body></html>'
    )


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A temporary crew home and a repository that looks like a reckon mount."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repo = tmp_path / "repo"
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "fixture.html").write_text(_plan_document(), encoding="utf-8")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "docs"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        _git(repo, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    return config_home, repo


@pytest.fixture(autouse=True)
def real_live_directory_is_not_a_fixture_target() -> None:
    """No case may write this fixture's pointer into the real crew home."""

    def fixture_pointers() -> list[str]:
        found = []
        for path in REAL_LIVE.glob("*.json"):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if PROJECT in text or NODE_ID in text:
                found.append(path.name)
        return found

    assert fixture_pointers() == []
    yield
    assert fixture_pointers() == []


def _node(config_home: Path) -> crew.TaskNode:
    return crew.TaskNode(
        id=NODE_ID,
        goal="hold the declared paths from the run id to the launched record",
        plan="fixture",
        section="s5",
        role="implement",
        spec_level="guided",
        done_when="pytest reports one claim per launch and no residue on refusal",
        write_paths=["src/claimed.py"],
        time_budget="20m",
        manifest_path=str(config_home / "manifests" / f"{NODE_ID}.md"),
    )


def _dispatch(config_home: Path, repo: Path, *, session: str = SESSION) -> dict:
    return crew.dispatch(
        node=_node(config_home),
        project=PROJECT,
        repo=repo,
        config=CONFIG,
        session=session,
        launcher=lambda *_args, **_kwargs: os.getpid(),
        check_budget=False,
    )


# ── The reads a claim assertion depends on ──────────────────────────────────


def live_pointers_naming(node_id: str) -> list[str]:
    """Run ids of live pointers of the fixture project that name the node."""
    naming = []
    for record in list_live(project=PROJECT):
        node = record.get("node") or {}
        if node.get("id") == node_id or node_id in str(record.get("run_id", "")):
            naming.append(str(record.get("run_id")))
    return naming


def run_directories_naming(config_home: Path, node_id: str) -> list[Path]:
    root = config_home / "crew" / "runs"
    if not root.exists():
        return []
    return [path for path in root.iterdir() if node_id in path.name]


def worktrees_naming(repo: Path, node_id: str) -> list[str]:
    listing = _git(repo, "worktree", "list", "--porcelain")
    return [
        line.removeprefix("worktree ")
        for line in listing.splitlines()
        if line.startswith("worktree ") and node_id in line
    ]


def _assert_no_trace(config_home: Path, repo: Path) -> None:
    """No live pointer, no run directory and no worktree for the node."""
    assert live_pointers_naming(NODE_ID) == []
    assert run_directories_naming(config_home, NODE_ID) == []
    assert worktrees_naming(repo, NODE_ID) == []


# ── Positive control: the reads do see a present run ────────────────────────


def test_a_launching_dispatch_returns_its_run_id_and_one_pointer(
    home: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shown present: the launched record is the one live pointer there is.

    The stored record carrying the worker's pid and the worktree is what tells
    a claim apart from the launch it belongs to: the claim is published while
    neither exists, so a record that has both is the launch's own write.
    """
    config_home, repo = home
    worktree_path = repo.parent / "worktrees" / NODE_ID

    def prepare_worktree(_repo: Path, _session: str, _node: str, base: str) -> dict:
        worktree_path.mkdir(parents=True, exist_ok=True)
        return {"path": str(worktree_path), "base": base, "base_sha": base}

    monkeypatch.setattr(dispatch_module, "_create_worktree", prepare_worktree)

    record = _dispatch(config_home, repo)
    assert record["run_id"], "a launching dispatch is accepted and names its run"
    run_id = str(record["run_id"])

    assert live_pointers_naming(NODE_ID) == [run_id]
    stored = json.loads(pointer_path(run_id).read_text(encoding="utf-8"))
    assert stored["pid"] == os.getpid()
    assert stored["worktree"] == str(worktree_path)
    assert not any(row["run_id"] != run_id for row in list_live(project=PROJECT))


# ── The claim a launching dispatch holds ────────────────────────────────────


def test_a_second_dispatch_meets_the_claim_of_the_launch_in_flight(
    home: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect: the launching dispatch held its paths invisibly.

    A second dispatch is issued from inside the first one's worktree seam —
    after the first has claimed its paths and before its worktree exists, which
    is the span a duplicate worker was launched over. It must be refused, and
    the refusal must name the first run.
    """
    config_home, repo = home
    calls = {"count": 0}
    observed: dict[str, Any] = {}

    def seam(_repo: Path, _session: str, _node: str, base: str) -> dict:
        calls["count"] += 1
        path = tmp_path / "worktrees" / f"seam-{calls['count']}"
        path.mkdir(parents=True, exist_ok=True)
        if calls["count"] == 1:
            observed["held"] = live_pointers_naming(NODE_ID)
            try:
                observed["second"] = _dispatch(
                    config_home, repo, session="session-second"
                )
            except crew.ScopeConflict as exc:
                observed["refusal"] = exc
        return {"path": str(path), "base": base, "base_sha": base}

    monkeypatch.setattr(dispatch_module, "_create_worktree", seam)

    first = _dispatch(config_home, repo)
    first_run_id = str(first["run_id"])

    assert observed["held"] == [first_run_id]
    refusal = observed.get("refusal")
    assert isinstance(refusal, crew.ScopeConflict)
    assert refusal.run_id == first_run_id
    assert "src/claimed.py" in str(refusal)
    # The refusal left nothing of the refused dispatch behind, and the launch
    # that held the claim kept its own single pointer, run directory and
    # worktree. The second dispatch never reached the worktree seam at all.
    assert live_pointers_naming(NODE_ID) == [first_run_id]
    assert len(run_directories_naming(config_home, NODE_ID)) == 1
    assert sorted(path.name for path in (tmp_path / "worktrees").iterdir()) == [
        "seam-1"
    ]
    assert calls["count"] == 1


def test_a_second_dispatch_meets_the_claim_made_before_the_preflight(
    home: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claim is taken before the slow part, not after it.

    A dispatch is held at its first preflight step: its run id is minted and
    held for the seconds the preflight, the worktree and the launch compose
    take. A second dispatch of the same declared path is issued from inside
    that hold, which is the window a duplicate worker was launched over — the
    first dispatch's claim is published already, so the second meets it and is
    refused. Asserted after the hold returns: the second dispatch never reached
    its own worktree, and the first kept its single live pointer.
    """
    config_home, repo = home
    original = dispatch_module._refuse_over_concurrency_ceiling
    calls = {"worktrees": 0, "held": False}
    observed: dict[str, Any] = {}

    def seam(backend_name: str, backend: dict, project: str | None = None, **rest):
        if not calls["held"]:
            calls["held"] = True
            observed["held"] = live_pointers_naming(NODE_ID)
            try:
                observed["second"] = _dispatch(
                    config_home, repo, session="session-second"
                )
            except crew.ScopeConflict as exc:
                observed["refusal"] = exc
        return original(backend_name, backend, project, **rest)

    def prepare_worktree(_repo: Path, _session: str, _node: str, base: str) -> dict:
        calls["worktrees"] += 1
        path = tmp_path / "worktrees" / f"hold-{calls['worktrees']}"
        path.mkdir(parents=True, exist_ok=True)
        return {"path": str(path), "base": base, "base_sha": base}

    monkeypatch.setattr(dispatch_module, "_refuse_over_concurrency_ceiling", seam)
    monkeypatch.setattr(dispatch_module, "_create_worktree", prepare_worktree)

    first = _dispatch(config_home, repo)
    first_run_id = str(first["run_id"])

    assert observed["held"] == [first_run_id]
    assert "second" not in observed
    refusal = observed.get("refusal")
    assert isinstance(refusal, crew.ScopeConflict)
    assert refusal.run_id == first_run_id
    assert "src/claimed.py" in str(refusal)
    assert live_pointers_naming(NODE_ID) == [first_run_id]
    assert calls["worktrees"] == 1


def test_a_claim_appearing_during_the_launch_is_met_at_the_pointer_write(
    home: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A claim published while this dispatch composes is met before the write.

    Two dispatches can both pass the admission check before either has
    published a claim, so the reading at the pointer write is what makes the
    one arriving second lose. The peer claim here is published from inside the
    worktree seam, which is after this dispatch's own admission check.
    """
    config_home, repo = home
    peer_run_id = "r-peer-claims-the-path"

    def seam(_repo: Path, _session: str, _node: str, base: str) -> dict:
        path = tmp_path / "worktrees" / "seam-peer"
        path.mkdir(parents=True, exist_ok=True)
        crew._write_json(
            pointer_path(peer_run_id),
            {
                "run_id": peer_run_id,
                "project": PROJECT,
                "repo": str(repo),
                "node": {"id": "node-peer", "write_paths": ["src/claimed.py"]},
                "phase": "starting",
            },
        )
        return {"path": str(path), "base": base, "base_sha": base}

    monkeypatch.setattr(dispatch_module, "_create_worktree", seam)

    with pytest.raises(crew.ScopeConflict) as refusal:
        _dispatch(config_home, repo)

    assert refusal.value.run_id == peer_run_id
    # This dispatch's own claim was not what refused it: its run id is absent
    # from the refusal, and its claim was released with the rest of the launch.
    assert "session-launch-claim" not in str(refusal.value)
    _assert_no_trace(config_home, repo)


# ── The residue a refusal must not leave ────────────────────────────────────


def test_a_refusal_during_the_launch_leaves_no_claim_behind(
    home: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claim is published before the slow step, so it must also be released."""
    config_home, repo = home
    monkeypatch.setattr(
        dispatch_module,
        "_create_worktree",
        lambda *_a, **_k: (_ for _ in ()).throw(crew.CrewError("no worktree")),
    )

    with pytest.raises(crew.CrewError):
        _dispatch(config_home, repo)

    _assert_no_trace(config_home, repo)
