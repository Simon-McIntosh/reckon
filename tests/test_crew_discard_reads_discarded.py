"""A deliberate discard reads discarded, distinct from a pointer that vanished.

A run leaves the fleet in three shapes a live pointer cannot tell apart by
itself: a promotion that wrote its ledger row, a discard a coordinator asked
for, and a pointer reaped or removed by hand. The first two are deliberate and
leave a trail — a ledger row and a discard marker respectively — so a
reader can act on the word instead of guessing. Reading a discard as the same
``withdrawn`` a vanished pointer produces hides the coordinator's own act, and
reading it as ``promoted`` promises work that never landed.

These tests drive the producer's own call shape — ``fleet_transitions`` with no
ledger reader supplied, the run's project carried on its snapshot — against a
synthetic config home, and assert the real crew directories are untouched.
"""

from __future__ import annotations

import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon import crew, ledger
from reckon.crew import promotion, recovery


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Move pointers, run directories and the ledger into a temporary home."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _landed_repository(home: Path, tmp_path: Path) -> tuple[Path, str]:
    """A checkout whose project state resolves to the temporary home's own.

    A promoted run's ledger row names the checkout it lives in, and the row is
    where the reader takes the delivered head from. The config home's state
    directory is the store the fixture writes, so the checkout carries it as
    the symlink a mounted project carries in the real system: one row, reached
    by the discovery path and by the checkout's own path alike.
    """
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    project_state = home / "state" / "proj"
    project_state.mkdir(parents=True)
    (project_state / "index.json").write_text("{}\n", encoding="utf-8")
    (repo / "docs" / "state").symlink_to(home / "state")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "worker@example.invalid")
    _git(repo, "config", "user.name", "Worker")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-q", "-m", "chore: seed the checkout")
    return repo, _git(repo, "rev-parse", "HEAD")


def _blocked_run(home: Path, run_id: str, *, repo: Path | None = None) -> dict:
    """A stopped run whose manifest reads blocked, with its run directory made.

    The run directory is created because a real run always has one — that is
    where the discard marker lives — and because a discard must never write
    into a directory that does not already exist.
    """
    stream = home / "streams" / f"{run_id}.jsonl"
    stream.parent.mkdir(parents=True, exist_ok=True)
    stream.write_text('{"type":"turn.started"}\n')
    manifest = home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: ticker-node\nstatus: blocked\n"
        "blockers: the fence excludes the file the repair needs\n"
    )
    crew.run_dir(run_id).mkdir(parents=True, exist_ok=True)
    pointer = {
        "run_id": run_id,
        "project": "proj",
        "node": {"id": "ticker-node", "plan": "plan-a", "time_budget": "20m"},
        "phase": "blocked",
        "created_at": datetime.now(tz=UTC).isoformat(),
        "manifest_path": str(manifest),
        "log_path": str(stream),
        "process_alive": False,
    }
    if repo is not None:
        pointer["repo"] = str(repo)
        pointer["worktree"] = str(repo)
    crew._write_json(crew.pointer_path(run_id), pointer)
    return crew.read_pointer(run_id)


def _snapshot(pointer: dict) -> dict:
    return recovery._watch_snapshot(pointer, moment=time.time(), stall_seconds=3600)


def _record_ledger_row(run_id: str, repo: Path, head: str) -> None:
    """Write the per-run ledger file a promotion leaves behind.

    The row names the checkout it was promoted into, because the reader asks
    that checkout's primary branch whether the delivered head reached it: a row
    whose head no branch carries is a landing still owed, which reads delivered
    rather than promoted. This head is on the branch.
    """
    path = ledger.run_path("proj", run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"run_id": run_id, "repo": str(repo), "commits": [head]})
    )


