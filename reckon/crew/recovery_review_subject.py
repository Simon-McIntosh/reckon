# ruff: noqa: I001, UP035
from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from reckon import ledger, review_tiers
from reckon.crew import plan_review, runs
from reckon.crew import review as review_module
from reckon.crew.node import (
    CrewError,
)



# The role a read-only investigation is dispatched under. A run carrying it
# writes a report and commits no source, so its review checks the report's
# claims rather than a diff.
_INVESTIGATE_ROLE = "investigate"


def _is_review_node(record: Mapping[str, Any]) -> bool:
    """Whether a run was minted as some run's review.

    The node id is the identity the review dispatch names, so a pointer written
    before a pointer carried a role is still recognisable as the reviewer. The
    role is the primary key — it survives a renamed node id — and this is the
    second, because either alone leaves a run that composes a review of itself
    and each link of that chain is a real dispatch against a real member.
    """
    node = record.get("node") or {}
    node_id = node.get("id") if isinstance(node, Mapping) else node
    return str(node_id or "").startswith((REVIEW_NODE_PREFIX, PLAN_REVIEW_NODE_PREFIX))


def _review_tree(record: Mapping[str, Any]) -> Path | None:
    """The tree whose head a review of this run must describe, when readable.

    A record that names neither a worktree nor a repository has no tree, and
    reports ``None``: the empty string is not a path to fall back on, because it
    resolves to the current working directory, which would make the caller read
    whatever checkout the process happens to stand in rather than the run's own.
    """
    worktree_raw = str(record.get("worktree") or "").strip()
    if worktree_raw:
        worktree = Path(worktree_raw)
        if worktree.is_dir():
            return worktree
    repo_raw = str(record.get("repo") or "").strip()
    if repo_raw:
        repo = Path(repo_raw)
        if repo.is_dir():
            return repo
    return None


