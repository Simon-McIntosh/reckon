"""The crew MCP tool carries the coordinator's eight write verbs.

Each of ``complete``, ``land``, ``dispose``, ``ack``, ``answer``, ``widen``,
``stop`` and ``discard`` runs through the same command function its crew CLI
verb runs through, so the two surfaces answer one call identically. Parity is
held by driving both surfaces on the same temporary configuration home and
comparing the documents they return, rather than by asserting about either one.

A CLI refusal that reaches the caller as a bare ``Error:`` line carries no JSON
document, so the comparison normalises it to the ``ok``/``error``/``detail``
shape the tool returns: the message the CLI prints is the one the tool states.

The negative control moves one action's argument mapping so it supplies a
different value from its CLI counterpart; that one action's parity case fails
and no other, which is what proves the parity test bites.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

import reckon.mcp as mcp_module

# Importing the run-command module registers its verbs (dispose, widen, ack,
# stop, discard, complete) on the shared crew group; without it the CLI does not
# know them and answers "No such command".
crew_run_commands = importlib.import_module("reckon.crew_run_commands")
crew_dispatch_commands = importlib.import_module("reckon.crew_dispatch_commands")

CREW = crew_dispatch_commands.crew
PROJECT = "reckon"


@pytest.fixture()
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthetic, empty configuration home with one mounted project."""
    home = tmp_path / "config"
    (home / "crew" / "live").mkdir(parents=True)
    monkeypatch.setenv("RECKON_HOME", str(home))
    repo = tmp_path / "repo"
    (repo / "docs" / "state" / PROJECT).mkdir(parents=True)
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    return home


def _cli_answer(arguments: list[str]) -> dict:
    """The document the CLI prints, or its refusal normalised to tool shape."""
    result = CliRunner().invoke(CREW, arguments)
    text = result.output.strip()
    try:
        return json.loads(text)
    except ValueError:
        message = text
        message = message.removeprefix("Error: ")
        return {"ok": False, "error": "crew_error", "detail": message}


#: Each action, the CLI argument vector that exercises it, and the tool kwargs
#: that must reach the same implementation. The pairs name the same call in the
#: two surfaces' own spellings.
CASES = [
    (
        "complete",
        [
            "complete",
            "--run",
            "run-missing",
            "--gate",
            "not-run",
            "--dry-run",
        ],
        {"run_id": "run-missing", "gate": "not-run", "dry_run": True},
    ),
    (
        "land",
        [
            "complete",
            "--run",
            "run-missing",
            "--gate",
            "not-run",
            "--land",
            "--dry-run",
        ],
        {"run_id": "run-missing", "gate": "not-run", "dry_run": True},
    ),
    (
        "dispose",
        [
            "dispose",
            "--project",
            PROJECT,
            "--run",
            "run-missing",
            "--dimension",
            "rigour",
            "--kind",
            "exempt",
            "--reason",
            "not applicable",
        ],
        {
            "project": PROJECT,
            "run_id": "run-missing",
            "dimension": "rigour",
            "kind": "exempt",
            "reason": "not applicable",
        },
    ),
    (
        "ack",
        [
            "ack",
            "--run",
            "run-missing",
            "--reason",
            "deferred",
            "--until",
            "2030-01-01T00:00:00Z",
        ],
        {
            "run_id": "run-missing",
            "reason": "deferred",
            "until": "2030-01-01T00:00:00Z",
        },
    ),
    (
        "answer",
        [
            "review",
            "--project",
            PROJECT,
            "--plan",
            "some-plan",
            "--answer",
            "f1",
            "--acted",
            "--session",
            "session-1",
        ],
        {
            "project": PROJECT,
            "plan": "some-plan",
            "finding": "f1",
            "acted": True,
            "session": "session-1",
        },
    ),
    (
        "widen",
        ["widen", "--run", "run-missing", "--write-path", "reckon/new.py"],
        {"run_id": "run-missing", "write_paths": ["reckon/new.py"]},
    ),
    ("stop", ["stop", "--run", "run-missing"], {"run_id": "run-missing"}),
    ("discard", ["discard", "--run", "run-missing"], {"run_id": "run-missing"}),
]


@pytest.mark.parametrize("action,argv,kwargs", CASES, ids=[c[0] for c in CASES])
def test_each_action_matches_its_cli_verb(config_home, action, argv, kwargs) -> None:
    """The tool action and its CLI verb answer one call with one document."""
    cli = _cli_answer(argv)
    tool = mcp_module._crew(action=action, **kwargs)

    assert tool == cli, (
        f"{action}: the tool and the CLI disagree about one call\n"
        f"  cli : {cli}\n"
        f"  tool: {tool}"
    )


def test_a_refused_complete_carries_every_failing_guard(config_home) -> None:
    """A refused promotion states the whole refusal list, not one entry.

    The tool re-reads a refused ``complete`` through the promotion's own dry
    run, so a coordinator fixing a run by trial learns every way it falls short
    from one call.
    """
    answer = mcp_module._crew(action="complete", run_id="run-missing", gate="passed")

    assert answer.get("ok") is False
    assert answer.get("guards"), (
        "a refused complete through the tool carried no failing guard"
    )
    assert any("run-missing" in str(guard) for guard in answer["guards"]), (
        "the guard list did not name the run the refusal was about"
    )


