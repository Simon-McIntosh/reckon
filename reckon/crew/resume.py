"""A coordinator can hold a delivered run, and a resume never vanishes.

An automatic lander promotes a delivered run the moment its evidence reads
clean, and a promotion deletes the run's live pointer. A coordinator who
intends to steer that delivery has nothing to tell the lander with: by the time
it resumes the run to carry a correction, the run has been merged and there is
no pointer left to resume, so the resume prints nothing and raises nothing and
the correction is lost.

Two facts fix that, and both live on the run record rather than in a
coordinator's head.

*A hold* names who deferred the landing, why, and when. While it is unreleased
the landing pass skips the run and the review-acceptance sweep refuses to
promote it, so the delivery waits for the coordinator instead of the
coordinator racing the lander. Releasing the hold records the reason the
deferral ended beside the reason it began, so the run's own record explains the
delay it carried.

*A resume holds before it runs.* A resume of a delivered run records the hold
first, so a landing pass that runs between the hold and the launch finds the
run held rather than free to promote. A resume of a run a promotion already
owns is refused, naming the promotion and the revision it asserted, rather than
ending silently as a launch of nothing.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import click

from reckon.crew import runs
from reckon.crew.node import CrewError

#: The pointer field carrying a coordinator's hold over a delivered run.
HOLD_FIELD = "delivery_hold"

#: Who a hold is taken by when the caller names nobody.
DEFAULT_HOLDER = "coordinator"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def delivery_hold(pointer: Mapping[str, Any]) -> dict[str, Any] | None:
    """The hold a run still carries, or ``None`` when it is free to land.

    A released hold stays on the record — the release carries its own reason
    and the delay it ended — so the field's presence alone is not a hold. Only
    an entry with no release stamp is one.
    """
    hold = pointer.get(HOLD_FIELD)
    if not isinstance(hold, Mapping):
        return None
    if str(hold.get("released_at") or "").strip():
        return None
    return dict(hold)


def covers_delivery(hold: Mapping[str, Any], revision: str) -> bool:
    """Whether a hold defers this delivery.

    A hold taken by a resume names the revision that was delivered when the
    resume was issued, because that is the delivery the lander must not take
    out from under it. When the resumed attempt delivers again it delivers a
    new head, and there is nothing left to defer: the hold stops covering the
    run and the lander judges the repair. A hold its coordinator took by hand
    names no revision and covers every delivery until it is released, because a
    coordinator who asked for the run to wait did not ask for one revision to
    wait.
    """
    named = str(hold.get("head") or "").strip()
    if not named:
        return True
    return named == str(revision or "").strip()


def hold_reason(pointer: Mapping[str, Any], revision: str | None = None) -> str:
    """The reason a held run names for ``revision``, or "" when it covers none.

    ``revision`` omitted asks whether the run carries any hold at all, which is
    the question a resume and a release ask. A revision asks the narrower
    question a landing pass asks: is this delivery the one the hold was taken
    against.
    """
    hold = delivery_hold(pointer)
    if hold is None:
        return ""
    if revision is not None and not covers_delivery(hold, revision):
        return ""
    return f"held by {hold.get('by') or DEFAULT_HOLDER}: {hold.get('reason') or ''}"


def hold_delivery(
    run_id: str, reason: str, *, by: str = DEFAULT_HOLDER, head: str = ""
) -> dict[str, Any]:
    """Record a coordinator's hold over a delivered run.

    A hold names its reason, because a deferral nobody can read is
    indistinguishable from a lander that forgot the run. Holding an already
    held run keeps the first hold: the reason it began is the fact a later
    reader needs, and a second reason would replace it with the second
    caller's wording.
    """
    reason = str(reason or "").strip()
    if not reason:
        raise CrewError(
            f"a hold on run {run_id!r} needs the reason it was taken: a "
            "deferral whose reason nobody recorded cannot be reviewed, "
            "released or explained"
        )

    def mutation(record: dict[str, Any]) -> dict[str, Any]:
        if delivery_hold(record) is not None:
            return record
        record[HOLD_FIELD] = {
            "reason": reason,
            "by": str(by or DEFAULT_HOLDER),
            "at": _now(),
            **({"head": str(head).strip()} if str(head).strip() else {}),
        }
        return record

    record = runs._mutate_pointer(run_id, mutation)
    hold = delivery_hold(record) or {}
    return {"run_id": run_id, "held": True, HOLD_FIELD: hold}


def release_hold(
    run_id: str, reason: str, *, by: str = DEFAULT_HOLDER
) -> dict[str, Any]:
    """Release a hold, recording why the deferral ended.

    Releasing a run that carries no hold is a refusal rather than a no-op: a
    coordinator that releases the wrong run should be told its hold never
    existed, not answered as though it had.
    """
    reason = str(reason or "").strip()
    if not reason:
        raise CrewError(
            f"a release of the hold on run {run_id!r} needs the reason it "
            "ended, so the delay the run carried is explained where it began"
        )

    def mutation(record: dict[str, Any]) -> dict[str, Any]:
        hold = delivery_hold(record)
        if hold is None:
            raise CrewError(f"run {run_id!r} carries no hold to release")
        hold["released_at"] = _now()
        hold["release_reason"] = reason
        hold["released_by"] = str(by or DEFAULT_HOLDER)
        record[HOLD_FIELD] = hold
        return record

    record = runs._mutate_pointer(run_id, mutation)
    return {
        "run_id": run_id,
        "held": False,
        HOLD_FIELD: record.get(HOLD_FIELD) or {},
    }


def promotion_of(run_id: str, project: str | None = None) -> dict[str, Any] | None:
    """The ledger row of a run a promotion already owns, or ``None``.

    Promotion deletes the live pointer and writes a committed row beside the
    project's ledger, so that row is the only place a promoted run's identity
    survives. A caller holding no project name resolves it from the mounts, the
    same way every other single-run verb does.
    """
    from reckon.crew import pending_landing

    name = project or runs._promoted_record_project(run_id, None)
    if not name:
        return None
    row = pending_landing._promoted_row(str(name), run_id)
    return dict(row) if row is not None else None


def promotion_in_flight(run_id: str) -> bool:
    """Whether a promotion of this run is running right now."""
    from reckon.crew import pending_landing

    return pending_landing._promotion_lease_held(run_id)


def _promoted_refusal(run_id: str, row: Mapping[str, Any]) -> CrewError:
    revision = str(row.get("promoted_revision") or "").strip()
    commits = [str(sha).strip() for sha in (row.get("commits") or ())]
    if not revision:
        revision = next((sha for sha in reversed(commits) if sha), "")
    by = str(row.get("promoted_by") or "").strip()
    detail = f" at {revision}" if revision else " with no revision recorded"
    if by:
        detail += f" (promoted by {by})"
    return CrewError(
        f"run {run_id!r} has already been promoted{detail}, so there is no live "
        "pointer to resume and no live pointer to reopen: the delivery is in the "
        "ledger now. The resume wrote nothing and launched nothing."
    )


def _delivered(record: Mapping[str, Any]) -> bool:
    """Whether a run has delivered and is therefore the lander's to take."""
    if str(record.get("phase") or "") == "complete":
        return True
    from reckon.crew import recovery

    try:
        classified = recovery.classify_pointer(record)
    except (OSError, ValueError):
        return False
    return str(classified.get("manifest_status") or "") == "complete"


