"""The copyable monitor invocation must not teach a session to turn colour off.

The pane is a pipe, so the conventional terminal probe would disable colour in
exactly the place it is wanted; colour is stated rather than detected, and the
arming line stays bare. Until the split these tests pin, the Watch and Follow
rows of ``reckon/crew/AGENTS.md`` presented their full option list as one
bracketed form, so a reader pasting the canonical line pasted ``--no-color``
with it — and three sessions relaunched their monitors that way within one
session, turning the colour back off. The copyable line now carries only the
arguments a caller must supply; every option is documented separately, and
``--no-color`` keeps its one legitimate reader: a sink that cannot render
colour.
"""

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
CREW_AGENTS = ROOT / "reckon" / "crew" / "AGENTS.md"


def _row(operation: str) -> str:
    text = CREW_AGENTS.read_text()
    for line in text.splitlines():
        if line.startswith(f"| {operation} |"):
            return line
    raise AssertionError(f"no table row for {operation!r} in reckon/crew/AGENTS.md")


def _command_cell(row: str) -> str:
    # A pipe escaped as \| is a literal table character, not a column boundary.
    return re.split(r"(?<!\\)\|", row)[2]


def _copyable(cell: str) -> str:
    match = re.search(r"`([^`]+)`", cell)
    if match is None:
        raise AssertionError(f"command cell carries no code span to paste: {cell!r}")
    return match.group(1)


# The bare command a caller must supply, per row. Every option belongs in the
# separately documented options list, never in the line a reader will paste.
EXPECTED_BARE = {
    "Watch": "reckon crew watch --project <project>",
    "Follow": "reckon crew follow --project <project>",
}


def test_watch_and_follow_copyable_line_is_the_bare_command() -> None:
    """The pasted line omits every option, `--no-color` included."""
    for operation, bare in EXPECTED_BARE.items():
        copyable = _copyable(_command_cell(_row(operation)))
        assert copyable == bare, (
            f"the {operation} row's copyable line is {copyable!r}; "
            f"expected the bare {bare!r} with options documented separately"
        )
        assert "--no-color" not in copyable, (
            f"the {operation} row's copyable line embeds --no-color: {copyable!r}"
        )


def test_every_documented_option_survives_outside_the_pasted_line() -> None:
    """Splitting line from options removed nothing from the documentation."""
    options = {
        "Watch": ("--stall-window", "--width", "--theme", "--no-color"),
        "Follow": ("--session", "--run", "--json", "--width", "--theme", "--no-color"),
    }
    for operation, flags in options.items():
        cell = _command_cell(_row(operation))
        for flag in flags:
            assert flag in cell, f"{operation} row dropped option {flag!r}"


def test_no_color_is_an_optout_for_a_sink_that_cannot_render_colour() -> None:
    """--no-color survives as an opt-out, named for its actual reader."""
    for operation in ("Watch", "Follow"):
        cell = _command_cell(_row(operation))
        idx = cell.find("--no-color")
        assert idx != -1, f"{operation} row no longer documents --no-color"
        assert "cannot render colour" in cell[idx : idx + 120], (
            f"{operation} row documents --no-color without saying it is for a "
            "sink that cannot render colour"
        )


def test_the_prose_and_the_presented_form_agree() -> None:
    """The row's own reasoning still explains why the pasted line stays bare."""
    follow = _row("Follow")
    assert "so both are stated" in follow
    assert "the arming line stays bare" in follow
    # The line above the can-be-detected claim must not contradict it: the
    # bare arming line is exactly the bare copyable line asserted elsewhere.
    assert _copyable(_command_cell(follow)) == EXPECTED_BARE["Follow"]
