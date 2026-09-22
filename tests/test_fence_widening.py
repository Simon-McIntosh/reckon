"""A blocked run's fence is widened through the pointer it will be judged by.

The fence is drawn once, at dispatch: ``--write-path`` is read there and no
later command touches scope, so a worker that blocks because its scope is too
narrow forces a choice between a redispatch that discards its session and its
commits, and a hand-edit of run state that promotion then reads. These cases
drive the CLI runner rather than the pointer helper, because what is under test
is the command surface a coordinator reaches for, and the field it writes is the
one the promotion validator reads.

Running the command is also held to the repository rule that a write-shaped test
accounts for the state it did not isolate. A case points RECKON_HOME at a
temporary home so its own pointer write stays inside the case, and the crew home
the process resolved *before* that substitution is watched byte for byte: if
home resolution ever stopped honouring the substitution, this fixture's pointer
would land in that other directory, and the witness is what names the escape
rather than leaving it to be discovered later. The production crew home is not
the directory under watch and cannot be, and the last case makes that assertion
fire on a substitute root so the witness is shown to speak.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon.cli import main as cli_main
from reckon.crew.runs import _write_json, crew_home, pointer_path

BLOCKED_RUN_ID = "r-20260921T000000000000-widen-blocked"
WORKING_RUN_ID = "r-20260921T000000000000-widen-working"
DECLARED = "reckon/crew/runs.py"
GRANTED = "reckon/crew/node.py"
SESSION_ID = "s19-codex-20260916"
COMMITS = ["1a2b3c4d5e6f", "0f1e2d3c4b5a"]


def _tree_bytes(root: Path) -> dict[str, bytes] | None:
    """Every entry under ``root`` by relative path, with the file's own bytes.

    Directories carry a trailing slash so an empty directory appearing inside
    the tree is a difference rather than an invisible one. ``None`` stands for
    a root that does not exist, which is a state the comparison may see change.
    """
    if not root.exists():
        return None
    entries: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            entries[relative + "/"] = b""
        elif path.is_file():
            entries[relative] = path.read_bytes()
    return entries


def _difference(before: dict[str, bytes] | None, after: dict[str, bytes] | None) -> str:
    """Name the paths that appeared, vanished or changed between two trees."""
    if before is None or after is None:
        return (
            f"the root existed={before is not None} before, {after is not None} after"
        )
    lines = []
    for relative in sorted(set(before) | set(after)):
        was, now = before.get(relative), after.get(relative)
        if was == now:
            continue
        if was is None:
            lines.append(f"  created: {relative}")
        elif now is None:
            lines.append(f"  removed: {relative}")
        else:
            lines.append(f"  changed: {relative} ({len(was)} -> {len(now)} bytes)")
    return "\n".join(lines)


class CrewHomeWatch:
    """A byte-for-byte witness of one crew home across a case's run."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.before = _tree_bytes(root)

    def assert_untouched(self) -> None:
        """Fail naming every path that moved under the watched root."""
        after = _tree_bytes(self.root)
        assert after == self.before, (
            f"the crew home {self.root} was written to by a case that did not "
            f"substitute it:\n{_difference(self.before, after)}"
        )


@pytest.fixture()
def crew_home_watch(isolated_reckon_home: Path) -> CrewHomeWatch:
    """Watch the crew home this process resolved before a case substituted one.

    The suite's own fixture is requested by name so its temporary home is
    already in place: the root under watch is where a case's pointer write
    lands if home resolution stops honouring the substitution the case makes,
    which is the escape the assertion exists to catch.
    """
    root = crew_home()
    assert not root.is_relative_to(Path.home()), (
        f"the witness is watching the production directory {root}: the suite's "
        "temporary home is not in force, so a pass would be measuring the fleet"
    )
    return CrewHomeWatch(root)


def _blocked_pointer(root: Path, run_id: str, *, phase: str) -> dict:
    """Write one canonical live pointer and return its value as recorded."""
    pointer = {
        "run_id": run_id,
        "project": "sample",
        "repo": str(root / "repo"),
        "worktree": str(root / "repo"),
        "base_sha": "0" * 40,
        "launch": "in-harness",
        "role": "implement",
        "phase": phase,
        "detail": "write scope too narrow to land the change",
        "session_id": SESSION_ID,
        "commits": list(COMMITS),
        "node": {
            "id": "widen",
            "plan": "fixture",
            "section": "s10",
            "write_paths": [DECLARED],
        },
        "manifest_path": str(root / "manifest.md"),
    }
    _write_json(pointer_path(run_id), pointer)
    return pointer


@pytest.fixture()
def blocked_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crew_home_watch: CrewHomeWatch
):
    """A blocked run whose fence declares one path, in an isolated crew home."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    (tmp_path / "repo").mkdir()
    return _blocked_pointer(tmp_path, BLOCKED_RUN_ID, phase="blocked")


@pytest.fixture()
def working_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crew_home_watch: CrewHomeWatch
):
    """A run still working, whose fence must not move under it."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    (tmp_path / "repo").mkdir()
    return _blocked_pointer(tmp_path, WORKING_RUN_ID, phase="working")


def _widen(run_id: str, *paths: str):
    arguments = ["crew", "widen", "--run", run_id]
    for path in paths:
        arguments += ["--write-path", path]
    return CliRunner().invoke(cli_main, arguments)


def _read(run_id: str) -> dict:
    return json.loads(pointer_path(run_id).read_text())


