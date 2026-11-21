"""A named member's registration reaches the shared store, and commits nothing.

A dispatch that names no member is disposable: it runs under an identity minted
from its own run id and registers nothing, so no unrelated task is refused for a
row another run in flight happens to hold. A declared member is registered by
the registration code as one store transaction: the roster is store-only, so its
row is readable by every checkout rather than existing only in the tree of the
checkout that wrote it, and the registration commits no revision and stages no
path. The ``commit`` argument is retained for callers and changes no outcome,
and ``write_members`` pairs the roster version it read, so a writer that lost
that pairing is refused rather than overwriting the row of whichever writer won.

These tests build a throwaway repository; the suite's autouse fixture points the
configuration home at a temp tree, so the store they write is that temp tree's.
They assert a registration reaches the store and leaves the tree it ran in
exactly as it found it, that the row the store serves is the roster another
checkout reads, that a registration stages and commits nothing, that a caller
which does not ask to commit still persists its row, that a roster write which
lost its version race is refused and changes nothing, and that the live fleet
home and the shared checkout are untouched.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from reckon import crew, ledger
from reckon.crew import routing
from tests.helpers.git import git as _git
from tests.real_home_traces import traces_naming

PROJECT = "proj"
SESSION = "session-for-a-node"

CONFIG = {
    "default_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "medium",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "25m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


def _run(repository: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repository, check=True)


def _seed(repository: Path, tracked: list[str]) -> None:
    _run(repository, "init", "-q", "-b", "main")
    _run(repository, "config", "user.email", "worker@example.invalid")
    _run(repository, "config", "user.name", "Worker")
    if tracked:
        _run(repository, "add", "--", *tracked)
    _run(repository, "commit", "--allow-empty", "-q", "-m", "chore: seed fixture")


def _repository(tmp_path: Path) -> Path:
    """A throwaway checkout whose state directory is deliberately untracked.

    Nothing under ``docs/`` is committed, so any path a call touched outside the
    store would show up as a dirty entry rather than hiding inside an
    already-committed tree.
    """
    repository = tmp_path / "repository"
    state = repository / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n",
        encoding="utf-8",
    )
    _seed(repository, [])
    return repository


def _register_session(repository: Path, member_id: str = SESSION) -> dict:
    return routing._register_session_member(
        PROJECT,
        member_id,
        backend="alpha",
        role="implement",
        root=repository,
    )


def _stored_ids(repository: Path) -> list[str]:
    return [str(entry["id"]) for entry in ledger.members(PROJECT, root=repository)]


# The registration reaches the store, and writes no path


def test_a_session_registration_reaches_the_shared_store(tmp_path: Path) -> None:
    """The falsifier: another checkout reads the row the registration wrote."""
    repository = _repository(tmp_path)
    other = tmp_path / "other-checkout"
    other.mkdir()
    base = _git(repository, "rev-parse", "HEAD")
    before = _git(repository, "status", "--porcelain")
    assert ledger.members(PROJECT, root=other) == []

    stored = _register_session(repository)

    assert stored["id"] == SESSION
    assert _stored_ids(repository) == [SESSION]
    # The roster is the project's, not the checkout's: a reader resolving from
    # a second checkout answers the same rows.
    assert ledger.members(PROJECT, root=other) == ledger.members(
        PROJECT, root=repository
    )
    # A registration commits nothing and leaves the tree it ran in untouched.
    assert _git(repository, "status", "--porcelain") == before
    assert _git(repository, "rev-parse", "HEAD") == base


def test_a_registration_leaves_the_checkout_clean(tmp_path: Path) -> None:
    """The registration cannot ride whichever unrelated commit comes next."""
    repository = _repository(tmp_path)
    unrelated = repository / "notes.txt"
    unrelated.write_text("committed\n", encoding="utf-8")
    _run(repository, "add", "--", "notes.txt")
    _run(repository, "commit", "-q", "-m", "docs: add fixture note")
    unrelated.write_text("dirty\n", encoding="utf-8")
    base = _git(repository, "rev-parse", "HEAD")

    _register_session(repository)

    assert _git(repository, "rev-parse", "HEAD") == base
    assert _git(repository, "diff", "--cached", "--name-only") == ""
    assert _git(repository, "status", "--porcelain").splitlines() == [
        "M notes.txt",
        "?? docs/",
    ]


def test_the_stored_row_is_what_the_roster_serves(tmp_path: Path) -> None:
    repository = _repository(tmp_path)

    _register_session(repository)

    rows = ledger.members(PROJECT, root=repository)
    assert _stored_ids(repository) == [SESSION]
    assert ledger.member(PROJECT, SESSION, root=repository) == rows[0]
    assert rows[0]["harness"] == "alpha"
    assert rows[0]["role"] == "implement"


# A caller that does not ask to commit still persists its row


def test_a_caller_that_does_not_ask_to_commit_still_persists_the_row(
    tmp_path: Path,
) -> None:
    """The ``commit`` argument is retained for callers and changes no outcome."""
    repository = _repository(tmp_path)
    base = _git(repository, "rev-parse", "HEAD")

    ledger.register_member(PROJECT, "internal-a", harness="alpha", root=repository)

    assert _stored_ids(repository) == ["internal-a"]
    assert _git(repository, "rev-parse", "HEAD") == base
    assert _git(repository, "status", "--porcelain") == "?? docs/"


def test_a_roster_write_that_lost_its_version_race_is_refused(
    tmp_path: Path,
) -> None:
    """A stale expected version is refused, and the row already stored survives."""
    repository = _repository(tmp_path)
    _unused, stale_version = ledger.load_auxiliary(PROJECT, root=repository)
    ledger.register_member(PROJECT, "internal-a", harness="alpha", root=repository)
    ledger.register_member(PROJECT, "internal-b", harness="alpha", root=repository)

    with pytest.raises(ledger.LedgerError) as excinfo:
        ledger.write_members(
            PROJECT,
            [{"id": SESSION, "harness": "alpha"}],
            stale_version,
            root=repository,
        )

    assert "roster version changed" in str(excinfo.value)
    assert _stored_ids(repository) == ["internal-a", "internal-b"]
    assert _git(repository, "status", "--porcelain") == "?? docs/"


def _dispatch_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "checkout"
    scripts = repository / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).parents[1] / "skills" / "reckon-build" / "scripts"
    fleet = scripts / "worktree_fleet.py"
    fleet.write_text(
        (source / "worktree_fleet.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    state = repository / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    state.joinpath("index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + chr(10),
        encoding="utf-8",
    )
    plan = repository / "docs" / "plans" / "plan-a.html"
    plan.parent.mkdir(parents=True, exist_ok=True)
    q = chr(34)
    meta = "<meta name=" + q + "docs-project" + q + " content=" + q + "proj" + q + ">"
    heading = "<h2 id=" + q + "session-routing" + q + ">Session routing</h2>"
    plan.write_text(
        chr(10).join(["<!doctype html>", meta, heading]) + chr(10), encoding="utf-8"
    )
    _seed(repository, ["docs", "skills"])
    home = Path(os.environ["RECKON_HOME"])
    mounts = home.joinpath("mounts.json")
    mounts.write_text(json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8")
    return repository


def _dispatch_node(node_id: str, section: str = "session-routing") -> crew.TaskNode:
    return crew.TaskNode(
        id=node_id,
        goal="dispatch a node under this test's member identity",
        plan="plan-a",
        section=section,
        done_when="pytest tests/test_member_registration_commits.py passes",
        write_paths=["reckon/target.py"],
        time_budget="20m",
        spec_level="exact",
    )


def test_a_dispatch_naming_no_member_is_disposable_and_writes_nothing(
    tmp_path: Path,
) -> None:
    """The unnamed dispatch is disposable: no roster row, no write, own identity.

    A named member is registered first, so the roster the unnamed dispatch
    leaves alone is one these assertions have already seen hold a row.
    """
    repository = _dispatch_repository(tmp_path)
    ledger.register_member(PROJECT, "named-member", harness="alpha", root=repository)
    base = _git(repository, "rev-parse", "HEAD")
    before = ledger.members(PROJECT, root=repository)

    record = crew.dispatch(
        node=_dispatch_node("node-a"),
        project=PROJECT,
        repo=repository,
        config=CONFIG,
        session="coordinator-a",
        launcher=lambda plan, *, log_path, stderr_path, prompt_path: os.getpid(),
        check_budget=False,
    )

    assert str(record["member"]) == "disposable-" + str(record["run_id"])
    assert record["session_id"] is None
    assert _git(repository, "rev-list", "--count", base + "..HEAD") == "0"
    assert _git(repository, "status", "--porcelain") == ""
    assert _stored_ids(repository) == ["named-member"]
    assert ledger.members(PROJECT, root=repository) == before


def test_a_dispatch_naming_a_member_still_registers_and_serialises(
    tmp_path: Path,
) -> None:
    """A named member stays a durable route: the row exists and a second run is refused."""
    repository = _dispatch_repository(tmp_path)
    ledger.register_member(
        PROJECT, "named-member", harness="alpha", root=repository, commit=True
    )
    base = _git(repository, "rev-parse", "HEAD")
    node = _dispatch_node("node-b")

    first = crew.dispatch(
        node=node,
        project=PROJECT,
        repo=repository,
        config=CONFIG,
        session="coordinator-a",
        member="named-member",
        launcher=lambda plan, *, log_path, stderr_path, prompt_path: os.getpid(),
        check_budget=False,
    )

    assert first["member"] == "named-member"
    assert _git(repository, "rev-list", "--count", base + "..HEAD") == "0"

    with pytest.raises(crew.MemberInFlight):
        crew.dispatch(
            node=node,
            project=PROJECT,
            repo=repository,
            config=CONFIG,
            session="coordinator-a",
            member="named-member",
            launcher=lambda *args, **kwargs: pytest.fail(
                "the named member must remain serialised"
            ),
            check_budget=False,
        )


def test_the_live_home_and_the_shared_checkout_are_untouched(tmp_path: Path) -> None:
    checkout = Path(__file__).resolve().parents[1]
    real_home = Path.home() / ".config" / "reckon"
    # This test's own session id is the identifier a write of its would carry,
    # into the real home's crew tree or the shared checkout's state tree. A
    # peer registering its own member, or a peer commit under docs/state, moves
    # the same trees without anything being wrong here.
    #
    # Only the live pointer directory and the watcher stream are searched under
    # the crew home. The run archive beside them is not walkable at a useful
    # cost — its per-run transcript trees are unbounded on the shared store —
    # and a leaked pointer is the trace that matters, because a run directory
    # never outlives the live pointer that claims it.
    needles = (SESSION,)
    subtrees = ("live", "watch")
    before_home = traces_naming(real_home, subtrees, needles)
    before_state = traces_naming(checkout, ("docs/state",), needles)
    repository = _repository(tmp_path)

    _register_session(repository)

    assert ledger.ledger_path(PROJECT, repository).is_relative_to(tmp_path)
    assert not Path(os.environ["RECKON_HOME"]).is_relative_to(Path.home())
    assert traces_naming(real_home, subtrees, needles) == before_home
    assert traces_naming(checkout, ("docs/state",), needles) == before_state


def test_a_reap_before_a_registration_does_not_block_it(tmp_path: Path) -> None:
    """Both roster writes a dispatch makes are store transactions, one after the other.

    A dispatch reaps idle session rows before it registers its own, so the two
    writes reach the same project's roster in that order. Each pairs the roster
    version it read, so the reap's removal and the registration's row are two
    recorded transactions: the registration never meets the reap's removal as a
    write it did not make, and the dispatch provisions its member.
    """
    repository = _repository(tmp_path)
    ledger.register_member(
        PROJECT,
        "session-old",
        harness="alpha",
        root=repository,
        now="2020-01-01T00:00:00Z",
    )
    base = _git(repository, "rev-parse", "HEAD")

    reaped = routing.reap_idle_session_members(PROJECT, root=repository)

    assert reaped["reaped"] == ["session-old"]
    assert _stored_ids(repository) == []
    assert _git(repository, "rev-parse", "HEAD") == base
    assert _git(repository, "status", "--porcelain") == "?? docs/"
    assert ledger.member(PROJECT, "session-old", root=repository) is None

    stored = _register_session(repository)

    assert stored["id"] == SESSION
    assert _git(repository, "status", "--porcelain") == "?? docs/"
    assert _stored_ids(repository) == [SESSION]
