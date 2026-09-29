"""A ledger write leaves each per-run file identical to its aggregate row.

A completed run can be recorded twice — once in the aggregate run list and once
in its own file under ``runs/`` — and :func:`ledger.load` refuses the whole
project when the two serialisations disagree. A write that edited an aggregate
row and left the file holding the previous revision therefore handed its own
caller a project its reader could not read: the writer produced state the
reader refuses. Measured on the shadow-dispatch fixtures, where each edits a run
record through ``ledger.write`` and every subsequent read died inside
``ledger.load`` with the differs-between refusal.

The writer is the defect, so the writer is what this file pins: a write keeps
every per-run file equal to its aggregate row, and a disagreement introduced by
editing a file directly, outside the writer is still refused. Every fixture
here is built in a temporary repository, and the first case closes by asserting
the write added nothing to that repository beyond the files the ledger already
held.

The last case pins the writer against itself: two writers prepared from one
revision, interleaved by a seam, and the reader must still find a readable
project holding the row that won.
"""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from reckon import ledger

PROJECT = "proj"
RUN_ID = "r-20260929T000000000000-node-a"

# One writer's half of the interleaved-write race, run as its own process.
# Processes rather than threads because the writers are separate commands and
# the exclusion being measured is the one that holds between them (an advisory
# process lock); a thread would share it invisibly and report a safety the
# deployed shape does not have.
_RACE_CHILD = '''\
"""One writer's half of the write race, in its own process."""

from __future__ import annotations

import fcntl
import json
import sys
import time
from pathlib import Path

from reckon import ledger

PROJECT = "proj"
WAIT_SECONDS = 60.0


def holding_the_write_lock(project: str) -> bool:
    """Whether this process already holds the project's ledger write lock.

    The pause below only means anything to a writer the other one can overtake.
    A revision that serialises the whole write holds this lock from before its
    per-run write until after its envelope write, so a commit during the pause
    is impossible by construction -- the other writer is blocked on this very
    lock -- and waiting for one would wait forever. The lock is asked for rather
    than inferred from the writer's shape, and a revision carrying no such lock
    answers no, so the pause stands and the overtaking writer is waited for.
    """
    lock_path = getattr(ledger, "ledger_lock_path", None)
    if lock_path is None:
        return False
    handle = lock_path(project).open("a+b")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return False


def main() -> int:
    role, root, scratch = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
    data, version = ledger.load(PROJECT, root)
    data["runs"][0]["outcome"] = f"completed by the {role}"
    if role == "late":
        real = ledger._keep_run_files_identical

        def paused(project, rows, root_arg):
            result = real(project, rows, root_arg)
            (scratch / "late-paused").write_text("1")
            if not holding_the_write_lock(project):
                # Held here in the middle of this writer's own pause, the other
                # writer's commit is the only thing that can resume it. A
                # deadline that silently expired instead would let this writer
                # commit first, collapsing the schedule into the ordinary
                # serialised order and reporting a pass this schedule never
                # produced.
                deadline = time.monotonic() + WAIT_SECONDS
                while not (scratch / "winner.json").exists():
                    if time.monotonic() >= deadline:
                        raise SystemExit(
                            "the other writer did not commit while this one paused"
                        )
                    time.sleep(0.02)
            return result

        ledger._keep_run_files_identical = paused
    else:
        # Wait for the other writer to reach its pause before writing, and fail
        # loudly if it never does: a writer that instead proceeds alone lands
        # first, collapsing the schedule into the serialised order this case
        # exists to exclude, and the case then reports a pass the schedule never
        # produced -- how this test first reported one.
        deadline = time.monotonic() + WAIT_SECONDS
        while not (scratch / "late-paused").exists():
            if time.monotonic() >= deadline:
                raise SystemExit("the other writer never reached its pause")
            time.sleep(0.02)
    outcome = {"result": "ok", "text": "completed by the " + role}
    try:
        ledger.write(PROJECT, data, version, root)
    except ledger.LedgerError as exc:
        outcome = {"result": "conflict", "text": str(exc)}
    (scratch / (role + ".json")).write_text(json.dumps(outcome))
    print(json.dumps(outcome))
    return 0


sys.exit(main())
'''


@pytest.fixture()
def repo(tmp_path):
    """A throwaway checkout carrying this project's state directory."""
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "docs" / "state" / PROJECT / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n"
    )
    return root


