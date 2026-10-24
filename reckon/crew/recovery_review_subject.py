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
from reckon.crew.repair import round_node_id


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
    if (
        stored is not None
        and stored.get("rubric") == review_module.FIGURE_REVIEW_RUBRIC
    ):
        stored = None
    if stored is not None:
        return ledger.normalize_identity(stored), ""
    newest = review_module.read_review(project, run_id)
    if (
        newest is not None
        and newest.get("rubric") == review_module.FIGURE_REVIEW_RUBRIC
    ):
        newest = None
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
    """The revision a review of this run is evidence about: its tree's head.

    A worker's worktree is detached, so its ``HEAD`` file holds the revision
    itself and no subprocess is needed to read it. The files answer only when
    one of them carries a full revision; a ref kept solely in ``packed-refs``,
    an unreadable git directory, or anything else unresolved falls through to
    git, which is the only writer that can resolve an arbitrary ref. The helper
    lives in the recovery memo beside the sweep that owns the same
    git-directory resolution; it is imported here rather than at module scope
    because that memo imports this module in turn.
    """
    tree = _review_tree(record)
    if tree is None:
        return ""
    from reckon.crew.recovery_memo import _worktree_head_revision

    revision = _worktree_head_revision(tree)
    if revision:
        return revision
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


# The compiled brief names live in ``runs`` beside ``STEERING_FILE``, because
# that module cannot import this one and the queue's steering copy asks the same
# question this composition answers. Both readers use ``runs.REBUILT_BRIEF_NAMES``
# so a name is added in one place and honoured by both.

# Distinguishes a read from the composition that launches a review. The
# run-review dispatch supplies ``delta`` — the commit delta, or None when the
# run gained none — while every reader calls without it, so the fields know
# whether the brief should be written to disk. A sentinel rather than a None
# default, because the dispatch legitimately passes None and a read must not.
_DELTA_UNSET: Any = object()


def _review_brief_text(
    record: Mapping[str, Any],
    head: str,
    *,
    rubric: str | None = None,
    project_path: str | None = None,
) -> str:
    """The rubric text and run identity a run review's brief carries.

    A run review's worker reads the prompt it is handed and nothing else: the
    rubric lives in ``reckon/crew/prompts/`` where a reviewer running inside
    this repository finds it by search, but a reviewer in any other mounted
    project has no such file in its worktree, so the reuse dimension, the
    assertion-independence check and the claims grammar reach it only by
    chance. The composed brief carries the rubric verbatim — the landed-node
    rubric, or the claims rubric for a read-only run — followed by the identity
    of the run under review: its id, the head it read, and, where the reviewed
    run was dispatched with a stored brief, that brief's path, which is the
    authority its own worker read.

    ``project_path`` selects the flight layer the node's declared review checks
    are read from, so a caller holding a fixture config does not have to install
    one; without it the project's own layer is resolved as the dispatch resolves
    it.
    """
    rubric_name = str(rubric or "").strip().lower()
    if rubric_name == review_module.FIGURE_REVIEW_RUBRIC:
        rubric_text = review_module.load_figure_review_prompt()
    elif _review_reads_a_report(record):
        rubric_text = review_module.load_claims_review_prompt()
    else:
        rubric_text = review_module.load_review_prompt()
    run_id = str(record.get("run_id") or "")
    node = record.get("node") or {}
    brief_path = str(node.get("brief_path") or node.get("brief") or "").strip()
    lines = [
        f"Reviewed run: {run_id}",
        f"Reviewed head: {head or '<unresolved>'}",
    ]
    if brief_path:
        lines.append(f"Reviewed run's brief: {brief_path}")
    # A figure record carries no text scores; its findings declare their own
    # severity.
    if rubric_name == review_module.FIGURE_REVIEW_RUBRIC:
        required_fields = [
            "Required: project, reviewed_run_id, review_run_id, reviewed_base_sha, reviewed_head_sha,",
            "added_failure_count, added_failure_ids (a figure review runs no suite, so 0 and an empty list),",
            "findings (each an object with file, line, severity and summary), status.",
            "Judge each figure only from the rendered PNGs named in this review: open, read, list,",
            "search, render or `git show` no SVG and no image other than the named PNGs; anchor a",
            "finding to the figure's repository path, or to a line of the script the run's diff shows",
            "drawing it, never to a line of SVG markup.",
        ]
    else:
        required_fields = [
            "Required: project, reviewed_run_id, review_run_id, reviewed_base_sha, reviewed_head_sha,",
            "scores (" + ", ".join(review_module.REVIEW_DIMENSIONS) + "),",
            "added_failure_count, added_failure_ids, findings, status.",
        ]
    lines.extend(
        [
            "",
            "Review record contract (run crew check --review --record <path> on each written record before ending):",
            *required_fields,
            "Accepted status values: "
            + ", ".join(sorted(review_module.RECOGNISED_REVIEW_STATUSES))
            + ".",
            "Use parsed when the rubric produced a readable review; unparsed when it did not;",
            "incomplete when the review has not finished and no verdict can be acted on.",
        ]
    )
    declared_block = _declared_check_block(
        str(record.get("project") or ""), node, project_path=project_path
    )
    if declared_block:
        lines.extend(["", declared_block])
    if rubric_name:
        lines.insert(0, f"Review rubric: {rubric_name}")
    text = rubric_text.rstrip("\n") + "\n\n" + "\n".join(lines) + "\n"
    from reckon.crew.promotion_scope import _is_shadow

    if _is_shadow(record):
        text += _shadow_review_block(record)
    return text


