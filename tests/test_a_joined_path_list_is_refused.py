"""A list joined by a separator the parser does not split is refused.

A manifest's list fields are split on commas and newlines. A worker that writes
several entries on one bare line joined by a semicolon instead gets a value read
as a *single* entry, and the guard that consumes those entries one at a time
then reads only the first: the write-scope audit checks the first path and
passes the rest, the cited-log audit opens the first log and never reads the
others, and a commit citation resolves the first identifier and hides the
second. The refusal names the field and the separator rather than reading the
joined value, so each guard sees every entry or a refusal and never a joined
string.

The refusal is scoped to the fields whose entries a guard consumes one by one —
``changed_paths``, ``orientation_write_paths``, ``test_logs`` and ``commits`` —
and every other list field keeps the reading it had. A prose field such as
``follow_ons`` or ``blockers`` routinely names a path after a semicolon as
ordinary punctuation, and refusing it would refuse a delivered manifest for its
wording rather than for a joined entry list, so a semicolon there is left as
written.

The accepted forms are asserted beside the refusal: the same paths written
comma-separated and as a JSON array still parse into separate entries and the
out-of-scope entry is reported, so the rule refuses a join and not the paths.
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

# Two gate logs joined by ``; `` — the shape a worker writes when it puts a gate
# and its base arm on one line. Each entry is opened on its own, for its command
# and its EXIT line, so a joined value hands that guard one entry.
JOINED_TEST_LOGS = "logs/one.log; logs/two.log"

# Two commit identifiers joined the same way. Each is resolved on its own, so a
# joined value cites the first and hides the second.
COMMIT_ONE = "1cf42c29e6c9240fd72501531f9e1185dbc3b6fa"
COMMIT_TWO = "5eb4dd585980880c2d66daf419803ddcf1ba2066"
JOINED_COMMITS = f"{COMMIT_ONE}; {COMMIT_TWO}"

# A prose list field naming two paths either side of a semicolon. This is
# ordinary writing in a follow_ons or a blocker, not a joined entry list.
PROSE_WITH_PATHS = "see scripts/foo.py; logs/bar.log for the run logs"


def _manifest(
    changed_paths: str = "none", *, commits: str = "1a2b3c4", **extra: str
) -> str:
    lines = [
        "node: a-joined-path-list-is-refused",
        "status: complete",
        f"commits: {commits}",
        "tests: pytest -q tests/x.py (passed)",
        f"changed_paths: {changed_paths}",
    ]
    lines.extend(f"{key}: {value}" for key, value in extra.items())
    return "\n".join(lines)


def _node() -> TaskNode:
    return TaskNode(
        id="a-joined-path-list-is-refused",
        goal="refuse a path list joined by an unsplit separator",
        plan="scoring-should-stop-a-promotion",
        write_paths=[REPORTS_ROOT],
    )


def _audit(changed_paths: str, tree: Path) -> dict:
    return audit_manifest(
        _manifest(changed_paths), _node(), worktree=tree, repository=tree
    )


def test_a_joined_changed_paths_list_is_refused(tmp_path: Path) -> None:
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


def test_a_joined_orientation_write_paths_list_is_refused() -> None:
    # The write-path declaration is a path list too, so the same join there is
    # refused the same way, naming that field.
    with pytest.raises(ManifestParseError) as raised:
        parse_manifest(_manifest(orientation_write_paths=JOINED_PATHS))
    message = str(raised.value)
    assert "orientation_write_paths" in message
    assert "';'" in message


def test_a_joined_test_logs_list_is_refused() -> None:
    # test_logs entries are opened one at a time for their command and their
    # EXIT line, so a joined value would have the second log never read — the
    # citation guard passing on a log it never opened. It is refused instead.
    with pytest.raises(ManifestParseError) as raised:
        parse_manifest(_manifest(test_logs=JOINED_TEST_LOGS))
    message = str(raised.value)
    assert "test_logs" in message
    assert "';'" in message
    assert "logs/two.log" in message


def test_a_joined_commits_list_is_refused() -> None:
    # Commit identifiers are resolved one at a time, so a joined value cites the
    # first and hides the second. It is refused, and the refusal names the
    # identifier that a comma reading would have cited.
    with pytest.raises(ManifestParseError) as raised:
        parse_manifest(_manifest(commits=JOINED_COMMITS))
    message = str(raised.value)
    assert "commits" in message
    assert "';'" in message
    assert COMMIT_TWO in message


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


def test_an_entry_retaining_a_semicolon_after_splitting_is_refused() -> None:
    # A value whose comma-split leaves a semicolon and a path in one entry is
    # refused the same way: the join is caught whether it spans the whole value
    # or survives inside a single entry.
    retained = f"{REPORTS_ROOT}/x.html, scripts/foo.py; logs/bar.log"
    with pytest.raises(ManifestParseError) as raised:
        parse_manifest(_manifest(retained))
    assert "changed_paths" in str(raised.value)
    assert "';'" in str(raised.value)


def test_outside_scope_prose_naming_two_paths_is_accepted() -> None:
    # A prose field that names two paths either side of a semicolon is not a
    # joined entry list, so it is read as written rather than refused. The same
    # text written as changed_paths is refused, which shows the refusal is
    # scoped to the guarded fields rather than to the semicolon.
    parsed = parse_manifest(_manifest(outside_scope=PROSE_WITH_PATHS))
    assert parsed["outside_scope"] == [PROSE_WITH_PATHS]

    with pytest.raises(ManifestParseError) as raised:
        parse_manifest(_manifest(PROSE_WITH_PATHS))
    assert "changed_paths" in str(raised.value)


def test_a_semicolon_in_blocker_prose_is_left_alone() -> None:
    # A blocker written as one sentence with a semicolon stays prose.
    prose = "tried the smoke run; it failed on the warm start"
    manifest = "\n".join(
        [
            "node: a-joined-path-list-is-refused",
            "status: blocked",
            f"blockers: {prose}",
            "changed_paths: none",
        ]
    )
    assert parse_manifest(manifest)["blockers"] == [prose]
