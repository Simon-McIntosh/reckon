"""A review finding folded into a run lets that run's fence widen.

A run that ended its turn normally folds its phase and its manifest to
``complete``, and ``crew widen`` refuses a complete run on the reasoning that a
finished run has no scope decision outstanding. A review that read the run can
leave exactly such a decision: a dimension scored below its declared floor,
disposed as folded back into the run's own node. That finding is the run's to
repair, and the repair resume is the only moment the boundary can be moved for
it -- but the grant has to reach the run before the resume, because after it the
run reads ``working`` and a widening refuses a live worker. This module covers
that door: the fold over a stopped worker admits the widening and the grant names
the disposition, whatever phase or manifest status the run ended in. The
disposition is the grant's own report, not its permission: a dimension exempted
instead of folded, and a fold naming some other node, widen the fence without
naming a disposition for this run, because neither leaves this run a scope
decision. A fold over a worker that is still running is refused.

The world a case reads from is synthesised inside a temporary config home: a
live pointer whose manifest reports ``complete``, the run's own review record
carrying the disposition, and the review store the record lives in. The write
under test lands in the pointer, so the assertions are made on the pointer
bytes on disk rather than on the reply alone, and the crew home this process
resolved before the case substituted one is watched byte for byte -- the
production store cannot serve as that witness, because a live fleet rewrites it
continuously and a difference there would say the machine is busy rather than
that a case escaped.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from reckon.cli import main as cli_main
from reckon.crew import review as review_module
from reckon.crew import review_dispositions as dispositions_module
from reckon.crew.runs import (
    _process_start_time,
    _write_json,
    crew_home,
    pointer_path,
    record_process_alive,
)

PROJECT = "folded-fixture"
SESSION_ID = "s22-cli-20261019"
NODE_ID = "a-tier-none-run-owes-no-review"
FOLDED_RUN_ID = "r-20261019T201233968744-a-tier-none-run-owes-no-review"
EXEMPTED_RUN_ID = "r-20261019T201233968745-a-tier-none-run-owes-no-review"
ALIVE_RUN_ID = "r-20261019T201233968746-a-tier-none-run-owes-no-review"
ELSEWHERE_RUN_ID = "r-20261019T201233968747-a-tier-none-run-owes-no-review"
OTHER_NODE_ID = "some-other-run-node"
DECLARED = "reckon/crew/obligations.py"
MISSING = "reckon/crew/recovery_classification.py"
DIMENSION = "reuse"
COMMITS = ["4f2a0b1c9d8e7f60"]

COMPLETE_MANIFEST = f"""orientation_worktree: {{worktree}}
orientation_base_sha: {"0" * 40}
orientation_write_paths: ["{DECLARED}"]
node: {NODE_ID}
status: complete
commits: {COMMITS[0]}
changed_paths: ["{DECLARED}"]
tests: ran the focused gate: 12 passed, 0 failed
test_logs: ["/tmp/gate.log"]
evidence_inputs: none
follow_ons: none
"""


def _tree_bytes(root: Path) -> dict[str, bytes] | None:
    """Every entry under ``root`` by relative path, with the file's own bytes.

    Directories carry a trailing slash so an empty directory appearing inside
    the tree is a difference rather than an invisible one. ``None`` stands for a
    root that does not exist, which is a state the comparison may see change.
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


def _case_home(tmp_path: Path) -> Path:
    home = tmp_path / "config"
    home.mkdir()
    return home


@pytest.fixture()
def crew_home_watch(isolated_reckon_home: Path) -> CrewHomeWatch:
    """Watch the crew home this process resolved before a case substituted one."""
    root = crew_home()
    assert not root.is_relative_to(Path.home()), (
        f"the witness is watching the production directory {root}: the suite's "
        "temporary home is not in force, so a pass would be measuring the fleet"
    )
    return CrewHomeWatch(root)


