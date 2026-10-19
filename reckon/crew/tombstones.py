"""Retired crew verbs name the invocation that replaced them.

A coordinator's session carries skill text loaded before a verb is folded away,
so a name that no longer resolves keeps being reached for long after the change
(the retired ``crew review-plan`` alias was called hundreds of times after it was
folded into ``crew review``). A tombstone answers that call in one turn: it
writes nothing, prints one JSON document naming the retired verb, its successor
and the successor's invocation with the caller's arguments carried across, and
exits with :data:`TOMBSTONE_EXIT_CODE` — one code the crew group's help
documents beside codes 0 to 8, so a caller branching on success can branch on a
retirement too.

A row's successor is one of two kinds:

* a CLI invocation (:attr:`Tombstone.argv`), a token path into the live click
  command tree, e.g. ``("crew", "review")``; or
* a view or action of the crew MCP tool (:attr:`Tombstone.mcp_view` /
  :attr:`Tombstone.mcp_action`), for a name that has no CLI twin by decision.

A tombstone is deliberately absent from the crew group's ``commands`` mapping.
It is resolved only when a caller actually types the retired name, through the
same path a registered subcommand uses (the group's ``get_command``), so a
retired name never appears in ``crew --help`` nor in a census that reads the
live command tree.
"""

from __future__ import annotations

from dataclasses import dataclass

import click

#: The exit code every tombstone exits with. It is documented in the crew
#: group's help, and it is deliberately the lowest code above 9 that no crew
#: command raises, so a caller may branch on it: 9 is member-in-flight, 75 is
#: queue-full and lane-paused, and the codes below 9 are the group's own.
TOMBSTONE_EXIT_CODE = 10

#: The crew group's own help documents the retirement code with this phrase.
TOMBSTONE_EXIT_HELP = (
    f"{TOMBSTONE_EXIT_CODE} the name is retired and the output names its successor"
)


@dataclass(frozen=True)
class Tombstone:
    """One retired verb and the invocation that replaced it.

    :param retired: the folded verb name exactly as a caller types it, e.g.
        ``"review-plan"`` for ``reckon crew review-plan``.
    :param successor: the successor as a reader names it, e.g.
        ``"crew review"`` — this is what the JSON document reports.
    :param argv: for a CLI successor, the token path from the crew group,
        e.g. ``("crew", "review")``. Empty for an MCP successor.
    :param mcp_view: for an MCP successor, the view the crew tool accepts.
    :param mcp_action: for an MCP successor, the action the crew tool accepts.
    :param detail: one line saying what the successor does, so the refusal
        explains itself without a second lookup.
    """

    retired: str
    successor: str
    argv: tuple[str, ...] = ()
    mcp_view: str | None = None
    mcp_action: str | None = None
    detail: str = ""

    @property
    def is_cli(self) -> bool:
        return bool(self.successor) and bool(self.argv)


#: The registry. Sections that fold further verbs add their own rows here.
TOMBSTONES: tuple[Tombstone, ...] = (
    Tombstone(
        retired="review-plan",
        successor="crew review",
        argv=("crew", "review"),
        detail="a plan review is composed by `crew review --plan`",
    ),
    Tombstone(
        retired="obligations",
        successor='crew(view="obligations")',
        mcp_view="obligations",
        detail=(
            "a session's owed duties are read through the crew MCP tool's "
            "obligations view; there is no CLI twin"
        ),
    ),
    Tombstone(
        retired="promote",
        successor="crew complete",
        argv=("crew", "complete"),
        detail="a finished run is promoted by `crew complete`",
    ),
)

#: Lookup by the retired name a caller types.
TOMBSTONE_BY_NAME: dict[str, Tombstone] = {row.retired: row for row in TOMBSTONES}

#: Options a tombstone maps onto the crew MCP tool's own parameter names.
_MCP_OPTION_PARAM = {
    "--project": "project",
    "--plan": "plan",
    "--section": "section",
    "--session": "session",
    "--run": "run_id",
    "--run-id": "run_id",
}


def _mcp_arguments(args: tuple[str, ...]) -> list[tuple[str, str]]:
    """The ``(parameter, value)`` pairs the caller's options map to, in order.

    Accepts both ``--opt value`` and ``--opt=value``. An option outside
    :data:`_MCP_OPTION_PARAM`, or one with no value, is dropped: the MCP
    successor is a different surface and only the arguments that map are
    carried across.
    """
    pairs: list[tuple[str, str]] = []
    index = 0
    while index < len(args):
        token = args[index]
        option, _, inline = token.partition("=")
        parameter = _MCP_OPTION_PARAM.get(option)
        if parameter is not None:
            if inline:
                pairs.append((parameter, inline))
            elif index + 1 < len(args) and not args[index + 1].startswith("-"):
                pairs.append((parameter, args[index + 1]))
                index += 1
        index += 1
    return pairs


def successor_invocation(row: Tombstone, args: tuple[str, ...]) -> str:
    """The invocation the successor takes, with ``args`` carried across.

    A CLI successor forwards the caller's arguments verbatim after its own
    token path, because the successor accepts the same option names the retired
    verb did. An MCP successor renders a ``crew(...)`` call carrying only the
    options that map onto the tool's parameters.
    """
    if row.is_cli:
        return " ".join(("reckon", *row.argv, *args))
    rendered = [f'{key}="{value}"' for key, value in _mcp_arguments(args)]
    view = f'view="{row.mcp_view}"' if row.mcp_view else ""
    action = f'action="{row.mcp_action}"' if row.mcp_action else ""
    call = ", ".join(part for part in (view, action, *rendered) if part)
    return f"crew({call})"


def tombstone_payload(row: Tombstone, args: tuple[str, ...]) -> dict[str, object]:
    """The one JSON document a tombstone prints."""
    return {
        "ok": False,
        "retired": row.retired,
        "successor": row.successor,
        "invocation": successor_invocation(row, args),
        "detail": row.detail,
        "exit_code": TOMBSTONE_EXIT_CODE,
    }


def tombstone_command(name: str) -> click.Command | None:
    """The hidden command that answers a retired ``name``, or ``None``.

    The command accepts any arguments, so the caller's options reach it intact
    whatever the successor will make of them, and it exits with
    :data:`TOMBSTONE_EXIT_CODE` after printing its document.
    """
    row = TOMBSTONE_BY_NAME.get(name)
    if row is None:
        return None

    @click.command(
        name=name,
        hidden=True,
        add_help_option=False,
        context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
    )
    @click.argument("args", nargs=-1, type=click.UNPROCESSED)
    @click.pass_context
    def _tombstone(ctx: click.Context, args: tuple[str, ...]) -> None:
        from reckon.crew_dispatch_commands import _emit

        _emit(tombstone_payload(row, args), pretty=False)
        ctx.exit(TOMBSTONE_EXIT_CODE)

    return _tombstone
