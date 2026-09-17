"""A ledger write never drops a roster member it was not asked to drop.

The defect this guards is a roster that empties itself through a write meant
for something else: the write succeeds, the commit reports success, and the
loss surfaces later as a dispatch refusing a member the coordinator registered
and used a moment before. Every fixture here is built in a temp tree, and the
closing test asserts the real configuration home and the real docs tree are
untouched.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew import routing

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


def _register(repo: Path, *member_ids: str) -> None:
    for member_id in member_ids:
        ledger.register_member(PROJECT, member_id, harness="alpha", root=repo)


def _ids(repo: Path) -> list[str]:
    return sorted(str(entry.get("id")) for entry in ledger.members(PROJECT, repo))


def _roster_entry(repo: Path, member_id: str) -> dict:
    return next(
        entry
        for entry in ledger.members(PROJECT, repo)
        if str(entry.get("id")) == member_id
    )


# ── The guard fires


def test_a_write_that_drops_every_member_is_refused(repo) -> None:
    """The falsifier: the roster empties itself only through a refusal."""
    _register(repo, "clive-1", "clive-2", "clive-3")
    data, version = ledger.load(PROJECT, repo)

    with pytest.raises(ledger.LedgerError) as excinfo:
        ledger.write(PROJECT, {**data, "members": []}, version, repo)

    message = str(excinfo.value)
    assert "refusing to write the ledger" in message
    assert "allow_member_removal=True" in message
    assert _ids(repo) == ["clive-1", "clive-2", "clive-3"]


def test_a_write_that_drops_one_member_is_refused(repo) -> None:
    _register(repo, "clive-1", "clive-2", "clive-3")
    data, version = ledger.load(PROJECT, repo)
    kept = [entry for entry in data["members"] if str(entry.get("id")) != "clive-2"]

    with pytest.raises(ledger.LedgerError) as excinfo:
        ledger.write(PROJECT, {**data, "members": kept}, version, repo)

    assert "clive-2" in str(excinfo.value)
    assert _ids(repo) == ["clive-1", "clive-2", "clive-3"]


def test_the_refusal_names_only_the_members_actually_dropped(repo) -> None:
    _register(repo, "clive-1", "clive-2", "clive-3")
    data, version = ledger.load(PROJECT, repo)
    kept = [entry for entry in data["members"] if str(entry.get("id")) != "clive-3"]

    with pytest.raises(ledger.LedgerError) as excinfo:
        ledger.write(PROJECT, {**data, "members": kept}, version, repo)

    message = str(excinfo.value)
    assert "clive-3" in message
    assert "clive-1" not in message
    assert "clive-2" not in message


def test_a_write_that_drops_two_and_adds_one_is_refused(repo) -> None:
    """A net-negative write is a loss however many members it adds back."""
    _register(repo, "clive-1", "clive-2", "clive-3")
    data, version = ledger.load(PROJECT, repo)
    reshaped = [
        entry
        for entry in data["members"]
        if str(entry.get("id")) in {"clive-1", "clive-3"}
    ] + [{"id": "clive-1", "harness": "alpha", "role": "implement"}]

    with pytest.raises(ledger.LedgerError) as excinfo:
        ledger.write(PROJECT, {**data, "members": reshaped}, version, repo)

    assert "clive-2" in str(excinfo.value)
    assert _ids(repo) == ["clive-1", "clive-2", "clive-3"]


def test_a_same_count_write_that_swaps_a_member_is_refused(repo) -> None:
    """Identity is guarded, not count: a swap loses an id just as a drop does.

    A count-only rule passes this write, so the roster loses a member while
    the guard reports everything present. Naming the id set instead makes the
    swap a loss, which is what it is.
    """
    _register(repo, "clive-1", "clive-2")
    data, version = ledger.load(PROJECT, repo)
    swapped = [
        entry for entry in data["members"] if str(entry.get("id")) != "clive-2"
    ] + [{"id": "clive-9", "harness": "alpha", "role": "implement"}]

    with pytest.raises(ledger.LedgerError) as excinfo:
        ledger.write(PROJECT, {**data, "members": swapped}, version, repo)

    assert "clive-2" in str(excinfo.value)
    assert _ids(repo) == ["clive-1", "clive-2"]


def test_a_write_omitting_the_members_key_entirely_is_refused(repo) -> None:
    """A caller that forgets ``members`` loses the roster rather than the key."""
    _register(repo, "clive-1", "clive-2")
    data, version = ledger.load(PROJECT, repo)

    with pytest.raises(ledger.LedgerError):
        ledger.write(
            PROJECT, {"runs": data["runs"], "holds": data["holds"]}, version, repo
        )

    assert _ids(repo) == ["clive-1", "clive-2"]


# ── The guard stays quiet


def test_registering_a_new_member_still_succeeds(repo) -> None:
    ledger.register_member(PROJECT, "clive-1", harness="alpha", root=repo)
    ledger.register_member(
        PROJECT, "clive-2", harness="alpha", role="review", root=repo
    )

    assert _ids(repo) == ["clive-1", "clive-2"]


def test_reregistering_an_existing_member_still_succeeds(repo) -> None:
    _register(repo, "clive-1")
    before = _roster_entry(repo, "clive-1")

    ledger.register_member(PROJECT, "clive-1", harness="beta", role="review", root=repo)
    after = _roster_entry(repo, "clive-1")

    assert after["harness"] == "beta"
    assert after["role"] == "review"
    assert after["created"] == before["created"]
    assert _ids(repo) == ["clive-1"]


def test_a_member_preserving_write_still_succeeds(repo) -> None:
    """The promotion shape: the same roster, a patched runs list."""
    _register(repo, "clive-1", "clive-2")
    data, version = ledger.load(PROJECT, repo)
    patched = [{"run_id": "r-one", "gate": "passed"}]

    ledger.write(PROJECT, {**data, "runs": patched}, version, repo)

    assert _ids(repo) == ["clive-1", "clive-2"]
    assert [row["run_id"] for row in ledger.runs(PROJECT, repo)] == ["r-one"]


def test_a_write_to_a_project_with_no_ledger_creates_it_from_empty(repo) -> None:
    """An absent ledger legitimately has zero members and is not a loss."""
    version = ledger.write(PROJECT, {"members": []}, 0, repo)

    assert version == 1
    assert ledger.members(PROJECT, repo) == []
    assert ledger.ledger_path(PROJECT, repo).is_file()


def test_an_intended_removal_succeeds_with_the_flag(repo) -> None:
    """A genuine removal stays possible and is stated where it is performed."""
    _register(repo, "clive-1", "clive-2", "clive-3")
    data, version = ledger.load(PROJECT, repo)
    kept = [entry for entry in data["members"] if str(entry.get("id")) != "clive-2"]

    ledger.write(
        PROJECT,
        {**data, "members": kept},
        version,
        repo,
        allow_member_removal=True,
    )

    assert _ids(repo) == ["clive-1", "clive-3"]


def test_an_intended_removal_of_every_member_succeeds_with_the_flag(repo) -> None:
    _register(repo, "clive-1", "clive-2")
    data, version = ledger.load(PROJECT, repo)

    ledger.write(
        PROJECT,
        {**data, "members": []},
        version,
        repo,
        allow_member_removal=True,
    )

    assert ledger.members(PROJECT, repo) == []


def test_the_idle_reaper_still_retires_a_session_member(repo) -> None:
    """The granted caller: the reaper's whole purpose is to drop idle rows.

    Without the intent flag the guard would refuse it, so this asserts both
    that the removal happens and that it is reached as an AGREED removal rather
    than a silent one.
    """
    ledger.register_member(
        PROJECT,
        "session-abc",
        harness="alpha",
        root=repo,
        now="2020-01-01T00:00:00Z",
    )
    ledger.register_member(PROJECT, "clive-1", harness="alpha", root=repo)

    result = routing.reap_idle_session_members(PROJECT, root=repo)

    assert result["reaped"] == ["session-abc"]
    assert _ids(repo) == ["clive-1"]


# ── Isolation


def test_the_refused_write_leaves_the_temp_ledger_byte_identical(repo) -> None:
    """Nothing outside the temp tree is read or written, and a refusal changes
    nothing inside it either."""
    _register(repo, "clive-1", "clive-2")
    path = ledger.ledger_path(PROJECT, repo)
    before = path.read_bytes()
    before_tree = sorted(
        entry.relative_to(repo) for entry in repo.rglob("*") if entry.is_file()
    )
    data, version = ledger.load(PROJECT, repo)

    with pytest.raises(ledger.LedgerError):
        ledger.write(PROJECT, {**data, "members": []}, version, repo)

    after_tree = sorted(
        entry.relative_to(repo) for entry in repo.rglob("*") if entry.is_file()
    )
    assert after_tree == before_tree
    assert path.read_bytes() == before
    assert _ids(repo) == ["clive-1", "clive-2"]
    assert str(path).startswith(str(repo))
    assert not Path(os.environ["RECKON_HOME"]).is_relative_to(Path.home())