def test_the_negative_control_breaks_one_action_only(
    config_home, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mis-mapped argument fails exactly the action that reads it.

    The declared mutation supplies ``ack``'s ``until`` argument for the verb's
    ``reason`` parameter, which changes the value the verb receives; its parity
    case must fail while every other action still matches.
    """
    from reckon import mcp_crew_actions

    broken = dict(mcp_crew_actions._PARAM_MAP["ack"])
    broken["until"] = "reason"
    monkeypatch.setitem(mcp_crew_actions._PARAM_MAP, "ack", broken)

    failures = []
    for action, argv, kwargs in CASES:
        cli = _cli_answer(argv)
        tool = mcp_module._crew(action=action, **kwargs)
        if tool != cli:
            failures.append(action)

    assert failures == ["ack"], (
        f"the negative control changed the answer for {failures}, not just ack"
    )


def test_the_obligations_view_prints_the_action_form(
    config_home, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A duty's printed command carries the tool's action form beside it.

    The form travels under its own key: ``next_action`` already means a printed
    command string on the classifier's rows, so overwriting it with a rendered
    call would hand a reader a different kind of value under one name.
    """
    from reckon.crew import obligations as obligations_module

    command = "reckon crew complete --run run-x --gate <verdict>"
    item = {
        "kind": "review-ready",
        "run_id": "run-x",
        "node": "node-x",
        "plan": "p",
        "age_seconds": 5,
        "next_command": command,
        "next_action": command,
    }
    monkeypatch.setattr(
        obligations_module,
        "obligations",
        lambda project, session: {
            "project": project,
            "session": session,
            "obligations": [obligations_module._with_action_form(item)],
            "acknowledged": [],
            "summary": {"count": 1, "oldest_age_seconds": 5, "unreconciled_runs": 0},
        },
    )

    view = mcp_module._crew(PROJECT, view="obligations", session="s")
    row = view["obligations"][0]

    assert row["next_action_form"] == (
        'crew(action="complete", run_id="run-x", gate="<verdict>")'
    )
    assert row["next_action"] == command, (
        "the rendered form displaced the printed command that key already carried"
    )


def test_action_form_leaves_other_commands_alone() -> None:
    from reckon.crew import obligations as obligations_module

    assert obligations_module.action_form("reckon crew dispatch --node n") is None
    assert obligations_module.action_form("not a command") is None
    assert obligations_module.action_form("reckon crew stop --run r") == (
        'crew(action="stop", run_id="r")'
    )


def test_a_commitless_promotion_keeps_its_waiver_and_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The commitless promotion renders with every option it was composed with.

    A run that changed nothing is offered a completion naming the commitless
    waiver and the outcome instead of a citation. A form that dropped either
    would offer a call the promotion refuses for the very reason the command
    was composed to answer.
    """
    from reckon.crew import obligations as obligations_module
    from reckon.crew import recovery_classification

    base = "a" * 40
    monkeypatch.setattr(recovery_classification, "_review_tree", lambda record: None)
    monkeypatch.setattr(
        recovery_classification, "_canonical_commits", lambda tree, commits: [base]
    )

    command = recovery_classification._promotion_command(
        "run-x", {"base_sha": base}, [base]
    )
    assert "--no-commit" in command and "--outcome" in command, (
        f"the commitless branch did not compose the command under test: {command}"
    )

    assert obligations_module.action_form(command) == (
        'crew(action="complete", run_id="run-x", gate="not-run", '
        'no_commit="<why the run produced no commit>", '
        'outcome="<what the run produced>")'
    )


def test_a_repeated_option_renders_as_one_list_parameter() -> None:
    """Two occurrences of an option become one list, not one parameter twice."""
    from reckon.crew import obligations as obligations_module

    form = obligations_module.action_form(
        "reckon crew complete --run run-x --gate passed "
        "--commit 1111111 --commit 2222222"
    )

    assert form == (
        'crew(action="complete", run_id="run-x", gate="passed", '
        'commits=["1111111", "2222222"])'
    )


def test_an_option_the_form_cannot_carry_renders_no_form() -> None:
    """A command carrying an unmapped option is left as a printed command.

    Rendering it without that option would offer a call that answers about a
    different request than the one the command states.
    """
    from reckon.crew import obligations as obligations_module

    assert (
        obligations_module.action_form(
            "reckon crew complete --run run-x --gate passed --land"
        )
        is None
    )
    assert obligations_module.action_form("reckon crew stop --run") is None


def test_two_commits_reach_the_verb_as_a_list(config_home) -> None:
    """The action passes a list of commits, the way the CLI passes two flags."""
    cli = _cli_answer(
        [
            "complete",
            "--run",
            "run-missing",
            "--gate",
            "not-run",
            "--commit",
            "1111111",
            "--commit",
            "2222222",
            "--dry-run",
        ]
    )
    tool = mcp_module._crew(
        action="complete",
        run_id="run-missing",
        gate="not-run",
        commits=["1111111", "2222222"],
        dry_run=True,
    )

    assert tool == cli, (
        "two commits reached the verb differently through the two surfaces\n"
        f"  cli : {cli}\n"
        f"  tool: {tool}"
    )