# The heading the review checks a repository declared for a node are carried
# under in a composed review brief. One heading for one block, so a reader
# handed two briefs sees the same marker whether or not their nodes matched.
DECLARED_CHECK_HEADING = "Declared review checks this node touches"


def domain_review_checks(
    project: str,
    node: Any,
    *,
    project_path: str | None = None,
    host_path: str | None = None,
    shipped_path: str | None = None,
    checkout_path: str | None = None,
) -> tuple[Any, ...]:
    """The declared review checks a node must answer, in declaration order.

    The match is taken from the one module that answers which rules a node
    touches, so the checklist a review brief carries, the verdicts the parser
    requires and the rules the node's own brief named cannot disagree. A project
    that declares no ``domain`` block, and a node matching none of its globs,
    both yield nothing.
    """
    from reckon.crew.domain_rules import rules_for

    if not project:
        return ()
    return tuple(
        rules_for(
            project,
            node,
            project_path=project_path,
            host_path=host_path,
            shipped_path=shipped_path,
            checkout_path=checkout_path,
        ).review_checks
    )


def _declared_check_block(
    project: str,
    node: Any,
    *,
    project_path: str | None = None,
    host_path: str | None = None,
    shipped_path: str | None = None,
    checkout_path: str | None = None,
) -> str:
    """Render the review checks a node must answer, or the empty string.

    A reviewer reading a composed brief has no flight config in its own worktree
    when the project it reviews is not this repository, so the questions and the
    verdict form reach it only if the brief carries them. The block states the
    line grammar beside the questions rather than in the rubric, because the
    rubric is shared by every project and these checks are declared by one.
    """
    checks = domain_review_checks(
        project,
        node,
        project_path=project_path,
        host_path=host_path,
        shipped_path=shipped_path,
        checkout_path=checkout_path,
    )
    if not checks:
        return ""
    outcomes = "|".join(review_module.CHECK_VERDICT_OUTCOMES)
    lines = [
        DECLARED_CHECK_HEADING + ":",
        f"  VERDICT <check id>: {outcomes} — <what you read>",
    ]
    lines.extend(f"  {check.id}: {check.question}" for check in checks)
    lines.append(
        "A check answered "
        f"{review_module.FAILED_CHECK_OUTCOME!r} is recorded as a blocking "
        "finding, and a check with no answered verdict leaves the record "
        "incomplete."
    )
    return "\n".join(lines)


def _shadow_review_block(record: Mapping[str, Any]) -> str:
    """The paragraph a shadow's review brief carries in place of a commit diff.

    A shadow is dispatched to produce evidence without committing, and the
    patch its worktree retains is its real deliverable — the same patch
    promotion preserves. A brief that describes the run only as committed work
    sends its reviewer to an empty commit diff and a head equal to the base, so
    this block names the patch commands the reviewer must read instead, states
    that an empty commit list is the contract rather than a finding, and binds
    every dimension to that patch. The two commands are composed from the
    record's own worktree and base, so they name the same patch promotion
    retains rather than a path the reviewer would have to infer.
    """
    worktree = shlex.quote(str(record.get("worktree") or ""))
    base = shlex.quote(str(record.get("base_sha") or ""))
    return (
        "\nThis run is a shadow, and its deliverable is its retained worktree "
        "patch, never its commits.\n"
        f"The diff to review is `git -C {worktree} diff {base}` together with "
        f"the new files named by `git -C {worktree} ls-files --others "
        "--exclude-standard`, which is exactly the patch promotion preserves "
        "for this run.\n"
        "An empty commits list and a head equal to the base are this run's "
        "contract, not a finding.\n"
        "Score every dimension on that patch as the reviewed change.\n"
    )


