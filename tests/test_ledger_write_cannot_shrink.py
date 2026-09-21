"""A ledger write never takes the committed run count below what it holds.

A promotion only ever appends a run, so a write that would reduce the count is
by construction a lost read: the writer prepared its payload from a snapshot
older than the file it wrote back, and the rows added in between are gone.
Measured on a project's committed ledger, one ``crew complete`` write took it
from 110 runs and 87 members to a single row, in a commit reading
``62 insertions(+), 22233 deletions(-)``.

The run store keeps every promoted run independently of the ledger file, so
the refusal names it as the recovery. Every fixture here is built in a temp
tree, and the closing test asserts the real configuration home and the real
docs tree are untouched.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from reckon import ledger

PROJECT = "proj"


@pytest.fixture()
def repo(tmp_path):
    """A throwaway checkout carrying this project's state directory."""
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "docs" / "state" / PROJECT / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n"
    )
    return root


def _run(run_id: str) -> dict:
    return {"run_id": run_id, "gate": "passed"}


def _member(member_id: str) -> dict:
    return {"id": member_id, "harness": "alpha"}


def _seed(repo: Path, *, runs: int, members: int) -> None:
    """Create the ledger with the named row counts."""
    data = {
        "runs": [_run(f"r-{index:03d}") for index in range(runs)],
        "members": [_member(f"clive-{index:03d}") for index in range(members)],
        "holds": [],
    }
    ledger.write(PROJECT, data, 0, repo)


def _stored_run_ids(repo: Path) -> list[str]:
    return [str(row.get("run_id")) for row in ledger.load(PROJECT, repo)[0]["runs"]]


# ── The guard fires


def test_a_write_that_reduces_the_run_count_is_refused(repo) -> None:
    """The falsifier: the count only falls through a refusal."""
    _seed(repo, runs=3, members=0)
    data, version = ledger.load(PROJECT, repo)

    with pytest.raises(ledger.LedgerError) as excinfo:
        ledger.write(PROJECT, {**data, "runs": data["runs"][:1]}, version, repo)

    message = str(excinfo.value)
    assert "committed run count from 3 to 1" in message
    assert _stored_run_ids(repo) == ["r-000", "r-001", "r-002"]


def test_the_run_refusal_names_the_run_store_as_the_recovery(repo) -> None:
    """The rows survive in the store, so the refusal names where to find them."""
    _seed(repo, runs=2, members=0)
    data, version = ledger.load(PROJECT, repo)

    with pytest.raises(ledger.LedgerError) as excinfo:
        ledger.write(PROJECT, {**data, "runs": []}, version, repo)

    assert "run store at" in str(excinfo.value)


def test_a_write_that_reduces_the_member_count_is_refused(repo) -> None:
    """The member axis is guarded on its own, naming both counts."""
    _seed(repo, runs=0, members=3)
    data, version = ledger.load(PROJECT, repo)

    with pytest.raises(ledger.LedgerError) as excinfo:
        ledger.write(PROJECT, {**data, "members": data["members"][:1]}, version, repo)

    message = str(excinfo.value)
    assert "member count from 3 to 1" in message
    assert len(ledger.members(PROJECT, repo)) == 3


def test_the_measured_incident_shape_is_refused(repo) -> None:
    """110 runs and 87 members to one row, in a single write, is refused.

    The roster check fires first, so both axes of the one write are asserted:
    the member counts with the removal undeclared, and the run counts once the
    member removal is declared intended, which is what unmasks the run guard.
    """
    _seed(repo, runs=110, members=87)
    truncated = {"runs": [_run("r-only")], "members": []}
    data, version = ledger.load(PROJECT, repo)

    with pytest.raises(ledger.LedgerError) as member_exc:
        ledger.write(PROJECT, {**data, **truncated}, version, repo)
    assert "member count from 87 to 0" in str(member_exc.value)

    with pytest.raises(ledger.LedgerError) as run_exc:
        ledger.write(
            PROJECT,
            {**data, **truncated},
            version,
            repo,
            allow_member_removal=True,
        )
    assert "committed run count from 110 to 1" in str(run_exc.value)

    assert len(_stored_run_ids(repo)) == 110
    assert len(ledger.members(PROJECT, repo)) == 87


def test_a_write_omitting_the_runs_key_entirely_is_refused(repo) -> None:
    """A caller that forgets ``runs`` loses the runs rather than the key."""
    _seed(repo, runs=2, members=1)
    data, version = ledger.load(PROJECT, repo)

    with pytest.raises(ledger.LedgerError):
        ledger.write(
            PROJECT,
            {"members": data["members"], "holds": data["holds"]},
            version,
            repo,
        )

    assert _stored_run_ids(repo) == ["r-000", "r-001"]


def test_a_second_writer_holding_the_pre_first_snapshot_is_refused(repo) -> None:
    """The measured concurrency shape: the refused writer never shrinks the file.

    A first writer appends its row; a second, prepared from the snapshot taken
    before that append, writes back a one-row list. It must be refused, and
    every row the file held — the snapshot's and the first writer's — must
    still be there.
    """
    _seed(repo, runs=3, members=0)
    snapshot, stale_version = ledger.load(PROJECT, repo)

    first, first_version = ledger.load(PROJECT, repo)
    first["runs"] = first["runs"] + [_run("r-winner")]
    ledger.write(PROJECT, first, first_version, repo)

    with pytest.raises(ledger.LedgerError):
        ledger.write(
            PROJECT, {**snapshot, "runs": [_run("r-loser")]}, stale_version, repo
        )

    assert _stored_run_ids(repo) == ["r-000", "r-001", "r-002", "r-winner"]


# ── The guard stays quiet


def test_an_appending_promotion_still_succeeds(repo) -> None:
    """A guard that blocks the healthy path would be removed, so assert it."""
    _seed(repo, runs=2, members=1)

    ledger.append_run(PROJECT, _run("r-new"), root=repo)

    assert _stored_run_ids(repo) == ["r-000", "r-001", "r-new"]


def test_a_patching_write_that_keeps_the_count_still_succeeds(repo) -> None:
    """The promotion shape: the same rows, one field patched."""
    _seed(repo, runs=2, members=1)
    data, version = ledger.load(PROJECT, repo)
    patched = [dict(row, gate="refused") for row in data["runs"]]

    ledger.write(PROJECT, {**data, "runs": patched}, version, repo)

    assert [row["gate"] for row in ledger.load(PROJECT, repo)[0]["runs"]] == [
        "refused",
        "refused",
    ]


# ── Isolation


def test_the_refused_write_leaves_the_temp_ledger_byte_identical(repo) -> None:
    """Nothing outside the temp tree is read or written, and a refusal changes
    nothing inside it either."""
    _seed(repo, runs=2, members=2)
    path = ledger.ledger_path(PROJECT, repo)
    before = path.read_bytes()
    before_tree = sorted(
        entry.relative_to(repo) for entry in repo.rglob("*") if entry.is_file()
    )
    data, version = ledger.load(PROJECT, repo)

    with pytest.raises(ledger.LedgerError):
        ledger.write(PROJECT, {**data, "runs": []}, version, repo)

    after_tree = sorted(
        entry.relative_to(repo) for entry in repo.rglob("*") if entry.is_file()
    )
    assert after_tree == before_tree
    assert path.read_bytes() == before
    assert _stored_run_ids(repo) == ["r-000", "r-001"]
    assert str(path).startswith(str(repo))
    assert not Path(os.environ["RECKON_HOME"]).is_relative_to(Path.home())
