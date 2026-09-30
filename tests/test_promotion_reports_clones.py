"""Promotion reports the functions a run adds or modifies that copy an existing one.

The six-line normalised clone detector in ``reckon.clones`` runs over the run's
own added or modified functions against the whole tree at the promoted revision.
A private copy of an existing implementation is the six-line-window duplicate
the crew pattern review measured, so promotion reports it on the ledger row under
``clone_matches`` with both functions' file:line. It warns and never refuses: a
run that adds a function matching nothing records an empty list.
"""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path

import pytest

from reckon import crew, ledger
from reckon.crew import routing
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "sample"

PARSE_UTC_COPY = """def copy_parse_utc(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return _from_epoch(float(value))
    if isinstance(value, str):
        return _from_iso8601(value)
    return None
"""

ORIGINAL_SOURCE = '''"""A timestamp module the run is measured against."""

from __future__ import annotations

from datetime import UTC, datetime

_MS_THRESHOLD = 1e11


def parse_utc(value):
    """Parse a timestamp into an aware UTC datetime, or None when malformed."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return _from_epoch(float(value))
    if isinstance(value, str):
        return _from_iso8601(value)
    return None


def _from_epoch(value):
    seconds = value / 1000.0 if abs(value) >= _MS_THRESHOLD else value
    return datetime.fromtimestamp(seconds, tz=UTC)
'''

UNRELATED = """def unrelated(a):
    total = a + 1
    pair = (total, a)
    scaled = pair[0] * 2
    return scaled + pair[1]
"""

STAMP_MODULE = '''"""A module the base already carries."""

from __future__ import annotations


def format_stamp(value):
    """A helper the base carries and the run overwrites in place."""
    return str(value)
'''

STAMP_MODULE_COPIED = '''"""A module the base already carries."""

from __future__ import annotations


def format_stamp(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return _from_epoch(float(value))
    if isinstance(value, str):
        return _from_iso8601(value)
    return None
'''


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "reckon").mkdir()
    (root / "reckon" / "_timestamps.py").write_text(ORIGINAL_SOURCE, encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "reckon"),
        ("commit", "-q", "-m", "chore: seed"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _detached_tree(repository: Path, path: Path) -> Path:
    _git(repository, "worktree", "add", "-q", "--detach", str(path), "HEAD")
    return path


def _pointer(
    repository: Path,
    run_tree: Path,
    run_id: str,
    base: str,
    *,
    write_paths: tuple[str, ...],
) -> None:
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(run_tree),
            "base_sha": base,
            "launch": "in-harness",
            "role": "implement",
            "backend": "native",
            "created_at": "2026-08-26T12:00:00Z",
            "repository_tree_snapshot": routing._repository_tree_snapshot(repository),
            "node": {
                "id": "clone-check",
                "plan": "fixture",
                "section": "guard",
                "time_budget": "25m",
                "write_paths": list(write_paths),
            },
        },
    )


def _promoted_row(repository: Path, run_id: str) -> dict:
    data, _version = ledger.load(PROJECT, root=repository)
    row = next(
        (item for item in data["runs"] if str(item.get("run_id") or "") == run_id),
        None,
    )
    assert row is not None, "the promotion did not commit a ledger row"
    return dict(row)


