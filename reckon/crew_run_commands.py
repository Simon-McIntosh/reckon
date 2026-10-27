import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import click

from reckon.crew.tombstones import tombstone_command
from reckon.crew_dispatch_commands import (
    _accept_a_matching_project,
    _budget_check_report,
    _crew_modules,
    _emit,
    _emit_crew_result,
    _lane_paused_detail,
    _resolved_flight,
    _resolved_gc_repo,
    crew,
)

# Preserve the CLI re-export while the retired name resolves through the
# crew group's tombstone lookup.
crew_unwatch = tombstone_command("unwatch")


crew_resume_ready = tombstone_command("resume-ready")


def _merge_land_refusal(
    exc: Exception, *, resume: str | None = None
) -> tuple[dict[str, Any], int]:
    """Map an exception raised by the merge step to a land refusal and its exit.

    Both commands that land a head report a merge failure through this one
    mapping, so a failure is refused the same way wherever it surfaces. A
    ``merge.MergeError`` is the merge module's own typed refusal and keeps its
    message verbatim; any other exception escaped the merge step untyped, so
    the refusal names the exception's type beside its message. ``resume`` is
    the command that lands the head again, and is absent where the caller is
    already that command. Both are exit 1: a refused landing, not a landed one.
    """
    from reckon.crew import merge as merge_module

    if isinstance(exc, merge_module.MergeError):
        error = str(exc)
    else:
        error = f"{type(exc).__name__}: {exc}"
    entry: dict[str, Any] = {"ok": False, "error": error}
    if resume:
        entry["resume"] = resume
    return entry, 1


