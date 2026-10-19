"""A list value joined by a separator the parser does not split is refused.

A manifest's list fields are split on commas and newlines. A worker that writes
several entries on one line joined by a semicolon instead gets a value read as a
single entry: the first path satisfies the write-scope audit and every path
after the separator is never checked at all, so the guard looks applied while
covering one entry. The refusal names the field and the separator rather than
reading the joined value, so the scope audit sees every entry or a refusal and
never a joined string.

The accepted forms are asserted beside the refusal: the same paths written
comma-separated and as a JSON array still parse into separate entries and the
out-of-scope entry is reported, so the rule refuses a join and not the paths.
The negative half of the path-like test is asserted too — a semicolon inside a
prose item whose tail is a word rather than a path is left alone — because the
rule fires on the shape of a joined path list, not on every semicolon.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon.crew.node import TaskNode
from reckon.crew.reports import ManifestParseError, audit_manifest, parse_manifest

# The measured shape: a report path under the reports root, then scripts and
# logs under the run directory, all on one bare line joined by ``; ``. Against
# a write scope that declares only the reports root, the first entry is in scope
# and the rest are not.
REPORTS_ROOT = "docs/evidence/reports"
JOINED_PATHS = f"{REPORTS_ROOT}/x.html; scripts/foo.py; logs/bar.log"
COMMA_PATHS = f"{REPORTS_ROOT}/x.html, scripts/foo.py, logs/bar.log"
JSON_PATHS = f'["{REPORTS_ROOT}/x.html", "scripts/foo.py", "logs/bar.log"]'
OUT_OF_SCOPE = ("scripts/foo.py", "logs/bar.log")


def _manifest(changed_paths: str) -> str:
    return "\n".join(
        [
            "node: a-joined-path-list-is-refused",
            "status: complete",
            "commits: 1a2b3c4",
            "tests: pytest -q tests/x.py (passed)",
            f"changed_paths: {changed_paths}",
        ]
    )


def _node() -> TaskNode:
    return TaskNode(
        id="a-joined-path-list-is-refused",
        goal="refuse a list value joined by an unsplit separator",
        plan="scoring-should-stop-a-promotion",
        write_paths=[REPORTS_ROOT],
    )


def _audit(changed_paths: str, tree: Path) -> dict:
    return audit_manifest(
        _manifest(changed_paths), _node(), worktree=tree, repository=tree
    )


def test_a_joined_path_list_is_refused(tmp_path: Path) -> None:
    # The joined value is refused at the parse, naming the field and the
    # separator, rather than read as one entry.
    with pytest.raises(ManifestParseError) as raised:
        parse_manifest(_manifest(JOINED_PATHS))
    message = str(raised.value)
    assert "changed_paths" in message
    assert "';'" in message

    # Entered through the audit a worker runs itself, the same value is refused
    # rather than answered ``ok`` with the entries after the separator unseen.
    audit = _audit(JOINED_PATHS, tmp_path)
    assert audit["ok"] is False
    joined = "\n".join(audit["findings"])
    assert "changed_paths" in joined
    assert "';'" in joined


def test_the_same_paths_comma_separated_still_parse_into_entries(
    tmp_path: Path,
) -> None:
    parsed = parse_manifest(_manifest(COMMA_PATHS))
    assert parsed["changed_paths"] == [
        f"{REPORTS_ROOT}/x.html",
        *OUT_OF_SCOPE,
    ]

    audit = _audit(COMMA_PATHS, tmp_path)
    assert audit["ok"] is False
    stray = "\n".join(audit["findings"])
    for path in OUT_OF_SCOPE:
        assert path in stray


def test_the_same_paths_as_a_json_array_still_parse_into_entries(
    tmp_path: Path,
) -> None:
    parsed = parse_manifest(_manifest(JSON_PATHS))
    assert parsed["changed_paths"] == [
        f"{REPORTS_ROOT}/x.html",
        *OUT_OF_SCOPE,
    ]

    audit = _audit(JSON_PATHS, tmp_path)
    assert audit["ok"] is False
    stray = "\n".join(audit["findings"])
    for path in OUT_OF_SCOPE:
        assert path in stray


def test_an_entry_retaining_a_semicolon_after_splitting_is_refused(
    tmp_path: Path,
) -> None:
    # A value whose comma-split leaves a semicolon and a path in one entry is
    # refused the same way: the join is caught whether it spans the whole value
    # or survives inside a single entry.
    retained = f"{REPORTS_ROOT}/x.html, scripts/foo.py; logs/bar.log"
    with pytest.raises(ManifestParseError) as raised:
        parse_manifest(_manifest(retained))
    assert "changed_paths" in str(raised.value)
    assert "';'" in str(raised.value)


def test_a_semicolon_in_prose_is_left_alone(tmp_path: Path) -> None:
    # A semicolon whose tail is a word rather than a path is prose, not a
    # joined path list, so a blocker written that way is read as written.
    prose = "tried the smoke run; it failed on the warm start"
    manifest = "\n".join(
        [
            "node: a-joined-path-list-is-refused",
            "status: blocked",
            f"blockers: {prose}",
            "changed_paths: none",
        ]
    )
    parsed = parse_manifest(manifest)
    assert parsed["blockers"] == [prose]