def _review_brief_path(record: Mapping[str, Any], *, rubric: str | None = None) -> Path:
    """Where a run review's composed brief is written.

    Durable rather than a scratch copy so the two places a review is composed
    from — the reflex dispatch and the ``review-missing`` obligation's next
    command — name one path holding one text: a reader who retypes the printed
    command opens the same brief the reflex would have carried. The path is
    keyed on the reviewed run, so the two consumers of one run's review agree
    without sharing a value between them.
    """
    name = (
        runs.FIGURE_REVIEW_BRIEF_NAME
        if rubric == review_module.FIGURE_REVIEW_RUBRIC
        else runs.RUN_REVIEW_BRIEF_NAME
    )
    return runs.run_dir(str(record.get("run_id") or "")) / name


def materialize_run_review_brief(source: str | Path) -> bool:
    """Compose a run review's brief when a dispatch names its composed path.

    A run review's brief is derived from the reviewed run's record rather than
    authored by the caller: it carries the rubric the review's done-when assumes
    plus the run's identity and the head its tree carries now. The readers that
    print the command — the ``review-missing`` obligation, the classifier's
    ``next_action``, dispatch's own unreconciled-runs refusal — compose that
    path and text without writing them, so a command retyped verbatim would
    refuse a brief no reflex ever composed, or digest one a previous dispatch
    left naming a head the run has since moved past.

    Dispatch names exactly the reviewed run's own ``review-brief.md``, so it
    composes the text here from the record, before it digests or stores it, and
    the head it records is the one the run's tree carries now. Any other path —
    a brief a coordinator authored, a plan review's own brief, a review run's
    stored copy of a brief — is left untouched, so a coordinator's brief stays
    the authority it was and a resume that reads a stored copy re-digests the
    bytes the worker read.

    Returns True when the path is a run review's composed brief and was written.
    A path that is not one, or one naming a run whose record cannot be read,
    returns False so the caller's own refusal for an unreadable brief still
    speaks.
    """
    path = Path(source).expanduser()
    if path.name not in runs.REBUILT_BRIEF_NAMES:
        return False
    run_id = path.parent.name
    if not run_id:
        return False
    try:
        if runs.run_dir(run_id).resolve() != path.parent.resolve():
            return False
    except OSError:
        return False
    try:
        record = runs.read_pointer(run_id)
    except CrewError:
        return False
    if not str(record.get("run_id") or ""):
        return False
    rubric = (
        review_module.FIGURE_REVIEW_RUBRIC
        if path.name == runs.FIGURE_REVIEW_BRIEF_NAME
        else None
    )
    _review_dispatch_fields(record, delta=None, rubric=rubric)
    return True