def _seed(repo: Path) -> dict:
    """Record one run in the aggregate and in its own file, as a promotion does.

    The aggregate row is written through the writer, then the per-run file is
    placed directly so the fixture starts in the dual-placement state the
    refusal is about — both copies present and already in agreement.
    """
    row = {
        "run_id": RUN_ID,
        "gate": "passed",
        "outcome": "completed with a recorded budget",
        "time_budget": "12m",
    }
    ledger.write(
        PROJECT,
        {"members": [], "runs": [dict(row)], "holds": []},
        0,
        repo,
    )
    target = ledger.run_path(PROJECT, RUN_ID, repo)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(ledger.serialize_run(row), encoding="utf-8")
    return row


def _file_text(repo: Path) -> str:
    return ledger.run_path(PROJECT, RUN_ID, repo).read_text(encoding="utf-8")


def _tree(repo: Path) -> set[str]:
    """Every file under the temporary checkout, named relative to its root.

    A snapshot of this before and after a write is what proves the write
    reached only the state files it owns: a sibling temporary left behind, or
    a file created anywhere else in the checkout, changes this set and fails
    the comparison.
    """
    return {str(path.relative_to(repo)) for path in repo.rglob("*") if path.is_file()}


def test_an_edited_row_is_written_to_its_per_run_file(repo) -> None:
    """Editing a row through the writer keeps the file equal to the row.

    Read the run, drop a field, write it back — the read that follows must not
    be refused, and the per-run file must carry the same edit rather than the
    field the row no longer holds.
    """
    _seed(repo)
    before = _tree(repo)

    data, version = ledger.load(PROJECT, repo)
    edited = data["runs"][0]
    edited.pop("time_budget")
    ledger.write(PROJECT, data, version, repo)

    reread, _ = ledger.load(PROJECT, repo)
    assert "time_budget" not in reread["runs"][0]
    assert "time_budget" not in json.loads(_file_text(repo))
    assert _file_text(repo) == ledger.serialize_run(reread["runs"][0])

    # The write reached the two state files it owns and nothing else: no
    # temporary left behind by the replacement, no file created elsewhere in
    # the checkout.
    assert _tree(repo) == before


def test_a_file_edited_outside_the_writer_is_still_refused(repo) -> None:
    """The falsifier: a direct file edit is a disagreement the reader refuses.

    With the aggregate left untouched, a per-run file carrying different bytes
    is exactly the state :func:`ledger.load` must refuse, so keeping the writer
    honest must not make the reader tolerant of a hand-edited file.
    """
    row = _seed(repo)

    tampered = dict(row, outcome="completed by a hand edit")
    ledger.run_path(PROJECT, RUN_ID, repo).write_text(
        ledger.serialize_run(tampered), encoding="utf-8"
    )

    with pytest.raises(ledger.LedgerError) as excinfo:
        ledger.load(PROJECT, repo)

    assert "differs between" in str(excinfo.value)
    assert RUN_ID in str(excinfo.value)


