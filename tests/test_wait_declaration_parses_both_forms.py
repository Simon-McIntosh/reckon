"""A waiting manifest's terminal-state list parses like the probe it runs.

A worker may write wait_terminal in either of the two forms the probe reader
accepts -- a list, or a JSON array written as a string -- and both must
decode to the same states. Comma-splitting a JSON array is what produced
state names carrying a bracket and a quote character, so a value that opens
with a square bracket is read as JSON only and never falls back to
comma-splitting; a value that cannot be read is an incomplete declaration,
not a silently honoured one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon.crew import recovery

# Characters that must never appear in a decoded state name: stripping the
# survivours of a comma-split array is exactly what the bug produced.
_BRACKET_PUNCTUATION = {"[", "]", '"', "'", "\\"}


def _assert_clean(states: list[str]) -> None:
    assert not any(
        character in state for state in states for character in _BRACKET_PUNCTUATION
    )


@pytest.mark.parametrize(
    "value",
    [
        ["COMPLETED", "FAILED"],
        '["COMPLETED", "FAILED"]',
        '  ["COMPLETED", "FAILED"]  ',
    ],
)
def test_a_json_array_of_states_parses_to_exactly_those_states(value) -> None:
    states = recovery._wait_terminal_values(value)
    assert states == ["COMPLETED", "FAILED"]
    _assert_clean(states)


@pytest.mark.parametrize(
    "value",
    [
        ["COMPLETED", "FAILED"],
        '["COMPLETED", "FAILED"]',
    ],
)
def test_the_probe_reader_and_the_terminal_reader_agree_on_one_input(value) -> None:
    assert (
        recovery._wait_probe(value)
        == recovery._wait_terminal_values(value)
        == ["COMPLETED", "FAILED"]
    )


def test_comma_separated_terminal_parses_with_spaces_around_the_commas() -> None:
    assert recovery._wait_terminal_values("COMPLETED, FAILED, CANCELLED") == [
        "COMPLETED",
        "FAILED",
        "CANCELLED",
    ]


def test_a_single_bare_state_with_no_comma_and_no_bracket_parses_to_one_state() -> None:
    assert recovery._wait_terminal_values("COMPLETED") == ["COMPLETED"]


@pytest.mark.parametrize(
    "malformed",
    [
        "[COMPLETED, FAILED]",  # unquoted identifiers are not JSON
        '["COMPLETED"',  # unbalanced
        "[",  # truncated
    ],
)
def test_a_bracket_value_that_does_not_parse_as_json_yields_no_states(
    malformed: str,
) -> None:
    assert recovery._wait_terminal_values(malformed) == []
    # The probe reader rejects the same malformed array, so the two readers
    # stay symmetric on the failure side as well as the success side.
    assert recovery._wait_probe(malformed) == []


def test_a_bracket_value_that_does_not_parse_as_json_reports_the_declaration_incomplete(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "manifest.md"
    manifest.write_text("status: waiting\n", encoding="utf-8")
    declaration = recovery._manifest_wait(
        {
            "status": "waiting",
            "wait_condition": "cluster job 7788",
            "wait_probe": ["scheduler-status", "--job", "7788"],
            "wait_terminal": "[COMPLETED, FAILED]",
            "resume_brief": "inspect the job result and finish",
        },
        manifest,
        now_seconds=1_700_000_000.0,
        stale_after_seconds=3600,
    )
    assert declaration is not None
    assert declaration["valid"] is False
    assert "wait_terminal" in declaration["error"]


def test_a_comma_inside_a_json_array_element_stays_inside_that_element() -> None:
    states = recovery._wait_terminal_values('["COMPLETED, WITH HOLD", "FAILED"]')
    assert states == ["COMPLETED, WITH HOLD", "FAILED"]
    assert recovery._wait_probe('["COMPLETED, WITH HOLD", "FAILED"]') == states
