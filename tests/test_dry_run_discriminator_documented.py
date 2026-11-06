"""The dry-run discriminator sits beside ``--dry-run`` in the dispatch help.

A check answers a narrower question than the plan section records, and it did
so affirmatively: ``--dry-run`` returned ``ok`` for a dispatch that could not
run, and the green was written down as a rule. The remedy is not more
validation but the question itself, so the next person adding a check to the
dispatch surface meets it where they are working rather than in a plan they
would have to know to look for.

The declared negative control removes the recorded sentence from the
``--dry-run`` help, and this file must then turn red: the help would carry the
old "validate and resolve only" text with no question beside it.

The check reads the option's own block in the rendered help, not the whole
output, so a question text parked somewhere unrelated in the same help does
not satisfy it; whitespace is normalised first because click wraps the text to
the terminal width.
"""

from __future__ import annotations

import re

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module

DISCRIMINATOR = (
    "what would make this field wrong while it still printed the same value?"
)
DECLARED_MUTATION = (
    "Remove the sentence asking what would make the field wrong while it still "
    "printed the same value from the --dry-run help of reckon crew dispatch."
)


def _dispatch_help() -> str:
    result = CliRunner().invoke(cli_module.main, ["crew", "dispatch", "--help"])
    assert result.exit_code == 0, result.output
    return result.output


def _dry_run_block(help_text: str) -> str:
    """Return the wrapped help text belonging to the ``--dry-run`` option."""
    lines = help_text.splitlines()
    starts = [
        index for index, line in enumerate(lines) if re.match(r"^\s+--dry-run\b", line)
    ]
    assert len(starts) == 1, (
        f"expected one --dry-run entry in the dispatch help, found {len(starts)}"
    )
    block = [lines[starts[0]]]
    for line in lines[starts[0] + 1 :]:
        if re.match(r"^\s+--\S", line) or not line.strip():
            break
        block.append(line)
    return " ".join(" ".join(block).split())


def test_the_dry_run_help_asks_the_discriminator_question() -> None:
    block = _dry_run_block(_dispatch_help())
    assert DISCRIMINATOR in block.lower(), (
        "the --dry-run help does not record the discriminator question "
        f"{DISCRIMINATOR!r}; it reads: {block!r}"
    )


def test_the_question_names_the_green_preview_it_disarms() -> None:
    """The question is useless unless it says what a green preview stands in for."""
    block = _dry_run_block(_dispatch_help()).lower()
    assert "green preview" in block, block
    assert "true statement standing in for the one the caller is asking" in block, block


def test_the_dry_run_help_still_states_what_it_skips() -> None:
    """The recorded question is added beside the promise, never in place of it."""
    block = _dry_run_block(_dispatch_help()).lower()
    assert "no worktree, no process, no record" in block, block


if __name__ == "__main__":  # pragma: no cover - names the red-log recipe
    import sys

    sys.stdout.write(
        f"{DECLARED_MUTATION}\n"
        "then run: PYTHONPATH=. .venv/bin/python -m pytest -p no:cacheprovider "
        "-q tests/test_dry_run_discriminator_documented.py\n"
    )
    raise SystemExit(pytest.main([__file__]))
