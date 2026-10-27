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
    ("ack",): "one run's obligations",
    ("admit",): "queued dispatches while the lane grants a slot",
    ("attach",): "prepared in-harness run",
    (
        "budget",
    ): "one budget group's pace, its banked reset, its lifts or backend holds",
    ("check",): "delivered manifest or run review",
    ("complete",): "finished run",
    ("directory",): "live coordinator ownership",
    ("discard",): "non-running live pointer",
    ("dispose",): "one sub-floor review dimension",
    ("dispatch",): "contract, routing, budget, watcher, and scope",
    ("drain",): "session-closure count",
    ("follow",): "one session's live runs",
    ("gate",): "shared crew gate state",
    ("host",): "one live session's crew followers",
    ("gc",): "integrated state makes them disposable",
    ("member", "add"): "missing roster member",
    ("member", "list"): "roster members and reusable sessions",
    ("merge",): "a reviewed head on the primary branch",
    ("observe",): "live run record",
    ("path",): "one kind of file",
    ("pick",): "live routing state",
    ("placement",): "one reservation a project places its workers into",
    ("queue", "drop"): "queued or awaiting-upstream request",
    ("queue", "edit"): "waiting request's steering text",
    ("queue", "hold"): "Pause one waiting request",
    ("queue", "release"): "Restore one held request",
    ("queue", "reprioritise"): "priority override on a waiting request",
    ("recover",): "live pointers",
    ("repair-completion",): "historical completion measurements missing",
    ("repair",): "a manifest's status word or a review's status",
    ("resume",): "one stopped run, every ready run",
    ("retention-sweep",): "run directories past their retention window",
    ("review",): "one plan's content, a code neighbourhood or a portfolio",
    ("shadow",): "committed run",
    # `split-runs` is a registered leaf of `crew`, so the census of reachable
    # verbs owes it a row like any other. It acts on the aggregate run rows a
    # mounted project's ledger still holds, which is the state its phrase names.
    ("split-runs",): "aggregate run rows",
    ("station",): "single node-local station",
    ("stop",): "running spawned worker",
    ("supersede-review",): "a misattributed row's review",
    ("suite", "run"): "the project's declared suite",
    ("suite", "waive"): "standing-suite hold",
    ("verify-gate",): "one run's gate at the integrated revision",
    ("view",): "one crew read view",
    ("watch",): "project-wide live runs",
    ("widen",): "run whose worker process has stopped",
}

NEAR_NAMESAKE_DISTINCTIONS: dict[CommandPath, str] = {
    ("member", "list"): "not live run pointers",
}

POINTER_STATE_READS: dict[CommandPath, str] = {
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
    for path, command in leaves.items():
        first_line = _first_help_line(command)
        result = _help_result(path)
        assert result.exit_code == 0, result.output
        assert " ".join(first_line.split()) in " ".join(result.output.split())
        assert STATE_PHRASES[path] in first_line


def test_the_retired_review_plan_alias_is_not_reachable() -> None:
    """``review`` is the one verb that composes a plan review.

    The retired alias must not answer: a session reaching for it is routed to
    the verb that replaced it rather than composing a second, divergent review.
    """
    assert "review-plan" not in cli_module.crew.commands
    result = _help_result(("review-plan",))
    assert result.exit_code != 0, result.output


def test_the_one_word_near_namesake_states_its_distinction() -> None:
    """A one-word verb that reads like another names the other it is not.

    The retired listing was one of the pair, so the surviving row is what keeps
    this a check rather than a nothing: the roster listing still has to say it
    is not the live run pointers the listing read.
    """
    leaves = _leaf_commands(cli_module.crew)

    assert len(NEAR_NAMESAKE_DISTINCTIONS) == 1
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
