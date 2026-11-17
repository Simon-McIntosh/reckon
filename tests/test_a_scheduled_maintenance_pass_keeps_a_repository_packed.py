"""One maintenance pass packs a repository, and the crew view reports it.

Git leaves an unreachable object loose for two weeks and switches automatic
collection off for a day once the loose count passes ``gc.auto``, so a busy
repository stays unpacked after one failed collection. This pass repacks the
store, keeps the unreachable objects in a cruft pack, and clears the stale
``gc.log`` that switched collection off. Its counts are recorded beside the
repository, and the fleet row reads the loose figure from that record.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

#: The loose count the fixture declares as git's automatic threshold. Git's
#: shipped default is 6,700, which a test cannot reach in the time it may take;
#: what the pass must do is the same at any threshold, so the fixture states a
#: small one and the assertions read it back from the repository.
THRESHOLD = 100

#: Unreachable objects the fixture writes. Comfortably above the threshold, so
#: a repository git would collect by itself is the one the pass is given.
UNREACHABLE = 150


def _git(repository: Path, *args: str, stdin: str | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (args, result.stderr)
    return result.stdout


def _repository(root: Path) -> Path:
    """A repository above git's own collection threshold, with a stale gc.log.

    The unreachable objects are written straight into the object store as
    loose blobs, which is what a discarded run and a conflict check leave
    behind: nothing references them, and nothing collects them.
    """

    repository = root / "checkout"
    (repository / "docs").mkdir(parents=True)
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.email", "fixture@example.invalid")
    _git(repository, "config", "user.name", "fixture")
    _git(repository, "config", "gc.auto", str(THRESHOLD))
    (repository / "README.md").write_text("reachable\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-qm", "the reachable history")

    blobs = root / "unreachable"
    blobs.mkdir()
    paths = []
    for index in range(UNREACHABLE):
        path = blobs / f"blob-{index}.txt"
        path.write_text(f"unreachable content {index}\n", encoding="utf-8")
        paths.append(str(path))
    _git(
        repository,
        "hash-object",
        "-w",
        "--stdin-paths",
        stdin="\n".join(paths) + "\n",
    )
    gc_log = repository / ".git" / "gc.log"
    gc_log.write_text("too many unreachable loose objects\n", encoding="utf-8")
    return repository


def _unreachable_blob(repository: Path) -> str:
    """One object the repository holds and no ref reaches."""

    loose = [
        path
        for path in (repository / ".git" / "objects").glob("??/*")
        if path.suffix != ".pack"
    ]
    assert loose, "the fixture wrote no unreachable loose object"
    directory, name = loose[0].parent.name, loose[0].name
    return directory + name


def _present(repository: Path, sha: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repository), "cat-file", "-e", sha],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def test_one_pass_packs_the_store_and_records_its_counts(
    tmp_path: Path, home: Path
) -> None:
    from reckon import fleet_index
    from reckon.crew import repository_maintenance as maintenance

    repository = _repository(tmp_path)
    unreachable = _unreachable_blob(repository)
    threshold = maintenance.automatic_threshold(repository)
    assert threshold is not None
    before = maintenance.object_counts(repository)
    assert before["count"] > threshold, before
    assert maintenance.gc_log_path(repository).exists()

    record = maintenance.maintenance_pass(repository, home=home)

    after = maintenance.object_counts(repository)
    assert after["count"] < threshold, after
    # The unreachable object is still there, in a cruft pack rather than loose.
    assert maintenance.cruft_packs(repository), maintenance.object_counts(repository)
    assert _present(repository, unreachable)
    # A successful pass clears the gc.log that switched automatic collection off.
    assert maintenance.gc_log_path(repository).exists() is False
    assert record["gc_log_removed"], record

    # The record carries the counts before and after, and says so on disk.
    assert record["loose_before"] == before["count"]
    assert record["loose_after"] == after["count"]
    stored = maintenance.read_record(repository, home=home)
    assert stored is not None
    # The record carries the threshold the repository declares, read against
    # the fixture's own figure rather than against the same function again.
    assert stored["threshold"] == THRESHOLD
    assert stored["loose_before"] == before["count"]
    assert stored["loose_after"] == after["count"]
    assert stored["before"]["count"] == before["count"]
    assert stored["after"]["count"] == after["count"]

    # The crew view that reports the repository reads the recorded figure.
    rows = fleet_index.collect_project_rows({"fixture": repository / "docs"}, home=home)
    assert len(rows) == 1
    assert rows[0]["loose_objects"] == after["count"]
    assert rows[0]["maintenance_ran_at"] == record["ran_at"]


def test_a_pass_reports_the_same_loose_count_a_second_time(
    tmp_path: Path, home: Path
) -> None:
    """A repository already packed costs nothing and still records a pass.

    The schedule runs whether or not there is anything to pack, so a second
    pass must leave the repository's object set exactly where the first left
    it: a maintenance pass that changed a packed store would be repacking the
    whole fleet every day for no gain.
    """

    from reckon.crew import repository_maintenance as maintenance

    repository = _repository(tmp_path)
    maintenance.maintenance_pass(repository, home=home)
    first = maintenance.object_counts(repository)

    maintenance.maintenance_pass(repository, home=home)

    assert maintenance.object_counts(repository) == first
    assert first["count"] < THRESHOLD


def test_a_failed_pass_is_recorded_rather_than_reported_as_packed(
    tmp_path: Path, home: Path
) -> None:
    """A pass that cannot pack records its failure and leaves the gc.log alone.

    Clearing the gc.log of a repository that was never packed would switch
    automatic collection back on to fail on the same population again, so the
    file stays and the record says the pass did not succeed.
    """

    from reckon.crew import repository_maintenance as maintenance

    repository = _repository(tmp_path)
    gc_log = maintenance.gc_log_path(repository)
    assert gc_log.exists()

    record = maintenance.maintenance_pass(repository, home=home, timeout=0)

    assert record["ok"] is False
    assert record["after"] is None
    assert record["loose_after"] is None
    assert record["gc_log_removed"] == ""
    assert gc_log.exists()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))
