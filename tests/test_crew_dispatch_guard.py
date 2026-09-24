"""Watcher dispatch guards over hermetic project state."""

from __future__ import annotations

import importlib
import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import runs


CONFIG = {
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


@pytest.fixture()
def isolated_project(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repo = tmp_path / "repo"
    scripts = repo / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    fleet_script = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (scripts / "worktree_fleet.py").write_text(
        fleet_script.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (plans / "fixture.html").write_text(
        """<!doctype html>
<html><head>
<meta name="docs-project" content="sample">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="fixture">
</head><body><h2 id="guard">Dispatch guard</h2></body></html>
""",
        encoding="utf-8",
    )
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills", "docs/plans/fixture.html"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        subprocess.run(
            ["git", *arguments], cwd=repo, check=True, capture_output=True
        )
    (config_home / "mounts.json").write_text(
        json.dumps({"sample": str(repo / "docs")}), encoding="utf-8"
    )
    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    watcher = {
        "arming_line": "reckon crew watch --project sample",
        "watcher_live": False,
        "watcher": {},
    }

    def watch_state(_project: str, *, session: str | None = None) -> dict:
        # The seat is faked; delivery is read from the real registry, because a
        # guard that both halves fake proves nothing about either.
        delivery = (
            runs.follower_state(_project, session) if session is not None else None
        )
        return {
            **watcher,
            "watcher": dict(watcher["watcher"]),
            "attach_line": runs._watch_attach_line(_project, session=session),
            "session": session,
            "session_attached": None if delivery is None else bool(delivery["live"]),
            "follower": {} if delivery is None else delivery["follower"],
        }

    def ensure_watch(_project: str, *, session: str | None = None) -> dict:
        watcher["watcher_live"] = True
        watcher["watcher"] = {"pid": 7319}
        return watch_state(_project, session=session)

    monkeypatch.setattr(dispatch_module, "watch_state", watch_state)
    monkeypatch.setattr(dispatch_module, "_ensure_watch_producer", ensure_watch)
    return config_home, repo


def _node(config_home: Path, name: str) -> crew.TaskNode:
    return crew.TaskNode(
        id=f"node-{name}",
        goal="record watcher state for one dispatch",
        plan="fixture",
        section="guard",
        spec_level="guided",
        done_when="pytest reports one passing watcher guard case",
        write_paths=[f"src/{name}.py"],
        time_budget="20m",
        manifest_path=str(config_home / "manifests" / f"{name}.md"),
    )


def _live_launcher(*_args, **_kwargs) -> int:
    """Stand in for the supervisor as a process that is running.

    The launcher seam replaces the supervisor, and the supervisor is a live
    process. A write claim is judged by the disposition of the run's recorded
    process, so a stub naming a pid that has exited leaves every owner's claim
    disregarded — an admitted second writer where the guard under test is the
    refusal of one.
    """
    return os.getpid()


def _dispatch(
    config_home: Path,
    repo: Path,
    name: str,
    *,
    watch_override: bool = False,
    write_paths: list[str] | None = None,
) -> dict:
    """Dispatch with this session's delivery registered, as a coordinator does.

    A producer alone does not admit a dispatch: the guard asks whether the
    dispatching session will hear the run finish, which is the only form of the
    question that a peer's seat cannot answer for you.
    """
    session = f"session-{name}"
    node = _node(config_home, name)
    if write_paths is not None:
        node.write_paths = write_paths
    with runs.follower_claim("sample", session, delivery="stream"):
        return crew.dispatch(
            node=node,
            project="sample",
            repo=repo,
            config=CONFIG,
            session=session,
            launcher=_live_launcher,
            watch_required=True,
            watch_override=watch_override,
        )


def test_first_dispatch_arms_a_watcher_without_a_waiver(
    isolated_project: tuple[Path, Path],
) -> None:
    config_home, repo = isolated_project

    record = _dispatch(config_home, repo, "first")

    assert record["watch"]["watcher_live"] is True
    assert record["watch"]["watcher"]["pid"] == 7319
    assert record["watch_override"] is None
    assert crew.read_pointer(record["run_id"])["watch_override"] is None


def test_occupied_project_reuses_the_watcher_armed_by_the_first_dispatch(
    isolated_project: tuple[Path, Path],
) -> None:
    config_home, repo = isolated_project
    owner = _dispatch(config_home, repo, "owner")

    accepted = _dispatch(config_home, repo, "accepted")

    assert accepted["watch"]["watcher_live"] is True
    assert accepted["watch"]["watcher"]["pid"] == owner["watch"]["watcher"]["pid"]
    # list_live attaches a read-side liveness re-derivation to each row without
    # writing it to the pointer, so compare the pointer fields dispatch defines.
    assert [
        {key: value for key, value in row.items() if key != "process_alive"}
        for row in crew.list_live(project="sample")
    ] == [owner, accepted]
    worktrees = subprocess.run(
        ["git", "worktree", "list"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "node-accepted" in worktrees


def test_two_concurrent_nodes_both_hold_the_shared_landing_paths(
    isolated_project: tuple[Path, Path],
) -> None:
    """The plan and evidence paths are not exclusive to the first node.

    Both nodes on one plan append their landing record to the same plan file
    and evidence record, so those paths cannot belong to whichever dispatches
    first. The exclusive-claim refusal exempts them, git merge resolves the
    appends, and a second dispatch on the same plan is admitted.
    """
    config_home, repo = isolated_project

    first = _dispatch(config_home, repo, "landing-owner")
    second = _dispatch(config_home, repo, "landing-second")

    for record in (first, second):
        declared = set(record["node"]["write_paths"])
        assert "docs/plans/fixture.html" in declared
        assert "docs/evidence/archive/fixture-landed.html" in declared
    assert (
        crew.read_pointer(second["run_id"])["node"]["write_paths"]
        == second["node"]["write_paths"]
    )


def test_peer_prompt_omits_shared_landing_paths(
    isolated_project: tuple[Path, Path],
) -> None:
    config_home, repo = isolated_project
    peer = _dispatch(
        config_home,
        repo,
        "landing-peer",
        write_paths=["reckon/crew/ticker.py"],
    )

    dispatched = _dispatch(config_home, repo, "landing-prompt")

    prompt = Path(dispatched["prompt_path"]).read_text(encoding="utf-8")
    peer_scope = next(
        line for line in prompt.splitlines() if peer["node"]["id"] in line
    )
    assert "docs/plans/fixture.html" not in peer_scope
    assert "docs/evidence/archive/fixture-landed.html" not in peer_scope
    assert "reckon/crew/ticker.py" in peer_scope


def _assert_attach_line_shape(
    line: str, project: str, session: str | None = None
) -> None:
    """Assert the attach line's shape, never a literal or the composer itself.

    The first token has to be an absolute path to the running ``reckon``
    console script, because the shell that arms the line need not carry the
    interpreter's bin directory on PATH. The remaining tokens are the fixed
    command carrying exactly the caller's project and session.
    """
    tokens = shlex.split(line)
    executable = tokens[0] if tokens else ""
    assert os.path.isabs(executable), f"the first token is not absolute: {line!r}"
    assert os.path.isfile(executable), f"the first token is not a file: {line!r}"
    assert os.access(executable, os.X_OK), f"the first token is not runnable: {line!r}"
    assert os.path.basename(executable) == "reckon", (
        f"the first token is not the reckon console script: {line!r}"
    )
    expected = ["crew", "follow", "--project", project]
    if session is not None:
        expected += ["--session", session]
    assert tokens[1:] == expected, (
        f"the attach line's arguments are not the fixed command: {line!r}"
    )


def test_no_watch_override_is_recorded_for_an_occupied_project(
    isolated_project: tuple[Path, Path],
) -> None:
    config_home, repo = isolated_project
    _dispatch(config_home, repo, "owner")

    waived = _dispatch(config_home, repo, "waived", watch_override=True)

    override = waived["watch_override"]
    assert override["requested"] is True
    assert override["arming_line"] == "reckon crew watch --project sample"
    assert override["watcher_live"] is True
    assert override["session_attached"] is True
    _assert_attach_line_shape(override["attach_line"], "sample", "session-waived")
    assert crew.read_pointer(waived["run_id"])["watch_override"] == override


def test_occupied_project_with_a_live_watcher_accepts_another_dispatch(
    isolated_project: tuple[Path, Path],
) -> None:
    config_home, repo = isolated_project
    owner = _dispatch(config_home, repo, "owner")
    accepted = _dispatch(config_home, repo, "accepted")

    assert accepted["watch"]["watcher_live"] is True
    assert accepted["watch"]["watcher"]["pid"] == owner["watch"]["watcher"]["pid"]
    assert accepted["watch_override"] is None


def test_member_lookup_uses_project_mount_from_another_repository(
    isolated_project: tuple[Path, Path], tmp_path: Path, monkeypatch
) -> None:
    config_home, plan_repo = isolated_project
    work_repo = tmp_path / "work-repo"
    scripts = work_repo / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    (work_repo / "docs").mkdir()
    fleet_script = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (scripts / "worktree_fleet.py").write_text(
        fleet_script.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (work_repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        subprocess.run(
            ["git", *arguments], cwd=work_repo, check=True, capture_output=True
        )
    mounts = json.loads((config_home / "mounts.json").read_text(encoding="utf-8"))
    mounts["work"] = str(work_repo / "docs")
    (config_home / "mounts.json").write_text(json.dumps(mounts), encoding="utf-8")
    crew.ledger.register_member("sample", "worker-a", harness="alpha", root=plan_repo)
    monkeypatch.chdir(work_repo)
    report = crew.reports_dir() / "sample" / "member-lookup.json"
    node = _node(config_home, "mounted-member")
    node.write_paths = [str(report)]

    record = crew.dispatch(
        node=node,
        project="sample",
        repo=plan_repo,
        config=CONFIG,
        session="mounted-member-session",
        member="worker-a",
        launcher=_live_launcher,
    )

    assert record["member"] == "worker-a"
    # The working directory is another registered repository's, and neither it
    # nor `--repo` decides: the project's mount does.
    assert Path.cwd() == work_repo
    assert record["repo"] == str(plan_repo.resolve())
    assert record["authority"]["plan"]["repository"] == str(plan_repo.resolve())
    # The node's own delivery path plus the shared landing paths dispatch grants
    # the fixture plan — its file, its cumulative evidence record and the plan's
    # figure topic directory — still declared relative to the repository that
    # owns it.
    assert sorted(record["node"]["write_paths"]) == sorted(
        [
            str(report),
            "docs/plans/fixture.html",
            "docs/evidence/archive/fixture-landed.html",
            "docs/figures/fixture",
        ]
    )


def _test_figures_root(repo: Path) -> Path:
    return repo / "docs" / "figures"


def test_a_second_node_on_a_claimed_figure_topic_is_refused_naming_the_owner(
    isolated_project: tuple[Path, Path],
) -> None:
    """A figure topic directory is an exclusive claim, not a shared workspace."""
    config_home, repo = isolated_project
    figures = _test_figures_root(repo)
    assert not figures.exists(), "the topic has no contents on disk"

    owner = _dispatch(
        config_home, repo, "capture-owner", write_paths=["docs/figures/capture"]
    )

    with pytest.raises(crew.ScopeConflict) as excinfo:
        _dispatch(
            config_home, repo, "capture-second", write_paths=["docs/figures/capture"]
        )

    refusal = excinfo.value
    assert refusal.run_id == owner["run_id"]
    assert refusal.node_id == owner["node"]["id"]
    assert refusal.candidate_path == "docs/figures/capture"
    assert refusal.claimed_path == "docs/figures/capture"
    assert owner["run_id"] in str(refusal)
    assert "node-capture-owner" in str(refusal)
    # A refused node never reaches worktree creation.
    worktrees = subprocess.run(
        ["git", "worktree", "list"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "node-capture-second" not in worktrees
    # The claim is asserted over a path that does not exist on disk.
    assert not (figures / "capture").exists()


def test_a_path_inside_a_claimed_figure_topic_is_refused_naming_the_owner(
    isolated_project: tuple[Path, Path],
) -> None:
    """The topic claim covers the tree, so a leaf inside it is a conflict."""
    config_home, repo = isolated_project
    owner = _dispatch(
        config_home, repo, "topic-owner", write_paths=["docs/figures/capture"]
    )

    with pytest.raises(crew.ScopeConflict) as excinfo:
        _dispatch(
            config_home,
            repo,
            "leaf-second",
            write_paths=["docs/figures/capture/after/plot.png"],
        )

    refusal = excinfo.value
    assert refusal.run_id == owner["run_id"]
    assert refusal.node_id == owner["node"]["id"]
    assert refusal.candidate_path == "docs/figures/capture/after/plot.png"
    assert refusal.claimed_path == "docs/figures/capture"
    assert owner["run_id"] in str(refusal)


def test_a_topic_claim_is_refused_by_a_leaf_already_claimed(
    isolated_project: tuple[Path, Path],
) -> None:
    """Tree coverage holds both ways: a directory claim meets a claimed leaf."""
    config_home, repo = isolated_project
    owner = _dispatch(
        config_home, repo, "leaf-owner", write_paths=["docs/figures/geo/plot.png"]
    )

    with pytest.raises(crew.ScopeConflict) as excinfo:
        _dispatch(config_home, repo, "topic-second", write_paths=["docs/figures/geo"])

    refusal = excinfo.value
    assert refusal.run_id == owner["run_id"]
    assert refusal.node_id == owner["node"]["id"]
    assert refusal.candidate_path == "docs/figures/geo"
    assert refusal.claimed_path == "docs/figures/geo/plot.png"


def test_the_refusal_holds_for_a_topic_directory_with_no_contents_on_disk(
    isolated_project: tuple[Path, Path],
) -> None:
    """A capture node claims a topic before any file exists in it.

    The exclusive claim is path-based, so an empty path binds exactly like one
    with contents; the fixture never creates ``docs/figures`` at all, which is
    the ordinary capture-node case and the one an existence check would mishandle.
    """
    config_home, repo = isolated_project
    figures = _test_figures_root(repo)
    assert not figures.exists()

    owner = _dispatch(
        config_home, repo, "fresh-owner", write_paths=["docs/figures/fresh-topic"]
    )
    assert not (figures / "fresh-topic").exists()

    with pytest.raises(crew.ScopeConflict) as excinfo:
        _dispatch(
            config_home, repo, "fresh-second", write_paths=["docs/figures/fresh-topic"]
        )
    assert excinfo.value.run_id == owner["run_id"]
    assert not figures.exists(), "declaring the claim creates nothing on disk"


def test_two_nodes_producing_one_figure_filename_is_a_scope_defect(
    isolated_project: tuple[Path, Path],
) -> None:
    """A figure is replaced wholesale; the same filename cannot be written twice."""
    config_home, repo = isolated_project
    owner = _dispatch(
        config_home, repo, "plot-owner", write_paths=["docs/figures/deploy/plot.png"]
    )

    with pytest.raises(crew.ScopeConflict) as excinfo:
        _dispatch(
            config_home,
            repo,
            "plot-second",
            write_paths=["docs/figures/deploy/plot.png"],
        )

    assert excinfo.value.run_id == owner["run_id"]
    assert excinfo.value.claimed_path == "docs/figures/deploy/plot.png"
    assert excinfo.value.candidate_path == "docs/figures/deploy/plot.png"


def test_disjoint_figure_topics_are_both_admitted(
    isolated_project: tuple[Path, Path],
) -> None:
    """The rule refuses overlap, not figure writes: two topics run concurrently."""
    config_home, repo = isolated_project

    first = _dispatch(config_home, repo, "topic-a", write_paths=["docs/figures/alpha"])
    second = _dispatch(config_home, repo, "topic-b", write_paths=["docs/figures/beta"])

    assert first["run_id"] != second["run_id"]
    for record in (first, second):
        declared = set(record["node"]["write_paths"])
        assert "docs/figures/alpha" in declared or "docs/figures/beta" in declared


def test_the_exclusive_claim_walk_reads_every_live_claim_at_refusal(
    isolated_project: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal walk reads every path the owner's run declared.

    One owner run declares its claimed figure topic directory, and dispatch
    grants it three shared landing paths — the plan file, its cumulative evidence
    record, and the plan's own figure topic directory — so the walk iterates four
    before refusing. The count is taken from the owner's own record rather than a
    literal, and the walk is instrumented, so a regression that stops walking a
    claim fails here without the expectation having to be re-typed.
    """
    config_home, repo = isolated_project
    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    owner = _dispatch(
        config_home, repo, "digit-owner", write_paths=["docs/figures/digit"]
    )
    claims_walked: dict[str, int] = {}
    real = dispatch_module._raise_repository_scope_conflict

    def wrapped(
        node,
        *,
        project,
        repo,
        authority,
        claims,
        disregarded=None,
    ):
        claims_walked["count"] = len(list(claims))
        return real(
            node,
            project=project,
            repo=repo,
            authority=authority,
            claims=claims,
            disregarded=disregarded,
        )

    monkeypatch.setattr(dispatch_module, "_raise_repository_scope_conflict", wrapped)
    with pytest.raises(crew.ScopeConflict):
        _dispatch(config_home, repo, "digit-second", write_paths=["docs/figures/digit"])

    assert claims_walked["count"] == len(owner["node"]["write_paths"])
    assert (
        crew.read_pointer(owner["run_id"])["node"]["write_paths"]
        == owner["node"]["write_paths"]
    )