def test_a_discarded_run_departs_as_discarded(home: Path) -> None:
    """A run removed by crew discard reads discarded and leaves its marker."""
    pointer = _blocked_run(home, "r-discarded")
    snapshot = _snapshot(pointer)
    assert snapshot["state"] == "blocked"

    result = crew.discard("r-discarded")
    assert not crew.pointer_path("r-discarded").exists()

    # The departure word is asserted before the marker, so a mutation that
    # stops the record being written fails on the reading it corrupts -- a
    # departure that reads withdrawn -- rather than on a missing file.
    folded, _ = recovery.fleet_transitions({"r-discarded": snapshot}, {})

    assert len(folded) == 1
    _emitted, previous, state, _counts = folded[0]
    assert previous == "blocked"
    assert state == "discarded"

    marker = promotion.discard_record_path("r-discarded")
    assert Path(str(result["discard_record"])) == marker
    assert marker.is_file()
    record = json.loads(marker.read_text(encoding="utf-8"))
    assert record["run_id"] == "r-discarded"
    assert record["discarded_at"]


def test_a_recorded_promotion_still_reads_promoted(home: Path, tmp_path: Path) -> None:
    """A departure the ledger records as a landed promotion reads promoted."""
    repo, head = _landed_repository(home=home, tmp_path=tmp_path)
    pointer = _blocked_run(home, "r-promoted", repo=repo)
    snapshot = _snapshot(pointer)
    _record_ledger_row("r-promoted", repo, head)
    crew.pointer_path("r-promoted").unlink()

    folded, _ = recovery.fleet_transitions({"r-promoted": snapshot}, {})

    assert folded[0][2] == "promoted"


def test_a_promotion_outweighs_a_discard_marker(home: Path, tmp_path: Path) -> None:
    """A recorded promotion reads promoted whatever else the run directory holds.

    A discard marker cannot be present on a genuinely promoted run, but a stale
    marker left by an earlier act must never outvote the ledger: the row is the
    fleet's evidence that work landed, and a directory file is not.
    """
    repo, head = _landed_repository(home=home, tmp_path=tmp_path)
    pointer = _blocked_run(home, "r-both", repo=repo)
    snapshot = _snapshot(pointer)
    marker = promotion.discard_record_path("r-both")
    marker.write_text(json.dumps({"run_id": "r-both"}), encoding="utf-8")
    _record_ledger_row("r-both", repo, head)
    crew.pointer_path("r-both").unlink()

    folded, _ = recovery.fleet_transitions({"r-both": snapshot}, {})

    assert folded[0][2] == "promoted"


def test_a_pointer_that_vanishes_with_neither_reads_withdrawn(home: Path) -> None:
    """A pointer removed with nothing recorded behind it still reads withdrawn."""
    pointer = _blocked_run(home, "r-vanished")
    snapshot = _snapshot(pointer)
    # The run directory survives, but holds no discard marker.
    assert not promotion.discard_record_path("r-vanished").exists()
    crew.pointer_path("r-vanished").unlink()

    folded, _ = recovery.fleet_transitions({"r-vanished": snapshot}, {})

    assert folded[0][2] == "withdrawn"


def test_a_discarded_run_reports_discarded_when_its_run_directory_is_gone(
    home: Path,
) -> None:
    """A discard whose run directory has gone is still recorded and reads discarded.

    The run directory is the run's own durable home; a discard that recreated it
    would bring a discarded run back into existence. So a run whose home is
    already gone records its departure in the shared spool instead, and departs
    discarded rather than the bare withdrawal a hand-removed pointer earns.
    """
    pointer = _blocked_run(home, "r-homeless")
    snapshot = _snapshot(pointer)
    crew.run_dir("r-homeless").rmdir()

    result = crew.discard("r-homeless")

    assert result["discard_record"] == str(promotion.discard_spool_path("r-homeless"))
    assert not crew.run_dir("r-homeless").exists()
    folded, _ = recovery.fleet_transitions({"r-homeless": snapshot}, {})
    assert folded[0][2] == "discarded"


def test_the_real_crew_directories_are_untouched(home: Path) -> None:
    """The fixture redirects every crew path, so no real run is written.

    A reader is only wrong when it reads outside state; a writer makes others
    wrong. Every path this module touches resolves under the temporary home, and
    the marker a discard writes is asserted to land there rather than in the
    operator's own crew directory.
    """
    assert crew.crew_home() == home / "crew"
    assert crew.runs_dir() == home / "crew" / "runs"
    pointer = _blocked_run(home, "r-isolated")
    _snapshot(pointer)

    crew.discard("r-isolated")

    assert promotion.discard_record_path("r-isolated") == (
        home / "crew" / "runs" / "r-isolated" / "discard.json"
    )