def _review_dispatch_fields(
    record: Mapping[str, Any],
    *,
    delta: Any = _DELTA_UNSET,
    write: bool = True,
    rubric: str | None = None,
    images: Sequence[str] = (),
) -> dict[str, Any]:
    """The facts a scoring run's review dispatch is built from.

    Composed from the run's own record so the command a reader may still retype
    and the command the reflex runs come from one source: two compositions of
    the same dispatch is how a displayed command and an executed one drift
    apart while each stays correct when read on its own.

    A run review's brief is materialized only when the fields are composed for
    the dispatch that launches the review — ``delta`` is supplied — and not on a
    read. The run-review dispatch passes ``delta`` (the commit delta when the
    run gained commits since it was reviewed, and ``None`` when it did not, both
    supplied); the obligation's next command, the in-flight check and every
    other reader call without it. A read composes the same path and text but
    writes nothing, so it never brings a run's directory into being and never
    draws a classification memo into a home the run never had.

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
    nothing is created: a preview leaves the plan report root unchanged and
    repeated previews mint no run directory for the delivered-review store to
    find. The paths returned are the same ones a real dispatch writes, so the
    preview still names the command and the scope that dispatch would use.
    """
    composing_for_dispatch = delta is not _DELTA_UNSET
    if not composing_for_dispatch:
        delta = None
    rubric_name = str(rubric or "").strip().lower()
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
        blob = (
            subprocess.run(
                ["git", "hash-object", "-w", "--stdin"],
                input=document_bytes,
                capture_output=True,
                check=True,
                cwd=record["repo"],
            )
            .stdout.decode("ascii")
            .strip()
        )
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
    # A paired dispatch launches two arms under one node id, so a review node id
    # minted from that node id resolves to whichever arm the fleet listing
    # returns first — the wrong one as easily as the right. The run id is the
    # arm's own identity, so a paired arm's review is minted from it, while
    # ``source_node`` stays the node id every other reader composes from. The
    # pairing test is the one promotion already resolves a partner through, so
    # both arms read it the same way and it is not written a second time here.
    from reckon.crew.promotion_checks import _paired_partner_run_id

    review_node_source = run_id if _paired_partner_run_id(record) else source_node
    head = _run_head_for_review(record)
    # Declared inside a directory of the reviewed run's own rather than at
    # the store path the record is finally read from. The store holds every run
    # of the project as a flat file in one directory, and a store path that does
    # not exist yet makes the fence grant that directory — and with it every
    # other run's record. The record directory holds review records and nothing
    # of another run's, so a review's fence reaches no record it was not asked
    # about; the delivered record is filed into the store when the run ends,
    # which is where every store reader looks for it. The directory is brought
    # into being here for the same reason the brief is written here — the fence
    # binds a directory that exists, and a missing one would be granted through
    # the whole run directory above it — and only for the dispatch that
    # launches the review, never on a read.
    record_directory = review_module.run_review_record_directory(runs.run_dir(run_id))
    if write and composing_for_dispatch:
        record_directory.mkdir(parents=True, exist_ok=True)
    write_paths = review_module.run_review_record_paths(
        runs.run_dir(run_id),
        run_id,
        reviewed_head_sha=head,
        rubric=rubric_name,
    )
    plan = str(node.get("plan") or "")
    section = str(node.get("section") or "")
    # A composed review brief cannot ride beside a plan that names no section:
    # dispatch reads a brief beside a plan as the coordinator's instructions for
    # a section and refuses the pair when the section is missing, so naming the
    # plan alone would compose a review command dispatch rejects for a reviewed
    # run that carried a plan with no section. The brief carries the rubric and
    # the reviewed run's identity, so it stays and the plan/section pair is
    # emitted only when a section names where the brief belongs.
    if plan and not section:
        plan = ""
    # A run review's brief is composed, not given: the rubric text followed by
    # the identity of the run under review. The reviewed run's stored brief,
    # when it carried one, is named inside that text rather than handed to the
    # review as its own authority — a run that names a plan keeps the plan as
    # its authority, and the composed brief rides beside the plan/section pair
    # as coordinator instructions, so every plan gate still runs. Writing the
    # brief here, before the lane decides, is what lets the printed
    # ``review-missing`` command and the executed dispatch name one path.
    brief_text = _review_brief_text(record, head, rubric=rubric_name)
    brief_file = _review_brief_path(record, rubric=rubric_name)
    # The brief is materialized only for the dispatch, never on a read: a read
    # that composed it wrote review-brief.md into the reviewed run's directory,
    # creating a directory behind a pointer that never had one and drawing the
    # classification memo in after it. The dispatch passes ``delta``; every
    # reader does not.
    if write and composing_for_dispatch:
        brief_file.parent.mkdir(parents=True, exist_ok=True)
        brief_file.write_text(brief_text, encoding="utf-8")
    base_node_id = round_node_id(
        REVIEW_NODE_PREFIX,
        review_node_source,
        head,
        rubric=(
            review_module.FIGURE_REVIEW_RUBRIC
            if rubric_name == review_module.FIGURE_REVIEW_RUBRIC
            else ""
        ),
    )
    figure_review = rubric_name == review_module.FIGURE_REVIEW_RUBRIC
    return {
        "run_id": run_id,
        "project": project,
        "head": head,
        "rubric": rubric_name,
        "images": list(images) if figure_review else [],
        "plan": plan,
        "section": section,
        "brief": str(brief_file),
        "brief_text": brief_text,
        "source_node": source_node,
        "node_id": base_node_id,
        "session": str(record.get("session") or "<session>"),
        "time_budget": str(node.get("time_budget") or "20m"),
        "goal": (
            f"attach an independent figure review to run {run_id}"
            if figure_review
            else f"attach an independent review to run {run_id}"
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
            f"the figure review for {run_id} stores a parsed read-only rubric "
            f"record with all {len(review_module.FIGURE_REVIEW_ITEMS)} rubric "
            "items at a declared write path, anchored to the reviewed base and head"
            if figure_review
            else _claims_review_done_when(run_id)
            if _review_reads_a_report(record)
            else (
                f"the review for {run_id} stores a parsed record scoring all "
                f"{len(review_module.REVIEW_DIMENSIONS)} dimensions in the range "
                f"0..{review_module.REVIEW_MAX_SCORE}, recording the revision it "
                "read as reviewed_base_sha and reviewed_head_sha, and carrying "
                "added_failure_count and added_failure_ids derived from the "
                "reviewed run's own baseline_suite and after_suite gate logs; the "
                "record is written at one of this run's declared write paths, "
                f"under {review_module.RUN_REVIEW_RECORD_DIRNAME}/ inside the "
                f"directory of run {run_id}, and never in the worktree; the "
                "turn ends once that record is stored and the manifest reads "
                "complete"
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
    """The dispatch argv for a resolved ``lane``, ready to run or to print.

    One builder serves every review subject, so a plan review, a run review and
    a code review cannot drift into three spellings of the same command. The
    plan/section pair is emitted for a plan subject alone: a code review's
    subject is a neighbourhood of modules, which no plan names, so it carries
    the composed brief and its report directory and no plan.
    """
    write_paths: list[str] = []
    for path in fields["write_paths"]:
        write_paths += ["--write-path", path]
    # The composed brief and the plan/section pair travel together when a run
    # names a plan: the brief carries the reviewed worker's rubric and identity,
    # and the plan stays the semantic authority so dispatch's plan gates still
    # run. Dispatch accepts a brief beside a plan section, carrying it verbatim.
    tokens = [
        "reckon",
        "crew",
        "dispatch",
        "--project",
        fields["project"],
    ]
    if str(fields.get("brief") or ""):
        tokens += ["--brief", str(fields["brief"])]
    if str(fields.get("subject") or "") != "code" and str(fields.get("plan") or ""):
        tokens += ["--plan", str(fields["plan"]), "--section", str(fields["section"])]
    tokens += [
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
    return tokens


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

    The completeness rule has one owner, :func:`_review_missing_measures`, and
    this is its negation, so a reader that asks only whether a record is
    complete and a reader that asks which measure it lacks read the same list.
    """
    return not _review_missing_measures(review)


def _review_missing_measures(
    review: Mapping[str, Any] | None,
) -> list[str]:
    """The measures a stored review lacks, in the order a reader checks them.

    A landed-node record is complete when it scores all six dimensions with
    none absent and carries the total they sum to, so a reader hits the same
    arithmetic a reviewer read. A claims record is complete on its own terms:
    it carries the verdicts for the findings the review re-ran and the summary
    check, and is not measured against the six dimensions a read-only run never
    scored. The rubric key chooses the measure, so neither record is read as
    absent of the other's. An empty list is a complete record; the names are
    what a refusal reports, so a review the fleet cannot promote says which
    measure it is missing rather than that it cannot be read.
    """
    if not review or review.get("status") != "parsed":
        return ["status"]
    if review.get("rubric") == review_module.CLAIMS_RUBRIC:
        return _claims_missing_measures(review)
    return _landed_missing_measures(review)


def _landed_missing_measures(review: Mapping[str, Any]) -> list[str]:
    """The dimensions and total a landed record does not carry.

    An ``absent`` value that is not a list cannot name the dimensions it
    excludes, so it is reported as a single unnamed missing measure rather than
    iterated. A record read by this function reaches it from four
    classification paths, so a malformed value must still read as incomplete
    rather than raise and take the pass down with it.
    """
    missing: list[str] = []
    scores = review.get("scores")
    if not isinstance(scores, Mapping):
        missing.extend(review_module.REVIEW_DIMENSIONS)
    else:
        missing.extend(
            dimension
            for dimension in review_module.REVIEW_DIMENSIONS
            if dimension not in scores
        )
    absent = review.get("absent")
    if absent:
        if isinstance(absent, (list, tuple)):
            missing.extend(str(dimension) for dimension in absent)
        else:
            missing.append("absent dimensions")
    total = review.get("total")
    if not isinstance(total, int) or isinstance(total, bool):
        missing.append("total")
    return missing


def _claims_missing_measures(review: Mapping[str, Any]) -> list[str]:
    """The claim verdicts a claims record does not carry."""
    claims = review.get("claims")
    if not isinstance(claims, list) or len(claims) < review_module.CLAIMS_REQUIRED:
        return ["claims"]
    if not all(
        isinstance(claim, Mapping)
        and str(claim.get("verdict") or "") in review_module.CLAIM_VERDICTS
        for claim in claims
    ):
        return ["claim verdicts"]
    summary = review.get("claim_summary")
    if not (
        isinstance(summary, Mapping)
        and str(summary.get("verdict") or "") in review_module.CLAIM_SUMMARY_VERDICTS
    ):
        return ["claim_summary"]
    return []


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
