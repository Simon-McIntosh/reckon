"""Appending a run preserves aggregate history and writes only its own file."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import ledger

PROJECT = "proj"


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _configure(root: Path) -> None:
    _git(root, "config", "user.email", "crew@example.invalid")
    _git(root, "config", "user.name", "crew")


def _history(root: Path, path: Path) -> str:
    """Return the commits git records for the ledger path, empty when none."""
    return _git(
        root, "log", "--all", "--format=%H", "--", path.relative_to(root).as_posix()
    ).stdout


def _write_ledger(root: Path, rows: list[dict[str, str]]) -> Path:
    path = ledger.ledger_path(PROJECT, root)
    path.write_text(
        json.dumps(
            {
                "project": PROJECT,
                "doc": ledger.LEDGER_SLUG,
                "data": {"_version": 1, "members": [], "runs": rows, "holds": []},
            }
        )
    )
    return path


def _clone(source: Path, destination: Path, *, depth: int | None = None) -> Path:
    """Clone a real repository; ``file://`` keeps ``--depth`` meaningful."""
    command = ["git", "clone", "-q"]
    if depth is not None:
        command += ["--depth", str(depth)]
    command += [f"file://{source}", str(destination)]
    subprocess.run(command, check=True, capture_output=True, text=True)
    return destination