def test_an_unwritable_run_file_leaves_the_project_readable(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A per-run file that refuses its write must not advance the aggregate.

    This is the failure the store cannot prevent and the writer must survive:
    one per-run file cannot be rewritten -- a read-only tree, a full disk --
    at the moment an edit needs it. If the aggregate envelope moved first, the
    file would keep the revision it held while the row moved past it, and the
    next read would refuse the whole project -- the state this node exists to
    prevent, reached by a write reporting no success at all. Writing the files
    first, and putting back any already changed, means the refusal leaves both
    copies at the revision the reader already had, so the write raises and the
    project stays readable.
    """

    _seed(repo)
    runs_dir = ledger.run_path(PROJECT, RUN_ID, repo).parent
    real_write_text = Path.write_text

    def refuse_run_file(self, *args, **kwargs):
        if self.parent == runs_dir:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_write_text(self, *args, **kwargs)

    data, version = ledger.load(PROJECT, repo)
    data["runs"][0].pop("time_budget")

    monkeypatch.setattr(Path, "write_text", refuse_run_file)
    with pytest.raises(ledger.LedgerError):
        ledger.write(PROJECT, data, version, repo)
    monkeypatch.undo()

    reread, _ = ledger.load(PROJECT, repo)
    assert reread["runs"][0]["time_budget"] == "12m"
    assert _file_text(repo) == ledger.serialize_run(reread["runs"][0])


def test_a_refused_envelope_puts_the_run_file_back(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An envelope that cannot be written must not leave its files ahead.

    Because the per-run files are brought forward first, an aggregate that
    could not be written would leave them holding a revision the row never
    reached -- the same disagreement, arrived at from the other side. The files
    put back in turn means the refusal leaves both copies at the revision the
    reader already had.
    """
    _seed(repo)

    def refuse_envelope(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    data, version = ledger.load(PROJECT, repo)
    data["runs"][0].pop("time_budget")

    monkeypatch.setattr(ledger._store, "_write_json_envelope", refuse_envelope)
    with pytest.raises(OSError, match="No space left on device"):
        ledger.write(PROJECT, data, version, repo)
    monkeypatch.undo()

    reread, _ = ledger.load(PROJECT, repo)
    assert reread["runs"][0]["time_budget"] == "12m"
    assert _file_text(repo) == ledger.serialize_run(reread["runs"][0])


def test_a_loser_of_the_write_race_cannot_overwrite_the_winner(
    repo: Path, tmp_path: Path
) -> None:
    """Two writers from one revision: the loser must not undo the winner.

    A ledger write reads the aggregate at a version, rewrites the per-run files
    that must follow their rows, and then writes the aggregate under that
    version. Two writers prepared from the same revision interleave inside that
    window: the first rewrites its run file, the second rewrites the file and
    commits, and the first, refused on its stale version, puts back the copy it
    captured before the second wrote. The per-run file then holds a row the
    aggregate does not, and the next read refuses the whole project -- the
    disagreement the write ordering exists to prevent, reached by a writer that
    reported no success at all.

    The interleaving is forced rather than hoped for. One writer is held by a
    seam between its per-run write and its envelope write; the other is held
    until that seam is reached, then writes and announces its commit, which is
    what resumes the first. Each runs in its own process, because the exclusion
    that must hold is the one between processes and a thread shares it
    invisibly. Where the writer performs the whole write under the project's
    lock the pause is skipped: that writer already holds the lock the other one
    would need, so a commit during the pause cannot happen and waiting for it
    would wait forever. The claim is weaker than "the first writer wins": either
    writer may reach the lock first, so the assertions are that exactly one is
    refused on the version, that the refusal wrote nothing, that the other
    writer's row survives in both copies, and that the project still reads.
    """
    _seed(repo)
    scratch = tmp_path / "race"
    scratch.mkdir()
    script = tmp_path / "race_child.py"
    script.write_text(_RACE_CHILD, encoding="utf8")

    # The child imports the package under test from the tree this test ran
    # from, resolved through the module itself so the same file works against
    # a checkout other than this one.
    package_root = Path(ledger.__file__).resolve().parents[1]
    env = dict(os.environ)
    inherited = env.get("PYTHONPATH")
    search = [str(package_root)]
    if inherited:
        search.append(inherited)
    env["PYTHONPATH"] = os.pathsep.join(search)
    transcripts: dict[str, str] = {}
    children = {
        role: subprocess.Popen(
            [sys.executable, str(script), role, str(repo), str(scratch)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(tmp_path),
        )
        for role in ("winner", "late")
    }
    for role, process in children.items():
        output, _ = process.communicate(timeout=120)
        assert process.returncode == 0, f"{role} exited {process.returncode}: {output}"
        transcripts[role] = output
    # The children's own lines are the evidence for every claim below: which
    # writer was refused, and with what.
    for role, output in sorted(transcripts.items()):
        print(f"{role}: {output.strip()}")

    outcomes = {
        role: json.loads((scratch / f"{role}.json").read_text(encoding="utf-8"))
        for role in ("winner", "late")
    }
    assert sorted(outcome["result"] for outcome in outcomes.values()) == [
        "conflict",
        "ok",
    ], transcripts

    ok_role = next(role for role, o in outcomes.items() if o["result"] == "ok")
    lost_role = "winner" if ok_role == "late" else "late"
    # Refused on the version, not on a torn read. A writer that reaches the
    # ledger while another is between its per-run write and its envelope write
    # reads a project whose two copies disagree, and an un-serialised writer
    # reports that to its caller as conflicting history -- a corruption the
    # ledger does not have, and the wrong remedy for a write that should simply
    # re-read.
    assert "moved from version" in outcomes[lost_role]["text"], (
        "the refused writer was not answered with a version conflict: "
        + outcomes[lost_role]["text"]
    )

    aggregate, _ = ledger.load(PROJECT, repo)
    row = aggregate["runs"][0]
    assert row["outcome"] == f"completed by the {ok_role}"
    assert _file_text(repo) == ledger.serialize_run(row)
    assert f"completed by the {lost_role}" not in json.dumps(aggregate)
    assert f"completed by the {lost_role}" not in _file_text(repo)

    # The exclusion lives outside the tree being written: a lock file inside
    # the checkout would dirty the repository it is protecting and would be
    # written by the same store whose failures the write already has to
    # survive.
    lock_path = ledger.ledger_lock_path(PROJECT)
    assert repo not in lock_path.parents
    assert Path(os.environ["RECKON_HOME"]).resolve() in lock_path.parents
