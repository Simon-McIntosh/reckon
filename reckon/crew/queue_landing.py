"""Land delivered, accepted implementation runs from the admission service."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from reckon.crew import landing_measure, recovery, runs
from reckon.crew.node import CrewError
from reckon.crew.promotion_records import _node_test_files
from reckon.crew.recovery_review_acceptance import (
    _review_accepts_promotion,
    record_landed_exemptions,
)

_run_srun = subprocess.run

# A revision as a manifest entry may spell it: hexadecimal, at least the
# seven characters git abbreviates to.
_REVISION = re.compile(r"[0-9A-Fa-f]{7,40}")

#: Seconds before a refusal that is environmental — a measurement bound that
#: was exceeded, or a base checkout that could not be built — is judged again.
#: Neither says anything about the run's code, so the run must be re-read even
#: though its code and review have not moved.
RETRY_BACKOFF_SECONDS = 900.0

#: The cap on that back-off. It doubles per repeated timeout, and without a cap
#: a run stuck on a permanently slow host would drift out of reach.
RETRY_BACKOFF_CAP_SECONDS = 6 * 60 * 60.0


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


def _review_reflex_state(pointer: Mapping[str, Any]) -> tuple[str, str]:
    """The review reflex's own state for this run, as ``(state, detail)``.

    A run waiting for its review is not a run with nothing to say: the reflex
    either dispatched a review or was refused, and it writes which onto the run
    it acted for. Reading that record beside the waiting run is what turns a
    silent skip into a verdict a coordinator can act on. A run carrying no
    record at all has not been picked up yet, which is reported as a refusal to
    dispatch with that reason rather than as an absence of state.
    """
    from reckon.crew.recovery_review_dispatch import REVIEW_DISPATCH_FIELD

    recorded = pointer.get(REVIEW_DISPATCH_FIELD)
    if not isinstance(recorded, Mapping):
        return "refused", "the review reflex has recorded no attempt for this run"
    if str(recorded.get("status") or "") == "dispatched":
        return "dispatched", ""
    reason = str(recorded.get("reason") or "").strip()
    return "refused", reason or str(recorded.get("status") or "")


def _awaiting_review_holds(
    previous: Mapping[str, Any] | None,
    head: str,
    state: str,
    detail: str,
    code_stamp: str,
) -> bool:
    """Whether this head already carries this awaiting-review record."""

    if not isinstance(previous, Mapping):
        return False
    return (
        previous.get("status") == "awaiting-review"
        and recovery.same_revision(str(previous.get("head") or ""), head)
        and str(previous.get("review_state") or "") == state
        and str(previous.get("review_detail") or "") == detail
        and bool(code_stamp)
        and str(previous.get("code_stamp") or "") == code_stamp
    )


def _record_awaiting_review(
    run_id: str,
    pointer: Mapping[str, Any],
    *,
    head: str,
    code_stamp: str,
    review_stamp: str,
) -> None:
    """Record that this head's landing waits on the review reflex.

    The run is not refused and not landed: it has a verdict, and the verdict
    names what it waits for. Writing it once per head and reflex state keeps a
    pass that runs every 30 seconds from rewriting the pointer for a run whose
    situation has not moved.
    """
    state, detail = _review_reflex_state(pointer)
    if _awaiting_review_holds(
        pointer.get("admission_landing"), head, state, detail, code_stamp
    ):
        return

    def mutation(record: dict[str, Any]) -> dict[str, Any]:
        record["admission_landing"] = {
            "head": head,
            "status": "awaiting-review",
            "review_state": state,
            "review_detail": detail,
            "code_stamp": code_stamp,
            "review_stamp": review_stamp,
        }
        return record

    runs._mutate_pointer(run_id, mutation)


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


#: The refusals that say the measurement could not be taken rather than that
#: the run is wrong. A timeout is a population that outgrew its bound on this
#: host, and a base-checkout failure is a scratch tree that could not be built;
#: neither is a finding about the delivered code, so neither holds without a
#: retry.
_ENVIRONMENTAL_GUARDS = (
    "landing measure unreadable: the base checkout could not be built",
)


def _is_environmental(guard: str) -> bool:
    """Whether a guard states an environmental failure rather than a finding.

    A refusal whose guard is a timeout or a base-checkout failure is judged
    again on a back-off; a refusal for an added regression, a blocking finding
    or a merge conflict stays hold until the code or the review moves.
    """
    text = str(guard or "")
    if text.startswith(_ENVIRONMENTAL_GUARDS):
        return True
    return (
        text.startswith("landing measure unreadable: ")
        and "exceeded its" in text
        and text.endswith("bound")
    )


def _retry_backoff(attempts: int) -> float:
    """Seconds to wait before judging an environmental refusal again.

    The wait doubles per repeated timeout and is capped, so a run whose host
    was briefly slow is retried soon while one that keeps timing out is retried
    no more often than the cap allows.
    """
    count = max(1, int(attempts or 1))
    try:
        waited = RETRY_BACKOFF_SECONDS * (2 ** (count - 1))
    except OverflowError:
        return RETRY_BACKOFF_CAP_SECONDS
    return min(waited, RETRY_BACKOFF_CAP_SECONDS)


def _retry_due(previous: Mapping[str, Any]) -> float | None:
    """When an environmental refusal recorded on the run may be judged again.

    ``None`` when the record carries no failure time, which is what a refusal
    stored before the retry clock was kept leaves: it says nothing about when
    it was made, so the run is judged once more rather than held on a guess.
    """
    failed_at = previous.get("failed_at")
    if isinstance(failed_at, bool) or not isinstance(failed_at, (int, float)):
        return None
    attempts = previous.get("attempts")
    count = (
        1 if isinstance(attempts, bool) or not isinstance(attempts, int) else attempts
    )
    return float(failed_at) + _retry_backoff(count)


def _refusal_holds(
    previous: Mapping[str, Any],
    code_stamp: str = "",
    review_stamp: str = "",
    now: float | None = None,
) -> bool:
    """Whether a recorded refusal still stands under this code and record.

    A refusal states the rule that made it and the record it read, and holds
    while both are unchanged. The one exception is a refusal whose guard is
    environmental: a timeout or a base-checkout failure is retried once its
    back-off has elapsed, with the code and the review unchanged, because
    waiting for either to move would hold a run behind a busy host. A refusal
    recorded before the stamps were kept carries none of them, so it says
    nothing about the current rule and the run is judged once more.
    """
    if not code_stamp or not review_stamp:
        return False
    if (
        str(previous.get("code_stamp") or "") != code_stamp
        or str(previous.get("review_stamp") or "") != review_stamp
    ):
        return False
    if not _is_environmental(str(previous.get("guard") or "")):
        return True
    due = _retry_due(previous)
    if due is None:
        return False
    return (time.time() if now is None else float(now)) < due


def _refusal_attempts(
    previous: Mapping[str, Any] | None, *, head: str, guard: str
) -> int:
    """How many times this head has been refused for the same kind of failure.

    Only an environmental refusal counts a repeat: a further timeout on the
    same head at the same kind of failure doubles its back-off, while a refusal
    for a finding starts the count again because it is a different judgement.
    """
    if not isinstance(previous, Mapping) or previous.get("status") != "refused":
        return 1
    if not recovery.same_revision(str(previous.get("head") or ""), head):
        return 1
    earlier = str(previous.get("guard") or "")
    if not (_is_environmental(earlier) and _is_environmental(guard)):
        return 1
    recorded = previous.get("attempts")
    count = (
        1 if isinstance(recorded, bool) or not isinstance(recorded, int) else recorded
    )
    return max(1, count) + 1


def _record_attempt(
    run_id: str,
    *,
    head: str,
    guard: str,
    status: str,
    code_stamp: str = "",
    review_stamp: str = "",
    previous: Mapping[str, Any] | None = None,
    now: float | None = None,
) -> None:
    """Keep a refusal on the live run, with what made it, so a later pass can
    tell a refusal that still stands from one the current rule would reverse.

    The record also keeps when the refusal was made and how many times this
    head has been refused for an environmental failure, because that pair is
    what a retry's back-off is counted from and how far it doubles.
    """

    def mutation(pointer: dict[str, Any]) -> dict[str, Any]:
        pointer["admission_landing"] = {
            "head": head,
            "guard": guard,
            "status": status,
            "code_stamp": code_stamp,
            "review_stamp": review_stamp,
            "attempts": _refusal_attempts(previous, head=head, guard=guard),
            "failed_at": time.time() if now is None else float(now),
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

    A record the measurement could not be taken for is not one: it states the
    bound the arm exceeded or the failure to build the base tree, and reusing
    it would refuse the retry the back-off exists to allow. Such a head is
    measured again, under the bound its own timed-out arm has just widened.
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
    if str(record.get("unreadable") or ""):
        return None
    return landing_measure.Measurement.from_dict(record)


def _measurement_record(run_id: str) -> dict[str, Any] | None:
    """One run's stored measurement, or ``None`` when it carries none."""
    try:
        record = json.loads(_measurement_path(run_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return dict(record) if isinstance(record, Mapping) else None


def _pass_history(
    pointers: Sequence[Mapping[str, Any]], project: str
) -> list[dict[str, Any]]:
    """This project's stored measurements, read once for a whole landing pass.

    A bound is sized from how long this project's populations actually take, so
    the history is the measurement record of each of the project's own live
    runs — not a walk of the runs directory, which holds thousands of other
    projects' and promoted runs' entries and is on shared storage. The pass
    reads this once and hands the same list to every run it measures, so the
    number of reads is the project's run count rather than the directory's
    entry count.
    """
    records: list[dict[str, Any]] = []
    if not project:
        return records
    for pointer in pointers:
        if str(pointer.get("project") or "") != project:
            continue
        record = _measurement_record(str(pointer.get("run_id") or ""))
        if record is not None:
            records.append(record)
    return records


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
    pointer: Mapping[str, Any],
    run_id: str,
    head: str,
    tree: str,
    stored: Sequence[Mapping[str, Any]] = (),
) -> Any:
    """Run the three suite readings for one delivered run.

    A run that armed no command has nothing to measure; the landing path keeps
    its earlier behaviour for such a run rather than inventing a command. A
    measurement that cannot be taken — no base revision, or a base checkout
    that cannot be built — yields an unreadable measurement so the caller
    refuses the landing instead of inferring a verdict from nothing.

    ``stored`` is the project's measurement history, read by the pass once for
    all its runs; a caller that names none measures with the floor as its only
    bound.
    """
    command = str(pointer.get("suite_command") or "").strip()
    if not command:
        return None
    base_sha = str(pointer.get("base_sha") or "").strip()
    project = str(pointer.get("project") or "")
    repo = Path(str(pointer.get("repo") or tree or ""))
    if not base_sha or not repo.is_dir():
        return landing_measure.Measurement(
            command=command,
            suite={},
            population={},
            unreadable="the run records no base revision to compare against",
            head=head,
            project=project,
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
                    stored=stored,
                    project=project,
                )
            except (OSError, ValueError) as exc:
                measured = landing_measure.Measurement(
                    command=command,
                    suite={},
                    population={},
                    unreadable=(f"the measurement raised {type(exc).__name__}: {exc}"),
                    head=head,
                    project=project,
                )
    except landing_measure.BaseCheckoutError as exc:
        measured = landing_measure.Measurement(
            command=command,
            suite={},
            population={},
            unreadable=f"the base checkout could not be built: {exc}",
            head=head,
            project=project,
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


#: Why the land step waives the plan's impl move. The landing pass ticks the
#: run's section done in ``_landing_beat``, and only after the merge this step
#: makes, so at the moment the step completes the plan's impl has not moved yet
#: and ``crew complete`` refuses a passing implement run without this. Recorded
#: on the ledger row of every run the pass lands.
LANDING_BEAT_REASON = (
    "the admission landing pass advances the run's plan section in its landing "
    "beat, after this merge; the impl has not moved at the moment of completion"
)


def _land_command(run_id: str) -> list[str]:
    """Run the coordinator's land command in one explicit fleet step.

    The revision the run's manifest delivers is presented to the promotion, so
    the step asserts the revision this pass read rather than leaving the
    promotion to read the worker's commit entry for itself. An entry carrying
    its subject after the revision, or several revisions on one line, is then
    the delivery it was to the pass, instead of a citation no store can resolve
    under the entry's own spelling.
    """
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
        *[
            token
            for revision in _delivered_revisions(run_id)
            for token in ("--commit", revision)
        ],
        "--no-impl-change",
        LANDING_BEAT_REASON,
        "--land",
    ]


def _delivered_revisions(run_id: str) -> tuple[str, ...]:
    """The revisions this run's manifest delivers, as the pass reads them.

    Read through the reader the pass used, because the two must agree on what a
    delivered run delivered: a land step that read the entries again would be a
    second reader, and a second reader is a second answer. Every entry the
    reader can place becomes a presented revision, so the promotion's boundary
    check covers the run's whole delivery and not only its tip.
    """
    try:
        pointer = runs.read_pointer(run_id)
    except (OSError, ValueError, CrewError):
        return ()
    classified = recovery.classify_pointer(pointer)
    revisions: list[str] = []
    for entry in classified.get("manifest_commits") or ():
        revision = _delivered_revision([entry])
        if revision and revision not in revisions:
            revisions.append(revision)
    return tuple(revisions)


def _land_timeout(run_id: str) -> float:
    """The wall-clock bound of one land step, from the run's own project config.

    The step runs the run's promotion gate inside the fleet allocation, so it
    is bounded by the same flight setting that bounds a measurement. A run whose
    pointer cannot be read is bounded by the default rather than left unbounded:
    a step with no bound is the stall the bound exists to prevent.
    """
    try:
        pointer = runs.read_pointer(run_id)
    except (OSError, ValueError, CrewError):
        return landing_measure.DEFAULT_MEASURE_TIMEOUT_SECONDS
    return _measure_timeout(pointer)


def _land(run_id: str) -> tuple[bool, str]:
    # The CLI calls the same promotion lease and merge index-lock retry a
    # coordinator uses. Its gate also runs inside this step, off the service.
    environment = os.environ.copy()
    source = str(Path(__file__).resolve().parents[2])
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (source, environment.get("PYTHONPATH", "")) if part
    )
    command = _land_command(run_id)
    timeout = _land_timeout(run_id)
    try:
        finished = _run_srun(
            command,
            capture_output=True,
            text=True,
            check=False,
            env=environment,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, (
            f"the land step exceeded its {timeout:g} s bound and was stopped: "
            + " ".join(command)
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
    stamp and the review record it judged are both unchanged. A refusal for an
    environmental failure — a measurement bound that was exceeded, or a base
    checkout that could not be built — is judged again on a doubling back-off
    with the code and the review unchanged, because neither would move while
    the host was merely busy. A manifest whose
    last commit entry names no revision is refused with that entry quoted and an
    obligation for the run's session, so the run is visible to a coordinator
    rather than dropped from the pass in silence. A delivered run whose head
    carries no review is recorded ``awaiting-review`` with the review reflex's
    own state beside it, so waiting for a review is a verdict on the record
    rather than a run the pass never mentions.
    """
    from reckon.crew.recovery_review_subject import select_review_for_head

    landed: list[str] = []
    obligations: list[dict[str, str]] = []
    deferred: list[str] = []
    measured_this_pass = False
    live = sorted(runs.list_live(), key=_delivery_order)
    # One history read per project for the whole pass, not one per run: the
    # run directory holds thousands of entries on shared storage, and the pass
    # exists to stay off that cost.
    history: dict[str, list[dict[str, Any]]] = {}
    for pointer in live:
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
                        previous=previous,
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
                    previous=previous,
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
            _record_awaiting_review(
                run_id,
                pointer,
                head=head,
                code_stamp=code_stamp,
                review_stamp=review_stamp,
            )
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
                previous=previous,
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
                previous=previous,
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
            if project not in history:
                history[project] = _pass_history(live, project)
            stored = history[project]
            measured = _measure_delivery(pointer, run_id, head, tree, stored)
            measured_this_pass = measured_this_pass or measured is not None
            if measured is not None:
                # A run measured in this pass is part of its project's history
                # for the runs judged after it.
                stored.append(measured.as_dict())
        measure_guard = _measure_guard(measured)
        if measure_guard:
            _record_attempt(
                run_id,
                head=head,
                guard=measure_guard,
                status="refused",
                code_stamp=code_stamp,
                review_stamp=review_stamp,
                previous=previous,
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
            previous=previous,
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
                previous=previous,
            )
            obligations.append(
                {
                    "run_id": run_id,
                    "session": str(pointer.get("session") or ""),
                    "guard": guard,
                }
            )
    return {"landed": landed, "obligations": obligations, "deferred": deferred}
