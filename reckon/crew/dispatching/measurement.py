"""Measurement runs: gate a replayed section without landing its work."""

from __future__ import annotations

from ._imports import Any, Mapping, Path

_FACADE_BINDINGS = ()

MEASURE_ONLY_FLAG = "--measure-only"

#: The roles whose work is expected to land on the branch. A section declared
#: ``done`` already carries that work, so a dispatch in one of these roles is
#: the replay the flag exists for. ``review``, ``verify`` and ``investigate``
#: read work rather than add it — a landed section is exactly what a review or
#: an investigation is given — so they dispatch without the flag, which is also
#: what lets the review reflex reach a run that replays one.
MEASUREMENT_LANDING_ROLES = frozenset(
    {"", "implement", "design", "test", "documentation", "cleanup"}
)


def _section_declaration(
    project: str, slug: str, *, repo: Path | None, section: str
) -> str:
    """One plan section's own declaration, or "" when the plan names none.

    The declaration is the plan's statement about the section, written by the
    same plan writer that lands it, so a dispatch reads it rather than guessing
    from the section's heading. An unreadable plan answers "", because the
    dispatch's own plan validation refuses that case with a message naming the
    plan rather than the section.

    The two spellings are compared through the ledger's own normal form: a
    dispatch addresses the section it names ("sN", "§N", "section N"), and
    the declaration map is keyed however the plan's author wrote it.
    """
    from reckon import _store as store_module
    from reckon.ledger import normalize_section

    if not slug or not section:
        return ""
    try:
        plan, _version = store_module.read_plan(project, slug, root=repo)
    except Exception:  # noqa: BLE001 - the plan's own validation reports it
        return ""
    declarations = plan.get("section_declarations")
    if not isinstance(declarations, Mapping):
        return ""
    wanted = normalize_section(section)
    declared = next(
        (
            value
            for identity, value in declarations.items()
            if normalize_section(identity) == wanted
        ),
        "",
    )
    if isinstance(declared, Mapping):
        declared = declared.get("status") or ""
    return str(declared or "").strip().lower()


def _measurement_flag_refusal(
    project: str,
    plan: str,
    section: str,
    *,
    repo: Path | None,
    measure_only: str,
    role: str = "",
) -> str:
    """Why this dispatch of an already-declared section needs the flag, or "".

    A section declared ``done`` is the plan saying its work is already on the
    branch. A dispatch without the flag would build that work a second time and
    hand the delivery to a review reflex and a lander that expect it to land, so
    the declaration is dispatchable only as a measurement run: the flag says the
    run builds and gates and then is neither reviewed, merged nor landed.
    ``implementable`` and every other declaration dispatch as they always have.

    A role that reads work rather than adds it is not refused: a review, a
    verification or an investigation of a landed section adds no delivery to
    land, and refusing it would take away the one dispatch a done section most
    needs.
    """
    if measure_only:
        return ""
    if str(role or "").strip().lower() not in MEASUREMENT_LANDING_ROLES:
        return ""
    if _section_declaration(project, plan, repo=repo, section=section) != "done":
        return ""
    return (
        f"section {section!r} of plan {plan!r} is declared done, so its work is "
        "already on the branch; it is dispatchable only as a measurement run, "
        f"which builds and gates and then never lands — pass {MEASURE_ONLY_FLAG} "
        "REASON saying why this replay is worth the work"
    )


def _mark_measurement_run(
    run_id: str, record: Mapping[str, Any], *, reason: str
) -> dict[str, Any]:
    """Record on the run's own pointer that it builds and gates but never lands.

    The pointer is what the review reflex and the lander read, so the marker goes
    on it rather than on the answer alone. It is written after the launch has
    published its full pointer, under that pointer's own lock, and the record
    returned to the caller carries the same field so a caller reading the answer
    sees the run it dispatched without a second read.
    """
    from reckon.crew.runs import _mutate_pointer

    marker = {"reason": str(reason).strip()}

    def mutation(pointer: dict[str, Any]) -> dict[str, Any]:
        pointer["measure_only"] = dict(marker)
        return pointer

    _mutate_pointer(run_id, mutation)
    return {**record, "measure_only": dict(marker)}