def prepare_resume(
    run_id: str,
    record: Mapping[str, Any] | None = None,
    *,
    by: str = DEFAULT_HOLDER,
) -> dict[str, Any]:
    """Take the hold a resume of a delivered run owes, or refuse the resume.

    Three outcomes, each recorded rather than assumed:

    * a run a promotion already owns is refused, naming its revision, so the
      coordinator learns its repair has nowhere to go instead of watching a
      resume end in silence;
    * a delivered run with no hold is held first, so a landing pass running
      between this call and the launch finds it held;
    * anything else is left alone — a run still working needs no hold, and a
      run already held keeps the reason its coordinator gave.

    The promotion's lease is read before the pointer is, because a promotion
    claims the lease before it does any work and removes the pointer only at the
    very end. Reading the pointer first would find it present through almost the
    whole promotion window, take a hold and launch a worker beside the promotion
    — the race this guard exists to close.
    """
    if promotion_in_flight(run_id):
        raise CrewError(
            f"run {run_id!r} is being promoted right now: a promotion of it "
            "holds its lease until that promotion returns, so a resume would "
            "launch a worker beside a promotion that is already rewriting and "
            "about to remove the run's pointer. Read the run's ledger row for "
            "the promotion's outcome, then resume if none was written."
        )
    if record is None:
        try:
            record = runs.read_pointer(run_id)
        except CrewError:
            record = None
    if record is None:
        row = promotion_of(run_id)
        if row is not None:
            raise _promoted_refusal(run_id, row)
        return {"run_id": run_id, "held": False, "reason": ""}
    if delivery_hold(record) is not None:
        return {
            "run_id": run_id,
            "held": True,
            "reason": str(delivery_hold(record).get("reason") or ""),
            "already_held": True,
        }
    if not _delivered(record):
        return {"run_id": run_id, "held": False, "reason": ""}
    from reckon.crew import recovery

    head, _tree = recovery._review_head_and_tree(record)
    held = hold_delivery(
        run_id,
        "a coordinator is resuming this delivered run to steer its result: "
        "the landing waits until that resume's attempt is judged",
        by=by,
        head=head,
    )
    return {"run_id": run_id, "held": True, **held, "already_held": False}


