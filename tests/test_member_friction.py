"""A named member resolves from the registered checkout, and unnamed runs
never share a session identity.

Two frictions measured on the dispatch surface. A dispatch that names a roster
member and a lane resolves the project's registered repository when --repo is
omitted, exactly as a dispatch that names no member does, and refuses with a
message naming --repo only when the project has no registered checkout at all.
A dispatch that names no member carries its own per-run identity, so a live
review worker from an earlier run never holds a later dispatch in flight.

The isolation guard watches a temporary crew home that stands in for the
machine's real one. It asserts only that this fixture's own ids never reach
that home: a peer session dispatching concurrently leaves entries, but none of
them names anything of ours, so a busy host cannot make the guard fire.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from reckon import crew, ledger
from reckon.crew import recovery
from reckon.crew.node import member_in_flight_verdict

CONFIG = {
    "default_backend": "worker",
    "local_backend": "worker",
    "backends": {
        "worker": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "25m",
        }
    },
    "roles": {"implement": {}, "review": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}

#: The identities this fixture owns. A dispatcher that escaped its isolated
#: home would leave one of these in the crew home it must not write.
FIXTURE_PROJECT = "sample"
FIXTURE_MEMBER = "fixture-member"
FIXTURE_RUN_IDS = ("r-first", "r-second")
_FIXTURE_IDS = (FIXTURE_PROJECT, FIXTURE_MEMBER, *FIXTURE_RUN_IDS)


def _fixture_writes_under(store: Path) -> list[str]:
    """Every entry under a crew home that names one of this fixture's ids.

    A dispatcher that wrote outside its isolated home would leave a live
    pointer or a roster row naming the fixture here, so this is a write-side
    check rather than a read of code that never ran. A peer session's entries
    name nothing of ours, so the whole store may move around them without this
    firing.
    """
    found: list[str] = []
    live = store / "crew" / "live"
    if live.is_dir():
        found.extend(
            str(entry)
            for entry in sorted(live.iterdir())
            if any(identity in entry.name for identity in _FIXTURE_IDS)
        )
    for roster in sorted((store / "state").glob("*/crew.json")):
        text = roster.read_text(encoding="utf-8", errors="replace")
        if any(identity in text for identity in _FIXTURE_IDS):
            found.append(str(roster))
    return found


def _assert_isolation(store: Path) -> None:
    """Refuse when this fixture's live pointers or roster rows reach ``store``.

    Keeps the guard's purpose — the test must not write into the crew home —
    while asserting only the entries this test could have caused.
    """
    leaked = _fixture_writes_under(store)
    assert leaked == [], (
        "the isolation guard found this fixture's own entries in the crew home "
        f"it must never write: {leaked}"
    )


@pytest.fixture()
def real_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary crew home standing in for the machine's real one.

    The isolation guard inspects this directory rather than the operator's real
    crew home, and it is installed as RECKON_HOME, so the suite never reads or
    writes the real home and a planted leak is removed with the temporary tree.
    """
    store = tmp_path / "real-crew-home"
    (store / "crew" / "live").mkdir(parents=True)
    (store / "state").mkdir()
    monkeypatch.setenv("RECKON_HOME", str(store))
    return store


@pytest.fixture()
def peer_dispatch() -> Iterator[list[Path]]:
    """Stand in for a concurrent peer session dispatching during the suite.

    A test appends the paths its peer leaves behind; the teardown removes them.
    ``isolated_project`` requests it, so this teardown runs after the isolation
    guard has inspected the store: the peer's entry is present while the guard
    looks and gone once the test ends, exactly as a peer's own run appears and
    disappears during a suite.
    """
    planted: list[Path] = []
    yield planted
    for path in planted:
        path.unlink(missing_ok=True)


@pytest.fixture()
def isolated_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    real_store: Path,
    peer_dispatch: list[Path],
) -> Iterator[tuple[Path, Path]]:
    _ = peer_dispatch  # requested for ordering, not for its value
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repo = tmp_path_repo = tmp_path / "repo"
    (repo / "docs" / "plans").mkdir(parents=True)
    (repo / "docs" / "plans" / "fixture.html").write_text(
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">'
        '<h2 id="reviews">Independent review dispatch</h2>',
        encoding="utf-8",
    )
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "docs/plans/fixture.html"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    (config_home / "mounts.json").write_text(
        json.dumps({"sample": str(repo / "docs")}), encoding="utf-8"
    )
    ledger.register_member("sample", "fixture-member", harness="worker", root=repo)

    yield config_home, tmp_path_repo

    _assert_isolation(real_store)


def test_isolation_guard_refuses_a_fixture_named_write(real_store: Path) -> None:
    """A pointer naming this fixture in the crew home is the leak the guard
    exists to catch, so the guard must refuse it."""
    leaked = real_store / "crew" / "live" / f"{FIXTURE_RUN_IDS[0]}.json"
    leaked.write_text("{}", encoding="utf-8")
    try:
        with pytest.raises(AssertionError):
            _assert_isolation(real_store)
    finally:
        leaked.unlink()


