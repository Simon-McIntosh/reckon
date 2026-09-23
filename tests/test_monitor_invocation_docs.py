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

The harness reference is the other place a reader copies an arming line from,
and its examples turn on two capabilities of the host: the entry point is
absolute, because the monitor's shell inherits no usable ``PATH``, and the
lifetime is bounded under the host's thirty-minute monitor cap, so the
follower's own final line arrives before the host's expiry notice does.
"""

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
CREW_AGENTS = ROOT / "reckon" / "crew" / "AGENTS.md"
HARNESS = (
    ROOT
    / "skills"
    / "reckon-ship"
    / "references"
    / "orchestrator-harness"
    / "claude-code.md"
)


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


def _harness_examples() -> list[str]:
    """Every arming example's command, its quoted parts rejoined."""
    text = HARNESS.read_text()
    blocks = re.findall(r"command:\s*(.*?),\n", text, re.DOTALL)
    return ["".join(re.findall(r"'([^']*)'", b)) for b in blocks]


def test_every_arming_examples_arm_an_absolute_entry_point() -> None:
    """A bare `reckon` in the monitor's shell can exit 127 before any line."""
    text = HARNESS.read_text()
    examples = _harness_examples()
    assert examples, "the reference arms a monitor with no example"
    assert len(examples) == text.count("Monitor({"), (
        "an example was added or reworded past this parser: "
        f"{len(examples)} parsed of {text.count('Monitor({')}"
    )
    for command in examples:
        binary = command.split()[0]
        assert binary.startswith("/"), command
        assert binary.endswith("reckon"), command
        assert "crew follow" in command, command
        assert "--lifetime 29m" in command, command


def test_the_reference_never_arms_a_persistent_monitor() -> None:
    """The tool dropped the parameter, so the reference must not teach it."""
    assert "persistent" not in HARNESS.read_text()


def test_the_reference_states_the_cap_the_example_passes() -> None:
    """The cap is a figure of the host, and the example spends the whole of it."""
    text = HARNESS.read_text()
    assert "1,800,000 milliseconds" in text
    assert "timeout_ms: 1800000" in text


def test_the_reference_rearms_on_the_followers_final_line() -> None:
    """The follower ends before the host, so its last line is the trigger."""
    words = " ".join(HARNESS.read_text().split())
    assert "its final line is the one to re-arm on" in words
    assert "follower's end" in words


def test_ansi_colour_reaches_an_agent_as_escape_sequences() -> None:
    """Hue is decoration in a notification; the state word carries itself."""
    words = " ".join(HARNESS.read_text().split())
    assert "escape sequences" in words
    assert "state word must carry the state alone" in words