def guard_resume_command(command: click.Command) -> click.Command:
    """Make the single-run resume hold first, and refuse a promoted run.

    The resume door composes its own arguments and its own launch path, and the
    guard belongs to the hold's owner rather than beside that composition, so
    the command's callback is wrapped here rather than edited where it is
    defined. Flags that write nothing — a ``--ready`` sweep, a ``--dry-run`` or
    a ``--print-only`` preview — are left unguarded: a preview that held a run
    would defer a landing to preview something nobody ran.
    """
    original = command.callback
    if original is None or getattr(original, "_delivery_hold_guard", False):
        return command

    @functools.wraps(original)
    def callback(*args: Any, **kwargs: Any) -> Any:
        run_id = str(kwargs.get("run_id") or "")
        if run_id and not (
            kwargs.get("ready") or kwargs.get("dry_run") or kwargs.get("print_only")
        ):
            try:
                prepare_resume(run_id)
            except CrewError as exc:
                raise click.ClickException(str(exc)) from exc
        return original(*args, **kwargs)

    callback._delivery_hold_guard = True
    command.callback = callback
    return command


def _emit(payload: Mapping[str, Any], pretty: bool) -> None:
    click.echo(
        json.dumps(payload, indent=2 if pretty else None, sort_keys=True)
        if pretty
        else json.dumps(payload, sort_keys=True)
    )


def _register_commands() -> None:
    """Attach the two hold verbs to the crew group, once per process."""
    from reckon.crew_dispatch_commands import crew

    if "hold" in crew.commands:
        return

    @crew.command(name="hold")
    @click.option("--run", "run_id", required=True, help="The delivered run to hold.")
    @click.option(
        "--reason", required=True, help="Why the landing is deferred; recorded."
    )
    @click.option("--by", default=DEFAULT_HOLDER, help="Who took the hold.")
    @click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
    def crew_hold(run_id: str, reason: str, by: str, pretty: bool) -> None:
        """Hold a delivered run from automatic promotion and landing.

        The landing pass and the review-acceptance sweep both read the hold, so
        a held run waits for its coordinator however clean its evidence reads.
        """
        try:
            result = hold_delivery(run_id, reason, by=by)
        except CrewError as exc:
            raise click.ClickException(str(exc)) from exc
        _emit(result, pretty)

    @crew.command(name="release")
    @click.option("--run", "run_id", required=True, help="The held run to release.")
    @click.option("--reason", required=True, help="Why the deferral ended; recorded.")
    @click.option("--by", default=DEFAULT_HOLDER, help="Who released the hold.")
    @click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
    def crew_release(run_id: str, reason: str, by: str, pretty: bool) -> None:
        """Release a hold so the run is the lander's again.

        The release is recorded beside the hold, with its own reason, so the
        run's record explains the delay it carried.
        """
        try:
            result = release_hold(run_id, reason, by=by)
        except CrewError as exc:
            raise click.ClickException(str(exc)) from exc
        _emit(result, pretty)


_register_commands()
