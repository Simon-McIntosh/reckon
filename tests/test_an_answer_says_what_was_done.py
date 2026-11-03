"""An acted answer to a finding says what the fix changed.

An answer of ``acted`` and nothing else records that something was done and not
what, so the next reviewer re-derives the fix from the diff and cannot tell a
defect the fix introduced from one the first round missed. The note is required
on the CLI and on the MCP answer action, it is stored on the finding's response
where the next brief reads it, and every skill example that shows an acted
answer shows the note with it.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from reckon import cli
from reckon.crew import plan_review
from reckon.mcp_crew_actions import crew_action
from tests.test_review_plan_command import project as project  # noqa: PLC0414

FINDING = "reuse-owner"
NOTE = "The ownership paragraph now names the core as the existing owner."

SKILLS = Path(__file__).resolve().parents[1] / "skills"

#: The skills whose examples this case holds: the three that describe answering
#: a plan-review finding while building, editing and reviewing a plan.
ANSWER_SKILLS = ("reckon-build", "reckon-create", "reckon-edit", "reckon-review")


def _store_review() -> None:
    """Store one design review carrying a finding that can be answered."""
    plan_review.store_plan_review(
        {
            "project": "sample",
            "plan_slug": "fixture",
            "plan_version": 3,
            "reviewed_blob_sha": "a" * 40,
            "rubric": "design",
            "findings": [
                {"id": FINDING, "type": "reuse_search", "text": "Name the owner."}
            ],
            "responses": {},
        }
    )


def _answer(*arguments: str) -> tuple[int, str]:
    result = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "review",
            "--project",
            "sample",
            "--plan",
            "fixture",
            "--answer",
            FINDING,
            *arguments,
        ],
    )
    return result.exit_code, result.output


def test_an_acted_answer_without_a_note_is_refused(project) -> None:
    """``--acted`` alone is not an answer: the refusal names the missing flag."""
    _store_review()

    code, output = _answer("--acted")

    assert code != 0, output
    assert "--note" in output, f"the refusal did not name --note:\n{output}"
    record = plan_review.read_plan_review("sample", "fixture")
    assert record["responses"] == {}, "a refused answer still wrote a response"


def test_an_acted_answer_stores_its_note(project) -> None:
    """The note lands on the response, where the next brief reads it."""
    _store_review()

    code, output = _answer("--acted", "--note", NOTE)

    assert code == 0, output
    record = plan_review.read_plan_review("sample", "fixture")
    assert record["responses"][FINDING]["action"] == "acted"
    finding = next(item for item in record["findings"] if item["id"] == FINDING)
    assert plan_review._stored_response(record, finding)["note"] == NOTE


def test_the_mcp_answer_action_stores_the_same_note(project) -> None:
    """The tool surface answers with the note the CLI surface answers with."""
    _store_review()

    answer = crew_action(
        "answer",
        project="sample",
        plan="fixture",
        finding=FINDING,
        acted=True,
        note=NOTE,
        session="coordinator",
    )

    record = plan_review.read_plan_review("sample", "fixture")
    assert record["responses"][FINDING]["action"] == "acted", json.dumps(answer)
    assert record["responses"][FINDING]["note"] == NOTE


def test_the_mcp_answer_action_refuses_an_acted_answer_without_a_note(project) -> None:
    """The tool surface refuses the same answer the CLI surface refuses."""
    _store_review()

    answer = crew_action(
        "answer",
        project="sample",
        plan="fixture",
        finding=FINDING,
        acted=True,
        session="coordinator",
    )

    assert answer.get("error"), json.dumps(answer)
    assert "--note" in json.dumps(answer)
    assert plan_review.read_plan_review("sample", "fixture")["responses"] == {}


def _skill_example_lines() -> list[tuple[Path, int, str]]:
    """Every skill line that shows an answer to a finding as acted."""
    lines = []
    paths = [
        path
        for name in ANSWER_SKILLS
        for path in sorted((SKILLS / name).glob("**/*.md"))
    ]
    for path in paths:
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            shows_cli_answer = "--answer" in line and "--acted" in line
            shows_tool_answer = "acted=true" in line
            if shows_cli_answer or shows_tool_answer:
                lines.append((path, number, line))
    return lines


def test_every_skill_example_of_an_acted_answer_shows_the_note() -> None:
    """A skill example that answers ``--acted`` teaches the answer that answers.

    The examples are what an agent copies, so an example without the note
    teaches an answer the command now refuses.
    """
    lines = _skill_example_lines()

    assert lines, "the skill scan found no acted answer to check"
    missing = [
        f"{path.relative_to(SKILLS.parent)}:{number}: {line.strip()}"
        for path, number, line in lines
        if "--note" not in line and "note=" not in line
    ]
    assert not missing, "an acted answer example names no note:\n" + "\n".join(missing)
