"""Land delivered, accepted implementation runs from the admission service."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from reckon.crew import landing_measure, recovery, runs
from reckon.crew.promotion_records import _node_test_files
from reckon.crew.recovery_review_acceptance import (
    _review_accepts_promotion,
    record_landed_exemptions,
)

_run_srun = subprocess.run

# A revision as a manifest entry may spell it: hexadecimal, at least the
# seven characters git abbreviates to.
_REVISION = re.compile(r"[0-9A-Fa-f]{7,40}")


def _delivered_revision(entries: Sequence[Any]) -> str:
    """The revision the last ``manifest_commits`` entry names, or "".

    A worker writes its commits however its manifest reads to it: a bare
    revision, a revision with its subject after it, or several revisions on one
    line. An entry whose every token is a revision is a bare list and its last
    token is the delivered commit. Any other entry leads with the revision and
    carries a subject after it, so its first token is the commit — and only
    while that token really is a revision. A subject word that happens to be
    hexadecimal (``deadbeef``), or an entry that leads with prose, therefore
    never stands in for a commit: the answer is "" so no head is guessed, and
    the caller reports the entry it could not read.
    """
    if not entries:
        return ""
    tokens = str(entries[-1] or "").split()
    if not tokens:
        return ""
    if all(_REVISION.fullmatch(token) for token in tokens):
        return tokens[-1]
    first = tokens[0]
    return first if _REVISION.fullmatch(first) else ""


def _unreadable_entry_guard(entries: Sequence[Any]) -> str:
    """A refusal naming the manifest commits entry no revision could be read
    from, or "" when there is no entry to name.

    An entry the parser cannot read is the run's own defect, not an absence of
    work: it is reported on the live pointer with the entry quoted, so a
    coordinator can correct the manifest instead of the run disappearing from
    the pass with nothing written down.
    """
    if not entries:
        return ""
    entry = str(entries[-1] or "").strip()
    if not entry:
        return ""
    return (
        "delivered revision: the manifest commits entry "
        f"{entry!r} names no revision at its head"
    )


def _unreadable_entry_holds(
    previous: Mapping[str, Any] | None, head: str, guard: str, code_stamp: str
) -> bool:
    """Whether this head already carries this refusal under this code."""

    if not isinstance(previous, Mapping):
        return False
    return (
        previous.get("status") == "refused"
        and recovery.same_revision(str(previous.get("head") or ""), head)
        and str(previous.get("guard") or "") == guard
        and bool(code_stamp)
        and str(previous.get("code_stamp") or "") == code_stamp
    )


def _landing_code_stamp() -> str:
    """The content identity of the code that judges a landing.

    A refusal is a judgement made by a rule, so it holds only for the rule that
    made it: the stamp covers the package the landing pass runs from and moves
    when that code lands as a commit. An identity that cannot be read records
    nothing, so the next pass judges the refusal again rather than trusting a
    judgement it cannot place.
    """
    from reckon.crew.follower_registration import follower_code_stamp

    try:
        return follower_code_stamp()
    except (OSError, ValueError):
        return ""


def _review_stamp(review: Mapping[str, Any] | None) -> str:
    """The identity of the review record a judgement was made on, or "".

    Keyed on the record's content, so a reviewer who answers a finding, records
    a disposition or corrects a score rewrites the identity and the next pass
    reads the record as it now stands. A run with no readable record has no
    identity to keep, and an empty one never matches.
    """
    if not isinstance(review, Mapping):
        return ""
    from reckon.crew.plan_review import _digest

    return _digest(review)


def _refusal_holds(
    previous: Mapping[str, Any], code_stamp: str = "", review_stamp: str = ""
) -> bool:
    """Whether a recorded refusal was made by this code about this record.

    A refusal states the rule that made it and the record it read, and holds
    only while both are unchanged. A refusal recorded before those were kept
    carries neither, so it says nothing about the current rule and the run is
    judged once more.
    """
    if not code_stamp or not review_stamp:
        return False
    return (
        str(previous.get("code_stamp") or "") == code_stamp
        and str(previous.get("review_stamp") or "") == review_stamp
    )


def _record_attempt(
    run_id: str,
    *,
    head: str,
    guard: str,
    status: str,
    code_stamp: str = "",
    review_stamp: str = "",
) -> None:
    """Keep a refusal on the live run, with what made it, so a later pass can
    tell a refusal that still stands from one the current rule would reverse."""

    def mutation(pointer: dict[str, Any]) -> dict[str, Any]:
        pointer["admission_landing"] = {
            "head": head,
            "guard": guard,
            "status": status,
            "code_stamp": code_stamp,
            "review_stamp": review_stamp,
        }
        return pointer

    runs._mutate_pointer(run_id, mutation)


def _record_suite_evidence(run_id: str, measured: landing_measure.Measurement) -> None:
    """Keep the measurement where a later reader can open it.

    The live pointer loses this at promotion, so the run's own directory holds
    the pair as well.
    """

    record = measured.as_dict()
    directory = runs.run_dir(run_id) / "landing-measure"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "measurement.json").write_text(
        json.dumps(record, indent=2, sort_keys=True), encoding="utf-8"
    )

    def mutation(pointer: dict[str, Any]) -> dict[str, Any]:
        pointer["suite_evidence"] = record
        return pointer

    runs._mutate_pointer(run_id, mutation)


def _measurement_path(run_id: str) -> Path:
    return runs.run_dir(run_id) / "landing-measure" / "measurement.json"


def _stored_measurement(
    run_id: str, head: str, command: str
) -> landing_measure.Measurement | None:
    """The measurement already recorded for this head, or ``None``.

    A pass never re-measures a head it has already measured: the record is kept
    under the run's directory and is valid only for the head and the command it
    names, so a moved head or a changed command measures again.
    """
    if not command:
        return None
    try:
        record = json.loads(_measurement_path(run_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, Mapping):
        return None
    if str(record.get("head") or "") != head:
        return None
    if str(record.get("command") or "") != command:
        return None
    return landing_measure.Measurement.from_dict(record)


def _merge_conflicts(pointer: Mapping[str, Any], head: str) -> list[str]:
    """The paths on which the run's head conflicts with the primary branch.

    Read through ``git merge-tree``, which builds the merged tree without
    touching the checkout or moving the branch, so a conflicting run is refused
    before anything is written to the branch. An instrument that cannot answer —
    an unresolvable head, a checkout that is not a work tree — returns no
    conflict here and the landing step itself reports the failure, because
    inventing a conflict would refuse a run git never judged.
    """
    repo = Path(str(pointer.get("repo") or "").strip())
    if not head or not repo.is_dir():
        return []
    from reckon.crew import merge

    try:
        _merged_tree, conflicts = merge._merge_tree(repo, "HEAD", head)
    except (merge.MergeError, OSError):
        return []
    return merge.refusing_conflicts(conflicts)


def _measure_timeout(pointer: Mapping[str, Any]) -> float:
    """The wall-clock bound per measurement subprocess, from flight config."""
    from reckon import flight

    try:
        resolved = flight.resolve(
            str(pointer.get("project") or "") or None,
            checkout_path=str(pointer.get("repo") or "") or None,
        )
    except (OSError, ValueError, flight.FlightConfigError):
        return landing_measure.DEFAULT_MEASURE_TIMEOUT_SECONDS
    return landing_measure.measure_timeout(resolved.config)


def _measure_delivery(
    pointer: Mapping[str, Any], run_id: str, head: str, tree: str
) -> Any:
    """Run the three suite readings for one delivered run.

    A run that armed no command has nothing to measure; the landing path keeps
    its earlier behaviour for such a run rather than inventing a command. A
    measurement that cannot be taken — no base revision, or a base checkout
    that cannot be built — yields an unreadable measurement so the caller
    refuses the landing instead of inferring a verdict from nothing.
    """
    command = str(pointer.get("suite_command") or "").strip()
    if not command:
        return None
    base_sha = str(pointer.get("base_sha") or "").strip()
    repo = Path(str(pointer.get("repo") or tree or ""))
    if not base_sha or not repo.is_dir():
        return landing_measure.Measurement(
            command=command,
            suite={},
            population={},
            unreadable="the run records no base revision to compare against",
        )
    timeout = _measure_timeout(pointer)
    logs = runs.run_dir(run_id) / "landing-measure"
    try:
        # Only the checkout is inside this except: an error raised by the
        # measurement itself is a different failure and is reported as one.
        with landing_measure.base_checkout(repo, base_sha, timeout) as base_tree:
            try:
                measured = landing_measure.measure_run(
                    command=command,
                    tree=Path(tree),
                    base_sha=base_sha,
                    head_sha=head,
                    base_tree=base_tree,
                    logs_dir=logs,
                    test_files=_node_test_files(pointer),
                    timeout=timeout,
                )
            except (OSError, ValueError) as exc:
                measured = landing_measure.Measurement(
                    command=command,
                    suite={},
                    population={},
                    unreadable=(f"the measurement raised {type(exc).__name__}: {exc}"),
                    head=head,
                )
    except landing_measure.BaseCheckoutError as exc:
        measured = landing_measure.Measurement(
            command=command,
            suite={},
            population={},
            unreadable=f"the base checkout could not be built: {exc}",
            head=head,
        )
    _record_suite_evidence(run_id, measured)
    return measured


def _measure_guard(measured: Any) -> str:
    """The refusal a measurement states, or "" when the run may land."""
    if measured is None:
        return ""
    if measured.unreadable:
        return f"landing measure unreadable: {measured.unreadable}"
    if not measured.real:
        return ""
    details = []
    for test_id in measured.real:
        results = measured.isolated.get(test_id) or {"base": [], "head": []}
        details.append(
            f"{test_id} failed {sum(results['head'])}/{len(results['head'])} at head "
            f"and {sum(results['base'])}/{len(results['base'])} at base"
        )
    return "added suite regression: " + "; ".join(details)


def _landing_beat(pointer: Mapping[str, Any], run_id: str) -> str:
    """Tick the landed node's section through the ops a coordinator uses.

    Returns "" when the beat was recorded or the run names no plan section, and
    the refusal otherwise. The evidence anchor is the node's own fragment path,
    so a reader follows the closing comment to the record the worker wrote.
    """
    node = pointer.get("node") if isinstance(pointer.get("node"), Mapping) else {}
    plan = str(node.get("plan") or "").strip()
    section = str(node.get("section") or "").strip()
    project = str(pointer.get("project") or "").strip()
    repo = str(pointer.get("repo") or "").strip()
    if not (plan and section and project and repo):
        return ""
    from datetime import UTC, datetime

    from reckon import _store
    from reckon.mcp_edit_plan import _edit_plan_tool

    anchor = str(node.get("id") or run_id)
    record_href = f"/{project}/evidence/archive/{plan}-landed.html#{anchor}"
    body = (
        f"<p>Landed {run_id} on the measured suite pair; "
        f'<a href="{record_href}">evidence</a></p>'
    )
    ops = [
        {
            "op": "append_evidence",
            "plan": plan,
            "anchor": anchor,
            "title": f"{section} landed",
            "body": f"<p>Run <code>{run_id}</code> landed after the landing pass "
            "measured its suite at base and head.</p>",
        },
        {"op": "set", "path": f"section_declarations.{section}", "value": "done"},
        {
            "op": "append",
            "target": "comments",
            "section": section,
            "item": {
                "id": f"c-close-{section}",
                "who": "reckon-build",
                "when": datetime.now(UTC).isoformat(),
                "body": body,
            },
        },
    ]
    _data, version = _store.read_plan(project, plan, repo)
    try:
        result = _edit_plan_tool(
            project,
            plan,
            expected_version=version,
            ops=ops,
            checkout_path=repo,
        )
    except (OSError, ValueError) as exc:
        return f"landing beat refused: {type(exc).__name__}: {exc}"
    if not result.get("ok"):
        return f"landing beat refused: {result.get('error') or 'unknown refusal'}"
    return ""


def _land_command(run_id: str) -> list[str]:
    """Run the coordinator's land command in one explicit fleet step."""
    from reckon.nested_launch import shim_directory

    return [
        str(shim_directory() / "srun"),
        "--overlap",
        "--ntasks=1",
        sys.executable,
        "-c",
        "from reckon.cli import main; main()",
        "crew",
        "complete",
        "--run",
        run_id,
        "--gate",
        "passed",
        "--land",
    ]


