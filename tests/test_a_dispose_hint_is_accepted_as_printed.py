"""Every dispose hint an obligation prints is accepted by the verb as printed.

An obligation's dispose hint is composed from the stored review record, and a
review record names its family: a code review's record carries the health
rubric, and a figure review's carries the figure rubric. The dispose verb
selects a figure record by ``--rubric figure`` and answers the run's text
review when no rubric is named, so a hint carrying any other family is refused
by the verb a coordinator is told to paste. This module reads the hint an
obligation prints for a code review and for a figure review, parses each one
with the verb's own argument parser, and runs it through the verb: the refusal
that surfaces is the store's absence, never the rubric.

Every crew directory resolves inside a temporary configuration home, so the
operator's own stores are never read or written.
"""

from __future__ import annotations

import importlib
import re
import shlex

import click
import pytest
from click.testing import CliRunner

from reckon.cli import main

obligations = importlib.import_module("reckon.crew.obligations")

PROJECT = "dispose-hint-fixture"
RUN = "r-dispose-hint-fixture"
SESSION = "coordinator-fixture"
REVIEW_RUN = "r-20261122T120000000000-review-run"

# The rubric each stored record family carries. A code review's record states
# the health rubric; a figure review's states its own family.
CODE_REVIEW_RUBRIC = "health"
FIGURE_REVIEW_RUBRIC = "figure"


def _parse(command: str):
    """Parse one printed command with the dispose verb's own argument parser."""
    tokens = shlex.split(command)
    assert tokens.pop(0) == "reckon"
    group = main
    context = None
    while isinstance(group, click.Group):
        context = group.make_context(group.name, tokens, parent=context)
        name, group, tokens = group.resolve_command(
            context, context._protected_args + context.args
        )
    return group.make_context(name, tokens, parent=context)


def _hint(monkeypatch, rubric: str) -> list[str]:
    """The dispose commands one overdue obligation prints for this record family."""
    monkeypatch.setattr(obligations, "_reflex_review_window_seconds", lambda: 0)
    monkeypatch.setattr(
        obligations.recovery_review_dispatch,
        "review_dispatch_refusal",
        lambda record: {
            "refused": True,
            "reason": "an open blocking finding needs a disposition",
            "undisposed_findings": [0],
            "review_rubric": rubric,
            "review_run_id": REVIEW_RUN,
        },
    )
    pointer = {"run_id": RUN, "project": PROJECT, "session": SESSION}
    duty = obligations._reflex_open_refusal(PROJECT, pointer, age=1)
    assert duty is not None
    commands = re.findall(r"`(reckon[^`]+)`", duty["next_command"])
    assert len(commands) == 2, duty["next_command"]
    return commands


@pytest.mark.parametrize(
    ("rubric", "expected"),
    [(CODE_REVIEW_RUBRIC, None), (FIGURE_REVIEW_RUBRIC, FIGURE_REVIEW_RUBRIC)],
    ids=["code-review", "figure-review"],
)
def test_every_dispose_hint_is_accepted_as_printed(
    monkeypatch, isolated_reckon_home, rubric, expected
):
    commands = _hint(monkeypatch, rubric)
    for command in commands:
        _parse(command)
        # The verb's own callback, not just its parser, decides acceptance: an
        # unknown family is refused before the store is read. The fixture home
        # holds no review for this run, so the only error left is that absence.
        result = CliRunner().invoke(main, shlex.split(command)[1:])
        assert "unknown review rubric" not in result.output, result.output
        assert result.exit_code != 2, result.output
        parsed = _parse(command)
        assert parsed.params["review_rubric"] == expected, command