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

import ast
import importlib
import json
import shlex
from pathlib import Path

import pytest
import yaml
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
    (
        "queue-edit",
        [
            "queue",
            "edit",
            "--run",
            "r-queued",
            "--brief",
            "@brief@",
            "--done-when",
            "pytest reports 2 passed.",
            "--write-path",
            "reckon/mcp.py",
            "--time-budget",
            "30m",
            "--backend",
            "codex",
            "--model",
            "sol",
        ],
        {
            "run_id": "r-queued",
            "brief_path": "@brief@",
            "done_when": "pytest reports 2 passed.",
            "write_paths": ["reckon/mcp.py"],
            "time_budget": "30m",
            "backend": "codex",
            "model": "sol",
        },
    ),
    (
        "queue-reprioritise",
        ["queue", "reprioritise", "--run", "r-queued", "--priority", "7"],
        {"run_id": "r-queued", "priority": 7},
    ),
    (
        "queue-drop",
        ["queue", "drop", "--run", "r-queued"],
        {"run_id": "r-queued"},
    ),
]


def _queued_request(home: Path) -> None:
    """Seed one valid waiting request for both sides of a parity case."""
    run_id = "r-queued"
    node = {
        "id": "queue-fixture",
        "goal": "Steer one request.",
        "plan": "sample-plan",
        "section": "§5",
        "brief": "",
        "brief_sha256": "",
        "brief_path": "",
        "role": "implement",
        "spec_level": "exact",
        "done_when": "pytest reports 1 passed.",
        "write_paths": ["reckon/crew/queue_edit.py"],
        "time_budget": "20m",
        "manifest_path": str(home.parent / "manifest.md"),
        "negative_control": "none: the node writes no check",
        "estimated_hours": 1.0,
        "requires_decisions": [],
        "peer_scopes": {},
        "regenerate_paths": [],
        "images": [],
        "inputs": [],
        "compares_with": "",
    }
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "plan": "sample-plan",
        "section": "§5",
        "repo": str(home.parent / "repo"),
        "session": "session-1",
        "node": node,
        "backend": "claude",
        "launch": "cli",
        "local": False,
        "phase": "queued",
        "queued_at": "2026-10-24T05:00:00+00:00",
        "created_at": "2026-10-24T05:00:00+00:00",
        "dispatch_options": {"locked_decisions": []},
    }
    (home / "crew" / "live" / f"{run_id}.json").write_text(
        json.dumps(pointer), encoding="utf-8"
    )


@pytest.mark.parametrize("action,argv,kwargs", CASES, ids=[c[0] for c in CASES])
def test_each_action_matches_its_cli_verb(
    config_home, monkeypatch, action, argv, kwargs
) -> None:
    """The tool action and its CLI verb answer one call with one document."""
    if action.startswith("queue-"):
        flight = {
            "default_backend": "claude",
            "local_backend": "claude",
            "backends": {
                "claude": {"launch": "cli", "lane": "claude", "model_key": "sol"},
                "codex": {"launch": "cli", "lane": "codex", "model_key": "luna"},
            },
            "lanes": {
                "claude": {
                    "default_model": "sol",
                    "models": {"sol": {"model": "claude-sol"}},
                },
                "codex": {
                    "default_model": "luna",
                    "models": {
                        "sol": {"model": "gpt-6-sol"},
                        "luna": {"model": "gpt-6-luna"},
                    },
                },
            },
        }
        catalogue = config_home.parent / "models.yaml"
        catalogue.write_text(
            yaml.safe_dump({"version": 1, "lanes": flight["lanes"], "aliases": {}}),
            encoding="utf-8",
        )
        monkeypatch.setenv("RECKON_MODEL_CATALOGUE", str(catalogue))
        monkeypatch.setattr(
            crew_dispatch_commands, "_resolved_flight", lambda *_a, **_k: flight
        )
        brief = config_home.parent / "replacement.md"
        brief.write_text("replacement steering text", encoding="utf-8")
        argv = [str(brief) if value == "@brief@" else value for value in argv]
        kwargs = {
            key: str(brief) if value == "@brief@" else value
            for key, value in kwargs.items()
        }
        _queued_request(config_home)
    cli = _cli_answer(argv)
    if action.startswith("queue-"):
        _queued_request(config_home)
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


