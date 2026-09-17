"""A manifest field's type comes from the declared schema, not from string splitting.

Seven point repairs to the tolerant line reader did not generalise, so the
reader is driven by a declared schema: each field names the shapes it accepts,
and a real parser (``yaml.compose``, which keeps every scalar's literal text)
decodes the structure. The four defects below are the measured cases that the
line scanner got wrong, each one a value whose type depended on how a string
happened to be split rather than on what the schema says:

* a block list is a list of items, so a comma inside a commit subject is part
  of the item rather than a separator that manufactures a fourth revision;
* a nested mapping is the writer's mapping, so it arrives whole rather than as
  the empty string a skipped body produced;
* a quoted scalar carries its own quoting rules, so a flow list's quoted item
  keeps its commas and its text instead of being split into a fragment;
* a nested key stays under its parent, so the mapping reads under that parent
  rather than being lost and leaving the top-level field untouched.

The negative half is asserted too: a body whose composed shape the field's
declared kind does not accept raises and names the field, rather than
half-succeeding with a plausible value.
"""

from __future__ import annotations

import pytest

from reckon.crew import reports
from reckon.crew.node import CrewError

# Three commits, the third subject carrying a comma. The tolerant reader joined
# the block items with ", " and split the join on commas, so the subject's own
# comma became a fourth revision consisting of a prose fragment.
_THREE_COMMITS_ONE_COMMA = """\
status: complete
commits:
  - 1111111 add the first guard
  - 2222222 add the second guard
  - 3333333 record the measurement, its measurement and the correction it forced
changed_paths:
  - reckon/crew/reports.py
"""

# A flow list whose first item is unquoted, so it is not JSON and the fallback
# splitter cut on every comma — including the one inside the quoted item — and
# stripped the quote characters off each fragment.
_FLOW_LIST_WITH_A_QUOTED_ITEM = """\
status: complete
commits: [29937e892, "5a1b2c3d4 record the measurement, its measurement and the correction it forced"]
"""

# A nested mapping under a text-or-mapping field. The body was collected by no
# field at all, so the field read as the empty string.
_NESTED_TESTS_MAPPING = """\
status: complete
tests:
  passed: 41
  failed: 0
"""


def test_a_block_list_item_keeps_its_own_commas() -> None:
    manifest = reports.parse_manifest(_THREE_COMMITS_ONE_COMMA)

    assert len(manifest["commits"]) == 3
    assert manifest["commits"][-1] == (
        "3333333 record the measurement, its measurement and the correction it forced"
    )
    assert "its measurement and the correction it forced" not in manifest["commits"]


def test_a_field_after_the_list_is_still_read() -> None:
    # The list body must end where the next field begins, so the field written
    # after it is not folded into the last item.
    manifest = reports.parse_manifest(_THREE_COMMITS_ONE_COMMA)

    assert manifest["changed_paths"] == ["reckon/crew/reports.py"]


def test_a_nested_mapping_under_tests_reads_as_that_mapping() -> None:
    manifest = reports.parse_manifest(_NESTED_TESTS_MAPPING)

    assert manifest["tests"] == {"passed": "41", "failed": "0"}


def test_a_quoted_item_in_a_flow_list_keeps_its_commas_and_text() -> None:
    manifest = reports.parse_manifest(_FLOW_LIST_WITH_A_QUOTED_ITEM)

    assert manifest["commits"] == [
        "29937e892",
        "5a1b2c3d4 record the measurement, its measurement and the correction it forced",
    ]


def test_a_nested_key_stays_under_its_parent() -> None:
    manifest = reports.parse_manifest(
        "status: complete\n"
        "failure_attribution:\n"
        "  test_local: abc123\n"
        "  test_other: def456\n"
    )

    assert manifest["failure_attribution"] is not None
    assert manifest["failure_attribution"]["test_local"] == "abc123"
    assert manifest["failure_attribution"]["test_other"] == "def456"
    assert "test_local" not in manifest


def test_a_mapping_body_under_a_list_field_is_refused_naming_the_field() -> None:
    with pytest.raises(reports.ManifestParseError) as excinfo:
        reports.parse_manifest(
            "status: complete\nchanged_paths:\n  files: 2\n  root: src\n"
        )

    assert "changed_paths" in str(excinfo.value)


def test_the_same_mapping_body_is_accepted_under_a_field_that_declares_it() -> None:
    # Positive control for the refusal above: the shape is not malformed of
    # itself, only wrong for a field whose schema accepts a list alone.
    manifest = reports.parse_manifest("status: complete\ntests:\n  files: 2\n")

    assert manifest["tests"] == {"files": "2"}


def test_the_refusal_is_a_crew_error_so_it_lands_on_the_cli_surface() -> None:
    with pytest.raises(CrewError):
        reports.parse_manifest("status: complete\nchanged_paths:\n  files: 2\n")