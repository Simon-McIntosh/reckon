"""A manifest key is read at the top level only.

An indented two-space ``commits: [...]`` line under a parent (a failure
attribution block, for example) used to parse exactly like a column-0 key and
overwrite the node-level value a coordinator acts on. Crew surfaces then
rendered the nested value — complete with the brackets and quotes left by
splitting a bracketed list on commas — into a runnable command. These tests
pin the fix: only a column-0 key sets a field, an indented list item still
appends to its key, and a bracketed list decodes to clean elements.
"""

from __future__ import annotations

from reckon.crew import reports

# Shape observed in a real fleet manifest: a top-level commits line plus a
# nested, indented commits line under the failure attribution block.
_NESTED_OVERWRITE_MANIFEST = """\
status: complete
commits: ["1ea93df8a"]
changed_paths: ["reckon/crew/reports.py","tests/test_manifest_nested_keys.py"]
test_logs:
  - /durable/foo.log
  - /abs/bar/baz.log
failure_attribution: {"test_local": "abc123"}
  commits: ["94d31af9f", "57155c6e6"]
"""


def test_a_nested_key_does_not_overwrite_a_top_level_value() -> None:
    manifest = reports.parse_manifest(_NESTED_OVERWRITE_MANIFEST)

    assert manifest["commits"] == ["1ea93df8a"]


def test_a_parent_with_an_empty_value_keeps_nested_keys_nested() -> None:
    manifest = reports.parse_manifest(
        "status: complete\n"
        'commits: ["1ea93df8a"]\n'
        "failure_attribution:\n"
        "  test_local: abc123\n"
        '  commits: ["94d31af9f", "57155c6e6"]\n'
    )

    assert manifest["commits"] == ["1ea93df8a"]
    # The nested candidate never leaks out as a top-level key.
    assert "test_local" not in manifest


def test_a_nested_only_commit_never_becomes_a_top_level_value() -> None:
    manifest = reports.parse_manifest(
        'status: blocked\nfailure_attribution:\n  commits: ["94d31af9f", "57155c6e6"]\n'
    )

    assert manifest["commits"] == []


def test_an_indented_list_item_still_appends_to_its_key() -> None:
    manifest = reports.parse_manifest(_NESTED_OVERWRITE_MANIFEST)

    assert manifest["test_logs"] == ["/durable/foo.log", "/abs/bar/baz.log"]


def test_a_bracketed_list_parses_to_bare_elements() -> None:
    manifest = reports.parse_manifest('commits: ["94d31af9f", "57155c6e6"]\n')

    assert manifest["commits"] == ["94d31af9f", "57155c6e6"]
    assert all(
        "[" not in item and "]" not in item and '"' not in item
        for item in manifest["commits"]
    )


def test_the_next_action_built_from_commits_has_no_bracket() -> None:
    manifest = reports.parse_manifest('commits: ["94d31af9f", "57155c6e6"]\n')

    action = "reckon crew complete --run <run> --gate <verdict>"
    action += "".join(f" --commit {commit}" for commit in manifest["commits"])

    assert "[" not in action and "]" not in action
    assert "--commit 94d31af9f --commit 57155c6e6" in action


def test_a_plain_comma_list_still_splits() -> None:
    manifest = reports.parse_manifest("commits: abc123, def456\n")

    assert manifest["commits"] == ["abc123", "def456"]