def _revision_at(tree: Path, timestamp: str) -> str:
    """The revision ``tree`` carried as its head at ``timestamp``."""
    if not timestamp:
        return ""
    try:
        completed = subprocess.run(
            ["git", "rev-list", "-1", f"--before={timestamp}", "HEAD"],
            cwd=tree,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return ""
    head = completed.stdout.strip()
    if completed.returncode or not re.fullmatch(r"[0-9A-Fa-f]{40,64}", head):
        return ""
    return head


def review_described_head(
    review: Mapping[str, Any] | None, *, tree: Path | None
) -> str:
    """The revision a stored review describes, or empty when it names none.

    A record carrying any of the revision spellings answers directly. A record
    predating the field carries none, so the revision it read is reconstructed
    from the reviewed run's own history — the head that tree carried at the
    moment the record was written. The comparison built on this is between two
    revisions, never between two timestamps: a timestamp says which was written
    first, not which revision was read.
    """
    if not review:
        return ""
    _, _, carried, head = review_module.carried_revision_pair(review)
    if carried and head:
        return head
    if tree is None:
        return ""
    return _revision_at(tree, str(review.get("timestamp") or "").strip())


def same_revision(left: str, right: str) -> bool:
    """Whether two spellings name the same commit, abbreviated or full."""
    left = str(left or "").strip().lower()
    right = str(right or "").strip().lower()
    if not left or not right:
        return False
    return left == right or left.startswith(right) or right.startswith(left)


def select_review_for_head(
    project: str,
    run_id: str,
    head: str,
    *,
    tree: Path | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Return the stored review describing ``head`` and any other head seen.

    Selection is shared by the classifier and the promotion gate so both agree
    on which stored record is evidence about a revision: a classifier reading a
    different record than promotion does is how a run reads promotable and is
    then refused, or reads scoring while a matching record sits on disk.

    The first element is the record describing ``head`` — or, for a legacy
    record that names no revision, the record reconstructed to ``head`` — and
    the second is a head a non-matching record did name, empty when none, so a
    refusal can name the two revisions that disagree rather than report an
    absence. A record naming a different revision is not this run's review
    however recently it was written. An empty ``head`` names no revision to
    key a record on, and this selection accepts none for it: reading whatever
    the store holds newest would let a review of an unknown revision stand as
    this run's evidence. The classifier, which holds the record the head came
    from, reads the newest record itself for the one empty-head case with no
    reclaimed worktree behind it.
    """
    if not head:
        return None, ""
    stored = review_module.read_review(project, run_id, reviewed_head_sha=head)
    if stored is not None:
        return ledger.normalize_identity(stored), ""
    newest = review_module.read_review(project, run_id)
    if newest is None:
        return None, ""
    described = review_described_head(newest, tree=tree)
    if not described:
        # A record naming no revision predates the field; refusing every review
        # stored before it existed would refuse older records that are correct.
        return ledger.normalize_identity(newest), ""
    if same_revision(described, head):
        return ledger.normalize_identity(newest), ""
    return None, described


def _reviewed_run_head(record: Mapping[str, Any]) -> str:
    """The revision a review of this run is evidence about: its tree's head."""
    tree = _review_tree(record)
    if tree is None:
        return ""
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tree,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return ""
    head = completed.stdout.strip()
    if completed.returncode or not re.fullmatch(r"[0-9A-Fa-f]{40,64}", head):
        return ""
    return head


def _resolve_commit(tree: Path, candidate: str) -> str:
    """The canonical object id ``candidate`` names in ``tree``, or empty.

    ``--verify`` refuses a value that names no commit rather than echoing it
    back, and ``--end-of-options`` keeps a candidate that begins with a dash
    from being read as an option, so a caller can tell a revision that
    resolved from one merely written down. The query is bounded so a
    repository on a stalled filesystem cannot hold a caller open.
    """
    if not candidate:
        return ""
    try:
        completed = subprocess.run(
            [
                "git",
                "rev-parse",
                "--verify",
                "--quiet",
                "--end-of-options",
                f"{candidate}^{{commit}}",
            ],
            cwd=tree,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    sha = completed.stdout.strip()
    if completed.returncode or not re.fullmatch(r"[0-9A-Fa-f]{40,64}", sha):
        return ""
    return sha


def _commit_candidates(entry: Any) -> list[str]:
    """The revision spellings to try for one manifest ``commits:`` entry.

    The whole entry is tried first, so a branch name or a full sha resolves as
    written. A sha embedded in a sentence is tried next, because a manifest
    that names its revision inside prose names it just as truly as one that
    does not.
    """
    text = str(entry or "").strip()
    if not text:
        return []
    return [text, *re.findall(r"\b[0-9A-Fa-f]{7,64}\b", text)]


def _canonical_commits(tree: Path | None, entries: Iterable[Any]) -> list[str]:
    """Resolve each manifest revision to the object id the run's tree names.

    A manifest's ``commits:`` field is free text: a worker cites a full sha, an
    abbreviation, a branch name, or prose naming no object at all. The remedy a
    refusal prints must carry revisions the promotion accepts, so each entry is
    resolved in the run's own tree and an entry naming no commit is dropped
    rather than printed — an unresolvable citation in the remedy reproduces the
    refusal it was composed to clear. Duplicates collapse so a remedy never
    cites one commit twice.
    """
    if tree is None:
        return []
    resolved: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        for candidate in _commit_candidates(entry):
            sha = _resolve_commit(tree, candidate)
            if sha:
                if sha not in seen:
                    seen.add(sha)
                    resolved.append(sha)
                break
    return resolved


def plan_review_subject(
    project: str,
    slug: str,
    session: str,
    *,
    rubric: str = "design",
    local: bool = False,
) -> dict[str, Any]:
    """Resolve a mounted plan as the subject of the shared review composer."""
    from reckon import flight
    from reckon.crew.dispatch import resolve_project_repository

    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", slug):
        raise CrewError("plan must name one slug")
    if rubric not in ("design", "content"):
        raise CrewError("rubric must be design or content")
    docs = flight.mounted_project_docs().get(project)
    if docs is None:
        raise CrewError(f"project {project!r} has no mounted docs")
    path = docs / "plans" / f"{slug}.html"
    if not path.is_file():
        raise CrewError(f"plan {slug!r} does not exist at {path}")
    return {
        "subject": "plan",
        "project": project,
        "plan_slug": slug,
        "plan_path": str(path),
        "repo": str(resolve_project_repository(project, None)),
        "session": session,
        "rubric": rubric,
        "local": local,
        "run_id": runs.new_run_id(f"{PLAN_REVIEW_NODE_PREFIX}{slug}"),
    }


def _run_made_no_commit(record: Mapping[str, Any]) -> bool:
    """Whether a brief-carried run committed nothing past its base.

    The count lives in the run's own git history, so it answers for a run whose
    report is its only deliverable: an investigation that writes a report and
    commits no source has zero commits past its base, whatever its manifest
    says. The helper is imported locally because it lives beside the recovery
    sweep that also reads it, and a module-level edge would tie this composer to
    that surface.
    """
    from reckon.crew.recovery_watch import _commits_beyond_base

    return _commits_beyond_base(record) == 0


def _review_reads_a_report(record: Mapping[str, Any]) -> bool:
    """Whether a run's review checks a report's claims rather than a diff.

    A read-only run leaves no commit that a reviewer must read. Its role is
    ``investigate``, or its node is brief-carried — it names a stored brief and
    no plan section — and it committed nothing past its base. Neither case runs
    a gate suite, so the landed-node rubric's six dimensions and its
    added-failure count measure nothing the run did; the review re-runs the
    commands the run's report cites and reports whether each claim reproduces.
    """
    from reckon.crew.recovery_watch import _pointer_role

    if _pointer_role(record) == _INVESTIGATE_ROLE:
        return True
    # A brief-carried node names no plan section; a plan node is reviewed as a
    # diff whatever it also carried, so its brief does not make it read-only.
    node = record.get("node") or {}
    brief = str(node.get("brief_path") or node.get("brief") or "").strip()
    if not brief or str(node.get("plan") or "").strip():
        return False
    return _run_made_no_commit(record)


def _claims_review_done_when(run_id: str) -> str:
    """The done-when a read-only run's review is composed with.

    It names the claims rubric and the record it asks for, so a reviewer told
    to re-run the report's own claims emits CLAIM lines rather than the six
    SCORE lines a landed diff would earn. It names the reviewed run's directory
    so the reviewer re-runs each cited command from where the report was
    written, and it asks for no added-failure count because no suite ran.
    """
    try:
        report_directory = str(runs.run_dir(run_id))
    except (OSError, ValueError):
        report_directory = ""
    where = (
        f" from {report_directory}"
        if report_directory
        else " from the report's own directory"
    )
    return (
        f"the review for {run_id} re-runs the claims its report cites and "
        "stores a parsed record under the claims rubric: pick the three "
        "findings the report ranks highest, re-run each cited command or "
        f"script{where}, and emit one CLAIM line per finding recording "
        "reproduced, differs with both figures, or not-runnable with the "
        "reason; check the report's own summary line against its table and "
        "emit it as a CLAIM_SUMMARY line. The record carries the claim verdicts "
        "and no suite-delta fields; the turn ends once that record is stored "
        "and the manifest reads complete"
    )


def _review_dispatch_fields(
    record: Mapping[str, Any],
    *,
    delta: Mapping[str, Any] | None = None,
    write: bool = True,
) -> dict[str, Any]:
    """The facts a scoring run's review dispatch is built from.

    Composed from the run's own record so the command a reader may still retype
    and the command the reflex runs come from one source: two compositions of
    the same dispatch is how a displayed command and an executed one drift
    apart while each stays correct when read on its own.

    The dispatch grants the head-keyed record path beside the legacy path, so
    the reviewer it composes for may write the record where the head it read is
    named. Granting only the legacy path is what made the store's head key
    unusable from the reflex: the compose step chose the path, so a reviewer
    told to write there was refused the very path the store would read back.
    The head is read from the run's own tree while it is readable, and from the
    run's own record once that worktree has been reclaimed; when neither
    resolves it the legacy path is granted alone rather than a guessed key.

    The resolved head is returned beside the paths because recognition keys on
    the pair a review stands for — the run it reviews and the head it read —
    so the caller comparing a standing review against this dispatch needs the
    head the dispatch composed for, not only the paths it granted.

    ``write`` is False for a preview. A plan review's report directory, brief,
    plan snapshot and sidecar are then returned as the paths and text they
    would be written with — ``brief_text`` carries the composed brief — and
    nothing is created: a preview leaves the plan's report root unchanged and
    repeated previews mint no run directory for the delivered-review store to
    find. The paths returned are the same ones a real dispatch writes, so the
    preview still names the command and the scope that dispatch would use.
    """
    if record.get("subject") == "plan":
        from reckon import _plan_html

        path = Path(record["plan_path"])
        document_bytes = path.read_bytes()
        document = document_bytes.decode("utf-8")
        project, slug, run_id = record["project"], record["plan_slug"], record["run_id"]
        directory = plan_review.review_report_directory(project, slug, run_id)
        report = directory / "report.md"
        snapshot = directory / plan_review._REVIEW_SNAPSHOT_NAME
        # ``-w`` writes the reviewed bytes to the repository's object store, so
        # a look-back at the record's ``reviewed_blob_sha`` can read the text a
        # review was taken against even after the plan is written past it or the
        # snapshot is gone: the digest alone names no bytes without the object.
        blob = subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],
            input=document_bytes,
            capture_output=True,
            check=True,
            cwd=record["repo"],
        ).stdout.decode("ascii").strip()
        rubric = record.get("rubric", "design")
        prompt = (
            review_module.load_plan_design_review_prompt()
            if rubric == "design"
            else review_module.load_plan_review_prompt()
        )
        brief = directory / "brief.md"
        brief_text = (
            prompt + f"\nPlan path: {path}\nReview the composed snapshot: {snapshot}\n"
            f"Repository roots to search: {record['repo']}\nReport path: {report}\n"
            "Write RUBRIC and FINDING lines to the report path. Review the snapshot "
            "so the report describes the content named by its sidecar.\n"
        )
        scope = plan_review.review_scope(project, slug, plan=path, rubric=rubric)
        if scope:
            brief_text += "\n" + scope
        if rubric == "design":
            from reckon.velocity import current_week_interface_counts

            interface_week = current_week_interface_counts(record["repo"], write=write)
            figures = ", ".join(
                f"{name} {count}" for name, count in interface_week["counts"].items()
            )
            brief_text += (
                f"\nInterface budget this week: {figures} "
                f"(read {interface_week['week_end']})\n"
            )
        # The sidecar's name belongs to the store that reads it back, so the
        # composed path and the written path are one spelling rather than two.
        sidecar = directory / plan_review._REVIEW_SIDECAR_NAME
        plan_version = _plan_html.read_state(document).get("version") or 0
        # A dispatch composes its run directory here, before the lane decides,
        # so it records whether this call created the directory: an exit that
        # launches nothing removes what it created and never a directory that
        # was already on disk.
        directory_created = write and not directory.exists()
        if write:
            directory.mkdir(parents=True, exist_ok=True)
            snapshot.write_bytes(document_bytes)
            brief.write_text(brief_text, encoding="utf-8")
            plan_review.write_review_sidecar(
                directory,
                project=project,
                plan_slug=slug,
                plan_version=plan_version,
                reviewed_blob_sha=blob,
                document=document,
                rubric=rubric,
                report_path=report,
            )
        return {
            "run_id": run_id,
            "project": project,
            "head": blob,
            "plan": "",
            "section": "",
            "source_node": slug,
            "node_id": f"{PLAN_REVIEW_NODE_PREFIX}{slug}",
            "session": record["session"],
            "time_budget": "20m",
            "brief": str(brief),
            "brief_text": brief_text,
            "sidecar": str(sidecar),
            "goal": f"review the {rubric} of plan {slug}",
            "done_when": (
                f"{report} contains RUBRIC lines for every checklist item and "
                "FINDING lines carrying WOULD_CHANGE_THE_PLAN and REASON for "
                "each finding; the manifest names the delivered report"
            ),
            "write_path": str(directory),
            "write_paths": [str(directory)],
            "report_directory_created": directory_created,
        }
    node = record.get("node") or {}
    run_id = str(record.get("run_id") or "")
    project = str(record.get("project") or "")
    source_node = str(node.get("id") or run_id)
    head = _run_head_for_review(record)
    write_paths = [str(review_module.review_path(project, run_id))]
    if head:
        write_paths.append(
            str(review_module.review_path(project, run_id, reviewed_head_sha=head))
        )
    plan = str(node.get("plan") or "")
    section = str(node.get("section") or "")
    # A brief-carried run names no plan section: its authority is the stored
    # brief the reviewed worker read. Composing the review from a bare plan and
    # section leaves it with no authority at all, which admission refuses as
    # not-dispatchable, so the brief's stored copy stands in for the plan/section
    # pair and the composed review is dispatchable as printed. A run that names
    # a plan keeps the plan as its authority even when it also carried a brief,
    # so the plan gates still run.
    brief = "" if plan else str(node.get("brief_path") or node.get("brief") or "")
    return {
        "run_id": run_id,
        "project": project,
        "head": head,
        "plan": plan,
        "section": section,
        "brief": brief,
        "source_node": source_node,
        "node_id": f"{REVIEW_NODE_PREFIX}{source_node}",
        "session": str(record.get("session") or "<session>"),
        "time_budget": str(node.get("time_budget") or "20m"),
        "goal": (
            f"attach an independent review to run {run_id}"
            + (
                " covering only the commits it gained since it was reviewed: "
                + ", ".join(str(path) for path in delta.get("paths") or ())
                if delta
                else ""
            )
        ),
        # The review's whole deliverable is the record it stores, so the brief
        # states where its turn ends: once that record is stored and its own
        # manifest reads complete there is nothing left to do, and a reviewer
        # that keeps working past its own delivery only spends the lane the
        # review holds. The statement lives in the done-when beside the record
        # condition it qualifies, so a reviewer reads the two together rather
        # than inferring a stopping point from the delivery condition alone.
        # The added-failure count is named with the reviewed manifest's own
        # fields — baseline_suite and after_suite are the gate logs the record
        # is annotated from at read time — so a reviewer told to derive the
        # count finds the logs by the names its own manifest will carry.
        # The revision the review read is named by the store's canonical pair,
        # because a record that omits it cannot be told apart from one about
        # other code, and the ledger row records the pair a review stands for.
        "done_when": (
            _claims_review_done_when(run_id)
            if _review_reads_a_report(record)
            else (
                f"the review for {run_id} stores a parsed record scoring all "
                f"{len(review_module.REVIEW_DIMENSIONS)} dimensions in the range "
                f"0..{review_module.REVIEW_MAX_SCORE}, recording the revision it "
                "read as reviewed_base_sha and reviewed_head_sha, and carrying "
                "added_failure_count and added_failure_ids derived from the "
                "reviewed run's own baseline_suite and after_suite gate logs; the "
                "turn ends once that record is stored and the manifest reads complete"
            )
        ),
        "write_path": write_paths[0],
        "write_paths": write_paths,
        "scope": list(delta.get("paths") or ()) if delta else None,
        "review_tier": review_tiers.LIGHT if delta else None,
        "delta_base": str(delta.get("reviewed_head") or "") if delta else "",
    }


def _review_dispatch_argv(
    record: Mapping[str, Any], *, config: Mapping[str, Any] | None = None
) -> list[str]:
    """The review dispatch as an argument vector, ready to run or to print.

    The unreconciled-runs waiver is part of the composed command because a run
    awaiting review *is* an unreconciled run: past the grace window the fence
    refuses new dispatches for the whole project until the backlog is
    reconciled, and the reconciling action for each of those runs is exactly
    the review dispatch being refused. Without the waiver the composition is a
    command that cannot succeed on the runs it is composed for.

    A withheld selection has no lane to name, so no dispatch composes; callers
    that print an action use :func:`_review_dispatch_action`, which prints the
    hold in place of a command.
    """
    fields = _review_dispatch_fields(record)
    lane = _composed_review_lane(fields["project"], record, config)
    if lane is None:
        raise ValueError(
            "the review selection is withheld by a saturated lane, so no "
            "dispatch command composes"
        )
    return _review_dispatch_tokens(fields, lane)


def _review_dispatch_tokens(
    fields: Mapping[str, Any], lane: Sequence[str]
) -> list[str]:
    """The dispatch argv for a resolved ``lane``, ready to run or to print."""
    write_paths: list[str] = []
    for path in fields["write_paths"]:
        write_paths += ["--write-path", path]
    return [
        "reckon",
        "crew",
        "dispatch",
        "--project",
        fields["project"],
        *(
            ["--brief", fields["brief"]]
            if fields.get("brief")
            else ["--plan", fields["plan"], "--section", fields["section"]]
        ),
        "--role",
        "review",
        "--spec-level",
        "exact",
        "--node",
        fields["node_id"],
        "--goal",
        fields["goal"],
        "--done-when",
        fields["done_when"],
        *write_paths,
        "--time-budget",
        fields["time_budget"],
        "--session",
        fields["session"],
        "--allow-unreconciled-runs",
        *lane,
    ]


def _composed_review_lane(
    project: str,
    record: Mapping[str, Any],
    config: Mapping[str, Any] | None,
) -> list[str] | None:
    """The ``--local``/``--backend`` argv naming a composed review's lane.

    The lane is selected by the same ordering the reflex uses to place its own
    automations, so the printed command and the executed one choose one lane
    rather than two. Selection rather than assertion is what keeps a reader's
    retyped command off a lane the flight configuration has removed from review
    routing; the local spelling stands in only when the ordering yields no
    lane, so a pointer written before a run carried a backend keeps the spelling
    it had. A flight configuration that cannot be read leaves the ordering empty
    and the owning lane is named directly, because a composed command is a
    printed artifact and must always print.

    A withheld selection is the one case that composes no lane: a saturated
    local lane with nothing eligible ahead of it yields no candidate, and the
    executed path holds the review rather than dispatching it. None is returned
    there, because a command naming that lane would send a retyped review onto
    the lane the hold is protecting; the caller prints the hold instead.
    """
    if record.get("subject") == "plan":
        resolved = _resolved_review_config(project, config)
        local = str(resolved.get("local_backend") or "")
        if record.get("local"):
            return ["--local", "--backend", local]
        # A plan review that names no lane takes the review lanes every review
        # takes, the project's declared review backend first: excluded and
        # in-harness backends never appear, so it stays off a metered lane, and
        # a saturated local lane with nothing ahead of it holds the review.
        declared = (resolved.get("roles", {}).get("review") or {}).get("backend")
        eligible, withheld = _review_lane_plan(
            resolved, owning_backend=str(declared or "")
        )
        if withheld is not None:
            return None
        chosen = eligible[0] if eligible else local
        if chosen == local:
            return ["--local", "--backend", local]
        return ["--backend", chosen]
    owning_backend = str(record.get("backend") or "").strip()
    try:
        resolved = _resolved_review_config(project, config)
    except Exception:  # noqa: BLE001 - a printed command must not raise
        return ["--backend", owning_backend] if owning_backend else ["--local"]
    eligible, withheld = _review_lane_plan(resolved, owning_backend=owning_backend)
    if withheld is not None:
        return None
    if not eligible:
        return ["--local"]
    chosen = eligible[0]
    local = str(resolved.get("local_backend") or "").strip()
    if chosen == local:
        return ["--local"]
    return ["--backend", chosen]


def _review_dispatch_action(
    record: Mapping[str, Any], *, config: Mapping[str, Any] | None = None
) -> str:
    """Return the review dispatch that advances one scoring run.

    A withheld selection prints the hold rather than a command: the printed
    action and the executed path name one lane, and while a saturated local lane
    withholds the review the executed path names none.
    """
    fields = _review_dispatch_fields(record)
    lane = _composed_review_lane(fields["project"], record, config)
    if lane is None:
        return _review_lane_hold_action(record, config=config)
    return " ".join(shlex.quote(part) for part in _review_dispatch_tokens(fields, lane))


def _review_lane_hold_action(
    record: Mapping[str, Any], *, config: Mapping[str, Any] | None = None
) -> str:
    """The action printed for a review its lane's own figures are withholding.

    The reflex records ``awaiting-lane`` rather than dispatch when a saturated
    local lane is the only lane a review may use, and the printed action says
    the same: a command naming that lane is a command to send the review onto
    the lane the hold is protecting. The sentence names the lane and the figures
    its own document published, so a reader learns what the review waits for
    rather than reading a hold that names no cause.
    """
    run_id = str(record.get("run_id") or "")
    try:
        resolved = _resolved_review_config(str(record.get("project") or ""), config)
    except Exception:  # noqa: BLE001 - a printed action must not raise
        return f"the review for {run_id} waits for its lane to drain"
    return _no_lane_reason(
        run_id,
        resolved,
        owning_backend=str(record.get("backend") or "").strip(),
        kind="review",
        previous_lane=_failed_review_backend(record),
    )


def newest_review_for_headless_run(
    project: str, run_id: str, *, reclaimed: bool
) -> dict[str, Any] | None:
    """The stored record to read for a run whose record resolves no head.

    An empty head names no revision to select by, and the reading follows from
    how it came to be empty, so every reader of a headless record takes it from
    here rather than holding a rule of its own. ``reclaimed`` is the
    classifier's case: a record naming a worktree that is no longer on disk
    would resolve, through the shared checkout, a head its review is not about,
    so nothing is read for it and the compose path refuses in turn naming the
    missing head. Every other headless record — one naming no worktree at all,
    or one whose tree resolves no head — has no checkout fallback to borrow and
    never had one, so the store's newest record for the run is the only
    evidence there is and it is read.
    """
    if reclaimed:
        return None
    review = review_module.read_review(project, run_id)
    return ledger.normalize_identity(review) if review is not None else None


def _stored_review(record: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """Read the review of the head being classified, not just any record.

    The classifier reads the review of the head being classified, not whatever
    the store holds newest, so a run whose only review describes an earlier
    revision is not called promotable on evidence about code that no longer
    exists. The selection it uses is the one promotion uses, so the two agree
    on which stored record is evidence about a revision.

    An empty head names no revision to select by; how it came to be empty
    decides the reading, and every reader of a headless record takes it from
    :func:`newest_review_for_headless_run`.
    """
    run_id = str(record.get("run_id") or "")
    project = str(record.get("project") or "")
    if not run_id or not project:
        return None, ""
    head, tree = _review_head_and_tree(record)
    try:
        if not head:
            review = newest_review_for_headless_run(
                project, run_id, reclaimed=_worktree_reclaimed(record)
            )
        else:
            review, _stale = select_review_for_head(project, run_id, head, tree=tree)
    except (OSError, ValueError) as exc:
        return {}, str(exc)
    if review is not None and not isinstance(review, dict):
        return {}, "stored review is not a JSON object"
    return review, ""


def _review_is_complete(review: Mapping[str, Any] | None) -> bool:
    """Whether a stored review holds every measurement its rubric demands.

    A landed-node record is complete when it scores all six dimensions with
    none absent, so a reader hits the same arithmetic a reviewer read. A claims
    record is complete on its own terms: it carries the verdicts for the
    findings the review re-ran and the summary check, and is not measured
    against the six dimensions a read-only run never scored. The rubric key
    chooses the measure, so neither record is read as absent of the other's.
    """
    if not review or review.get("status") != "parsed":
        return False
    if review.get("rubric") == review_module.CLAIMS_RUBRIC:
        return _claims_review_is_complete(review)
    scores = review.get("scores")
    return (
        isinstance(scores, Mapping)
        and set(review_module.REVIEW_DIMENSIONS).issubset(scores)
        and not review.get("absent")
        and isinstance(review.get("total"), int)
        and not isinstance(review.get("total"), bool)
    )


def _claims_review_is_complete(review: Mapping[str, Any]) -> bool:
    """Whether a claims record carries every verdict the rubric asks for.

    The rubric asks for the three highest-ranked findings re-run and the
    report's own summary line checked, so the record must carry at least that
    many claims, each with a declared verdict, and a summary verdict. A claims
    record below that count names a review that re-ran too little to stand in
    for the report it reviews.
    """
    claims = review.get("claims")
    if not isinstance(claims, list) or len(claims) < review_module.CLAIMS_REQUIRED:
        return False
    if not all(
        isinstance(claim, Mapping)
        and str(claim.get("verdict") or "") in review_module.CLAIM_VERDICTS
        for claim in claims
    ):
        return False
    summary = review.get("claim_summary")
    return (
        isinstance(summary, Mapping)
        and str(summary.get("verdict") or "") in review_module.CLAIM_SUMMARY_VERDICTS
    )


from .recovery_review_dispatch import (  # noqa: E402
    _failed_review_backend,
    _no_lane_reason,
    _resolved_review_config,
    _review_head_and_tree,
    _review_lane_plan,
    _run_head_for_review,
    _worktree_reclaimed,
)
from .recovery_vocabulary import (  # noqa: E402
    PLAN_REVIEW_NODE_PREFIX,
    REVIEW_NODE_PREFIX,
)
