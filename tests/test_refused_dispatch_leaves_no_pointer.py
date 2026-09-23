"""A refused dispatch leaves no pointer, no worktree and no watch row behind.

A dispatch that refuses after it has already cut a dispatch worktree must be
indistinguishable from one that never ran: no live pointer, no worktree for the
node, and no row in the project's watcher transition stream naming it. A live
pointer is the harmful residue, because a reader takes it for a run whose
process is gone without a manifest and reports it as a death — which is what
happened to imas-ambix-b6, where a refused dispatch's leftover pointer was read
as ``abandoned`` and its worktree was removed on the strength of that
misreading.

The residue is in dispatch's own unwind. On any failure after the worktree is
cut, ``dispatch`` unwires peer channels, signals the spawned worker, removes the
worktree, and only then unlinks the pointer. But the worktree remover refuses a
worktree that a live pointer still claims, and until this run's own pointer is
unlinked it is that claim. So every failure reached after the pointer is written
— the tree snapshot, the peer wiring, the watcher read, the member registration,
the final pointer write — meets the remover with its own pointer on disk, the
remover refuses, and the later unlink never runs: pointer, run directory and
worktree all survive as an orphan that reads as a death.

Each refusal path dispatch can return after worktree creation begins is forced
here in a temporary ``RECKON_HOME`` and repository, and each is asserted clean.
A positive control drives the ordinary successful path and shows every read the
assertion depends on does see a present trace, so an absence is a measurement
rather than a blind read. The real pointer directory under the user's config
home is asserted untouched, because an isolated read does not prove an isolated
write.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from reckon import crew
from reckon.crew import runs as runs_module
from reckon.crew.runs import _WatchStreamProducer, list_live, pointer_path

dispatch_module = importlib.import_module("reckon.crew.dispatch")

PROJECT = "refused-dispatch-fixture"

# A CLI backend, so a dispatch cuts a worktree and writes a live pointer rather
# than handing back an in-harness directive. Every launcher is supplied by the
# test, so no command named here is ever executed.
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

NODE_ID = "node-refused"
SESSION = "session-refused-dispatch"
REAL_LIVE = Path.home() / ".config" / "reckon" / "crew" / "live"


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
        '</head><body><h2 id="s5">Refusal leaves no trace</h2></body></html>'
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


def _node(config_home: Path, *, name: str = NODE_ID) -> crew.TaskNode:
    return crew.TaskNode(
        id=name,
        goal="force a refusal after worktree creation and leave no residue",
        plan="fixture",
        section="s5",
        role="implement",
        spec_level="guided",
        done_when=(
            "pytest reports the refusal path leaves no pointer, no worktree "
            "and no watch row for the node"
        ),
        write_paths=["src/refused.py"],
        time_budget="20m",
        manifest_path=str(config_home / "manifests" / f"{name}.md"),
    )


def _stub_launcher(pid: int = 4242):
    def launch(*_args: Any, **_kwargs: Any) -> int:
        return pid

    return launch


def _raising_launcher(exc: BaseException):
    def launch(*_args: Any, **_kwargs: Any) -> int:
        raise exc

    return launch


def _dispatch(
    config_home: Path, repo: Path, *, launcher, session: str = SESSION
) -> dict:
    return crew.dispatch(
        node=_node(config_home),
        project=PROJECT,
        repo=repo,
        config=CONFIG,
        session=session,
        launcher=launcher,
        check_budget=False,
    )


# ── The reads a clean refusal must satisfy ──────────────────────────────────


def live_pointers_naming(node_id: str) -> list[str]:
    """Run ids of live pointers of the fixture project that name the node."""
    naming = []
    for record in list_live(project=PROJECT):
        node = record.get("node") or {}
        if node.get("id") == node_id or node_id in str(record.get("run_id", "")):
            naming.append(str(record.get("run_id")))
    return naming


def run_directories_naming(config_home: Path, node_id: str) -> list[Path]:
    """Run directories left behind for the node, by directory name."""
    root = config_home / "crew" / "runs"
    if not root.exists():
        return []
    return [path for path in root.iterdir() if node_id in path.name]


def worktrees_naming(repo: Path, node_id: str) -> list[str]:
    """Registered worktree paths of the repository whose path names the node."""
    listing = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [
        line.removeprefix("worktree ")
        for line in listing.splitlines()
        if line.startswith("worktree ") and node_id in line
    ]


def transition_rows(config_home: Path) -> list[dict[str, Any]]:
    """Every row of the project's watcher transition stream, parsed."""
    path = runs_module.watch_stream_path(PROJECT)
    if not path.exists():
        return []
    rows = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        entry = raw.strip()
        if not entry:
            continue
        try:
            rows.append(json.loads(entry))
        except ValueError:
            continue
    return rows


def transition_rows_naming(config_home: Path, node_id: str) -> list[dict[str, Any]]:
    return [row for row in transition_rows(config_home) if node_id in json.dumps(row)]


def _publish_transitions() -> None:
    """Append this project's transitions exactly as the watcher seat does.

    ``list_live(project=...)`` publishes every fleet transition once, but only
    while a producer is registered. Registering one here drives the same read
    the durable watcher drives, so a pointer that survives a refusal would land
    a row in the stream.
    """
    previous = runs_module._WATCH_STREAM_PRODUCERS.get(PROJECT)
    runs_module._WATCH_STREAM_PRODUCERS[PROJECT] = _WatchStreamProducer(
        path=runs_module.watch_stream_path(PROJECT),
        known={},
        stall_window="5m",
    )
    try:
        list_live(project=PROJECT)
    finally:
        if previous is None:
            runs_module._WATCH_STREAM_PRODUCERS.pop(PROJECT, None)
        else:
            runs_module._WATCH_STREAM_PRODUCERS[PROJECT] = previous