def test_isolation_guard_tolerates_a_peer_dispatch(
    isolated_project: tuple[Path, Path],
    real_store: Path,
    peer_dispatch: list[Path],
) -> None:
    """A peer's live pointer must not be mistaken for this fixture's leak.

    The peer's entry is planted after the isolated home is set up and stays
    until the guard has looked, which is exactly the window a concurrent
    dispatch on a shared host occupies.
    """
    peer = real_store / "crew" / "live" / "r-20260901T000000000000-peer-node.json"
    peer.write_text('{"run_id": "r-20260901T000000000000-peer-node"}', encoding="utf-8")
    peer_dispatch.append(peer)

    assert _fixture_writes_under(real_store) == []
    _assert_isolation(real_store)


def _member_node() -> crew.TaskNode:
    return crew.TaskNode(
        id="member-resolution",
        goal="resolve a named member from the project's registered checkout",
        plan="fixture",
        section="reviews",
        role="implement",
        spec_level="exact",
        done_when=(
            "pytest tests/test_member_friction.py reports the registered-checkout "
            "case passing"
        ),
        write_paths=["records/member-resolution.json"],
        time_budget="20m",
    )


def test_member_dispatch_from_the_registered_checkout_resolves_without_repo(
    isolated_project: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lane-naming dispatch inside the checkout resolves the mount itself.

    This is the validating path the CLI dry run takes: it hands the raw
    ``--repo`` through, so an omitted flag arrives as ``None`` while the
    launching path would have resolved the project's registered mount.
    """
    _config_home, repo = isolated_project
    monkeypatch.chdir(repo)

    resolution = crew.plan_dispatch(
        node=_member_node(),
        config=CONFIG,
        project="sample",
        repo=None,
        local=True,
        member="fixture-member",
    )

    assert resolution.validation.ok, resolution.validation.findings
    assert resolution.authority is not None
    assert resolution.authority["write"]["repository"] == str(repo.resolve())
    assert resolution.backend == "worker"


def test_member_dispatch_without_a_registered_checkout_names_repo(
    isolated_project: tuple[Path, Path],
) -> None:
    """No registered checkout is the one case that still refuses, naming the
    flag that supplies the missing repository."""
    _config_home, _repo = isolated_project

    with pytest.raises(crew.CrewError) as refusal:
        crew.plan_dispatch(
            node=_member_node(),
            config=CONFIG,
            project="unmounted",
            repo=None,
            local=True,
            member="fixture-member",
        )
    assert "--repo" in str(refusal.value)


def _scoring_pointer(config_home: Path, repo: Path, run_id: str) -> dict:
    manifest = config_home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {run_id}\nstatus: complete\ncommits: {run_id}\n",
        encoding="utf-8",
    )
    record = {
        "run_id": run_id,
        "project": "sample",
        "repo": str(repo),
        "node": {"id": run_id, "plan": "fixture", "section": "reviews"},
        "backend": "worker",
        "launch": "cli",
        "argv": ["codex"],
        "phase": "complete",
        "process_alive": False,
        "session": "session-orchestrating",
        "manifest_path": str(manifest),
    }
    crew._write_json(crew.pointer_path(run_id), record)
    return record


def test_unnamed_dispatch_is_admitted_while_a_review_worker_is_alive(
    isolated_project: tuple[Path, Path],
) -> None:
    """The review worker from the first run is still alive when the second
    dispatch runs, yet the second is admitted: an unnamed dispatch carries its
    own identity rather than the dispatching session's shared one."""
    config_home, repo = isolated_project
    first_source = _scoring_pointer(config_home, repo, "r-first")
    second_source = _scoring_pointer(config_home, repo, "r-second")
    launched: list[dict] = []

    def launcher(*args, **kwargs):
        launched.append(kwargs)
        return os.getpid()

    first = recovery.dispatch_review_for_run(
        first_source, config=CONFIG, launcher=launcher
    )
    assert first["dispatched"] is True, first.get("reason")
    first_pointer = crew.read_pointer(first["review_run_id"])
    assert first_pointer["pid"] == os.getpid()
    assert member_in_flight_verdict(first_pointer).blocks is True
    assert first_pointer["member"] == f"disposable-{first['review_run_id']}"

    second = recovery.dispatch_review_for_run(
        second_source, config=CONFIG, launcher=launcher
    )
    assert second["dispatched"] is True, second.get("reason")

    second_pointer = crew.read_pointer(second["review_run_id"])
    assert second_pointer["member"] == f"disposable-{second['review_run_id']}"
    assert second_pointer["member"] != first_pointer["member"]
    assert len(launched) == 2