def _land(run_id: str) -> tuple[bool, str]:
    # The CLI calls the same promotion lease and merge index-lock retry a
    # coordinator uses. Its gate also runs inside this step, off the service.
    environment = os.environ.copy()
    source = str(Path(__file__).resolve().parents[2])
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (source, environment.get("PYTHONPATH", "")) if part
    )
    finished = _run_srun(
        _land_command(run_id),
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    output = (finished.stdout or "").strip()
    try:
        receipt = json.loads(output)
    except ValueError:
        receipt = {}
    if finished.returncode == 0 and receipt.get("ok") is True:
        return True, ""
    if isinstance(receipt, Mapping):
        steps = receipt.get("steps")
        if isinstance(steps, Mapping):
            for step in ("promote", "land", "release"):
                entry = steps.get(step)
                if isinstance(entry, Mapping) and entry.get("ok") is False:
                    return (
                        False,
                        f"{step}: {entry.get('error') or receipt.get('error') or 'refused'}",
                    )
        detail = receipt.get("error") or receipt.get("detail")
        if detail:
            return False, str(detail)
    return False, (
        finished.stderr or output or f"land exited {finished.returncode}"
    ).strip()


def _delivery_order(pointer: Mapping[str, Any]) -> tuple[int, str]:
    """Delivery order: the manifest write time, then the run id.

    The landing pass runs inside the admission loop, so a delivered run it
    cannot measure yet must not starve the ones delivered before it.
    """
    return (
        runs._manifest_mtime_ns(str(pointer.get("manifest_path") or "")),
        str(pointer.get("run_id") or ""),
    )


def land_once() -> dict[str, Any]:
    """Land each accepted delivery once and return obligations for refusals.

    At most one delivered run is measured per pass. A measurement runs the
    suite at two revisions plus up to six isolated re-runs, so measuring every
    waiting run in one pass would hold admission for as long as the slowest
    suites take. The rest wait for later passes in delivery order, and a head
    already measured is reused rather than measured again.

    A head refused on an earlier pass is judged again as soon as the rule that
    refused it or the review it read has changed, so a run held by a rule since
    replaced does not stay held: the refusal is skipped only while the code
    stamp and the review record it judged are both unchanged. A manifest whose
    last commit entry names no revision is refused with that entry quoted and an
    obligation for the run's session, so the run is visible to a coordinator
    rather than dropped from the pass in silence.
    """
    from reckon.crew.recovery_review_subject import select_review_for_head

    landed: list[str] = []
    obligations: list[dict[str, str]] = []
    deferred: list[str] = []
    measured_this_pass = False
    for pointer in sorted(runs.list_live(), key=_delivery_order):
        if (
            str(pointer.get("role") or (pointer.get("node") or {}).get("role") or "")
            != "implement"
        ):
            continue
        run_id = str(pointer.get("run_id") or "")
        project = str(pointer.get("project") or "")
        if not run_id or not project:
            continue
        classified = recovery.classify_pointer(pointer)
        if classified.get("manifest_status") != "complete":
            continue
        head, tree = recovery._review_head_and_tree(pointer)
        if not head:
            continue
        entries = classified.get("manifest_commits") or ()
        delivered = _delivered_revision(entries)
        code_stamp = _landing_code_stamp()
        if not delivered:
            guard = _unreadable_entry_guard(entries)
            if guard:
                previous = pointer.get("admission_landing")
                if not _unreadable_entry_holds(previous, head, guard, code_stamp):
                    _record_attempt(
                        run_id,
                        head=head,
                        guard=guard,
                        status="refused",
                        code_stamp=code_stamp,
                    )
                    obligations.append(
                        {
                            "run_id": run_id,
                            "session": str(pointer.get("session") or ""),
                            "guard": guard,
                        }
                    )
            continue
        if not recovery.same_revision(head, delivered):
            continue
        try:
            review, stale = select_review_for_head(project, run_id, head, tree=tree)
        except (OSError, ValueError) as exc:
            review, stale = None, str(exc)
        review_stamp = _review_stamp(review)
        previous = pointer.get("admission_landing")
        if isinstance(previous, Mapping) and recovery.same_revision(
            str(previous.get("head") or ""), head
        ):
            if previous.get("status") == "attempting":
                guard = "automatic landing interrupted before a receipt"
                _record_attempt(
                    run_id,
                    head=head,
                    guard=guard,
                    status="refused",
                    code_stamp=code_stamp,
                    review_stamp=review_stamp,
                )
                obligations.append(
                    {
                        "run_id": run_id,
                        "session": str(pointer.get("session") or ""),
                        "guard": guard,
                    }
                )
                continue
            if previous.get("status") == "refused" and _refusal_holds(
                previous, code_stamp, review_stamp
            ):
                continue
        if stale or review is None:
            continue
        if not _review_accepts_promotion(review):
            guard = "review acceptance: a blocking finding or an incomplete record"
            _record_attempt(
                run_id,
                head=head,
                guard=guard,
                status="refused",
                code_stamp=code_stamp,
                review_stamp=review_stamp,
            )
            obligations.append(
                {
                    "run_id": run_id,
                    "session": str(pointer.get("session") or ""),
                    "guard": guard,
                }
            )
            continue
        # A head that conflicts with the primary branch cannot be landed at all,
        # so it is refused before the expensive measurement rather than after
        # it, and the refusal names the paths the next writer must resolve.
        conflicting = _merge_conflicts(pointer, head)
        if conflicting:
            guard = "the head conflicts with the primary branch: " + ", ".join(
                conflicting
            )
            _record_attempt(
                run_id,
                head=head,
                guard=guard,
                status="refused",
                code_stamp=code_stamp,
                review_stamp=review_stamp,
            )
            obligations.append(
                {
                    "run_id": run_id,
                    "session": str(pointer.get("session") or ""),
                    "guard": guard,
                }
            )
            continue
        command = str(pointer.get("suite_command") or "").strip()
        measured = _stored_measurement(run_id, head, command)
        if measured is not None:
            # The stored head is this head: reuse it, and re-apply it to the
            # pointer so a promoted pass still records what it measured on.
            _record_suite_evidence(run_id, measured)
        elif command and measured_this_pass:
            deferred.append(run_id)
            continue
        else:
            measured = _measure_delivery(pointer, run_id, head, tree)
            measured_this_pass = measured_this_pass or measured is not None
        measure_guard = _measure_guard(measured)
        if measure_guard:
            _record_attempt(
                run_id,
                head=head,
                guard=measure_guard,
                status="refused",
                code_stamp=code_stamp,
                review_stamp=review_stamp,
            )
            obligations.append(
                {
                    "run_id": run_id,
                    "session": str(pointer.get("session") or ""),
                    "guard": measure_guard,
                }
            )
            continue
        # The review's non-blocking findings do not hold the landing, and the
        # finding a reviewer raised must not vanish with the pointer that
        # carried it. Exemptions are recorded only once the landing has
        # succeeded: a refusal here leaves the run unlanded, and marking its
        # findings exempted would say a landing happened that did not.
        _record_attempt(
            run_id,
            head=head,
            guard="crew land",
            status="attempting",
            code_stamp=code_stamp,
            review_stamp=review_stamp,
        )
        try:
            accepted, guard = _land(run_id)
        except (OSError, ValueError) as exc:
            accepted, guard = False, f"{type(exc).__name__}: {exc}"
        if accepted:
            landed.append(run_id)
            try:
                record_landed_exemptions(
                    project, run_id, review, reviewed_head_sha=head
                )
            except (OSError, ValueError) as exc:
                # The landing stands; only the record of what the reviewer
                # raised is missing, so the run is reported and not unlanded.
                obligations.append(
                    {
                        "run_id": run_id,
                        "session": str(pointer.get("session") or ""),
                        "guard": (
                            "the run landed but its review's non-blocking "
                            "findings could not be recorded as exempted: "
                            f"{type(exc).__name__}: {exc}"
                        ),
                    }
                )
            beat_refusal = _landing_beat(pointer, run_id)
            if beat_refusal:
                obligations.append(
                    {
                        "run_id": run_id,
                        "session": str(pointer.get("session") or ""),
                        "guard": beat_refusal,
                    }
                )
        else:
            guard = guard or "crew land refused without a guard"
            _record_attempt(
                run_id,
                head=head,
                guard=guard,
                status="refused",
                code_stamp=code_stamp,
                review_stamp=review_stamp,
            )
            obligations.append(
                {
                    "run_id": run_id,
                    "session": str(pointer.get("session") or ""),
                    "guard": guard,
                }
            )
    return {"landed": landed, "obligations": obligations, "deferred": deferred}
