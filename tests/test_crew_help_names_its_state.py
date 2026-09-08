"""Crew help identifies the state or condition each reachable verb applies to."""

from __future__ import annotations

import re
import shlex

from click import Command, Group
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon.crew.refusals import DISPATCH_REFUSAL_REMEDIES

CommandPath = tuple[str, ...]

STATE_PHRASES: dict[CommandPath, str] = {
    ("attach",): "prepared in-harness run",
    ("complete",): "finished run",
    ("directory",): "live coordinator ownership",
    ("discard",): "non-running live pointer",
    ("dispatch",): "contract, routing, budget, watcher, and scope",
    ("drain",): "session-closure count",
    ("follow",): "one session's live runs",
    ("gc",): "integrated state makes them disposable",
    ("ledger",): "committed run records",
    ("list",): "live run pointers",
    ("member", "add"): "missing roster member",
    ("member", "list"): "roster members and reusable sessions",
    ("observe",): "live run record",
    ("preflight",): "backend budget state",
    ("recover",): "live pointers left by an interrupted orchestrator",
    ("redispatch",): "working run",
    ("repair-completion",): "historical completion measurements missing",
    ("resume",): "one blocked run with advice",
    ("resume-ready",): "provider hold or declared external wait has ended",
    ("shadow",): "committed run",
    ("stop",): "running spawned worker",
    ("unwatch",): "registered project watcher",
    ("watch",): "project-wide live runs",
}

NEAR_NAMESAKE_DISTINCTIONS: dict[CommandPath, str] = {
    ("list",): "not roster members",
    ("member", "list"): "not live run pointers",
    ("resume",): "do not sweep all newly ready runs",
    ("resume-ready",): "answer none with advice",
}

POINTER_STATE_READS: dict[CommandPath, str] = {
    ("list",): "List live run pointers",
    ("drain",): "Report the session-closure count",
    ("recover",): "Classify live pointers",
}

_REMEDY_COMMAND = re.compile(r"`reckon crew ([^`]+)`")


def _leaf_commands(
    group: Group, prefix: CommandPath = ()
) -> dict[CommandPath, Command]:
    leaves: dict[CommandPath, Command] = {}
    for name, command in group.commands.items():
        path = (*prefix, name)
        if isinstance(command, Group):
            leaves.update(_leaf_commands(command, path))
        else:
            leaves[path] = command
    return leaves


def _first_help_line(command: Command) -> str:
    return (command.help or command.short_help or "").strip().splitlines()[0]


def _help_result(path: CommandPath):
    return CliRunner().invoke(cli_module.main, ["crew", *path, "--help"])


def _registered_remedy_paths(leaves: dict[CommandPath, Command]) -> set[CommandPath]:
    paths: set[CommandPath] = set()
    for remedy in DISPATCH_REFUSAL_REMEDIES.values():
        if remedy is None:
            continue
        for rendered_command in _REMEDY_COMMAND.findall(remedy):
            tokens = shlex.split(rendered_command)
            group: Group = cli_module.crew
            path: list[str] = []
            for token in tokens:
                if token.startswith(("-", "<")):
                    break
                command = group.commands.get(token)
                if command is None:
                    break
                path.append(token)
                if isinstance(command, Group):
                    group = command
                else:
                    break
            command_path = tuple(path)
            assert command_path in leaves, rendered_command
            paths.add(command_path)
    return paths


def test_every_crew_verb_help_names_its_state_or_condition() -> None:
    leaves = _leaf_commands(cli_module.crew)

    assert set(leaves) == set(STATE_PHRASES)
    assert len(leaves) == 23
    for path, command in leaves.items():
        first_line = _first_help_line(command)
        result = _help_result(path)
        assert result.exit_code == 0, result.output
        assert " ".join(first_line.split()) in " ".join(result.output.split())
        assert STATE_PHRASES[path] in first_line


def test_the_four_one_word_near_namesakes_state_their_distinction() -> None:
    leaves = _leaf_commands(cli_module.crew)

    assert len(NEAR_NAMESAKE_DISTINCTIONS) == 4
    for path, distinction in NEAR_NAMESAKE_DISTINCTIONS.items():
        assert distinction in _first_help_line(leaves[path])


def test_pointer_list_drain_and_recovery_name_three_different_answers() -> None:
    leaves = _leaf_commands(cli_module.crew)

    for path, answer in POINTER_STATE_READS.items():
        assert answer in _first_help_line(leaves[path])


def test_every_refusal_remedy_names_a_reachable_state_specific_verb() -> None:
    leaves = _leaf_commands(cli_module.crew)
    remedy_paths = _registered_remedy_paths(leaves)

    assert remedy_paths
    for path in remedy_paths:
        result = _help_result(path)
        assert result.exit_code == 0, result.output
        assert STATE_PHRASES[path] in _first_help_line(leaves[path])