def _assert_clean(config_home: Path, repo: Path, node_id: str = NODE_ID) -> None:
    """No live pointer, no run directory, no worktree and no watch row."""
    assert live_pointers_naming(node_id) == []
    assert run_directories_naming(config_home, node_id) == []
    assert worktrees_naming(repo, node_id) == []
    _publish_transitions()
    assert transition_rows_naming(config_home, node_id) == []


@pytest.fixture(autouse=True)
def real_live_directory_is_not_a_fixture_target() -> None:
    """No case may write this fixture's pointer into the real crew home.

    The real live directory is a shared resource under concurrent use by peer
    sessions, so it is checked for this fixture's own project rather than by a
    whole-directory listing: a listing changes whenever a peer dispatches, which
    says nothing about what this test wrote.
    """

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


# ── Positive control: the reads do see a present run ────────────────────────


def test_a_launched_dispatch_is_seen_by_every_read(
    home: tuple[Path, Path],
) -> None:
    """Shown present: the pointer, the worktree and the watch row all appear.

    Without this, a clean assertion on a refusal could pass because the reads
    are blind rather than because nothing was left.
    """
    config_home, repo = home

    record = _dispatch(config_home, repo, launcher=_stub_launcher())
    run_id = str(record["run_id"])

    assert live_pointers_naming(NODE_ID) == [run_id]
    _publish_transitions()
    assert transition_rows_naming(config_home, NODE_ID) != []
    assert worktrees_naming(repo, NODE_ID) != []


# ── The refusal paths ───────────────────────────────────────────────────────


def test_a_post_pointer_tree_snapshot_refusal_leaves_no_trace(
    home: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The leak: the snapshot raises after the pointer is written.

    It is the class the writer meets in production — the boundary baseline runs
    after dispatch's own writes, so any failure there is post-pointer.
    """
    config_home, repo = home
    monkeypatch.setattr(
        dispatch_module,
        "_repository_tree_snapshot",
        lambda *_a, **_k: (_ for _ in ()).throw(crew.CrewError("snapshot failed")),
    )

    with pytest.raises(crew.CrewError):
        _dispatch(config_home, repo, launcher=_stub_launcher())

    _assert_clean(config_home, repo)


def test_a_post_pointer_peer_wiring_refusal_leaves_no_trace(
    home: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second member of the post-pointer class, to show it is not one call."""
    config_home, repo = home
    monkeypatch.setattr(
        dispatch_module,
        "_wire_peer_channels",
        lambda *_a, **_k: (_ for _ in ()).throw(crew.CrewError("peer wiring failed")),
    )

    with pytest.raises(crew.CrewError):
        _dispatch(config_home, repo, launcher=_stub_launcher())

    _assert_clean(config_home, repo)


def test_a_pre_pointer_spawn_refusal_leaves_no_trace(
    home: tuple[Path, Path],
) -> None:
    """The worker never starts: the spawn raises a launch refusal."""
    config_home, repo = home

    with pytest.raises(crew.CrewError):
        _dispatch(config_home, repo, launcher=_raising_launcher(OSError("no exec")))

    _assert_clean(config_home, repo)


def test_a_pre_pointer_working_directory_refusal_leaves_no_trace(
    home: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The launch plan cannot resolve a working directory for the worktree."""
    config_home, repo = home
    monkeypatch.setattr(
        dispatch_module._backends,
        "launch_working_directory",
        lambda **_k: (_ for _ in ()).throw(
            dispatch_module._backends.BackendError("no working directory")
        ),
    )

    with pytest.raises(crew.CrewError):
        _dispatch(config_home, repo, launcher=_stub_launcher())

    _assert_clean(config_home, repo)


def test_a_refusal_over_an_existing_worktree_leaves_no_new_trace(
    home: tuple[Path, Path],
) -> None:
    """The refusal the plan calls correct: a worktree already sits at the path.

    The pre-existing worktree belongs to an earlier run, so it is the
    discarding command's to remove and dispatch must leave it untouched; what
    dispatch must not leave is a trace of its own — a pointer, a run directory
    or a watch row for the node it refused.
    """
    config_home, repo = home
    first = _dispatch(config_home, repo, launcher=_stub_launcher())
    # Drop the first run's pointer so the second dispatch is refused by the
    # occupied worktree rather than by a member still in flight.
    pointer_path(str(first["run_id"])).unlink()
    pre_existing = worktrees_naming(repo, NODE_ID)
    pre_run_dirs = run_directories_naming(config_home, NODE_ID)
    assert pre_existing != []

    with pytest.raises(crew.CrewError):
        _dispatch(config_home, repo, launcher=_stub_launcher())

    assert live_pointers_naming(NODE_ID) == []
    # The refused dispatch adds no run directory of its own; the first run's
    # directory is a live run's, not this refusal's residue.
    assert run_directories_naming(config_home, NODE_ID) == pre_run_dirs
    # The refusal does not over-reach: the earlier run's worktree stays.
    assert worktrees_naming(repo, NODE_ID) == pre_existing
