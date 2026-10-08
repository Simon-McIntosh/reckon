import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import click

from reckon.crew_dispatch_commands import (
    _crew_modules,
    _emit,
    _emit_crew_result,
    _lane_paused_detail,
    _resolved_flight,
    _resolved_gc_repo,
    _resolved_session,
    crew,
)


@crew.command(name="resume-ready")
@click.option("--project", required=True, help="Project whose ready runs to resume.")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Report what would be resumed without resuming anything.",
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_resume_ready(project, dry_run, pretty):
    """Resume when a provider hold or declared external wait has ended; sweep all blocked runs, answer none with advice.

    Idempotent and cheap: eligibility is computed from records already on disk,
    so nothing is spent to discover it and a second pass over the same fleet
    reports nothing to do. The follower runs this on a cadence, which is how
    the recovery happens without anyone noticing the outage; this command is
    the same sweep by hand.
    """
    from reckon.crew.node import CrewError
    from reckon.crew.resumption import sweep

    try:
        report = sweep(project, dry_run=dry_run)
    except CrewError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_crew_result(report, pretty)



@crew.command(name="unwatch")
@click.option("--project", required=True, help="Project whose watcher to stop.")
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_unwatch(project, pretty):
    """Stop the registered project watcher and release its live seat."""
    from reckon.crew.node import CrewError
    from reckon.crew.recovery import unwatch

    try:
        result = unwatch(project)
    except CrewError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_crew_result(result, pretty)



@crew.command(name="placement")
@click.option(
    "--ensure",
    is_flag=True,
    help=(
        "Hold this host's placement reservation and return, instead of "
        "reporting one. Idempotent: a reservation already held and still in "
        "the system is reported and nothing is submitted."
    ),
)
@click.option(
    "--replace",
    "replace_job",
    default=None,
    help=(
        "Point this host's reservation at an already-running job, replacing "
        "whatever record is held. Refuses a job that is pending, that belongs "
        "to another user, or that the scheduler does not know."
    ),
)
@click.option(
    "--session",
    default=None,
    help="Session asking for the reservation; recorded with it when first held.",
)
@click.option(
    "--project",
    default=None,
    help="Project whose reservation to report or hold; omitted reads the "
    "pre-project host-global record, which belongs to no project.",
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_placement(ensure, replace_job, session, project, pretty):
    """Report or hold the one reservation a project places its workers into.

    One reservation held once per project, published into the crew state that
    project's sessions read, is what keeps concurrency a matter of sizing an
    allocation rather than of submitting more of them. Workers then run inside
    it as steps, so the cluster queue sees one entry and never sees a step.

    It is keyed by project because a reservation admits one project's workers
    and its roster bounds them. Keying it by host made a per-project decision
    carry a fleet-wide ceiling, counting every project's workers against one
    allocation that only one of them held.

    ``--replace`` moves the record to an already-running job of your own, which
    is what moves the fleet to a new allocation before the old one drains; the
    ensure path only replaces a record whose own job has left the queue.
    """
    from reckon.crew import runs as runs_module
    from reckon.crew.node import CrewError

    if ensure and replace_job is not None:
        raise click.ClickException(
            "--ensure holds a reservation and --replace moves an existing one; "
            "name only one."
        )
    if replace_job is not None:
        from reckon.crew import placement as placement_module

        try:
            result = placement_module.replace_reservation(
                job_id=replace_job, session=session, project=project
            )
        except CrewError as exc:
            raise click.ClickException(str(exc)) from exc
        _emit_crew_result(result, pretty)
        return
    if ensure:
        try:
            result = runs_module.ensure_placement_reservation(
                session=session, project=project
            )
        except CrewError as exc:
            raise click.ClickException(str(exc)) from exc
        _emit_crew_result(result, pretty)
        return
    from reckon.crew import placement as placement_module

    record = placement_module.read_reservation(project)
    _emit_crew_result(
        {
            "project": project,
            "job_id": (record or {}).get("job_id"),
            "held": bool(record),
            "record": record,
            "ensure_line": runs_module.placement_ensure_line(),
        },
        pretty,
    )



@crew.command(name="list")
@click.option("--project", default=None, help="Return runs for one project only.")
@click.option("--phase", default=None, help="Return runs in one phase only.")
@click.option(
    "--session",
    default=None,
    help="Mark rows dispatched by this session as mine, and report its follower.",
)
@click.option("--mine", is_flag=True, help="Return only rows this --session owns.")
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_list(project, phase, session, mine, pretty):
    """List live run pointers, not roster members or session-closure state.

    Every row names its owning session and, when one is supplied, whether that
    session owns it. The first question a recovering orchestrator asks is which
    of these runs are its own, and a node name is not evidence of ownership.
    """
    from reckon.crew.recovery import agent_label
    from reckon.crew.runs import project_watch_visibility

    if mine and session is None:
        raise click.ClickException("--mine needs --session to say whose runs to keep.")

    crew_module, _ = _crew_modules()
    project_records = crew_module.list_live(project=project)
    runs = []
    for record in project_records:
        if phase is not None and str(record.get("phase") or "") != phase:
            continue
        owner = str(record.get("session") or "") or None
        if mine and owner != session:
            continue
        classified = crew_module.classify_pointer(record)
        runs.append(
            {
                "run_id": record.get("run_id"),
                "node": (record.get("node") or {}).get("id"),
                "project": record.get("project"),
                "plan": (record.get("node") or {}).get("plan"),
                "session": owner,
                # The worker's own session, resolved rather than passed
                # through: a listing is exactly where a reader decides a run is
                # unresumable, and the pointer field is null until something
                # folds the stream in.
                **_resolved_session(str(record.get("run_id") or ""), record),
                "member": record.get("member"),
                "agent": agent_label(record) or None,
                "mine": None if session is None else owner == session,
                "backend": record.get("backend"),
                "launch": record.get("launch"),
                "phase": record.get("phase"),
                "worktree": record.get("worktree"),
                "manifest_path": record.get("manifest_path"),
                "classification": classified.get("classification"),
                "process_alive": classified.get("process_alive"),
                "elapsed_seconds": classified.get("elapsed_seconds"),
                "log_age_seconds": classified.get("log_age_seconds"),
                "budget_seconds": classified.get("budget_seconds"),
                "budget_overrun": classified.get("budget_overrun"),
                # The token allowance and the separately named wall-clock
                # ceiling are distinct: the budget verdict is denominated in
                # generated tokens when a token budget is set, while
                # ceiling_overrun catches a hang under the ceiling's own name.
                "budget_tokens": classified.get("budget_tokens"),
                "generated_tokens": classified.get("generated_tokens"),
                "budget_overrun_tokens": classified.get("budget_overrun_tokens"),
                "hang_ceiling_seconds": classified.get("hang_ceiling_seconds"),
                "ceiling_overrun": classified.get("ceiling_overrun"),
                "manifest_status": classified.get("manifest_status"),
                "next_action": classified.get("next_action"),
            }
        )
    payload = {"runs": runs}
    if project is not None:
        payload["watcher"] = project_watch_visibility(project, session=session)
    else:
        projects = sorted(
            {
                str(record.get("project") or "")
                for record in project_records
                if record.get("project")
            }
        )
        payload["watchers"] = [
            project_watch_visibility(project_name, session=session)
            for project_name in projects
        ]
    _emit_crew_result(payload, pretty)



@crew.command(name="directory")
@click.option("--project", default=None, help="Return coordinators for one project.")
@click.option("--run", "run_id", default=None, help="Resolve one live run's owner.")
@click.option("--node", "node_id", default=None, help="Resolve one live node's owner.")
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_directory(project, run_id, node_id, pretty):
    """List live coordinator ownership across repositories, not individual run state."""
    from reckon.crew.directory import DirectoryError, directory

    try:
        result = directory(project, run_id=run_id, node_id=node_id)
    except DirectoryError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_crew_result(result, pretty)



_PATH_KINDS = ("config-home", "reports", "runs", "live", "reviews")



@crew.command(name="path")
@click.option(
    "--kind",
    "kind",
    required=True,
    type=click.Choice(_PATH_KINDS),
    help="Storage kind whose absolute path to print.",
)
@click.option(
    "--project",
    default=None,
    help="Project to resolve under a project-keyed kind.",
)
@click.option(
    "--run",
    "run_id",
    default=None,
    help="Run to resolve under a run-keyed kind.",
)
def crew_path(kind, project, run_id):
    """Print where reckon keeps one kind of file, as a plain path.

    Consumers outside this repository need the location, not a JSON envelope
    they unwrap before use, so this writes the resolved absolute path and a
    newline on it. Every value comes from the resolver the crew store already
    uses for that kind, so when a directory moves, this verb follows. A
    selector the kind does not accept is refused rather than ignored: a
    silently dropped `--run` answers a different question than the one asked.
    """
    from reckon._store import _config_home
    from reckon.crew.review import review_path, review_store_root
    from reckon.crew.runs import live_dir, pointer_path, reports_dir, run_dir, runs_dir

    if kind == "config-home":
        if project is not None or run_id is not None:
            raise click.ClickException(
                "--kind config-home takes no selector, it is the home every "
                "other kind resolves under."
            )
        path = _config_home()
    elif kind == "reports":
        if run_id is not None:
            raise click.ClickException(
                "--kind reports takes --project only, reports are keyed by "
                "project rather than by run."
            )
        path = reports_dir() / project if project is not None else reports_dir()
    elif kind in ("runs", "live"):
        if project is not None:
            raise click.ClickException(
                f"--kind {kind} takes --run only, the form of the path is "
                "decided by the run it names."
            )
        if run_id is None:
            path = runs_dir() if kind == "runs" else live_dir()
        else:
            path = run_dir(run_id) if kind == "runs" else pointer_path(run_id)
    else:  # kind == "reviews"; click.Choice admits nothing else
        if project is None:
            raise click.ClickException(
                "--kind reviews requires --project, the review store is keyed "
                "by project first."
            )
        path = (
            review_store_root() / project
            if run_id is None
            else review_path(project, run_id)
        )
    click.echo(str(path))



def _reviewed_head_from_run_records(project: str, reviewed_run_id: str) -> str:
    """The revision a run with no live pointer was reviewed at, from its records.

    A promoted run has no live pointer, so the run's own committed records
    answer which revision its stored review is about: the revision its ledger
    row records as the tip its promotion landed, or the head its stored review
    carries. Both are the run's own account of itself, and a row that names no
    revision falls through to the head the review recorded.

    The caller's working directory is not a third source. An absent tree reads
    as the empty string, which is ``.``, so a head resolved from it belongs to
    whatever repository the operator happened to stand in, and the review
    selected against that revision is another run's — which is why this
    resolves both records here rather than passing the absence on.

    A run whose records name no revision, and a ledger that cannot be read, are
    refused by name: a revision nobody recorded cannot be answered, and writing
    a disposition against a guessed one reports a finding retired on a diff
    that was never read.
    """
    from reckon import ledger as ledger_module
    from reckon._store import CorruptEnvelopeError
    from reckon.crew import review as review_module

    try:
        ledger_data, _version = ledger_module.load(project)
    except (
        OSError,
        ValueError,
        ledger_module.LedgerError,
        CorruptEnvelopeError,
    ) as exc:
        raise ValueError(
            f"cannot read the ledger for project {project!r}, which records "
            f"the revision run {reviewed_run_id!r} landed at: {exc}"
        ) from exc
    for row in ledger_data.get("runs") or []:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("run_id") or "") != reviewed_run_id:
            continue
        promoted = str(row.get("promoted_revision") or "").strip()
        if promoted:
            return promoted
        break
    try:
        _path, record = review_module.stored_record(project, reviewed_run_id)
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"cannot read the stored review for run {reviewed_run_id!r}: {exc}"
        ) from exc
    if isinstance(record, Mapping):
        _carried, _base, carries_head, head = review_module.carried_revision_pair(
            record
        )
        if carries_head and head:
            return head
    raise ValueError(
        f"run {reviewed_run_id!r} has no live pointer, and neither its ledger "
        f"row in project {project!r} nor its stored review names a revision; "
        "refusing to take one from the working directory, which would answer a "
        "review belonging to whatever repository the caller stands in"
    )