def _exited_pid() -> int:
    """The pid of a process that has run to completion and been collected."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def _store_review(run_id: str, disposition: dict[str, Any]) -> Path:
    """Write the run's own review record carrying one dimension disposition.

    The record lands where the disposition writers rewrite it -- the project's
    store keyed by the reviewed run -- so the read under test resolves the file
    the production writer would have answered from. The disposition entry itself
    is built through the production builder, so the stored shape is the closed
    shape the readers honour rather than a hand-rolled approximation of it.
    """
    record = {
        "project": PROJECT,
        "reviewed_run_id": run_id,
        "review_run_id": f"{run_id}-re-review",
        "reviewed_base_sha": "0" * 40,
        "reviewed_head_sha": "a" * 40,
        "reviewed_commit": "a" * 40,
        "timestamp": "2026-10-19T21:11:00+00:00",
        "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 18),
        dispositions_module.DIMENSION_DISPOSITIONS_KEY: {DIMENSION: disposition},
    }
    path = review_module.review_path(PROJECT, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def _fold_entry(kind: str, *, node: str | None = None, reason: str | None = None):
    """The stored entry the production builder produces."""
    return dispositions_module._build_disposition_entry(kind, node, reason)


def _run_pointer(
    root: Path,
    run_id: str,
    *,
    disposition: dict[str, Any],
    pid: int | None = None,
) -> dict:
    """Write one live pointer, its completed delivery and the run's review."""
    worktree = root / "repo"
    manifest_path = root / f"{run_id}.md"
    manifest_path.write_text(
        COMPLETE_MANIFEST.format(worktree=worktree), encoding="utf-8"
    )
    pointer: dict[str, Any] = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(worktree),
        "worktree": str(worktree),
        "base_sha": "0" * 40,
        "launch": "cli",
        "role": "implement",
        "phase": "complete",
        "detail": "session ended its turn after delivering the work",
        "session_id": SESSION_ID,
        "commits": list(COMMITS),
        "node": {
            "id": NODE_ID,
            "plan": "fixture",
            "section": "s12",
            "write_paths": [DECLARED],
        },
        "manifest_path": str(manifest_path),
        "manifest_baseline_mtime_ns": 0,
    }
    if pid is not None:
        pointer["pid"] = pid
        start = _process_start_time(pid)
        if start is not None:
            pointer["pid_start_time"] = start
    _write_json(pointer_path(run_id), pointer)
    _store_review(run_id, disposition)
    return pointer


@pytest.fixture()
def folded_finding_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crew_home_watch: CrewHomeWatch
):
    """The case the door exists for: complete, stopped, folded into its own node."""
    monkeypatch.setenv("RECKON_HOME", str(_case_home(tmp_path)))
    (tmp_path / "repo").mkdir()
    return _run_pointer(
        tmp_path,
        FOLDED_RUN_ID,
        disposition=_fold_entry("folded", node=NODE_ID),
        pid=_exited_pid(),
    )


@pytest.fixture()
def exempted_finding_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crew_home_watch: CrewHomeWatch
):
    """The sibling: the same finding, exempted rather than sent back."""
    monkeypatch.setenv("RECKON_HOME", str(_case_home(tmp_path)))
    (tmp_path / "repo").mkdir()
    return _run_pointer(
        tmp_path,
        EXEMPTED_RUN_ID,
        disposition=_fold_entry(
            "exempted", reason="the finding is answered by the merged head"
        ),
        pid=_exited_pid(),
    )


@pytest.fixture()
def folded_finding_over_a_live_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crew_home_watch: CrewHomeWatch
):
    """A fold into the run's node over a worker that has not stopped."""
    monkeypatch.setenv("RECKON_HOME", str(_case_home(tmp_path)))
    (tmp_path / "repo").mkdir()
    return _run_pointer(
        tmp_path,
        ALIVE_RUN_ID,
        disposition=_fold_entry("folded", node=NODE_ID),
        pid=os.getpid(),
    )


@pytest.fixture()
def folded_finding_naming_another_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crew_home_watch: CrewHomeWatch
):
    """A fold of the same kind and dimension, but naming some other node.

    The finding was sent back, though not to this run: another node carries it,
    so this run has no scope decision outstanding and its boundary must not
    move. This is the case the node-name half of the read exists for.
    """
    monkeypatch.setenv("RECKON_HOME", str(_case_home(tmp_path)))
    (tmp_path / "repo").mkdir()
    return _run_pointer(
        tmp_path,
        ELSEWHERE_RUN_ID,
        disposition=_fold_entry("folded", node=OTHER_NODE_ID),
        pid=_exited_pid(),
    )


def _widen(run_id: str, *paths: str):
    arguments = ["crew", "widen", "--run", run_id]
    for path in paths:
        arguments += ["--write-path", path]
    return CliRunner().invoke(cli_main, arguments)


def _read_pointer(run_id: str) -> dict:
    return json.loads(pointer_path(run_id).read_text())


def test_a_folded_finding_widens_a_complete_run(
    folded_finding_run: dict, crew_home_watch: CrewHomeWatch
) -> None:
    """The falsifier for the refusal: this run is wideniable, and the grant names it."""
    before = _read_pointer(FOLDED_RUN_ID)

    result = _widen(FOLDED_RUN_ID, MISSING)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["added"] == [MISSING]
    assert payload["manifest_status"] == "complete"
    assert payload["folded_disposition"] == {
        "dimension": DIMENSION,
        "node": NODE_ID,
        "kind": "folded",
    }
    after = _read_pointer(FOLDED_RUN_ID)
    assert after["node"]["write_paths"] == [DECLARED, MISSING]
    assert before["node"]["write_paths"] == [DECLARED]
    crew_home_watch.assert_untouched()