@crew.command(name="merge")
@click.option(
    "--project",
    required=True,
    help="Project whose branch the head lands on.",
)
@click.option("--head", required=True, help="Reviewed commit to land as a merge.")
@click.option("--message", default="", help="Merge commit subject.")
@click.option(
    "--body",
    default="",
    help="Merge commit body; a subject-only message is refused.",
)
@click.option(
    "--candidate",
    default=None,
    help="A merge commit already built, landed unchanged if its parents match.",
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_merge(project, head, message, body, candidate, pretty):
    """Land a reviewed head on the primary branch without touching the shared index.

    The merge is built from objects — ``merge-tree`` computes the tree,
    ``commit-tree`` the commit — so nothing is staged and the checkout is
    touched only by the closing fast-forward. A conflicting head is refused
    with nothing written, and a held ``index.lock`` is retried rather than
    leaving the staged tree a hand repair.

    A push that fails after the fast-forward has landed reports the landed
    commit and exits with a status distinct from a refusal: the merge is in
    place, only the push is outstanding, and a rerun for the same head pushes
    rather than building a second merge.
    """
    from reckon.crew import merge

    try:
        repository = merge.project_repository(project)
        report = merge.land_merge(
            repository,
            head=head,
            message=message,
            body=body,
            candidate=candidate or None,
        )
    except merge.MergePushOutstandingError as exc:
        _emit(exc.report, pretty)
        raise click.exceptions.Exit(merge.PUSH_OUTSTANDING_EXIT) from exc
    except Exception as exc:
        entry, _ = _merge_land_refusal(exc)
        raise click.ClickException(entry["error"]) from exc
    _emit_crew_result(report, pretty)


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


@crew.command(name="directory")
@click.option("--project", default=None, help="Return coordinators for one project.")
@click.option("--run", "run_id", default=None, help="Resolve one live run's owner.")
@click.option("--node", "node_id", default=None, help="Resolve one live node's owner.")
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_directory(project, run_id, node_id, pretty):
    """List live coordinator ownership across repositories, not individual run state."""
    _accept_a_matching_project(run_id, project)
    from reckon.crew.directory import DirectoryError, directory

    try:
        result = directory(project, run_id=run_id, node_id=node_id)
    except DirectoryError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_crew_result(result, pretty)


@crew.command(name="view")
@click.argument("name", required=False, default="summary")
@click.option("--project", default=None, help="Project whose read view to serve.")
@click.option(
    "--checkout-path",
    default=None,
    type=click.Path(path_type=Path),
    help="Repo root the view resolves in (default: the registered checkout).",
)
@click.option(
    "--plan", default=None, help="Plan a plan-review or records view narrows to."
)
@click.option("--path", default=None, help="Path prefix the findings view narrows to.")
@click.option("--since", default=None, help="Window start, ISO-8601.")
@click.option("--until", default=None, help="Window close, ISO-8601 (default: now).")
@click.option("--limit", type=int, default=None, help="Records per page.")
@click.option("--cursor", default=None, help="Resume a paged view at a cursor.")
@click.option(
    "--session", default=None, help="Coordinator session the view is read for."
)
@click.option(
    "--phase", default=None, help="Live view: keep only pointers in this phase."
)
@click.option(
    "--mine",
    is_flag=True,
    help="Live view: keep only the rows --session owns.",
)
@click.option("--run", "run_id", default=None, help="One run the view selects.")
@click.option("--node", default=None, help="One node the view selects.")
@click.option("--section", default=None, help="One section the view selects.")
@click.option("--member", default=None, help="One roster member the view selects.")
@click.option("--classification", default=None, help="One classification to keep.")
@click.option(
    "--resumable/--no-resumable",
    default=None,
    help="Keep only runs that are, or are not, resumable.",
)
@click.option("--newest-per-node", is_flag=True, help="Keep the newest row per node.")
@click.option(
    "--source", default="all", show_default=True, help="Where rows are read from."
)
@click.option(
    "--scope",
    default="project",
    show_default=True,
    help="Project or workstation scope for the runs view.",
)
@click.option(
    "--fields",
    "fields",
    multiple=True,
    help="An optional block to include; repeatable and comma-separated.",
)
@click.option(
    "--candidates",
    default=None,
    help="A JSON array of candidate nodes for the scopes view.",
)
@click.option(
    "--refresh", is_flag=True, help="Recheck the public model list for the models view."
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_view(
    name,
    project,
    checkout_path,
    plan,
    path,
    since,
    until,
    limit,
    cursor,
    session,
    phase,
    mine,
    run_id,
    node,
    section,
    member,
    classification,
    resumable,
    newest_per_node,
    source,
    scope,
    fields,
    candidates,
    refresh,
    pretty,
):
    """Serve one crew read view, the same document the crew MCP tool returns.

    This is the transport for hooks and scripts, and the arrow every retired
    read verb names. It calls the one function the crew tool also calls, so the
    two surfaces cannot disagree about a view. NAME is a view name; `summary`
    is served when none is given.
    """
    _accept_a_matching_project(run_id, project)
    import json as _json

    from reckon.mcp_views import serve_crew_view

    requested = [
        part
        for value in fields
        for part in (chunk.strip() for chunk in value.split(","))
        if part
    ]
    parsed_candidates = None
    if candidates is not None:
        try:
            parsed_candidates = _json.loads(candidates)
        except ValueError as exc:
            raise click.ClickException(
                f"--candidates is not valid JSON: {exc}"
            ) from exc
        if not isinstance(parsed_candidates, list):
            raise click.ClickException("--candidates must be a JSON array")
    payload = serve_crew_view(
        name,
        refresh=refresh,
        project=project,
        checkout_path=str(checkout_path) if checkout_path is not None else None,
        plan=plan,
        path=path,
        since=since,
        until=until,
        limit=limit,
        cursor=cursor,
        candidates=parsed_candidates,
        session=session,
        run_id=run_id,
        node=node,
        section=section,
        member=member,
        classification=classification,
        resumable=resumable,
        newest_per_node=newest_per_node,
        source=source,
        scope=scope,
        fields=requested or None,
        phase=phase,
        mine=mine,
    )
    _emit(payload, pretty)
    if payload.get("ok") is False:
        raise click.exceptions.Exit(1)


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
    # The run's own project, resolved from the run and refused when a --project
    # disagrees with it; a kind keyed by run can then fall back to it when
    # --project is omitted rather than demanding a project the run already
    # names.
    resolved_project = _accept_a_matching_project(run_id, project)
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
        if run_id is None:
            if project is not None:
                raise click.ClickException(
                    f"--kind {kind} takes --run only, the form of the path is "
                    "decided by the run it names."
                )
            path = runs_dir() if kind == "runs" else live_dir()
        else:
            path = run_dir(run_id) if kind == "runs" else pointer_path(run_id)
    else:  # kind == "reviews"; click.Choice admits nothing else
        store_project = project or resolved_project
        if store_project is None:
            raise click.ClickException(
                "--kind reviews requires --project or a --run whose project "
                "resolves, the review store is keyed by project first."
            )
        path = (
            review_store_root() / store_project
            if run_id is None
            else review_path(store_project, run_id)
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
    default=None,
    help="Project whose review store holds the run's record. Optional: the "
    "run's own project is resolved from the run, and a --project that "
    "disagrees with it is refused.",
)
@click.option(
    "--run",
    "reviewed_run_id",
    required=True,
    help="Run the review is about, not the run that reviewed it.",
)
@click.option(
    "--dimension",
    required=False,
    default=None,
    help="Review dimension carrying the sub-floor finding.",
)
@click.option(
    "--finding",
    "finding_index",
    required=False,
    default=None,
    type=int,
    help="Index of a finding in the run's newest review record, as the "
    "findings rows print it.",
)
@click.option(
    "--rubric",
    "review_rubric",
    default=None,
    help="Review rubric the finding belongs to. Omit to answer the run's text "
    "review, which is the behaviour of every earlier call; name figure to "
    "answer a finding on the run's figure review.",
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
def crew_dispose(
    project,
    reviewed_run_id,
    dimension,
    finding_index,
    kind,
    node_id,
    reason,
    pretty,
    review_rubric=None,
):
    """Record the disposition one sub-floor review dimension or review finding carries.

    A finding is a sub-floor review dimension, or an entry in the run's newest
    review record: a dimension below the floor flight configuration declares for
    it, or a finding the review rows print by index. Whichever the caller names is an
    obligation row until an entry in the closed set answers it: a fold naming
    the dispatched node the finding went into, or an exemption naming why it is
    not being acted on. This is that entry's writer, so a coordinator retires
    the row through a command rather than editing the review store by hand or
    promoting the close over it. ``--dimension`` and ``--finding`` name the two
    spaces and are mutually exclusive; exactly one is required.

    Every refusal is the store's own, and nothing is written on one: an unknown
    dimension, an index naming no finding, a kind outside the closed set, a fold
    naming no node, an exemption carrying no reason, and either kind supplied
    with the other's field all exit non-zero with the reason stated. The record
    rewritten is the one the dimension duty reader selects, and a finding
    disposition is written under its own key beside the dimension map, so the
    same reading and the same file carry both. A run with no live pointer, the
    review is answered from its own records rather than from the directory the
    operator stands in; see :func:`_reviewed_head_from_run_records`.

    A run may be reviewed under more than one rubric, and a figure review is
    stored beside the run's text review rather than in place of it.
    ``--rubric figure`` answers a finding on the run's figure review: the
    record read, rewritten and read back for the receipt is the figure record,
    and a run carrying no figure review is refused naming that absence.
    Omitted, the verb answers the run's text review exactly as before. The
    parameter is last and defaults, so a caller reaching the callback without
    naming a rubric answers the text review rather than failing on an argument
    it never had to know about.
    """
    from reckon.crew import obligations as obligations_module
    from reckon.crew import recovery, review_dispositions, runs
    from reckon.crew import review as review_module

    # The run's own project is the store the disposition is written to, so
    # --project is optional and a disagreeing one is refused rather than
    # redirecting the write to a store the run does not belong to.
    project = _accept_a_matching_project(reviewed_run_id, project)
    if not project:
        raise click.ClickException(
            f"cannot resolve a project for run {reviewed_run_id!r}; name its "
            "project with --project"
        )
    named_dimension = str(dimension or "").strip() if dimension is not None else ""
    if bool(named_dimension) == (finding_index is not None):
        raise click.ClickException(
            "name exactly one of --dimension and --finding: a disposition "
            "answers one review finding, whether a sub-floor dimension or an "
            "index must be named to answer it"
        )
    rubric = str(review_rubric or "").strip().lower() or None
    if rubric is not None and rubric != review_module.FIGURE_REVIEW_RUBRIC:
        raise click.ClickException(
            f"unknown review rubric {review_rubric!r}; the one rubric a "
            f"disposition may name is {review_module.FIGURE_REVIEW_RUBRIC!r}, "
            "and omitting --rubric answers the run's text review"
        )
    if rubric and named_dimension:
        raise click.ClickException(
            "a figure review scores no dimensions, so --dimension names one it "
            "does not carry; name one of its findings with --finding, or omit "
            "--rubric to answer a dimension of the run's text review"
        )

    # A code review scores no dimensions, so its only disposition space is its
    # findings: a dimension named against one is refused by name rather than
    # resolved to a dimension no code review carries.
    code_key = review_dispositions.code_review_identity(reviewed_run_id)
    if code_key and named_dimension:
        raise click.ClickException(
            f"run {reviewed_run_id!r} is a code review, and a code review "
            "scores no dimensions; name one of its findings with --finding, "
            "which is the only space a code review's entries live in"
        )

    if rubric:
        # A figure review is stored beside the run's text review under the
        # rubric's own name, and the by-head readers below select text records
        # only, so the figure record is read through the store's rubric-keyed
        # selection: it is the file the writer rewrites and the receipt reads.
        _figure_path, figure_record = review_module.stored_record(
            project, reviewed_run_id, rubric=rubric
        )
        if figure_record is None:
            raise click.ClickException(
                f"run {reviewed_run_id!r} carries no figure review record in "
                f"project {project!r} to record a disposition against"
            )
        stored = figure_record
    elif code_key:
        # A code review is keyed by the neighbourhood it read rather than by a
        # revision, so its record is resolved through the store's own path
        # builder and read directly; no head or run pointer is consulted.
        _record_path, stored = review_dispositions.code_review_record_for_run(
            project, reviewed_run_id
        )
        described = ""
        head = str((stored or {}).get("reviewed_revision") or "")
    else:
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
            # The revision the run's work is at, named by the refusal below when
            # the store's record describes another; the record itself is
            # selected through the reading both readers share.
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
        if finding_index is None:
            path = review_dispositions.record_dimension_disposition(
                project,
                reviewed_run_id,
                named_dimension,
                kind=kind,
                node=node_id,
                reason=reason,
                reviewed_head_sha=reviewed_head_sha,
            )
        else:
            path = review_dispositions.record_finding_disposition(
                project,
                reviewed_run_id,
                finding_index,
                kind=kind,
                node=node_id,
                reason=reason,
                reviewed_head_sha=reviewed_head_sha,
                rubric=rubric,
            )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    record = review_dispositions.stored_record_for_disposition(
        project,
        reviewed_run_id,
        reviewed_head_sha=reviewed_head_sha,
        rubric=rubric,
    )
    if finding_index is None:
        dispositions = (record or {}).get(
            review_module.DIMENSION_DISPOSITIONS_KEY
        ) or {}
        entry = dispositions.get(named_dimension.lower())
    else:
        # The disposition is rendered through the findings reader's own predicate,
        # so the receipt reports what a later read of the record reports.
        from reckon.crew.findings import finding_disposition

        entry = finding_disposition(record or {}, finding_index)
    _emit_crew_result(
        {
            "project": project,
            "run_id": reviewed_run_id,
            "dimension": named_dimension or None,
            # The index is named ``finding_index``, not ``finding``: a bare
            # ``finding`` key is in the crew verdict's refusal vocabulary, where
            # a truthy value marks a report carrying a problem, so a receipt
            # naming the index that way would exit non-zero on a success.
            "finding_index": finding_index,
            "path": str(path),
            "disposition": entry,
        },
        pretty,
    )


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


def crew_check_review(record_path, pretty):
    """Check whether a delivered run review can be filed, without writing it."""
    import json

    from reckon.crew import review as review_module

    try:
        body = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        findings = [f"record could not be read as JSON: {exc}"]
    else:
        findings = (
            review_module.review_record_refusals(body)
            if isinstance(body, dict)
            else ["record is not an object"]
        )
    _emit(
        {"ok": not findings, "record": str(record_path), "findings": findings}, pretty
    )
    if findings:
        raise click.exceptions.Exit(1)


def crew_repair_review(reviewed_run_id, record_path, change, reason, pretty):
    """Repair only a review's status or missing subject identity, preserving it."""
    from reckon.crew import review as review_module
    from reckon.crew.node import CrewError

    try:
        result = review_module.repair_run_review(
            reviewed_run_id, record_path, change, reason
        )
    except (OSError, ValueError, TypeError, CrewError) as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_crew_result(result, pretty)


@crew.command(name="check")
@click.option(
    "--manifest", "manifest", is_flag=True, help="Check a run's delivered manifest."
)
@click.option(
    "--review", "review", is_flag=True, help="Check a delivered or stored run review."
)
@click.option("--run", "run_id", help="Run whose manifest or stored review to check.")
@click.option(
    "--project", default=None, help="Project owning the run; cross-checks the run."
)
@click.option(
    "--record",
    "record_path",
    type=click.Path(path_type=Path),
    help="Delivered review record to check.",
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_check(manifest, review, run_id, record_path, pretty, project=None):
    """Judge a delivered manifest or run review against its filing contract."""
    _accept_a_matching_project(run_id, project)
    if manifest == review:
        raise click.UsageError("choose exactly one of --manifest or --review")
    if manifest:
        if not run_id or record_path:
            raise click.UsageError(
                "--manifest needs --run and does not accept --record"
            )
        return crew_check_manifest(run_id, pretty)
    if bool(run_id) == bool(record_path):
        raise click.UsageError("--review needs exactly one of --run or --record")
    if run_id:
        from reckon.crew import review as review_module
        from reckon.crew import runs as runs_module
        from reckon.crew.node import CrewError

        try:
            pointer = runs_module.read_pointer(run_id)
            project = str(pointer["project"])
        except (KeyError, OSError, ValueError, CrewError) as exc:
            raise click.ClickException(
                f"run {run_id} could not be resolved: {exc}"
            ) from exc
        record_path, _ = review_module.stored_record(project, run_id)
        if record_path is None:
            raise click.ClickException(f"run {run_id} has no stored review")
    return crew_check_review(record_path, pretty)


@crew.command(name="repair")
@click.option(
    "--status",
    type=click.Choice(["complete", "blocked", "failed"]),
    help="Replace the run's manifest status word.",
)
@click.option(
    "--review",
    "repair_review",
    is_flag=True,
    help="Repair a review's status or missing subject.",
)
@click.option(
    "--run", "run_id", required=True, help="Run whose manifest or review is repaired."
)
@click.option(
    "--project", default=None, help="Project owning the run; cross-checks the run."
)
@click.option(
    "--record",
    "record_path",
    type=click.Path(path_type=Path),
    help="Delivered review record to repair.",
)
@click.option("--set", "change", help="Review field and value to repair.")
@click.option("--reason", required=True, help="Why the recorded repair is justified.")
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_repair(
    status, repair_review, run_id, record_path, change, reason, pretty, project=None
):
    """Repair a manifest's status word or a review's status or subject identity."""
    _accept_a_matching_project(run_id, project)
    if bool(status) == repair_review:
        raise click.UsageError("choose exactly one of --status or --review")
    if status:
        if record_path or change:
            raise click.UsageError("--status does not accept --record or --set")
        return crew_repair_status(run_id, status, reason, pretty)
    if not record_path or not change:
        raise click.UsageError("--review needs --record and --set")
    return crew_repair_review(run_id, record_path, change, reason, pretty)


@crew.command(name="supersede-review")
@click.option(
    "--project", required=True, help="Project whose committed ledger holds the row."
)
@click.option(
    "--run", "run_id", required=True, help="Run whose review row is corrected."
)
@click.option("--reason", required=True, help="Why the row's review is superseded.")
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_supersede_review(project, run_id, reason, pretty):
    """Append a correction that supersedes a misattributed row's review; every reader then reads the row as unreviewed."""
    _accept_a_matching_project(run_id, project)
    from reckon.crew.node import CrewError

    try:
        result = supersede_run_review(project, run_id, reason)
    except (OSError, ValueError, TypeError, CrewError) as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_crew_result(result, pretty)


def _dispatched_head_for_row(row: Mapping[str, Any]) -> str:
    """The head a committed row was dispatched to read, or "" when it names none.

    The strongest source is a declared ``.at-<head>`` write path, read through
    the delivery code's own predicate so a path a reviewer was granted names the
    head without the name being parsed a second time. Falling back, the head
    suffix the review node id mints names the same revision when the dispatch
    composed a head-keyed id; a hand dispatch under a coordinator's own id
    carries neither source and is left unresolved rather than guessed.
    """
    from reckon.crew.recovery_review_dispatch import _review_record_subject
    from reckon.crew.recovery_vocabulary import REVIEW_NODE_PREFIX
    from reckon.crew.repair import round_node_head_suffix

    definition = row.get("node_definition")
    write_paths = (
        definition.get("write_paths") if isinstance(definition, Mapping) else None
    )
    for declared in write_paths or []:
        _run_id, head = _review_record_subject(Path(str(declared)))
        if head:
            return head
    return round_node_head_suffix(str(row.get("node") or ""), REVIEW_NODE_PREFIX)


def supersede_run_review(project: str, run_id: str, reason: str) -> dict[str, Any]:
    """Append one review correction to a run's committed row, correcting it.

    A committed row records what a promotion believed when it landed, and that
    belief is evidence in its own right, so a row whose review fields describe
    another round is never rewritten in place. The correction is appended
    beside the recorded fields instead: it names the head the row was
    dispatched to read, the head it recorded, the reason and the field it
    supersedes. Every reader of a row's review reads the correction through the
    ledger's own accessor, so a corrected row reads as a run with no review.

    Three row shapes are refused and left untouched, so a second run of the
    verb cannot append a second correction to one row. A row whose recorded
    head is the same revision as its dispatched head was not misattributed and
    has nothing to correct. A row naming no dispatched head from any source
    cannot be compared, so its attribution is unknown rather than wrong. A row
    that already carries a correction has been corrected once.
    """
    import json

    from reckon import ledger
    from reckon.crew.promotion_scope import _commit_landing_writes
    from reckon.crew.recovery_review_subject import same_revision
    from reckon.crew.review import carried_revision_pair

    explanation = str(reason or "").strip()
    if not explanation:
        raise ValueError("a review correction requires a non-empty --reason")

    source = ledger.run_path(project, run_id, None)
    resolved_source = source.resolve()
    row: Mapping[str, Any] | None = None
    if resolved_source.is_file():
        loaded = json.loads(resolved_source.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            row = loaded
    aggregate, version = ledger.load_aggregate(project)
    rows = aggregate["runs"]
    index = next(
        (
            position
            for position, item in enumerate(rows)
            if isinstance(item, Mapping) and str(item.get("run_id") or "") == run_id
        ),
        None,
    )
    if row is None:
        if index is None:
            raise ValueError(
                f"run {run_id!r} names no committed row for {project!r}: neither "
                f"{source} nor the project's ledger holds it"
            )
        row = dict(rows[index])

    if row.get("review_correction"):
        raise ValueError(f"run {run_id!r} already carries a review correction")

    dispatched = _dispatched_head_for_row(row)
    if not dispatched:
        raise ValueError(
            f"run {run_id!r} names no dispatched head to compare against, so its "
            "attribution cannot be judged"
        )
    review = row.get("review")
    if not isinstance(review, Mapping):
        raise TypeError(f"run {run_id!r} carries no review to supersede")
    _base_carried, _base, _head_carried, recorded_head = carried_revision_pair(review)
    recorded = str(recorded_head or "").strip()
    if not recorded:
        raise ValueError(f"run {run_id!r} records no reviewed head to supersede")
    if same_revision(recorded, dispatched):
        raise ValueError(
            f"run {run_id!r} records the head it was dispatched to read "
            f"({recorded}); its review is correctly attributed and is left as it is"
        )

    correction = {
        "time": datetime.now(UTC).isoformat(),
        "reason": explanation,
        "dispatched_head": dispatched,
        "recorded_head": recorded,
        "superseded_field": "review",
    }
    corrected = dict(row)
    corrected["review_correction"] = correction

    if index is not None:
        rows[index] = corrected
        ledger.write(project, aggregate, version)
    else:
        ledger.write_run_file(project, corrected, mode=None)

    resolved_aggregate = ledger.ledger_path(project).resolve()
    checkout = resolved_aggregate.parents[2]
    paths = [ledger.ledger_path(project)]
    if resolved_source.is_file():
        paths.append(source)
    landing = _commit_landing_writes(
        run_id=run_id,
        verdict="superseded",
        checkout=checkout,
        paths=paths,
        subject=f"supersede-review({run_id}): append a review correction",
        body=(
            "Append one review correction to a misattributed run's committed "
            "ledger row, so every reader of its review reads the row as "
            "unreviewed without rewriting the fields the promotion recorded."
        ),
    )
    return {
        "project": project,
        "run_id": run_id,
        "correction": correction,
        "landing": landing,
    }


# The one refusing status a review's own fold answers. A run that ended
# normally left scope work outstanding for the review that read it, so a review
# that folded a finding back into that same node is a scope decision the
# boundary can be moved for. A run reporting failed states no such decision, so
# the fold does not reach it.
WIDEN_FOLDED_MANIFEST_STATUS = "complete"


WIDEN_RULE = (
    "only a run whose worker process has stopped is widened, "
    "because a live worker is already writing against the boundary a widening "
    "would move"
)

#: Where a reopen records what it granted, beside the run's other records.
REOPEN_RECORD_NAME = "reopened.json"


def _record_reopening(
    run_id: str,
    *,
    granted_paths: list[str],
    reason: str,
    delivered: Path | None,
) -> dict[str, Any]:
    """Record that a run was reopened, and what the reopen granted.

    A reopen is not a verdict, so it is not written as one: the run is still
    working towards a status of its own, and a status word here would be an
    outcome no attempt reached — exactly what left a resumed worker classified
    from its predecessor's delivery. What a reopen does owe a reader is the
    fact itself, its reason, the paths it granted and the delivered text it set
    aside, and those are recorded in the run's own directory where the attempt
    records live.
    """
    from reckon.crew import runs as runs_module

    record = {
        "run_id": run_id,
        "reason": reason,
        "granted_paths": list(granted_paths),
        "reopened_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    if delivered is not None:
        record["delivered"] = str(delivered)
    runs_module._write_json(runs_module.run_dir(run_id) / REOPEN_RECORD_NAME, record)
    return record


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


def _widen_folded_disposition(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """The folded dimension authorising this run's fence, or ``None``.

    A review that scored a dimension below its floor records what answered it in
    the run's own review record, and one of the closed kinds names the node the
    finding was folded into. When the node it names is the run's own node, the
    finding is the run's to repair, and the repair resume is the only moment the
    boundary can be moved for it: after the resume the run reads working and a
    widening refuses a live worker. So this read is what makes that grant
    reachable, and it is scoped as narrowly as the finding is -- a fold naming
    any other node is a finding some other run carries, and answers nothing here.

    The record read is the one the disposition writers rewrite, so a code review
    and a run review are both reached, and it is selected by the run's own id
    rather than by a head: the finding is about the run, not about one revision
    of it. A run with no stored record, a store that cannot be read, and a
    record carrying no dispositions all answer ``None``, which is the absence of
    a fold rather than an unreadable one, because the refusal a fold answers is
    stated elsewhere and a raise here would replace it.

    The returned row carries the dimension, the folded node and the recorded
    kind, so a caller records the grant against the finding that needed it.
    """
    project = str(record.get("project") or "").strip()
    run_id = str(record.get("run_id") or "").strip()
    node = record.get("node")
    node_id = str(node.get("id") or "").strip() if isinstance(node, Mapping) else ""
    if not project or not run_id or not node_id:
        return None
    from reckon.crew.review import DIMENSION_DISPOSITIONS_KEY
    from reckon.crew.review_dispositions import stored_record_for_disposition

    try:
        stored = stored_record_for_disposition(project, run_id)
    except (OSError, ValueError):
        return None
    if not isinstance(stored, Mapping):
        return None
    dispositions = stored.get(DIMENSION_DISPOSITIONS_KEY)
    if not isinstance(dispositions, Mapping):
        return None
    for dimension in sorted(dispositions):
        entry = dispositions.get(dimension)
        if not isinstance(entry, Mapping):
            continue
        if str(entry.get("kind") or "").strip() != "folded":
            continue
        if str(entry.get("node") or "").strip() != node_id:
            continue
        return {
            "dimension": str(dimension),
            "node": node_id,
            "kind": "folded",
        }
    return None


@crew.command(name="widen")
@click.option("--run", "run_id", required=True, help="Run whose fence to widen.")
@click.option(
    "--project", default=None, help="Project owning the run; cross-checks the run."
)
@click.option(
    "--write-path",
    "write_paths",
    multiple=True,
    required=True,
    help="Path to add to the run's declared write scope. Repeat for each path.",
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_widen(run_id, write_paths, pretty, project=None):
    """Add a path to any run whose worker process has stopped.

    Claim mutation checks the worker and peer claims under the pointer lock.
    The run's phase and manifest status describe its work, not whether its
    process can still write against the boundary being moved.
    """
    _accept_a_matching_project(run_id, project)
    crew_module, _ = _crew_modules()
    from reckon.crew.claims import ClaimAmendmentRefusedError, widen_claim

    try:
        updated, added = widen_claim(run_id, write_paths)
    except ClaimAmendmentRefusedError as exc:
        raise click.ClickException(
            f"{exc}; use `crew resume` once its turn has ended"
        ) from exc
    except crew_module.CrewError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_crew_result(
        {
            "run_id": run_id,
            "phase": str(updated.get("phase") or ""),
            "manifest_status": _manifest_reported_status(updated),
            "node": str((updated.get("node") or {}).get("id") or ""),
            "session_id": str(updated.get("session_id") or ""),
            "folded_disposition": _widen_folded_disposition(updated),
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
    default=None,
    help="Run whose obligations are deliberately deferred; live or promoted.",
)
@click.option(
    "--notice", "notice_id", default=None, help="Model notice to acknowledge."
)
@click.option(
    "--project", default=None, help="Project owning the run; cross-checks the run."
)
@click.option(
    "--reason",
    required=True,
    help="Why the deferral is deliberate, for example awaiting a peer's release.",
)
@click.option(
    "--until",
    default=None,
    help="ISO-8601 instant the deferral expires; the obligation returns after it.",
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_ack(run_id, notice_id, reason, until, pretty, project=None):
    """Defer one run's obligations until an instant, recording why.

    A live run's deferral is written beside its pointer; one for a run already
    promoted is written under the crew home and swept once its instant passes.
    """
    _accept_a_matching_project(run_id, project)
    if bool(run_id) == bool(notice_id):
        raise click.ClickException("exactly one of --run and --notice is required")
    if notice_id:
        if until:
            raise click.ClickException("--notice takes no --until")
        from reckon.crew.model_catalogue import acknowledge_notice

        try:
            updated = acknowledge_notice(notice_id, reason)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        _emit_crew_result(
            {"notice_id": notice_id, "acknowledgement": updated["acknowledgement"]},
            pretty,
        )
        return
    if not until:
        raise click.ClickException("--run requires --until")
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
    return


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
    """Run the project's declared suite and every declared population.

    Each runs under its own declared budget and is recorded as its own row, so a
    slow or browser-driven population keeps its time and its failure ids
    separable from the standing suite's: a population's failing run does not
    read as the standing suite's latest run and does not hold a promotion the
    standing suite would allow. One row per declared population plus the
    standing suite's own row is written and reported.
    """
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
    runs_dir = standing_suite.suite_runs_dir(root, project)
    standing_log = runs_dir / f"suite-{time.time_ns()}.log"
    record = standing_suite.run(root, declaration, standing_log)
    standing_suite.record(root, project, record)
    populations = []
    for name, population in standing_suite.population_declarations(config):
        population_log = runs_dir / f"population-{name}-{time.time_ns()}.log"
        population_record = standing_suite.run(
            root, population, population_log, population=name
        )
        path = standing_suite.record(root, project, population_record)
        populations.append(
            {"population": name, "path": str(path), "record": population_record}
        )
    # The standing suite's own fields ride at the top level, so the command's
    # exit verdict answers the standing suite alone: a named population's
    # failure is recorded on its own row and does not fail this command.
    _emit_crew_result(
        {"project": project, **record, "populations": populations}, pretty
    )


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


@crew.command(name="retention-sweep")
@click.option(
    "--project",
    default=None,
    help="Confine the pass to one mounted project; every mount by default.",
)
@click.option(
    "--limit",
    type=click.IntRange(min=1),
    default=None,
    help="Remove at most this many run directories in one pass.",
)
@click.option(
    "--apply",
    is_flag=True,
    help="Perform the removals; omission reports the exact dry run.",
)
@click.option(
    "--install-timer",
    "install_timer",
    is_flag=True,
    help="Write the hourly user service and timer, and start the timer.",
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_retention_sweep(project, limit, apply, install_timer, pretty):
    """Retire run directories past their retention window across every mounted project.

    The pass runs the run-directory retention alone, judging each directory
    against the ledger of the project that dispatched it, and removes at most
    one pass's worth so it stays short on shared storage. ``--install-timer``
    writes the hourly user units the sweep runs from.
    """
    from reckon.crew import gc as gc_module

    if install_timer:
        from reckon import service

        try:
            result = gc_module.install_retention_timer()
        except service.ServiceError as exc:
            raise click.ClickException(
                f"could not install the retention timer: {exc}"
            ) from exc
        _emit(
            {
                "changed": result["changed"],
                "service": str(result["service"]),
                "timer": str(result["timer"]),
            },
            pretty,
        )
        return
    settings = {} if limit is None else {"limit": limit}
    report = gc_module.retention_sweep(project=project, apply=apply, **settings)
    _emit_crew_result(report, pretty)


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
    _accept_a_matching_project(run_id, project)
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
@click.option("--run", "run_id", help="Run id to answer or reopen.")
@click.option("--project", help="Project whose ready runs to sweep.")
@click.option("--advice", default="", help="The orchestrator's answer.")
@click.option(
    "--ready", is_flag=True, help="Resume every newly ready run in a project."
)
@click.option(
    "--reopen", is_flag=True, help="Reopen a completed run before resuming it."
)
@click.option(
    "--write-path",
    "write_paths",
    multiple=True,
    help="Path to add to a reopened run's write scope. Repeat as needed.",
)
@click.option(
    "--backend",
    default="",
    help="Move the blocked run to this backend while retaining its identity.",
)
@click.option("--model", default="", help="Model served by the destination backend.")
@click.option(
    "--reason",
    default="",
    help="Why a backend move is required; recorded in the run's lane history.",
)
@click.option(
    "--estimated-hours",
    type=click.FloatRange(min=0.0, min_open=True),
    default=None,
    help="Neutral worker-hours for a backend move.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Report a ready-run sweep without resuming anything.",
)
@click.option(
    "--print-only",
    is_flag=True,
    help="Show the resume invocation without running it.",
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_resume(
    run_id,
    project,
    advice,
    ready,
    reopen,
    write_paths,
    backend,
    model,
    reason,
    estimated_hours,
    dry_run,
    print_only,
    pretty,
):
    """Resume one stopped run, every ready run, or a run on another backend.

    The ready sweep answers provider holds and declared external waits.
    Reopening grants paths and repairs a completed delivery before its session
    continues; a backend move may choose the lane's declared model.
    """
    _accept_a_matching_project(run_id, project)
    if ready:
        if not project or run_id or reopen or backend or model or write_paths or advice:
            raise click.UsageError("--ready needs --project and no single-run options")
        from reckon.crew.node import CrewError
        from reckon.crew.resumption import sweep

        try:
            report = sweep(project, dry_run=dry_run)
        except CrewError as exc:
            raise click.ClickException(str(exc)) from exc
        _emit_crew_result(report, pretty)
        return
    if dry_run:
        raise click.UsageError("--dry-run requires --ready")
    if not run_id:
        raise click.UsageError("--run is required unless --ready is set")
    if model and not backend:
        raise click.UsageError("--model requires --backend")
    if reason and not (backend or reopen):
        raise click.UsageError("--reason requires --backend or --reopen")
    if write_paths and not reopen:
        raise click.UsageError("--write-path requires --reopen")
    if reopen and (not write_paths or backend or print_only):
        raise click.UsageError(
            "--reopen needs --write-path and cannot combine with --backend or --print-only"
        )
    if not advice:
        if reopen or backend:
            advice = "continue"
        else:
            raise click.UsageError("--advice is required for a single-run resume")
    crew_module, flight_module = _crew_modules()
    from reckon.crew import runs as runs_module
    from reckon.crew.dispatch import LanePaused

    try:
        record = crew_module.read_pointer(run_id)
        if reopen:
            from reckon.crew.claims import widen_claim
            from reckon.crew.resumption import _reset_delivered_manifest_status

            if str(record.get("phase") or "") != "complete" and (
                _manifest_reported_status(record) != "complete"
            ):
                raise click.ClickException("--reopen requires a completed run")
            widen_claim(run_id, write_paths)
            # Reopening continues the run, so the delivered verdict has to go
            # before the resumed worker writes anything, exactly as a resume
            # clears it: left as it is, a live attempt is classified from the
            # delivery it is continuing and the run reads delivered while its
            # worker streams. The clearing is the resume path's own step, and it
            # is done here through that same owner rather than by writing a
            # status word of the reopen's own, which would be a verdict no
            # attempt reached. The delivered text is kept beside the manifest
            # under an attempt-named copy by that call.
            delivered = _reset_delivered_manifest_status(run_id, record)
            _record_reopening(
                run_id,
                granted_paths=list(write_paths),
                reason=reason or "reopening the completed run",
                delivered=delivered,
            )
            record = crew_module.read_pointer(run_id)
        project = str(record.get("project") or "")
        config = (
            _resolved_flight(
                flight_module,
                project,
                record.get("repo"),
                runs_module.recorded_dispatch_overrides(record),
            )
            if project
            else None
        )
        if backend:
            from reckon import ledger
            from reckon.crew.dispatch import change_lane

            destination = backend
            if model:
                entry = (config or {}).get("backends", {}).get(backend, {})
                lane = str(entry.get("lane") or backend)
                try:
                    destination, lane_name, model_key = ledger.backend_serving_model(
                        config or {}, lane=lane, model=model
                    )
                except ValueError as exc:
                    raise click.ClickException(str(exc)) from exc
                if lane_name and model_key:
                    config = flight_module.select_lane_model(
                        config, destination, lane_name, model_key
                    )

            moved = change_lane(
                run_id,
                destination,
                reason or "backend move requested",
                config=config or {},
                advice=advice,
                estimated_hours=estimated_hours,
                launch=not print_only,
            )
            _emit_crew_result(moved, pretty)
            return
        if print_only:
            plan = crew_module.resume_plan(run_id, advice, config=config)
            _emit_crew_result({"run_id": run_id, **plan.as_dict()}, pretty)
            return
        # One launch path serves this door and the sweep's, so the done-status
        # reset, the in-flight marker and the attempt record are not
        # re-composed here where CLI copy could drift from the launcher that
        # writes the attempt. The spawn is the package attribute this door has
        # always launched through, handed in so an override installed on it
        # still reaches the launch.
        from reckon.crew.resumption import _resume

        result = _resume(
            run_id,
            record,
            config=config,
            advice=advice,
            launcher=crew_module._spawn,
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
    plan = result["plan"]
    payload = {"run_id": run_id, **plan.as_dict()}
    payload.update(
        {
            "pid": result["pid"],
            "log_path": result["log_path"],
            "resumed_turn": result["turn"],
        }
    )
    _emit_crew_result(payload, pretty)


crew_redispatch = tombstone_command("redispatch")


@crew.command(name="stop")
@click.option("--run", "run_id", required=True, help="Run id to stop.")
@click.option(
    "--project", default=None, help="Project owning the run; cross-checks the run."
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_stop(run_id, pretty, project=None):
    """Stop one running spawned worker and record its stopped state."""
    _accept_a_matching_project(run_id, project)
    crew_module, _ = _crew_modules()
    try:
        record = crew_module.terminate(run_id)
    except crew_module.CrewError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_crew_result(record, pretty)


@crew.command(name="discard")
@click.option("--run", "run_id", required=True, help="Run id to discard.")
@click.option(
    "--project", default=None, help="Project owning the run; cross-checks the run."
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_discard(run_id, pretty, project=None):
    """Remove one non-running live pointer without recording promoted evidence."""
    _accept_a_matching_project(run_id, project)
    crew_module, _ = _crew_modules()
    try:
        result = crew_module.discard(run_id)
    except crew_module.CrewError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_crew_result(result, pretty)


@crew.group(name="queue")
def crew_queue():
    """A request's own state while it waits to launch.

    A queued or awaiting-upstream request is not yet a run: its steering text,
    its measure, its scope, its budget and its route are still the
    coordinator's, and dropping it costs a queue entry rather than a worker.
    Each verb here refuses a request that has launched.
    """


@crew_queue.command(name="edit")
@click.option("--run", "run_id", required=True, help="Queued request to change.")
@click.option(
    "--brief",
    "brief_path",
    default="",
    help="File whose bytes replace the request's steering text.",
)
@click.option(
    "--done-when",
    default=None,
    help="Replacement measure the launched worker is held to.",
)
@click.option(
    "--write-path",
    "write_paths",
    multiple=True,
    help="Replacement exclusive write path; repeat for each.",
)
@click.option("--time-budget", default=None, help="Replacement wall-clock allowance.")
@click.option("--backend", default="", help="Lane the request launches on.")
@click.option(
    "--model",
    default="",
    help="Model key, model id or alias within the lane named beside it.",
)
@click.option(
    "--project", default=None, help="Project owning the request; cross-checks it."
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_queue_edit(
    run_id,
    brief_path,
    done_when,
    write_paths,
    time_budget,
    backend,
    model,
    project,
    pretty,
):
    """Replace a waiting request's steering text, measure, scope, budget or route."""
    _accept_a_matching_project(run_id, project)
    from reckon.crew import queue_edit as queue_edit_module
    from reckon.crew.node import CrewError

    try:
        result = queue_edit_module.edit(
            run_id,
            steering_path_source=brief_path,
            done_when=done_when,
            write_paths=list(write_paths) if write_paths else None,
            time_budget=time_budget,
            backend=backend,
            model=model,
            project=project,
        )
    except (CrewError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    except click.ClickException:
        raise
    _emit_crew_result(result, pretty)


@crew_queue.command(name="reprioritise")
@click.option("--run", "run_id", required=True, help="Queued request to re-rank.")
@click.option(
    "--priority",
    type=int,
    required=True,
    help="Explicit priority override; higher sorts ahead of the roadmap key.",
)
@click.option("--reason", default="", help="Why the request is being re-ranked.")
@click.option(
    "--project", default=None, help="Project owning the request; cross-checks it."
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_queue_reprioritise(run_id, priority, reason, project, pretty):
    """Store an explicit priority override on a waiting request."""
    _accept_a_matching_project(run_id, project)
    from reckon.crew import queue_edit as queue_edit_module
    from reckon.crew.node import CrewError

    try:
        result = queue_edit_module.reprioritise(
            run_id, priority, reason=reason, project=project
        )
    except CrewError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_crew_result(result, pretty)


@crew_queue.command(name="drop")
@click.option("--run", "run_id", required=True, help="Queued request to withdraw.")
@click.option("--reason", default="", help="Why the request is being withdrawn.")
@click.option(
    "--project", default=None, help="Project owning the request; cross-checks it."
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_queue_drop(run_id, reason, project, pretty):
    """Remove a queued or awaiting-upstream request before it launches."""
    _accept_a_matching_project(run_id, project)
    from reckon.crew import queue_edit as queue_edit_module
    from reckon.crew.node import CrewError

    try:
        result = queue_edit_module.drop(run_id, reason=reason, project=project)
    except CrewError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_crew_result(result, pretty)


def _queue_hold_change(verb, run_id, plan, session, reason, project, pretty):
    from reckon.crew import queue_edit
    from reckon.crew.node import CrewError

    if run_id:
        _accept_a_matching_project(run_id, project)
    try:
        result = getattr(queue_edit, verb)(
            run_id, plan=plan, session=session, reason=reason, project=project
        )
    except CrewError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_crew_result(result, pretty)


@crew_queue.command(name="hold")
@click.option("--run", "run_id", default=None, help="One waiting request to hold.")
@click.option("--plan", default=None, help="Hold every waiting request of this plan.")
@click.option("--session", default=None, help="Hold this session's waiting requests.")
@click.option("--reason", default="", help="Why these requests are paused.")
@click.option(
    "--project", default=None, help="Project owning the request; cross-checks it."
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_queue_hold(run_id, plan, session, reason, project, pretty):
    """Pause one waiting request, a plan's requests or a session's requests."""
    _queue_hold_change("hold", run_id, plan, session, reason, project, pretty)


@crew_queue.command(name="release")
@click.option("--run", "run_id", default=None, help="One held request to release.")
@click.option("--plan", default=None, help="Release this plan's held requests.")
@click.option("--session", default=None, help="Release this session's held requests.")
@click.option("--reason", default="", help="Why these requests resume.")
@click.option(
    "--project", default=None, help="Project owning the request; cross-checks it."
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_queue_release(run_id, plan, session, reason, project, pretty):
    """Restore one held request, a plan's requests or a session's requests."""
    _queue_hold_change("release", run_id, plan, session, reason, project, pretty)


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


def _refuse_a_flag_as_a_reason(
    ctx: click.Context, param: click.Parameter, value: str
) -> str:
    """Refuse a reason-taking flag whose value is the next flag.

    A flag that takes a free-text reason is written ``--no-commit --gate
    passed`` the moment an operator omits the reason, and the parser then hands
    the next flag over as the reason: the promotion proceeds with ``--gate``
    recorded as the justification for a commitless landing, and the gate it
    meant to state is never read. The value is refused rather than parsed
    around, because a reason is prose and prose never opens with two dashes —
    so a value that does is the missing reason, whatever the operator typed it
    for. Refused in the option's own callback, which runs before the parser
    reaches whatever the swallowed flag was followed by, so the operator reads
    the omission rather than a complaint about the trailing word.
    """
    del ctx
    text = str(value or "").strip()
    if text.startswith("--"):
        raise click.BadParameter(
            f"{param.opts[0]} requires a reason, but it was given {text!r} — the "
            f"reason is missing, and {text} was read as its value. Repeat the "
            f"call with {param.opts[0]} '<why>' before {text}"
        )
    return value


@crew.command(name="complete")
@click.option("--run", "run_id", required=True, help="Run id to promote.")
@click.option(
    "--project", default=None, help="Project owning the run; cross-checks the run."
)
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
    callback=_refuse_a_flag_as_a_reason,
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
    callback=_refuse_a_flag_as_a_reason,
    default="",
    metavar="REASON",
    help="Promote despite added suite failures and record the reason and delta.",
)
@click.option(
    "--waive-boundary-refusal",
    callback=_refuse_a_flag_as_a_reason,
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
    callback=_refuse_a_flag_as_a_reason,
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
    callback=_refuse_a_flag_as_a_reason,
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
    callback=_refuse_a_flag_as_a_reason,
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
    callback=_refuse_a_flag_as_a_reason,
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
    callback=_refuse_a_flag_as_a_reason,
    default="",
    metavar="REASON",
    help=(
        "Promote a run whose recorded worker process is still alive and whose "
        "manifest status is not terminal, and record why it may land anyway. "
        "Refused when the run has no live, in-progress worker to waive."
    ),
)
@click.option(
    "--waive-domain-check",
    callback=_refuse_a_flag_as_a_reason,
    default="",
    metavar="REASON",
    help=(
        "Promote despite a declared domain invariant whose check command does "
        "not pass over the paths this run writes, and record which invariants "
        "were waived and why. Refused when no check is failing."
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
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    help=(
        "Evaluate every guard this promotion would apply and report all of "
        "their refusals together with the one invocation that would pass, "
        "writing nothing: no ledger row, no plan comment, no pointer change."
    ),
)
@click.option(
    "--land",
    "land",
    is_flag=True,
    help=(
        "After the promotion, merge the run's reviewed head onto the project's "
        "primary branch, refuse if the merge dropped the change, push it, and "
        "release the worktree. Refused with --dry-run, which writes nothing."
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
    waive_domain_check,
    plan_link,
    unplanned_reason,
    dry_run,
    land,
    pretty,
    project=None,
):
    """Promote one finished run into the owning repository's committed ledger.

    The ledger append happens before the pointer is deleted, so an interruption
    between them leaves a recoverable pointer rather than a lost record. A
    passing gate is refused unless it carries the check that produced it —
    this is the sole route by which a run is promoted, so the requirement is
    unconditional here even though the underlying ledger call remains
    permissive for internal callers assembling records from other evidence.

    With ``--land`` the promotion is followed by three reported steps: the
    reviewed head is merged onto the project's primary branch without touching
    the shared index, the merged tree is refused unless it carries a marker the
    change introduced and lacks any marker it removed, and the branch is pushed
    before the worktree is released. A step that fails stops the call and the
    report names the command that resumes from it.
    """
    _accept_a_matching_project(run_id, project)
    if land and dry_run:
        raise click.ClickException(
            "--land merges and pushes, which --dry-run promises not to do; run "
            "the promotion with --dry-run first, then promote with --land"
        )
    crew_module, _ = _crew_modules()
    ledger_module = _ledger_module()
    from reckon.crew import review as review_module
    from reckon.crew import runs as runs_module

    try:
        pointer = runs_module.read_pointer(run_id)
    except crew_module.CrewError:
        pointer = {}
    if accepted_paths and not dry_run:
        from reckon.crew.claims import widen_claim

        if any(not str(reason).strip() for _, reason in accepted_paths):
            raise click.ClickException("--accept-path requires a stated reason")
        original = pointer.get("accepted_scope_before_widening")
        declared = set(
            original
            if original is not None
            else (pointer.get("node") or {}).get("write_paths") or ()
        )
        named = [path for path, _ in accepted_paths]
        if len(set(named)) != len(named) or any(path in declared for path in named):
            raise click.UsageError(
                "--accept-path must name distinct paths outside the run's declared fence"
            )
        try:
            widen_claim(run_id, named, acceptance=True)
            pointer = runs_module.read_pointer(run_id)
        except crew_module.CrewError as exc:
            raise click.ClickException(str(exc)) from exc
    if (
        pointer
        and str(pointer.get("role") or (pointer.get("node") or {}).get("role") or "")
        == "review"
    ):
        delivered = []
        for declared in (pointer.get("node") or {}).get("write_paths") or ():
            source = Path(str(declared))
            if (
                source.parent.name != review_module.RUN_REVIEW_RECORD_DIRNAME
                or not source.is_file()
            ):
                continue
            try:
                import json

                body = json.loads(source.read_text(encoding="utf-8"))
                reasons = (
                    review_module.store_review_refusals(body)
                    if isinstance(body, dict)
                    else ["record is not an object"]
                )
            except (OSError, ValueError) as exc:
                reasons = [f"unreadable JSON: {exc}"]
            delivered.append({"record": str(source), "reasons": reasons})
        if delivered and all(entry["reasons"] for entry in delivered):
            _emit({"ok": False, "review_filed": False, "refused": delivered}, pretty)
            raise click.exceptions.Exit(1)
    gate_check = {
        "command": gate_command,
        "exit_status": gate_exit_status,
        "log_path": gate_log_path,
        "log_digest": gate_log_digest,
    }
    if dry_run:
        from reckon.crew import promotion as promotion_module

        _emit_crew_result(
            promotion_module.complete_dry_run(
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
                domain_waiver=waive_domain_check,
                plan_link=plan_link,
                unplanned_reason=unplanned_reason,
            ),
            pretty,
        )
        return
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
            domain_waiver=waive_domain_check,
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
    if land:
        report, exit_code = _land_a_promoted_run(
            result,
            run_id=run_id,
            gate=gate,
            checkout_path=checkout_path,
            pointer=pointer,
        )
        _emit_crew_result(report, pretty)
        if exit_code:
            raise click.exceptions.Exit(exit_code)
        return
    _emit_crew_result(result, pretty)


def _land_a_promoted_run(
    promoted: Mapping[str, Any],
    *,
    run_id: str,
    gate: str,
    checkout_path: Path | None,
    pointer: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], int]:
    """Merge, verify and push a promoted run's head, then release its worktree.

    Each step reports under ``steps``: the promotion that already happened, the
    merge that carries the reviewed head onto the primary branch, and the
    release that follows it. A refused step stops the call — nothing is pushed
    when the merge dropped the change — and its entry names the command that
    resumes from there rather than restating what the report already shows.
    Promotion is reported first because it is the step whose guards run before
    any code reaches the branch: a run promotion refuses lands nothing. The
    merge is also refused here, before it runs, when the run's worktree still
    exists at a head this merge would leave behind — the release step reads the
    same fact afterwards, which is after the branch has moved.

    The workspace the release reads is carried over from the live pointer
    captured before the promotion deleted it: the committed ledger row states
    the run's outcome and its release, not the path of the tree the release has
    to remove.
    """
    import shlex

    from reckon.crew import merge as merge_module
    from reckon.crew import promotion_release as release_module

    project = str(promoted.get("project") or "")
    record: dict[str, Any] = dict(promoted.get("record") or {})
    for key in ("worktree", "repo", "scratch", "pid", "pid_start_time"):
        if key not in record and key in (pointer or {}):
            record[key] = pointer[key]
    report: dict[str, Any] = {
        "ok": False,
        "run_id": run_id,
        "project": project,
        "steps": {"promote": {"ok": True}},
        "promotion": promoted,
    }
    head = _reviewed_head_of(record)
    if not head:
        report["steps"]["land"] = {
            "ok": False,
            "error": (
                "the promoted run records no commit to land, so there is no "
                "reviewed head to merge"
            ),
        }
        report["worktree_released"] = bool(
            (promoted.get("release") or {}).get("worktree_released")
        )
        return report, 1
    try:
        repository = merge_module.project_repository(project)
    except merge_module.MergeError as exc:
        report["steps"]["land"] = {"ok": False, "error": str(exc)}
        return report, 1
    message = f"land({run_id}): merge the reviewed head onto the primary branch"
    body = (
        f"Run {run_id} was promoted, so its reviewed head {head} is merged "
        "onto the branch and reaches every reader rather than only the "
        "worktree the change was written in."
    )

    def _merge_command(revision: str) -> str:
        return shlex.join(
            [
                "reckon",
                "crew",
                "merge",
                "--project",
                project,
                "--head",
                revision,
                "--message",
                message,
                "--body",
                body,
            ]
        )

    resume_merge = _merge_command(head)
    # The release step reads this same reachability fact after the merge, and
    # reads it too late: a head behind the run's own work merges, pushes, and
    # only then reports that the worktree it left behind carries commits no
    # reader has. The worktree is still on disk at this point, so the fact is
    # readable before anything is merged or pushed.
    worktree_value = str(record.get("worktree") or "").strip()
    worktree = Path(worktree_value) if worktree_value else None
    worktree_head = ""
    if worktree is not None and worktree.is_dir():
        try:
            worktree_head = merge_module._rev_parse(worktree, "HEAD")
        except merge_module.MergeError:
            worktree_head = ""
    if (
        worktree_head
        and worktree_head != head
        and not merge_module._is_ancestor(repository, worktree_head, head)
    ):
        worktree_merge = _merge_command(worktree_head)
        report["steps"]["land"] = {
            "ok": False,
            "error": (
                f"the run's worktree {worktree} is at {worktree_head}, which is "
                f"neither the reviewed head {head} being merged nor an ancestor "
                f"of it; merge the worktree's head instead with `{worktree_merge}`"
            ),
            "reviewed_head": head,
            "worktree_head": worktree_head,
            "resume": worktree_merge,
        }
        report["worktree_released"] = bool(
            (promoted.get("release") or {}).get("worktree_released")
        )
        return report, 1
    try:
        landed = merge_module.land(repository, head=head, message=message, body=body)
    except merge_module.MergePushOutstandingError as exc:
        report["steps"]["land"] = {
            "ok": True,
            "push_outstanding": True,
            "resume": resume_merge,
            **{key: exc.report[key] for key in ("head", "commit", "base")},
        }
        report["worktree_released"] = False
        report["error"] = str(exc)
        return report, merge_module.PUSH_OUTSTANDING_EXIT
    except Exception as exc:  # noqa: BLE001 - any failure after promotion is a refused land
        entry, code = _merge_land_refusal(exc, resume=resume_merge)
        report["steps"]["land"] = entry
        report["worktree_released"] = bool(
            (promoted.get("release") or {}).get("worktree_released")
        )
        return report, code
    report["steps"]["land"] = {"ok": True, **landed}
    release = release_module._release_after_promotion(
        run_id, record, None, gate=str(gate)
    )
    repository_value = str(record.get("repo") or "").strip()
    # The release records its own outcome as a second ledger commit, so the
    # branch moves again after the push. The remote is left equal to the local
    # branch rather than behind it. A push that fails here is the same shape as
    # a landing whose push failed: the work is committed and only the push is
    # outstanding.
    release, recorded = release_module._record_release_on_ledger(
        project=project,
        root=checkout_path,
        run_id=run_id,
        release=release,
        checkout=Path(repository_value) if repository_value else None,
    )
    if recorded is not None:
        report["promotion"] = {**promoted, "record": recorded}
    report["steps"]["release"] = {
        "ok": bool(release.get("worktree_released")),
        **release,
    }
    branch = str(landed.get("branch") or "")
    remote = str(landed.get("remote") or "origin")
    if branch and repository_value:
        try:
            head_now = merge_module._rev_parse(Path(repository_value), branch)
            pushed_head = str(landed.get("remote_head") or "")
        except merge_module.MergeError as exc:  # pragma: no cover - reported, not fatal
            report["steps"]["release"]["push_error"] = str(exc)
            head_now = pushed_head = ""
    else:
        head_now = pushed_head = ""
    if head_now and head_now != pushed_head:
        try:
            pushed = merge_module._push_and_confirm(
                Path(repository_value), remote, head_now, branch
            )
        except merge_module._PushError as exc:
            report["steps"]["release"]["push_error"] = str(exc)
        else:
            report["steps"]["land"]["remote_head"] = pushed
            report["steps"]["release"]["release_commit_pushed"] = head_now
    report["release"] = release
    if not release.get("worktree_released"):
        report["steps"]["release"]["resume"] = shlex.join(
            ["reckon", "crew", "gc", "--project", project, "--apply"]
        )
        report["error"] = (
            "the reviewed head landed and was pushed, but the worktree was "
            "withheld: " + str(release.get("worktree_withheld") or "no reason given")
        )
        return report, 1
    report["ok"] = True
    return report, 0


def _reviewed_head_of(record: Mapping[str, Any]) -> str:
    """Return the commit a promoted run's worktree was reviewed at.

    The promoted revision is the head a review read; where the row recorded no
    revision the run's own commits name it, newest last, so the change that
    reached the branch is the one the ledger already attributes to the run.
    """
    promoted_revision = str(record.get("promoted_revision") or "").strip()
    if promoted_revision:
        return promoted_revision
    commits = [str(sha).strip() for sha in (record.get("commits") or ())]
    return next((sha for sha in reversed(commits) if sha), "")


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
    _accept_a_matching_project(run_id, project)
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


# Preserve the CLI re-export while the retired names resolve through the crew
# group's tombstone lookup.
crew_list = tombstone_command("list")


crew_ledger = tombstone_command("ledger")


crew_velocity = tombstone_command("velocity")


def _budget_reset_reach() -> dict[str, Any]:
    """Name the sessions a reset announcement reaches, and those it does not.

    The pane that draws a reset row is the one a live follower registration
    feeds, so the liveness verdict is ``follower_registration.list_followers``'s
    own and nothing here decides it again. For every mounted project this reads
    that project's registrations and folds them by session: a session holding
    any live registration is reached, and a session whose registrations are all
    not live is listed separately, because that is the session the announcer
    has to message by hand. A session's own identity is its registration's,
    not this process's, and the read writes nothing.
    """
    from reckon.crew import follower_registration
    from reckon.flight import mounted_project_docs

    reached: set[str] = set()
    unreached: set[str] = set()
    for project in sorted(mounted_project_docs()):
        for row in follower_registration.list_followers(project):
            name = str(row.get("session") or "")
            if not name:
                continue
            if row.get("live"):
                reached.add(name)
                unreached.discard(name)
            elif name not in reached:
                unreached.add(name)
    reached_sessions = sorted(reached)
    unreached_sessions = sorted(unreached)
    total = len(reached_sessions) + len(unreached_sessions)
    if total:
        detail = (
            f"the announcement reaches {len(reached_sessions)} of {total} "
            f"registered sessions' panes; message the "
            f"{len(unreached_sessions)} listed under 'unreached' directly"
        )
    else:
        detail = (
            "no session holds a follower registration, so no pane will draw the row"
        )
    return {
        "reached": reached_sessions,
        "unreached": unreached_sessions,
        "detail": detail,
    }


def _budget_reset_payload(
    group,
    mark_available,
    mark_used,
    by,
    reason=None,
    clock=None,
    session=None,
    starts_at=None,
):
    """Set, clear or read whether a budget group's banked reset is available or used.

    A metered subscription can carry one banked reset, which grants one extra
    full window of allowance while it is available. With no action flag the
    current state is printed; ``--available`` flags one (never stacking) and
    ``--used`` records the reset, which clears the flag when the event is global.
    Budget preflight and the picker read the flag from the same durable record,
    so flagging it here changes the pace they report, and the event itself is
    what a pane announces and ``crew budget --list`` shows. ``--used`` also
    reports the announcement's reach, from every mounted project's follower
    registrations, so the recorder learns which sessions it has to tell by hand.
    """
    from reckon.crew import budget_reset as budget_reset_module

    if mark_available and mark_used:
        raise click.ClickException("--available and --used are mutually exclusive")
    if mark_available:
        result = budget_reset_module.mark_available(group, by=by)
    elif mark_used:
        if not (isinstance(reason, str) and reason.strip()):
            raise click.UsageError("--reason is required with --reset --used")
        moment = None
        if starts_at:
            moment = _lift_moment(starts_at, now=datetime.now(UTC))
            if moment is None:
                raise click.ClickException(
                    f"--from {starts_at!r} is neither an ISO-8601 time nor a duration"
                )
        result = budget_reset_module.mark_used(
            group,
            by=by,
            moment=moment,
            reason=reason,
            clock=clock,
            session=session,
        )
        # The announcement's audience is the whole point of recording it: a
        # pane fed by a live registration draws the row, and the sessions left
        # out are the ones the announcer has to reach some other way.
        result["reach"] = _budget_reset_reach()
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
    return result


_LIFT_CLOCKS = ("seven_day", "five_hour")


def _lift_moment(text, *, now):
    """Resolve a lift time expression to an aware instant, or ``None``.

    An ISO-8601 stamp is read directly. Anything else is a duration -- an
    integer with an ``s``/``m``/``h`` unit, as :func:`reckon.crew.node.parse_duration`
    reads -- and is taken as an offset from ``now``, so ``--until 12h`` means
    twelve hours from the grant, and ``--drain-by 6h`` means the same. A text
    that parses as neither yields ``None`` and the caller refuses it.
    """
    from reckon.crew import budget_lift as budget_lift_module
    from reckon.crew.node import CrewError, parse_duration

    stamp = budget_lift_module._parse_stamp(text)
    if stamp is not None:
        return stamp
    try:
        seconds = parse_duration(str(text).strip())
    except CrewError:
        return None
    if seconds is None:
        return None
    return now + timedelta(seconds=int(seconds))


def _lift_ends(*, until, clock, now):
    """Build ``grant``'s ``ends`` block from ``--until``/``--clock``.

    The default ``reset`` anchors the lift to the clock's own reset, which the
    record resolves at expiry so the lift ends when the window does whatever
    that turns out to be. An explicit ISO stamp or duration pins the lift's end
    instead.
    """
    text = str(until or "").strip()
    if not text or text == "reset":
        return {"kind": "reset", "clock": clock}
    moment = _lift_moment(text, now=now)
    if moment is None:
        raise click.ClickException(
            f"--until {text!r} is neither 'reset', an ISO-8601 time nor a duration"
        )
    return {"kind": "at", "at": moment.isoformat()}


def _budget_lift_payload(
    group,
    multiple,
    drain_by,
    uncapped,
    global_scope,
    session,
    starts_at,
    until,
    clock,
    reason,
    clear_flag,
    lift_id,
    credits=None,
):
    """Grant or clear a lift on a budget group's pace.

    A lift raises one declared group's pace ahead of its configured rate, or
    releases it from the pace hold entirely, until its end. It is a grant, not
    a spend, so a run cannot lift its own group's budget; the command is
    refused while ``RECKON_RUN_ID`` is set. The projection is printed against
    the group's own reset and the grant is admitted regardless of it. ``--clear``
    with an optional ``--session`` or ``--id`` revokes a lift in force.
    """
    from reckon.crew import budget_lift as budget_lift_module

    form_flags = [
        multiple is not None,
        drain_by is not None,
        uncapped,
        credits is not None,
    ]
    try:
        if clear_flag:
            if any(form_flags):
                raise click.ClickException(
                    "--clear takes none of --multiple, --drain-by, --uncapped "
                    "or --credits"
                )
            cleared = budget_lift_module.clear(
                group=group,
                lift_id=lift_id,
                session=session,
                path=None,
            )
            if cleared is None:
                return {
                    "ok": True,
                    "action": "clear",
                    "group": group,
                    "cleared": None,
                    "lift_id": lift_id,
                    "detail": "no in-force lift matched; nothing was revoked",
                }
            return {
                "ok": True,
                "action": "clear",
                "group": group,
                "cleared": cleared,
                "lift_id": cleared.get("id"),
                "detail": f"revoked lift {cleared.get('id')} on {group}",
            }

        if sum(form_flags) != 1:
            raise click.ClickException(
                "name exactly one lift form: --multiple, --drain-by, --uncapped "
                "or --credits"
            )
        if global_scope and session:
            raise click.ClickException(
                "--global and --session are mutually exclusive; a session lift is "
                "not global"
            )
        if not (isinstance(reason, str) and reason.strip()):
            raise click.ClickException("--reason is required to grant a lift")

        _, flight_module = _crew_modules()
        config = _resolved_flight(flight_module, None, None, ())

        balance_at_grant = None
        if uncapped:
            form = budget_lift_module.UNCAPPED
        elif credits is not None:
            form = budget_lift_module.CREDITS
            balance_at_grant = _group_credit_balance(config, group)
        elif drain_by is not None:
            form = budget_lift_module.DRAIN_BY
        else:
            form = budget_lift_module.MULTIPLE

        now = datetime.now(UTC)
        starts = None
        if starts_at:
            starts = _lift_moment(starts_at, now=now)
            if starts is None:
                raise click.ClickException(
                    f"--from {starts_at!r} is neither an ISO-8601 time nor a duration"
                )

        target = None
        if form == budget_lift_module.DRAIN_BY:
            target = _lift_moment(drain_by, now=now)
            if target is None:
                raise click.ClickException(
                    f"--drain-by {drain_by!r} is neither an ISO-8601 time nor a "
                    "duration"
                )

        ends = _lift_ends(until=until, clock=clock, now=now)
        readings = budget_lift_module.published_readings(config, group=group, now=now)

        lift = budget_lift_module.grant(
            config,
            group=group,
            reason=reason,
            form=form,
            multiple=multiple,
            target=target,
            clock=clock,
            ends=ends,
            scope=("session" if session else budget_lift_module.GLOBAL),
            session=session,
            starts_at=starts,
            readings=readings,
            now=now,
            credits=credits,
            balance_at_grant=balance_at_grant,
        )
    except budget_lift_module.LiftRefusedError as exc:
        raise click.ClickException(str(exc)) from exc

    scope = lift.get("scope")
    return {
        "ok": True,
        "action": "grant",
        "group": group,
        "lift_id": lift.get("id"),
        "form": lift.get("form"),
        "multiple": lift.get("pace_multiple"),
        "scope": scope,
        "clock": clock,
        "ends": lift.get("ends"),
        "projected_exhaustion": lift.get("projected_exhaustion"),
        "admitted": True,
        "lift": lift,
        "detail": (
            f"granted lift {lift.get('id')} on {group}: the group's budget "
            "is admitted regardless of when the projection lands"
        ),
    }


def _group_credit_balance(config, group):
    """Read the credit balance a credit lift is measured from, or ``None``.

    The balance comes from the account surface of the group's first member that
    reads one, at the moment of the grant. A grant with no readable balance is
    refused downstream rather than measured from a guess.
    """
    from reckon import budget as budget_module
    from reckon.crew import budget_group as budget_group_module

    backends = (config or {}).get("backends") or {}
    members = budget_group_module.declared_groups(config).get(group) or ()
    for name in members:
        settings = backends.get(name)
        if not isinstance(settings, Mapping) or not settings.get("budget_check"):
            continue
        state = budget_module.state_for(name, settings)
        if state.credit_balance is not None:
            return state.credit_balance
    return None


def _budget_lifts_payload(group):
    """List active and recent budget lifts, newest first, beside the day's resets.

    Ended lifts are kept as history so a multiple can be tuned against what it
    did, so this prints every lift on record. A lift still in force carries a
    null ``cleared_at`` and an ``ends`` block not yet reached. The resets are
    the last day's events — the announced ones and the ones the window readings
    detected — so a lift can be read against the reset it answered.
    """
    from reckon.crew import budget_lift as budget_lift_module
    from reckon.crew import budget_reset as budget_reset_module

    lifts = budget_lift_module.list_lifts(group=group, path=None)
    resets = budget_reset_module.recent_resets(horizon=timedelta(hours=24))
    if group:
        resets = [row for row in resets if row.get("group") == group]
    return {
        "ok": True,
        "group": group,
        "count": len(lifts),
        "lifts": lifts,
        "reset_count": len(resets),
        "resets": resets,
    }


@crew.command(name="budget")
@click.option(
    "--lift",
    "do_lift",
    is_flag=True,
    help="Grant or clear a lift on a budget group's pace.",
)
@click.option(
    "--list",
    "do_list",
    is_flag=True,
    help=(
        "List active and recent budget lifts, newest first, beside the last "
        "day's reset events."
    ),
)
@click.option(
    "--reset",
    "do_reset",
    is_flag=True,
    help=(
        "Flag, record or read whether a group's banked reset is available or was used."
    ),
)
@click.option(
    "--check",
    "do_check",
    is_flag=True,
    help="Report whether backend budget state allows a wave to open.",
)
@click.option(
    "--group",
    default=None,
    help=(
        "Budget group the action applies to. Required with --lift and --reset, "
        "optional with --list."
    ),
)
@click.option(
    "--multiple",
    type=float,
    default=None,
    help=(
        "With --lift, grant a pace multiple above the group's configured rate. "
        "One of --multiple, --drain-by or --uncapped names the lift's form."
    ),
)
@click.option(
    "--drain-by",
    default=None,
    help=(
        "With --lift, hold the pace until the group's window is drained by this "
        "time (an ISO-8601 stamp or a duration) and then resume."
    ),
)
@click.option(
    "--uncapped",
    is_flag=True,
    help="With --lift, release the group to its whole window, no pace cap.",
)
@click.option(
    "--credits",
    type=float,
    default=None,
    help=(
        "With --lift, admit the group past a spent window on purchased credits "
        "until this many credits have been drawn since the grant."
    ),
)
@click.option(
    "--global",
    "global_scope",
    is_flag=True,
    help="With --lift, grant a workstation-wide lift (the default scope).",
)
@click.option(
    "--session",
    default=None,
    help=(
        "With --lift, scope the lift to one session id rather than the "
        "workstation. With --reset --used, scope the reset to one session, "
        "leaving the banked-reset flag alone; omit it for the account's own reset."
    ),
)
@click.option(
    "--from",
    "starts_at",
    default=None,
    help=(
        "With --lift, when the lift begins: an ISO-8601 time or a duration from "
        "now. With --reset --used, when the reset happened, in the same forms; "
        "default: now."
    ),
)
@click.option(
    "--until",
    default="reset",
    show_default=True,
    help=(
        "With --lift, 'reset' (track the clock's own reset), an ISO-8601 time, or "
        "a duration from now, at which the lift ends."
    ),
)
@click.option(
    "--clock",
    type=click.Choice(_LIFT_CLOCKS),
    default="seven_day",
    show_default=True,
    help=(
        "With --lift, which rate-limit window the lift is anchored to. With "
        "--reset --used, which window the reset belongs to."
    ),
)
@click.option(
    "--reason",
    default=None,
    help=(
        "With --lift, why the group's pace is being raised; required for a grant. "
        "With --reset --used, why the reset happened; required."
    ),
)
@click.option(
    "--clear",
    "clear_flag",
    is_flag=True,
    help="With --lift, revoke an in-force lift on the group instead of granting one.",
)
@click.option(
    "--id",
    "lift_id",
    default=None,
    help=(
        "With --lift --clear, revoke this exact lift id rather than the group's "
        "governing lift."
    ),
)
@click.option(
    "--available",
    "mark_available",
    is_flag=True,
    help=(
        "With --reset, flag one banked reset as available. Idempotent: flagging a "
        "group that already carries one changes nothing and says so."
    ),
)
@click.option(
    "--used",
    "mark_used",
    is_flag=True,
    help=(
        "With --reset, record that the reset happened, clearing a banked flag "
        "when the event is global, and report which sessions the announcement "
        "reaches. Requires --reason."
    ),
)
@click.option(
    "--by",
    default=None,
    help="With --reset, who is setting or clearing the flag (default: $USER).",
)
@click.option(
    "--project",
    default=None,
    help="With --check, the project whose run records are read.",
)
@click.option(
    "--role",
    "roles",
    multiple=True,
    help=(
        "With --check, a role taking part in the wave; its backend is checked. "
        "Repeat as needed."
    ),
)
@click.option(
    "--backend",
    "backends",
    multiple=True,
    help="With --check, a backend to check by name, instead of resolving roles.",
)
@click.option(
    "--model",
    "models",
    multiple=True,
    help=(
        "With --check, a model key, model id or alias to check, resolved through "
        "the catalogue and added to the backends checked. Repeat as needed."
    ),
)
@click.option(
    "--purpose",
    type=click.Choice(["dispatch", "resume"]),
    default="dispatch",
    show_default=True,
    help="With --check, a dispatch keeps back the resume reserve; a resume may spend it.",
)
@click.option(
    "--checkout-path",
    default=None,
    type=click.Path(path_type=Path),
    help="With --check, the repo root whose ledger and project flight layer are read.",
)
@click.option(
    "--set",
    "overrides",
    multiple=True,
    metavar="KEY=VALUE",
    help="With --check, a flight override that always wins over config layers.",
)
@click.option(
    "--ready",
    "ready",
    multiple=True,
    metavar="NAME=GROUP:SCORE",
    help=(
        "With --check, a node a wave would open with: its name, its declared "
        "wallet and its open-endedness score. Repeat as needed."
    ),
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_budget(
    do_lift,
    do_list,
    do_reset,
    do_check,
    group,
    multiple,
    drain_by,
    uncapped,
    credits,
    global_scope,
    session,
    starts_at,
    until,
    clock,
    reason,
    clear_flag,
    lift_id,
    mark_available,
    mark_used,
    by,
    project,
    roles,
    backends,
    models,
    purpose,
    checkout_path,
    overrides,
    ready,
    pretty,
):
    """Report one budget group's pace, its banked reset, its lifts or backend holds.

    Four actions share one verb, each named by its own flag and giving the
    answer the retired verb it replaced gave: ``--lift`` grants or clears a
    pace lift on a group, ``--list`` prints the lifts on record, ``--reset``
    flags, clears or reads a group's banked reset, and ``--check`` reports
    whether backend budget state allows a wave to open without spending any of
    it, exiting 3 when a backend is held. Name exactly one; a retired name
    answers with the flag to use.
    """
    chosen = [
        flag
        for flag, set_flag in (
            ("--lift", do_lift),
            ("--list", do_list),
            ("--reset", do_reset),
            ("--check", do_check),
        )
        if set_flag
    ]
    if len(chosen) != 1:
        raise click.UsageError(
            "name exactly one action: --lift, --list, --reset or --check"
        )

    if do_check:
        if project is None:
            raise click.UsageError("--project is required with --check")
        report, code = _budget_check_report(
            project,
            roles,
            backends,
            models,
            purpose,
            checkout_path,
            overrides,
            ready,
        )
        _emit_crew_result(report, pretty, failure_exit=3)
        raise click.exceptions.Exit(code)

    if group is None and not do_list:
        raise click.UsageError("--group is required with --lift and --reset")

    if do_list:
        _emit_crew_result(_budget_lifts_payload(group), pretty)
        return

    if do_reset:
        result = _budget_reset_payload(
            group,
            mark_available,
            mark_used,
            by,
            reason=reason,
            clock=clock,
            session=session,
            starts_at=starts_at,
        )
    else:
        result = _budget_lift_payload(
            group=group,
            multiple=multiple,
            drain_by=drain_by,
            uncapped=uncapped,
            global_scope=global_scope,
            session=session,
            starts_at=starts_at,
            until=until,
            clock=clock,
            reason=reason,
            clear_flag=clear_flag,
            lift_id=lift_id,
            credits=credits,
        )
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


@crew.command(name="admit")
@click.option(
    "--lane",
    default="",
    help="Lane whose queued dispatches are admitted; the default admits every lane.",
)
@click.option(
    "--once",
    is_flag=True,
    help="Run a single admission pass and return instead of looping on a cadence.",
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_admit(lane, once, pretty):
    """Launch queued dispatches while the lane grants a slot.

    One pass reads the fleet gate, then the queued run pointers, and launches
    each in fair-share order through the same dispatch path a coordinator uses.
    A held or paused gate launches nothing, and a launch the shared path
    refuses leaves the request queued with the failure recorded.

    The bare form is the loop: it repeats that pass on its cadence until it is
    stopped, printing each pass's verdicts as one JSON line, and it is what the
    fleet runs as its admit service. ``--once`` is the single manual pass: it
    runs one pass and returns instead of looping. One pass is bounded by the
    lane and fleet gate reads it makes, each under a five-second deadline, so a
    caller that needs it to return asks for ``--once`` rather than wrapping the
    loop in a timeout of its own.
    """
    from reckon.crew import queue_admission
    from reckon.crew.node import CrewError

    try:
        result = queue_admission.admit_until_stopped(lane=lane, once=once)
    except CrewError as exc:
        raise click.ClickException(str(exc)) from exc
    # A pass that could not launch a queued request, or could not read the
    # fleet gate at all, is a negative verdict. A gate that is paused on
    # purpose admits nothing and remains a successful pass.
    _emit_crew_result(
        {
            **result,
            "ok": not result.get("failed") and result.get("gate") != "unreadable",
        },
        pretty,
    )