@crew.command(name="dispose")
@click.option(
    "--project",
    required=True,
    help="Project whose review store holds the run's record.",
)
@click.option(
    "--run",
    "reviewed_run_id",
    required=True,
    help="Run the review is about, not the run that reviewed it.",
)
@click.option(
    "--dimension",
    required=True,
    help="Review dimension carrying the sub-floor finding.",
)
@click.option(
    "--kind",
    required=True,
    help="Disposition kind: folded with --node, or exempted with --reason.",
)
@click.option(
    "--node",
    "node_id",
    default=None,
    help="Node id the finding was folded into, for --kind folded.",
)
@click.option(
    "--reason",
    default=None,
    help="Why the finding is not being acted on, for --kind exempted.",
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_dispose(project, reviewed_run_id, dimension, kind, node_id, reason, pretty):
    """Record the disposition one sub-floor review dimension carries.

    A review dimension below the floor flight configuration declares for it is
    an obligation row until an entry in the closed set answers it: a fold
    naming the dispatched node the finding went into, or an exemption naming
    why it is not being acted on. This is that entry's writer, so a
    coordinator retires the row through a command rather than editing the
    review store by hand or promoting the close over it.

    Every refusal is the store's own, and nothing is written on one: an
    unknown dimension, a kind outside the closed set, a fold naming no node,
    an exemption carrying no reason, and either kind supplied with the other's
    field all exit non-zero with the reason stated. The record rewritten is the
    one the sub-floor duty reader selects — the same reading, taken through the
    same function and arguments the duty was built from — so a duty row raised
    is one this command can retire, and a store holding a legacy copy beside a
    revision-keyed record for the same head cannot take the entry in the copy
    nobody reads. A run with no live pointer, the ordinary case once it is
    promoted, is answered from its own records rather than from the directory
    the operator stands in; see :func:`_reviewed_head_from_run_records`.
    """
    from reckon.crew import obligations as obligations_module
    from reckon.crew import recovery, runs
    from reckon.crew import review as review_module

    try:
        pointer = runs.read_pointer(reviewed_run_id)
    except runs.CrewError:
        pointer = None
    if pointer is None:
        # A promoted run has no live pointer, and an empty mapping here is a
        # tree of ``.``: the head would be resolved from the caller's own
        # repository and the review selected against a revision belonging to
        # something else.
        tree = None
        try:
            head = _reviewed_head_from_run_records(project, reviewed_run_id)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
    else:
        # The revision the run's work is at, named by the refusal below when the
        # store's record describes another; the record itself is selected
        # through the reading both readers share.
        head, _tree = recovery._review_head_and_tree(pointer)
    try:
        if pointer is None:
            stored, described = recovery.select_review_for_head(
                project, reviewed_run_id, head, tree=tree
            )
        else:
            stored, described = obligations_module.stored_review_for_run(
                project, reviewed_run_id, pointer
            )
    except (OSError, ValueError) as exc:
        raise click.ClickException(
            f"cannot read the stored review for run {reviewed_run_id!r}: {exc}"
        ) from exc
    if stored is None:
        if described:
            raise click.ClickException(
                f"the stored review for run {reviewed_run_id!r} describes "
                f"{described}, while the run's work is at {head}; a disposition "
                "recorded now would answer a review its own head does not name"
            )
        raise click.ClickException(
            f"no stored review for run {reviewed_run_id!r} in project "
            f"{project!r} to record a disposition against"
        )
    # The head the selected record carries, not the one resolved from the tree:
    # a record that names no revision was selected by the reader's fallback, and
    # naming a revision for it would leave the writer nothing to read back.
    _, _, carries_head, stored_head = review_module.carried_revision_pair(stored)
    reviewed_head_sha = stored_head if carries_head and stored_head else None
    try:
        path = review_module.record_dimension_disposition(
            project,
            reviewed_run_id,
            dimension,
            kind=kind,
            node=node_id,
            reason=reason,
            reviewed_head_sha=reviewed_head_sha,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    _, record = review_module.stored_record(
        project, reviewed_run_id, reviewed_head_sha=reviewed_head_sha
    )
    dispositions = (record or {}).get(review_module.DIMENSION_DISPOSITIONS_KEY) or {}
    _emit_crew_result(
        {
            "project": project,
            "run_id": reviewed_run_id,
            "dimension": dimension,
            "path": str(path),
            "disposition": dispositions.get(str(dimension or "").strip().lower()),
        },
        pretty,
    )



@crew.command(name="check-manifest")
@click.option("--run", "run_id", required=True, help="Run id whose manifest to check.")
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_check_manifest(run_id, pretty):
    """Judge a delivered manifest against its own node while its writer can fix it.

    The promotion contract reads the same manifest hours after the worker's
    process has ended, when the only party who can satisfy a finding is a
    coordinator editing an artifact it did not author. This reads the run's
    live pointer, rebuilds the node it records and reports the audit findings,
    so the refusal reaches whoever still holds the pen.

    The scope test resolves declared write paths against the run's own worktree
    and repository, read from the live pointer, because a declaration naming a
    location absolutely only maps to the repository-relative path a manifest
    records when it is resolved against the tree that run worked in. Resolving
    against the caller's working directory instead would judge the same
    manifest differently depending on where the check was invoked from, which
    is the drift between this check and promotion that the shared mapping
    exists to remove.
    """
    from reckon.crew.dispatch import _recorded_task_node
    from reckon.crew.runs import read_pointer

    crew_module, _ = _crew_modules()
    try:
        record = read_pointer(run_id)
        node = _recorded_task_node(record)
    except crew_module.CrewError as exc:
        raise click.ClickException(str(exc)) from exc
    manifest_path = str(node.manifest_path or "")
    text = ""
    if not manifest_path:
        findings = [f"run {run_id} records no manifest path to check"]
    else:
        try:
            text = Path(manifest_path).read_text(encoding="utf-8")
        except OSError as exc:
            findings = [f"manifest {manifest_path!r} could not be read: {exc}"]
        else:
            worktree = Path(record["worktree"]) if record.get("worktree") else None
            repository = Path(record["repo"]) if record.get("repo") else worktree
            findings = list(
                crew_module.audit_manifest(
                    text, node, worktree=worktree, repository=repository
                )["findings"]
            )
    _emit(
        {
            "ok": not findings,
            "run_id": run_id,
            "node": node.id,
            "manifest_path": manifest_path,
            "findings": findings,
        },
        pretty,
    )
    if findings:
        raise click.exceptions.Exit(1)



WIDENABLE_PHASE = "blocked"



WIDEN_REFUSING_MANIFEST_STATUSES = frozenset({"complete", "failed"})



WIDEN_RULE = (
    "only a run whose worker process has stopped on a blocked fence is widened, "
    "because a live worker is already writing against the boundary a widening "
    "would move"
)



def _manifest_reported_status(record: Mapping[str, Any]) -> str:
    """The status a run's own manifest reports, or "" when none can be read.

    A run launched through a backend folds its phase from its stream's terminal
    event, so a worker that writes ``status: blocked`` and ends its turn folds
    to ``complete`` and the one place the block is stated -- the delivery the
    worker wrote -- is never consulted. This reads that file, because it is the
    authority whose mirror the folded phase is. A manifest that is absent,
    unreadable, older than the attempt that is reading it, or still carrying the
    dispatch contract's unsubstituted status choice reports no status rather
    than a wrong one, and eligibility then rests on the phase alone.
    """
    from reckon.crew.reports import (
        ManifestParseError,
        manifest_status_is_template,
        parse_manifest,
    )

    path = str(record.get("manifest_path") or "").strip()
    if not path:
        return ""
    crew_module, _ = _crew_modules()
    if not crew_module._manifest_freshness(record)[1]:
        # A resumed attempt points at the same delivery path, so a terminal
        # status left there by the attempt before it is not this attempt's
        # verdict. Only a manifest written after the attempt began is read.
        return ""
    try:
        delivered = parse_manifest(Path(path).read_text(encoding="utf-8"))
    except (OSError, ManifestParseError):
        return ""
    status = str(delivered.get("status") or "").strip().lower()
    return "" if manifest_status_is_template(status) else status



def _widen_eligibility(record: Mapping[str, Any]) -> tuple[str, str, str]:
    """The state refusing this run's fence, its manifest status, and its remedy.

    The first value is the state that refuses a widening, or ``""`` when the
    fence may move; the second is the manifest's own reported status, so the
    answer can name which of the two accounts authorised the write; the third is
    what a refused caller can do next, and is empty when the refusal leaves no
    move open.

    One rule decides this, and the command's own documentation states the same
    sentence: only a run whose worker process has stopped on a blocked fence is
    widened. Its two halves are read from different places on purpose. Whether
    the run has stopped is read from the worker the record names, never from the
    phase -- a phase mirrors the stream that folded it and the manifest the run
    wrote, so a live worker under a blocked-looking phase is exactly the writer
    this refusal exists for. Whether the run stated a block is read from the
    folded phase and from the manifest the run delivered. A run is wideniable
    when either reports ``blocked``, because a run launched through a backend
    folds its phase from a terminal stream event and a worker that writes
    ``status: blocked`` before ending its turn leaves the two disagreeing. A
    manifest reporting complete or failed refuses on its own -- a finished run
    has no scope decision outstanding, and a wider fence would be granted to
    work that has already ended.
    """
    phase = str(record.get("phase") or "")
    reported = _manifest_reported_status(record)
    if reported in WIDEN_REFUSING_MANIFEST_STATUSES:
        return f"it reports {reported!r} in its own manifest", reported, ""
    if WIDENABLE_PHASE not in (phase, reported):
        label = phase or "unphased"
        return f"it reads {label!r}, not {WIDENABLE_PHASE!r}", reported, ""
    running = _widen_running_worker(record)
    if running:
        return (
            f"it records a worker still running as pid {running}",
            reported,
            (
                "Wait for that process to exit and widen again, or resume the "
                "run with `crew resume` once its turn has ended"
            ),
        )
    return "", reported, ""



def _widen_running_worker(record: Mapping[str, Any]) -> str:
    """The pid of this run's worker while it is still running, or ``""`` once gone.

    The fence a widening moves is the boundary the run's worker writes against,
    so the process that must no longer exist is the one the record names, and the
    read goes through the single helper every liveness decision about a run
    takes: a pid the kernel has since handed to a different process does not
    answer for the worker this record means.

    A record that names no process is admitted rather than refused. ``None`` is
    not a running worker, the pointer is written before its worker is spawned,
    and a widening already requires a block stated in a manifest the run itself
    delivered -- which no unspawned worker can have written.
    """
    from reckon.crew.runs import record_process_alive

    if record_process_alive(record) is not True:
        return ""
    return str(record.get("pid") or "")



def _widen_refusal(run_id: str, state: str, remedy: str, *, where: str) -> str:
    """Compose the refusal naming the state, where it was read, and the next move."""
    message = (
        f"run {run_id!r} cannot have its fence widened{where}: {state}; {WIDEN_RULE}"
    )
    return f"{message}. {remedy}" if remedy else message



@crew.command(name="widen")
@click.option("--run", "run_id", required=True, help="Run whose fence to widen.")
@click.option(
    "--write-path",
    "write_paths",
    multiple=True,
    required=True,
    help="Path to add to the run's declared write scope. Repeat for each path.",
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_widen(run_id, write_paths, pretty):
    """Add a path to a blocked run's own fence, in place and without a redispatch.

    A worker that blocks because its write scope was drawn too narrowly can be
    answered but not re-scoped: ``--write-path`` is read once, at dispatch, and
    no later command touches scope, so the fence is fixed for the life of the
    run. The coordinator's remaining moves were a redispatch that discards the
    run's session and whatever it had committed, or a hand-edit of the live
    pointer. This is that edit, declared and auditable.

    Only the run's own pointer changes, and only its ``write_paths``: every
    other field — the session id, the recorded commits, the worktree — is
    carried through byte for byte, so the widened run resumes on its own
    session with its own work intact. The field written is the one promotion
    reads, so a scope granted here is the scope the promotion validator honours.

    The run must have stopped, and it must have stated a block. Whether it has
    stopped is read from the worker process the pointer records -- a run whose
    worker is still running is refused whatever its phase and its manifest say,
    because that worker is already writing against the boundary a widening would
    move. Whether it stated a block is read from two sources rather than from the
    folded phase alone: the phase, and the status the run's own manifest reports.
    A run whose manifest reports ``blocked`` is wideniable even when its phase
    has folded to ``complete``, because a run launched through a backend folds
    its phase from its stream's terminal event -- so a worker that ends its turn
    after writing ``status: blocked`` folds to ``complete``, and its own
    delivery is the only place the block is stated. A manifest reporting
    ``complete`` or ``failed`` is refused, because a finished run has no scope
    decision outstanding. Eligibility is read twice -- once on the pointer as it
    stands and again on the record read under the per-run lock -- so a run that
    reaches either read in a non-wideniable state is refused rather than widened
    in place. Added paths are checked against other binding live claims during
    that locked write; a conflict names its holder and leaves this pointer
    unchanged.
    """
    crew_module, _ = _crew_modules()
    try:
        record = crew_module.read_pointer(run_id)
    except crew_module.CrewError as exc:
        raise click.ClickException(str(exc)) from exc

    state, manifest_status, remedy = _widen_eligibility(record)
    if state:
        raise click.ClickException(_widen_refusal(run_id, state, remedy, where=""))
    if not isinstance(record.get("node"), Mapping):
        raise click.ClickException(
            f"live pointer for {run_id!r} records no node holding a write scope"
        )

    requested = [str(path).strip() for path in write_paths if str(path).strip()]
    added: list[str] = []

    def widen(pointer: dict[str, Any]) -> dict[str, Any]:
        # The eligibility above was read before the per-run lock, so it is a claim
        # about the pointer as it was, not as it is. Re-check it on the record this
        # mutation read under the lock: between the two reads a run can leave the
        # wideniable state and start writing against its boundary, and widening
        # there would move that boundary under a live process. Raising before the
        # write leaves the pointer as this mutation found it.
        locked_state, _, locked_remedy = _widen_eligibility(pointer)
        if locked_state:
            raise click.ClickException(
                _widen_refusal(
                    run_id,
                    locked_state,
                    locked_remedy,
                    where=" at the pointer write, and nothing was written",
                )
            )
        node = dict(pointer.get("node") or {})
        declared = [str(path) for path in node.get("write_paths") or ()]
        for path in requested:
            if path not in declared:
                declared.append(path)
                added.append(path)
        if added:
            from reckon.crew.dispatch import refuse_widen_scope_conflicts

            try:
                refuse_widen_scope_conflicts(pointer, added)
            except crew_module.CrewError as exc:
                raise click.ClickException(str(exc)) from exc
        node["write_paths"] = declared
        pointer["node"] = node
        return pointer

    updated = crew_module._mutate_pointer(run_id, widen)
    _emit_crew_result(
        {
            "run_id": run_id,
            "phase": str(updated.get("phase") or ""),
            # The status the run's own manifest reported. It is carried because
            # the phase alone can read complete here: a widening authorised by
            # the manifest would otherwise look, in this very output, like a
            # widening of a finished run.
            "manifest_status": manifest_status,
            "node": str((updated.get("node") or {}).get("id") or ""),
            "session_id": str(updated.get("session_id") or ""),
            "added": added,
            "write_paths": [
                str(path)
                for path in (updated.get("node") or {}).get("write_paths") or ()
            ],
        },
        pretty,
    )



@crew.command(name="drain")
@click.option("--project", required=True, help="Project whose live pointers to drain.")
@click.option(
    "--session",
    default=None,
    help=(
        "Count only this session's outstanding runs while reporting peer-session "
        "rows separately. Omit for the project-wide count."
    ),
)
@click.option(
    "--leave",
    "leaves",
    multiple=True,
    metavar="RUN=DISPOSITION",
    help=(
        "Record why a live run remains: handed-off or still-working. "
        "Repeat for each deliberate remainder."
    ),
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_drain(project, session, leaves, pretty):
    """Report the session-closure count and dispositions for live run pointers."""
    crew_module, _ = _crew_modules()
    requested = []
    for leave in leaves:
        run_id, separator, disposition = leave.partition("=")
        if not separator or not run_id.strip() or not disposition.strip():
            raise click.ClickException(f"--leave {leave!r} must be RUN=DISPOSITION")
        if disposition.strip() not in crew_module.RUN_DRAIN_DISPOSITIONS:
            allowed = ", ".join(crew_module.RUN_DRAIN_DISPOSITIONS)
            raise click.ClickException(
                f"run disposition {disposition.strip()!r} is not one of {allowed}"
            )
        requested.append((run_id.strip(), disposition.strip()))

    try:
        recorded = [
            crew_module.record_run_disposition(
                run_id,
                disposition,
                project=project,
                session=session,
            )
            for run_id, disposition in requested
        ]
        result = crew_module.drain(project, session=session)
    except crew_module.CrewError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_crew_result(
        {
            **result,
            "recorded": [
                {
                    "run_id": row.get("run_id"),
                    "disposition": row.get("closure_disposition"),
                }
                for row in recorded
            ],
        },
        pretty,
    )



@crew.command(name="ack")
@click.option(
    "--run",
    "run_id",
    required=True,
    help="Run whose obligations are deliberately deferred; live or promoted.",
)
@click.option(
    "--reason",
    required=True,
    help="Why the deferral is deliberate, for example awaiting a peer's release.",
)
@click.option(
    "--until",
    required=True,
    help="ISO-8601 instant the deferral expires; the obligation returns after it.",
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_ack(run_id, reason, until, pretty):
    """Defer one run's obligations until an instant, recording why.

    A live run's deferral is written beside its pointer; one for a run already
    promoted is written under the crew home and swept once its instant passes.
    """
    crew_module, _ = _crew_modules()
    from reckon.crew import runs as runs_module

    try:
        updated = runs_module.record_run_acknowledgement(run_id, reason, until)
    except crew_module.CrewError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_crew_result(
        {
            "run_id": run_id,
            "acknowledgement": updated.get("acknowledgement"),
        },
        pretty,
    )



@crew.group(name="suite")
def crew_suite():
    """The project's declared suite: run it once, or waive its hold."""
    return None



def _suite_project_root(project: str, checkout_path: Path | None) -> Path:
    """Return the checkout root a project's suite runs and is recorded in.

    A project's registered mount decides it, never the caller's enclosing
    checkout, so a run started from elsewhere still lands its record where the
    promotion that reads it will look. A project with no mount needs an explicit
    ``--checkout-path``, because a suite run filed under the wrong root would be
    invisible to the very promotion it is meant to answer.
    """
    from reckon.crew.dispatch import project_mount_repository

    if checkout_path is not None:
        return checkout_path.expanduser().resolve()
    mount = project_mount_repository(project)
    if mount is None:
        raise click.ClickException(
            f"project {project!r} has no registered mount; pass --checkout-path "
            "naming the repository its suite runs in"
        )
    return mount



@crew_suite.command(name="run")
@click.option("--project", required=True, help="Project whose declared suite to run.")
@click.option(
    "--checkout-path",
    default=None,
    type=click.Path(path_type=Path),
    help="Repo root whose suite is run (default: the registered mount).",
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_suite_run(project, checkout_path, pretty):
    """Run the project's declared suite under its budget and record the result."""
    from reckon.crew import standing_suite

    _, flight_module = _crew_modules()
    root = _suite_project_root(project, checkout_path)
    try:
        config = flight_module.resolve(project, checkout_path=root).config
    except flight_module.FlightConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    declaration = flight_module.review_suite(config)
    if declaration is None:
        raise click.ClickException(
            f"project {project!r} declares no review.suite command, so there is "
            "nothing to run"
        )
    log_path = (
        standing_suite.suite_runs_dir(root, project) / f"suite-{time.time_ns()}.log"
    )
    record = standing_suite.run(root, declaration, log_path)
    standing_suite.record(root, project, record)
    _emit_crew_result({"project": project, **record}, pretty)



@crew_suite.command(name="waive")
@click.option("--project", required=True, help="Project whose hold is waived.")
@click.option("--reason", required=True, help="Why the hold is lifted.")
@click.option("--who", default="lead", show_default=True, help="Who waived it.")
@click.option(
    "--checkout-path",
    default=None,
    type=click.Path(path_type=Path),
    help="Repo root whose state is written (default: the registered mount).",
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_suite_waive(project, reason, who, checkout_path, pretty):
    """Record a waiver that lifts the standing-suite hold."""
    from reckon.crew import standing_suite

    if not reason.strip():
        raise click.ClickException("--reason must not be empty")
    root = _suite_project_root(project, checkout_path)
    path = standing_suite.record_waiver(root, project, who=who, why=reason)
    _emit_crew_result({"project": project, "waiver": str(path)}, pretty)



@crew.command(name="gc")
@click.option(
    "--repo",
    default=None,
    type=click.Path(path_type=Path),
    help="Repository whose managed worktrees are inspected.",
)
@click.option(
    "--project",
    default=None,
    help="Project ledger used for transient run cleanup; all local ledgers by default.",
)
@click.option(
    "--integrated-into",
    default="HEAD",
    show_default=True,
    help="Revision that must contain a worktree commit before removal.",
)
@click.option(
    "--retention-days",
    type=click.IntRange(min=0),
    default=30,
    show_default=True,
    help="Keep promoted run directories for at least this many days.",
)
@click.option(
    "--apply",
    is_flag=True,
    help="Perform eligible removals; omission reports the exact dry run.",
)
@click.option(
    "--confirm-cross-repo",
    is_flag=True,
    help=(
        "Allow --repo to disagree with --project's registered checkout, "
        "stating the cross-pairing deliberately."
    ),
)
@click.option(
    "--scratch",
    is_flag=True,
    help=(
        "Survey the node-local scratch directories runs own, and the stray "
        "trees no run can be attributed to. A directory is removed only when "
        "its run has no live pointer and a terminal record; never by age."
    ),
)
@click.option(
    "--run",
    "run_id",
    default=None,
    help=(
        "Confine the sweep to this run's own worktree, pointer and run "
        "directory, so clearing one held tree cannot reach a peer's."
    ),
)
@click.option(
    "--pin-unique-commits",
    is_flag=True,
    help=(
        "Release a dirty worktree whose commits have no patch-equivalent on "
        "--integrated-into by first pinning those commits to an archive ref "
        "under refs/reckon/archive/; needs --apply to act."
    ),
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_gc(
    repo,
    project,
    integrated_into,
    retention_days,
    apply,
    confirm_cross_repo,
    scratch,
    run_id,
    pin_unique_commits,
    pretty,
):
    """Report workspaces whose integrated state makes them disposable; remove on request."""
    crew_module, flight_module = _crew_modules()
    if scratch and run_id:
        raise click.ClickException(
            "--run names one run's worktree, which the scratch survey does "
            "not cover; drop either --run or --scratch"
        )
    try:
        repo_root = _resolved_gc_repo(
            crew_module, flight_module, repo, project, confirm_cross_repo
        )
        if scratch:
            from reckon.crew.routing import garbage_collect_scratch

            report = garbage_collect_scratch(
                repo=repo_root, project=project, apply=apply
            )
        else:
            report = crew_module.garbage_collect(
                repo=repo_root,
                project=project,
                integrated_into=integrated_into,
                retention_days=retention_days,
                apply=apply,
                pin_unique_commits=pin_unique_commits,
                run_id=run_id,
            )
    except crew_module.CrewError as exc:
        partial = getattr(exc, "partial", None)
        if partial is None:
            raise click.ClickException(str(exc)) from exc
        # A sweep that stops mid-pass has already removed and refused trees: a
        # gc that has deleted something must say so before it exits nonzero.
        _emit({"ok": False, "error": str(exc), **partial}, pretty)
        raise click.exceptions.Exit(1) from exc
    _emit_crew_result(report, pretty)



@crew.command(name="resume")
@click.option("--run", "run_id", required=True, help="Run id to answer.")
@click.option("--advice", required=True, help="The orchestrator's answer.")
@click.option(
    "--backend",
    default="",
    help="Move the blocked run to this backend while retaining its identity.",
)
@click.option(
    "--reason",
    default="",
    help="Why a backend move is required; recorded in the run's lane history.",
)
@click.option(
    "--print-only",
    is_flag=True,
    help="Show the resume invocation without running it.",
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_resume(run_id, advice, backend, reason, print_only, pretty):
    """Continue one blocked run with advice; do not sweep all newly ready runs.

    Without a backend override, the resumed turn carries the prior context.
    A cross-harness move reports that it must start a fresh session.
    """
    if reason and not backend:
        raise click.UsageError("--reason requires --backend")
    crew_module, flight_module = _crew_modules()
    from reckon.crew.dispatch import LanePaused, _require_fleet_gate_open

    try:
        record = crew_module.read_pointer(run_id)
        project = str(record.get("project") or "")
        config = (
            _resolved_flight(flight_module, project, record.get("repo"), ())
            if project
            else None
        )
        if backend:
            from reckon.crew.dispatch import change_lane

            moved = change_lane(
                run_id,
                backend,
                reason,
                config=config or {},
                advice=advice,
                launch=not print_only,
            )
            _emit_crew_result(moved, pretty)
            return
        plan = crew_module.resume_plan(run_id, advice, config=config)
    except LanePaused as exc:
        _emit(
            {
                "ok": False,
                "error": "lane-paused",
                "detail": _lane_paused_detail(exc.gate),
                "reason": exc.gate.get("reason"),
                "lane_gate": exc.gate,
            },
            pretty,
        )
        raise click.exceptions.Exit(75) from exc
    except crew_module.BudgetHold as exc:
        _emit(
            {
                "ok": False,
                "error": "budget-hold",
                "detail": str(exc),
                "hold": exc.verdict,
            },
            pretty,
        )
        raise click.exceptions.Exit(3) from exc
    except crew_module.CrewError as exc:
        raise click.ClickException(str(exc)) from exc
    payload = {"run_id": run_id, **plan.as_dict()}
    if print_only:
        _emit_crew_result(payload, pretty)
        return
    directory = crew_module.run_dir(run_id)
    turn = len(list(directory.glob("resume-*.jsonl"))) + 1
    advice_path = directory / f"resume-{turn}-advice.txt"
    from reckon.crew.resumption import _write_resume_prompt

    _write_resume_prompt(advice_path, plan=plan, advice=advice)
    log_path = directory / f"resume-{turn}.jsonl"
    stderr_path = directory / f"resume-{turn}.stderr.log"
    current = crew_module.read_pointer(run_id)
    manifest_baseline_mtime_ns = crew_module._manifest_mtime_ns(
        current.get("manifest_path") or ""
    )
    attempt_started_at = crew_module._utc_now()
    try:
        _require_fleet_gate_open()
        pid = crew_module._spawn(
            plan, log_path=log_path, stderr_path=stderr_path, prompt_path=advice_path
        )
    except LanePaused as exc:
        _emit(
            {
                "ok": False,
                "error": "lane-paused",
                "detail": _lane_paused_detail(exc.gate),
                "reason": exc.gate.get("reason"),
                "lane_gate": exc.gate,
            },
            pretty,
        )
        raise click.exceptions.Exit(75) from exc
    crew_module.record_resumption(
        run_id,
        pid=pid,
        turn=turn,
        log_path=log_path,
        stderr_path=stderr_path,
        attempt_started_at=attempt_started_at,
        manifest_baseline_mtime_ns=manifest_baseline_mtime_ns,
    )
    payload.update({"pid": pid, "log_path": str(log_path), "resumed_turn": turn})
    _emit_crew_result(payload, pretty)



@crew.command(name="redispatch")
@click.option("--run", "run_id", required=True, help="Run id to move.")
@click.option("--backend", required=True, help="Destination backend.")
@click.option(
    "--reason",
    required=True,
    help="Why the run must change backend; recorded in its lane history.",
)
@click.option(
    "--advice",
    default="",
    help="Continuation advice for a reusable session or fresh worker.",
)
@click.option(
    "--estimated-hours",
    type=click.FloatRange(min=0.0, min_open=True),
    default=None,
    help=(
        "Neutral worker-hours for this attempt; replaces the estimate the run "
        "carried and is recorded on it."
    ),
)
@click.option(
    "--print-only",
    is_flag=True,
    help="Show the lane-change launch without stopping or starting a worker.",
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_redispatch(
    run_id, backend, reason, advice, estimated_hours, print_only, pretty
):
    """Move one working run to another backend without replacing its identity."""
    crew_module, flight_module = _crew_modules()
    from reckon.crew.dispatch import LanePaused

    try:
        record = crew_module.read_pointer(run_id)
        project = str(record.get("project") or "")
        config = (
            _resolved_flight(flight_module, project, record.get("repo"), ())
            if project
            else {}
        )
        from reckon.crew.dispatch import change_lane

        moved = change_lane(
            run_id,
            backend,
            reason,
            config=config,
            advice=advice,
            estimated_hours=estimated_hours,
            launch=not print_only,
        )
    except LanePaused as exc:
        _emit(
            {
                "ok": False,
                "error": "lane-paused",
                "detail": _lane_paused_detail(exc.gate),
                "reason": exc.gate.get("reason"),
                "lane_gate": exc.gate,
            },
            pretty,
        )
        raise click.exceptions.Exit(75) from exc
    except crew_module.BudgetHold as exc:
        _emit(
            {
                "ok": False,
                "error": "budget-hold",
                "detail": str(exc),
                "hold": exc.verdict,
            },
            pretty,
        )
        raise click.exceptions.Exit(3) from exc
    except crew_module.CrewError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_crew_result(moved, pretty)



@crew.command(name="stop")
@click.option("--run", "run_id", required=True, help="Run id to stop.")
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_stop(run_id, pretty):
    """Stop one running spawned worker and record its stopped state."""
    crew_module, _ = _crew_modules()
    try:
        record = crew_module.terminate(run_id)
    except crew_module.CrewError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_crew_result(record, pretty)



@crew.command(name="discard")
@click.option("--run", "run_id", required=True, help="Run id to discard.")
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_discard(run_id, pretty):
    """Remove one non-running live pointer without recording promoted evidence."""
    crew_module, _ = _crew_modules()
    try:
        result = crew_module.discard(run_id)
    except crew_module.CrewError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_crew_result(result, pretty)



@crew.command(name="repair-status")
@click.option(
    "--run",
    "run_id",
    required=True,
    help="Run whose manifest status word the coordinator is replacing.",
)
@click.option(
    "--status",
    type=click.Choice(["complete", "blocked", "failed"]),
    required=True,
    help=(
        "The verdict the run actually reached: the terminal words, restated "
        "here because importing the crew vocabulary would drag the whole "
        "facade onto every reckon --help."
    ),
)
@click.option(
    "--reason",
    required=True,
    help="Why this is the verdict; recorded in the run directory.",
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_repair_status(run_id, status, reason, pretty):
    """Replace a manifest's status word, keeping the file as delivered."""
    crew_module, _ = _crew_modules()
    from reckon.crew import runs as runs_module

    try:
        result = runs_module.repair_manifest_status(run_id, status, reason)
    except crew_module.CrewError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_crew_result(result, pretty)



def _ledger_module():
    """Import the ledger helpers on demand."""
    from reckon import ledger as ledger_module

    return ledger_module



@crew.command(name="complete")
@click.option("--run", "run_id", required=True, help="Run id to promote.")
@click.option(
    "--gate",
    required=True,
    help="Gate verdict: passed, failed or not-run.",
)
@click.option(
    "--failure-classification",
    type=click.Choice(
        [
            "work-rejected",
            "correct-refusal",
            "malformed-node",
            "infrastructure-failure",
            "pre-existing-failure",
            "negative-result",
        ]
    ),
    default=None,
    help="Required for a failing gate; names what the failure measures.",
)
@click.option(
    "--commit",
    "commits",
    multiple=True,
    help="Commit the run landed; repeat for each.",
)
@click.option(
    "--outcome",
    default="",
    help=(
        "One line on what the run produced. Required for a non-passing gate "
        "except on a review run, whose stored review's total score and finding "
        "count supply the summary when this is omitted."
    ),
)
@click.option(
    "--no-commit",
    default="",
    help="Declare deliberately that a passing node produced no commit, and why. "
    "Recorded on the ledger row, so a commitless promotion is distinguishable "
    "from one that recorded nothing by accident.",
)
@click.option(
    "--tests-added",
    type=int,
    default=None,
    help="Tests this run added, for later calibration.",
)
@click.option(
    "--scope-changed",
    is_flag=True,
    help="The node's scope was widened mid-flight, so it measures neither the "
    "estimate nor the worker and is excluded from calibration.",
)
@click.option(
    "--completed-at",
    default="",
    help="Observed completion stamp, when promotion happens later.",
)
@click.option(
    "--checkout-path",
    default=None,
    type=click.Path(path_type=Path),
    help="Repo root whose ledger receives the record (default: the run's own).",
)
@click.option(
    "--gate-command",
    default="",
    help=(
        "The check a passing gate ran, e.g. the test command. The integration "
        "re-run executes it verbatim at the merged head, so it has to be the "
        "command rather than a description of it: angle-bracket placeholders, "
        "an ellipsis standing for a file list, and parenthetical prose "
        "selections are refused at promotion."
    ),
)
@click.option(
    "--gate-exit-status",
    type=int,
    default=None,
    help="The check command's exit status.",
)
@click.option(
    "--gate-log-path",
    default="",
    help="Path to the check's captured output.",
)
@click.option(
    "--gate-log-digest",
    default="",
    help="Digest of the check's captured output, when the log itself is not kept.",
)
@click.option(
    "--waive-suite-delta",
    default="",
    metavar="REASON",
    help="Promote despite added suite failures and record the reason and delta.",
)
@click.option(
    "--waive-boundary-refusal",
    default="",
    metavar="REASON",
    help=(
        "Promote despite a stray uncommitted edit in another dispatch-visible "
        "tree at a declared path, and record the reason and the waived paths. "
        "Refused if the run has no boundary violation to waive."
    ),
)
@click.option(
    "--waive-resume-path",
    default="",
    metavar="REASON",
    help=(
        "Resolve a promotion refused because the run's session is still "
        "recoverable. State why its resume path may be discarded; the reason "
        "is recorded on the promoted ledger row."
    ),
)
@click.option(
    "--waive-unreviewed-promotion",
    default="",
    metavar="REASON",
    help=(
        "Promote a passing implement run without its independent review and "
        "record why the review requirement is being waived. Refused when the "
        "run is not awaiting review."
    ),
)
@click.option(
    "--waive-negative-control",
    default=None,
    metavar="REASON",
    help=(
        "Promote despite a negative-control log not naming its declared mutation "
        "and record the reason, declaration and log path. Refused when the run "
        "has no control-match refusal to waive."
    ),
)
@click.option(
    "--accept-path",
    "accepted_paths",
    type=(str, str),
    multiple=True,
    metavar="PATH REASON",
    help=(
        "Resolve a promotion refused because the run changed an undeclared "
        "companion path. Name its repository path and the reason it belongs "
        "to this run; repeat for each path."
    ),
)
@click.option(
    "--no-impl-change",
    default="",
    metavar="REASON",
    help=(
        "Promote a passing implement or test run whose plan impl did not move "
        "since dispatch, and record why it did not. Worded as a reason you give, "
        "not a rebuke: some landings legitimately do not move the plan. A "
        "corrective run — one dispatched as a resume or redispatch — is exempt "
        "and needs no flag."
    ),
)
@click.option(
    "--waive-live-run",
    default="",
    metavar="REASON",
    help=(
        "Promote a run whose recorded worker process is still alive and whose "
        "manifest status is not terminal, and record why it may land anyway. "
        "Refused when the run has no live, in-progress worker to waive."
    ),
)
@click.option(
    "--plan-link",
    default="",
    metavar="SLUG",
    help=(
        "Name the plan whose product an unplanned (brief) implement landing "
        "changed. Required for an implement-role brief run unless "
        "--unplanned-reason is given; the value lands on the ledger row as "
        "plan_link."
    ),
)
@click.option(
    "--unplanned-reason",
    default="",
    metavar="TEXT",
    help=(
        "State why an unplanned (brief) implement landing changed no plan. "
        "Required for an implement-role brief run unless --plan-link is given; "
        "the value lands on the ledger row as unplanned_reason."
    ),
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_complete(
    run_id,
    gate,
    failure_classification,
    commits,
    outcome,
    no_commit,
    tests_added,
    scope_changed,
    completed_at,
    checkout_path,
    gate_command,
    gate_exit_status,
    gate_log_path,
    gate_log_digest,
    waive_suite_delta,
    waive_boundary_refusal,
    waive_resume_path,
    waive_unreviewed_promotion,
    waive_negative_control,
    accepted_paths,
    no_impl_change,
    waive_live_run,
    plan_link,
    unplanned_reason,
    pretty,
):
    """Promote one finished run into the owning repository's committed ledger.

    The ledger append happens before the pointer is deleted, so an interruption
    between them leaves a recoverable pointer rather than a lost record. A
    passing gate is refused unless it carries the check that produced it —
    this is the sole route by which a run is promoted, so the requirement is
    unconditional here even though the underlying ledger call remains
    permissive for internal callers assembling records from other evidence.
    """
    crew_module, _ = _crew_modules()
    ledger_module = _ledger_module()
    gate_check = {
        "command": gate_command,
        "exit_status": gate_exit_status,
        "log_path": gate_log_path,
        "log_digest": gate_log_digest,
    }
    try:
        result = crew_module.complete(
            run_id,
            gate=gate,
            failure_classification=failure_classification or "",
            commits=commits,
            outcome=outcome,
            no_commit=no_commit,
            tests_added=tests_added,
            scope_changed=scope_changed,
            completed_at=completed_at,
            root=checkout_path,
            gate_check=gate_check,
            require_gate_check=True,
            suite_delta_waiver=waive_suite_delta,
            boundary_waiver=waive_boundary_refusal,
            resume_waiver=waive_resume_path,
            review_waiver=waive_unreviewed_promotion,
            negative_control_waiver=waive_negative_control,
            accepted_paths=dict(accepted_paths),
            no_impl_change=no_impl_change,
            live_run_waiver=waive_live_run,
            plan_link=plan_link,
            unplanned_reason=unplanned_reason,
        )
    except ledger_module.SuiteDeltaError as exc:
        _emit(
            {
                "ok": False,
                "error": "suite-delta-refused",
                "detail": str(exc),
                "missing_fields": exc.missing_fields,
                "added_failure_ids": exc.added_failure_ids,
            },
            pretty,
        )
        raise click.exceptions.Exit(1) from exc
    except crew_module.CrewError as exc:
        detail = str(exc)
        if "changed paths outside its declared write scope" in detail:
            detail += (
                ". Retry with --accept-path PATH REASON for each deliberate "
                "companion path"
            )
        raise click.ClickException(detail) from exc
    except ledger_module.LedgerError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_crew_result(result, pretty)



@crew.command(name="verify-gate")
@click.option("--project", required=True, help="Project owning the run ledger.")
@click.option("--run", "run_id", required=True, help="Run id whose gate is re-run.")
@click.option(
    "--checkout-path",
    required=True,
    type=click.Path(path_type=Path),
    help="Checked-out repository holding the merged integrated revision.",
)
@click.option(
    "--revision",
    default="HEAD",
    help="Integrated revision to re-run the gate against (default: HEAD).",
)
@click.option(
    "--timeout-seconds",
    "timeout_seconds",
    type=float,
    default=None,
    help=(
        "Bound on the gate re-run before it is reported as timed out. "
        "Omitted, the bound is derived from the duration the run's recorded "
        "gate log carries, with headroom between a 300 s floor and a 1800 s "
        "ceiling; a run whose log records no duration keeps the 300 s default."
    ),
)
@click.option(
    "--command",
    "command",
    default=None,
    help=(
        "Command to run instead of the run's stored gate command, so a "
        "coordinator can measure a wider suite at the merged head."
    ),
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_verify_gate(
    project, run_id, checkout_path, revision, timeout_seconds, command, pretty
):
    """Re-run one run's gate at the integrated revision, recording the report on the run.

    Reads the run's stored gate command and base verdict from its committed
    ledger row, re-runs that command against the tree at --checkout-path — the
    head the coordinator merged — and patches the re-run report back onto the
    run's ledger row, so a gate the integrated revision no longer satisfies is
    recorded against the run rather than only printed. The report is recorded
    whether or not a finding exists.

    --command runs the given command in place of the stored one, so a
    coordinator can measure a whole-repository suite rather than only the
    node's own gate, and can do so on a run that stored no gate command. The
    recorded report names the command that actually ran and whether it came
    from the option or the stored row.

    The re-run's bound is derived from the duration the run's recorded gate
    log carries unless --timeout-seconds names one explicitly; the report
    records which bound applied and its value either way.
    """
    crew_module, _ = _crew_modules()
    try:
        result = crew_module.record_gate_rerun_at_integrated_revision(
            project=project,
            run_id=run_id,
            repository=checkout_path,
            integrated_revision=revision,
            timeout_seconds=timeout_seconds,
            root=checkout_path,
            command=command,
        )
    except crew_module.CrewError as exc:
        raise click.ClickException(str(exc)) from exc
    except _ledger_module().LedgerError as exc:
        raise click.ClickException(
            f"{exc}; the ledger moved while the re-run was being recorded, so "
            f"run `reckon crew verify-gate --project {project} --run {run_id} "
            f"--checkout-path {checkout_path}` again"
        ) from exc
    _emit_crew_result(result, pretty)



@crew.command(name="recover")
@click.option("--project", default=None, help="Limit to one project's runs.")
@click.option(
    "--dispatch-reviews",
    is_flag=True,
    help="Launch reviews for scoring runs in the named project with live coordinators.",
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_recover(project, dispatch_reviews, pretty):
    """Classify live pointers; launch reviews only with --dispatch-reviews and --project.

    Reports running, completed-but-unpromoted (with its manifest path) and
    abandoned runs. By default it repairs the record and launches nothing.
    An opted-in review stays awaiting-coordinator when its session is no longer
    live. No worktree is removed or run promoted on this command's initiative.
    """
    crew_module, flight_module = _crew_modules()
    config = None
    if project:
        config = _resolved_flight(flight_module, project, None, ())
    try:
        report = crew_module.recover(
            project=project, config=config, dispatch_reviews=dispatch_reviews
        )
    except crew_module.CrewError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_crew_result(report, pretty)



@crew.group(name="member")
def crew_member():
    """The project's committed team roster."""



@crew_member.command(name="add")
@click.option("--project", required=True, help="Project owning the roster.")
@click.option("--member", "member_id", required=True, help="Stable member id.")
@click.option("--harness", required=True, help="Backend this member dispatches to.")
@click.option("--role", default="implement", show_default=True, help="Routing role.")
@click.option(
    "--session",
    default="",
    help="Existing session id; omit so the first run captures one.",
)
@click.option(
    "--checkout-path",
    default=None,
    type=click.Path(path_type=Path),
    help="Repo root whose roster is written (default: the registered checkout).",
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_member_add(project, member_id, harness, role, session, checkout_path, pretty):
    """Register a missing roster member or update an existing member; launch no work."""
    ledger_module = _ledger_module()
    try:
        entry = ledger_module.register_member(
            project,
            member_id,
            harness=harness,
            role=role,
            session_id=session or None,
            root=checkout_path,
            commit=True,
        )
    except ledger_module.LedgerError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_crew_result({"project": project, "member": entry}, pretty)



@crew_member.command(name="list")
@click.option("--project", required=True, help="Project owning the roster.")
@click.option(
    "--checkout-path",
    default=None,
    type=click.Path(path_type=Path),
    help="Repo root whose roster is read (default: the registered checkout).",
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_member_list(project, checkout_path, pretty):
    """List roster members and reusable sessions, not live run pointers."""
    ledger_module = _ledger_module()
    try:
        roster = ledger_module.members(project, checkout_path)
    except ledger_module.LedgerError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_crew_result({"project": project, "members": roster}, pretty)



@crew.command(name="ledger")
@click.option("--project", required=True, help="Project whose ledger is read.")
@click.option(
    "--view",
    type=click.Choice(["summary", "records"]),
    default="summary",
    show_default=True,
    help="Rolled-up measures, or every completed record.",
)
@click.option(
    "--checkout-path",
    default=None,
    type=click.Path(path_type=Path),
    help="Repo root whose ledger is read (default: the registered checkout).",
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_ledger(project, view, checkout_path, pretty):
    """Read committed run records, not live run pointers."""
    ledger_module = _ledger_module()
    try:
        if view == "records":
            # The committed rows as a reader receives them: each carries its
            # own wall time and the width of the fleet when it was dispatched.
            records, _version = ledger_module.read_records(project, checkout_path)
            payload = {
                "runs": records,
                "holds": ledger_module.holds(project, checkout_path),
                "members": ledger_module.members(project, checkout_path),
            }
        else:
            payload = ledger_module.summary(project, root=checkout_path)
    except ledger_module.LedgerError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_crew_result({"project": project, **payload}, pretty)



@crew.command(name="velocity")
@click.option(
    "--project",
    required=True,
    help="Project measured, or * for every mounted checkout.",
)
@click.option(
    "--since",
    default=None,
    help="Window start, ISO-8601. Refused when absent or unparseable.",
)
@click.option(
    "--until",
    default=None,
    help="Window close, ISO-8601 (default: now).",
)
@click.option(
    "--fields",
    multiple=True,
    help=(
        "An optional block to include. Repeatable and comma-separated; the "
        "aggregate tables are served by default. Unrecognised names are refused."
    ),
)
@click.option("--limit", type=int, default=None, help="Cells per page.")
@click.option(
    "--cursor",
    default=None,
    help="Resume the project-lane-day cells at a cursor from a previous page.",
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_velocity(project, since, until, fields, limit, cursor, pretty):
    """Report what the fleet delivered over a caller-named window.

    The per-project, per-lane and per-day tables are the default answer; the
    project-lane-day cells and every other block are served only when named in
    --fields, and the cells are paged by --limit and --cursor. This is the same
    payload the crew read view answers with for the same window, because both
    call one composition.
    """
    from reckon import velocity as velocity_module

    requested = [
        name
        for value in fields
        for name in (part.strip() for part in value.split(","))
        if name
    ]
    payload = velocity_module.view(
        project,
        since=since,
        until=until,
        fields=requested or None,
        limit=limit,
        cursor=cursor,
    )
    if not payload.get("ok"):
        detail = payload.get("detail") or payload.get("message") or str(payload)
        raise click.ClickException(str(detail))
    _emit_crew_result(payload, pretty)



@crew.command(name="budget-reset")
@click.option(
    "--group",
    required=True,
    help="Budget group whose banked reset is flagged, cleared or read.",
)
@click.option(
    "--available",
    "mark_available",
    is_flag=True,
    help=(
        "Flag one banked reset as available. Idempotent: flagging a group that "
        "already carries one changes nothing and says so."
    ),
)
@click.option(
    "--used",
    "mark_used",
    is_flag=True,
    help="Clear the flag, recording that the banked reset was spent.",
)
@click.option(
    "--by",
    default=None,
    help="Who is setting or clearing the flag (default: $USER).",
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_budget_reset(group, mark_available, mark_used, by, pretty):
    """Set, clear or read whether a budget group's banked reset is available or used.

    A metered subscription can carry one banked reset, which grants one extra
    full window of allowance while it is available. With no action flag the
    current state is printed; ``--available`` flags one (never stacking) and
    ``--used`` clears it. Budget preflight and the picker read the flag from the
    same durable record, so flagging it here changes the pace they report.
    """
    from reckon.crew import budget_reset as budget_reset_module

    if mark_available and mark_used:
        raise click.ClickException("--available and --used are mutually exclusive")
    if mark_available:
        result = budget_reset_module.mark_available(group, by=by)
    elif mark_used:
        result = budget_reset_module.mark_used(group, by=by)
    else:
        found = budget_reset_module.record(group)
        found = found or {}
        result = {
            "ok": True,
            "group": group,
            "available": bool(found.get("available")),
            "changed": False,
            "set_at": found.get("set_at"),
            "set_by": found.get("set_by"),
            "cleared_at": found.get("cleared_at"),
            "cleared_reason": found.get("cleared_reason"),
            "detail": (
                "a banked reset is available for this group"
                if found.get("available")
                else "no banked reset is available for this group"
            ),
        }
    _emit_crew_result(result, pretty)



@crew.command(name="split-runs")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Report what each project would migrate, writing and committing nothing.",
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_split_runs(dry_run, pretty):
    """Move each mounted project's aggregate run rows into their own files."""
    ledger_module = _ledger_module()
    try:
        report = ledger_module.split_runs(dry_run=dry_run)
    except ledger_module.LedgerError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_crew_result(report, pretty)



@crew.command(name="repair-completion")
@click.option("--project", required=True, help="Project whose run ledger is checked.")
@click.option(
    "--write",
    "write_changes",
    is_flag=True,
    help="Persist re-derived completion measurements; the default only reports.",
)
@click.option(
    "--checkout-path",
    default=None,
    type=click.Path(path_type=Path),
    help="Repo root whose ledger is checked (default: the registered checkout).",
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_repair_completion(project, write_changes, checkout_path, pretty):
    """Repair historical completion measurements missing from surviving run streams."""

    ledger_module = _ledger_module()
    try:
        report = ledger_module.repair_completion(
            project,
            root=checkout_path,
            write_changes=write_changes,
        )
    except ledger_module.LedgerError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_crew_result(report, pretty)

