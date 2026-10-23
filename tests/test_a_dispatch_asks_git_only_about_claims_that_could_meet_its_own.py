"""A dispatch's claim read probes git only for claims that could meet its own.

A live pointer's disposition costs a ``git status`` and a reachability query in
its worktree, and resolving its repository costs a ``git rev-parse``. A dispatch
can collide with a claim only through a path the two both declare, so a pointer
whose declared paths reach none of the dispatch's writes cannot turn its
disposition into a refusal. This file holds that the read spends nothing on
those, that the claims it does share still refuse and release as before, and
that the scopes view — which asks about no particular dispatch — still judges
every claim.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import node as node_module
from reckon.crew.dispatch_claims import _repository_scope_claims

CONFIG = {
    "default_backend": "worker",
    "backends": {
        "worker": {
            "launch": "cli",
            "command": "codex",
            "sandbox": "worktree-full",
            "time_budget": "20m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}


def _directory_snapshot(path: Path) -> tuple[str, ...]:
    if not path.is_dir():
        return ()
    return tuple(sorted(item.name for item in path.iterdir()))


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    live_directory = crew.live_dir()
    before = _directory_snapshot(live_directory)
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    yield config_home
    assert _directory_snapshot(live_directory) == before


def _repository(root: Path, projects: tuple[str, ...]) -> Path:
    (root / "skills" / "reckon-build" / "scripts").mkdir(parents=True)
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "package").mkdir()
    fleet_script = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (root / "skills" / "reckon-build" / "scripts" / "worktree_fleet.py").write_text(
        fleet_script.read_text(encoding="utf-8"), encoding="utf-8"
    )
    for project in projects:
        (root / "docs" / "plans" / f"{project}-plan.html").write_text(
            f'<meta name="docs-project" content="{project}">'
            '<meta name="reckon-type" content="plan">'
            f'<meta name="plan-slug" content="{project}-plan">'
            '<h2 id="dispatch">Dispatch</h2>',
            encoding="utf-8",
        )
    (root / "package" / "target.py").write_text("value = 1\n", encoding="utf-8")
    (root / "package" / "schema.yaml").write_text("value: 1\n", encoding="utf-8")
    (root / "package" / "generated.py").write_text("value = 1\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "docs", "package", "skills"],
        ["commit", "-q", "-m", "chore: seed fixture"],
    ):
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)
    return root


def _mounts(home: Path, projects: dict[str, Path]) -> None:
    (home / "mounts.json").write_text(
        json.dumps({project: str(repo / "docs") for project, repo in projects.items()}),
        encoding="utf-8",
    )


def _node(project: str, path: str, *, node_id: str = "candidate") -> crew.TaskNode:
    return crew.TaskNode(
        id=node_id,
        goal="record one repository claim result",
        plan=f"{project}-plan",
        section="dispatch",
        spec_level="exact",
        done_when="pytest reports all repository claim checks passing",
        write_paths=[path],
        time_budget="20m",
    )


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _own_checkout(path: Path) -> Path:
    """One claim's own checkout path, the shape a fleet gives each pointer.

    The read caches a checkout's repository identity by the path it probed, so
    two pointers spelling two paths pay twice whatever those paths hold; a
    fixture handing every claim one shared path measures a cache hit no fleet
    produces, and a bound read off it is a bound on nothing.
    """
    path.mkdir(parents=True)
    return path


def _reaped_pid() -> int:
    """Return a process id that has certainly exited and been collected."""
    worker = subprocess.Popen(["true"])
    worker.wait()
    return worker.pid


def _commit_in(worktree: Path, path: str, body: str) -> None:
    (worktree / path).write_text(body, encoding="utf-8")
    _git(worktree, "add", path)
    _git(
        worktree,
        "-c",
        "user.email=worker@example.invalid",
        "-c",
        "user.name=Worker",
        "commit",
        "-q",
        "-m",
        "fix(package): change the value",
    )


def _claim(
    repo: Path,
    *,
    project: str,
    path: str,
    run_id: str = "r-owner",
    member: str = "",
    authority_repositories: tuple[Path, ...] = (),
    worktree: Path | None = None,
    pid: int | None = None,
    base_sha: str = "",
) -> dict:
    record = {
        "run_id": run_id,
        "project": project,
        "repo": str(repo.resolve()),
        "phase": "working",
        "member": member,
        "node": {
            "id": "owner-node",
            "plan": f"{project}-plan",
            "write_paths": [path],
        },
    }
    if worktree is not None:
        record["worktree"] = str(worktree)
    if pid is not None:
        record["pid"] = pid
    if base_sha:
        record["base_sha"] = base_sha
    if authority_repositories:
        record["authority"] = {
            "repositories": [str(root.resolve()) for root in authority_repositories],
            "write": {"projects": [project]},
        }
    crew._write_json(crew.pointer_path(run_id), record)
    return record


# ── The claim read narrows to the claims that could meet it ─────────────────

_DISJOINT_PER_REPOSITORY = 10


class _GitProbe:
    """A stand-in for the subprocess module that records every git call.

    ``reckon.crew.node`` reaches git only through ``subprocess.run``, so
    replacing that name in its namespace counts exactly the probes one claim
    read makes, without touching any other caller's.
    """

    def __init__(self, real):
        self._real = real
        self.calls = []

    def run(self, argv, *args, **kwargs):
        command = [str(item) for item in argv]
        if command and command[0] == "git":
            self.calls.append((command, str(kwargs.get("cwd") or "")))
        return self._real.run(argv, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)

    def status_and_reachability(self):
        return [
            call
            for call in self.calls
            if len(call[0]) > 1 and call[0][1] in {"status", "rev-list"}
        ]

    def identity_probes(self):
        return [
            call
            for call in self.calls
            if len(call[0]) > 2
            and call[0][1:3] == ["rev-parse", "--path-format=absolute"]
        ]


def _stopped_claim(repo, *, project, path, run_id, worktree):
    """One stopped live pointer whose worktree holds the paths it declares."""
    return _claim(
        repo,
        project=project,
        path=path,
        run_id=run_id,
        worktree=worktree,
        pid=_reaped_pid(),
        base_sha=_git(repo, "rev-parse", "HEAD"),
    )


def _two_repositories_with_one_overlapping_claim(tmp_path, home):
    """Twenty disjoint stopped claims and one whose claim meets the dispatch.

    The twenty are split evenly over two repositories and declare files the
    dispatch does not; each is stopped, so reading its disposition costs a
    ``git status`` and a reachability query the read must not pay. Each also
    names its own checkout, the shape a fleet runs in: a pointer's worktree is
    its own linked worktree, not the repository root its neighbours write from,
    so nothing about one claim's checkout is shared with another's.

    The twenty-first declares the ``package`` directory that contains the
    dispatch's ``package/target.py`` and holds it uncommitted, so its
    disposition binds.
    """
    first = _repository(tmp_path / "first", ("project-a",))
    second = _repository(tmp_path / "second", ("project-b",))
    _mounts(home, {"project-a": first, "project-b": second})
    checkouts = tmp_path / "checkouts"
    for index in range(_DISJOINT_PER_REPOSITORY):
        _stopped_claim(
            first,
            project="project-a",
            path=f"package/module_a{index}.py",
            run_id=f"r-disjoint-a{index}",
            worktree=_own_checkout(checkouts / f"a{index}"),
        )
        _stopped_claim(
            second,
            project="project-b",
            path=f"package/module_b{index}.py",
            run_id=f"r-disjoint-b{index}",
            worktree=_own_checkout(checkouts / f"b{index}"),
        )
    (first / "package" / "target.py").write_text("value = 2\n", encoding="utf-8")
    overlapping = _stopped_claim(
        first,
        project="project-a",
        path="package",
        run_id="r-overlapping",
        worktree=first,
    )
    return first, second, overlapping


def test_the_read_probes_git_only_for_a_claim_that_can_meet_its_own(
    home, tmp_path, monkeypatch
):
    first, second, overlapping = _two_repositories_with_one_overlapping_claim(
        tmp_path, home
    )
    candidate = (first / "package" / "target.py").resolve()
    probe = _GitProbe(node_module.subprocess)
    monkeypatch.setattr(node_module, "subprocess", probe)

    claims = _repository_scope_claims(candidate_paths=[candidate])

    matched = [claim for claim in claims if claim.run_id == overlapping["run_id"]]
    assert matched, [claim.run_id for claim in claims]
    assert all(claim.binding for claim in matched)
    assert all(not claim.unread for claim in matched)

    # Every disposition spent is spent for the one run: twenty-one pointers over
    # two repositories cost one status and one reachability query, and no other
    # pointer's worktree is read at all.
    spent = probe.status_and_reachability()
    assert len(spent) == 2, spent
    assert {Path(cwd).resolve() for _command, cwd in spent} == {first.resolve()}

    # A claim the read never places is never probed in its checkout. The twenty
    # disjoint claims name twenty checkouts of their own, and the read reaches
    # git in none of them: every probe it makes lands on a root it had to place a
    # claim through, at most one per distinct root rather than one per pointer.
    probes = probe.identity_probes()
    assert probes, probe.calls
    probed = [Path(cwd).resolve() for _command, cwd in probes]
    assert set(probed) <= {first.resolve(), second.resolve()}, probes
    assert len(set(probed)) == len(probed) <= 2, probes
    assert not [cwd for cwd in probed if (tmp_path / "checkouts") in cwd.parents], (
        probes
    )


def test_a_claim_that_cannot_meet_the_dispatch_is_returned_unread(home, tmp_path):
    first, _second, overlapping = _two_repositories_with_one_overlapping_claim(
        tmp_path, home
    )
    candidate = (first / "package" / "target.py").resolve()

    claims = _repository_scope_claims(candidate_paths=[candidate])

    unread = {claim.run_id for claim in claims if claim.unread}
    assert overlapping["run_id"] not in unread, unread
    assert len(unread) == _DISJOINT_PER_REPOSITORY * 2, unread


def test_the_overlapping_claim_refuses_the_dispatch_and_names_the_owner(home, tmp_path):
    first, _second, overlapping = _two_repositories_with_one_overlapping_claim(
        tmp_path, home
    )

    with pytest.raises(crew.ScopeConflict) as excinfo:
        crew.dispatch(
            node=_node("project-a", "package/target.py"),
            project="project-a",
            repo=first,
            config=CONFIG,
            session="overlapping-claim-refuses",
            check_budget=False,
            launcher=lambda *args, **kwargs: pytest.fail("conflict must not launch"),
        )

    assert excinfo.value.run_id == overlapping["run_id"]
    assert overlapping["run_id"] in str(excinfo.value)


def test_the_dispatch_itself_probes_git_only_for_a_claim_that_can_meet_its_own(
    home, tmp_path, monkeypatch
):
    """The production call, not the private read, is the path this bounds.

    A dispatch reaches the claim read through its own admission check, and a
    read that narrows only when a caller names its candidate paths leaves that
    call site spending a disposition per live pointer. This asserts the bound
    from the arbitration's own entry: the same fixture as the private read's
    test, dispatched rather than read directly.
    """
    first, _second, overlapping = _two_repositories_with_one_overlapping_claim(
        tmp_path, home
    )
    probe = _GitProbe(node_module.subprocess)
    monkeypatch.setattr(node_module, "subprocess", probe)

    with pytest.raises(crew.ScopeConflict) as excinfo:
        crew.dispatch(
            node=_node("project-a", "package/target.py"),
            project="project-a",
            repo=first,
            config=CONFIG,
            session="dispatch-reads-only-the-claim-that-can-meet-it",
            check_budget=False,
            launcher=lambda *args, **kwargs: pytest.fail("conflict must not launch"),
        )

    assert excinfo.value.run_id == overlapping["run_id"]

    spent = probe.status_and_reachability()
    assert len(spent) == 2, spent
    assert {Path(cwd).resolve() for _command, cwd in spent} == {first.resolve()}


def test_the_overlapping_claim_releases_once_its_work_is_integrated(home, tmp_path):
    first, _second, overlapping = _two_repositories_with_one_overlapping_claim(
        tmp_path, home
    )
    worktree = Path(overlapping["worktree"]).resolve()
    _git(worktree, "restore", "package/target.py")

    record = crew.dispatch(
        node=_node("project-a", "package/target.py"),
        project="project-a",
        repo=first,
        config=CONFIG,
        session="integrated-claim-releases",
        check_budget=False,
        launcher=lambda *args, **kwargs: 0,
    )

    assert crew.pointer_path(record["run_id"]).is_file()


def test_the_scopes_view_reports_a_disposition_for_every_claim(home, tmp_path):
    _two_repositories_with_one_overlapping_claim(tmp_path, home)

    claims = _repository_scope_claims()

    assert len({claim.run_id for claim in claims}) == _DISJOINT_PER_REPOSITORY * 2 + 1
    # A claim nobody judged is the one state this view may not report, because
    # the read's own default marks an unjudged claim binding with no reason:
    # reading `binding or disposition_reason` would pass on a claim carrying a
    # verdict nobody reached. So the unjudged flag is asserted directly, and
    # then the reason every judged claim names, which an unjudged claim's
    # absent reason would fail.
    unjudged = [claim.run_id for claim in claims if claim.unread]
    assert not unjudged, unjudged
    assert all(claim.disposition_reason for claim in claims), [
        claim.run_id for claim in claims if not claim.disposition_reason
    ]