def test_widening_a_blocked_run_adds_the_named_path(
    blocked_run: dict, crew_home_watch: CrewHomeWatch
) -> None:
    result = _widen(BLOCKED_RUN_ID, GRANTED)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["added"] == [GRANTED]
    assert _read(BLOCKED_RUN_ID)["node"]["write_paths"] == [DECLARED, GRANTED]
    crew_home_watch.assert_untouched()


def test_the_widening_carries_the_session_and_recorded_commits_through(
    blocked_run: dict, crew_home_watch: CrewHomeWatch
) -> None:
    """Every field but the write scope is the value it was before the widening.

    The point of widening rather than redispatching is that the run keeps its
    session and the work it had already committed, so the case asserts the
    carriage directly rather than only that the new path arrived: a widening
    that added the path while rewriting the session would satisfy the first
    case and defeat the reason the command exists.
    """
    before = json.loads(pointer_path(BLOCKED_RUN_ID).read_text())

    result = _widen(BLOCKED_RUN_ID, GRANTED)

    assert result.exit_code == 0, result.output
    after = json.loads(pointer_path(BLOCKED_RUN_ID).read_text())
    assert after["session_id"] == before["session_id"] == SESSION_ID
    assert after["commits"] == before["commits"] == COMMITS
    assert {key: value for key, value in after.items() if key != "node"} == {
        key: value for key, value in before.items() if key != "node"
    }
    assert {
        key: value for key, value in after["node"].items() if key != "write_paths"
    } == {key: value for key, value in before["node"].items() if key != "write_paths"}
    crew_home_watch.assert_untouched()


def test_a_path_already_declared_is_not_declared_twice(
    blocked_run: dict, crew_home_watch: CrewHomeWatch
) -> None:
    result = _widen(BLOCKED_RUN_ID, DECLARED)

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["added"] == []
    assert _read(BLOCKED_RUN_ID)["node"]["write_paths"] == [DECLARED]
    crew_home_watch.assert_untouched()


def test_widening_a_working_run_is_refused_and_writes_nothing(
    working_run: dict, crew_home_watch: CrewHomeWatch
) -> None:
    """A working run's fence is the boundary it is already writing against.

    The refusal is asserted on both halves: a non-zero exit and a pointer whose
    bytes are what they were, because a command that refuses in name while
    still rewriting the file would leave the boundary moved and the operator
    believing it had not.
    """
    before = _tree_bytes(pointer_path(WORKING_RUN_ID).parent)

    result = _widen(WORKING_RUN_ID, GRANTED)

    assert result.exit_code != 0, result.output
    assert GRANTED not in result.output
    assert _read(WORKING_RUN_ID)["node"]["write_paths"] == [DECLARED]
    assert _tree_bytes(pointer_path(WORKING_RUN_ID).parent) == before
    crew_home_watch.assert_untouched()


def test_a_run_that_leaves_blocked_before_the_pointer_write_is_refused(
    blocked_run: dict, crew_home_watch: CrewHomeWatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The phase is re-read on the record the pointer mutation itself read.

    The command reads the pointer once to decide, then the pointer mutation
    reads it again under the per-run lock. A run that leaves the blocked phase
    in that window has started writing against its boundary, so a widening that
    trusts the first read moves that boundary under a live process -- the exact
    case the guard exists to refuse. The flip is injected between the two reads
    by making the pointer read answer `blocked` once and `working` afterwards,
    which is the shape of the race, and the case fails unless the under-lock
    read is re-checked rather than assumed.
    """
    from reckon.crew import runs as runs_module

    read_through = runs_module.read_pointer
    reads = {"count": 0}

    def leaving_blocked(run_id: str) -> dict:
        pointer = read_through(run_id)
        reads["count"] += 1
        if reads["count"] == 1:
            return pointer
        return {**pointer, "phase": "working"}

    monkeypatch.setattr(runs_module, "read_pointer", leaving_blocked)
    monkeypatch.setattr("reckon.crew.read_pointer", leaving_blocked)

    before = _tree_bytes(pointer_path(BLOCKED_RUN_ID).parent)

    result = _widen(BLOCKED_RUN_ID, GRANTED)

    assert result.exit_code != 0, result.output
    on_disk = _read(BLOCKED_RUN_ID)
    assert on_disk["phase"] == "blocked"
    assert on_disk["node"]["write_paths"] == [DECLARED]
    assert _tree_bytes(pointer_path(BLOCKED_RUN_ID).parent) == before
    crew_home_watch.assert_untouched()


def test_an_unknown_run_is_refused_rather_than_widened(
    blocked_run: dict, crew_home_watch: CrewHomeWatch
) -> None:
    result = _widen("r-20260921T000000000000-absent", GRANTED)

    assert result.exit_code != 0
    assert result.output.strip()
    crew_home_watch.assert_untouched()


def test_the_isolation_assertion_fires_on_a_substituted_root(tmp_path: Path) -> None:
    """The witness is shown to fire, on a root the production home never sees.

    A guard that never fires is indistinguishable from no guard, so the failure
    the witness exists to catch is made to happen here and the assertion is
    shown to name it. The write goes into a substitute root, and the production
    crew home is never a party to this case at all.
    """
    root = tmp_path / "substituted-crew-home"
    (root / "live").mkdir(parents=True)
    watch = CrewHomeWatch(root)

    watch.assert_untouched()  # nothing written yet: the witness stays quiet

    _write_json(
        root / "live" / f"{BLOCKED_RUN_ID}.json",
        {"run_id": BLOCKED_RUN_ID, "node": {"id": "widen"}},
    )

    with pytest.raises(AssertionError, match=f"live/{BLOCKED_RUN_ID}.json"):
        watch.assert_untouched()