def test_the_folded_grant_carries_every_other_recorded_field_unchanged(
    folded_finding_run: dict, crew_home_watch: CrewHomeWatch
) -> None:
    """The resume the grant exists for keeps its session and its recorded work."""
    before = _read_pointer(FOLDED_RUN_ID)

    result = _widen(FOLDED_RUN_ID, MISSING)

    assert result.exit_code == 0, result.output
    after = _read_pointer(FOLDED_RUN_ID)
    assert after["session_id"] == before["session_id"] == SESSION_ID
    assert {key: value for key, value in after.items() if key != "node"} == {
        key: value for key, value in before.items() if key != "node"
    }
    crew_home_watch.assert_untouched()


def test_an_exempted_finding_does_not_name_a_disposition(
    exempted_finding_run: dict, crew_home_watch: CrewHomeWatch
) -> None:
    """Only a fold sends the finding back, so only a fold is reported as one.

    A stopped worker's fence moves whatever the run's manifest says, so the
    exempted run widens like its folded sibling. What separates them is the
    disposition the grant names: exempting the finding answers it here, so
    nothing was sent back to this node and the grant reports no fold for it.
    A reader that took the widening itself as the fold would resume this run
    for work it does not owe.
    """
    before = _read_pointer(EXEMPTED_RUN_ID)

    result = _widen(EXEMPTED_RUN_ID, MISSING)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["added"] == [MISSING]
    assert payload["folded_disposition"] is None
    after = _read_pointer(EXEMPTED_RUN_ID)
    assert after["node"]["write_paths"] == [DECLARED, MISSING]
    assert {key: value for key, value in after.items() if key != "node"} == {
        key: value for key, value in before.items() if key != "node"
    }
    crew_home_watch.assert_untouched()


def test_a_fold_naming_another_node_names_no_disposition_for_this_run(
    folded_finding_naming_another_node: dict, crew_home_watch: CrewHomeWatch
) -> None:
    """A fold is this run's only when it names this run's own node.

    The disposition is the same kind and answers the same dimension, so a read
    that stopped at the kind would report it as this run's; the finding was
    folded into a different node, which carries it. The widening still moves --
    the worker is stopped, which is all the fence asks -- but the grant names no
    disposition, because this run has no scope decision outstanding and a resume
    driven by one would repair work that is another node's.
    """
    before = _read_pointer(ELSEWHERE_RUN_ID)

    result = _widen(ELSEWHERE_RUN_ID, MISSING)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["added"] == [MISSING]
    assert payload["folded_disposition"] is None
    after = _read_pointer(ELSEWHERE_RUN_ID)
    assert after["node"]["write_paths"] == [DECLARED, MISSING]
    assert {key: value for key, value in after.items() if key != "node"} == {
        key: value for key, value in before.items() if key != "node"
    }
    crew_home_watch.assert_untouched()


def test_a_folded_run_whose_worker_still_runs_is_refused(
    folded_finding_over_a_live_worker: dict, crew_home_watch: CrewHomeWatch
) -> None:
    """The fold answers the completion, not the liveness: the process decides."""
    alive = record_process_alive(_read_pointer(ALIVE_RUN_ID))
    assert alive is True, (
        f"the fixture's worker for {ALIVE_RUN_ID} reads alive={alive}, not True: "
        "the case would otherwise be measuring a different state"
    )
    before = _tree_bytes(pointer_path(ALIVE_RUN_ID).parent)

    result = _widen(ALIVE_RUN_ID, MISSING)

    assert result.exit_code != 0, result.output
    assert "still running" in result.output
    assert MISSING not in result.output
    assert _read_pointer(ALIVE_RUN_ID)["node"]["write_paths"] == [DECLARED]
    assert _tree_bytes(pointer_path(ALIVE_RUN_ID).parent) == before
    crew_home_watch.assert_untouched()


def test_the_witness_fires_on_a_substituted_root(tmp_path: Path) -> None:
    """The witness is shown to fire, on a root the production home never sees."""
    root = tmp_path / "substituted-crew-home"
    (root / "live").mkdir(parents=True)
    watch = CrewHomeWatch(root)

    watch.assert_untouched()  # nothing written yet: the witness stays quiet

    _write_json(
        root / "live" / f"{FOLDED_RUN_ID}.json",
        {"run_id": FOLDED_RUN_ID, "node": {"id": NODE_ID}},
    )

    with pytest.raises(AssertionError, match=f"live/{FOLDED_RUN_ID}.json"):
        watch.assert_untouched()