def _partial_clone(source: Path, destination: Path) -> Path:
    """A genuine blobless partial clone; the origin must allow filtering."""
    _git(source, "config", "uploadpack.allowFilter", "true")
    subprocess.run(
        [
            "git",
            "clone",
            "-q",
            "--filter=blob:none",
            f"file://{source}",
            str(destination),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return destination


def _grafts_path(clone: Path) -> Path:
    reported = _git(clone, "rev-parse", "--git-path", "info/grafts").stdout.strip()
    grafts = Path(reported)
    return grafts if grafts.is_absolute() else clone / grafts


def _origin_with_removed_ledger(tmp_path: Path) -> Path:
    """An origin whose ledger was committed and deleted inside its history.

    A later commit advances the index so a depth-1 clone's boundary sits after
    the deletion: the clone holds no commit that can account for the path.
    """
    origin = tmp_path / "origin"
    state = origin / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    index = state / "index.json"
    index.write_text(json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n")
    _git(origin, "init", "-q")
    _configure(origin)
    _git(origin, "add", "docs/state/proj/index.json")
    _git(origin, "commit", "-qm", "record project index")

    path = _write_ledger(origin, [{"run_id": "r-prior"}])
    _git(origin, "add", path.relative_to(origin).as_posix())
    _git(origin, "commit", "-qm", "record promotion")
    path.unlink()
    _git(origin, "add", path.relative_to(origin).as_posix())
    _git(origin, "commit", "-qm", "remove the ledger")

    index.write_text(json.dumps({"project": PROJECT, "data": {"_version": 1}}) + "\n")
    _git(origin, "add", "docs/state/proj/index.json")
    _git(origin, "commit", "-qm", "advance the project index")
    return origin


def _origin_never_tracking_ledger(tmp_path: Path) -> Path:
    """An origin with real history that never recorded the ledger path."""
    origin = tmp_path / "origin"
    state = origin / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    index = state / "index.json"
    index.write_text(json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n")
    _git(origin, "init", "-q")
    _configure(origin)
    _git(origin, "add", "docs/state/proj/index.json")
    _git(origin, "commit", "-qm", "record project index")
    index.write_text(json.dumps({"project": PROJECT, "data": {"_version": 1}}) + "\n")
    _git(origin, "add", "docs/state/proj/index.json")
    _git(origin, "commit", "-qm", "advance the project index")
    return origin


@pytest.fixture()
def repository(tmp_path: Path) -> Path:
    """A real checkout whose ledger path git has never recorded."""
    root = tmp_path / "repository"
    state = root / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n"
    )
    _git(root, "init", "-q")
    _configure(root)
    _git(root, "add", "docs/state/proj/index.json")
    _git(root, "commit", "-qm", "record project index")
    return root


def _record(run_id: str = "r-first") -> dict[str, str]:
    return {"run_id": run_id, "gate": "passed"}


def _assert_appended_file(root: Path, result: dict) -> None:
    """A fresh append writes the canonical run without creating an aggregate."""
    path = root / "docs" / "state" / PROJECT / "runs" / "r-first.json"
    assert result["path"] == str(path)
    assert path.read_text() == ledger.serialize_run(_record())
    assert result["version"] is None
    assert result["store"] == {"status": "written"}
    assert ledger.index_lag(PROJECT, root) == 0
    assert not ledger.ledger_path(PROJECT, root).exists()


def _commit_then_remove_ledger(root: Path) -> Path:
    """Reproduce the incident shape: the ledger is committed, then unlinked."""
    path = _write_ledger(root, [{"run_id": "r-prior"}])
    _git(root, "add", path.relative_to(root).as_posix())
    _git(root, "commit", "-qm", "record promotion")
    path.unlink()
    return path


def test_a_promotion_refuses_a_ledger_deleted_from_the_tree(repository: Path) -> None:
    """A ledger git once tracked is a recovery condition, not an empty project."""
    path = _commit_then_remove_ledger(repository)
    assert _history(repository, path), "fixture must carry history for the path"

    with pytest.raises(ledger.LedgerError) as excinfo:
        ledger.append_run(PROJECT, _record(), root=repository)

    message = str(excinfo.value)
    assert str(path) in message
    assert "independent authority that holds every promoted run" in message
    assert not path.exists()


def test_a_promotion_initialises_a_ledger_git_never_tracked(repository: Path) -> None:
    """A path git has never recorded is a new project, not a deleted ledger."""
    path = ledger.ledger_path(PROJECT, repository)
    assert _history(repository, path) == "", "fixture must have no history"

    result = ledger.append_run(PROJECT, _record(), root=repository)

    stored, version = ledger.load(PROJECT, repository)
    _assert_appended_file(repository, result)
    assert version == 0
    assert [row["run_id"] for row in stored["runs"]] == ["r-first"]
    assert not path.exists()


def test_a_shallow_clone_refuses_a_ledger_deleted_before_its_boundary(
    tmp_path: Path,
) -> None:
    """A depth-1 clone cannot answer for a deletion outside its boundary."""
    origin = _origin_with_removed_ledger(tmp_path)
    clone = _clone(origin, tmp_path / "shallow", depth=1)
    path = ledger.ledger_path(PROJECT, clone)
    assert (
        _git(clone, "rev-parse", "--is-shallow-repository").stdout.strip() == "true"
    ), "fixture must be a shallow clone"
    assert not path.exists()
    assert _history(clone, path) == "", "the boundary hides the path's history"

    with pytest.raises(ledger.LedgerError) as excinfo:
        ledger.append_run(PROJECT, _record(), root=clone)

    message = str(excinfo.value)
    assert str(path) in message
    assert "independent authority that holds every promoted run" in message
    assert not path.exists()


def test_a_full_clone_never_tracking_the_ledger_initialises_it(tmp_path: Path) -> None:
    """A complete history that never recorded the path is a new project."""
    origin = _origin_never_tracking_ledger(tmp_path)
    clone = _clone(origin, tmp_path / "full")
    path = ledger.ledger_path(PROJECT, clone)
    assert (
        _git(clone, "rev-parse", "--is-shallow-repository").stdout.strip() == "false"
    ), "the probe must be able to answer a complete history"
    assert _history(clone, path) == "", "fixture must have no history for the path"

    result = ledger.append_run(PROJECT, _record(), root=clone)

    stored, version = ledger.load(PROJECT, clone)
    _assert_appended_file(clone, result)
    assert version == 0
    assert [row["run_id"] for row in stored["runs"]] == ["r-first"]
    assert not path.exists()


def test_a_partial_clone_does_not_initialise_a_ledger(tmp_path: Path) -> None:
    """A partial clone may be missing the objects the answer rests on."""
    origin = _origin_never_tracking_ledger(tmp_path)
    clone = _partial_clone(origin, tmp_path / "partial")
    path = ledger.ledger_path(PROJECT, clone)
    assert (
        _git(clone, "config", "--get", "remote.origin.promisor").stdout.strip()
        == "true"
    ), "fixture must be a partial clone"
    assert _history(clone, path) == "", "git's own answer would read never-tracked"

    with pytest.raises(ledger.LedgerError):
        ledger.append_run(PROJECT, _record(), root=clone)

    assert not path.exists()


def test_a_replace_ref_does_not_initialise_a_ledger(tmp_path: Path) -> None:
    """A replace ref rewrites history, so an empty log speaks for nothing."""
    origin = _origin_never_tracking_ledger(tmp_path)
    clone = _clone(origin, tmp_path / "replaced")
    _git(clone, "replace", "--graft", "HEAD")
    path = ledger.ledger_path(PROJECT, clone)
    assert _git(
        clone, "for-each-ref", "--format=%(refname)", "refs/replace/"
    ).stdout.strip(), "fixture must carry a replace ref"
    assert _history(clone, path) == "", "git's own answer would read never-tracked"

    with pytest.raises(ledger.LedgerError):
        ledger.append_run(PROJECT, _record(), root=clone)

    assert not path.exists()


def test_an_info_grafts_file_does_not_initialise_a_ledger(tmp_path: Path) -> None:
    """The classic graft file hides history the same way a replace ref does."""
    origin = _origin_never_tracking_ledger(tmp_path)
    clone = _clone(origin, tmp_path / "grafts")
    grafts = _grafts_path(clone)
    grafts.write_text(_git(clone, "rev-parse", "HEAD").stdout.strip() + "\n")
    path = ledger.ledger_path(PROJECT, clone)
    assert grafts.exists(), "fixture must carry a grafts file"
    assert _history(clone, path) == "", "git's own answer would read never-tracked"

    with pytest.raises(ledger.LedgerError):
        ledger.append_run(PROJECT, _record(), root=clone)

    assert not path.exists()


def test_a_ledger_outside_a_checkout_still_requires_explicit_initialisation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Where git cannot answer, the refusal stands and ``allow_create`` is the way.

    ``GIT_CEILING_DIRECTORIES`` is pinned to the temporary root so discovery
    cannot ascend into an ambient checkout when ``tmp_path`` happens to sit
    inside one — the case tests the code rather than the machine.
    """
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    root = tmp_path / "nowhere"
    state = root / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n"
    )

    with pytest.raises(ledger.LedgerError):
        ledger.append_run(PROJECT, _record(), root=root)

    result = ledger.append_run(PROJECT, _record(), root=root, allow_create=True)
    stored, version = ledger.load(PROJECT, root)
    _assert_appended_file(root, result)
    assert version == 0
    assert [row["run_id"] for row in stored["runs"]] == ["r-first"]


@pytest.mark.parametrize("key", ["members", "runs"])
def test_append_scan_miss_preserves_an_unrelated_malformed_collection(
    repository: Path, key: str
) -> None:
    """A byte-scan miss leaves unrelated aggregate content untouched."""
    path = ledger.ledger_path(PROJECT, repository)
    path.write_text(
        json.dumps(
            {
                "project": PROJECT,
                "doc": ledger.LEDGER_SLUG,
                "data": {
                    "_version": 4,
                    "members": [] if key != "members" else {"bad": "shape"},
                    "runs": [] if key != "runs" else {"bad": "shape"},
                    "holds": [],
                },
            }
        )
    )

    before = path.read_bytes()
    assert _record()["run_id"].encode() not in before
    with pytest.raises(ledger.LedgerError, match=rf"{key} must be list-valued"):
        ledger.load(PROJECT, repository)

    result = ledger.append_run(PROJECT, _record(), root=repository)

    target = ledger.run_path(PROJECT, "r-first", repository)
    assert result["path"] == str(target)
    assert target.read_text() == ledger.serialize_run(_record())
    assert result["store"] == {"status": "written"}
    assert path.read_bytes() == before
    with pytest.raises(ledger.LedgerError, match=rf"{key} must be list-valued"):
        ledger.load(PROJECT, repository)