def test_two_commits_reach_the_promotion_as_a_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two commits an action passes are the two its CLI form presents.

    The case is built on a run that exists in a temporary project, so the
    commits reach the promotion's own checks: a run that is not there refuses
    before any citation is read, and the same refusal would answer for two
    commits, for one and for none. The commits are deliberately identifiers
    that resolve to nothing, because that is what makes them visible — the
    boundary check reports each one it was presented, so the answer states
    which commits arrived.
    """
    run_id = "run-with-commits"
    _seed_a_run(tmp_path, monkeypatch, run_id)
    first, second = "1" * 40, "2" * 40
    arguments = [
        "complete",
        "--run",
        run_id,
        "--gate",
        "passed",
        "--outcome",
        "the run produced an answer",
        "--dry-run",
    ]
    common = {
        "action": "complete",
        "run_id": run_id,
        "gate": "passed",
        "outcome": "the run produced an answer",
        "dry_run": True,
    }

    both = mcp_module._crew(**common, commits=[first, second])
    cli_both = _cli_answer([*arguments, "--commit", first, "--commit", second])

    assert both == cli_both, (
        "two commits reached the promotion differently through the two surfaces\n"
        f"  cli : {cli_both}\n"
        f"  tool: {both}"
    )
    presented = " ".join(str(refusal) for refusal in both.get("refusals") or [])
    assert first in presented and second in presented, (
        "the answer did not present both commits the action passed: "
        f"{both.get('refusals')}"
    )

    one = mcp_module._crew(**common, commits=[first])
    none = mcp_module._crew(**common)

    assert one != both, "one commit and two commits answered identically"
    assert second not in " ".join(str(r) for r in one.get("refusals") or []), (
        "the second commit appeared in an answer that was not given it"
    )
    assert none != both, "no commits and two commits answered identically"


def test_action_form_renders_values_as_literals() -> None:
    """A value carrying quotes or a backslash still renders a runnable call."""
    from reckon.crew import obligations as obligations_module

    for value in ('say "hi" now', "it's done", "a\\b", "tab\there"):
        form = obligations_module.action_form(
            "reckon crew complete --run run-x --gate not-run "
            f"--outcome {shlex.quote(value)}"
        )
        assert form is not None
        parsed = ast.parse(form, mode="eval")
        assert isinstance(parsed.body, ast.Call)
        passed = {
            keyword.arg: keyword.value.value
            for keyword in parsed.body.keywords
            if isinstance(keyword.value, ast.Constant)
        }
        assert passed["outcome"] == value, (
            f"{value!r} did not survive the render: {form}"
        )


def test_a_repeated_option_of_values_with_quotes_still_parses() -> None:
    """A list-valued parameter escapes each value it carries."""
    from reckon.crew import obligations as obligations_module

    first, second = 'a"b.py', "c\\d.py"
    form = obligations_module.action_form(
        "reckon crew widen --run run-x "
        f"--write-path {shlex.quote(first)} --write-path {shlex.quote(second)}"
    )

    assert form is not None
    parsed = ast.parse(form, mode="eval")
    assert isinstance(parsed.body, ast.Call)
    paths = next(
        keyword.value
        for keyword in parsed.body.keywords
        if keyword.arg == "write_paths"
    )
    assert isinstance(paths, ast.List)
    assert [element.value for element in paths.elts] == [first, second]


def _seed_a_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_id: str) -> Path:
    """One live run in a temporary project, with a repository behind it.

    The pointer names a repository the promotion can resolve commits against,
    so a commit the action presents is judged rather than dropped for want of
    a tree.
    """
    from reckon.crew import runs
    from tests.helpers.git import git as _git

    home = tmp_path / "config"
    (home / "crew" / "live").mkdir(parents=True)
    monkeypatch.setenv("RECKON_HOME", str(home))
    repo = tmp_path / "repo"
    (repo / "docs" / "state" / PROJECT).mkdir(parents=True)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed a promotion fixture"),
    ):
        _git(repo, *arguments)
    base = _git(repo, "rev-parse", "HEAD")
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    runs._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": "session-commits",
            "process_alive": False,
            "worktree": str(repo),
            "base_sha": base,
            "node": {
                "id": "node-commits",
                "plan": "fixture-plan",
                "section": "fixture-section",
                "time_budget": "20m",
                "write_paths": ["seed.txt"],
            },
        },
    )
    return repo
