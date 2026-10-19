"""A retired crew verb answers with the invocation that replaced it.

Four properties hold for every row of the tombstone registry: the retired name
exits with the code the crew group's help documents and prints its successor; it
never appears in ``reckon crew --help``; it carries the caller's arguments onto
its successor; and its successor resolves — a CLI successor in the live command
tree, an MCP successor among the views and actions the crew tool accepts. The
resolution check is exercised against a row that names an unregistered verb, so
the guard is shown to fire rather than merely to pass.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import click
from click.testing import CliRunner

from reckon import mcp
from reckon.cli import crew, main
from reckon.crew.tombstones import (
    TOMBSTONE_EXIT_CODE,
    TOMBSTONE_EXIT_HELP,
    TOMBSTONES,
    Tombstone,
)

UNREGISTERED = "__unregistered_fixture__"

ROOT = Path(__file__).resolve().parents[1]

#: A literal process exit code raised by a crew command.
_EXIT = re.compile(r"(?:click\.exceptions\.Exit|ctx\.exit|sys\.exit)\((\d+)\)")


def _crew_exit_codes() -> set[int]:
    """Every literal exit code the crew commands raise.

    The crew group's help promises a branchable code, so a code the tombstones
    answer with must belong to no crew command's own vocabulary. The set is read
    from the source the commands run from rather than from a list that would
    drift: ``reckon/crew_*.py`` and everything under ``reckon/crew/``.
    """
    codes: set[int] = set()
    for path in (*ROOT.glob("reckon/crew_*.py"), *ROOT.glob("reckon/crew/**/*.py")):
        for match in _EXIT.finditer(path.read_text(encoding="utf-8")):
            codes.add(int(match.group(1)))
    return codes


def _normalized(text: str) -> str:
    return " ".join(text.split())


def _crew_help() -> str:
    result = CliRunner().invoke(main, ["crew", "--help"])
    assert result.exit_code == 0, result.output
    return result.output


def _cli_resolves(argv: tuple[str, ...]) -> bool:
    node: click.Command = main
    for token in argv:
        subs = getattr(node, "commands", None)
        if not subs or token not in subs:
            return False
        node = subs[token]
    return True


def _mcp_successor_resolves(row: Tombstone) -> bool:
    """Ask the crew tool whether it accepts this successor, by using it.

    A view the tool does not accept answers ``invalid_view`` and an unknown
    action answers ``invalid_action``; any other answer — including a readable
    refusal about missing data — means the tool recognised the name.
    """
    if row.mcp_action is not None:
        answer = mcp._crew(
            action=row.mcp_action, project=UNREGISTERED, run_id=UNREGISTERED
        )
        return answer.get("error") != "invalid_action"
    answer = mcp._crew(project=UNREGISTERED, view=row.mcp_view, session=UNREGISTERED)
    return answer.get("error") != "invalid_view"


def _successor_problems(rows: tuple[Tombstone, ...]) -> list[str]:
    """One line per row whose successor does not resolve, naming the verb."""
    problems: list[str] = []
    for row in rows:
        if row.is_cli:
            if not _cli_resolves(row.argv):
                problems.append(
                    f"{row.retired}: successor {' '.join(row.argv)} names no command"
                )
        elif row.mcp_view or row.mcp_action:
            if not _mcp_successor_resolves(row):
                problems.append(f"{row.retired}: successor {row.successor} is refused")
        else:
            problems.append(f"{row.retired}: declares no successor")
    return problems


def test_each_row_exits_with_the_documented_code_and_prints_its_successor() -> None:
    assert str(TOMBSTONE_EXIT_CODE) not in {"0", "1", "2", "3", "4", "5", "6", "7", "8"}
    assert _normalized(TOMBSTONE_EXIT_HELP) in _normalized(_crew_help())

    runner = CliRunner()
    for row in TOMBSTONES:
        result = runner.invoke(main, ["crew", row.retired])
        assert result.exit_code == TOMBSTONE_EXIT_CODE, result.output
        payload = json.loads(result.output)
        assert payload["retired"] == row.retired
        assert payload["successor"] == row.successor
        assert payload["exit_code"] == TOMBSTONE_EXIT_CODE


def test_no_row_appears_in_the_crew_help() -> None:
    """A retired name is never listed as a command of the crew group.

    The check reads the group's own command listing rather than the whole help
    body: a retired word may appear inside an unrelated verb's description
    (``ack`` defers "one run's obligations"), which is prose, not a leak.
    """
    with click.Context(crew) as ctx:
        listed = set(crew.list_commands(ctx))
    for row in TOMBSTONES:
        assert row.retired not in crew.commands
        assert row.retired not in listed


def test_review_plan_carries_its_arguments_onto_the_review_invocation() -> None:
    """The retired ``review-plan`` answers with ``crew review``, in literals.

    The expectations are written here rather than read from the row, so the
    check fails if the registry and the command's own answer drift together.
    """
    result = CliRunner().invoke(
        main, ["crew", "review-plan", "--plan", "p", "--answer", "f", "--acted"]
    )
    assert result.exit_code == TOMBSTONE_EXIT_CODE, result.output
    payload = json.loads(result.output)
    assert payload["retired"] == "review-plan"
    assert payload["successor"] == "crew review"
    assert payload["invocation"] == "reckon crew review --plan p --answer f --acted"
    assert payload["exit_code"] == TOMBSTONE_EXIT_CODE


def test_the_retirement_code_is_raised_by_no_crew_command() -> None:
    """A caller may branch on the tombstone code, so no command may share it.

    ``reckon crew dispatch`` already exits 9 for member-in-flight and 75 is
    queue-full and lane-paused, so a tombstone answering 9 would be ambiguous
    with a refusal. The code the tombstones use must belong to no crew command.
    """
    codes = _crew_exit_codes()
    assert codes, "read no exit codes from the crew sources; the scan is broken"
    assert TOMBSTONE_EXIT_CODE > 9, TOMBSTONE_EXIT_CODE
    assert TOMBSTONE_EXIT_CODE not in codes, (
        f"exit code {TOMBSTONE_EXIT_CODE} is also raised by a crew command: "
        f"{sorted(codes)}"
    )


def test_every_rows_successor_resolves() -> None:
    problems = _successor_problems(TOMBSTONES)
    assert not problems, "\n".join(problems)


def test_a_successor_naming_an_unregistered_verb_is_reported() -> None:
    """The guard fires on a row whose CLI successor is not a live command."""
    bogus = Tombstone(
        retired="ghost",
        successor="crew no-such-verb",
        argv=("crew", "no-such-verb"),
    )
    problems = _successor_problems((bogus,))
    assert problems, "an unregistered successor resolved as if it were a command"
    assert "no-such-verb" in problems[0]
