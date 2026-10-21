"""Retired crew verbs name the invocation that replaced them.

A coordinator's session carries skill text loaded before a verb is folded away,
so a name that no longer resolves keeps being reached for long after the change
(the retired ``review-plan`` alias was called hundreds of times after it was
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
        ``"review-plan"`` for the retired plan-review alias.
    :param successor: the successor as a reader names it, e.g.
        ``"crew review"`` — this is what the JSON document reports, narrowed by
        :func:`successor_name` to the view a case-mapped call actually reaches.
    :param argv: for a CLI successor, the token path from the crew group,
        e.g. ``("crew", "review")``. Empty for an MCP successor.
    :param flags: for a CLI successor that selects its behaviour with a flag,
        the flag that names the retired verb's own case, e.g. ``("--lift",)``
        for the retired ``budget-lift`` folded into ``crew budget --lift``. Carried
        into the successor invocation ahead of the caller's arguments, because
        the arguments alone do not select the case. It is deliberately not part
        of ``argv``: ``argv`` is a token path into the live command tree, and a
        flag is not a command.
    :param positional: for a CLI successor that names the retired verb's own
        case with a positional operand rather than a flag, the operand the
        successor takes, e.g. ``("velocity",)`` for the retired ``velocity``
        folded into ``crew view velocity``. Like :attr:`flags` it is carried
        ahead of the caller's arguments and kept out of ``argv``, which stays a
        token path into the live command tree.
    :param operand_flag: for a retired verb that selected its own case with a
        flag while the successor names that case with the positional operand,
        the flag carrying the selection, e.g. ``"--view"`` for the retired
        ``ledger``, whose ``--view records`` is ``crew view records``. Where it
        is set the caller's value is rendered as the successor's operand and
        the flag pair is consumed rather than forwarded: forwarding it verbatim
        prints an invocation the successor rejects, because ``crew view`` takes
        a view name as an operand and has no ``--view``. With the flag absent
        from the call the row's own :attr:`positional` stands.
    :param operand_aliases: for a retired verb whose own case names are not the
        successor's operand names, the ``(caller's value, successor operand)``
        pairs that reconcile them, e.g. ``(("summary", "ledger"),)`` for the
        retired ``ledger``, whose default ``--view summary`` is the ledger
        rollup and whose successor document is ``crew view ledger``. A value
        absent from the pairs is carried across unchanged, which is what sends
        ``--view records`` to ``crew view records``. Without the pairs one
        retired document would reach two successors depending on how the caller
        spelled its case.
    :param mcp_view: for an MCP successor, the view the crew tool accepts.
    :param mcp_action: for an MCP successor, the action the crew tool accepts.
    :param detail: one line saying what the successor does, so the refusal
        explains itself without a second lookup.
    """

    retired: str
    successor: str
    argv: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()
    positional: tuple[str, ...] = ()
    operand_flag: str = ""
    operand_aliases: tuple[tuple[str, str], ...] = ()
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
    Tombstone(
        retired="budget-lift",
        successor="crew budget --lift",
        argv=("crew", "budget"),
        flags=("--lift",),
        detail="a budget group's pace is lifted or cleared by `crew budget --lift`",
    ),
    Tombstone(
        retired="budget-lifts",
        successor="crew budget --list",
        argv=("crew", "budget"),
        flags=("--list",),
        detail="active and recent budget lifts are listed by `crew budget --list`",
    ),
    Tombstone(
        retired="budget-reset",
        successor="crew budget --reset",
        argv=("crew", "budget"),
        flags=("--reset",),
        detail=(
            "a budget group's banked reset is flagged, cleared or read by "
            "`crew budget --reset`"
        ),
    ),
    Tombstone(
        retired="preflight",
        successor="crew budget --check",
        argv=("crew", "budget"),
        flags=("--check",),
        detail="backend budget state is checked by `crew budget --check`",
    ),
    Tombstone(
        retired="unwatch",
        successor="crew watch --stop",
        argv=("crew", "watch"),
        flags=("--stop",),
        detail="a registered project watcher is stopped by `crew watch --stop`",
    ),
    Tombstone(
        retired="repair-status",
        successor="crew repair --status",
        argv=("crew", "repair"),
        detail="a manifest status is replaced by `crew repair --status`",
    ),
    Tombstone(
        retired="repair-review",
        successor="crew repair --review",
        argv=("crew", "repair"),
        flags=("--review",),
        detail="a review record is repaired by `crew repair --review`",
    ),
    Tombstone(
        retired="check-manifest",
        successor="crew check --manifest",
        argv=("crew", "check"),
        flags=("--manifest",),
        detail="a delivered manifest is checked by `crew check --manifest`",
    ),
    Tombstone(
        retired="check-review",
        successor="crew check --review",
        argv=("crew", "check"),
        flags=("--review",),
        detail="a delivered review is checked by `crew check --review`",
    ),
    Tombstone(
        retired="resume-ready",
        successor="crew resume --ready",
        argv=("crew", "resume"),
        flags=("--ready",),
        detail="ready runs are swept by `crew resume --ready`",
    ),
    Tombstone(
        retired="redispatch",
        successor="crew resume --backend",
        argv=("crew", "resume"),
        detail="a run moves backend through `crew resume --backend`",
    ),
    Tombstone(
        retired="list",
        successor="crew view live",
        argv=("crew", "view"),
        positional=("live",),
        detail=(
            "live run pointers are read by `crew view live`; its --project, "
            "--phase, --session and --mine options are the listing's own"
        ),
    ),
    Tombstone(
        retired="ledger",
        successor="crew view ledger",
        argv=("crew", "view"),
        positional=("ledger",),
        operand_flag="--view",
        operand_aliases=(("summary", "ledger"),),
        detail=(
            "committed run records are read by `crew view ledger`; the retired "
            "--view summary is that same rollup, and --view records and "
            "--view findings name the `records` and `findings` views"
        ),
    ),
    Tombstone(
        retired="velocity",
        successor="crew view velocity",
        argv=("crew", "view"),
        positional=("velocity",),
        detail="what the fleet delivered is read by `crew view velocity`",
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


def _operand_and_forwarded(
    args: tuple[str, ...],
    flag: str,
    default: tuple[str, ...],
    aliases: dict[str, str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split a caller's arguments into the successor's operand and the rest.

    The operand is the value the caller gave ``flag``, or ``default`` when the
    call does not carry it; the flag and its value are consumed so the
    successor does not see them. Both ``--flag value`` and ``--flag=value`` are
    read, and an occurrence with no value falls back to ``default`` rather than
    emitting an operand the successor would read as its own argument. A value
    the row renames through ``aliases`` is rendered as the successor's operand,
    so a case the two verbs name differently still reaches one successor.
    """
    operand = default
    forwarded: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        name, separator, inline = token.partition("=")
        if name != flag:
            forwarded.append(token)
            index += 1
            continue
        if separator:
            value, index = inline, index + 1
        elif index + 1 < len(args) and not args[index + 1].startswith("-"):
            value, index = args[index + 1], index + 2
        else:
            value, index = "", index + 1
        operand = (aliases.get(value, value),) if value else default
    return operand, tuple(forwarded)


def successor_invocation(row: Tombstone, args: tuple[str, ...]) -> str:
    """The invocation the successor takes, with ``args`` carried across.

    A CLI successor forwards the caller's arguments verbatim after its own
    token path, because the successor accepts the same option names the retired
    verb did — except where the row names an :attr:`~Tombstone.operand_flag`,
    in which case the value the caller gave that flag becomes the successor's
    operand, renamed through the row's
    :attr:`~Tombstone.operand_aliases` where the two verbs name a case
    differently, and the flag itself is dropped, so the printed invocation is
    one the successor accepts. An MCP successor renders a ``crew(...)`` call
    carrying only the options that map onto the tool's parameters.
    """
    if row.is_cli:
        operand, forwarded = row.positional, args
        if row.operand_flag:
            operand, forwarded = _operand_and_forwarded(
                args, row.operand_flag, row.positional, dict(row.operand_aliases)
            )
        return " ".join(("reckon", *row.argv, *operand, *row.flags, *forwarded))
    rendered = [f'{key}="{value}"' for key, value in _mcp_arguments(args)]
    view = f'view="{row.mcp_view}"' if row.mcp_view else ""
    action = f'action="{row.mcp_action}"' if row.mcp_action else ""
    call = ", ".join(part for part in (view, action, *rendered) if part)
    return f"crew({call})"


def successor_name(row: Tombstone, args: tuple[str, ...]) -> str:
    """The successor as it applies to this call.

    The row's own :attr:`~Tombstone.successor` — except for a row that maps the
    caller's case onto the successor's operand, where the successor names the
    view this call reaches. Otherwise the document would name one view and
    print an invocation that reads another, so the two halves of the answer
    would send a caller to different documents.
    """
    if row.is_cli and row.operand_flag:
        operand, _ = _operand_and_forwarded(
            args, row.operand_flag, row.positional, dict(row.operand_aliases)
        )
        return " ".join(("crew", *row.argv[1:], *operand))
    return row.successor


def tombstone_payload(row: Tombstone, args: tuple[str, ...]) -> dict[str, object]:
    """The one JSON document a tombstone prints."""
    return {
        "ok": False,
        "retired": row.retired,
        "successor": successor_name(row, args),
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