def _parse_utc_line(repository: Path) -> int:
    tree = ast.parse((repository / "reckon" / "_timestamps.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "parse_utc":
            return node.lineno
    raise AssertionError("parse_utc is missing from the fixture module")


def test_a_private_copy_is_reported_with_the_function_it_copies(
    repository: Path, tmp_path: Path
) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    run_tree = _detached_tree(repository, tmp_path / "run-tree")
    run_id = "r-clone-copy"
    (run_tree / "reckon" / "private_copy.py").write_text(
        PARSE_UTC_COPY, encoding="utf-8"
    )
    _git(run_tree, "add", "reckon/private_copy.py")
    _git(run_tree, "commit", "-q", "-m", "test: add a private copy of parse_utc")
    commit = _git(run_tree, "rev-parse", "HEAD")
    _pointer(
        repository,
        run_tree,
        run_id,
        base,
        write_paths=("reckon/private_copy.py",),
    )

    crew.complete(run_id, gate="passed", commits=[commit], root=repository)

    row = _promoted_row(repository, run_id)
    matches = row.get("clone_matches")
    assert isinstance(matches, list) and matches, row
    existing = matches[0]["existing_function"]
    assert existing["path"] == "reckon/_timestamps.py"
    assert existing["name"] == "parse_utc"
    assert existing["line"] == _parse_utc_line(repository)
    assert matches[0]["run_function"]["path"] == "reckon/private_copy.py"


def test_a_modified_function_becoming_a_copy_is_reported(
    repository: Path, tmp_path: Path
) -> None:
    """The run rewrites an existing function, in a file the base carries.

    The file is present at the base and the function name is unchanged, so the
    run's function is not an addition: it is a modification. The detector must
    still see the rewritten body as a copy of ``parse_utc`` rather than treating
    the file as untouched.
    """
    target = repository / "reckon" / "stamps.py"
    target.write_text(STAMP_MODULE, encoding="utf-8")
    _git(repository, "add", "reckon/stamps.py")
    _git(repository, "commit", "-q", "-m", "feat: add a stamp formatter")
    base = _git(repository, "rev-parse", "HEAD")
    run_tree = _detached_tree(repository, tmp_path / "run-tree")
    run_id = "r-clone-modified"
    (run_tree / "reckon" / "stamps.py").write_text(
        STAMP_MODULE_COPIED, encoding="utf-8"
    )
    _git(run_tree, "add", "reckon/stamps.py")
    _git(run_tree, "commit", "-q", "-m", "test: rewrite the formatter as a copy")
    commit = _git(run_tree, "rev-parse", "HEAD")
    _pointer(
        repository,
        run_tree,
        run_id,
        base,
        write_paths=("reckon/stamps.py",),
    )

    crew.complete(run_id, gate="passed", commits=[commit], root=repository)

    row = _promoted_row(repository, run_id)
    matches = row.get("clone_matches")
    assert isinstance(matches, list) and matches, row
    existing = matches[0]["existing_function"]
    assert existing["path"] == "reckon/_timestamps.py"
    assert existing["name"] == "parse_utc"
    assert existing["line"] == _parse_utc_line(repository)
    assert matches[0]["run_function"]["path"] == "reckon/stamps.py"


def test_a_function_matching_nothing_reports_an_empty_list(
    repository: Path, tmp_path: Path
) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    run_tree = _detached_tree(repository, tmp_path / "run-tree")
    run_id = "r-clone-none"
    (run_tree / "reckon" / "unrelated.py").write_text(UNRELATED, encoding="utf-8")
    _git(run_tree, "add", "reckon/unrelated.py")
    _git(run_tree, "commit", "-q", "-m", "test: add an unrelated function")
    commit = _git(run_tree, "rev-parse", "HEAD")
    _pointer(
        repository,
        run_tree,
        run_id,
        base,
        write_paths=("reckon/unrelated.py",),
    )

    crew.complete(run_id, gate="passed", commits=[commit], root=repository)

    row = _promoted_row(repository, run_id)
    assert row.get("clone_matches") == []


def test_an_unmeasured_run_is_not_recorded_as_an_empty_match_list(
    repository: Path, tmp_path: Path
) -> None:
    """A run with no revision pair to compare carries the unmeasured marker.

    A commitless promotion asserts no revision, so the detector never ran. The
    row must record that as a marker rather than as the empty list a measured
    run whose functions copied nothing carries, or a reader cannot tell the two
    apart.
    """
    run_id = "r-clone-unmeasured"
    head = _git(repository, "rev-parse", "HEAD")
    delivered = tmp_path / "elsewhere" / "review.json"
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: clone-check\n"
        "status: complete\n"
        f"changed_paths: {delivered}\n"
        "tests: report-only promotion; nothing to run\n",
        encoding="utf-8",
    )
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "base_sha": head,
            "launch": "in-harness",
            "role": "review",
            "backend": "native",
            "created_at": "2026-09-30T12:00:00Z",
            "manifest_path": str(manifest),
            "node": {
                "id": "clone-check",
                "plan": "fixture",
                "section": "guard",
                "time_budget": "25m",
                "write_paths": [str(delivered)],
            },
        },
    )

    crew.complete(run_id, gate="passed", root=repository)

    row = _promoted_row(repository, run_id)
    report = row.get("clone_matches")
    assert report != []
    assert isinstance(report, dict) and report["status"] == "unmeasured"
    assert report["reason"]
