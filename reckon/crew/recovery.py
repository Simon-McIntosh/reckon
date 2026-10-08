from __future__ import annotations

import contextlib
import fcntl
import hashlib
import importlib
import json
import math
import os
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timezone
from functools import lru_cache
from pathlib import Path
from statistics import median
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from reckon import ledger, review_tiers
from reckon._timestamps import parse_utc
from reckon.capabilities import _charged_input_from_usage
from reckon.crew import lane_document as _lane_document
from reckon.crew import metering, plan_review, quota_weight, runs
from reckon.crew import repair as repair_module
from reckon.crew import review as review_module
from reckon.crew import review_need
from reckon.crew.host_lease import LEASE_RENEW_SECONDS
from reckon.crew.node import (
    _TERMINAL_RUN_PHASES,
    DEFAULT_WATCH_STALL_WINDOW,
    INTERRUPTED_RUN_PHASE,
    LOG_STALE_AFTER_SECONDS,
    CrewError,
    parse_duration,
)
from reckon.crew.reports import (
    NON_TERMINAL_MANIFEST_STATUSES,
    TERMINAL_MANIFEST_STATUSES,
    ManifestParseError,
    manifest_status_is_template,
    parse_manifest,
)
from reckon.crew.routing import _signal_process_group
from reckon.crew.runs import (
    _manifest_freshness,
    _mutate_pointer,
    _process_start_time,
    _project_watch_claim,
    _read_watch_record,
    _stream_quiet_seconds,
    _utc_now,
    _write_watch_record,
    list_live,
    producer_lease_seconds,
    read_pointer,
    update_watch_registration,
    watch_lease_renewed_at,
    watch_lock_path,
)
from reckon.crew.ticker import NEEDS_ACTION, Ticker, _agent_label

# The classifier reaches liveness through the module at the point of call, so
# replacing the definition on its owning module replaces what classification
# consults. This import-time snapshot of the same function stays on this module
# for callers that patch this namespace; classification itself reads the live
# module attribute.
process_alive = runs.process_alive


# ── Recovery: what an interrupted orchestrator left behind ───────────────────

# What a live pointer can be once nobody is watching it. Worker-reported
# blocked and failed outcomes remain distinct so neither can be mistaken for a
# completed delivery that is eligible for promotion. An unreadable manifest is
# its own outcome: a file exists but no reader can judge it, which is neither a
# delivered record (completed_unpromoted) nor an absence (abandoned). A worker
# whose recorded exit ended the turn with work committed while its manifest
# still reads a working status has delivered something no status word claims, so
# it is its own outcome too: the record needs the verdict word the worker never
# wrote, and the committed work is safe in the tree. Paused is
# the wait that lifts itself: the run is waiting on time or on its own job, and
# nobody has to act, because whoever or whatever lifts the run is not a person.
# The discriminator is exactly that — who lifts it. A stop that needs a person
# or another session stays blocked; a stop whose own job, a window reset or a
# bounded wait ends it is paused. Blocked is alarming because it demands a
# reader; paused must therefore always name what will lift it, so it never
# becomes the bucket a forgotten run sits in.
RECOVERY_CLASSES = (
    "running",
    "waiting",
    "paused",
    "stopped",
    "completed_unpromoted",
    "blocked",
    "failed",
    "unreadable",
    "exited-unfinished",
    "abandoned",
    "lane-event",
)

# A burst is anchored to its first end, so a chain of nearby endings cannot
# silently join events whose first and last runs ended minutes apart.
LANE_EVENT_WINDOW_SECONDS = 30

WAITING_STATUS = "waiting"
# The waiting family is the stop that lifts itself, an overdue wait included:
# a run whose declared external wait has aged past its expectation has not
# failed and nothing about it lifts by inspection, so it is still waiting — the
# fleet counts it here, never in the blocked tally. Its age is the news, and
# the news is carried by the action marker on its row, so the wait-aged state
# also sits in the action set while remaining a member of this family.
WAITING_STATES = frozenset({"waiting", "wait-aged", "paused", "queued"})
# The manifest status vocabulary — TERMINAL_MANIFEST_STATUSES,
# NON_TERMINAL_MANIFEST_STATUSES and manifest_status_is_template — is imported
# from reckon.crew.reports, which owns the single statement of it so the reader
# refusing an unrecognised word names the same set the classifier decides
# against.
WAIT_CONDITION_STATES = frozenset({"pending", "met", "unknown"})
WAIT_PROBE_TIMEOUT_SECONDS = 1.0

# A dispatch writes no manifest of its own, so between launch and the worker's
# first write a live run has no verdict to read. Reporting that gap as unwritten
# the instant it opens makes a working run flicker, so a manifest that carries
# no status is not called unwritten inside this window after the run's dispatch;
# the run keeps whatever its liveness already said. Past the window the word is
# the truthful one: a live run still without a written verdict is unwritten.
LAUNCH_WINDOW_SECONDS = 120
# Workers write manifests by hand rather than atomically, so a reader can catch
# one mid-rewrite: an unparseable, just-modified or shrunk file is not the
# worker's verdict but its absence in transit, and is treated as unchanged. The
# window is short because it only has to cover a single rewrite, not a stall.
MANIFEST_REWRITE_WINDOW_SECONDS = 10
# The size each readable manifest last had, with the moment it was read, keyed by
# the run and attempt that owns the path. A file that shrank since that read is
# the signature of a truncating rewrite caught between the truncate and the
# write, so the previous reading stands — earlier than a normal rewrite, before
# its mtime has moved — and it expires with the same short window so it can
# never hold the unwritten reading back forever.
_MANIFEST_SIZES_READ: dict[str, tuple[int, float]] = {}
# A reader classifies every run it is shown, over days, so the size memory is
# capped and drops its least recently touched entry.
MANIFEST_SIZE_MEMORY_MAX = 256

# This is the authoritative answer to "what should the coordinator do now?".
# The older classification remains a lifecycle grouping used by recovery and
# promotion, while this vocabulary names the cause whose remedy differs. A
# lane hold and a worker failure therefore cannot share an instruction even
# though both remain attention-worthy terminal-looking rows.
RECOVERY_VERBS = {
    "running": "observe",
    "queued": "wait",
    "waiting": "wait",
    "paused": "wait",
    "completed_unpromoted": "promote",
    "held": "resume",
    "needs-help": "answer",
    "failed": "redispatch",
    "stalled": "investigate",
    "blocked": "decide",
    "stopped": "inspect",
    "scoring": "review",
    "promotable": "promote",
    "unreadable": "repair",
    "exited-unfinished": "repair",
    "unwritten": "resume",
    "ready": "resume",
    "abandoned": "recover",
    "lane-event": "inspect",
    "refused-at-admission": "resume",
    "launch-failed": "resume",
    "ended-without-manifest": "resume",
    "wait-aged": "investigate",
    INTERRUPTED_RUN_PHASE: "redispatch",
}
RECOVERY_CLASSIFICATIONS = tuple(RECOVERY_VERBS)
ACTIONABLE_RECOVERY_CLASSIFICATIONS = frozenset(
    {
        "held",
        "needs-help",
        "failed",
        "stalled",
        "blocked",
        "stopped",
        "scoring",
        "unreadable",
        "exited-unfinished",
        "unwritten",
        "ready",
        "abandoned",
        "lane-event",
        "refused-at-admission",
        "wait-aged",
        INTERRUPTED_RUN_PHASE,
        # A launch that never reached a model wants the coordinator to repair a
        # command or a PATH, which is work only a person can do; leaving it out
        # of the actionable count is how such a run reads as invisible while it
        # occupies a lane.
        "launch-failed",
    }
)

# Classifications whose remedy decides whether a run's resume path survives.
# Each one reads the resolved session — pointer, then stream, then the promoted
# ledger row — rather than the pointer's own session_id field, which lags it:
# a stopped or abandoned pointer whose stream still names a session resumes
# with every turn intact, so an advice to discard or redispatch it would throw
# away a session that is still there. The blocked and interrupted arms have
# read the resolution since the escape hatch was built; the two disposal arms
# are what this set adds.
RESUMPTION_READING_CLASSIFICATIONS = frozenset(
    {"blocked", INTERRUPTED_RUN_PHASE, "stopped", "abandoned"}
)


REVIEW_NODE_PREFIX = "review-of-"
PLAN_REVIEW_NODE_PREFIX = "plan-review-of-"

# The dispatch role that produces reviews. A run carrying it is the reviewer,
# never the reviewed, so no classification may compose a review of it: the
# composed dispatch names its own source run, so reviewing a review spawns
# another review of the same shape without limit. The promotion boundary keys
# its own exemption on the same fact — a review run is never gated on a review
# of itself.
REVIEW_ROLE = "review"


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
            f"the review for {run_id} stores a parsed record scoring all "
            f"{len(review_module.REVIEW_DIMENSIONS)} dimensions in the range "
            f"0..{review_module.REVIEW_MAX_SCORE}, recording the revision it read as "
            "reviewed_base_sha and reviewed_head_sha, and carrying "
            "added_failure_count and added_failure_ids derived from the reviewed "
            "run's own baseline_suite and after_suite gate logs; the turn ends "
            "once that record is stored and the manifest reads complete"
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
    """Whether a stored review contains every independently scored dimension."""
    if not review or review.get("status") != "parsed":
        return False
    scores = review.get("scores")
    return (
        isinstance(scores, Mapping)
        and set(review_module.REVIEW_DIMENSIONS).issubset(scores)
        and not review.get("absent")
        and isinstance(review.get("total"), int)
        and not isinstance(review.get("total"), bool)
    )


# ── A delivered review ends its turn ────────────────────────────────────────
# A review's entire deliverable is the record it stores for the run it reviews,
# so once that record parses and the reviewer's own manifest reads complete
# there is nothing left to produce. Measured 2026-09-23: two reviews stored
# their records and complete manifests, then kept streaming for another 56 and
# 40 minutes — not stuck, but inventing further verification after the
# deliverable existed and spending a lane the fleet needed, at a moment the
# lane read 180% of its computed budget. The grace below is measured from the
# later of the two writes that make a review delivered, so a reviewer that
# stored its record and is still finishing its own turn is left alone until its
# stream has genuinely outlived the delivery rather than the watcher's notice.
DELIVERED_REVIEW_GRACE_SECONDS = 300.0


def _is_review_run(record: Mapping[str, Any]) -> bool:
    """Whether a run is a reviewer, by its role or by its minted node id."""
    return _is_review_node(record) or _pointer_role(record) == REVIEW_ROLE


def _review_store_record_paths(record: Mapping[str, Any], project: str) -> list[Path]:
    """The review-store paths a reviewer was told to write, in order.

    The dispatch composes them from the reviewed run, so they name the record
    this run is meant to deliver. A path is selected by where it sits — under
    the review store's own project directory — rather than by its filename,
    because the head-keyed and legacy spellings differ only in a suffix the
    reader must not have to guess.
    """
    node = record.get("node") or {}
    directory = review_module.review_store_root() / project
    paths: list[Path] = []
    for declared in node.get("write_paths") or ():
        candidate = Path(str(declared))
        if candidate.parent == directory:
            paths.append(candidate)
    return paths


def _resolved_reviewed_run_id(record: Mapping[str, Any], project: str) -> str:
    """The run a reviewer reviews, resolved from its minted node id, or empty.

    The review node id carries what its dispatch reviewed — the source run's id
    or, where it had one, its node id — so the store can be read even when the
    pointer predates the write paths that name the record directly. A source
    that is not a run id is matched against the live fleet's node ids, which is
    the only place the reviewed run's id is recoverable once its own node id
    was resolved to a run id at dispatch.
    """
    node_id = str((record.get("node") or {}).get("id") or "")
    if not node_id.startswith(REVIEW_NODE_PREFIX):
        return ""
    source = node_id[len(REVIEW_NODE_PREFIX) :]
    if not source:
        return ""
    if source.startswith("r-"):
        return source
    for pointer in list_live(project=project):
        if str((pointer.get("node") or {}).get("id") or "") == source:
            return str(pointer.get("run_id") or "")
    return source


def _complete_manifest_write_time(path: Path) -> float | None:
    """When a run's manifest last read complete, or None when it does not.

    Read directly rather than through the classifier: the classifier defers a
    live process's terminal report, and the whole point of this reader is a
    review whose process is still alive while its manifest has already reached
    its verdict. A status still carrying the dispatch template is not a verdict,
    so a placeholder never reads as a delivery.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = parse_manifest(text)
    except ManifestParseError:
        return None
    status = str(data.get("status") or "").strip().lower()
    if manifest_status_is_template(status) or status != "complete":
        return None
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _read_complete_review(path: Path) -> tuple[float, Path] | None:
    """A stored review at ``path`` and when it landed, when it parses complete."""
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(stored, Mapping) or not _review_is_complete(stored):
        return None
    try:
        return path.stat().st_mtime, path
    except OSError:
        return None


def _delivered_review_record(
    record: Mapping[str, Any], project: str
) -> tuple[float, Path] | None:
    """The stored review record a reviewer wrote and when it landed, or None.

    The record the dispatch told this run to write is read first; the store
    lookup by the reviewed run is the fallback for a pointer that carried no
    write paths. Both judge a parse with the predicate promotion uses, so a
    watcher and a coordinator cannot disagree about whether a review counts as
    delivered.
    """
    for path in _review_store_record_paths(record, project):
        stored = _read_complete_review(path)
        if stored is not None:
            return stored
    reviewed = _resolved_reviewed_run_id(record, project)
    if not reviewed:
        return None
    candidates = [review_module.review_path(project, reviewed)]
    directory = review_module.review_store_root() / project
    if directory.is_dir():
        candidates.extend(sorted(directory.glob(f"{reviewed}.at-*.json")))
    for path in candidates:
        stored = _read_complete_review(path)
        if stored is not None:
            return stored
    return None


def review_delivered(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """The facts of a review run's delivery, or None while it has not delivered.

    A review delivers when the record it was minted to store parses and its own
    manifest reads complete; both are read from this run's own store path and
    manifest, never from a classifier that defers a live process's verdict. The
    returned mapping names both paths and both write times, so a caller
    measuring a grace reads the later of the two rather than guessing which
    write finished last. Public because more than the watcher asks whether a
    review is done: the fact is a property of the run, and any reader with the
    record can ask it.
    """
    if not _is_review_run(record):
        return None
    project = str(record.get("project") or "")
    manifest_path = Path(str(record.get("manifest_path") or ""))
    manifest_time = _complete_manifest_write_time(manifest_path)
    if manifest_time is None:
        return None
    stored = _delivered_review_record(record, project)
    if stored is None:
        return None
    record_time, record_path = stored
    return {
        "record_path": str(record_path),
        "manifest_path": str(manifest_path),
        "record_mtime": record_time,
        "manifest_mtime": manifest_time,
        "delivered_at": max(record_time, manifest_time),
    }


def _stop_delivered_reviews(
    pointers: Sequence[Mapping[str, Any]],
    *,
    grace_seconds: float = DELIVERED_REVIEW_GRACE_SECONDS,
    signal_run: Callable[..., None] | None = None,
) -> list[dict[str, Any]]:
    """Stop each review whose stream has outlived its delivery by the grace.

    A review that delivered and kept streaming is not still working: its
    deliverable is stored and its manifest is a verdict, so every further turn
    spends the lane the review holds and nothing else. The stop is signalled to
    the run's own recorded pid and nothing wider, because the watcher shares the
    login node with every peer session and a group signal reaches work this rule
    was never given. A stop that cannot be delivered is left for the next tick
    rather than recorded as one, because a record claiming a stopped process
    that is still running is worse than no record at all.
    """
    stopped: list[dict[str, Any]] = []
    for pointer in pointers:
        delivered = review_delivered(pointer)
        if not delivered:
            continue
        if str(pointer.get("phase") or "") in _TERMINAL_RUN_PHASES:
            continue
        stream_mtime = _run_stream_mtime(pointer)
        if stream_mtime is None:
            continue
        if stream_mtime - delivered["delivered_at"] <= grace_seconds:
            continue
        pid = pointer.get("pid")
        try:
            if signal_run is None:
                _signal_process_group(
                    int(pid),
                    pointer.get("pid_start_time"),
                    run_dir=_run_directory(pointer),
                    reason="delivered-review-outlived-grace",
                )
            else:
                signal_run(int(pid), pointer.get("pid_start_time"))
        except (
            CrewError,
            ProcessLookupError,
            PermissionError,
            OSError,
            TypeError,
            ValueError,
        ):
            continue
        run_id = str(pointer.get("run_id") or "")
        ended_at = _utc_now()
        detail = {
            "stopped_at": ended_at,
            "grace_seconds": grace_seconds,
            "record_path": delivered["record_path"],
            "manifest_path": delivered["manifest_path"],
        }

        def record(
            existing: dict[str, Any],
            *,
            _detail: Mapping[str, Any] = detail,
            _ended_at: str = ended_at,
        ) -> dict[str, Any]:
            existing["phase"] = "stopped"
            existing["stopped_at"] = _ended_at
            existing["ended_after_delivery"] = dict(_detail)
            return existing

        written = _mutate_pointer(run_id, record) if run_id else None
        if isinstance(pointer, dict) and written is not None:
            pointer.update(written)
        stopped.append(
            {
                "run_id": run_id,
                "pid": int(pid),
                "stopped_at": ended_at,
                "ended_after_delivery": dict(detail),
            }
        )
    return stopped


# ── The review reflex: a scoring run runs the command it composed ───────────
# A run that reaches scoring has already had its whole review dispatch composed
# and returned as a string, and leaving it there is what left six runs waiting
# on one workstation at one moment and nine unreconciled across a working day.
# The reflex below runs that command instead of printing it. It is deliberately
# built on the same dispatch a coordinator would type, so every admission check
# — scope, member, follower, context fit, budget — decides the automatic path
# too: an automatic dispatch that bypasses admission is worse than a manual one
# that does not, because nobody is watching it.

# The pointer field recording what the reflex did, so a sweep can tell a review
# it already launched from one it has not, and so a reader can see why a run is
# still in scoring rather than guessing.
REVIEW_DISPATCH_FIELD = "review_dispatch"

# The pointer field recording the one repair a finding-bearing review round
# dispatched. A round is the pair a review stands for — the run it reviewed and
# the head it read — so a second sweep over the same stored record reads this
# field and dispatches nothing, while a run reviewed again at a later head
# composes a different round and is free to dispatch its own repair. Named
# beside the review field because a reader asking why a run with findings is not
# yet repaired should find the answer on the reviewed run's own pointer.
REPAIR_DISPATCH_FIELD = "repair_dispatch"

# How many times one round may be resumed before the reflex stops. A resumed
# turn that ends without answering is retried once; a round that has been
# resumed this many times without its head moving is exhausted, so the sweep
# cannot spend lane capacity on it on every cadence forever. The count is read
# from the pointer's durable repair record, not from the entry-time mapping.
REPAIR_RESUME_LIMIT = 2

# The line every composed repair advice carries, and the marker a retry reads to
# tell a refusal from a mid-work death: a manifest quoting it has read a round's
# advice. It is written by :func:`_repair_resume_advice` from this one constant,
# so the marker and the advice it recognises cannot drift apart.
REPAIR_ADVICE_SCOPE_LINE = "Write scope for this round: "

# The line every composed repair advice opens with, naming the round it belongs
# to. The scope line above is identical in every round, so the round token is the
# marker a retry reads to tell a refusal of *this* round from a manifest quoting
# an earlier round's advice.
REPAIR_ROUND_TOKEN_LINE = "Repair round: "  # noqa: S105 - a manifest line prefix, not a credential


def _repair_round_token(round_id: str) -> str:
    """The advice line naming ``round_id``, as the retry reads it back from the
    manifest."""
    return REPAIR_ROUND_TOKEN_LINE + str(round_id or "")


# The pointer field recording every repair round the reflex has *opened* for a
# run. A round opens only when a repair actually starts — a resume or a
# dispatch — so a refusal, an awaiting-lane hold, a decline-only round and an
# exhausted retry each leave it unchanged and the round in hand free to be
# attempted again. The record is run-wide and durable: a new head reviewed after
# the first repair is a second round, and once one round has opened the run is
# handed back to its coordinator rather than repaired again. Held beside the
# per-round field because the two answer different questions: the dispatch field
# says what happened to the head in hand, this says whether any round ever
# opened and how far it got.
REPAIR_ROUNDS_FIELD = "repair_rounds"

# The statuses under which a repair round opens. Only these advance the opened-
# round record; every other outcome is a refusal or a hold on the round already
# in hand.
REPAIR_ROUND_OPENING_STATUSES = frozenset({"resumed", "dispatched"})

# The dispatch role whose findings a repair acts on, and the node-id prefix a
# composed repair carries. A review of a review, an investigate run and a test
# run each carry findings a repair is not meant to act on, and a repair of a
# repair would open a further round for every link — the guard that names them
# lives at the point of use, importing the prefix from the composer so the two
# agree on one spelling rather than restating it.
REPAIR_SOURCE_ROLE = "implement"

# The flight key naming backends a review must never be composed onto. It is a
# routing rule rather than a preference: the composed lane is a fallback list,
# so an exclusion a fallback can step over cannot be honoured.
REVIEW_EXCLUDED_BACKENDS_KEY = "review_excluded_backends"

# The declared launch kind of a backend a coordinator attaches to a task it is
# already running, rather than one reckon spawns. A composed review names a run
# nothing will start.
IN_HARNESS_LAUNCH = "in-harness"


# The head suffix the review store writes onto a head-keyed record path is
# validated with the same pattern before the write, so the reader parses by the
# grammar the writer enforces.
_REVIEW_HEAD_PATTERN = re.compile(r"[0-9A-Fa-f]{7,64}")


def _review_record_subject(path: Path) -> tuple[str, str]:
    """The reviewed run and head a granted record path spells, if either.

    The store writes two spellings — ``<run>.json`` at the legacy path and
    ``<run>.at-<head>.json`` when the head it read is named — and a reviewer
    is granted the reviewed run's own paths whatever it is called, so the path
    names the run it reviews even when the reviewer's node id does not. A tail
    that is not a head falls back to the legacy reading rather than dropping
    the path: a run id is free to contain the suffix's letters.
    """
    name = path.name
    if not name.endswith(".json"):
        return "", ""
    stem = name[: -len(".json")]
    run_id, separator, head = stem.rpartition(".at-")
    if separator and _REVIEW_HEAD_PATTERN.fullmatch(head):
        return run_id, head
    return stem, ""


def _review_subject(pointer: Mapping[str, Any], project: str) -> tuple[str, str]:
    """The reviewed run and head a live review pointer stands for.

    Read from the record paths the reviewer was told to write, which the
    dispatch composes from the reviewed run: a review hand-launched under a
    coordinator's own node id still names the run it is scoring there, and a
    path that names a head is preferred over the legacy spelling because the
    head is the revision the review is evidence about. A reviewer granted none
    of the store's paths falls back to the run its node id spells, which is the
    reviewed run when a coordinator dispatched the review by hand; an id that
    resolves to nothing is left unresolved rather than guessed.
    """
    named_run = ""
    named_head = ""
    legacy_run = ""
    for path in _review_store_record_paths(pointer, project):
        run_id, head = _review_record_subject(path)
        if not run_id:
            continue
        if head and not named_run:
            named_run, named_head = run_id, head
        if not legacy_run:
            legacy_run = run_id
    if named_run:
        return named_run, named_head
    if legacy_run:
        return legacy_run, ""
    return _resolved_reviewed_run_id(pointer, project), ""


def _review_head_covers(reviewed_head: str, head: str) -> bool:
    """Whether a review that read ``reviewed_head`` stands for the head ``head``.

    A review whose dispatch composed no head — or a run whose own head could
    not be resolved — cannot be told apart from one standing at any revision,
    and reading the unnamed side as covering keeps every case the pair cannot
    discriminate exactly where a single-key recogniser left it. With both heads
    named, the review covers only the revision it read, compared with prefix
    tolerance so a short spelling still matches the full one it abbreviates.
    """
    reviewed = str(reviewed_head or "").strip()
    current = str(head or "").strip()
    if not reviewed or not current:
        return True
    return same_revision(reviewed, current)


def _recorded_review_is_live(recorded: Any) -> bool:
    """Whether a recorded dispatch still names a readable live review pointer.

    The reflex's dispatch record is read by two callers that must agree: the
    reflex consults it before composing another review, and the obligations
    reader consults it to decide whether the run still owes one. A pointer path
    that exists but cannot be read — truncated, not JSON, not an object, or
    unreadable by permission — names no run anybody can observe, so the review
    is not in flight and the run is free to be dispatched again. Failing the
    read closed is what keeps the two callers from splitting on exactly the case
    neither can resolve.
    """
    if not isinstance(recorded, Mapping):
        return False
    standing = str(recorded.get("run_id") or "")
    if not standing:
        return False
    try:
        read_pointer(standing)
    except (CrewError, OSError):
        return False
    return True


def _readable_standing_review(review_run_id: str) -> Mapping[str, Any] | None:
    """The standing review's pointer, or None when nothing readable is there."""
    if not review_run_id:
        return None
    try:
        return read_pointer(review_run_id)
    except (CrewError, OSError):
        return None


def _discard_stranded_review(review_run_id: str, *, reason: str) -> str:
    """Discard a review pointer whose launch never spawned a worker.

    Returns a sentence recording what happened, for the reviewed run's own
    dispatch record. The discard leaves its departure marker in the review's
    run directory, and the reason is written onto that marker because the
    marker is the durable place a later reader asks why the pointer went. A
    discard that refuses or fails is reported rather than raised: the reflex is
    a sweep that must reach the rest of the fleet, and a pointer already gone
    is a release as much as one removed.
    """
    promotion_module = importlib.import_module("reckon.crew.promotion")
    try:
        promotion_module.discard(review_run_id)
    except (CrewError, OSError) as exc:
        return f"its discard was refused: {exc}"
    except Exception as exc:  # noqa: BLE001 - a release must not stop the sweep
        return f"its discard failed: {exc}"
    try:
        marker = promotion_module.discard_record_path(review_run_id)
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "its pointer was discarded; no marker was readable to hold the reason"
    if isinstance(data, Mapping):
        data = dict(data)
        data["reason"] = reason
        try:
            runs._write_json(marker, data)
        except OSError:
            return "its pointer was discarded; the reason could not be written"
    return "its pointer was discarded with the reason recorded"


def _review_in_flight(record: Mapping[str, Any]) -> str:
    """The review run already standing for this run at this head, or empty.

    Recognition keys on the pair a review stands for — the run it reviews and
    the head it read — never on the reviewer's node id, which a hand dispatch
    chooses freely: a review hand-dispatched under a coordinator's own id is as
    much this run's standing review as one the reflex launched, and a review
    that read an earlier revision no longer stands for a run resumed past it.
    The dispatch record the reflex wrote is read first, because it is the
    precise answer and it carries the head the attempt composed for; a review
    launched by a coordinator by hand carries no record, so the live pointers
    are swept for one whose granted record paths name the same pair. A recorded
    run whose pointer is gone is not in flight — the review died — and the
    reflex is free to dispatch again rather than wait on a run that no longer
    exists: a promoted or abandoned review leaves no live pointer, and a sweep
    that reads the missing one as a pointer to inspect raises out of the reflex
    rather than recomposing, failing on exactly the case it was written to
    route.
    """
    fields = _review_dispatch_fields(record)
    head = str(fields.get("head") or "")
    recorded = record.get(REVIEW_DISPATCH_FIELD)
    if isinstance(recorded, Mapping) and str(recorded.get("status") or "") == (
        "withdrawn"
    ):
        # A withdrawn attempt stands for nothing: the run's own resume moved the
        # head past it, so it is neither in flight nor a covering claim.
        recorded = None
    if isinstance(recorded, Mapping):
        standing = str(recorded.get("run_id") or "")
        if (
            standing
            and _review_head_covers(str(recorded.get("head") or ""), head)
            and _recorded_review_is_live(recorded)
        ):
            return standing
    project = fields["project"]
    if not project:
        return ""
    run_id = str(record.get("run_id") or "")
    for pointer in list_live(project=project):
        if not _is_review_run(pointer):
            continue
        reviewed, reviewed_head = _review_subject(pointer, project)
        if reviewed and reviewed == run_id and _review_head_covers(reviewed_head, head):
            return str(pointer.get("run_id") or "")
    return ""


def _record_review_dispatch(
    run_id: str | Mapping[str, Any],
    *,
    status: str,
    reason: str,
    review_run_id: str = "",
    backend: str = "",
    reviewed_head: str = "",
) -> None:
    """Write the reflex's outcome onto the run it acted for.

    A skip is recorded as loudly as a dispatch: a review which ran and wrote
    nothing is indistinguishable from one that was never dispatched, and a
    reflex that fires into that ambiguity re-fires against the same run forever.

    The backend the attempt targeted is recorded beside its outcome, because it
    is the only durable fact that lets the next attempt know which lane already
    dropped this run. A recorded run_id does not carry that: once the review
    dies its pointer is gone, and the run goes back to looking unattempted.

    The head the attempt composed for is recorded with the outcome, because an
    attempt about one revision must not read as covering a run that has since
    moved past it: the next sweep compares the recorded pair and sees that the
    standing review no longer speaks for the run's current head.
    """
    if not run_id:
        return

    def record(pointer: dict[str, Any]) -> dict[str, Any]:
        pointer[REVIEW_DISPATCH_FIELD] = {
            "status": status,
            "reason": reason,
            "run_id": review_run_id or None,
            "backend": backend or None,
            "head": reviewed_head or None,
            "at": _utc_now(),
            "attempt": int(
                (pointer.get(REVIEW_DISPATCH_FIELD) or {}).get("attempt") or 0
            )
            + 1,
        }
        return pointer

    if isinstance(run_id, Mapping):
        from reckon._store import write_json_atomically

        directory = plan_review.review_report_directory(
            run_id["project"], run_id["plan_slug"], run_id["run_id"]
        )
        write_json_atomically(
            directory / "dispatch.json",
            record({})[REVIEW_DISPATCH_FIELD],
            indent=2,
            sort_keys=True,
            mode=None,
        )
    else:
        _mutate_pointer(run_id, record)


def _object_id(text: str) -> str:
    """``text`` when it already names a git object id, otherwise empty."""
    return text if re.fullmatch(r"[0-9A-Fa-f]{40,64}", text) else ""


def _resolve_abbreviated_commit(repository: Path | None, text: str) -> str:
    """``text`` resolved to its full commit id through ``repository``, or empty.

    A manifest's commit entry is usually abbreviated, and the run's repository
    shares the object store its worktree wrote into, so the abbreviation still
    names the revision the run reached. An ambiguous or unknown abbreviation is
    left unresolved rather than guessed.
    """
    if repository is None or not text:
        return ""
    return _resolve_commit(repository, text)


def _record_carried_head(record: Mapping[str, Any]) -> str:
    """The head a run's own record names, for a worktree that has been reclaimed.

    A manifest's ``commits:`` field is a run's last word on the revisions it
    landed, so its last resolvable entry is the head the run reached. An entry
    that is already an object id stands as itself; a shorter citation is
    resolved to its full id through the run's repository, whose object store it
    wrote into while the worktree existed, because workers cite abbreviated
    ids. An entry git cannot resolve unambiguously is skipped rather than
    guessed, and the pointer's own recorded head is read after the manifest,
    because a run that has not yet written one still names the revision it was
    dispatched against.
    """
    manifest = Path(str(record.get("manifest_path") or ""))
    try:
        data = parse_manifest(manifest.read_text())
    except (OSError, ManifestParseError):
        data = {}
    entries = data.get("commits") or []
    if isinstance(entries, str):
        entries = [entries]
    repo_raw = str(record.get("repo") or "").strip()
    repository = Path(repo_raw) if repo_raw and Path(repo_raw).is_dir() else None
    for entry in reversed(list(entries)):
        text = str(entry).strip()
        sha = _object_id(text)
        if sha:
            return sha
        if re.fullmatch(r"[0-9A-Fa-f]{7,64}", text):
            resolved = _resolve_abbreviated_commit(repository, text)
            if resolved:
                return resolved
    return _object_id(str(record.get("head") or "").strip())


def _worktree_reclaimed(record: Mapping[str, Any]) -> bool:
    """Whether the run named a worktree that is no longer on disk."""
    worktree_raw = str(record.get("worktree") or "").strip()
    return bool(worktree_raw) and not Path(worktree_raw).is_dir()


def _review_head_and_tree(
    record: Mapping[str, Any],
) -> tuple[str, Path | None]:
    """The head a review of this run is about, and the tree that sourced it.

    The head and the tree have to come from one source: a legacy review record
    naming no revision has the revision it read reconstructed from the tree's
    history, so a tree that did not supply the head would reconstruct against a
    history the head does not belong to and the two sources could disagree.
    While the worktree is readable it supplies both. Once it has been reclaimed
    the run's own record supplies the head, and there is no tree left to
    reconstruct against — ``None`` is returned rather than the shared checkout,
    whose HEAD is not this run's head. A reclaimed worktree whose record names
    no resolvable head therefore reads as no head at all: falling through to
    the repository would key the review on the checkout's HEAD, which is
    exactly the revision this run's review is not about. A record naming no
    worktree at all still resolves through its repository, the only reading
    left for it.
    """
    if _worktree_reclaimed(record):
        return _record_carried_head(record), None
    return _reviewed_run_head(record), _review_tree(record)


def _run_head_for_review(record: Mapping[str, Any]) -> str:
    """The revision a review of this run is about: worktree head, or record head.

    The run's worktree is the authority while it is readable. Once that worktree
    has been reclaimed, ``_review_tree`` would fall back to the run's repository
    — the shared main checkout — whose HEAD is whatever that repository carries
    now rather than the revision this run reached, so a composed dispatch and a
    dropped-lane comparison would both key on the wrong commit. The run's own
    record then supplies the head instead, and a reclaimed record that names no
    resolvable head of its own reads as empty rather than borrowing the
    checkout's HEAD. A record that names no worktree at all still resolves
    through its repository, which for such a record is the only tree left to
    read.
    """
    return _review_head_and_tree(record)[0]


# A review is sized to the risk of a head move, not to the run it examines a
# second time. When a reviewed run gains commits, the stored review stands while
# those commits change no runtime source, and only a runtime-source commit earns
# a re-review — a light one scoped to that commit alone rather than a second
# full read of the whole run. The carry-forward is written to the run's own
# record so it satisfies the review requirement visibly, never as a silent
# absence that a reader would have to reconstruct.
CARRY_FORWARD_FIELD = "review_carry_forward"

# The window a resumed run is left to settle before the reflex dispatches a
# review of it. A review launched against a head a resume is about to move
# reads a revision the run no longer carries, so the reflex waits out the
# window before composing; a review already running when a resume lands
# finishes, and the head-move rule below decides what it means.
REVIEW_SETTLE_SECONDS = 300


def _changed_paths_between(tree: Path | None, older: str, newer: str) -> list[str] | None:
    """The paths that changed between two revisions, or None if git cannot say.

    None is the fail-safe: a history git cannot compare cannot be shown to
    change no runtime source, so the caller must read it as a runtime-source
    move and re-review rather than carry the stored review over an unread diff.
    """
    if tree is None or not older or not newer:
        return None
    try:
        completed = subprocess.run(
            ["git", "diff", "--name-only", f"{older}..{newer}"],
            cwd=tree,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if completed.returncode:
        return None
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def _repo_relative_cited_path(value: str, roots: Sequence[Path | None]) -> str:
    """A finding's cited file as a repository-relative path, or empty.

    A reviewer may cite a path relative to the repository or absolute under the
    tree it read, and the two spellings name one file. Both are reduced to the
    repository-relative form the changed-path lists carry, so a cited path can
    be compared against a moved path without comparing two encodings of the
    same location. An absolute path under none of the known roots names no file
    in this repository and is dropped rather than compared against.
    """
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        return ""
    candidate = Path(text)
    if candidate.is_absolute():
        for root in roots:
            if root is None:
                continue
            try:
                return str(candidate.relative_to(root)).replace("\\", "/")
            except ValueError:
                continue
        return ""
    while text.startswith("./"):
        text = text[2:]
    return text


def _review_delivered_paths(tree: Path, review: Mapping[str, Any]) -> list[str] | None:
    """The repo-relative paths the reviewed run delivered or a finding cites.

    The delivered diff is the pair the stored record itself carries, so it names
    the work that review is about, not whichever revision the run has since
    reached. Its findings cite the files the review found things in. ``None``
    means the delivered diff could not be read: the caller must treat the move
    as touching the run's own work rather than prove safety from the half it
    holds, because a history git cannot compare cannot be shown to leave the
    delivered work alone. A stored record that names no base is in that same
    case: without a base the delivered paths are unknown rather than empty, so
    an empty reading would carry a review nothing shows to be safe.
    """
    delivered: list[str] = []
    _, base, _, head = review_module.carried_revision_pair(review)
    base_sha = str(base or "").strip()
    head_sha = str(head or "").strip()
    if not base_sha:
        # No base means no pair to diff, so the delivered paths are unknown
        # rather than empty. Reading the absence as an empty diff would let a
        # move over the run's own deliverable carry a review nothing shows to
        # be safe, so it is read as unprovable, exactly as an uncomparable
        # pair is.
        return None
    changed = _changed_paths_between(tree, base_sha, head_sha)
    if changed is None:
        return None
    delivered.extend(changed)
    roots: list[Path | None] = [tree]
    reviewed_worktree = str(review.get("reviewed_worktree") or "").strip()
    if reviewed_worktree:
        roots.append(Path(reviewed_worktree))
    findings = review.get("findings")
    if isinstance(findings, Sequence) and not isinstance(findings, (str, bytes)):
        for finding in findings:
            if not isinstance(finding, Mapping):
                continue
            cited = _repo_relative_cited_path(str(finding.get("file") or ""), roots)
            if cited:
                delivered.append(cited)
    unique: list[str] = []
    for path in delivered:
        if path not in unique:
            unique.append(path)
    return unique


def review_head_move(record: Mapping[str, Any]) -> dict[str, Any]:
    """How a run's head moved past the head its stored review read.

    Empty when there is nothing to decide: no readable tree, no complete stored
    review, a review that already describes the run's current head, or a move
    between revisions git cannot compare. The changed paths are classified once,
    through :func:`reckon.review_tiers.changes_runtime_source`, so the reflex
    and the promotion gate cannot split on whether a moved commit is runtime
    source — a second classifier here is exactly the drift this shares instead.

    The head is the run's own — its worktree's while that is readable, its
    record's once the worktree has been reclaimed. A reclaimed record that
    names no resolvable head moves nowhere: reading the shared checkout's HEAD
    in its place would carry a review to a revision the run never reached.
    """
    tree = _review_tree(record)
    if tree is None:
        return {}
    head = _run_head_for_review(record)
    if not head:
        return {}
    project = str(record.get("project") or "")
    run_id = str(record.get("run_id") or "")
    stored = review_module.read_review(project, run_id)
    if stored is None or not _review_is_complete(stored):
        return {}
    reviewed_head = review_described_head(stored, tree=tree)
    if not reviewed_head or same_revision(reviewed_head, head):
        return {}
    paths = _changed_paths_between(tree, reviewed_head, head)
    if paths is None:
        return {}
    delivered = _review_delivered_paths(tree, stored)
    if delivered is None:
        touches_delivered_work = True
        touched: list[str] = []
    else:
        delivered_set = set(delivered)
        touched = [path for path in paths if path in delivered_set]
        touches_delivered_work = bool(touched)
    return {
        "reviewed_head": reviewed_head,
        "head": head,
        "paths": paths,
        "changes_runtime_source": review_tiers.changes_runtime_source(paths),
        "touches_delivered_work": touches_delivered_work,
        "deliverable_paths": touched,
    }


def _diff_between(tree: Path | None, older: str, newer: str) -> str | None:
    """The text diff between two revisions, or None if git cannot give one."""
    if tree is None or not older or not newer:
        return None
    try:
        completed = subprocess.run(
            ["git", "diff", f"{older}..{newer}"],
            cwd=tree,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if completed.returncode:
        return None
    return completed.stdout


def _judge_head_move(
    record: Mapping[str, Any],
    move: Mapping[str, Any],
    *,
    config: Mapping[str, Any] | None,
) -> review_need.Verdict | None:
    """Whether a move the review rule would send back still needs a reviewer.

    Asked only when the stored review is clean — complete, at or above the
    floor and raising no finding. A review that raised findings is answered by
    the move, as a repair is, so the move is read whatever its size. The judge
    reads the move's diff against the run's goal and done-when; None means it
    was not asked, and the caller's rule stands.
    """
    from reckon import flight

    project = str(record.get("project") or "")
    run_id = str(record.get("run_id") or "")
    if not _review_accepts_promotion(review_module.read_review(project, run_id)):
        return None
    diff = _diff_between(_review_tree(record), move["reviewed_head"], move["head"])
    if not diff:
        return None
    node = record.get("node") or {}
    goal = str(node.get("goal") or "")
    done_when = str(node.get("done_when") or "")
    verdicts = review_need.judge(
        [
            review_need.Change(
                identity=run_id, reviewed="", present="", goal=done_when, diff=diff
            )
        ],
        subject=review_need.RUN_SUBJECT,
        goals={"goal": goal, "done_when": done_when},
        threshold=flight.review_need_threshold(config),
    )
    return verdicts.get(run_id)


def carry_review_forward(
    record: Mapping[str, Any], *, config: Mapping[str, Any] | None = None
) -> dict[str, Any] | None:
    """Carry a stored review forward over a data-only move, else light re-review.

    Returns None when the head did not move. This is the reflex's decision for a
    reviewed run that gained commits, and it is expressed through the same
    record selection every reader shares, so the classifier, the promotion gate
    and the obligation producer cannot disagree about which review a run owes.

    A move over paths that change neither runtime source nor any path the run
    delivered is carried: the stored record is re-stored at the run's new head,
    marked with the revision it came from and the paths the move touched, so a
    review exists at the head the run now carries and the run raises no review
    requirement.

    A move is not carried when it touches runtime source, or when it touches a
    path the reviewed diff changed or a stored finding cites. Runtime source
    earns a light re-review scoped to the new commit alone, as before. A move
    over the run's own deliverable or a finding's file is new work a reviewer
    must read: the review is not re-stamped onto a head no reviewer read, and
    the refusal is written to the run's record so a reader sees the head was
    left unreviewed on purpose.

    Either kind of move is carried after all when the stored review is clean
    and the review-need judge, reading the move's diff against the run's goal
    and done-when, answers that it needs no new review. The judgement is kept
    on the carried record. A judge that cannot answer leaves the rule above.
    """
    move = review_head_move(record)
    if not move:
        return None
    run_id = str(record.get("run_id") or "")
    project = str(record.get("project") or "")
    judged = (
        _judge_head_move(record, move, config=config)
        if move["changes_runtime_source"] or move["touches_delivered_work"]
        else None
    )
    carried_by_judgement = judged is not None and judged.required is False
    if move["changes_runtime_source"] and not carried_by_judgement:
        return {
            "run_id": run_id,
            "carried": False,
            "review_tier": review_tiers.LIGHT,
            "scope": list(move["paths"]),
            "paths": list(move["paths"]),
            "reviewed_head": move["reviewed_head"],
            "head": move["head"],
        }
    if move["touches_delivered_work"] and not carried_by_judgement:
        touched = list(move["deliverable_paths"])
        if touched:
            detail = "touches the delivered work: " + ", ".join(touched)
        else:
            detail = (
                "cannot be shown to leave the delivered work alone, because the "
                "reviewed diff could not be read"
            )
        declined = (
            f"the move {detail}; the stored review does not carry to the new head"
        )
        _mutate_pointer(
            run_id,
            lambda pointer: {
                **pointer,
                CARRY_FORWARD_FIELD: {
                    "carried": False,
                    "from": move["reviewed_head"],
                    "to": move["head"],
                    "paths": list(move["paths"]),
                    "deliverable_paths": touched,
                    "reason": declined,
                    "at": _utc_now(),
                },
            },
        )
        return {
            "run_id": run_id,
            "carried": False,
            "review_tier": review_tiers.LIGHT,
            "scope": list(move["paths"]),
            "paths": list(move["paths"]),
            "deliverable_paths": touched,
            "reason": declined,
            "reviewed_head": move["reviewed_head"],
            "head": move["head"],
        }
    raw = review_module.stored_record(
        project, run_id, reviewed_head_sha=move["reviewed_head"]
    )[1]
    if raw is None:
        return None
    carried = dict(raw)
    carried["reviewed_base_sha"] = move["reviewed_head"]
    carried["reviewed_head_sha"] = move["head"]
    carried["timestamp"] = _utc_now()
    carried["carried_forward"] = {
        "from": move["reviewed_head"],
        "to": move["head"],
        "paths": list(move["paths"]),
        "at": carried["timestamp"],
    }
    if carried_by_judgement:
        # The move changed what the rule alone would send back to a reviewer;
        # the judgement that let the review carry is kept beside it.
        carried["carried_forward"]["judged"] = {
            "probability": judged.probability,
            "source": judged.source,
        }
    review_module.store_review(carried)
    _mutate_pointer(
        run_id,
        lambda pointer: {
            **pointer,
            CARRY_FORWARD_FIELD: dict(carried["carried_forward"]),
        },
    )
    return {
        "run_id": run_id,
        "carried": True,
        "review_tier": review_tiers.NONE,
        "scope": list(move["paths"]),
        "reviewed_head": move["reviewed_head"],
        "head": move["head"],
        **(
            {"judged": dict(carried["carried_forward"]["judged"])}
            if carried_by_judgement
            else {}
        ),
    }


def review_settle_seconds_remaining(record: Mapping[str, Any]) -> float:
    """Seconds left before a resumed run is settled enough to review, or zero.

    A review dispatched against a head a resume is about to move reads a
    revision the run no longer carries, so the reflex waits the settle window
    out from the run's own resume stamp before composing. A run with no resume
    stamp is settled already.
    """
    resume = record.get("auto_resume")
    resumed_at = parse_utc(resume.get("at")) if isinstance(resume, Mapping) else None
    if resumed_at is None:
        return 0.0
    elapsed = (datetime.now(tz=UTC) - resumed_at).total_seconds()
    return max(0.0, REVIEW_SETTLE_SECONDS - elapsed)


def withdraw_superseded_review(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """Withdraw a queued review the run's own resume has moved past.

    A review dispatch is queued against the head it composed for. Once the run
    is resumed its head moves, and the queued review speaks for a revision the
    run no longer carries. The reflex records the attempt withdrawn rather than
    letting it stand, so the superseded attempt is visible and the run is free
    to be re-reviewed — or carried — at its new head. A dispatch that already
    covers the current head, or a run not resumed since the attempt was
    recorded, is left standing.

    Only an attempt that never started is withdrawn. A review whose worker has
    launched is running, and a running review finishes: its verdict reaches the
    run's new head through the carry-forward rule rather than being discarded
    here, so the review run's own launch record decides and the age of the
    dispatch record does not.
    """
    run_id = str(record.get("run_id") or "")
    recorded = record.get(REVIEW_DISPATCH_FIELD)
    if not run_id or not isinstance(recorded, Mapping):
        return None
    if str(recorded.get("status") or "") == "withdrawn":
        return None
    resume = record.get("auto_resume")
    resumed_at = parse_utc(resume.get("at")) if isinstance(resume, Mapping) else None
    if resumed_at is None:
        return None
    at = parse_utc(recorded.get("at"))
    if at is not None and resumed_at <= at:
        return None
    if _review_worker_launched(str(recorded.get("run_id") or "")):
        # The review's own worker already launched, so the review is running:
        # it finishes and its verdict reaches the run's new head through the
        # carry-forward rule rather than being withdrawn here.
        return None
    head = _run_head_for_review(record)
    if _review_head_covers(str(recorded.get("head") or ""), head):
        return None
    _record_review_dispatch(
        run_id,
        status="withdrawn",
        reason=(
            "the run was resumed after this review was queued, so the review "
            "speaks for a head the run no longer carries"
        ),
    )
    return {
        "run_id": run_id,
        "withdrawn": True,
        "review_run_id": str(recorded.get("run_id") or ""),
        "head": head,
    }


def _review_worker_launched(review_run_id: str) -> bool:
    """Whether the recorded review run ever spawned a worker to read the head.

    A review whose worker already launched is running and finishes; only an
    attempt that never started may be withdrawn when a resume moves the head
    past it. The review run's own worker record is the fact that decides it —
    the supervisor writes it at spawn, and it outlives the worker — so its
    presence says a review started and its absence (a queued attempt, or one
    refused before any worker existed) leaves the attempt open to withdrawal.
    """
    if not review_run_id:
        return False
    return _worker_record({"run_id": review_run_id}) is not None


def _review_attempt_withdrawn_before_launch(run_id: str) -> bool:
    """Whether a recorded review run never spawned a worker to read the head.

    An attempt whose run was withdrawn during setup — a claim, node name or
    worktree clash the supervisor refused before any worker existed — leaves an
    exit record saying the attempt ended during launch with no stream byte read.
    No review ever ran, so the lane is free to carry the next sweep's
    composition rather than being withheld as one that dropped the head.

    The worker record the supervisor writes at spawn decides that, not the exit
    record alone. A spawned worker killed before it wrote a single stream byte
    also leaves an exit record saying the launch ended with no stream record
    read, so classifying on that record alone frees the lane that did drop the
    head and recomposes onto it — the refusal loop this rule exists to bound.
    The presence of the worker record is positive evidence a worker launched,
    whatever the stream count, and withholds the lane.

    Only that positive evidence frees the lane. A run naming no id, or one whose
    records are gone, is left standing as an attempt that stood: recomposing
    onto a lane that did drop this exact head costs at most one refused
    dispatch, whereas wrongly withholding a lane leaves a run with no review at
    all.
    """
    if not run_id:
        return False
    if _worker_record({"run_id": run_id}) is not None:
        return False
    exit_record = _run_exit_record({"run_id": run_id})
    return exit_record is not None and _exit_record_is_launch_failure(exit_record)


def _failed_review_backend(record: Mapping[str, Any]) -> str:
    """The lane the run's most recent recorded attempt used for its current head.

    A recorded attempt that produced neither a stored nor an in-flight review
    has failed, and the caller reaches selection only when neither exists — so a
    backend the record names *for this head* is one the run has already been
    dropped by, and recomposing onto it repeats the attempt rather than
    advancing it.

    The attempt's status gates that reading: only a ``dispatched`` attempt ever
    reached a lane. A dispatch refused at admission, or one recorded as
    awaiting-lane, started nothing, so its named lane neither dropped the run
    nor is barred from the next sweep.

    The head the attempt composed for gates it too. A run that has since moved
    past the recorded revision is owed a different review, so the lane that
    dropped the earlier attempt is free to carry the new one: counting it as
    dropped would refuse the re-review on the very lane that carried the run.
    An attempt naming no head cannot be tied to the current head and is not
    counted either — the conservative direction, because recomposing onto a lane
    that did drop this exact head costs one refused dispatch, whereas wrongly
    withholding a lane leaves a run with no review at all.

    Finally an attempt withdrawn before a worker launched is not a drop the lane
    earned, as :func:`_review_attempt_withdrawn_before_launch` reads from the
    review run's own exit record; the caller has already established that no
    review stands for the head.
    """
    recorded = record.get(REVIEW_DISPATCH_FIELD)
    if not isinstance(recorded, Mapping):
        return ""
    if str(recorded.get("status") or "").strip() != "dispatched":
        return ""
    backend = str(recorded.get("backend") or "").strip()
    if not backend:
        return ""
    recorded_head = str(recorded.get("head") or "").strip()
    if not recorded_head:
        return ""
    current_head = _run_head_for_review(record)
    if not current_head or not same_revision(recorded_head, current_head):
        return ""
    if _review_attempt_withdrawn_before_launch(
        str(recorded.get("run_id") or "").strip()
    ):
        return ""
    return backend


def _review_backend_excluded(name: str, config: Mapping[str, Any]) -> bool:
    """Compare an exclusion's pair, or every model when it names a lane."""
    raw = {str(item).strip() for item in config.get(REVIEW_EXCLUDED_BACKENDS_KEY) or ()}
    if name in raw:
        return True
    if not raw:
        return False
    settings = (config.get("backends") or {}).get(name) or {}
    identity = ledger.normalize_identity({**settings, "backend": name})
    for excluded in raw:
        lane, key = ledger.resolve_name(excluded)
        if (
            lane
            and identity.get("lane") == lane
            and (excluded == lane or identity.get("model_key") == key)
        ):
            return True
    return False


def _review_excluded_backends(config: Mapping[str, Any]) -> set[str]:
    """Backends the flight configuration removes from review routing.

    Read as a rule about which lanes may carry a review rather than a
    preference: it is consulted before any ordering, so a fallback cannot walk
    around it.
    """
    raw = config.get(REVIEW_EXCLUDED_BACKENDS_KEY)
    names = {str(name).strip() for name in raw or () if str(name).strip()}
    return names | {
        str(name)
        for name in config.get("backends") or {}
        if _review_backend_excluded(str(name), config)
    }


def _review_in_harness_backends(config: Mapping[str, Any]) -> set[str]:
    """Configured backends whose declared launch is the calling harness itself.

    An in-harness backend is started by a coordinator attaching it to a task it
    already runs, so a review composed onto one is recorded, holds the review's
    write-path claim and never executes. Read from the backend declaration
    rather than from a name list, because a list is maintained by hand in every
    project layer and a layer that omits the name gets the unlaunchable review
    back.
    """
    backends = config.get("backends") or {}
    return {
        str(name)
        for name, settings in backends.items()
        if isinstance(settings, Mapping) and settings.get("launch") == IN_HARNESS_LAUNCH
    }


# How old a lane's own published reading may be before a review compose stops
# trusting it. The lane publishes its occupancy on its own clock and states the
# shelf life it suggests for the figure; this bound is the shorter of the two,
# because a reading that old describes a fleet that has since drained or filled
# and acting on it would hold work for a lane that is no longer busy.
LANE_READING_FRESH_SECONDS = 45


def _ordered_review_lanes(
    config: Mapping[str, Any], *, owning_backend: str = ""
) -> list[str]:
    """Configured backends a composed review may run on, in preference order.

    The owning run's recorded backend leads when it is known and not excluded,
    because the review is of that run and the lane that carried it is the one
    its coordinator chose; the locally served backend follows, then the rest in
    a stable alphabetical order. Excluded backends never appear: a coordinator
    that has removed a backend from review routing must not see a fallback land
    on it, or the exclusion is a note rather than a rule. Neither does an
    in-harness backend, whatever any list says about it: a review composed onto
    one is a run nothing starts, which is the shape this ordering exists to
    prevent. The owning lane and the local lane lead even when the configuration
    no longer lists them, so a composed command names the lane the run was
    actually carried on rather than substituting one the reader did not choose;
    whether a named lane can be dispatched is dispatch's own check, not this
    ordering's.
    """
    backends = config.get("backends") or {}
    excluded = _review_excluded_backends(config)
    in_harness = _review_in_harness_backends(config)
    names = [
        str(name)
        for name in sorted(backends)
        if str(name) not in excluded and str(name) not in in_harness
    ]
    local = str(config.get("local_backend") or "").strip()
    owning = str(owning_backend or "").strip()
    ordered: list[str] = []
    for preferred in (owning, local):
        if (
            preferred
            and not _review_backend_excluded(preferred, config)
            and preferred not in in_harness
            and preferred not in ordered
        ):
            ordered.append(preferred)
    ordered.extend(name for name in names if name not in ordered)
    return ordered


def _current_lane_reading(
    config: Mapping[str, Any], name: str
) -> dict[str, Any] | None:
    """The named lane's own published reading, while it still describes now.

    A backend may declare ``lane_document``: the JSON a serving lane republishes
    about its own occupancy. Only a current reading is returned. A document
    that is absent, unreadable or malformed answers None without raising, and
    so does a reading older than the lane's own declared shelf life or than
    :data:`LANE_READING_FRESH_SECONDS`; each of those reads as a lane the
    reflex has no measurement of, which is a lane it composes onto as before.
    The direction is deliberate: a missing reading must never hold work.
    """
    backends = config.get("backends") or {}
    settings = backends.get(name) if isinstance(backends, Mapping) else None
    if not isinstance(settings, Mapping):
        return None
    declared = str(settings.get("lane_document") or "").strip()
    if not declared:
        return None
    report = _lane_document.read_lane_document_file(declared)
    age = report.get("age_seconds")
    if report.get("stale") or isinstance(age, bool):
        return None
    if not isinstance(age, (int, float)) or age > LANE_READING_FRESH_SECONDS:
        return None
    return report


def _lane_over_ceiling(
    reading: Mapping[str, Any],
) -> tuple[int | float, int | float] | None:
    """The ``(running, ceiling)`` a reading reports at or over, else None.

    Both figures are needed and both must be numbers the document published: a
    document carrying a running count but no ceiling states no limit this lane
    measured itself against, and either figure missing leaves the lane usable
    rather than saturated, because a hold rests on a measurement and never on
    its absence.
    """
    running = reading.get("running")
    ceiling = reading.get("concurrent_requests")
    for value in (running, ceiling):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
    if running < ceiling:
        return None
    return running, ceiling


def _review_lane_plan(
    config: Mapping[str, Any], *, owning_backend: str = ""
) -> tuple[list[str], tuple[str, int | float, int | float] | None]:
    """The review lanes still eligible, and the lane withholding the selection.

    The ordering is :func:`_ordered_review_lanes`; this pass then reads each
    candidate's own published reading and removes a lane its own document
    reports at or over the ceiling it computed for itself. A saturated lane
    cannot take the review now, so the next eligible candidate follows it —
    the reflex honours the destination lane's headroom rather than pinning
    work to a lane that is already over its own limit.

    The local lane is the exception, and the reason the withheld lane is
    returned rather than only dropped: a review is local-lane-shaped work,
    which stays on the local lane, so a saturated local lane with nothing
    eligible ahead of it withholds the selection instead of handing the review
    to a metered lane. The caller records the hold as ``awaiting-lane`` naming
    it, and a later sweep composes the review once the lane has drained.

    The lane that withholds the selection comes back with the counts its own
    document published, so the hold can name the figures it rests on.
    """
    local = str(config.get("local_backend") or "").strip()
    eligible: list[str] = []
    for name in _ordered_review_lanes(config, owning_backend=owning_backend):
        reading = _current_lane_reading(config, name)
        over = None if reading is None else _lane_over_ceiling(reading)
        if over is None:
            eligible.append(name)
            continue
        if name == local and not eligible:
            return [], (name, over[0], over[1])
    return eligible, None


def _review_lane_candidates(
    config: Mapping[str, Any], *, owning_backend: str = ""
) -> list[str]:
    """Configured backends a composed review may run on, in selection order.

    The ordering and the rules that remove a backend from it are
    :func:`_ordered_review_lanes`; what this adds is the lane's own reading of
    itself. A candidate whose published document reports it running at or above
    its own concurrent-request ceiling is passed over while that reading is
    current, and a saturated local lane with no eligible candidate ahead of it
    yields no lane at all, so the review waits for that lane rather than moving
    to a metered one. A lane whose reading is stale, unreadable or absent keeps
    its place: the hold exists to avoid composing onto a lane measured over its
    limit, not to withhold work no document speaks about.
    """
    eligible, _withheld = _review_lane_plan(config, owning_backend=owning_backend)
    return eligible


def _no_lane_reason(
    run_id: str,
    config: Mapping[str, Any],
    *,
    owning_backend: str = "",
    kind: str,
    previous_lane: str = "",
) -> str:
    """Why the composed ``kind`` for a run has no lane left, naming each rule.

    Each rule that removes a lane is worth naming on its own: a reader told only
    that no configured backend remains would look for a lane to add, when what
    the configuration actually says is that the lane is deliberately withheld
    (an exclusion), that the lane cannot be started at all (an in-harness
    launch) or that the lane is saturated by its own published figures. Either
    way the reader learns which lever to reach for rather than reading a hold
    that names no cause. The rules are the same for every run the reflex
    composes onto a lane — a review and the repair that answers it draw from one
    candidate list — so ``kind`` names which run is being held rather than which
    rules applied. ``previous_lane`` is the lane an attempt for the same work
    already used, and is empty where a kind places no lane by that rule.
    """
    parts: list[str] = []
    withheld = _review_lane_plan(config, owning_backend=owning_backend)[1]
    if withheld is not None:
        lane, running, ceiling = withheld
        parts.append(
            f"backend {lane!r} is saturated at {running:g} running against its "
            f"{ceiling:g} concurrent_requests ceiling"
        )
    excluded = _review_excluded_backends(config)
    if excluded:
        parts.append(
            f"{REVIEW_EXCLUDED_BACKENDS_KEY} excludes "
            + ", ".join(sorted(excluded))
            + " from review routing"
        )
    in_harness = _review_in_harness_backends(config)
    if in_harness:
        parts.append(
            "backend(s) "
            + ", ".join(sorted(in_harness))
            + f" launch as {IN_HARNESS_LAUNCH} and cannot be started"
        )
    if previous_lane:
        parts.append(f"backend {previous_lane!r} already dropped it")
    detail = "; ".join(parts)
    reason = f"the {kind} for {run_id} has no eligible lane"
    return f"{reason} ({detail})" if detail else reason


def _resolved_review_config(
    project: str, config: Mapping[str, Any] | None
) -> Mapping[str, Any]:
    """The flight config a review dispatch resolves its local lane against."""
    if config is not None:
        return config
    from reckon import flight

    return flight.resolve(project=project).config


def _standing_plan_review(project: str, plan_slug: str) -> str:
    """The live plan-review run standing for ``plan_slug``, or empty.

    One reader for both the locked dispatch and the preview, so the answer a
    dry run reports and the answer the lock protects cannot be two spellings
    of the same scan.
    """
    for pointer in list_live(project=project):
        node = pointer.get("node") or {}
        if node.get("id") == f"{PLAN_REVIEW_NODE_PREFIX}{plan_slug}":
            return str(pointer.get("run_id") or "")
    return ""


def dispatch_review_for_run(
    record: Mapping[str, Any],
    *,
    config: Mapping[str, Any] | None = None,
    launcher: Callable[..., Any] | None = None,
    allow_unreconciled_runs: bool = True,
    prefer_local: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run the review dispatch a scoring run has already composed for itself.

    The return value is the reflex's own report, not a command: ``dispatched``
    says whether a review run is now in flight, ``run_id`` names it, and
    ``reason`` explains a false. Nothing here is raised for an ordinary refusal
    — a scope, member, follower, context-fit or budget refusal is *reported*
    and recorded against the run, because the caller is a sweep that must reach
    the rest of the fleet. The refusal itself still comes from dispatch, so the
    automatic path is refused exactly where a manual dispatch is rather than
    being waved through.

    ``allow_unreconciled_runs`` defaults on because a run awaiting review is
    itself an unreconciled run: past the grace window the fence refuses the
    review dispatch that is the only thing able to clear it, so the automatic
    path would deadlock on the runs it exists for. The waiver is recorded on
    the review run's own pointer by dispatch, naming the runs it waived, so the
    exception stays visible after the command that supplied it is gone.
    """
    if record.get("subject") == "plan":
        if dry_run:
            # A preview takes no lock and creates nothing: the directory and
            # its lock file are themselves artifacts of a dispatch that has not
            # happened, and the in-flight read it guards is a plain read here.
            standing = _standing_plan_review(record["project"], record["plan_slug"])
            if standing:
                return {
                    "dispatched": False,
                    "review_run_id": standing,
                    "reason": "a plan review is already in flight as a live run",
                }
            return _dispatch_composed_review(
                record,
                _review_dispatch_fields(record, write=False),
                config=config,
                launcher=launcher,
                allow_unreconciled_runs=allow_unreconciled_runs,
                prefer_local=prefer_local,
                dry_run=True,
            )
        directory = plan_review.review_report_directory(
            record["project"], record["plan_slug"], record["run_id"]
        ).parent
        directory.mkdir(parents=True, exist_ok=True)
        # The lock spans the pointer check and launch across coordinator sessions.
        with (directory / "dispatch.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            standing = _standing_plan_review(record["project"], record["plan_slug"])
            if standing:
                return {
                    "dispatched": False,
                    "review_run_id": standing,
                    "reason": "a plan review is already in flight as a live run",
                }
            # The composed fields are held so the one exit below can discard the
            # run directory they composed when the dispatch launched nothing:
            # every refusal and hold the dispatch can return reads as not
            # dispatched, so one check covers them all rather than a removal
            # copied into each branch.
            fields = _review_dispatch_fields(record)
            report = _dispatch_composed_review(
                record,
                fields,
                config=config,
                launcher=launcher,
                allow_unreconciled_runs=allow_unreconciled_runs,
                prefer_local=prefer_local,
                dry_run=False,
            )
            if not report.get("dispatched"):
                report = _discard_composed_plan_review(fields, report)
            return report
    run_id = str(record.get("run_id") or "")
    if _is_review_node(record):
        return {
            "run_id": run_id,
            "dispatched": False,
            "reason": (
                "the run is itself a review, so dispatching its review would "
                "compose a review of a review"
            ),
        }
    row = classify_pointer(record)
    if row["classification"] != "scoring":
        return {
            "run_id": run_id,
            "dispatched": False,
            "reason": f"the run is not awaiting review ({row['classification']})",
        }
    review, review_error = _stored_review(record)
    if review is not None or review_error:
        # A stored review is evidence, readable or not. Regenerating over an
        # unparseable one would discard what the reviewer actually wrote, so
        # the run is left for its coordinator with the reason named.
        return {
            "run_id": run_id,
            "dispatched": False,
            "review_status": "unreadable" if review_error else "present",
            "reason": (
                "a review is already stored for this run and is not a complete "
                "parse; repair or replace it rather than dispatching a second"
            ),
        }
    in_flight = _review_in_flight(record)
    if in_flight:
        standing = _readable_standing_review(in_flight)
        if standing is not None and _stranded_launch(standing, now_seconds=time.time()):
            # The standing claim is stranded: it never spawned a worker, so
            # nothing is coming to finish it or to release it, and the run
            # cannot be reviewed while it stands. The reflex releases it — the
            # pointer is discarded with the reason recorded — and falls through
            # to compose the review again for the same pair.
            reason = (
                f"the review run {in_flight} was a stranded launch: its pointer "
                f"held the pre-spawn phase {str(standing.get('phase') or '')!r} "
                f"for more than {STRANDED_LAUNCH_BOUND_SECONDS}s with no worker "
                "record, stream or launch log, so its claim was released and the "
                "review composed again"
            )
            release_note = _discard_stranded_review(in_flight, reason=reason)
            _record_review_dispatch(
                run_id,
                status="released-stranded-launch",
                reason=f"{reason}; {release_note}",
                review_run_id=in_flight,
            )
        else:
            return {
                "run_id": run_id,
                "dispatched": False,
                "reason": "a review is already in flight as a live run",
                "review_run_id": in_flight,
            }

    # The run has a stored review that speaks for an earlier head, or none at
    # all. A move the stored review no longer covers is either carried forward
    # — data-only commits, no re-review — or re-reviewed lightly, scoped to the
    # commits that changed runtime source. Only a run with no review at all
    # (or a move git cannot compare) falls through to the full review below.
    move = carry_review_forward(record, config=config)
    if move is not None and move["carried"]:
        reason = (
            "the run gained only commits that change no runtime source, so its "
            "stored review is carried forward to the new head " + move["head"][:12]
        )
        _record_review_dispatch(
            run_id,
            status="carried-forward",
            reason=reason,
            reviewed_head=move["head"],
        )
        return {**move, "dispatched": False, "reason": reason}
    delta = move if move is not None else None

    fields = _review_dispatch_fields(record, delta=delta)
    # A review keys on the pair it stands for — the run it reviews and the head
    # it read — so a run whose worktree has been reclaimed and whose record
    # names no resolvable head composes nothing. Falling through here would
    # dispatch a review of the shared checkout's HEAD, which is not this run's
    # head, and every review of that revision would leave the run still owing
    # one: the loop this composition exists to refuse.
    if not fields["head"] and _worktree_reclaimed(record):
        reason = (
            f"the run's worktree {str(record.get('worktree') or '').strip()} "
            "has been reclaimed and its record names no resolvable head, so no "
            "review composes; the run must name the head it reached before a "
            "review can key on it"
        )
        _record_review_dispatch(run_id, status="refused", reason=reason)
        return {
            "run_id": run_id,
            "dispatched": False,
            "refused": True,
            "reason": reason,
        }
    return _dispatch_composed_review(
        record,
        fields,
        config=config,
        launcher=launcher,
        allow_unreconciled_runs=allow_unreconciled_runs,
        prefer_local=prefer_local,
        dry_run=dry_run,
    )


def _discard_composed_plan_review(
    fields: Mapping[str, Any], report: Mapping[str, Any]
) -> dict[str, Any]:
    """Remove the plan-review run directory a dispatch composed but did not launch.

    A plan-review attempt composes its run directory — the brief, the plan
    snapshot and the sidecar — before the lane admits it, so the caller that
    composed it discards it here once when the dispatch launched nothing, rather
    than a removal copied into each of the dispatch's refusal branches. Only the
    directory this call created is removed: a run directory that already existed
    when the fields were composed leaves the returned report untouched. A
    removal that fails is not swallowed — it is named on the report under
    ``discard_error`` with the path and the error, so a caller can see what was
    left behind.
    """
    if not fields.get("report_directory_created"):
        return dict(report)
    directory = Path(str(fields.get("write_path") or ""))
    try:
        shutil.rmtree(directory)
    except OSError as exc:
        return {**report, "discard_error": {"path": str(directory), "error": str(exc)}}
    return dict(report)


def _dispatch_composed_review(
    record: Mapping[str, Any],
    fields: Mapping[str, Any],
    *,
    config: Mapping[str, Any] | None,
    launcher: Callable[..., Any] | None,
    allow_unreconciled_runs: bool,
    prefer_local: bool,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Launch either review subject from the shared fields and dispatch path."""
    run_id = str(record.get("run_id") or "")
    dispatch_subject = record if record.get("subject") == "plan" else run_id
    project = fields["project"]
    repo = str(record.get("repo") or "")
    if not project or not repo:
        reason = "the run records no project or repository to dispatch against"
        _record_review_dispatch(
            dispatch_subject,
            status="refused",
            reason=reason,
            reviewed_head=fields["head"],
        )
        return {"run_id": run_id, "dispatched": False, "reason": reason}

    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    from reckon.crew.dispatch import BudgetHold, LanePaused
    from reckon.crew.node import TaskNode

    resolved = _resolved_review_config(project, config)
    if record.get("subject") != "plan" or record.get("local"):
        try:
            from reckon import flight

            resolved = flight.select_local_backend(resolved)
        except Exception as exc:  # noqa: BLE001 - the configured lane is the reason
            reason = f"the local lane is unavailable: {exc}"
            _record_review_dispatch(
                dispatch_subject,
                status="awaiting-lane",
                reason=reason,
                reviewed_head=fields["head"],
            )
            return {
                "run_id": run_id,
                "dispatched": False,
                "awaiting_lane": True,
                "reason": reason,
            }

    if record.get("subject") == "plan":
        lane = _composed_review_lane(project, record, resolved)
        if lane is None:
            reason = _no_lane_reason(run_id, resolved, kind="review")
            _record_review_dispatch(
                dispatch_subject,
                status="awaiting-lane",
                reason=reason,
                reviewed_head=fields["head"],
            )
            return {
                "run_id": run_id,
                "dispatched": False,
                "awaiting_lane": True,
                "reason": reason,
            }
        on_local_lane = "--local" in lane
        backend = lane[-1] if lane else str(resolved.get("default_backend") or "")
    else:
        # The continuous reflex can try another eligible lane after one drops a
        # review. Recovery's explicit sweep stays on the local lane unless the
        # reviewed node declared another, and waits if that lane is unavailable.
        local_lane = str(resolved.get("local_backend") or "").strip()
        owning_lane = str(record.get("backend") or "").strip()
        if prefer_local:
            node = record.get("node") or {}
            declaration = node.get("lane_declaration") or {}
            owning_lane = str(declaration.get("backend") or "").strip()
        previous_lane = _failed_review_backend(record)
        candidates = [
            name
            for name in _review_lane_candidates(resolved, owning_backend=owning_lane)
            if name != previous_lane
            and (not prefer_local or name == (owning_lane or local_lane))
        ]
        if not candidates:
            reason = _no_lane_reason(
                run_id,
                resolved,
                owning_backend=owning_lane,
                kind="review",
                previous_lane=previous_lane,
            )
            _record_review_dispatch(
                dispatch_subject,
                status="awaiting-lane",
                reason=reason,
                backend=previous_lane,
                reviewed_head=fields["head"],
            )
            return {
                "run_id": run_id,
                "dispatched": False,
                "awaiting_lane": True,
                "backend": previous_lane,
                "reason": reason,
            }
        backend = candidates[0]
        on_local_lane = backend == local_lane

    node = TaskNode(
        id=fields["node_id"],
        goal=fields["goal"],
        plan=fields["plan"],
        section=fields["section"],
        brief=str(fields.get("brief") or ""),
        role="review",
        spec_level="exact",
        done_when=fields["done_when"],
        write_paths=list(fields["write_paths"]),
        time_budget=fields["time_budget"],
    )
    if dry_run:
        # The plan review's brief is composed rather than given, so a preview
        # holds its text while a dispatch writes it. The resolver reads the
        # brief to digest it, so the composed text is staged into a scratch
        # copy for that read and removed with the call: the preview reaches the
        # verdict a real dispatch reaches, the digest is over the bytes the
        # real brief would carry, and the report root is left untouched. The
        # staged path is never the path the preview reports.
        staged_brief: tempfile.TemporaryDirectory[str] | None = None
        if record.get("subject") == "plan" and fields.get("brief_text"):
            staged_brief = tempfile.TemporaryDirectory(prefix="plan-review-preview-")
            staged_path = Path(staged_brief.name) / "brief.md"
            staged_path.write_text(str(fields["brief_text"]), encoding="utf-8")
            node.brief = str(staged_path)
        try:
            resolution = dispatch_module.plan_dispatch(
                node=node,
                config=resolved,
                project=project,
                repo=repo,
                session=fields["session"],
                local=on_local_lane,
                backend_override=backend or None,
                route="deterministic",
            )
        finally:
            if staged_brief is not None:
                node.brief = str(fields.get("brief") or "")
                staged_brief.cleanup()
        return {
            "dispatched": False,
            "dry_run": True,
            "refused": not resolution.validation.ok,
            **resolution.as_dict(),
            "argv": _review_dispatch_tokens(
                fields,
                lane
                if record.get("subject") == "plan"
                else ["--local"]
                if on_local_lane
                else ["--backend", backend],
            ),
        }
    try:
        launched = dispatch_module.dispatch(
            node=node,
            project=project,
            repo=repo,
            config=resolved,
            session=fields["session"],
            launcher=launcher,
            watch_required=True,
            local=on_local_lane,
            backend_override=backend
            if record.get("subject") == "plan" or not on_local_lane
            else None,
            unreconciled_override=allow_unreconciled_runs,
            route="deterministic",
        )
    except BudgetHold as exc:
        reason = f"the {backend} lane is unavailable: {exc}"
        _record_review_dispatch(
            dispatch_subject,
            status="awaiting-lane",
            reason=reason,
            backend=backend,
            reviewed_head=fields["head"],
        )
        return {
            "run_id": run_id,
            "dispatched": False,
            "awaiting_lane": True,
            "backend": backend,
            "lane": getattr(exc, "verdict", None),
            "reason": reason,
        }
    except LanePaused as exc:
        # The lane's own gate says paused, or cannot be answered: the reflex
        # launches nothing and reports the gate it read. It is not a refusal —
        # the run is still owed a review, and the reflex re-fires once the gate
        # opens — so the report carries the lane-paused error and the reason
        # rather than reading as a run that no longer owes one.
        gate = dict(exc.gate)
        reason = (
            str(gate.get("detail") or "").strip()
            or str(gate.get("reason") or "").strip()
            or f"the {backend} lane gate is {gate.get('state')!r}"
        )
        _record_review_dispatch(
            dispatch_subject,
            status="lane-paused",
            reason=reason,
            backend=backend,
            reviewed_head=fields["head"],
        )
        return {
            "run_id": run_id,
            "dispatched": False,
            "error": "lane-paused",
            "backend": backend,
            "lane_gate": gate,
            "reason": reason,
        }
    except CrewError as exc:
        # Scope, member, follower, context-fit, plan visibility and competence
        # refusals all arrive here. The automatic path must not be the one place
        # they are skipped, so the refusal is recorded and reported rather than
        # caught and shrugged off.
        _record_review_dispatch(
            dispatch_subject,
            status="refused",
            reason=str(exc),
            backend=backend,
            reviewed_head=fields["head"],
        )
        return {
            "run_id": run_id,
            "dispatched": False,
            "refused": True,
            "backend": backend,
            "reason": str(exc),
        }

    review_run_id = str(launched.get("run_id") or "")
    _record_review_dispatch(
        dispatch_subject,
        status="dispatched",
        reason=f"the review dispatched automatically as run {review_run_id}",
        review_run_id=review_run_id,
        backend=backend,
        reviewed_head=fields["head"],
    )
    return {
        "run_id": run_id,
        "dispatched": True,
        "backend": backend,
        "review_run_id": review_run_id,
        "scope": fields.get("scope"),
        "review_tier": fields.get("review_tier"),
        "reason": f"dispatched the composed review as run {review_run_id}",
        **({"dispatch": launched} if record.get("subject") == "plan" else {}),
    }


def _is_repair_node(record: Mapping[str, Any]) -> bool:
    """Whether a run was minted by the repair composer.

    A repair carries the composer's own node-id prefix, so a review of it can be
    recognised without consulting the dispatch record. The chain this closes is
    the reason it exists: a finding on a repair opened another repair, up to a
    fourth round on one node, and each round inherited the parent's whole fence.
    """
    node = record.get("node") or {}
    return str(node.get("id") or "").startswith(repair_module.REPAIR_NODE_PREFIX)


def _repair_source_refusal(record: Mapping[str, Any]) -> str:
    """Why this run's findings open no repair, or empty when they may.

    Only an implement run is repaired. A review run and a repair run are both
    excluded on identity rather than on role, because their findings describe
    the record of a prior round rather than source a repair could edit; an
    investigate or a test run is excluded on its role, because its findings are
    about what it investigated or measured rather than about code it may change.
    """
    if _is_review_node(record) or _pointer_role(record) == REVIEW_ROLE:
        return "the run is itself a review, so it carries no repair round"
    if _is_repair_node(record):
        return "the run is itself a repair, so its findings open no further repair"
    role = _pointer_role(record)
    if role != REPAIR_SOURCE_ROLE:
        return (
            f"the run's role {role or '(unset)'} is not {REPAIR_SOURCE_ROLE}, "
            "so its findings open no repair"
        )
    return ""


def _repair_round_started_mtime_ns(
    record: Mapping[str, Any], round_id: str
) -> int | None:
    """The manifest baseline when this round's resume began, in nanoseconds.

    The round opens with the reviewed run's own resume write, which records the
    manifest's mtime as the attempt's baseline; the manifest's own mtime is
    compared against that baseline rather than against the second-resolution
    ``at`` stamp, because a manifest written just before the round can share the
    stamp's second and would then read as fresh. The pointer's own file mtime
    stands in when no baseline was recorded, the pointer having been written as
    the round opened. None when the record carries no outcome for this round, so
    a start belonging to another round is never used.
    """
    recorded = record.get(REPAIR_DISPATCH_FIELD)
    if not isinstance(recorded, Mapping):
        return None
    if str(recorded.get("round_id") or "") != str(round_id or ""):
        return None
    baseline = record.get("manifest_baseline_mtime_ns")
    if isinstance(baseline, int) and not isinstance(baseline, bool):
        return baseline
    run_id = str(record.get("run_id") or "")
    if not run_id:
        return None
    try:
        return runs.pointer_path(run_id).stat().st_mtime_ns
    except OSError:
        return None


def _manifest_reports_round(text: str, round_id: str) -> str | None:
    """Which round's advice a manifest quotes: ``this``, ``other``, or None.

    A refusal may quote the advice verbatim, in which case the round token the
    advice opens with is present, or it may paraphrase the blocker and quote
    nothing, which reads the same as a manifest that simply predates the round.
    Telling those apart is the caller's job; this answers only whether the
    manifest names this round's token, a different round's token, or no token at
    all, so a manifest quoting an earlier round's advice is never mistaken for
    this round's refusal however the surrounding text is worded.
    """
    marker = REPAIR_ROUND_TOKEN_LINE
    rid = str(round_id or "")
    seen_other = False
    index = 0
    while True:
        found = text.find(marker, index)
        if found == -1:
            return "other" if seen_other else None
        after = text[found + len(marker) :]
        if rid and after.startswith(rid):
            return "this"
        seen_other = True
        index = found + len(marker)


def _reviewed_run_refused_the_round(record: Mapping[str, Any], round_id: str) -> bool:
    """Whether the reviewed run's own manifest refused this round's advice.

    A resumed turn can end in two ways the busy guard cannot tell apart: it can
    die mid-work, leaving the run's manifest untouched, or it can read the
    round's advice, refuse the dead end it names, and write a terminal manifest.
    Only the second is a dead end — a retry would re-send byte-identical advice
    into the same refusal — so the retry is suppressed exactly when the manifest
    already answers this round. The signal is not the wording: a terminal
    manifest written after this round's resume began is a refusal whether or not
    it copies the advice's opening token. The token corroborates: a manifest
    quoting this round's token is a refusal, and one quoting a different round's
    is not evidence about this round at all. A manifest that cannot be read, one
    carrying no recognised terminal status, or one the run held before the round
    opened is not a refusal, so the guard degrades toward the retry rather than
    suppressing it. The caller reaches this only for a round whose reviewed head
    has not moved, the moved head having moved the round on, so the head-unmoved
    half of the signal is settled before the manifest is read.
    """
    path = str(record.get("manifest_path") or "")
    if not path:
        return False
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return False
    try:
        parsed = parse_manifest(text, path=path)
    except Exception:  # noqa: BLE001 - an unreadable manifest is not a refusal
        return False
    if not manifest_status_is_terminal(parsed.get("status")):
        return False
    reported = _manifest_reports_round(text, round_id)
    if reported == "other":
        return False
    if reported == "this":
        return True
    started_ns = _repair_round_started_mtime_ns(record, round_id)
    if started_ns is None:
        return False
    try:
        written_ns = Path(path).stat().st_mtime_ns
    except OSError:
        return False
    return written_ns > started_ns


def _reviewed_run_is_busy(record: Mapping[str, Any]) -> str:
    """Why the reviewed run's own worker still holds its finding, or empty.

    A repair is composed for work nobody is doing. A reviewed run whose worker
    is live is already working; one whose worker was resumed on the same finding
    is doing exactly the repair the reflex would compose, and dispatching a
    second node for it is what a peer measured as a repair withdrawn about
    fourteen seconds after dispatch and re-fired on every sweep. Liveness is
    read from the run's own record — the supervisor's pid and the run's own
    worker-record pid, both checked on this host — and the resumed turn from the
    run's newest stream, which is a resume file only once a resume has run and
    holds an assistant record only while that turn is producing work. A resumed
    turn has ended once its stream's last record is the result line a finished
    turn writes, so a finished resume no longer reads as a turn in progress and
    does not hold the round busy forever. A run recording no process and
    carrying no unfinished resumed turn is left free to be repaired.
    """
    if runs.record_process_alive(record, process_alive) is True:
        return "the reviewed run's worker is live"
    if _worker_record_liveness(record) is True:
        return "the reviewed run's worker is live"
    found = _record_newest_stream(record)
    if (
        found is not None
        and found[0].name.startswith("resume-")
        and _stream_holds_assistant_record(found[0])
        and _newest_stream_last_record_type(record) != STREAM_RESULT_RECORD_TYPE
    ):
        return "the reviewed run has a resumed turn in progress"
    return ""


_RECORD_PATH_PREFIXES = ("docs/evidence/", "docs/figures/")


def _within_fence(path: str, fence: Sequence[str] | None) -> bool:
    """Whether a path sits inside a fence the reviewed run was itself granted.

    A fence entry matches the path itself or the directory a finding's file lies
    under, so a finding naming ``docs/figures/x/run.json`` is inside a fence
    granting ``docs/figures/x``. A leading ``./`` is dropped so the two
    spellings of one path compare alike. An absent or empty fence grants nothing.
    """
    text = str(path or "").strip().removeprefix("./").rstrip("/")
    if not text:
        return False
    for granted in fence or ():
        entry = str(granted or "").strip().removeprefix("./").rstrip("/")
        if entry and (text == entry or text.startswith(entry + "/")):
            return True
    return False


def _is_record_path(path: str, *, fence: Sequence[str] | None = None) -> bool:
    """Whether a finding's path names the fleet's own record, not a source file.

    Run directories, manifests, gate logs and the review store all live outside
    the repository under review — an absolute path or a home-relative one is a
    record for that reason — and the evidence fragments and figures live inside
    it under their own subtrees. A path inside the reviewed run's own declared
    fence is the exception: that run was granted the subtree, so a finding under
    it is work however it is spelled and the record prefixes apply only outside
    the fence. Everything else is a repository source or test path a repair may
    be granted.
    """
    text = str(path or "").strip()
    if not text:
        return True
    if _within_fence(text, fence):
        return False
    if text.startswith("~") or Path(text).is_absolute():
        return True
    return text.startswith(_RECORD_PATH_PREFIXES)


def _repairable_scope(
    paths: Iterable[str], *, fence: Sequence[str] | None = None
) -> list[str]:
    """The repository source and test paths among a repair's finding paths.

    Duplicates collapse to their first occurrence and record paths are dropped,
    so the repair is never granted a record outside its run's fence — a run
    directory, a manifest, a gate log or a review-store path. A path inside the
    reviewed run's own fence is kept whatever its spelling, so a finding under
    the run's granted ``docs/figures/`` or ``docs/evidence/`` subtree is
    repairable rather than mistaken for the fleet's own record. Empty answers
    "every finding cites only run records or evidence documents", which the
    caller reads as no repair to dispatch. The population is the caller's: the
    decline decision passes the blocking findings' own cited paths, and the
    composed scope passes the node's write paths, so the two can never disagree
    about which paths are records.
    """
    scope: list[str] = []
    for path in paths:
        text = str(path or "").strip()
        if text and text not in scope and not _is_record_path(text, fence=fence):
            scope.append(text)
    return scope


def _review_carried_head(review: Mapping[str, Any]) -> str:
    """The head a stored review read, as its own record carries it.

    The round a repair belongs to is keyed on the head the *review* read, not on
    the reviewed run's current tree head: the composer re-reads the store by the
    round's head, so passing the tree head would select a different record — or
    none — the moment the reviewed run moves past the revision the review speaks
    for.
    """
    _, _, _, head = review_module.carried_revision_pair(review)
    return str(head or "").strip()


def _record_repair_dispatch(
    run_id: str,
    *,
    status: str,
    reason: str,
    round_id: str = "",
    repair_run_id: str = "",
    backend: str = "",
    node_id: str = "",
) -> int:
    """Write the reflex's repair outcome onto the reviewed run it acted for.

    A skip is recorded as loudly as a dispatch, for the same reason the review
    outcome is: a round that produced no repair is otherwise indistinguishable
    from one the reflex never considered. The round and the node it composed are
    recorded with the outcome, so the next sweep tells an attempt of this round
    from a later round the run has since moved to.

    The attempt count is read from the pointer at the moment of the write and
    returned, so a caller can record the value the run now carries rather than
    deriving its own. It counts every outcome written for the round, so a caller
    that caps how many times a round may be resumed reads it as that count. The
    count is keyed to ``round_id``: an outcome written for a round other than the
    one the pointer last carried starts the new round's count afresh, so a round
    the reflex has not yet written reads attempt 1 whatever an earlier round of
    the same run recorded, rather than continuing that round's count.

    A write under an opening status also advances the run-wide opened-round
    record, so a reader can tell a round that opened from one that was only ever
    refused. The count advances once per distinct round rather than once per
    attempt: the retry of a resumed turn writes the same round's id, so it moves
    the round's attempt figure and leaves the count where it was. A refusal does
    not touch the record at all, so the round in hand stays free to open.
    """
    if not run_id:
        return 0
    written: dict[str, int] = {}

    def record(pointer: dict[str, Any]) -> dict[str, Any]:
        prior = pointer.get(REPAIR_DISPATCH_FIELD)
        prior = prior if isinstance(prior, Mapping) else {}
        # The count belongs to the round, not to the run: a write for a round the
        # pointer did not last carry starts at one, so a run whose earlier round
        # recorded attempts does not make its next round begin part-way exhausted.
        same_round = str(prior.get("round_id") or "") == str(round_id or "")
        attempt = int(prior.get("attempt") or 0) + 1 if same_round else 1
        written["attempt"] = attempt
        if status in REPAIR_ROUND_OPENING_STATUSES and round_id:
            existing = pointer.get(REPAIR_ROUNDS_FIELD)
            opened = dict(existing) if isinstance(existing, Mapping) else {}
            if str(opened.get("round_id") or "") != round_id:
                opened["count"] = int(opened.get("count") or 0) + 1
                opened["round_id"] = round_id
            opened["attempts"] = attempt
            pointer[REPAIR_ROUNDS_FIELD] = opened
        pointer[REPAIR_DISPATCH_FIELD] = {
            "status": status,
            "reason": reason,
            "run_id": repair_run_id or None,
            "node_id": node_id or None,
            "round_id": round_id or None,
            "backend": backend or None,
            "at": _utc_now(),
            "attempt": attempt,
        }
        return pointer

    _mutate_pointer(run_id, record)
    return written.get("attempt", 0)


def _repair_resume_advice(
    composed: Mapping[str, Any], scope: Sequence[str], round_id: str
) -> str:
    """The advice a resume of the reviewed run carries for its composed round.

    The reviewed run's own worker already holds its worktree, its claim and the
    context the findings are about, so the repair reaches it as the composed
    brief — which names every blocking finding by id — together with the write
    scope the round's findings grant, the round's done-when and the negative
    control the composer declared. The findings are therefore answered by id in
    the reviewed run's own manifest, and no finding is left without an answer.

    The advice opens with the round id, so a worker's refusal that quotes the
    advice names the round it refused and a retry can tell it from a manifest
    quoting an earlier round's advice.
    """
    findings = list(composed.get("findings") or ())
    ids = ", ".join(str(finding.get("id") or "") for finding in findings)
    scope = [str(path) for path in scope]
    parts = [
        _repair_round_token(round_id),
        "",
        f"An independent review of this run found {len(findings)} blocking "
        f"finding(s) ({ids}). Answer each in this run.",
        "",
        str(composed.get("brief") or ""),
        "",
        REPAIR_ADVICE_SCOPE_LINE + (", ".join(scope) if scope else "none"),
        "",
        f"Done when: {composed.get('done_when') or ''}",
        f"Negative control: {composed.get('negative_control') or ''}",
    ]
    return "\n".join(parts).strip() + "\n"


def _promoted_run_ids(project: str) -> set[str]:
    """The run ids the project's ledger holds, or empty when it cannot be read.

    A ledger that cannot be read is not evidence a repair was promoted, so the
    refusal degrades to "unknown" and the round stays free to be attempted.
    """
    from reckon import ledger as ledger_module

    try:
        return ledger_module.run_ids(project)
    except Exception:  # noqa: BLE001 - an unreadable ledger is not a promotion
        return set()


def _opened_repair_rounds(record: Mapping[str, Any]) -> Mapping[str, Any]:
    """The run-wide record of repair rounds the reflex has opened, or empty.

    Read from the pointer afresh rather than from the entry-time mapping, because
    the sweep holds a pointer read before the round ran and a round opened on an
    earlier cadence must still be visible here. A pointer that has gone, or one
    that carries no record yet, answers empty, which leaves the round free to
    open — the same direction the other repair guards degrade in.
    """
    durable = read_pointer(str(record.get("run_id") or "")) or record
    opened = durable.get(REPAIR_ROUNDS_FIELD)
    return opened if isinstance(opened, Mapping) else {}


def _repair_in_flight(record: Mapping[str, Any], *, node_id: str, project: str) -> str:
    """The repair run already standing for this round, or empty.

    Recognition keys on the node id the composer minted, which is a pure
    function of the round — the run reviewed and the head read — so a redispatch
    of a failed attempt is recognised as the same node rather than composed as a
    second. A round stands while its repair's pointer is live; once that pointer
    is gone the run has either died or been promoted, and the ledger settles
    which: a promoted repair satisfies the round, while a repair that died with
    no ledger row leaves the round free to be attempted again.
    """
    recorded = record.get(REPAIR_DISPATCH_FIELD)
    if isinstance(recorded, Mapping) and str(recorded.get("node_id") or "") == node_id:
        standing = str(recorded.get("run_id") or "")
        if standing:
            if runs.pointer_path(standing).exists() and read_pointer(standing):
                return standing
            if project and standing in _promoted_run_ids(project):
                return standing
    for pointer in list_live(project=project or None):
        node = pointer.get("node") or {}
        if str(node.get("id") or "") == node_id:
            return str(pointer.get("run_id") or "")
    return ""


def _repair_launch_refusal(run_id: str, project: str) -> str:
    """Why the reviewed run must not be repaired at the moment of launch, or empty.

    The pointer is re-read immediately before the dispatch call, because the
    composition above takes time and a coordinator may promote the reviewed run
    in that window: four repairs were measured dispatched seconds before or
    after a promotion, and a repair holding a promoted run's manifest would
    rewrite a settled record. A pointer that has vanished is a run promotion has
    reconciled away or a run whose worker took it with it, and either way there
    is no live reviewed run to repair; a run the ledger already holds, or one
    whose re-read carries a promoted revision, is settled and skipped too.
    """
    fresh = read_pointer(run_id)
    if fresh is None:
        return "the reviewed run's live pointer is gone"
    if str(fresh.get("promoted_revision") or "").strip():
        return "the reviewed run is promoted"
    if project and run_id in _promoted_run_ids(project):
        return "the reviewed run is promoted"
    return ""


def _reviewed_run_suite_command(record: Mapping[str, Any]) -> str:
    """The suite command the repair inherits from the run it repairs.

    The repair's own review derives its added-failure count from the pair of
    suite observations its manifest carries, and it can only reconcile that pair
    against the reviewed run when both were measured with the same command. So
    the repair inherits the reviewed run's own recorded suite command, read from
    its pointer's ``suite_command``. A reviewed run that recorded none was
    unarmed, and its repair is unarmed too: the project's standing
    ``review.suite`` declaration is the project's own gate, not a measurement the
    reviewed run was ever taken with, so inheriting it would compare the repair's
    observation against one the reviewed run never made.
    """
    return str(record.get("suite_command") or "").strip()


def _config_carrying_suite(
    config: Mapping[str, Any], suite_command: str
) -> Mapping[str, Any]:
    """Return ``config`` carrying the given suite command in its gates block.

    A run's pointer records the suite command dispatch reads from
    ``config.gates.suite_command``, so a caller composing a run against another's
    recorded measurement supplies the inherited command here. The original
    mapping is left untouched, because the lane above already resolved against
    it and the caller owns it. An empty command returns the config unchanged, so
    an unarmed reviewed run's repair records no suite command rather than a blank
    that would overwrite the config's own value.
    """
    if not suite_command:
        return config
    gates = config.get("gates")
    merged = dict(config)
    merged["gates"] = {
        **(dict(gates) if isinstance(gates, Mapping) else {}),
        "suite_command": suite_command,
    }
    return merged


def dispatch_repair_for_run(
    record: Mapping[str, Any],
    *,
    config: Mapping[str, Any] | None = None,
    launcher: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Run the one repair a finding-bearing review round composes for itself.

    A stored review that carries findings is a review *round* — the run it read
    and the head it read — and its work is one node: three findings answered in
    one turn cost one dispatch, where three repairs cost three lanes and three
    merges against the same files. The composition itself lives in
    :mod:`reckon.crew.repair`, so this function and a coordinator reading the
    same record compose the same node rather than each deriving its own.

    The return value is the reflex's own report, not a command: ``dispatched``
    says a repair run is now in flight, ``node_id`` and ``round_id`` name what
    was composed, ``goal`` and ``write_paths`` carry the brief the repair was
    dispatched with, and ``reason`` explains a false. Nothing here is raised for
    an ordinary refusal: a scope, member, lane or budget refusal is *reported*
    and recorded against the run, because the caller is a sweep that must reach
    the rest of the fleet.

    Four guards keep the reflex off a run it must not touch, each measured
    against a live fleet failure: only an implement run's findings open a
    repair, and a repair's own findings open none; a run whose worker is live or
    resumed is left to its own worker; a round whose findings name only the
    reflex's own record — a manifest, a gate log, a review-store path — cites no
    repository path, so it composes no repair and is recorded decline-only; and
    the reviewed run's pointer is re-read immediately before the launch so a run
    promoted in that window is not repaired.

    An unpromoted reviewed run whose worker has exited is repaired in place: the
    composed findings ride a resume of the reviewed run itself, which already
    holds the worktree and the commit claim a new node could only be refused
    for. A promoted run, or one whose worker is live or whose live pointer has
    gone, takes the dispatch path and is settled by its own guards there. The
    resume is offered while the run's worker is not live, so a resumed turn that
    ends without answering is retried rather than leaving the round stuck, and
    the attempt count records each retry. The retry is bounded at
    REPAIR_RESUME_LIMIT resumes per round; a round resumed that many times
    without its head moving is recorded exhausted, so no sweep spends lane
    capacity on it again.
    """
    run_id = str(record.get("run_id") or "")
    refusal = _repair_source_refusal(record)
    if refusal:
        return {"run_id": run_id, "dispatched": False, "reason": refusal}
    project = str(record.get("project") or "")
    if not project:
        return {
            "run_id": run_id,
            "dispatched": False,
            "reason": "the run records no project to compose a repair against",
        }
    # A cheap presence check before the head resolution below costs a git
    # subprocess: a run with no review record on disk has no round to repair,
    # and the sweep reaches this for every live pointer on its cadence.
    _stored_path, stored = review_module.stored_record(project, run_id)
    if stored is None:
        return {
            "run_id": run_id,
            "dispatched": False,
            "reason": "no review is stored for this run",
        }
    busy = _reviewed_run_is_busy(record)
    if busy:
        return {"run_id": run_id, "dispatched": False, "reason": busy}
    review, review_error = _stored_review(record)
    if review is None or not _review_is_complete(review):
        return {
            "run_id": run_id,
            "dispatched": False,
            "review_status": "unreadable" if review_error else "present",
            "reason": (
                f"the stored review could not be read: {review_error}"
                if review_error
                else "the stored review does not cover the run's current head"
            ),
        }
    findings = repair_module.review_findings(review)
    if not findings:
        return {
            "run_id": run_id,
            "dispatched": False,
            "reason": "the stored review carries no finding, so composes no repair",
        }
    fields = _review_dispatch_fields(record)
    round_id = repair_module.repair_round_id(review, reviewed_run_id=run_id)
    # The reflex opens at most one automatic repair round per run. A round that
    # has already opened — a resume or a dispatch that actually started — settles
    # the run's automatic repair for good: a *later* head composes a different
    # round, and opening that one too is what a field run measured as the same
    # node resumed on reviews scoring 82, 83 and 83, each a new head drawing a
    # new review and a new round. The run is handed back to its coordinator
    # instead, recorded with the earlier round, its attempts and the new review's
    # score so a reader sees why no repair fired. A round already recorded as
    # opened is the round in hand, so its own retry (below) is untouched, and a
    # round that was only ever refused never entered the record at all, leaving
    # it free to open.
    opened_rounds = _opened_repair_rounds(record)
    opened_count = int(opened_rounds.get("count") or 0)
    opened_round_id = str(opened_rounds.get("round_id") or "")
    if opened_count and opened_round_id and opened_round_id != round_id:
        earlier_attempts = int(opened_rounds.get("attempts") or 0)
        score = review.get("total")
        reason = (
            f"a repair round was already opened for this run (round "
            f"{opened_round_id}, {earlier_attempts} attempt(s)); this review "
            f"scores {score}, so the run is handed back to its coordinator"
        )
        _record_repair_dispatch(
            run_id,
            status="handed-to-coordinator",
            reason=reason,
            round_id=round_id,
        )
        return {
            "run_id": run_id,
            "dispatched": False,
            "handed_to_coordinator": True,
            "round_id": round_id,
            "reason": reason,
        }
    repo = str(record.get("repo") or "")
    if not repo:
        reason = "the run records no repository to dispatch a repair against"
        _record_repair_dispatch(
            run_id, status="refused", reason=reason, round_id=round_id
        )
        return {"run_id": run_id, "dispatched": False, "reason": reason}

    # The head the review read. The repair is cut from it so the reviewed head
    # is an ancestor of the repair's own tree: a repair dispatched at the branch
    # tip would answer findings against a revision the review never saw, and the
    # round's identity — the run at the head it read — would name a tree the
    # repair never stood on. A promoted run never reaches here (the launch
    # refusal above settles it), so this is the unpromoted case by construction.
    reviewed_head = _review_carried_head(review)
    # The repair inherits the reviewed run's own recorded suite command, so its
    # review measures added failures against the same suite the reviewed run was
    # measured with. An unarmed reviewed run yields no command, and the repair is
    # unarmed like the run it repairs.
    inherited_suite = _reviewed_run_suite_command(record)
    composed = repair_module.compose_repair_for_run(
        project,
        run_id,
        reviewed_head_sha=reviewed_head or None,
        source_node=fields["source_node"],
        plan=str(fields["plan"]),
        section=str(fields["section"]),
        session=str(fields["session"]),
        time_budget=str(fields["time_budget"]),
        # The reviewed run's own fence is laid into the scope: its test paths
        # are the repair's gate, and a finding under the run's granted
        # ``docs/figures/`` or ``docs/evidence/`` subtree is work the run
        # already held rather than the fleet's own record.
        run_record=record,
        suite_command=inherited_suite,
    )
    if composed is None:
        # The composer returns None for a finding-bearing record in two cases,
        # told apart from the findings already read here so the recorded reason
        # names the follow-on count rather than one string standing for both. A
        # round whose findings are all follow-ons has nothing that blocks to
        # answer, so it composes no repair. A finding with no readable severity
        # blocks, so such a round carries no unmarked finding to count; the
        # unmarked list below stays for the record and is empty here, and any it
        # did name would be listed by file and line so the record names what was
        # reported rather than repaired. The record is left on the reviewed run
        # as the round's outcome. Otherwise the record the composer re-read
        # differs from the one selected here, and the round is left for a
        # coordinator rather than dispatched from a stale parse.
        if not repair_module.blocking_findings(review):
            follow_ons = repair_module.follow_on_findings(review)
            unmarked = repair_module.unmarked_findings(review)
            reason = (
                "the review round carried no blocking finding "
                f"({len(follow_ons)} follow-on finding(s))"
            )
            _record_repair_dispatch(
                run_id,
                status="decline-only",
                reason=reason,
                round_id=round_id,
            )
            return {
                "run_id": run_id,
                "dispatched": False,
                "reason": reason,
                "unmarked_findings": [
                    {"file": finding["file"], "line": finding["line"]}
                    for finding in unmarked
                ],
            }
        return {
            "run_id": run_id,
            "dispatched": False,
            "reason": "no repair composes for this review round",
        }
    node_id = str(composed["node_id"])
    round_id = str(composed["round_id"])
    run_fence = repair_module._run_fence(record)
    blocking = repair_module.blocking_findings(review)
    # The round is decline-only when no *blocking finding cites a repairable
    # path. The decision must read the blocking findings, the same population the
    # scope is composed from: reading every finding let a follow-on citing a
    # source path carry a round through whose blocking findings cited only the
    # fleet's own record, so the scope came out empty. The composed scope below
    # also carries the reviewed run's whole fence, granted so the repair can run
    # the reviewed gate, so a decision read from that scope is never empty for a
    # run holding a test path; the cited paths are the only place the findings'
    # own files appear, and they are read here. A path inside the run's own fence
    # is repairable whatever its spelling, so an in-fence figure or evidence
    # finding is not mistaken for the fleet's own record.
    if not _repairable_scope(
        (str(finding.get("file") or "") for finding in blocking), fence=run_fence
    ):
        reason = "no finding cites a repository path, so the round is decline-only"
        _record_repair_dispatch(
            run_id,
            status="decline-only",
            reason=reason,
            round_id=round_id,
            node_id=node_id,
        )
        return {
            "run_id": run_id,
            "dispatched": False,
            "node_id": node_id,
            "round_id": round_id,
            "reason": reason,
        }
    scope = _repairable_scope(composed["write_paths"], fence=run_fence)
    # A composed scope that filters to empty while blocking findings exist is a
    # dead end: the round would otherwise resume with the advice line "Write
    # scope for this round: none", which is no scope for work the review's
    # blocking findings named. Record it declined instead of resuming it.
    if not scope:
        reason = (
            "the composed write scope filtered to empty, so the round is decline-only"
        )
        _record_repair_dispatch(
            run_id,
            status="decline-only",
            reason=reason,
            round_id=round_id,
            node_id=node_id,
        )
        return {
            "run_id": run_id,
            "dispatched": False,
            "node_id": node_id,
            "round_id": round_id,
            "reason": reason,
            "blocking_findings": len(blocking),
        }
    standing = _repair_in_flight(record, node_id=node_id, project=project)
    if standing:
        return {
            "run_id": run_id,
            "dispatched": False,
            "reason": "a repair for this review round is already standing",
            "repair_run_id": standing,
            "node_id": node_id,
            "round_id": round_id,
        }

    resolved = _resolved_review_config(project, config)
    try:
        from reckon import flight

        resolved = flight.select_local_backend(resolved)
    except Exception as exc:  # noqa: BLE001 - the configured lane is the reason
        reason = f"the local lane is unavailable: {exc}"
        _record_repair_dispatch(
            run_id,
            status="awaiting-lane",
            reason=reason,
            round_id=round_id,
            node_id=node_id,
        )
        return {
            "run_id": run_id,
            "dispatched": False,
            "awaiting_lane": True,
            "reason": reason,
        }

    local_lane = str(resolved.get("local_backend") or "").strip()
    owning_lane = str(record.get("backend") or "").strip()
    candidates = _review_lane_candidates(resolved, owning_backend=owning_lane)
    if not candidates:
        reason = _no_lane_reason(
            run_id, resolved, owning_backend=owning_lane, kind="repair"
        )
        _record_repair_dispatch(
            run_id,
            status="awaiting-lane",
            reason=reason,
            round_id=round_id,
            node_id=node_id,
        )
        return {
            "run_id": run_id,
            "dispatched": False,
            "awaiting_lane": True,
            "reason": reason,
        }
    backend = candidates[0]
    on_local_lane = backend == local_lane

    # An unpromoted reviewed run whose worker has exited still holds its own
    # worktree and its commit claim, so a repair node over the reviewed run's own
    # paths is refused at dispatch — measured on every sweep as a repair
    # withdrawn about fourteen seconds after it was dispatched and re-fired on
    # each cadence. The repair for such a run is the reviewed run itself: the
    # composed findings ride a resume of the run that already owns the worktree
    # and the claim, through the same entry point a hand-typed ``crew resume``
    # uses, and no second node is composed. A run whose worker is live or resumed
    # never reaches here (the busy guard above), and a promoted run or one whose
    # pointer has gone keeps the dispatch path below, which its launch refusal
    # settles.
    #
    # The round is in flight only while that worker is live: the busy guard above
    # returns for a live worker, so reaching this point means the resumed turn has
    # ended. If it ended without answering — the reviewed run's head has not moved
    # past the reviewed head, since a moved head no longer matches the stored
    # review — the round is resumed once more, and the attempt count recorded on
    # the run makes the retry visible. The retry is bounded: a round is resumed
    # at most REPAIR_RESUME_LIMIT times, the first plus the one retry, so a turn
    # that keeps ending without answering is exhausted rather than spending lane
    # capacity on every sweep forever. The count is read from the pointer's
    # durable record, so an entry-time mapping cannot stale-hold it.
    if not _repair_launch_refusal(run_id, project):
        from reckon.crew import resumption as resumption_module
        from reckon.crew.dispatch import BudgetHold, LanePaused

        durable = read_pointer(run_id) or {}
        recorded = durable.get(REPAIR_DISPATCH_FIELD)
        same_round = isinstance(recorded, Mapping) and str(
            recorded.get("round_id") or ""
        ) == str(round_id or "")
        prior = int((recorded or {}).get("attempt") or 0) if same_round else 0
        if (
            same_round
            and prior >= 1
            and _reviewed_run_refused_the_round(durable or record, round_id)
        ):
            reason = (
                "the ended turn refused this round's advice; a retry would "
                "re-send it into the same dead end"
            )
            attempt = _record_repair_dispatch(
                run_id,
                status="exhausted",
                reason=reason,
                round_id=round_id,
                node_id=node_id,
            )
            return {
                "run_id": run_id,
                "dispatched": False,
                "exhausted": True,
                "node_id": node_id,
                "round_id": round_id,
                "attempt": attempt,
                "reason": reason,
            }
        if prior >= REPAIR_RESUME_LIMIT:
            reason = "the round was resumed twice without answering its findings"
            if same_round and str(recorded.get("status") or "") == "exhausted":
                return {
                    "run_id": run_id,
                    "dispatched": False,
                    "exhausted": True,
                    "node_id": node_id,
                    "round_id": round_id,
                    "attempt": prior,
                    "reason": reason,
                }
            attempt = _record_repair_dispatch(
                run_id,
                status="exhausted",
                reason=reason,
                round_id=round_id,
                node_id=node_id,
            )
            return {
                "run_id": run_id,
                "dispatched": False,
                "exhausted": True,
                "node_id": node_id,
                "round_id": round_id,
                "attempt": attempt,
                "reason": reason,
            }

        advice = _repair_resume_advice(composed, scope, round_id)
        try:
            resumed = resumption_module._resume(
                run_id, record, config=config, launcher=launcher, advice=advice
            )
        except LanePaused as exc:
            gate = dict(exc.gate)
            reason = (
                str(gate.get("detail") or "").strip()
                or str(gate.get("reason") or "").strip()
                or f"the {backend} lane gate is {gate.get('state')!r}"
            )
            _record_repair_dispatch(
                run_id,
                status="lane-paused",
                reason=reason,
                round_id=round_id,
                node_id=node_id,
                backend=backend,
            )
            return {
                "run_id": run_id,
                "dispatched": False,
                "error": "lane-paused",
                "backend": backend,
                "lane_gate": gate,
                "reason": reason,
            }
        except (BudgetHold, CrewError, OSError) as exc:
            reason = f"the reviewed run could not be resumed: {exc}"
            _record_repair_dispatch(
                run_id,
                status="refused",
                reason=reason,
                round_id=round_id,
                node_id=node_id,
            )
            return {
                "run_id": run_id,
                "dispatched": False,
                "refused": True,
                "reason": reason,
            }
        attempt = _record_repair_dispatch(
            run_id,
            status="resumed",
            reason="the reviewed run was resumed with the composed findings as advice",
            round_id=round_id,
            node_id=node_id,
        )
        return {
            "run_id": run_id,
            "dispatched": False,
            "resumed": True,
            "node_id": node_id,
            "round_id": round_id,
            "attempt": attempt,
            "reason": "resumed the reviewed run with the composed findings as advice",
            "resumed_turn": resumed.get("turn"),
        }

    dispatch_module = importlib.import_module("reckon.crew.dispatch")
    from reckon.crew.dispatch import BudgetHold, LanePaused
    from reckon.crew.node import TaskNode

    node = TaskNode(
        id=node_id,
        goal=str(composed["goal"]),
        plan=str(composed["plan"]),
        section=str(composed["section"]),
        role=str(composed["role"]),
        spec_level=str(composed["spec_level"]),
        done_when=str(composed["done_when"]),
        write_paths=list(scope),
        time_budget=str(composed["time_budget"]),
        negative_control=str(composed.get("negative_control") or ""),
    )
    # The re-read is the last thing before the launch: the composition and the
    # lane resolution above take time, and a coordinator may promote the
    # reviewed run in that window. A run promoted here is settled, and a repair
    # composed against it would rewrite a record its owner has landed.
    launch_refusal = _repair_launch_refusal(run_id, project)
    if launch_refusal:
        _record_repair_dispatch(
            run_id,
            status="refused",
            reason=launch_refusal,
            round_id=round_id,
            node_id=node_id,
            backend=backend,
        )
        return {
            "run_id": run_id,
            "dispatched": False,
            "refused": True,
            "reason": launch_refusal,
        }
    # The repair carries the reviewed run's suite command so its own review,
    # reading the pair of suite observations the repair's manifest records,
    # measures added failures against the same suite the reviewed run was
    # measured with. dispatch stamps the command it records on a run's pointer
    # from ``config.gates.suite_command``, so the inherited command rides the
    # config handed to the launch rather than editing the resolved config the
    # lane above already read.
    repair_config = _config_carrying_suite(resolved, inherited_suite)
    try:
        launched = dispatch_module.dispatch(
            node=node,
            project=project,
            repo=repo,
            config=repair_config,
            session=str(composed["session"]),
            # Cut the repair's worktree at the head the review read, so the
            # reviewed head is an ancestor of the repair's tree. The reviewed
            # run is unpromoted here — the launch refusal above settles any
            # promoted one — so the base is always the reviewed head.
            base=reviewed_head or "HEAD",
            launcher=launcher,
            watch_required=True,
            local=on_local_lane,
            backend_override=None if on_local_lane else backend,
            unreconciled_override=True,
        )
    except BudgetHold as exc:
        reason = f"the {backend} lane is unavailable: {exc}"
        _record_repair_dispatch(
            run_id,
            status="awaiting-lane",
            reason=reason,
            round_id=round_id,
            node_id=node_id,
            backend=backend,
        )
        return {
            "run_id": run_id,
            "dispatched": False,
            "awaiting_lane": True,
            "backend": backend,
            "lane": getattr(exc, "verdict", None),
            "reason": reason,
        }
    except LanePaused as exc:
        # The lane gate holds the repair exactly as it holds any other dispatch.
        # The round has not opened, so a later sweep composes the same repair
        # once the gate opens rather than reading this as a refusal of it.
        gate = dict(exc.gate)
        reason = (
            str(gate.get("detail") or "").strip()
            or str(gate.get("reason") or "").strip()
            or f"the {backend} lane gate is {gate.get('state')!r}"
        )
        _record_repair_dispatch(
            run_id,
            status="lane-paused",
            reason=reason,
            round_id=round_id,
            node_id=node_id,
            backend=backend,
        )
        return {
            "run_id": run_id,
            "dispatched": False,
            "error": "lane-paused",
            "backend": backend,
            "lane_gate": gate,
            "reason": reason,
        }
    except CrewError as exc:
        # Scope, member, follower, context-fit, plan visibility and competence
        # refusals all arrive here. The automatic path must not be the one place
        # they are skipped, so the refusal is recorded and reported rather than
        # caught and shrugged off.
        _record_repair_dispatch(
            run_id,
            status="refused",
            reason=str(exc),
            round_id=round_id,
            node_id=node_id,
            backend=backend,
        )
        return {
            "run_id": run_id,
            "dispatched": False,
            "refused": True,
            "backend": backend,
            "reason": str(exc),
        }

    repair_run_id = str(launched.get("run_id") or "")
    _record_repair_dispatch(
        run_id,
        status="dispatched",
        reason=f"the repair dispatched automatically as run {repair_run_id}",
        round_id=round_id,
        node_id=node_id,
        repair_run_id=repair_run_id,
        backend=backend,
    )
    return {
        "run_id": run_id,
        "dispatched": True,
        "backend": backend,
        "repair_run_id": repair_run_id,
        "node_id": node_id,
        "round_id": round_id,
        "goal": str(composed["goal"]),
        "write_paths": list(scope),
        "reason": f"dispatched the composed repair as run {repair_run_id}",
    }


def _sweeping_session(project: str | None) -> str:
    """The session whose follower this process serves, or empty.

    A sweep runs inside one session's follower, and the review it composes for
    a run belongs to that run's owning session: composing one for another
    session's run attributes a lane and a member to a coordinator that did not
    choose either, which is a project-wide sweep placing runs under someone
    else's runtime. The follower's registration names the session and the
    process that wrote it, so a registration written by this process is the
    identity. A process holding no registration — a hand-run sweep — has no
    session to confine to and returns empty, which the caller reads as no
    filter.
    """
    if not project:
        return ""
    for row in runs.list_followers(project):
        follower = row.get("follower") or {}
        if follower.get("pid") == os.getpid():
            return str(row.get("session") or "")
    return ""


def _sweep_review_tier(
    record: Mapping[str, Any], manifest_commits: Sequence[Any]
) -> str:
    """The review tier a scoring run resolves to, through promotion's resolver.

    The reflex must skip exactly the runs promotion's own gate would land
    without a review, so it reads the tier through the same
    :mod:`reckon.review_tiers` resolver promotion calls, on the same inputs —
    changed paths, changed lines, declared spec level and declared capability
    risk. Re-deriving the rule here is how a sweep and a promotion come to
    disagree about which run owes a review, and the disagreement is silent in
    both directions. A scope that cannot be measured is read as the fuller
    tier, so a run whose change is unreadable is still reviewed rather than
    skipped.
    """
    from reckon.crew import promotion

    commits = _canonical_commits(_review_tree(record), manifest_commits)
    try:
        return promotion._run_review_tier(
            str(record.get("run_id") or ""),
            record,
            commit_list=commits,
            root=str(record.get("repo") or "") or None,
        )
    except Exception:  # noqa: BLE001 - an unreadable scope owes the fuller review
        return review_tiers.FULL


# The score a complete review must reach for its acceptance to promote
# the reviewed run without a coordinator. The total a review can score is the
# schema dimensions at ``REVIEW_MAX_SCORE`` each, and the floor is nine tenths of
# that, so the accepting branch over a stored record whose total merely parses
# is what this exists to prevent.
REVIEW_ACCEPTANCE_FLOOR = (
    len(review_module.REVIEW_DIMENSIONS) * review_module.REVIEW_MAX_SCORE * 9 // 10
)


def _review_accepts_promotion(review: Mapping[str, Any] | None) -> bool:
    """Whether a stored review is clean enough to promote the run it read.

    Clean means complete — every dimension scored and a total parsed — with a
    total at or above the floor and no finding at all. Findings are read from
    the record rather than from the promotion-time severity filter: a review
    that carries any finding returns to the coordinator, so one finding of any
    severity is enough to withhold acceptance.
    """
    if not _review_is_complete(review):
        return False
    total = review.get("total")
    if isinstance(total, bool) or not isinstance(total, (int, float)):
        return False
    if int(total) < REVIEW_ACCEPTANCE_FLOOR:
        return False
    findings = review.get("findings")
    return not (isinstance(findings, Sequence) and len(findings))


def _recorded_gate_passed(
    record: Mapping[str, Any],
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """The run's recorded gate check when it passed and its named commits.

    The gate a run records is its own manifest's evidence — the command under
    ``tests``, the log under ``test_logs`` and the ``EXIT=`` line the log
    carries — read through the same resolver a promotion fills its evidence
    from. A command with no exit status of zero, or no command at all, is not a
    passing gate and acceptance stands down for it. The resolved check and the
    commits the manifest names are returned together, so the caller can both
    re-run the same command at the merged head and require those commits to be
    on the branch that head names.
    """
    from reckon.crew import promotion as promotion_module

    gate_check, commits = promotion_module._default_gate_evidence_from_manifest(
        record, verdict="passed", gate_check=None, commits=()
    )
    if not str(gate_check.get("command") or "").strip():
        return None, ()
    if gate_check.get("exit_status") != 0:
        return None, ()
    return gate_check, tuple(commits)


def _commits_missing_from(repository: Path, commits: Sequence[str]) -> list[str]:
    """The named commits that are not ancestors of the repository's HEAD.

    A run's work is only landable once a coordinator has merged it onto the
    branch the checkout carries; a run reaching acceptance straight after its
    review has not been merged, so its commits are absent from the primary
    branch and a gate re-run here would verify a tree the change is not in.
    ``git merge-base --is-ancestor`` answers with status 0 for an ancestor, 1
    for a commit present but not an ancestor, and any other status for an
    instrument fault — an unresolvable object, a checkout that is not a work
    tree, git itself failing. Status 1 is the only one read as "not merged";
    every other failure raises, because an instrument that cannot answer must
    not be reported as the answer "not an ancestor".
    """
    missing: list[str] = []
    for commit in commits:
        sha = str(commit).strip()
        if not sha:
            continue
        probe = subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "merge-base",
                "--is-ancestor",
                sha,
                "HEAD",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.returncode == 0:
            continue
        if probe.returncode == 1:
            missing.append(sha)
            continue
        raise CrewError(
            f"git could not decide whether {sha} is an ancestor of HEAD in "
            f"{repository}: exit {probe.returncode}: "
            f"{(probe.stderr or probe.stdout).strip()}"
        )
    return missing


def accept_clean_review(
    pointer: Mapping[str, Any],
    *,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Promote the reviewed run when its stored review is clean.

    When a review run completes with a stored record whose complete total
    is at least the acceptance floor and which carries no finding, the reviewed
    run is promoted without a coordinator: the commits its manifest names must
    already be ancestors of the repository's head, the run's own recorded gate
    must already have passed, the gate is re-run against the repository's
    current head with the run merged, and on all of those passing the run is
    promoted with ``promoted_by: review-acceptance`` on its ledger row.
    Anything short of that returns to the coordinator exactly as
    before, so this is the accepting branch beside the refusal that stops a
    promotion without a clean review rather than a bypass of it. Acceptance
    never merges: an unmerged run stands down and names the commits it found
    off the branch.

    None is returned when the run is not acceptance-eligible at all — no stored
    review, or a review that is incomplete, below the floor, or carrying a
    finding — because those runs are the coordinator's and this handler has
    nothing to add. A dict is returned whenever the run was eligible and an
    outcome was reached, so the sweep records why a refusal did not promote.
    """
    run_id = str(pointer.get("run_id") or "")
    project = str(pointer.get("project") or "")
    if not run_id or not project:
        return None
    head, tree = _review_head_and_tree(pointer)
    try:
        review, _reviewed_head = select_review_for_head(
            project, run_id, head, tree=tree
        )
    except (OSError, ValueError):
        review = None
    if not _review_accepts_promotion(review):
        return None
    checkout_value = str(pointer.get("repo") or "").strip()
    checkout = Path(checkout_value).expanduser() if checkout_value else None
    if checkout is None or not checkout.is_dir():
        return {
            "run_id": run_id,
            "accepted": False,
            "reason": "the run names no repository to verify the merged gate against",
        }
    gate_check, commits = _recorded_gate_passed(pointer)
    if gate_check is None:
        return {
            "run_id": run_id,
            "accepted": False,
            "reason": "the run records no passing gate to re-run at the merged head",
        }
    # The run's work must already be on the branch this checkout carries. A run
    # reaching acceptance straight after its review has not been merged, so a
    # gate re-run here would verify a tree the change is not in and record a
    # promotion whose commits never landed. Only a coordinator merges; this
    # handler stands down and says which commits are missing.
    missing = _commits_missing_from(checkout, commits)
    if missing:
        return {
            "run_id": run_id,
            "accepted": False,
            "reason": (
                "the run's commits are not on the primary branch yet; a "
                "coordinator merges it: " + ", ".join(sha[:12] for sha in missing)
            ),
            "unmerged_commits": list(missing),
        }
    from reckon.crew import promotion as promotion_module

    worktree = str(pointer.get("worktree") or "").strip()
    report = promotion_module.rerun_gate_at_integrated_revision(
        repository=checkout,
        gate_check=gate_check,
        base_verdict="passed",
        integrated_revision="HEAD",
        worktree_roots=(worktree,) if worktree else (),
    )
    if str(report.get("integrated_verdict")) != "passed":
        return {
            "run_id": run_id,
            "accepted": False,
            "reason": str(
                report.get("finding")
                or report.get("reason")
                or "the integrated gate did not pass at the merged head"
            ),
            "gate_report": report,
        }
    try:
        result = promotion_module.complete(
            run_id,
            gate="passed",
            gate_check=gate_check,
            root=checkout,
            promoted_by="review-acceptance",
        )
    except (CrewError, OSError) as refusal:
        return {
            "run_id": run_id,
            "accepted": False,
            "reason": str(refusal),
            "gate_report": report,
        }
    return {
        "run_id": run_id,
        "accepted": True,
        "promoted": True,
        "review": {
            "total": review.get("total") if review else None,
            "findings": 0,
        },
        "gate_report": report,
        "ledger": {
            "path": str(result.get("ledger_path") or ""),
            "promoted_by": (
                str((result.get("record") or {}).get("promoted_by") or "")
                if isinstance(result.get("record"), Mapping)
                else "review-acceptance"
            ),
        },
    }


def dispatch_awaiting_reviews(
    *,
    project: str | None = None,
    config: Mapping[str, Any] | None = None,
    launcher: Callable[..., Any] | None = None,
    session: str | None = None,
) -> dict[str, Any]:
    """Dispatch the review every scoring run composes, and report each outcome.

    This is the reflex's entry point: called on a sweep, it is what makes a run
    entering scoring dispatch its own review without anyone issuing the
    ``reckon crew dispatch`` a coordinator would otherwise have to retype. A
    run whose review is already stored or already in flight is left alone, so
    the sweep is idempotent and the negative half of the property holds — a
    reflex that re-fires would manufacture runs rather than reviews.

    The same sweep carries the accepting branch: a promotable run whose stored
    review is clean and whose recorded gate still passes at the merged head is
    promoted here, with no coordinator command in between. A run that is not
    acceptance-eligible is left for its coordinator exactly as before.

    ``session`` names the sweeping session, and only runs whose pointer records
    that ownership: its lane and its member belong to the coordinator that
    chose them. Left unset it is resolved from the follower registration this
    process holds, so a sweep inside a follower is confined to its own session
    without the caller having to declare it.
    """
    reports: list[dict[str, Any]] = []
    dispatched: list[str] = []
    refused: list[dict[str, Any]] = []
    accepted: list[str] = []
    awaiting_lane: list[str] = []
    lane_paused: list[str] = []
    repaired: list[str] = []
    sweeping = session if session is not None else _sweeping_session(project)
    for pointer in list_live(project=project):
        if (
            str(pointer.get("project") or "")
            and project
            and str(pointer.get("project")) != project
        ):
            continue
        if sweeping and str(pointer.get("session") or "") != sweeping:
            # Another session's run: its review is that session's to compose,
            # because only that coordinator chose its lane and its member.
            continue
        # A review run is never its own source run: counting one as a run
        # awaiting review is what composes a review of a review, and each link
        # of that chain is a real dispatch against a real member.
        if _is_review_node(pointer):
            continue
        # A run resumed since its queued review was recorded carries a head the
        # review no longer speaks for; the queued attempt is withdrawn rather
        # than left standing against a revision the run has moved past.
        withdraw_superseded_review(pointer)
        scan: dict[str, Any] | None = None
        try:
            scan = classify_pointer(pointer)
        except Exception:  # noqa: BLE001 - one unreadable run must not stop the sweep
            scan = None
        if scan is not None and scan["classification"] == "scoring":
            # A run whose tier is none changes no runtime source, so promotion
            # lands it with the review gate standing down and records the tier
            # as the reason no review exists. Composing a review for it anyway
            # spends a member and a lane on a diff no reviewer is owed, so the
            # tier is resolved through promotion's own resolver and the skip is
            # recorded here rather than left as a silent absence.
            run_id = str(pointer.get("run_id") or "")
            remaining = review_settle_seconds_remaining(pointer)
            if remaining > 0:
                # A run resumed within the settle window is still moving toward
                # the head it will be reviewed at; composing now would read a
                # revision the run is about to leave, so the sweep waits.
                reason = (
                    "the run was resumed less than five minutes ago, so its "
                    "head has not settled; the reflex waits before reviewing it"
                )
                _record_review_dispatch(run_id, status="settling", reason=reason)
                reports.append(
                    {
                        "run_id": run_id,
                        "dispatched": False,
                        "settling_seconds": int(remaining),
                        "reason": reason,
                    }
                )
                continue
            tier = _sweep_review_tier(pointer, scan.get("manifest_commits") or [])
            if tier == review_tiers.NONE:
                reason = (
                    "the run changes no runtime source, so its review tier is "
                    "none; no review is dispatched and the merged-head gate "
                    "checks it instead"
                )
                _record_review_dispatch(run_id, status="skipped", reason=reason)
                reports.append(
                    {
                        "run_id": run_id,
                        "dispatched": False,
                        "review_tier": review_tiers.NONE,
                        "reason": reason,
                    }
                )
            else:
                report = dispatch_review_for_run(
                    pointer, config=config, launcher=launcher
                )
                reports.append(report)
                if report.get("dispatched"):
                    dispatched.append(str(report.get("review_run_id") or ""))
                elif report.get("awaiting_lane"):
                    awaiting_lane.append(str(report.get("run_id") or ""))
                elif report.get("error") == "lane-paused":
                    lane_paused.append(str(report.get("run_id") or ""))
                elif report.get("refused"):
                    # The report itself, not a generator over it: a refusal list
                    # is read and serialized by whoever consumes the sweep, and a
                    # generator is neither readable nor JSON-serializable, so the
                    # refusal would be lost at exactly the moment a reader needs
                    # to know which lane was refused.
                    refused.append(report)
        # The accepting branch: a promotable run whose stored review is clean is
        # promoted here, without a coordinator command, once its own recorded
        # gate has passed and the gate still passes at the repository's merged
        # head. A run that is not acceptance-eligible returns None and is left
        # to its coordinator, and an eligible run whose acceptance was refused
        # is reported with its reason rather than silently skipped.
        if scan is not None and scan["classification"] == "promotable":
            acceptance = accept_clean_review(pointer, config=config)
            if acceptance is not None:
                reports.append(acceptance)
                if acceptance.get("accepted"):
                    accepted.append(str(acceptance.get("run_id") or ""))
        # The repair pass, keyed on the stored review rather than on the run's
        # classification: a review carrying findings may leave the run reading
        # promotable, so gating this on ``scoring`` alone would leave a
        # finding-bearing round unrepaired. Its guards — role, identity,
        # liveness, record-only findings, and a pointer re-read at launch —
        # leave every run they do not apply to untouched, so sweeping the rest
        # of the fleet is unaffected.
        repair_report = dispatch_repair_for_run(
            pointer, config=config, launcher=launcher
        )
        if repair_report.get("dispatched"):
            reports.append(repair_report)
            repaired.append(str(repair_report.get("repair_run_id") or ""))
    return {
        "reports": reports,
        "dispatched": dispatched,
        "refused": refused,
        "accepted": accepted,
        "awaiting_lane": awaiting_lane,
        "lane_paused": lane_paused,
        "repaired": repaired,
    }


SELF_LIFTING_RECOVERY_CLASSIFICATIONS = frozenset({"waiting", "paused"})
DEFAULT_LIFTING_CONDITIONS = {
    "waiting": "the declared condition reaches one of its terminal states",
    "paused": "the condition named by the row ends",
}


def manifest_status_is_terminal(value: Any) -> bool:
    """Whether a worker supplied one exact terminal status value."""
    status = str(value or "").strip().lower()
    return not manifest_status_is_template(status) and (
        status in TERMINAL_MANIFEST_STATUSES
    )


def _stream_completion_stamp(record: Mapping[str, Any]) -> str | None:
    """The run's own finish stamp as its recorded stream dates it, else None.

    Promotion writes this same stream completion stamp to the ledger row, so
    the elapsed measure and the promoted record agree on when a run ended
    rather than each keeping its own idea of the finish. None for an in-harness
    run or a stream that was never written; the caller only resolves it once
    liveness says the process is gone, so a still-writing stream is never
    mistaken for a completion.
    """
    if record.get("launch") != "cli":
        return None
    from reckon.crew.promotion import _terminal_stream_data

    return _terminal_stream_data(record).completed_at


def _declared_token_budget(record: Mapping[str, Any]) -> int | None:
    """The run's token-denominated allowance, or None when none is set.

    The budget lives on the node block as dispatch resolves and records it;
    a top-level mirror is accepted as a fallback so a hand-built or imported
    record that carries the value at the pointer root still reads it. A value
    that does not coerce to a positive integer is treated as unset rather
    than as a charge surface, so a malformed declaration degrades to the
    wall-clock behaviour instead of refusing to measure.
    """
    node = record.get("node")
    value = node.get("token_budget") if isinstance(node, Mapping) else None
    if value is None:
        value = record.get("token_budget")
    if value is None or value == "":
        return None
    try:
        budget = int(value)
    except (TypeError, ValueError):
        return None
    return budget if budget > 0 else None


def _generated_tokens(record: Mapping[str, Any]) -> int | None:
    """The run's recorded generated output tokens, or None when unmeasured.

    observe() folds the stream's measured throughput block into the pointer,
    so a run that has been observed carries its token total here. Absence is
    not a verdict: an unmeasured run is charged nothing, matching how a run
    with no stream is never called an overrun on elapsed either.
    """
    throughput = record.get("throughput")
    if not isinstance(throughput, Mapping):
        return None
    value = throughput.get("generated_tokens")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _positive_rate(value: Any) -> float | None:
    """Read a measured rate without treating zero or a boolean as throughput."""
    if isinstance(value, bool):
        return None
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return None
    return rate if math.isfinite(rate) and rate > 0 else None


@lru_cache(maxsize=64)
def _historical_reference_rate(
    project: str, backend: str, model: str, window_bucket: int
) -> tuple[float | None, int]:
    """Median recent committed rate for the same backend and model.

    The five-minute bucket bounds repeated ledger reads by a live watcher. A
    small or absent cohort is not a reference; a run then keeps an unknown
    cause instead of inheriting another backend's or model's rate.
    """
    from reckon import ledger as ledger_module

    try:
        rows = ledger_module.load(project)[0]["runs"]
    except (ledger_module.LedgerError, OSError, ValueError, KeyError, TypeError):
        return None, 0
    end = window_bucket * 300
    start = end - 7 * 86400
    rates: list[float] = []
    for row in rows:
        if not isinstance(row, Mapping) or row.get("backend") != backend:
            continue
        agent = row.get("agent")
        if not isinstance(agent, Mapping) or agent.get("model") != model:
            continue
        completed = parse_utc(str(row.get("completed_at") or ""))
        if completed is None or not start <= completed.timestamp() <= end:
            continue
        throughput = row.get("throughput")
        if not isinstance(throughput, Mapping):
            continue
        rate = _positive_rate(throughput.get("tokens_per_second"))
        if rate is not None:
            rates.append(rate)
    return (median(rates), len(rates)) if len(rates) >= 10 else (None, len(rates))


def _budget_overrun_cause(
    record: Mapping[str, Any],
    timing: Mapping[str, Any],
    *,
    now_seconds: float | None = None,
) -> dict[str, Any]:
    """Attribute an overrun using its rate beside a same-model reference.

    A slow generation rate is lane saturation in this operational vocabulary;
    it can also reflect a serving defect, so the rate and reference travel with
    the label. Normal-rate work is over-large only when its token allowance was
    exceeded or its generated volume would exceed the seconds allowance even
    at the reference rate. Anything unmeasured stays unknown.
    """
    if not timing.get("budget_overrun") and not timing.get("budget_overrun_seconds"):
        return {}
    throughput = record.get("throughput")
    rate = (
        _positive_rate(throughput.get("tokens_per_second"))
        if isinstance(throughput, Mapping)
        else None
    )
    project = str(record.get("project") or "")
    backend = str(record.get("backend") or "")
    agent = record.get("agent")
    model = str(agent.get("model") or "") if isinstance(agent, Mapping) else ""
    reference: float | None = None
    count = 0
    if rate is not None and project and backend and model:
        moment = _utc_seconds() if now_seconds is None else float(now_seconds)
        reference, count = _historical_reference_rate(
            project, backend, model, int(moment // 300)
        )
    cause = "unknown"
    if rate is not None and reference is not None:
        if rate < reference / 2:
            cause = "lane-saturated"
        else:
            tokens = _generated_tokens(record)
            budget_seconds = timing.get("budget_seconds")
            if timing.get("budget_overrun_tokens", 0) or (
                tokens is not None
                and isinstance(budget_seconds, (int, float))
                and budget_seconds > 0
                and tokens / reference > budget_seconds
            ):
                cause = "over-large"
    return {
        "budget_overrun_cause": cause,
        "budget_overrun_rate": rate,
        "budget_overrun_reference_rate": reference,
        "budget_overrun_reference_runs": count,
        "budget_overrun_reference_source": "recent committed runs, same backend and model",
    }


def _token_budget_timing(
    token_budget: int,
    generated_tokens: int | None,
    *,
    budget_seconds: int | None,
    elapsed_seconds: int | None,
) -> dict[str, Any]:
    """Measure a run against a token budget, keeping the seconds ceiling.

    The worker's own budget is denominated in generated tokens — the quantity
    the same task needs regardless of what else the lane is doing — so a slow
    lane inside its token budget is not an overrun however long it took, and
    a lane that delivered more tokens than the allowance is charged for the
    work. Wall clock cannot bound a process that stopped producing, so the
    seconds allowance survives here under its own name as the ceiling that
    still refuses such a run; the two verdicts never share a name.
    """
    wall_overrun = (
        max(0, int(elapsed_seconds) - int(budget_seconds))
        if elapsed_seconds is not None and budget_seconds is not None
        else 0
    )
    if generated_tokens is None:
        token_overrun = 0
    else:
        token_overrun = max(0, generated_tokens - token_budget)
    return {
        "budget_seconds": budget_seconds,
        "elapsed_seconds": elapsed_seconds,
        "budget_overrun": generated_tokens is not None and token_overrun > 0,
        "budget_overrun_seconds": wall_overrun,
        "budget_tokens": token_budget,
        "generated_tokens": generated_tokens,
        "budget_overrun_tokens": token_overrun,
        "hang_ceiling_seconds": budget_seconds,
        "ceiling_overrun": wall_overrun > 0,
    }


def _budget_timing(
    record: Mapping[str, Any], *, now_seconds: float | None = None
) -> dict[str, Any]:
    """Measure one run against its declared allowance without mutating it.

    A run whose worker process is gone has finished, so its elapsed is measured
    to its own stream completion — the same stamp promotion records — rather
    than to the moment of reading. A still-running run measures to now, and the
    wall-clock ceiling that protects the fleet from a hang is untouched because
    a live process still anchors here. A reader resolving a run late therefore
    reports the worker's own time, not the coordinator's wait to promote it.

    When the run carries a token budget, the budget verdict is denominated in
    generated tokens (the worker is charged for the work, not the queue) and
    the wall-clock allowance becomes the separately named hang ceiling. Without
    one, the wall-clock overrun is the only verdict, unchanged.
    """
    node = record.get("node") or {}
    token_budget = _declared_token_budget(record)
    try:
        if "attempt_budget_seconds" in record:
            budget_seconds = int(record["attempt_budget_seconds"])
        else:
            budget_seconds = parse_duration(str(node.get("time_budget") or ""))
        started = parse_utc(
            str(record.get("attempt_started_at") or record.get("created_at") or "")
        )
    except (CrewError, TypeError, ValueError):
        started = None
    if started is None:
        if token_budget is not None:
            return _token_budget_timing(
                token_budget,
                _generated_tokens(record),
                budget_seconds=None,
                elapsed_seconds=None,
            )
        return {
            "budget_seconds": None,
            "elapsed_seconds": None,
            "budget_overrun": False,
            "budget_overrun_seconds": 0,
        }
    moment = _utc_seconds() if now_seconds is None else float(now_seconds)
    elapsed_to = None
    if record.get("process_alive") is False:
        try:
            completion = _stream_completion_stamp(record)
        except (CrewError, OSError):
            completion = None
        if isinstance(completion, str) and completion:
            finished = parse_utc(completion)
            if finished is not None:
                elapsed_to = finished.timestamp()
    if elapsed_to is None:
        elapsed = max(0, int(moment - started.timestamp()))
    else:
        elapsed = max(0, int(elapsed_to - started.timestamp()))
    if token_budget is not None:
        return _token_budget_timing(
            token_budget,
            _generated_tokens(record),
            budget_seconds=budget_seconds,
            elapsed_seconds=elapsed,
        )
    overrun = max(0, elapsed - budget_seconds)
    return {
        "budget_seconds": budget_seconds,
        "elapsed_seconds": elapsed,
        "budget_overrun": overrun > 0,
        "budget_overrun_seconds": overrun,
    }


def _apply_budget_watchdog(
    record: dict[str, Any], config: Mapping[str, Any] | None
) -> None:
    """Record deadline posture and optionally stop an over-grace CLI worker."""
    timing = _budget_timing(record)
    timing.update(_budget_overrun_cause(record, timing))
    record.update(timing)
    fences = (config or {}).get("fences") or {}
    if not fences.get("enforce_budget_watchdog"):
        return
    budget_seconds = timing["budget_seconds"]
    elapsed_seconds = timing["elapsed_seconds"]
    try:
        grace = float(fences.get("budget_grace_multiple", 1.0))
    except (TypeError, ValueError):
        return
    if (
        budget_seconds is None
        or elapsed_seconds is None
        or elapsed_seconds <= budget_seconds * grace
        or record.get("launch") != "cli"
        or record.get("phase") in _TERMINAL_RUN_PHASES
        or record.get("process_alive") is not True
    ):
        return
    pid = record.get("pid")
    try:
        _signal_process_group(
            int(pid),
            record.get("pid_start_time"),
            run_dir=_run_directory(record),
            reason="budget-watchdog",
        )
    except (
        CrewError,
        ProcessLookupError,
        PermissionError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        record["watchdog_detail"] = f"budget watchdog could not stop pid {pid}: {exc}"
        return
    record["phase"] = "stopped"
    record["stopped_at"] = _utc_now()
    record["watchdog_enforced"] = True
    record["detail"] = (
        f"budget watchdog stopped pid {pid} after {elapsed_seconds}s "
        f"against {budget_seconds}s with {grace:g}x grace"
    )


def _refusal_block(
    record: Mapping[str, Any], budget: Mapping[str, Any]
) -> dict[str, Any]:
    """Normalise a refusal budget block into the fields a blocked reason needs."""
    return {
        "backend": str(record.get("backend") or "unknown"),
        "limit_kind": str(budget.get("rate_limit_type") or "quota"),
        "resets_at": str(budget.get("resets_at") or "unknown"),
    }


def _harness_command(record: Mapping[str, Any], argv: Any) -> str | None:
    """The command that names a cli run's harness, for a stream translation.

    A placed launch prefixes its resolved argv with the scheduler invocation, so
    ``argv[0]`` on such a record names the scheduler rather than the harness and
    a translation built from it fails. The record carries the harness the launch
    resolved under ``command``, captured before the placement wrapped the plan,
    so that field is taken first and ``argv[0]`` is the fallback for a record
    written before the field existed.
    """
    command = record.get("command")
    if command:
        return str(command)
    if isinstance(argv, list) and argv:
        return str(argv[0])
    dialect = record.get("dialect")
    return str(dialect) if dialect else None


@contextlib.contextmanager
def _memo_published(record: Mapping[str, Any], memo: dict[str, Any]) -> Iterator[None]:
    """Make a classification's memo reachable to the stream readers it calls.

    The readers that consult this run's stream take the record and nothing else,
    because callers replace them wholesale in tests that need to shape a row.
    Widening their signature to carry a cache would break every such caller, so
    the memo is published here for the duration of the calls that need it and
    read back by run id. A memo left published by an interrupted call answers
    for the same run only, and the stream entry is guarded by the identity of
    the file it was read from, so a stale in-flight memo can serve nothing the
    file itself does not still say.
    """
    global _CLASSIFICATION_MEMO_IN_FLIGHT
    previous = _CLASSIFICATION_MEMO_IN_FLIGHT
    _CLASSIFICATION_MEMO_IN_FLIGHT = (str(record.get("run_id") or ""), memo)
    try:
        yield
    finally:
        _CLASSIFICATION_MEMO_IN_FLIGHT = previous


def _memo_for(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """The memo a running classification published for this run, if any."""
    active = _CLASSIFICATION_MEMO_IN_FLIGHT
    if active is None or active[0] != str(record.get("run_id") or ""):
        return None
    return active[1]


def _observed_stream(
    record: Mapping[str, Any],
    *,
    memo: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """One cli run's stream observation, served from a memo while it still holds.

    Two readers of a classification consult the same stream — the budget gates
    and the background-wait signal — and each would otherwise pay a full parse
    of it. They share this read, so one classification parses the stream once
    and the memo beside the pointer carries what it found, which is what makes a
    second classification of an unchanged run cost no parse at all.

    The memo's stream entry is served only while the file it was read from is
    still that file by identity. When it is not, the cursor carries the byte
    offset the last read reached and a fingerprint of the stream's opening, so the read
    resumes only while the stream still opens with that same fingerprint: an
    offset past the end of the file, a replaced stream, or one rewritten in place
    to a new opening all read from the first record, because an offset into a
    predecessor's bytes means nothing in a file that no longer holds them.
    """
    if record.get("launch") != "cli":
        return None
    log = Path(str(record.get("log_path") or ""))
    if not log.is_file():
        return None
    command = _harness_command(record, record.get("argv"))
    if not command:
        return None
    from reckon import _backends

    stored = memo.get("stream") if memo is not None else None
    resume: dict[str, Any] | None = None
    if isinstance(stored, Mapping) and str(stored.get("path") or "") == str(log):
        state = stored.get("state")
        # An offset only means the same thing in the file it was reached in: a
        # stream replaced at this path since means nothing here, so a changed
        # inode re-reads from the first record while a grown one resumes.
        if (
            isinstance(state, Mapping)
            and str(stored.get("inode") or "") == _file_inode(log)
        ):
            resume = {
                "offset": int(stored.get("offset") or 0),
                "state": state,
                "head": stored.get("head"),
            }
        # What an observation is a function of is the file it was read from and
        # the lane it was translated for, so those are what an entry is served
        # against: an unchanged stream read for the same command and backend
        # cannot have a different observation, and one held by a memo written
        # before the key moved is still this run's own reading of this file.
        if (
            stored.get("ident") == _file_identity(log)
            and stored.get("command") == command
            and stored.get("backend") == str(record.get("backend") or "")
            and isinstance(stored.get("observation"), Mapping)
        ):
            return dict(stored.get("observation") or {})

    try:
        observation = _backends.observe_log(
            backend_name=str(record.get("backend") or ""),
            backend={"command": command},
            log_path=log,
            resume=resume,
        )
    except (_backends.BackendError, CrewError, OSError, ValueError):
        # An unreadable or untranslatable stream carries no readable budget and
        # no final message; the manifest and liveness paths still classify it.
        return None
    seen = observation.as_dict()
    if memo is not None:
        offset = int(observation.stream_state.get("offset") or 0)
        memo["stream"] = {
            "path": str(log),
            "ident": _file_identity(log),
            "inode": _file_inode(log),
            "head": _backends.stream_head_fingerprint(log, offset=offset),
            "command": command,
            "backend": str(record.get("backend") or ""),
            "offset": offset,
            "state": observation.stream_state,
            "observation": seen,
        }
    return seen


def _stream_budget(record: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The budget block a cli run's stream records, folded in or read fresh.

    Shared by the refusal and retry-shape gates so the stream is parsed once
    even when both are consulted for the same run. observe() folds the stream's
    budget into the pointer, while the ticker reads raw pointers that have not
    been through observe; both paths resolve through the same backend
    translation, so they reach the same block and a ticker reading a raw
    pointer cannot disagree with observe's phase. The read itself, memo
    included, belongs to :func:`_observed_stream`.
    """
    budget = record.get("budget")
    if isinstance(budget, Mapping) and budget.get("refusal"):
        return budget
    seen = _observed_stream(record, memo=_memo_for(record))
    if seen is None:
        return None
    return seen.get("budget") or None


def _stream_refusal_block(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """The provider refusal a cli run's stream records, folded in or read fresh.

    A spend or usage refusal is a block, not an abandonment: the account is not
    broken, only spent until a moment the refusal names. The block comes from
    the same budget the retry-shape gate reads, so the two dead-lane readings
    agree on one stream rather than each owning a separate translation.

    Declining is the load-bearing half. A stream that reports an ordinary
    failed turn — a bad model id, a lost stream, a context overflow — carries
    none of the recognised limit phrases and returns None, so a crash is never
    mistaken for a block.
    """
    budget = _stream_budget(record)
    if budget is not None and budget.get("refusal"):
        return _refusal_block(record, budget)
    return None


# A spent local lane's mid-flight shape, folded from the budget block the
# stream observer wrote: rate-limit retries counted with no terminal result, so
# the number is a magnitude and liveness is the verdict. The exhaustion shape
# (retries ended in a terminal error result) reads as a refusal instead, so the
# two dead-lane readings never overlap.
_RATE_LIMIT_RETRY_RE = re.compile(r"after (\d+) rate-limit retries")

# An exhausted unmetered lane folds no refusal at all: the observer writes
# refusal false with lane_backpressure true and a detail naming the retry count
# ("run died after N consumer-queue retries ...; the lane refused"), because a
# lane without a budget has nothing to refuse from. The marker is what a run
# that retried and recovered never carries, so it discriminates terminal
# exhaustion from routine retries.
_BACKPRESSURE_RETRY_RE = re.compile(r"run died after (\d+) consumer-queue retries")


def _stream_exhaustion_block(
    record: Mapping[str, Any], budget: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Terminal retry exhaustion on an unmetered lane, as a refusal block.

    A spent unmetered consumer ends its retries in an error result, and the
    budget observer surfaces that terminal shape as ``lane_backpressure`` true
    with the retry count in the detail — not as a budget refusal, because the
    lane's budget is not what was spent. The marker is absent on a run that
    retried and recovered, so no block is reached for a live or successful run;
    only classify_pointer's dead-process hand joins the marker into a blocked
    reading, so the row names the lane and offers resume. ``budget`` is the
    block :func:`_stream_budget` already resolved, so the stream is parsed once
    regardless of which gates consult it.
    """
    if budget.get("refusal"):
        return None
    if not budget.get("lane_backpressure"):
        return None
    detail = str(budget.get("detail") or "")
    match = _BACKPRESSURE_RETRY_RE.search(detail)
    if match is None:
        return None
    return {
        "backend": str(record.get("backend") or "unknown"),
        "limit_kind": "rate-limit",
        "resets_at": None,
        "retries": int(match.group(1)),
    }


def _stream_retry_block(
    record: Mapping[str, Any], budget: Mapping[str, Any]
) -> dict[str, Any] | None:
    """The mid-flight rate-limit retry shape budget carries, else None.

    The local lane reports a spent consumer as rate-limit ``api_retry`` records,
    and the budget observer surfaces the count in the block's detail with no
    terminal result ("no terminal result yet"). From the stream alone that
    shape is indistinguishable from a live worker mid-retry-burst, so no verdict
    is reached here: the block names the lane and the count, and only
    classify_pointer's dead-process hand joins it into a blocked reading. An
    alive worker mid-retry-burst (measured completing with seven retries) reads
    running, not blocked. ``budget`` is the block :func:`_stream_budget` already
    resolved, so the stream is parsed once regardless of which gates consult it.
    """
    if budget.get("refusal"):
        return None
    detail = str(budget.get("detail") or "")
    if "no terminal result yet" not in detail:
        return None
    match = _RATE_LIMIT_RETRY_RE.search(detail)
    if match is None:
        return None
    return {
        "backend": str(record.get("backend") or "unknown"),
        "limit_kind": str(budget.get("rate_limit_type") or "rate-limit"),
        "retries": int(match.group(1)),
        "resets_at": str(budget.get("resets_at") or "unknown"),
    }


# The client substitutes this exact string into a message's model field when no
# model served the turn, so it is a marker rather than a model name.
_SYNTHETIC_MODEL = "<synthetic>"


def _assistant_refusal_text(message: Mapping[str, Any]) -> str:
    """The prose of a synthetic assistant message, or the empty string."""
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    parts = [
        str(block.get("text") or "")
        for block in content
        if isinstance(block, Mapping) and block.get("type") == "text"
    ]
    return " ".join(parts).strip()


def _result_turned_no_tokens(event: Mapping[str, Any]) -> bool:
    """Whether a result record reports an error turn that generated nothing.

    Every token counter zero and a zero API duration are what separate a turn
    the client refused before dispatching from one that ran and then failed —
    a failed turn still reports the tokens it spent and the API duration it
    waited on.
    """
    if event.get("is_error") is not True:
        return False
    try:
        if int(event.get("duration_api_ms") or 0) != 0:
            return False
        if int(event.get("num_turns") or 0) > 1:
            return False
    except (TypeError, ValueError):
        return False
    usage = event.get("usage")
    if not isinstance(usage, Mapping):
        return False
    for key in (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ):
        try:
            if int(usage.get(key) or 0) != 0:
                return False
        except (TypeError, ValueError):
            return False
    return True


def _admission_refusal(
    record: Mapping[str, Any], *, memo: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """The marks of a run the backend refused before serving its first turn.

    A refusal at admission ends the run in three lines: an assistant record
    whose model is the client's substitution for "no model served this turn"
    (the literal ``<synthetic>``), carrying ``error: invalid_request`` with the
    reason it refused; and a result record whose terminal reason is
    ``blocking_limit`` with a zero API duration and every token counter zero.
    Together they say no model was reached at all, which is a different stop
    from a worker whose process died mid-turn. The generic dead-process
    classification cannot say which happened, so this one names it and carries
    the paths a reader acts on.

    Requiring the zero-token result beside the synthetic message is deliberate:
    a stream that merely mentions the same words while doing real work returns
    None, and an ordinary failed turn — which reports the tokens it spent —
    cannot reach this reading. None means the ordinary dead-process arms
    classify the run, so this gate never widens them.

    The scan's result is memoised against the stream's stat identity. Decoded
    events come from the shared stream cache, so a grown stream parses only its
    append and an unchanged stream needs no decoding here.
    """
    if record.get("launch") != "cli":
        return None
    log = Path(str(record.get("log_path") or ""))
    if not log.is_file():
        return None
    ident = _file_identity(log)
    cached = memo.get("admission") if memo is not None else None
    if isinstance(cached, Mapping) and str(cached.get("ident") or "") == ident:
        refusal = cached.get("refusal")
        return dict(refusal) if isinstance(refusal, Mapping) else None
    refusal_reason = ""
    terminal_reason = ""
    zero_token_error = False
    from reckon import _backends

    try:
        events, _malformed = _backends.cached_stream_events(log)
        size = log.stat().st_size
    except OSError:
        return None
    for event in events:
        kind = str(event.get("type") or "")
        if kind == "assistant":
            message = event.get("message")
            if not isinstance(message, Mapping):
                continue
            if str(message.get("model") or "") != _SYNTHETIC_MODEL:
                continue
            if str(event.get("error") or "") != "invalid_request":
                continue
            text = _assistant_refusal_text(message)
            if text:
                refusal_reason = text
        elif kind == "result":
            terminal_reason = str(event.get("terminal_reason") or "")
            if _result_turned_no_tokens(event):
                zero_token_error = True
    _count_admission_bytes(size)
    refusal: dict[str, Any] | None = None
    if refusal_reason and terminal_reason == "blocking_limit" and zero_token_error:
        refusal = {
            "reason": refusal_reason,
            "terminal_reason": terminal_reason,
        }
    if memo is not None:
        memo["admission"] = {"ident": ident, "refusal": refusal}
    return refusal


def _budget_hold_block(
    record: Mapping[str, Any], budget: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    """A rate-limit event that rejected the turn, as a hold that ages out.

    A metered harness reports a spent window as ``rate_limit_event`` with
    ``status: rejected`` long before any prose refusal appears: the request was
    refused, the window names itself, and its reset is the moment time lifts the
    hold. This is distinct from :func:`_stream_refusal_block`, which reads a
    terminal prose or retry-exhaustion refusal, so the two never compete for the
    same run — a rejected window carries ``refusal`` false and reaches only
    this gate, while a refusal block is read through the other. ``budget`` is
    the block :func:`_stream_budget` already resolved.
    """
    if budget is None or budget.get("refusal"):
        return None
    if str(budget.get("threshold_status") or "").casefold() != "rejected":
        return None
    return {
        "backend": str(record.get("backend") or "unknown"),
        "limit_kind": str(budget.get("rate_limit_type") or "rate-limit"),
        "resets_at": str(budget.get("resets_at") or "unknown"),
    }


def _blocked_session_resolution(
    record: Mapping[str, Any], run_id: str
) -> dict[str, Any]:
    """Resolve a blocked run's session without changing its evidence.

    Session resolution already has one ordered authority spanning the live
    pointer, the run's stream, and its promoted ledger row. Importing it only
    when a block needs a session answer avoids making routine classification consult
    durable history, while keeping this read pure: neither the pointer nor any
    of its evidence is rewritten here.
    """
    from reckon.crew.resumption import resolve_session

    return resolve_session(
        run_id,
        record=record,
        project=str(record.get("project") or ""),
        root=record.get("repo"),
    )


def _resume_remedy(resolution: Mapping[str, Any], run_id: str) -> dict[str, str] | None:
    """Return an executable recovery command when session evidence exists."""
    if not resolution.get("resolved"):
        return None
    return {
        "command": (f"reckon crew resume --run {run_id} --advice continue"),
        "session_id": str(resolution["session_id"]),
        "source": str(resolution["source"]),
    }


# A print-mode invocation makes exactly one turn and exits when it ends, so a
# worker still waiting on a background task at that moment leaves one of two
# traces rather than a clean result. The ceiling message is the harness's own
# stderr line when it gave up waiting and terminated the task itself. The
# duration is read from the environment and varies, so only the sentence
# around it is fixed.
_BACKGROUND_WAIT_CEILING_RE = re.compile(
    r"Background tasks still running after \d+s; terminating\.\s*"
    r"Set CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0 to wait indefinitely\.",
)
# The agent's own last words when its turn ended before the background work
# it was waiting on did. Matched loosely around the fixed clause so a run
# naming a different suite or task still recognises the same shape.
_BACKGROUND_WAIT_FINAL_MESSAGE_RE = re.compile(
    r"waiting for (the )?background .+? before finalizing the manifest",
    re.IGNORECASE | re.DOTALL,
)


def _background_wait_signal(record: Mapping[str, Any]) -> str | None:
    """The one sentence proving a vanished process was waiting on background work.

    A dead process with no complete manifest is indistinguishable from one
    that simply crashed, unless the run directory itself says otherwise. Two
    traces say otherwise: the harness's own ceiling message on stderr, or the
    agent's last turn stating in its own words that it was waiting on
    background work before finalizing the manifest — with nothing after that
    turn because a print-mode invocation has no next one to write. Neither is
    a crash; both name a run whose session is intact and whose only
    outstanding step is a resume long enough to collect the manifest it was
    already about to write.
    """
    stderr_path = record.get("stderr_path")
    if stderr_path:
        try:
            stderr_text = Path(str(stderr_path)).read_text()
        except OSError:
            stderr_text = ""
        if _BACKGROUND_WAIT_CEILING_RE.search(stderr_text):
            return (
                "the worker's stderr recorded the background-wait ceiling "
                "before the process terminated"
            )

    final_message = str(record.get("final_message") or "")
    if not final_message and record.get("launch") == "cli":
        # observe() folds the stream's final message onto the pointer, but a
        # caller reading the raw pointer — the watch producer's path — has
        # none of it cached yet. Reading the log directly keeps that path
        # answering the same question the folded record would; the shared read
        # means a classification that already parsed this stream pays nothing
        # for asking a second question of the same bytes.
        seen = _observed_stream(record, memo=_memo_for(record))
        if seen is not None:
            final_message = str(seen.get("final_message") or "")

    if final_message and _BACKGROUND_WAIT_FINAL_MESSAGE_RE.search(final_message):
        return (
            "the worker's last turn reported waiting on background work "
            f"before finalizing the manifest: {final_message.strip()}"
        )
    return None


# A tool call whose own contract ends the wait. A quiet stream is read as a hang
# unless the last thing the worker asked for was something that ends on its own:
# a bounded sleep, the peer channel's bounded read, or a task wait that cannot
# outlive its window. Only the last assistant turn is consulted, so a hang that
# follows an earlier sleep still reads as a hang.
_BOUNDED_WAIT_TOOL_NAMES = frozenset({"TaskOutput", "TaskOutputFull", "ScheduleWakeup"})
# The double dash takes no leading word boundary — ``--wait`` follows a space,
# and neither is a word character — so only the trailing boundary is anchored.
_PEER_CHANNEL_WAIT_RE = re.compile(r"peer-read[^\n]*--wait\b", re.IGNORECASE)
_SLEEP_RE = re.compile(r"(?:\btime\.)?\bsleep\s+(\d+)", re.IGNORECASE)


def _last_bounded_wait(record: Mapping[str, Any]) -> str | None:
    """The last tool call's bounded wait, named, or None when there is none.

    Reads only the tool call the worker most recently started, because that is
    the call a quiet stream is currently sitting in. A poll loop that sleeps is
    bounded by its own sleeps; a peer-channel read with a ``--wait`` is bounded
    by that duration; a task wait is bounded by its own contract. None of these
    need a person — each wakes itself, which is what separates them from a hang.
    """
    log = Path(str(record.get("log_path") or ""))
    if not log.is_file():
        return None
    try:
        with log.open(encoding="utf-8", errors="replace") as handle:
            last_name = ""
            last_command = ""
            for line in handle:
                try:
                    event = json.loads(line)
                except (ValueError, TypeError):
                    continue
                message = event.get("message")
                if not isinstance(message, Mapping):
                    continue
                content = message.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if (
                        not isinstance(block, Mapping)
                        or block.get("type") != "tool_use"
                    ):
                        continue
                    name = str(block.get("name") or "")
                    command = ""
                    if isinstance(block.get("input"), Mapping):
                        command = str(
                            block["input"].get("command")
                            or block["input"].get("prompt")
                            or ""
                        )
                    last_name, last_command = name, command
    except OSError:
        return None
    if not last_name and not last_command:
        return None
    if last_name in _BOUNDED_WAIT_TOOL_NAMES:
        return f"a {last_name} task wait"
    if _PEER_CHANNEL_WAIT_RE.search(last_command):
        return "a peer-channel read with a bounded wait"
    match = _SLEEP_RE.search(last_command)
    if match is not None:
        return f"a {match.group(1)}s sleep"
    return None


def _stall_wait_reason(record: Mapping[str, Any]) -> str | None:
    """Why a quiet, alive run is paused rather than hung, or None.

    Three shapes turn a quiet stream into a wait instead of a stall: the worker
    is mid-retry on a rate limit (the lane's window resets on its own), its
    last request was refused on a rate-limit window that resets, or its last
    tool call was a bounded wait (it wakes itself). A run with none of these is
    genuinely hung and must stay stalled, and a live rate-limit retry loop that
    is still emitting keeps reading as working — only a quiet one is arbitrated
    here.
    """
    budget = _stream_budget(record)
    if budget is not None and not budget.get("refusal"):
        retry = _stream_retry_block(record, budget)
        if retry is not None:
            return (
                f"a rate-limit retry loop ({retry['retries']} retries); "
                "the lane's window resets and the loop keeps the session alive"
            )
        hold = _budget_hold_block(record, budget)
        if hold is not None:
            return (
                f"a rejected {hold['limit_kind']} window that resets "
                f"{hold['resets_at']}"
            )
    return _last_bounded_wait(record)


def _wait_probe(value: Any) -> list[str]:
    """Read a shell-free argument vector from a waiting manifest."""
    if isinstance(value, list):
        probe = value
    else:
        try:
            probe = json.loads(str(value))
        except (TypeError, json.JSONDecodeError):
            return []
    if not isinstance(probe, list) or not probe:
        return []
    if any(not isinstance(item, str) or not item.strip() for item in probe):
        return []
    return [item.strip() for item in probe]


# The shapes a wait declaration's condition can take, named wherever one is
# refused. Three workers wrote the wait block three wrong ways in one hour, each
# with the right key names and a value the reader discarded: the run then read
# to a coordinator as a worker that had declared nothing, so the repair could
# not be made from the row. A reader that silently reduces an unrecognised shape
# to the absence of a declaration is the defect; naming what it does accept in
# the refusal is what removes it.
_WAIT_ACCEPTED_SHAPES = (
    "the reader accepts a shell-free argument vector whose first element is a "
    "bare program name, or a file condition declared as wait_file with one "
    "path or a JSON array of paths"
)


def _wait_file_paths(value: Any) -> list[str]:
    """Read the paths a file condition names.

    One path is a plain scalar and several are a JSON array, the same pair of
    forms the terminal list accepts -- except that a scalar is never
    comma-split here, because a comma inside a path is part of the path and a
    reader that split it would invent two paths that do not exist. A value that
    is present and is neither of those forms reads as no paths, so a caller
    telling the shapes apart can refuse it rather than read it as an undeclared
    condition.
    """
    if isinstance(value, list):
        if any(not isinstance(item, str) or not item.strip() for item in value):
            return []
        return [item.strip() for item in value]
    text = str(value or "").strip()
    if not text:
        return []
    if not text.lstrip().startswith("["):
        return [text]
    try:
        parsed = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list) or any(
        not isinstance(item, str) or not item.strip() for item in parsed
    ):
        return []
    return [item.strip() for item in parsed]


def _wait_probe_shape_refusal(value: Any) -> str:
    """The reason a declared probe is not a shape the reader can run, or "".

    Absence is not a refusal: a declaration carrying no probe is incomplete,
    which the reader reports by naming the missing field, and the two outcomes
    stay distinguishable. Refused is a probe that is present and unreadable,
    because that is the shape that used to reduce silently to the absence of a
    probe -- and a worker reading its own row was then told it had declared
    nothing when it had declared something.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return ""
    candidate: Any = value
    if isinstance(value, str):
        try:
            candidate = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return (
                "wait_probe is a single value the reader cannot parse as a "
                f"JSON array; {_WAIT_ACCEPTED_SHAPES}"
            )
    if not isinstance(candidate, list):
        return f"wait_probe is a scalar rather than a list; {_WAIT_ACCEPTED_SHAPES}"
    if not candidate:
        return ""
    if any(not isinstance(item, str) or not item.strip() for item in candidate):
        return (
            "wait_probe is a list of mappings rather than of program "
            f"arguments; {_WAIT_ACCEPTED_SHAPES}"
        )
    first = candidate[0].strip()
    if not first or " " in first or "\t" in first:
        return (
            f"wait_probe starts with {first!r}, a whole command line rather "
            f"than a program name, so nothing can exec it; {_WAIT_ACCEPTED_SHAPES}"
        )
    return ""


def _wait_file_shape_refusal(value: Any) -> str:
    """The reason a declared file condition is not a shape the reader reads."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return ""
    if isinstance(value, list):
        if not value:
            return ""
        if any(not isinstance(item, str) or not item.strip() for item in value):
            return (
                "wait_file is a list of mappings rather than of paths; "
                f"{_WAIT_ACCEPTED_SHAPES}"
            )
        return ""
    if isinstance(value, str):
        if not value.lstrip().startswith("["):
            return ""
        if not _wait_file_paths(value):
            return (
                "wait_file opens with a bracket but does not parse as a JSON "
                f"array of paths; {_WAIT_ACCEPTED_SHAPES}"
            )
        return ""
    return f"wait_file is neither a path nor a list of paths; {_WAIT_ACCEPTED_SHAPES}"


# A terminal value names a state the probe prints. The exit-code sentinel is
# not one: the observation the reader matches against is the probe's own output,
# so a declaration whose terminal is an exit status reads as pending on every
# sweep of a job that has already ended, and the run never lifts.
_WAIT_EXIT_SENTINEL = re.compile(r"exit:\s*\d+\s*$", re.IGNORECASE)


def _wait_terminal_names_no_probe_state(terminal: Sequence[str]) -> str:
    """Name a terminal value that is not a state any probe prints, or ""."""
    for value in terminal:
        spelled = str(value).strip()
        if spelled and _WAIT_EXIT_SENTINEL.match(spelled):
            return spelled
    return ""


def _wait_file_probe(files: Sequence[str]) -> list[str]:
    """The shell-free argument vector a file condition derives.

    The vector is the shape a file condition takes so it reads like every other
    wait: ``test -e`` per path joined by ``-a`` exits 0 exactly when every
    declared path exists, and the terminal the declaration needs is the
    ``exit:0`` sentinel that exit status prints.

    Two readers exist, and only one of them runs this vector. The sweep's
    reader in ``reckon/crew/resumption.py`` executes it and is what decides a
    lift, so this vector is the load-bearing half for a park. The classifier's
    reader -- ``_run_wait_condition_probe`` below -- answers a file condition by
    looking for the paths themselves and returns before any vector runs, so a
    row can name which paths are still missing. The two agree on the answer and
    not on the mechanism; nothing here is run by the classifier.
    """
    argv = ["test"]
    for index, path in enumerate(files):
        if index:
            argv.append("-a")
        argv.extend(["-e", path])
    return argv


def _wait_terminal_values(value: Any) -> list[str]:
    """Read the external states that mean a condition has terminated.

    A terminal-state list is read from the same forms the probe accepts: a
    list, or a JSON array written as a string, each validated the same way --
    a list whose members are all non-empty strings. A value whose first
    non-space character is an opening square bracket is read as JSON only:
    when it parses to a list of non-empty strings those strings are the
    states, and when it does not parse the result is no states at all, so a
    manifest written that way is reported as an incomplete wait declaration
    rather than honoured with state names carrying JSON punctuation. The
    comma-separated form keeps working for briefs and manifests in flight,
    and is never applied to a value that opens with a bracket: comma-splitting
    a malformed array is what produces a state name containing a bracket.
    """
    if isinstance(value, list):
        if any(not isinstance(i, str) or not i.strip() for i in value):
            return []
        states = value
    else:
        text = str(value or "")
        if text.lstrip().startswith("["):
            try:
                parsed = json.loads(text)
            except (TypeError, json.JSONDecodeError):
                return []
            if not isinstance(parsed, list) or any(
                not isinstance(i, str) or not i.strip() for i in parsed
            ):
                return []
            states = parsed
        else:
            states = text.split(",")
    return [str(item).strip() for item in states if str(item).strip()]


def _wait_condition_observation(
    value: Any, *, terminal_values: list[str]
) -> dict[str, str]:
    """Normalise a probe result into pending, met, or unknown."""
    if isinstance(value, bool):
        return {
            "state": "met" if value else "pending",
            "observed": "terminal" if value else "pending",
            "detail": "condition test returned a boolean verdict",
        }
    if isinstance(value, Mapping):
        state = str(value.get("state") or "").strip().lower()
        if state not in WAIT_CONDITION_STATES:
            terminal = value.get("terminal")
            state = (
                "met"
                if terminal is True
                else "pending"
                if terminal is False
                else "unknown"
            )
        return {
            "state": state,
            "observed": str(value.get("observed") or state),
            "detail": str(value.get("detail") or "condition test returned a verdict"),
        }
    observed = str(value or "").strip()
    terminal = {item.casefold() for item in terminal_values}
    return {
        "state": "met" if observed.casefold() in terminal else "unknown",
        "observed": observed or "unavailable",
        "detail": "condition test returned an unstructured observation",
    }


def _wait_file_condition_observation(
    record: Mapping[str, Any], files: Sequence[str]
) -> dict[str, str]:
    """Read a file condition by looking for the paths it declares.

    The condition is met when every path exists, and the paths still missing
    are named, so a row says which one the wait is on rather than only that
    something is absent. A relative path resolves against the run's worktree,
    which is where the worker that declared it was running.
    """
    worktree = Path(str(record.get("worktree") or "."))
    root = worktree if worktree.is_dir() else Path(".")

    def _resolved(path: str) -> Path:
        candidate = Path(path)
        return candidate if candidate.is_absolute() else root / candidate

    missing = [path for path in files if not _resolved(path).exists()]
    if missing:
        return {
            "state": "pending",
            "observed": "absent",
            "detail": (
                f"{len(missing)} of {len(files)} declared paths are not there "
                f"yet: {', '.join(missing)}"
            ),
        }
    return {
        "state": "met",
        "observed": "present",
        "detail": f"all {len(files)} declared paths exist",
    }


def _run_wait_condition_probe(
    record: Mapping[str, Any], wait: Mapping[str, Any]
) -> dict[str, str]:
    """Answer a declared condition for a classifier row, as a tri-state.

    This is the classifier's reader, not the sweep's. Two things separate them,
    and both are deliberate:

    * A file condition is answered here by looking for each declared path, so
      the row can name the ones still missing, and this function returns before
      any argument vector runs. The vector the declaration derives is run by
      the sweep's reader in ``reckon/crew/resumption.py`` -- the reader that
      decides whether a park lifts -- and not here.
    * A vector that prints nothing and exits is a terminal state only for the
      sweep's reader, which falls back to ``exit:<code>`` as the worker
      protocol documents. Here an empty answer is ``unknown``: a classifier row
      is read by a person, and reporting a state the probe never printed would
      have the row assert more than the probe said.

    The two are therefore not one implementation under two names, and a caller
    must not treat them as interchangeable: a verdict from here reaches a
    reader, a verdict from the sweep's reader lifts a run.
    """
    files = [str(path) for path in (wait.get("files") or ())]
    if files:
        return _wait_file_condition_observation(record, files)
    worktree = Path(str(record.get("worktree") or "."))
    try:
        completed = subprocess.run(
            list(wait.get("probe") or ()),
            cwd=worktree if worktree.is_dir() else None,
            text=True,
            capture_output=True,
            check=False,
            timeout=WAIT_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "state": "unknown",
            "observed": "unavailable",
            "detail": f"condition probe could not answer: {exc}",
        }
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    observed = lines[-1] if lines else "unavailable"
    if completed.returncode != 0 or not lines:
        return {
            "state": "unknown",
            "observed": observed,
            "detail": (
                f"condition probe did not answer successfully; exit "
                f"{completed.returncode}"
            ),
        }
    candidates = {observed.casefold()}
    candidates.update(
        line.split(maxsplit=1)[0].rstrip("+").casefold() for line in lines
    )
    terminal = {str(value).strip().casefold() for value in wait.get("terminal") or ()}
    if candidates & terminal:
        return {
            "state": "met",
            "observed": observed,
            "detail": f"condition probe reported terminal state {observed!r}",
        }
    return {
        "state": "unknown",
        "observed": observed,
        "detail": (
            f"condition probe reported {observed!r}, which matches no declared "
            "terminal state"
        ),
    }


_WAIT_HORIZON_FIELDS = (
    "wait_expected_seconds",
    "wait_expected",
    "wait_horizon",
)
_WAIT_DECLARATION_SCALAR_FIELDS = (
    *_WAIT_HORIZON_FIELDS,
    "wait_started_at",
)


def _unquote_wait_declaration_scalar(value: Any) -> str:
    """Return a scalar value after removing one matched pair of quotes."""
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    return text


def _wait_expected_seconds(
    manifest_data: Mapping[str, Any], *, default_seconds: int
) -> tuple[int, str]:
    """Read a declared wait horizon, retaining the existing default if absent."""
    field = ""
    value: Any = None
    for candidate in _WAIT_HORIZON_FIELDS:
        if candidate in manifest_data:
            field = candidate
            value = manifest_data.get(candidate)
            break
    if not field:
        return int(default_seconds), ""
    try:
        if field == "wait_expected_seconds" and not isinstance(value, str):
            seconds = int(value)
        else:
            seconds = parse_duration(_unquote_wait_declaration_scalar(value))
    except (CrewError, TypeError, ValueError):
        return int(default_seconds), f"readable positive {field}"
    if seconds <= 0:
        return int(default_seconds), f"positive {field}"
    return seconds, ""


def _wait_condition_declares_no_wait(condition: str) -> bool:
    """True when the condition's own prose says there is nothing to wait on.

    A worker told to write its manifest before starting long output writes its
    first one at orientation, and a healthy worker offered wait fields at that
    moment fills them with a note about where it is rather than what is awaited.
    The same happens later and on purpose: a worker recording a durable
    checkpoint before a long compose step writes the fields deliberately and
    says so in the condition. Recorded notes read: a condition opening with the
    word ``none`` ("none - this is an interim checkpoint, not a held wait"),
    read here exactly as the changed-paths prose-none rule reads that word — the
    sentence must *open* with it, so a real condition that merely mentions
    ``none`` later is left alone; the phrase written while the condition is
    still unestablished ("exploring; not yet set"); and a sentence stating in
    plain English that nothing is being awaited, in either wording a worker
    reached for: "no external condition is awaited; this is an interim progress
    record written before the report is composed", and the ledger's own
    "no external resource is awaited - this first write records orientation
    before the first edit". Every one of those declared its own absence, and
    each was escalated anyway on the presence of the field alone.
    """
    text = condition.strip()
    if re.match(r"none(?:\s|$)", text, re.IGNORECASE):
        return True
    if re.search(r"\bnot yet set\b", text, re.IGNORECASE):
        return True
    if re.search(r"\bno\s+external\b[^.;]{0,60}\bawaited\b", text, re.IGNORECASE):
        return True
    return bool(
        re.search(r"\bnothing\s+(?:is\s+|to\s+be\s+)?awaited\b", text, re.IGNORECASE)
    )


# A probe that cannot report a pending state is not a probe: the null command
# succeeds whatever is happening and prints nothing, so a declaration resting on
# it reads terminal on every sweep, however completely the other fields are
# filled in.
_WAIT_PROBE_NO_OP_COMMANDS = frozenset({"true", ":", "exit"})

# A probe also has to be able to differ. ``echo pending`` prints a constant and
# ``git rev-parse HEAD`` reports the worker's own tree; neither can change
# between sweeps however the awaited work is doing, so both are satisfied
# unconditionally and a wait resting on one tests nothing. What makes a probe
# able to differ is a reference to something outside the worker, and the
# references a wait actually rests on are a job id, a pid, a port and a path.
# That reference is resolved rather than inferred from the characters the
# vector happens to carry: ``git rev-parse HEAD2`` carries a digit and still
# names nothing a sweep could read, so a token counts only when its place in
# the vector gives it a kind. A job id and a port are named by the flag they
# follow (``squeue -j 1271081``, ``ssh -p 2222``) or, for the commands that
# take them by position, by the operand's place (``nc -z a-host 8765``); a pid
# is a numeric operand of a process command; and a path is a token that
# resolves on the filesystem, the command token included when it is given as a
# path rather than as a bare name. The file-test commands are read for an
# operand as well, because ``test -f checkpoints/done`` and ``test -f DONE``
# are the same kind of wait and the derived probe of a ``wait_file`` condition
# is exactly this shape: there the operand is the path being waited for, so it
# need not exist yet.
_WAIT_FILE_TEST_COMMANDS = frozenset({"test", "["})
_WAIT_PROBE_JOB_FLAGS = frozenset({"-j", "--job", "--jobid", "--job-id"})
_WAIT_PROBE_PID_FLAGS = frozenset({"--pid"})
_WAIT_PROBE_PORT_FLAGS = frozenset({"--port", "--local-port"})
# ``-p`` names a pid or a port depending on the command it belongs to.
_WAIT_PROBE_SHORT_FLAG = "-p"
_WAIT_PROBE_PROCESS_COMMANDS = frozenset({"kill", "ps", "pgrep", "pkill"})
_WAIT_PROBE_SHELL_COMMANDS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
_WAIT_PROBE_PORT_COMMANDS = frozenset(
    {"nc", "netcat", "ncat", "ss", "netstat", "curl", "lsof", "telnet", "ssh"}
)
_WAIT_PROBE_COUNT = re.compile(r"[0-9]+")
_WAIT_PROBE_PORT_RANGE = (1, 65535)
# A shell-free vector gives a printer no way to read anything: ``echo`` and
# ``printf`` write their own arguments whatever is happening outside the
# worker, so a probe spelled with one is a command that cannot fail even when
# its text mentions a path. A probe that needs a shell to reach the scheduler
# keeps its ``bash -lc`` head, which is not a printer and is read by the
# reference rule instead.
_WAIT_PROBE_PRINTER_COMMANDS = frozenset({"echo", "printf"})


def _wait_probe_reference_kind(probe: Sequence[str]) -> str | None:
    """The kind of external reference the probe names, or None.

    The kinds a wait rests on are a job id, a pid, a port and a path, and one
    is named here only when the vector places a token as that kind: a value
    after a flag that names it (``-j 1271081``, ``-p 2222``), a numeric operand
    of a command that takes one by position (``nc -z a-host 8765``,
    ``kill -0 424242``), an operand of a file-test command, or a token that
    resolves on the filesystem. Anything else names nothing a sweep can read,
    however many digits, slashes, dollars or backticks its text carries:
    ``git rev-parse HEAD2`` resolves to no kind, and that is the difference
    between a reference and a character.

    Two shapes are read through rather than taken at face value. A shell
    keeps its string argument in one token, so a probe spelled
    ``bash -lc 'q=$(squeue -h -j 1274028); …'`` reaches the job flag only
    after the string is read as the shell vector it is. And the path kind is
    resolved against the filesystem, so a token counts only while it exists —
    except the operand of a file-test command, which is the path being waited
    *for*: ``test -f DONE`` is satisfied by DONE appearing, so that operand
    need not exist yet.
    """
    command = Path(str(probe[0])).name
    words = [str(item) for item in probe[1:]]
    if command in _WAIT_PROBE_SHELL_COMMANDS:
        words = [word for item in words for word in _wait_probe_shell_words(item)]
    takes_pids = command in _WAIT_PROBE_PROCESS_COMMANDS
    takes_ports = command in _WAIT_PROBE_PORT_COMMANDS
    tokens = [command, *words]
    for index, token in enumerate(tokens):
        name, separator, inline = token.partition("=")
        value = (
            inline
            if separator
            else (tokens[index + 1] if index + 1 < len(tokens) else "")
        )
        if name in _WAIT_PROBE_JOB_FLAGS and _wait_probe_names_a_count(value):
            return "job"
        if name in _WAIT_PROBE_PID_FLAGS and _wait_probe_names_a_count(value):
            return "pid"
        if name in _WAIT_PROBE_PORT_FLAGS and _wait_probe_names_a_port(value):
            return "port"
        if name == _WAIT_PROBE_SHORT_FLAG:
            if takes_pids and _wait_probe_names_a_count(value):
                return "pid"
            if takes_ports and _wait_probe_names_a_port(value):
                return "port"
        if name in _WAIT_FILE_TEST_COMMANDS and any(
            _wait_probe_names_an_operand(item) for item in tokens[index + 1 :]
        ):
            return "path"
        if token.isdigit():
            if takes_pids:
                return "pid"
            if takes_ports and _wait_probe_names_a_port(token):
                return "port"
        if Path(token).exists():
            return "path"
    return None


def _wait_probe_shell_words(text: str) -> list[str]:
    """The words a shell argument is spelled with, stripped of its punctuation.

    A shell reaches its reference through a string rather than through a token
    of the vector, so the string is read as the words it is: separators and
    control punctuation split it, and quoting and expansion characters are
    dropped from each word's edges. A job id written ``1274028);`` therefore
    reads as the number it is, and the flag before it still names the kind.
    """
    words: list[str] = []
    for word in re.split(r"[\s;|&()]+", text):
        cleaned = word.strip("'\"`${}<>")
        if cleaned:
            words.append(cleaned)
    return words


def _wait_probe_names_an_operand(item: str) -> bool:
    """True when a token is an operand rather than a flag."""
    return bool(item.strip()) and not item.startswith("-")


def _wait_probe_names_a_count(value: str) -> bool:
    """True when a value is a plain number, as job ids and pids are written."""
    return bool(_WAIT_PROBE_COUNT.fullmatch(value.strip()))


def _wait_probe_names_a_port(value: str) -> bool:
    """True when a value is a number a port could be."""
    if not _wait_probe_names_a_count(value):
        return False
    low, high = _WAIT_PROBE_PORT_RANGE
    return low <= int(value.strip()) <= high


def _wait_probe_cannot_fail(probe: Sequence[str]) -> bool:
    """True when a present probe's result cannot differ between sweeps.

    The discriminator is whether anything the probe reads lives outside the
    worker: a job id, a pid, a port or a path. A printer is refused outright
    because in a shell-free vector it can only echo its own text, and any
    other probe must resolve one of those references through
    :func:`_wait_probe_reference_kind`: ``["squeue", "-h", "-j", "1271081"]``
    resolves a job id; ``["echo", "pending"]`` and ``["git", "rev-parse",
    "HEAD2"]`` resolve nothing, run whatever is happening, and read the same on
    every sweep. Only a probe that is actually present is read this way: an
    absent one is the incomplete-declaration case the reader already reports,
    so the two outcomes stay distinguishable.
    """
    if not probe:
        return False
    command = Path(str(probe[0])).name
    if command in _WAIT_PROBE_NO_OP_COMMANDS:
        return True
    if command in _WAIT_PROBE_PRINTER_COMMANDS:
        return True
    return _wait_probe_reference_kind(probe) is None


def _wait_probe_is_a_no_op(probe: Sequence[str]) -> bool:
    """True when a present probe can report nothing but success.

    The null command prints nothing at all, which makes it the extreme member
    of the probes that cannot fail; the wider family is read by
    :func:`_wait_probe_cannot_fail`, and this reading keeps its own meaning so
    a declaration resting on ``["true"]`` still reduces to no declaration at
    all.
    """
    if not probe:
        return False
    return Path(str(probe[0])).name in _WAIT_PROBE_NO_OP_COMMANDS


# A state that means the awaited work has not finished is never a terminal
# state. A job scheduler spells three of them, and a probe may invent its own
# wording for the same situation on a branch it takes only while the job is
# still in the queue — which is how a wait declaring RUNNING terminal reads as
# satisfied on every sweep of a job that has not started. The fixed spellings
# are refused outright; the probe's own live branch is read out of its text,
# because a renamed live state is exactly what the fixed spellings cannot see.
_WAIT_LIVE_STATE_TOKENS = frozenset({"running", "pending", "waiting"})

# The variable a probe fills from `squeue`, whose non-empty test guards the
# branch it takes while the job is still in the queue.
_SQUEUE_GUARDED_VAR = re.compile(
    r"(?P<var>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*\$\(\s*squeue\b"
)
_WAIT_BRANCH_STOP = re.compile(r"\b(?:elif|else|fi)\b")


def _emitted_tokens(branch: str) -> list[str]:
    """Bare word tokens a shell branch prints, one statement at a time."""
    tokens: list[str] = []
    for statement in re.split(r"[;\n]|&&|\|\||\b(?:then|do)\b", branch):
        words = [word.strip("\"'") for word in statement.split()]
        if not words or words[0] not in {"echo", "printf"}:
            continue
        tokens.extend(word for word in words[1:] if word and not word.startswith("-"))
    return tokens


def _probe_live_state_tokens(probe: Sequence[str]) -> list[str]:
    """Tokens a probe prints from a branch guarded by a non-empty squeue result.

    Those tokens describe a job that is still in the queue whatever the wait
    declaration calls the state, so any of them listed as terminal is the
    declaration contradicting its own probe.
    """
    text = " ".join(str(item) for item in probe)
    tokens: list[str] = []
    for assignment in _SQUEUE_GUARDED_VAR.finditer(text):
        var = re.escape(assignment.group("var"))
        guard = re.search(
            r"\[\s*-n\s+\"?(?:\$\{?" + var + r"\}?|\$\{\s*" + var + r"\s*\})\"?\s*\]"
            r"|\[\s*\"?\$\{?" + var + r"\}?\"?\s*\]",
            text,
        )
        if guard is None:
            continue
        branch = text[guard.end() :]
        stop = _WAIT_BRANCH_STOP.search(branch)
        if stop:
            branch = branch[: stop.start()]
        tokens.extend(_emitted_tokens(branch))
    return tokens


def _wait_terminal_names_a_live_state(
    terminal: Sequence[str], probe: Sequence[str]
) -> str:
    """Name the terminal value the probe reports while the awaited job is live.

    Empty when nothing in the terminal list names a live state, which is the
    only case a wait declaration is read at all.
    """
    if not terminal or not probe or _wait_probe_is_a_no_op(probe):
        return ""
    emitted = _probe_live_state_tokens(probe)
    for value in terminal:
        spelled = str(value).strip()
        if spelled.casefold() in _WAIT_LIVE_STATE_TOKENS:
            return spelled
        if spelled and spelled in emitted:
            return spelled
    return ""


def _wait_declaration_signature(
    condition: str,
    probe: Sequence[str],
    terminal: Sequence[str],
    resume_brief: str,
) -> str:
    """Identity of a wait declaration, without its file's modification time.

    A lift is keyed to what the declaration asks for, not to when the file was
    written. A worker that re-parks rewrites its manifest and so advances the
    mtime, which made every re-park a brand-new condition and re-lifted a wait
    whose terminal state had not actually ended anything — the loop that
    resumed one run thirty times. The same declaration arriving twice is the
    same condition; only an edit to it is a new one.
    """
    material = json.dumps(
        {
            "condition": condition,
            "probe": [str(item) for item in probe],
            "terminal": [str(item) for item in terminal],
            "resume_brief": resume_brief,
        },
        sort_keys=True,
    )
    return "wait:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


# The shapes a run's own output takes inside one run directory: the initial
# stream, one per resume turn, and one per lane change. A run that resumes or
# changes lane keeps writing to a new file, so the newest of these is its
# current activity; the file the pointer first named goes stale the moment that
# happens.
RUN_STREAM_GLOBS = ("stream.jsonl", "resume-*.jsonl", "lane-change-*.jsonl")


def stream_paths_newest_first(
    run_dir: str | Path, *, include: Iterable[str | Path] = ()
) -> list[Path]:
    """Every non-empty stream in a run directory, newest write first.

    Empty files are skipped rather than counted: a stream a process has opened
    but written nothing to is not activity, and reading its mtime would report
    a resumed run as producing output the instant its file was created. The
    pointer's own log path joins the candidates when a caller supplies it, so a
    record whose current stream sits outside the run directory is still read.
    """
    directory = Path(run_dir)
    candidates: list[Path] = []
    for pattern in RUN_STREAM_GLOBS:
        candidates.extend(directory.glob(pattern))
    for extra in include:
        if str(extra or ""):
            candidates.append(Path(str(extra)))
    written: dict[Path, float] = {}
    for path in candidates:
        try:
            if path.stat().st_size > 0:
                written[path] = path.stat().st_mtime
        except OSError:
            # A directory entry can vanish between the glob and the stat; that
            # is no stream rather than an error.
            continue
    return sorted(written, key=lambda path: (written[path], str(path)), reverse=True)


def newest_stream(
    run_dir: str | Path, *, include: Iterable[str | Path] = ()
) -> tuple[Path, float] | None:
    """The newest non-empty stream a run has, and when it was written.

    None means the run holds no readable, non-empty stream, so a caller can
    tell "no measurement taken" from "an infinitely old one". This is the one
    reader for both questions a stream answers — how long the run has been
    quiet, and which session it is continuing — so the stall classifier and the
    session lookup cannot disagree about which stream is current.
    """
    paths = stream_paths_newest_first(run_dir, include=include)
    if not paths:
        return None
    newest = paths[0]
    try:
        return newest, newest.stat().st_mtime
    except OSError:
        return None


def _request_input_tokens(event: Mapping[str, Any]) -> int | None:
    """The charged input one stream record reports for a single request.

    Only per-request records answer: an ``assistant`` record's own
    ``message.usage`` on the claude grammar, and the ``turn.completed`` usage
    on the codex grammar, which is that grammar's only usage record. A claude
    ``result`` record is deliberately not consulted — its modelUsage is a
    run-length aggregate of un-cached input, which grows with the run's length
    rather than describing the context a resumed turn would re-send.
    """
    kind = event.get("type")
    if kind == "assistant":
        message = event.get("message")
        usage = message.get("usage") if isinstance(message, Mapping) else None
    elif kind == "turn.completed":
        usage = event.get("usage")
    else:
        return None
    charged = _charged_input_from_usage(usage)
    if isinstance(charged, bool) or not isinstance(charged, (int, float)):
        return None
    return int(charged)


def _last_recorded_input_tokens(run_id: str, record: Mapping[str, Any]) -> int | None:
    """The session's last recorded request input, from the run's own streams.

    Streams are read newest write first, and the first stream carrying a
    per-request figure answers: a resume attempt that died before reaching the
    model leaves a stream with no usage at all, and it must not blank the count
    an earlier attempt recorded. None means no stream carried a figure, which
    refuses nothing — an unmeasured session is not a session known to be too
    large.
    """
    include = [record.get("log_path")] if record.get("log_path") else []
    for path in stream_paths_newest_first(runs.run_dir(run_id), include=include):
        measured: int | None = None
        try:
            handle = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if not isinstance(event, Mapping):
                    continue
                measured_value = _request_input_tokens(event)
                if measured_value is not None:
                    measured = measured_value
        if measured is not None:
            return measured
    return None


def _lane_input_window(
    record: Mapping[str, Any],
    backend: Mapping[str, Any],
    config: Mapping[str, Any] | None,
) -> int | None:
    """The input window the run's lane publishes, or None when it publishes none.

    Two figures narrow the gate, and the smaller wins for the same reason the
    dispatch-time context-fit check takes the smaller: the declared
    ``usable_input_window`` is what the lane says its endpoint accepts — already
    net of the launcher's output reservation, so a count above it plus the
    reservation exceeds the engine cap — while an ``effective_input_window`` is
    the lowest input the lane's endpoint is recorded refusing. A lane declaring
    neither the run record nor the configuration answers None, and an unstated
    window refuses nothing.
    """
    backends = (config or {}).get("backends")
    name = str(record.get("backend") or "")
    configured = backends.get(name) if isinstance(backends, Mapping) else None
    figures: list[int] = []
    for source in (backend, configured):
        if not isinstance(source, Mapping):
            continue
        for key in ("usable_input_window", "effective_input_window"):
            value = source.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if int(value) > 0:
                figures.append(int(value))
    return min(figures) if figures else None


def resume_window_refusal(
    run_id: str,
    record: Mapping[str, Any],
    *,
    backend: Mapping[str, Any],
    config: Mapping[str, Any] | None = None,
) -> CrewError | None:
    """The refusal a resume owes a session the lane's window cannot hold.

    A resumed turn re-sends the session's whole context, so a session that has
    grown past the lane's input window dies at launch: the endpoint refuses the
    prompt after the attempt file is already open, and the opened attempt then
    makes the delivered manifest read stale to promotion. The count is the
    run's own last recorded request input; the window is the lane's published
    input window. The gate is consulted before anything is written, so a
    refused resume leaves no attempt behind and touches neither the run's
    classification nor its manifest, and the remedy is a fresh repair node,
    because the session itself cannot be continued on this lane.
    """
    window = _lane_input_window(record, backend, config)
    if window is None:
        return None
    count = _last_recorded_input_tokens(run_id, record)
    if count is None or count <= window:
        return None
    lane = str(record.get("backend") or "unknown")
    return CrewError(
        f"run {run_id!r} is not resumed: its session last carried {count} input "
        f"tokens, above backend {lane!r}'s published input window of {window} "
        "tokens, so the resumed turn would die at the endpoint's context limit "
        "and leave an open attempt behind. Dispatch a fresh repair node instead "
        "of resuming this session."
    )


# An engine writes one of these when a turn has run to its own conclusion, so
# the process ending after it is an end of turn rather than a death mid-turn.
# The two read differently on the pane because their remedies differ: a turn
# that ended is continued, while a death mid-turn needs its cause read first.
STREAM_RESULT_RECORD_TYPE = "result"

# The tail a last-record read takes from a stream. The answer is one line, and
# the watcher asks this per run per snapshot, so the whole file — megabytes on a
# long run — is never read for it. It is a floor rather than a limit: a final
# record taller than the window leaves the window inside that one record with no
# line boundary in it, and the read then reaches further back until one is in.
_STREAM_TAIL_BYTES = 64 * 1024


def _last_record_type_in(chunk: bytes) -> str | None:
    """The type of the last complete record in a chunk of a stream.

    Lines are read backwards, so the answer is the record the file ends on
    rather than the one it starts with. A line that does not parse is passed
    over: a trailing partial write is not evidence that a record completed, and
    a reader that took it for one would name an end the run never reached.
    """
    for raw in reversed(chunk.splitlines()):
        text = raw.strip()
        if not text:
            continue
        try:
            event = json.loads(text)
        except (TypeError, ValueError):
            continue
        if not isinstance(event, Mapping):
            continue
        record_type = event.get("type")
        if isinstance(record_type, str) and record_type:
            return record_type
    return None


def _newest_stream_last_record_type(record: Mapping[str, Any]) -> str | None:
    """The type of a run's newest stream's last complete record.

    None answers "no last record to read": no stream, one that cannot be read,
    or one whose tail holds no complete record. A trailing partial write is
    skipped rather than parsed — an engine appending a record is not evidence
    that the record completed — and a caller therefore never reads "could not
    tell" as a particular type.

    The window is a floor because a record can be taller than it. A chunk that
    begins inside a record holds only a fragment of the file's last record when
    that record is taller than the window, and a fragment parses as nothing, so
    the read reaches further back until the chunk begins where a record begins
    and the record ending the file is read whole. One window answers the common
    case — a last record of a few hundred bytes behind any length of stream —
    because such a chunk holds that record entire and the read stops there.
    """
    found = _record_newest_stream(record)
    if found is None:
        return None
    try:
        with found[0].open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            window = _STREAM_TAIL_BYTES
            while True:
                start = max(0, size - window)
                handle.seek(start)
                chunk = handle.read()
                record_type = _last_record_type_in(chunk)
                if record_type is not None or start == 0:
                    return record_type
                # Nothing in the chunk read as a record. A chunk beginning at a
                # record boundary holds only complete lines, so the stream has
                # no record to read here and reaching further back would answer
                # the same; a chunk beginning inside one is the tail of a
                # record larger than the window, which the next read must
                # contain.
                handle.seek(start - 1)
                if handle.read(1) == b"\n":
                    return None
                window *= 4
    except OSError:
        return None


# The record an engine writes when the model answers a turn. It is the run's
# own first evidence that work is happening rather than that a launch was
# started: the pointer's phase is a label only ``observe`` advances, while this
# record lands the moment the worker's first turn is under way.
STREAM_ASSISTANT_RECORD_TYPE = "assistant"


def _stream_holds_assistant_record(path: Path) -> bool:
    """Whether a stream holds at least one complete assistant record.

    Read forwards and stopped at the first match, because a worker's first
    assistant turn lands within its first few records: the scan answers after a
    few kilobytes on a stream that grows to megabytes over a long run. A line
    that does not parse is passed over, exactly as the tail read passes over
    one, so a trailing partial write is never read as a record that completed.
    """
    try:
        with path.open("rb") as handle:
            for raw in handle:
                text = raw.strip()
                if not text:
                    continue
                try:
                    event = json.loads(text)
                except (TypeError, ValueError):
                    continue
                if (
                    isinstance(event, Mapping)
                    and event.get("type") == STREAM_ASSISTANT_RECORD_TYPE
                ):
                    return True
    except OSError:
        return False
    return False


def _newest_stream_shows_work(record: Mapping[str, Any]) -> bool:
    """Whether a run's newest stream carries an assistant record.

    The newest stream is the shared reader's answer, so this agrees with the
    stall clock about which file is the run's current one; a run that resumed
    or changed lane wrote a newer file than the one it started with. False
    answers "no such record to read" — no stream, one that cannot be read, or
    one holding no assistant turn — so a caller never reads an absent answer
    as work.
    """
    found = _record_newest_stream(record)
    if found is None:
        return False
    return _stream_holds_assistant_record(found[0])


def _process_exit_reason(record: Mapping[str, Any], last_record_type: str) -> str:
    """Why a dead worker's row says the process exited, in its records' terms.

    The run directory's exit record is the supervisor's account of the end and
    outranks the bare pid, so a kill is named by its signal and a clean end by
    its code; a run with no recorded exit says so rather than inventing one.
    The last record type travels beside it, because a stream that carried no
    result record is what makes the end a death mid-turn rather than a turn
    that finished.
    """
    exit_record = _run_exit_record(record)
    if exit_record is not None:
        end = (
            f"the worker process {_exit_record_end_phrase(exit_record)} "
            f"(recorded at {exit_record.get('exited_at') or 'an unrecorded moment'})"
        )
    else:
        end = "the worker process is gone with no recorded exit"
    return (
        f"{end} before the run completed; the stream's last record is "
        f"{last_record_type}, so the turn did not end"
    )


def _run_directory(record: Mapping[str, Any] | dict[str, Any]) -> Path:
    """The directory holding a run's streams, from its id or its log path."""
    run_id = str(record.get("run_id") or "")
    if run_id:
        return Path(runs.run_dir(run_id))
    return Path(str(record.get("log_path") or ".")).parent


# The run directory's own record of the worker's pid. A supervised launch
# writes the supervisor's pid on the pointer and the worker's pid here, so the
# two answer different questions: the pointer pid says whether the launcher
# still lives, this record says whether the work does.
WORKER_RECORD_NAME = "worker.json"

# The phases a run holds before its worker has been spawned and observed. A run
# that died in one of them recorded no worker and no exit, so its pointer pid
# going silent is not proof that any work stopped.
_PRE_SPAWN_PHASES = frozenset({"starting", "launching", "launcher", "dispatching"})

# A launch cut off between composing its record and spawning its worker leaves
# a pointer holding a pre-spawn phase and nothing else: no pid, no worker
# record, no stream and no launch log in its run directory. Nothing about it is
# in flight — there is no process to observe and no session to resume — and
# nothing about it ends either, so it holds whatever claim it took, a lane or a
# review another run is told is covered, for as long as the pointer lives. Past
# this bound the absence of any launch evidence is itself the reading and the
# pointer is a stranded launch. The bound matches the quiet window a dispatched
# run is given before it reads as stalled, so a launch that never spawned is
# called stranded on the same clock as one that spawned and went silent.
STRANDED_LAUNCH_BOUND_SECONDS = 900


def _observed_phase(
    phase: str,
    *,
    alive: bool | None,
    worker_alive: bool | None,
    worker_record_names_pid: bool,
    ended_exit: Mapping[str, Any] | None,
    manifest_status: str,
    commits_beyond_base: int,
    stream_shows_work: bool = False,
) -> str:
    """The phase a run's own evidence supports, not the last writer's label.

    A pointer's phase is written by the launcher: a supervisor sets it at spawn,
    and a run whose launch was interrupted can keep a pre-spawn label for its
    whole life. Where the stored phase is still one of those labels, the run's
    own evidence decides instead — a live worker record or retained commits
    show the launch got past starting. A live supervisor or stream alone does
    not: the supervisor may still be between admission and worker spawn, and a
    stream can be inherited from an earlier attempt. A terminal verdict on a
    gone process shows it finished. With no evidence at all the label stands:
    nothing has happened yet, and inventing an advance would be as wrong as
    inventing an end.

    A delivered manifest is itself evidence the launch got past starting: a
    worker cannot report a verdict before it has run. That holds whatever the
    process table says, so a delivered report never falls back to the
    launcher's pre-spawn label, which would render a finished run as
    dispatched. A process still reported alive outranks the report — the
    classifier reads that pairing as a deferred outcome, not a finished run —
    so the phase is working then.

    A worker record answers for every phase it was read in, not only while its
    process lives: the supervisor writes it once the worker is spawned, so its
    presence means the launch happened whatever the process table now says. An
    answer of "gone" is therefore evidence of the advance too, and only a
    record that names no pid leaves the label standing. Reading the answer as
    proof only while it was ``True`` let the phase fall back to the pre-spawn
    label the moment the worker exited, so a run that had already been reported
    working was reported dispatched again. Presence is the launch evidence and
    is read without the host gate that liveness carries: whether the pid can be
    probed *here* decides only whether the worker is alive now, while a record
    sitting in the run's directory proves the launch happened wherever it did,
    so a run whose launching host is another machine still advances past
    starting and is never rendered dispatched for it.

    An assistant record in the run's newest stream answers the same way, and it
    is the evidence left when nothing else has been written: the phase advances
    only when ``observe`` folds the stream, so a worker that has been thinking
    and editing for an hour keeps the label its launcher set until a reader
    happens to run one. The record is the worker's own first turn rather than a
    stream's mere existence, which an earlier attempt can leave behind, and it
    is consulted last so the stronger answers above decide first.
    """
    if phase not in _PRE_SPAWN_PHASES:
        return phase
    if manifest_status in TERMINAL_MANIFEST_STATUSES:
        return "working" if alive is True else "complete"
    if ended_exit is not None:
        return "complete"
    if worker_record_names_pid or commits_beyond_base:
        return "working"
    if stream_shows_work:
        return "working"
    return phase


def _carries_orientation_write(text: str, data: Mapping[str, Any]) -> bool:
    """Whether a manifest body is a worker's orientation write and nothing more.

    Every dispatch records where it is working before it has a status, so a body
    carrying the orientation keys and no status line is a run one minute into its
    life rather than a delivery that failed to declare a verdict. The raw body is
    read because the manifest reader requires a status key. A parsed body is
    accepted too, so a reader that once tolerated a missing status still lands
    here.

    A body with a ``status:`` line is not this case at all: whatever it says,
    the file has moved past its orientation write.
    """
    if data and data.get("orientation_worktree"):
        return not str(data.get("status") or "").strip()
    if "orientation_worktree" not in text:
        return False
    return not any(line.startswith("status:") for line in text.splitlines())


def _orientation_write_of_a_run_in_motion(
    record: Mapping[str, Any],
    *,
    alive: bool | None,
    worker_alive: bool | None,
    manifest_text: str,
    manifest_data: Mapping[str, Any],
) -> bool:
    """Whether a run holding only its orientation write is working right now.

    The file alone cannot answer this: a stub and the first minute of a turn are
    the same bytes, and the difference is whether the worker is still writing
    them. Liveness is that difference, and it must be a positive answer — a
    worker whose process is gone left its stub behind as the missing verdict it
    may well be, and the word for that stands. Beside liveness the reading wants
    the evidence the phase derivation uses for the same run, so the two surfaces
    cannot disagree: a worker record naming a pid, or an assistant turn in the
    run's newest stream, says the launch got past starting.
    """
    if alive is not True:
        return False
    if not _carries_orientation_write(manifest_text, manifest_data):
        return False
    if worker_alive is True:
        return True
    return _newest_stream_shows_work(record)


def _worker_record(record: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The run's own worker record, or None when none was written or readable."""
    try:
        data = json.loads((_run_directory(record) / WORKER_RECORD_NAME).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, Mapping) else None


def _worker_record_liveness(record: Mapping[str, Any]) -> bool | None:
    """Whether the worker pid the run recorded for itself is still running.

    None answers "nothing to ask": no worker record, an unreadable one, or one
    naming no pid. Reported as its own fact rather than folded into the
    pointer's answer, because a supervisor that has exited before its worker
    takes the pointer pid with it while the work continues.

    The worker record carries no host of its own, so its pid is meaningful only
    on the machine that issued it: a number live here is no evidence about a run
    launched *elsewhere*, and reading it as one hands a foreign run a life this
    host cannot support. The read is therefore refused only for a run whose own
    launching host names a different machine. An unnamed host is not refused:
    the resumed-attempt deferral this fact exists for reads a run whose pointer
    cannot be resolved here, and a pointer written before the launching host was
    recorded names none, so refusing it would leave exactly the resumed run the
    deferral was built for with no liveness at all. The pid itself is decided by
    ``runs.record_process_alive``, which owns the start-tick comparison that
    keeps a recycled number from reading as the registered worker.
    """
    data = _worker_record(record)
    if data is None:
        return None
    if _record_is_known_foreign(record):
        return None
    return runs.record_process_alive(data, process_alive)


def _worker_launched_after_manifest(record: Mapping[str, Any], manifest: Path) -> bool:
    """Whether the run's current worker started after the manifest was written.

    A resumed attempt reuses its run directory, so the manifest beside the
    pointer may be the verdict a previous turn left. Its own launch time is the
    fact that separates the two: a worker that started after the manifest was
    last written cannot have written it, so a terminal status the file still
    carries belongs to the superseded attempt rather than to the worker running
    now. Unreadable evidence answers False — the reading never invents a launch
    it did not observe.
    """
    data = _worker_record(record)
    if data is None:
        return False
    raw = str(data.get("launched_at") or "").strip()
    if not raw:
        return False
    launched = parse_utc(raw)
    if launched is None:
        return False
    try:
        written = manifest.stat().st_mtime
    except OSError:
        return False
    return launched.timestamp() > written


def _int_or_none(value: Any) -> int | None:
    """A recorded number, or None when the record carries none to read."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _worker_record_pid(record: Mapping[str, Any]) -> int | None:
    """The pid the run's own worker record names, or None when it names none."""
    data = _worker_record(record)
    if data is None:
        return None
    return _int_or_none(data.get("pid"))


def _process_children(pid: Any) -> list[int] | None:
    """The pids the kernel lists as a process's own children, or None.

    The kernel's child list is read rather than every process's parent field,
    because one worker's children cost a read each while a host-wide scan costs
    a read per process on the machine. Every thread's list is read: a child may
    have been forked by a thread rather than by the process's first one. None
    answers "could not be read" — the process has gone, or this kernel does not
    publish the list — and is not the same answer as a process with no
    children, so a caller never reports an absence it did not observe.
    """
    number = _int_or_none(pid)
    if number is None:
        return None
    tasks = Path("/proc") / str(number) / "task"
    try:
        thread_ids = [entry.name for entry in tasks.iterdir()]
    except OSError:
        return None
    children: list[int] = []
    read_any = False
    for thread_id in thread_ids:
        try:
            listed = (tasks / thread_id / "children").read_text()
        except OSError:
            continue
        read_any = True
        children.extend(int(token) for token in listed.split() if token.isdigit())
    return children if read_any else None


def _live_descendant(pid: Any) -> bool:
    """Whether a running process has a running process under it.

    A worker waiting on a job — a build, a scheduler reservation, a probe — is
    a worker whose own stream says nothing while the child does the work, so a
    live child is the evidence on this host that something is still moving.
    One level of the child list answers the whole question: a process's
    children are reparented the moment it exits, so a running grandchild is
    always listed under a running child, and a child the kernel has finished is
    a table entry its parent has not yet reaped rather than a process running
    anything.
    """
    children = _process_children(pid)
    if children is None:
        return False
    return any(runs.process_alive(child) is True for child in children)


def _process_reading(
    alive: bool | None,
    *,
    liveness_proven: bool,
    exit_record: Mapping[str, Any] | None,
) -> str:
    """The three-way process state a row states in words.

    One reading, because three callers quote it and they must not disagree: a
    stalled row says which situation its stall is, and the resume offer asks
    whether the end was observed before it is made. A life is only the first of
    the three; death is claimed only where something observed it — a pid checked
    on the reading host, which the row's own ``liveness_proven`` records, or the
    supervisor's exit record, which survives a pointer nobody updated and a pid
    no other machine can look up. A stored answer carried because the launching
    host could not be shown to be this host is neither observation, and neither
    is no answer at all, so those read as unproven rather than as a death.
    """
    if alive is True:
        return "alive"
    if alive is False and (liveness_proven is True or exit_record is not None):
        return "process gone"
    return "liveness unknown"


def _record_newest_stream(record: Mapping[str, Any]) -> tuple[Path, float] | None:
    """A record's newest stream, through the shared reader."""
    return newest_stream(
        _run_directory(record), include=(record.get("log_path"),)
    )


def _run_stream_mtime(record: Mapping[str, Any]) -> float | None:
    """The newest write to any of the run's streams, or None when there is none.

    The stream is where an engine's own output lands, so its mtime is the one
    fact about a run that says it is producing something right now. Every
    stream a run has is considered, so a resumed or lane-changed run ages
    against the file it is writing now rather than the one it started with.
    Absent or unreadable is None rather than a zero: a run with no stream has
    taken no measurement, and a missing file must not read as infinitely stale.
    """
    found = _record_newest_stream(record)
    return found[1] if found is not None else None


# The run directory's own record of the current attempt's identity. A
# supervisor writes it before the attempt's worker can start, so it names the
# moment the attempt now running began even when the pointer predates the field.
ATTEMPT_RECORD_NAME = "attempt.json"


def _attempt_started_seconds(record: Mapping[str, Any]) -> float | None:
    """When the run's current attempt began, or None when nothing records it.

    The pointer carries ``attempt_started_at`` once a supervisor wrote it; a run
    whose pointer predates that field still names the same moment in the current
    attempt record the supervisor publishes beside it, so both are read. Absent
    from both means the run has no attempt clock, and its quiet time then falls
    back to the launch window a fresh dispatch gets.
    """
    raw = record.get("attempt_started_at")
    if not raw:
        try:
            marker = json.loads(
                (_run_directory(record) / ATTEMPT_RECORD_NAME).read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, ValueError):
            marker = None
        if isinstance(marker, Mapping):
            raw = marker.get("attempt_started_at")
    if not raw:
        return None
    started = parse_utc(str(raw))
    if started is None:
        return None
    return started.timestamp()


def _stranded_launch(record: Mapping[str, Any], *, now_seconds: float) -> bool:
    """Whether a launch that recorded nothing stopped happening past the bound.

    The phases a launch holds before it spawns are shared with a launch that is
    merely young, so the age alone cannot decide and neither can the phase:
    what separates the two is evidence. A spawned worker leaves a worker record,
    a stream, or a launch log in the run directory, so a pointer whose session
    wrote any of them is somewhere in flight whatever its phase says. With none
    of them written and the clock past the bound, there is nothing to wait for:
    the launch was cut off between composing the record and spawning the
    worker, and a pointer that holds a phase as though something were coming
    holds it forever.

    The clock is the current attempt's own start, which the launch composes on
    the pointer before spawning anything. A pointer naming no start has no
    launch clock to read and is left as one in flight.
    """
    if str(record.get("phase") or "") not in _PRE_SPAWN_PHASES:
        return False
    if record.get("pid"):
        return False
    if _worker_record(record) is not None:
        return False
    if _record_newest_stream(record) is not None:
        return False
    stderr_path = str(record.get("stderr_path") or "").strip()
    if stderr_path and Path(stderr_path).exists():
        return False
    started = _attempt_started_seconds(record)
    if started is None:
        return False
    return now_seconds - started > STRANDED_LAUNCH_BOUND_SECONDS


def _run_stream_quiet_seconds(
    record: Mapping[str, Any], *, now_seconds: float
) -> int:
    """Quiet time for a run, from its current attempt's own log and launch.

    Two clocks bound the reading, and the later of them decides. One is the
    newest stream the run has written: a resume or a superseded attempt writes
    a new file while the pointer keeps naming the old one, so the newest stream
    is the attempt's own output where one exists, and a fresh resume or lane
    change keeps the run working. The other is the attempt's launch, taken from
    the attempt record when a supervisor published one and otherwise from the
    same pointer-and-creation clock a fresh dispatch falls back to.

    A superseded attempt's stream must not age the run. A run resumed seconds
    ago reads ``stalled`` when its predecessor's stream is old and the resumed
    attempt has not written its own log yet, because the reading came from the
    attempt that already ended. Capping the stream's silence at the launch
    clock keeps such a run inside the window a fresh dispatch gets, and the
    clock grows with the run, so a resume that never writes its own log still
    stalls once that window has genuinely elapsed.
    """
    found = _record_newest_stream(record)
    if found is None:
        quiet = _stream_quiet_seconds(record, now_seconds=now_seconds)
    else:
        quiet = max(0, int(now_seconds - found[1]))
    attempt_started = _attempt_started_seconds(record)
    if attempt_started is None:
        # Nothing recorded when the attempt began, so the launch window a
        # fresh dispatch gets is the only launch clock there is: the pointer a
        # resume just rewrote, or the run's creation moment.
        launch = _stream_quiet_seconds(record, now_seconds=now_seconds)
    else:
        launch = max(0, int(now_seconds - attempt_started))
    return min(quiet, launch)


def _declared_wait_age_seconds(
    *,
    started_seconds: float,
    stream_mtime: float | None,
    now_seconds: float,
) -> int:
    """Age a declared wait, without counting time the run was writing output.

    A worker that is producing output is not parked, whatever its manifest
    declares. Measured 2026-09-18: a run was escalated from waiting to
    wait-aged at 1804 seconds while its newest stream file was zero minutes
    old and still growing, because the age was read from the declaration and
    nothing compared it against the run's own output. The clock that matters
    is therefore the later of the two, so a declaration sitting above a live
    stream stays young until the output actually stops — which is what makes
    wait-aged usable as a recovery trigger rather than a reading a coordinator
    has to take a second measurement to disbelieve.
    """
    latest = started_seconds
    if stream_mtime is not None and stream_mtime > latest:
        latest = stream_mtime
    return max(0, int(now_seconds - latest))


def _manifest_wait(
    manifest_data: Mapping[str, Any],
    manifest: Path,
    *,
    now_seconds: float,
    stale_after_seconds: int,
    stream_mtime: float | None = None,
    previous_lift: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return the external-wait declaration a manifest actually holds.

    None means the manifest holds no wait — either it is not waiting at all, or
    it carries the four wait fields without a wait in them. A worker recording
    where it stands at orientation is not a parked run during an orientation:
    reading the mere presence of the fields as a declaration put healthy
    workers in the waiting column, aged them into wait-aged, and offered them
    to the resume sweep, which resumed them on a probe that was trivially
    true. A declaration whose condition names no wait, or whose probe cannot
    report a pending state, is therefore no declaration at all.

    A declaration that survives both readings still ages against the run's own
    output: a worker whose stream is still being written is producing, not
    parked, so the age of its wait is measured from the newer of the wait's
    declaration and the last stream write.

    A declaration whose terminal list names a state its own probe reports while
    the awaited job is still live is refused rather than honoured: it can never
    report a pending state, so it reads satisfied on every sweep and offers its
    run to the resume loop forever. The offending token is named in the refusal
    so the repair is a one-line edit rather than a reread of the probe.

    A declaration whose probe cannot fail is refused as invalid, with the
    probe named, and for the same reason: it names nothing outside the worker
    — ``echo pending``, ``git rev-parse HEAD`` — so it reports the same thing
    whatever the awaited work is doing and can neither end a wait nor report
    one still pending. The refusal names the probe rather than reducing the
    declaration to nothing, because the shape is one a worker can repair from
    the row.

    A condition takes one of two shapes. An argument vector is the one a
    scheduler query needs; ``wait_file`` is the ordinary one, because a worker
    waits for a job's log far more often than for a scheduler to report that
    the job left the queue. It names one path or an array of paths, and its
    probe and terminal are derived from those paths rather than declared, so
    the declaration reads as a wait in the same shape as any other. Two
    readers then answer it, and they are not one implementation: the sweep's
    reader in ``reckon/crew/resumption.py`` runs the derived vector and its
    ``exit:0`` sentinel and is the one that lifts a park, while
    ``_run_wait_condition_probe`` below looks for the paths directly and
    returns a row naming the ones still missing. Both must read the same
    declaration, and a case in ``tests/test_wait_shapes.py`` pins the lift
    through the sweep because a case that stops at the classifier's reader
    cannot show a run is ever resumed.

    A probe that is present but unreadable is refused by naming the shapes the
    reader does accept, and never reduced to the absence of a probe: a worker
    whose declaration was discarded reported to a coordinator as a worker that
    had declared nothing, which is a failure invisible at the moment it could
    still be repaired.

    ``previous_lift`` is the pointer's record of the last condition that lifted
    this run. A declaration identical to the one already lifted, arriving again
    after the worker re-parked, is the same condition reporting terminal a
    second time without ending: a wait-key defect, marked on the wait so the
    reader sees why the lift loop is stopped instead of watching it repeat.
    """
    if str(manifest_data.get("status") or "").strip().lower() != WAITING_STATUS:
        return None
    condition = str(manifest_data.get("wait_condition") or "").strip()
    declared_probe = _wait_probe(manifest_data.get("wait_probe"))
    files = _wait_file_paths(manifest_data.get("wait_file"))
    declared_terminal = _wait_terminal_values(manifest_data.get("wait_terminal"))
    if _wait_condition_declares_no_wait(condition):
        return None
    if _wait_probe_is_a_no_op(declared_probe):
        return None
    # A file condition's end is its paths' existence, so the terminal the
    # reader matches is derived rather than declared: one probe path answers
    # both shapes, and a file condition needs no exit-code sentinel written by
    # hand for the sweep that lifts it to read.
    terminal = declared_terminal or (["exit:0"] if files else [])
    probe = _wait_file_probe(files) if files else declared_probe
    resume_brief = str(manifest_data.get("resume_brief") or "").strip()
    missing = [
        name
        for name, value in (
            ("wait_condition", condition),
            ("wait_probe", probe),
            ("wait_terminal", terminal),
            ("resume_brief", resume_brief),
        )
        if not value
    ]
    # A shape the reader does not understand is refused by naming what it does
    # accept, so the declaration reaches the follower as something to repair
    # rather than as a worker that declared nothing.
    missing.extend(
        reason
        for reason in (
            _wait_probe_shape_refusal(manifest_data.get("wait_probe")),
            _wait_file_shape_refusal(manifest_data.get("wait_file")),
        )
        if reason
    )
    if files and declared_probe:
        missing.append(
            "either wait_probe or wait_file, not both: a wait has one shape"
        )
    if declared_probe and _wait_probe_cannot_fail(declared_probe):
        # A probe that runs but cannot differ is satisfied unconditionally, so
        # a wait resting on it reads the same however the awaited work is
        # doing. The refusal names the probe and the references the reader
        # accepts, because the repair is a one-line edit to the declaration.
        missing.append(
            f"wait_probe {[str(item) for item in declared_probe]!r} cannot "
            "fail: it names nothing outside the worker — no job id, pid, port "
            "or path — so its result cannot differ between sweeps and it tests "
            f"nothing; {_WAIT_ACCEPTED_SHAPES}"
        )
    if files and declared_terminal:
        missing.append(
            "wait_terminal alongside wait_file, where the condition ends when "
            "its paths exist"
        )
    unemitted = _wait_terminal_names_no_probe_state(declared_terminal)
    if unemitted:
        missing.append(
            f"wait_terminal listing {unemitted!r}, an exit-code sentinel "
            f"rather than a state the probe prints; {_WAIT_ACCEPTED_SHAPES}"
        )
    live_token = _wait_terminal_names_a_live_state(terminal, probe)
    if live_token:
        missing.append(
            f"wait_terminal listing {live_token!r}, a state the probe reports "
            "while the awaited job is still live"
        )
    started = None
    started_value = str(manifest_data.get("wait_started_at") or "").strip()
    if started_value:
        timestamp_value = _unquote_wait_declaration_scalar(started_value)
        started = parse_utc(timestamp_value)
        if started is None:
            missing.append("readable wait_started_at")
    if started is None:
        try:
            started_seconds = manifest.stat().st_mtime
        except OSError:
            started_seconds = now_seconds
    else:
        started_seconds = started.timestamp()
    age_seconds = _declared_wait_age_seconds(
        started_seconds=started_seconds,
        stream_mtime=stream_mtime,
        now_seconds=now_seconds,
    )
    expected_seconds, expected_error = _wait_expected_seconds(
        manifest_data, default_seconds=stale_after_seconds
    )
    if expected_error:
        missing.append(expected_error)
    signature = _wait_declaration_signature(condition, probe, terminal, resume_brief)
    wait_key_defect = ""
    if isinstance(previous_lift, Mapping) and previous_lift.get("trigger") == signature:
        wait_key_defect = (
            "wait-key defect: this declaration already lifted the run and has "
            "come back unchanged, so its terminal state "
            f"({', '.join(terminal) or 'unset'}) did not end the wait; the "
            "lift loop stays stopped until the declaration changes"
        )
    return {
        "condition": condition,
        "probe": probe,
        "terminal": terminal,
        "files": files,
        "resume_brief": resume_brief,
        "started_at": started_value
        or datetime.fromtimestamp(started_seconds, tz=UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "age_seconds": age_seconds,
        "expected_horizon_seconds": expected_seconds,
        "overdue": age_seconds > expected_seconds,
        "signature": signature,
        "valid": not missing,
        "error": "missing or invalid " + ", ".join(missing) if missing else "",
        "wait_key_defect": wait_key_defect,
    }


def external_wait(
    record: Mapping[str, Any],
    *,
    now_seconds: float | None = None,
    stale_after_seconds: int = LOG_STALE_AFTER_SECONDS,
) -> dict[str, Any] | None:
    """Read a fresh external-wait declaration from one live pointer."""
    manifest = Path(str(record.get("manifest_path") or ""))
    _present, fresh = _run_chain_manifest_freshness(record)
    if not fresh:
        return None
    try:
        data = parse_manifest(manifest.read_text(encoding="utf-8"))
    except (OSError, ManifestParseError):
        return None
    moment = _utc_seconds() if now_seconds is None else float(now_seconds)
    return _manifest_wait(
        data,
        manifest,
        now_seconds=moment,
        stale_after_seconds=stale_after_seconds,
        stream_mtime=_run_stream_mtime(record),
        previous_lift=record.get("auto_resume"),
    )


def _reading_host() -> str:
    """The host this reader runs on, for gating process-table lookups."""
    return socket.gethostname()


def _launched_on_this_host(record: Mapping[str, Any]) -> bool:
    """Whether this host is the one that issued the record's pid.

    A pointer written before the launching host was recorded names none, and an
    unnamed host cannot be shown to be this one.
    """
    return (
        record.get("launcher_host") is not None
        and str(record.get("launcher_host")) == _reading_host()
    )


def _record_is_known_foreign(record: Mapping[str, Any]) -> bool:
    """Whether the record names a launching host that is a different machine.

    Distinct from :func:`_launched_on_this_host`, which also answers false for
    an unnamed host. A pointer with no launching host cannot be *shown* to be
    this host, but neither can it be shown to be another one, so a pid read that
    is safe to refuse on proof of a foreign machine is left to run on the mere
    absence of a name: an unnamed pointer predates the field, and its records
    are read as they always were rather than being refused for a host that was
    never written down.
    """
    host = record.get("launcher_host")
    return host is not None and str(host) != _reading_host()


def local_liveness(record: Mapping[str, Any]) -> tuple[bool | None, bool]:
    """The liveness this host can stand behind for one live pointer.

    Returns ``(alive, proven)``. The process table answers only when the
    record's launching host is this host: a pid is meaningful only on the
    machine that issued it, and the crew home is shared across login nodes, so
    asking a foreign process table — or carrying an answer taken there —
    fabricates a verdict in both directions. Where the launching host cannot be
    shown to be this host the stored answer is kept and ``proven`` is false,
    which is no evidence either way rather than proof of death, and every
    consumer that reads liveness from a pointer reads it here so one run cannot
    read two ways across the views that render it.

    A pointer pid that is gone does not yet end the work. The pointer names the
    supervisor for a supervised launch while the worker pid lives on the run
    directory's own worker record, and a supervisor that exits before its
    worker takes the pointer pid with it — the recorded pid then answers for a
    process that is gone while the work continues. The worker record is asked
    next, on the same terms: it carries no host of its own and numbers are
    reused across machines, so only a run launched here is read alive from it.

    The recorded end is not folded in here. A caller that emits an exit record
    reads it anyway, the reading above is what says whether the record applies,
    and a resumed attempt reuses the run directory, so an earlier attempt's
    record must not call the new worker dead.
    """
    launched_here = _launched_on_this_host(record)
    if launched_here and record.get("pid"):
        # The launched pid's kernel state is the authority at this instant, and
        # the recorded start tick rules out a reused pid. A zombie entry answers
        # not alive, composing with the narrowed probe rather than reviving an
        # older answer.
        alive = runs.record_process_alive(record)
        expected_start = record.get("pid_start_time")
        if alive is True and expected_start is not None:
            alive = _process_start_time(record.get("pid")) == expected_start
        proven = True
    else:
        alive = record.get("process_alive")
        proven = False
    if launched_here and alive is not True and _worker_record_liveness(record) is True:
        alive = True
        proven = True
    return alive, proven


def live_worker_pid(record: Mapping[str, Any]) -> int | None:
    """The pid this host observes still holding the run, or None.

    :func:`local_liveness` answers *whether* the run's process lives; a
    refusal that stops a resume over a living worker is checkable only if it
    also says *which* process, because the reader who receives it can then ask
    the process table the same question. This composes the same two reads in
    the same order — the pointer's pid, then the worker record the supervisor
    writes beside the stream, which is where a run whose supervisor exited
    ahead of its worker keeps the process that still runs it — and returns
    which of them answered. A pid is meaningful only on the machine that
    issued it, so a record that cannot be shown to be this host's names
    nothing here. None means no pid answered: either the run is not alive, or
    its liveness came from a stored answer that carries no process to name.
    """
    if not _launched_on_this_host(record):
        return None
    pointer_pid = _int_or_none(record.get("pid"))
    if pointer_pid is not None and runs.record_process_alive(record) is True:
        return pointer_pid
    worker_pid = _worker_record_pid(record)
    if worker_pid is not None and _worker_record_liveness(record) is True:
        return worker_pid
    return None


def _run_chain_manifest_freshness(record: Mapping[str, Any]) -> tuple[bool, bool]:
    """Judge delivery against the first dispatch across the attempt chain."""
    try:
        attempt = int(record.get("attempt") or 1)
        attempt_baseline = int(record["manifest_baseline_mtime_ns"])
    except (KeyError, TypeError, ValueError):
        return _manifest_freshness(record)
    first_dispatch = parse_utc(str(record.get("created_at") or ""))
    if first_dispatch is None:
        return _manifest_freshness(record)
    if attempt <= 1:
        return _manifest_freshness(record)
    first_dispatch_ns = (
        int(first_dispatch.timestamp()) * 1_000_000_000
        + first_dispatch.microsecond * 1_000
    )
    if attempt_baseline <= first_dispatch_ns:
        return _manifest_freshness(record)

    # Every attempt in one run shares the first dispatch as its time boundary.
    # A manifest written by any attempt is newer than that boundary and remains
    # readable as a handover, whatever status it carries. A manifest predating
    # the run stays older, so the freshness gate still rejects an unrelated
    # artifact instead of crediting it as this run's outcome. The resumed
    # attempt's own start time cannot serve here: the handover necessarily
    # predates the attempt that inherits it.
    chain_record = dict(record)
    chain_record["manifest_baseline_mtime_ns"] = first_dispatch_ns
    return _manifest_freshness(chain_record)


# The run directory's account of how a worker's process ended, written by the
# per-run supervisor in :mod:`reckon.crew.dispatch`. The supervisor is the only
# process holding the worker's parentage, so this file is where an exit is
# recorded even when the pointer is never updated again — and unlike a pid it
# stays meaningful on a machine that never launched the worker.
EXIT_RECORD_NAME = "exit.json"


def _run_exit_record(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """The supervisor's exit record for a run, or None when there is none.

    Read verbatim and defensively: a file that cannot be read or parsed, or
    that names a different run, is absent rather than an error, so a damaged
    record classifies a run on its other evidence instead of refusing it.
    """
    run_id = str(record.get("run_id") or "")
    try:
        payload = json.loads(
            (_run_directory(record) / EXIT_RECORD_NAME).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None
    recorded = str(payload.get("run_id") or "")
    if run_id and recorded and recorded != run_id:
        return None
    return dict(payload)


def _exit_record_end_phrase(exit_record: Mapping[str, Any]) -> str:
    """How the recorded process ended, in the record's own terms."""
    if exit_record.get("signal") is not None:
        name = exit_record.get("signal_name") or f"signal {exit_record['signal']}"
        return f"ended by {name}"
    exit_code = exit_record.get("exit_code")
    if exit_code is None:
        return "ended with no wait status recorded"
    return f"exited with code {exit_code}"


def _exit_record_is_launch_failure(exit_record: Mapping[str, Any]) -> bool:
    """Whether the record itself says the launch never reached a model."""
    if str(exit_record.get("ended_during") or "") == "launch":
        return True
    try:
        return int(exit_record.get("stream_records_seen")) == 0
    except (TypeError, ValueError):
        return False


def _interruption_evidence(
    record: Mapping[str, Any],
    *,
    phase: str,
    process_alive: bool | None,
    liveness_proven: bool,
    exit_record: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, int]:
    """Return why unfinished work stopped involuntarily, plus retained commits.

    A recorded wait status or exit record is direct evidence that a signal
    ended the worker. Where no exit was recorded, death alone is ambiguous: an
    orphaned pointer already records that no terminal event arrived, while
    commits beyond the dispatch base prove an apparently working run left
    recoverable work behind. A deliberate stop or a recorded
    completion/promotion always outranks either inference, and a recorded exit
    outranks the death inferences: it is the end itself rather than a reading
    of a vanished pid, so a run that chose its exit is not an interruption.

    Retained work is inferred from the liveness reading alone, so the reading
    has to be one this host can stand behind: the crew home is shared across
    login nodes and a pid answers only on the host that issued it, so a stored
    answer from another observer is no reading here. Consumed without that
    qualification it lets a worker still running on its own machine read as an
    interruption of the work it is holding. An ending the run itself recorded is
    unaffected: it is read through the exit record, or through the phase an
    observer wrote beside the reading, and neither rests on the reading alone.
    """
    if phase in {"complete", "promoted", "stopped"} or record.get("promoted_at"):
        return None, 0

    signal_number = None
    signal_name = None
    signal_exit_code = None
    wait_status = record.get("wait_status")
    if isinstance(wait_status, Mapping) and wait_status.get("signal") is not None:
        signal_number = wait_status.get("signal")
        signal_name = str(wait_status.get("signal_name") or f"signal {signal_number}")
        signal_exit_code = wait_status.get("exit_code")
    elif exit_record is not None and exit_record.get("signal") is not None:
        # The pointer carries no wait status: that field is written by the
        # launcher holding the wait, and a supervisor-launched worker's exit
        # lands in the run directory instead. Same fact, recorded by the
        # process that collected it.
        signal_number = exit_record.get("signal")
        signal_name = str(exit_record.get("signal_name") or f"signal {signal_number}")
        signal_exit_code = exit_record.get("exit_code")
    if signal_number is not None:
        return (
            {
                "reason": "signal",
                "signal": signal_number,
                "signal_name": signal_name,
                "exit_code": signal_exit_code,
            },
            0,
        )

    if process_alive is not False:
        return None, 0
    if exit_record is not None:
        return None, 0
    if phase == "orphaned":
        return (
            {
                "reason": "dead-pid-no-exit",
                "signal": None,
                "signal_name": None,
                "exit_code": None,
            },
            0,
        )

    if not liveness_proven:
        return None, 0

    commits = _commits_beyond_base(record)
    if commits:
        return (
            {
                "reason": "dead-pid-with-retained-work",
                "signal": None,
                "signal_name": None,
                "exit_code": None,
            },
            commits,
        )
    return None, 0


def _seconds_since_dispatch(record: Mapping[str, Any], moment: float) -> float | None:
    """Seconds between the run's dispatch and ``moment``, or None when unknown.

    The dispatch is the pointer's ``created_at``, recorded by the launcher. A
    missing or unparseable stamp is None rather than zero: a run whose launch
    time cannot be read has taken no measurement, and a zero would place every
    such run outside the launch window on no evidence.
    """
    dispatched = parse_utc(str(record.get("created_at") or ""))
    if dispatched is None:
        return None
    return moment - dispatched.timestamp()


def _within_launch_window(record: Mapping[str, Any], moment: float) -> bool:
    """Whether the run is still inside the window after its own dispatch.

    A negative elapsed time — a clock that moved backwards, or a caller passing
    a moment before the launch — is not inside the window: the guard covers the
    run's own first minutes and nothing a future time is asked to invent.
    """
    elapsed = _seconds_since_dispatch(record, moment)
    return elapsed is not None and 0 <= elapsed < LAUNCH_WINDOW_SECONDS


def _manifest_may_be_mid_rewrite(
    record: Mapping[str, Any], manifest: Path, manifest_error: str, moment: float
) -> bool:
    """Whether a manifest that cannot be parsed is plausibly being rewritten.

    Workers write manifests in place rather than atomically, so a reader can
    catch a file between the truncate and the write: it is unparseable, and it
    either moved seconds ago or is smaller than the size the last readable read
    saw. Both are the absence of a verdict in transit, not an absence of
    delivery, so the reader treats the file as unchanged rather than reading its
    contents as a refusal. Only a parse failure qualifies — a readable manifest
    is a verdict whatever the writer would do next.

    Both signatures are bounded by the same short window, and neither can hold a
    reading back past it. The mtime signature is measured from the file's own
    modification; the size signature is measured from the read that recorded the
    larger size. Without that second bound a path that once held a bigger
    readable manifest would suppress the unwritten reading for as long as it
    stayed unreadable and smaller, which is exactly the shape of a retry reusing
    its run directory's manifest, and the run would never be reported at all.
    """
    if not manifest_error:
        return False
    try:
        stat = manifest.stat()
    except OSError:
        return False
    if moment - stat.st_mtime < MANIFEST_REWRITE_WINDOW_SECONDS:
        return True
    remembered = _MANIFEST_SIZES_READ.get(_manifest_size_key(record, manifest))
    if remembered is None:
        return False
    size, recorded_at = remembered
    return (
        moment - recorded_at < MANIFEST_REWRITE_WINDOW_SECONDS and stat.st_size < size
    )


def _manifest_size_key(record: Mapping[str, Any], manifest: Path) -> str:
    """The identity of one run's view of one manifest path.

    A retry writes the same path, so the path alone would let it inherit the
    size a predecessor left behind and suppress its own reading. The key carries
    the run and the attempt, and falls back to the dispatch stamp for a pointer
    that records no attempt, so a redispatch starts with nothing remembered.
    """
    run_id = str(record.get("run_id") or "")
    attempt = record.get("attempt")
    if attempt is None:
        attempt = str(record.get("created_at") or "")
    return f"{run_id}::{attempt}::{manifest}"


def _remember_manifest_size(key: str, size: int, moment: float) -> None:
    """Record the last readable size of a manifest under its run's identity.

    A long-lived reader classifies every run it sees, so the memory is capped
    and evicts the least recently touched entry. The map's own bound is the
    policy: holding entries only for runs that currently hold a live pointer
    would tie this cache to a fleet read it does not otherwise need, and would
    drop the entry for a run whose pointer is briefly unreadable.
    """
    _MANIFEST_SIZES_READ.pop(key, None)
    _MANIFEST_SIZES_READ[key] = (size, moment)
    while len(_MANIFEST_SIZES_READ) > MANIFEST_SIZE_MEMORY_MAX:
        oldest = next(iter(_MANIFEST_SIZES_READ))
        del _MANIFEST_SIZES_READ[oldest]


def _absence_of_a_verdict_is_transient(
    record: Mapping[str, Any],
    manifest: Path,
    manifest_error: str,
    moment: float,
) -> bool:
    """Whether a live run's missing written verdict is too early to report.

    Two windows cover it: the launch window after dispatch, before the worker
    has had time to write anything, and a manifest caught mid-rewrite. In both
    the run has not failed to deliver — it has not finished writing — so the
    caller keeps its liveness reading instead of naming it unwritten.
    """
    return _within_launch_window(record, moment) or _manifest_may_be_mid_rewrite(
        record, manifest, manifest_error, moment
    )


# ── The classification memo ─────────────────────────────────────────────────
# A classification reads the pointer, the assertion a run's manifest makes, the
# worker's own stream and the review stored against it. Over a fleet that is
# several hundred files, and the stream is the expensive one: a worker's log is
# megabytes by the end of a turn and every reader that re-derives a row pays to
# parse it again. The memo below keeps what those reads produced beside the
# pointer, keyed by the stat identity of every file the classification read, so
# a second reader of an unchanged run answers from the memo instead of the
# files. Nothing about the run's liveness is memoised: a process table is not a
# file, and the row must still be built from a reading taken now.
CLASSIFICATION_MEMO_NAME = "classification.json"
CLASSIFICATION_MEMO_VERSION = 1

# The memo a classification in progress is reading through, published for the
# calls that consult this run's stream and withdrawn when they return.
_CLASSIFICATION_MEMO_IN_FLIGHT: tuple[str, dict[str, Any]] | None = None

# Bytes represented by records the admission check examined since the count
# was last taken. The parsed cache can supply those records without disk I/O;
# this counts logical scan work rather than physical reads. A one-element cell
# keeps the counter mutable without a module-level global statement.
_ADMISSION_STREAM_BYTES = [0]


def take_admission_stream_bytes() -> int:
    """Logical stream bytes the admission check examined since last taken."""
    value = _ADMISSION_STREAM_BYTES[0]
    _ADMISSION_STREAM_BYTES[0] = 0
    return value


def _count_admission_bytes(count: int) -> None:
    if count > 0:
        _ADMISSION_STREAM_BYTES[0] += count


# The run directory's records the classification consults, named here so the
# memo's key covers them: each is a file whose content moves the row.
_CLASSIFICATION_RUN_RECORDS = (
    EXIT_RECORD_NAME,
    WORKER_RECORD_NAME,
    ATTEMPT_RECORD_NAME,
)


def _file_identity(path: str | Path) -> str:
    """The stat identity of one input, or ``absent`` when there is no file.

    Absence is an identity rather than a null: a record that appears where the
    last classification found none changes the answer, and a key that could not
    tell the two apart would serve the reading taken before it existed.
    """
    try:
        stat = Path(path).stat()
    except OSError:
        return "absent"
    return f"{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}"


def _file_inode(path: str | Path) -> str:
    """A file's device and inode, the identity that moves when it is replaced.

    A stream that has only grown keeps its inode and may be resumed; a stream
    written anew at the same path is a different file, and an offset into a
    predecessor's bytes means nothing in a file that never held them.
    """
    try:
        info = Path(path).stat()
    except OSError:
        return "absent"
    return f"{info.st_dev}:{info.st_ino}"


def _classification_memo_path(record: Mapping[str, Any]) -> Path | None:
    """One run's classification memo, or None for a record without a run id.

    The memo lives in the run's own directory rather than beside its pointer.
    The pointer directory is enumerated as pointers — several readers list it
    and take every ``*.json`` in it for a run, and one of them asserts the list
    holds nothing else — so a cache written there would be read as a run that
    does not exist. Nothing enumerates a run directory for pointers, and the
    memo is the run's own business besides: what it caches is what the run's
    manifest, stream and review said.
    """
    run_id = str(record.get("run_id") or "")
    if not run_id:
        return None
    return runs.run_dir(run_id) / CLASSIFICATION_MEMO_NAME


def _read_classification_memo(record: Mapping[str, Any]) -> dict[str, Any]:
    """The memo persisted for one run, or an empty one.

    Read verbatim and defensively: a file another writer caught mid-rewrite, or
    one written by an older layout, is an empty memo rather than an error, so a
    damaged cache costs a recomputation and never a wrong row.
    """
    path = _classification_memo_path(record)
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, Mapping):
        return {}
    if payload.get("version") != CLASSIFICATION_MEMO_VERSION:
        return {}
    return dict(payload)


def _write_classification_memo(
    record: Mapping[str, Any], memo: Mapping[str, Any]
) -> None:
    """Persist one run's memo in its directory, atomically and best-effort.

    The memo is a cache written by a read, so it never brings a run's home into
    being: a directory that is absent or empty is not yet the run's home, and a
    memo written into it would make it one — leaving a run directory behind a
    pointer that never had any, which a discard then finds a marker's place in,
    reading a deliberate discard for a pointer that only ever vanished. A
    directory that already holds the run's records is left to keep its memo.

    Every reader of the live fleet shares these files, so the write lands
    through a rename: a reader either sees the previous memo or this one, never
    half of either. A memo that cannot be written is not an error — it costs
    the next reader a recomputation, which is the state the fleet was in before
    the memo existed.
    """
    path = _classification_memo_path(record)
    if path is None:
        return
    if not path.parent.is_dir() or not any(path.parent.iterdir()):
        return
    payload = dict(memo)
    payload["version"] = CLASSIFICATION_MEMO_VERSION
    from reckon._store import write_atomically

    try:
        write_atomically(
            path,
            lambda handle: json.dump(payload, handle, sort_keys=True),
            fsync=False,
            mode=0o600,
        )
    except (OSError, TypeError, ValueError):
        return


def _git_directory(tree: Path) -> Path | None:
    """A checkout's own git directory, following the pointer a worktree writes.

    A linked worktree keeps a file where a checkout keeps a directory, and that
    file names the git directory the worktree's refs and head live in. Both
    shapes resolve here, so the identity below reads the same two files whether
    the run sits in the repository or in a worktree of it.
    """
    marker = tree / ".git"
    try:
        if marker.is_dir():
            return marker
        text = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    written = Path(text.split(":", 1)[1].strip())
    if not written.is_absolute():
        written = tree / written
    return written


def _worktree_head_identity(tree: Path | None) -> str:
    """The revision a checkout currently names, read as files rather than by git.

    A stored review is selected against the head of the tree it describes, so
    that head is an input of the classification exactly as the manifest and the
    stream are. Reading it through a subprocess would cost a process per
    pointer per sweep, and the answer is two small files: the git directory's
    ``HEAD``, which either carries a revision or names a ref, and the ref it
    names. A ref held only in ``packed-refs`` is identified by that file's stat
    instead, which moves when a pack is rewritten.
    """
    if tree is None:
        return "no-tree"
    git_dir = _git_directory(tree)
    if git_dir is None:
        return "no-git"
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return "no-head"
    parts = [head]
    ref = head.split(":", 1)[1].strip() if head.startswith("ref:") else ""
    if ref:
        common = git_dir
        try:
            pointer = (git_dir / "commondir").read_text(encoding="utf-8").strip()
        except OSError:
            pointer = ""
        if pointer:
            written = Path(pointer)
            common = written if written.is_absolute() else git_dir / written
        loose = None
        for base in (git_dir, common):
            candidate = base / ref
            try:
                loose = candidate.read_text(encoding="utf-8").strip()
                break
            except OSError:
                continue
        parts.append(
            loose if loose is not None else _file_identity(common / "packed-refs")
        )
    return "|".join(parts)


def _own_review_record_exists(record: Mapping[str, Any]) -> bool:
    """Whether the store holds a record at one of this run's own paths.

    The distinction decides what a memo may serve: a run with its own file is
    answered from that file alone, while a run without one is answered by a
    listing of the whole store for a record filed under another run's id. Only
    the first of those is keyed on the run's own paths, so only the first may be
    served from a memo without a store-wide identity behind it.
    """
    project = str(record.get("project") or "")
    run_id = str(record.get("run_id") or "")
    if not project or not run_id:
        return False
    directory = review_module.review_store_root() / project
    if (directory / f"{run_id}.json").is_file():
        return True
    try:
        return any(directory.glob(f"{run_id}.at-*.json"))
    except OSError:
        return False


def _misfiled_review_candidates(record: Mapping[str, Any]) -> list[Path]:
    """The store's files that can answer this run from another run's id.

    A run with no record of its own is answered by searching the store for a
    record whose content names it, and that search resolves through the store
    index rather than opening every file. The index is the same enumeration
    read here, so the candidates named are the ones such a lookup can return
    and the two cannot drift.
    """
    project = str(record.get("project") or "")
    run_id = str(record.get("run_id") or "")
    if not project or not run_id:
        return []
    directory = review_module.review_store_root() / project
    entries = review_module._store_index(directory).get(run_id) or []
    return [Path(entry["path"]) for entry in entries if entry.get("path") is not None]


def _review_input_identities(record: Mapping[str, Any]) -> dict[str, str]:
    """The identity of every review-store file this run's review is read from.

    The store is read by run id and by revision, so the candidates are the
    run's own path and any revision-keyed copy of it. The project directory
    joins them because a record filed under another run id is found by listing
    the directory, and the listing moves when an entry is added or removed. A
    record filed under another run id is itself among the inputs whenever this
    run has none of its own, because it is then the record the selection reads:
    the directory's identity does not move when such a file is rewritten in
    place, so the file's own identity is what carries that rewrite into the key.
    """
    project = str(record.get("project") or "")
    run_id = str(record.get("run_id") or "")
    identities: dict[str, str] = {}
    if not project or not run_id:
        return identities
    # The committed reviews tree is read before the staging store, so a promoted
    # run's classification reads it: its run directory and every record filed
    # under the run id are inputs beside the staging candidates. A committed
    # record appears where the staging store holds none, and committing one adds
    # a file and moves the directory, so a key that skipped that tree would
    # serve the classification taken before the record was committed over the
    # record the reader now returns.
    committed = review_module.committed_review_root(project)
    committed_record_exists = False
    if committed is not None:
        run_directory = committed / review_module.COMMITTED_RUN_DIRNAME / run_id
        identities[str(run_directory)] = _directory_identity(run_directory)
        with contextlib.suppress(OSError):
            for path in sorted(run_directory.glob("*.json")):
                identities[str(path)] = _file_identity(path)
                committed_record_exists = True
    directory = review_module.review_store_root() / project
    identities[str(directory)] = _directory_identity(directory)
    # A run whose record is committed is answered from the committed tree before
    # any staging file is consulted, so its own record exists and no store-wide
    # staging search runs — the same settling the staging check makes alone.
    own_record_exists = committed_record_exists or _own_review_record_exists(record)
    # Whether the run has a record at one of its own paths is part of the key
    # rather than a note beside it: a run with none is answered by listing the
    # whole store, so the appearance of its own file is what moves that answer
    # from a listing to a read, and a key that could not see the difference
    # would serve the listing's verdict over the record a reviewer just wrote.
    identities[f"own-review-record:{run_id}"] = (
        "present" if own_record_exists else "absent"
    )
    candidates = [directory / f"{run_id}.json"]
    with contextlib.suppress(OSError):
        candidates.extend(sorted(directory.glob(f"{run_id}.at-*.json")))
    if not own_record_exists:
        candidates.extend(_misfiled_review_candidates(record))
    for path in candidates:
        identities[str(path)] = _file_identity(path)
    return identities


def _directory_identity(path: Path) -> str:
    """The stat identity of a directory, including when its entries last moved.

    A directory's own mtime and ctime change when an entry is added, removed or
    replaced, which is what makes an index over its entries current or stale.
    That is a different question from a file's identity — a file rewritten in
    place keeps its own name — so the two are read by different readers.
    """
    try:
        info = path.stat()
    except OSError:
        return "absent"
    return f"{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}:{info.st_ctime_ns}"


def _classification_inputs(record: Mapping[str, Any], log: Path) -> dict[str, str]:
    """Every file one classification reads, with its identity.

    The set is the pointer, the manifest, the stream the observation reads, the
    run directory's own records, and the review store's candidates for this
    run. It is computed from the same paths the classification itself resolves,
    so the key describes the reads that were actually made rather than a
    separately maintained list of them.
    """
    identities: dict[str, str] = {}
    run_id = str(record.get("run_id") or "")
    if run_id:
        pointer = runs.pointer_path(run_id)
        identities[str(pointer)] = _file_identity(pointer)
    manifest = str(record.get("manifest_path") or "")
    if manifest:
        identities[manifest] = _file_identity(manifest)
    identities[str(log)] = _file_identity(log)
    directory = _run_directory(record)
    for name in _CLASSIFICATION_RUN_RECORDS:
        record_path = directory / name
        identities[str(record_path)] = _file_identity(record_path)
    identities.update(_review_input_identities(record))
    # A review is chosen against the revision the run's tree carries now, so the
    # head is part of the key even though no reader of the review calls it a
    # file: a tree that gained a commit between two classifications moves the
    # record that describes it, and serving the earlier one would report a
    # review of code the run no longer holds.
    repository = _review_tree(record)
    identities[f"git-head:{repository}"] = _worktree_head_identity(repository)
    return identities


def _classification_key(identities: Mapping[str, str]) -> str:
    """One key from a set of input identities, order-independent and stable."""
    rendered = "\n".join(f"{path}={identities[path]}" for path in sorted(identities))
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


# Tokens a terminal message carries that belong to the run rather than to the
# cause: the run's own id, a named process id, and any timestamp the message
# quotes. They are removed before the message is hashed into a lane-cause
# signature, so two runs one cause stopped correlate, while everything else the
# message says and the cause kind beside it stay in the hash and keep two
# different causes apart.
_LANE_CAUSE_RUN_ID = re.compile(r"\br-\d{8}t\d{6,}[a-z0-9-]*")
_LANE_CAUSE_PID = re.compile(r"\bpid[=: ]+\d+\b")
_LANE_CAUSE_TIMESTAMP = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:z|[+-]\d{2}:?\d{2})?\b"
)
_LANE_CAUSE_CLOCK_TIME = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?(?:[ap]\.?m\.?)?\b")


def _lane_cause_signature_text(lowered: str) -> str:
    """A terminal message with the tokens that vary per run removed."""
    text = lowered
    for pattern in (
        _LANE_CAUSE_RUN_ID,
        _LANE_CAUSE_PID,
        _LANE_CAUSE_TIMESTAMP,
        _LANE_CAUSE_CLOCK_TIME,
    ):
        text = pattern.sub(" ", text)
    return " ".join(text.split())


def _terminal_lane_signal(
    record: Mapping[str, Any],
) -> tuple[dict[str, str] | None, str | None]:
    """Read cause and end time from the latest terminal result, never stderr."""
    found = _record_newest_stream(record)
    if found is None:
        return None, None
    result: Mapping[str, Any] | None = None
    try:
        with found[0].open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, Mapping) and event.get("type") == "result":
                    result = event
    except OSError:
        return None, None
    if result is None:
        return None, None
    stamp = str(result.get("timestamp") or "") or None
    if not result.get("is_error"):
        return None, stamp
    raw = result.get("result") or result.get("error") or result.get("message")
    if isinstance(raw, Mapping):
        raw = raw.get("message") or raw.get("detail")
    terminal_text = " ".join(str(raw or "").split())
    reason = terminal_text[:240]
    lowered = terminal_text.casefold()
    kind = ""
    if (
        "issue with the selected model" in lowered
        or "unknown model" in lowered
        or "unserved model" in lowered
        or "model not found" in lowered
        or "model does not exist" in lowered
        or "model unavailable" in lowered
    ):
        kind = "backend-catalog-change"
    elif "rate limit" in lowered or "rate-limit" in lowered:
        kind = "rate-limit"
    elif "connection refused" in lowered or "transport" in lowered:
        kind = "transport-outage"
    if not kind:
        return None, stamp
    agent = record.get("agent")
    model = (
        str(agent.get("model") or "").strip() if isinstance(agent, Mapping) else ""
    ) or str(record.get("model") or "").strip()
    # A generic catalog error cannot identify which model was unserved without
    # the configured model. Keep its per-run cause, but do not correlate it.
    identity = (
        f"{kind}\0{model if kind == 'backend-catalog-change' else ''}\0"
        f"{_lane_cause_signature_text(lowered)}"
    )
    signature = (
        hashlib.sha256(identity.encode("utf-8")).hexdigest()
        if kind != "backend-catalog-change" or model
        else ""
    )
    return {
        "kind": kind,
        "reason": reason,
        "model": model if kind == "backend-catalog-change" else "",
        "signature": signature,
    }, stamp


def group_terminal_lane_events(
    rows: Sequence[Mapping[str, Any]],
    *,
    window_seconds: int = LANE_EVENT_WINDOW_SECONDS,
) -> list[dict[str, Any]]:
    """Replace terminals sharing a recorded cause and end window with one row."""
    grouped: dict[tuple[str, str], list[tuple[float, int]]] = {}
    for index, row in enumerate(rows):
        terminal_failure = row.get("classification") in {
            "abandoned",
            "blocked",
            "failed",
            "exited-unfinished",
            "stopped",
            "refused-at-admission",
            INTERRUPTED_RUN_PHASE,
        } or (row.get("classification") == "paused" and row.get("lane_cause"))
        if row.get("process_alive") is not False or not terminal_failure:
            continue
        backend = str(row.get("backend") or "").strip()
        cause = row.get("lane_cause")
        signature = (
            str(cause.get("signature") or "") if isinstance(cause, Mapping) else ""
        )
        ended = parse_utc(str(row.get("lane_ended_at") or ""))
        if not backend or not signature or ended is None:
            continue
        grouped.setdefault((backend, signature), []).append((ended.timestamp(), index))

    events: dict[int, dict[str, Any]] = {}
    suppressed: set[int] = set()
    for (backend, _signature), endings in grouped.items():
        endings.sort()
        clusters: list[list[tuple[float, int]]] = []
        for ending in endings:
            if not clusters or ending[0] - clusters[-1][0][0] > window_seconds:
                clusters.append([ending])
            else:
                clusters[-1].append(ending)
        for cluster in clusters:
            if len(cluster) < 2:
                continue
            members = [rows[index] for _stamp, index in cluster]
            cause = members[0]["lane_cause"]
            run_ids = [str(row.get("run_id") or "") for row in members]
            member_actions = []
            for member in members:
                remedy = member.get("resume_remedy")
                session = member.get("session_resolution")
                worktree = str(member.get("worktree") or "")
                resumable = bool(
                    isinstance(remedy, Mapping)
                    and remedy.get("session_id")
                    and isinstance(session, Mapping)
                    and session.get("resolved")
                    and worktree
                    and Path(worktree).is_dir()
                )
                recovery = (
                    "resume" if resumable else str(member.get("recovery") or "inspect")
                )
                next_action = str(member.get("next_action") or "")
                if not resumable and recovery == "resume":
                    recovery = "inspect"
                    next_action = f"inspect run {member.get('run_id')}; no usable resume session was resolved"
                member_actions.append(
                    {
                        "run_id": member.get("run_id"),
                        "classification": member.get("classification"),
                        "recovery": recovery,
                        "next_action": (
                            str(remedy["command"]) if resumable else next_action
                        ),
                        "resumable": resumable,
                        "session_id": str(remedy["session_id"]) if resumable else None,
                        "worktree": worktree or None,
                    }
                )
            recoveries = {member["recovery"] for member in member_actions}
            event = {
                "backend": backend,
                "run_ids": run_ids,
                "cause": str(cause.get("kind") or ""),
                "reason": str(cause.get("reason") or ""),
                "model": str(cause.get("model") or "") or None,
                "members": member_actions,
                "started_at": members[0].get("lane_ended_at"),
                "ended_at": members[-1].get("lane_ended_at"),
                "window_seconds": window_seconds,
            }
            leader = cluster[0][1]
            report = dict(rows[leader])
            report.pop("resume_remedy", None)
            report.pop("session_resolution", None)
            report.update(
                classification="lane-event",
                recovery_classification="lane-event",
                recovery=next(iter(recoveries)) if len(recoveries) == 1 else "inspect",
                lane_event=event,
                detail=f"backend {backend!r} ended {len(run_ids)} runs together: {event['reason']}",
                next_action=(
                    f"read each member's recovery and next_action in lane_event.members "
                    f"when backend {backend!r} returns"
                ),
            )
            if isinstance(report.get("fleet_verdict"), Mapping):
                report["fleet_verdict"] = _watch_verdict(
                    report,
                    report,
                    moment=time.time(),
                    stall_seconds=LOG_STALE_AFTER_SECONDS,
                )
            events[leader] = report
            suppressed.update(index for _stamp, index in cluster[1:])
    return [
        events.get(index, dict(row))
        for index, row in enumerate(rows)
        if index not in suppressed
    ]


def classify_pointer(
    record: Mapping[str, Any],
    *,
    stale_after_seconds: int = LOG_STALE_AFTER_SECONDS,
    now_seconds: float | None = None,
    condition_test: Callable[[Mapping[str, Any], Mapping[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """Classify one live pointer, without touching it.

    Pure and read-only, so the same judgement serves an MCP read and
    :func:`recover`. Liveness is established at the moment of use: when the
    record's launching host is this host the process table is asked now, and
    otherwise the stored answer is carried and marked unproven. The recorded
    launching host is the pointer's ``launcher_host`` field, spelled with
    ``socket.gethostname()`` on the machine that launched the run. Delivery
    comes from the manifest's status, because a terminal stream event only says
    the worker's turn ended. It does not say the node completed successfully.
    """
    run_id = str(record.get("run_id") or "")
    phase = str(record.get("phase") or "")
    # Read once here because the manifest read records when it happened, and
    # every reading below is ordered against the same instant.
    moment = _utc_seconds() if now_seconds is None else float(now_seconds)
    manifest = Path(str(record.get("manifest_path") or ""))
    manifest_file_present, manifest_present = _run_chain_manifest_freshness(record)
    # The memo is keyed on the files this classification reads, so it is
    # resolved from the same paths the reads below use: a key taken from a
    # differently resolved path would describe a read nobody made.
    memo = _read_classification_memo(record)
    memo_inputs = _classification_inputs(
        record, Path(str(record.get("log_path") or ""))
    )
    memo_key = _classification_key(memo_inputs)
    memo_fresh = memo.get("key") == memo_key
    manifest_data: dict[str, Any] = {}
    manifest_error = ""
    manifest_digest: str | None = None
    manifest_text = ""
    if manifest_present and memo_fresh:
        served_manifest = memo.get("manifest")
        if isinstance(served_manifest, Mapping):
            manifest_text = str(served_manifest.get("text") or "")
            manifest_data = dict(served_manifest.get("data") or {})
            manifest_digest = served_manifest.get("digest")
            manifest_error = str(served_manifest.get("error") or "")
            _remember_manifest_size(
                _manifest_size_key(record, manifest),
                int(served_manifest.get("size") or 0),
                moment,
            )
            manifest_present = bool(served_manifest.get("present"))
        else:
            manifest_present = False
    elif manifest_present:
        try:
            manifest_text = manifest.read_text()
            manifest_data = parse_manifest(manifest_text)
            # A content digest lets a watcher tell a rewrite that changed
            # something from a touch that did not. Computed from the same read
            # that parsed the status, so the digest and the verdict can never
            # describe different versions of the file.
            manifest_digest = hashlib.sha256(manifest_text.encode("utf-8")).hexdigest()
            # The last size a readable manifest had, with the moment of this
            # read, so a later read that finds the file smaller recognises a
            # truncating rewrite earlier than the mtime signature would.
            _remember_manifest_size(
                _manifest_size_key(record, manifest),
                len(manifest_text.encode("utf-8")),
                moment,
            )
        except (OSError, ManifestParseError) as exc:
            # The file exists but no reader can judge it: an unreadable file is
            # a condition of the delivery, not an exception in the classifier.
            # Collecting it here keeps the refusal text (the parse error) in a
            # channel the classification branches read, so a manifest that
            # declares a format and is not readable degrades to its own outcome
            # rather than escaping this function and failing every ticker
            # refresh for every session.
            manifest_error = str(exc)
        memo["manifest"] = {
            "text": manifest_text,
            "data": manifest_data,
            "digest": manifest_digest,
            "error": manifest_error,
            "size": len(manifest_text.encode("utf-8")),
            "present": True,
        }
    manifest_reported_status = str(manifest_data.get("status") or "").strip().lower()
    # The orientation write is the first thing every dispatch writes: the tree,
    # the base revision and the write paths, before any status exists. A body
    # carrying those keys and no readable status is a run in progress, not a
    # delivery a reader has to repair, so it reaches the unwritten handling
    # beside the template. The body is read whole because the status is required
    # before any field reaches the classifier, so a reader that refused it may
    # have refused a file whose only sin was being one minute old.
    manifest_unwritten = manifest_status_is_template(
        manifest_reported_status
    ) or _carries_orientation_write(manifest_text, manifest_data)
    # The dispatch contract prints all terminal choices as a placeholder. It
    # is evidence that the worker never wrote a verdict, not a fourth spelling
    # of one, so no terminal predicate may see it as delivered state.
    manifest_status = "" if manifest_unwritten else manifest_reported_status
    manifest_derived = str(manifest_data.get("derived") or "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    if manifest_derived:
        # A recovery artifact preserves evidence; it is not delivery by the
        # worker and therefore cannot satisfy the promotion precondition.
        manifest_present = False
        manifest_digest = None
    manifest_commits = list(manifest_data.get("commits") or [])
    manifest_blockers = list(manifest_data.get("blockers") or [])
    needs_help = manifest_data.get("needs_help")
    # Populated only while a dead unfinished run is being distinguished as an
    # interruption with retained work or as an abandonment with nothing left:
    # asking git costs a subprocess, so live and settled delivery paths never pay.
    commits_beyond_base = 0
    # Liveness is read at the moment it is used, not carried from the fleet
    # read that loaded the pointer, through the one host-gated reading every
    # consumer shares.
    alive, liveness_proven = local_liveness(record)
    local_reading = liveness_proven
    # The worker record is read again for the descendant check below, which
    # asks whether anything runs under the worker: that answer is about the
    # pid's children rather than about the run's liveness.
    worker_alive = _worker_record_liveness(record)
    # Whether anything runs under the worker is the second half of the same
    # question, so it is read here rather than by each consumer: the pid asked
    # is the process the work happens in, which for a supervised launch is the
    # worker the supervisor spawned and not the pointer's own pid. A supervisor
    # holds its worker as a child for as long as it lives, so asking the
    # pointer's pid whether anything runs below it would answer yes for every
    # supervised run and tell a reader nothing. A pid is worth asking about
    # only on the host that issued it: a run launched elsewhere asks nothing,
    # and the row then carries no descendant reading rather than a foreign
    # process table's opinion of some other machine's pid.
    worker_pid: int | None = None
    if local_reading and alive is True:
        worker_pid = _worker_record_pid(record) if worker_alive is True else None
        if worker_pid is None:
            worker_pid = _int_or_none(record.get("pid"))
    descendant_alive = _live_descendant(worker_pid) if worker_pid is not None else None
    # The run's own supervisor records the worker's exit in the run directory,
    # and that account survives a pointer nobody updates and a pid no machine
    # but the launching one can look up. It is consulted only where the process
    # table has not answered that the worker is still there, because a resumed
    # attempt reuses the run directory and the record an earlier attempt left
    # behind must not call the new worker dead. Where the pid cannot answer, the
    # record is the proof of the end that a bare pid never was, so the run stops
    # being inferred dead from a missing process and is read from its record.
    exit_record = _run_exit_record(record)
    ended_exit = exit_record if exit_record is not None and alive is not True else None
    if ended_exit is not None:
        alive = False
    # The liveliest stream the run has, taken through the shared reader, so a
    # resumed or lane-changed run is aged against what it is writing now rather
    # than the first file the pointer named. Absent a non-empty stream the
    # pointer's own log path is still used, so an empty stream file keeps
    # reporting its own age rather than none at all.
    stream_reading = _record_newest_stream(record)
    log = Path(str(record.get("log_path") or ""))
    if stream_reading is not None:
        log = stream_reading[0]
    age = None
    if log.is_file():
        age = max(0, int(_utc_seconds() - log.stat().st_mtime))
    # Superseded-by-newer-activity applies to an ordinary non-terminal report
    # that is not yet a verdict. A declared wait is different: the manifest is
    # the authority for what the worker is parked on, and its process may stay
    # alive briefly or exit immediately without changing that condition.
    # Terminal-looking reports are handled below: the live process outranks
    # every worker-reported outcome regardless of file recency, and the
    # manifest becomes authoritative when that process exits.
    if (
        manifest_status
        and manifest_status not in TERMINAL_MANIFEST_STATUSES
        and manifest_status != WAITING_STATUS
        and alive is True
        and log.is_file()
        and manifest.is_file()
        and log.stat().st_mtime_ns > manifest.stat().st_mtime_ns
    ):
        manifest_present = False
        manifest_data = {}
        manifest_digest = None
        manifest_status = ""
        manifest_commits = []
        manifest_blockers = []
        needs_help = None
    # A provider refusal makes an otherwise-abandoned run a block: the process
    # is gone but the stop is triageable (a named backend, limit and reset) and
    # resumable once the limit lifts. Detected from the same stream observe
    # reads, so the two paths agree.
    with _memo_published(record, memo):
        budget = _stream_budget(record)
    refusal_block = (
        _refusal_block(record, budget)
        if budget is not None and budget.get("refusal")
        else None
    )
    # A spent lane writes retries, not a refusal event; its mid-flight shape is
    # read alongside the refusal and only when no refusal already explains the
    # stop, so the two dead-lane readings never compete for the same run. The
    # budget is resolved once above, so both gates share a single stream read. A
    # background wait is checked only when neither already explains the stop:
    # all three name a process that is gone but resumable, and the lane reason
    # is the most triageable of the three when more than one is present.
    retry_block = (
        _stream_retry_block(record, budget)
        if budget is not None and not budget.get("refusal")
        else None
    )
    # Terminal retry exhaustion on an unmetered lane: the budget block carries
    # lane_backpressure and a retry count where the metered exhaustion carries
    # a refusal, so the two dead-lane readings stay on their own gates and a
    # live or recovered run never reaches a block through either.
    exhaustion_block = (
        _stream_exhaustion_block(record, budget)
        if budget is not None and not budget.get("refusal")
        else None
    )
    # A rejected rate-limit window is a hold time lifts, not a refusal a person
    # resolves: the event names the window and its reset, so the run pauses
    # until the window turns over rather than blocking for a coordinator.
    budget_hold = _budget_hold_block(record, budget)
    with _memo_published(record, memo):
        background_wait = (
            None
            if (refusal_block or retry_block or budget_hold)
            else _background_wait_signal(record)
        )
    # A refusal at admission is read from the stream's own marks, not from the
    # budget block: it is not a spend refusal — nothing was requested — and the
    # block carries no budget to refuse from. It is resolved here so the
    # dead-process chain consults the stream once for the shape. Only a run whose
    # process is gone can reach that arm, so the read is taken only when it can
    # be used: a live run never pays for a scan whose verdict the chain discards.
    admission_refusal = (
        None
        if (
            refusal_block
            or retry_block
            or exhaustion_block
            or budget_hold
            or alive is not False
        )
        else _admission_refusal(record, memo=memo)
    )
    terminal = phase in ("complete", "failed")
    wait = _manifest_wait(
        manifest_data,
        manifest,
        now_seconds=moment,
        stale_after_seconds=stale_after_seconds,
        stream_mtime=_run_stream_mtime(record),
        previous_lift=record.get("auto_resume"),
    )
    if wait is not None and not wait["valid"]:
        # An incomplete wait declaration is a reading failure carried on the
        # row whatever the process state: a gone run reads unreadable from it,
        # a live run reads running from liveness with the same text beside it,
        # so both readings share one refusal instead of each arm re-deriving it.
        manifest_error = str(wait["error"])
    wait_observation: dict[str, str] | None = None
    if wait is not None and wait["valid"]:
        observe_condition = (
            _run_wait_condition_probe if condition_test is None else condition_test
        )
        try:
            wait_observation = _wait_condition_observation(
                observe_condition(record, wait),
                terminal_values=list(wait["terminal"]),
            )
        # A probe is untrusted external input. Any ordinary fault says nothing
        # about either the condition or the worker, so it becomes unknown and
        # the run stays waiting; abandoned remains reserved for proof of death.
        except Exception as exc:  # noqa: BLE001
            wait_observation = {
                "state": "unknown",
                "observed": "unavailable",
                "detail": f"condition probe could not answer: {exc}",
            }
    # A live worker may hold no written verdict yet, because the reader caught
    # its manifest between a rewrite's truncate and write. That is not a
    # delivery a reader must repair, but naming it unwritten in that instant
    # makes a working run flicker, so the reading is held back while the file is
    # clearly mid-rewrite. Past that window a live run whose manifest still
    # carries no readable verdict is genuinely unwritten, which keeps the
    # word's purpose. An absent manifest is deliberately left alone: a live run
    # with no manifest at all is already classified by the stall and liveness
    # arms, and re-labelling it here would take that reading away from it.
    if alive is True:
        if _absence_of_a_verdict_is_transient(record, manifest, manifest_error, moment):
            manifest_unwritten = False
        elif manifest_present and not manifest_reported_status and bool(manifest_error):
            manifest_unwritten = True
            manifest_status = ""
    terminal_at = None
    terminal_age_seconds = None
    # A terminal manifest is provisional while a worker that could have
    # superseded it is alive. The pointer's own pid proves that on the host that
    # launched the run, but a resumed attempt often carries no such proof: the
    # run directory and its manifest are reused, and the pointer's process
    # reading can be left unproven, so the stale verdict would otherwise be read
    # as delivery — a working run counted as unpromoted and offered a promotion
    # that would delete its live pointer. The worker's own record is the second
    # proof: a worker launched after the manifest was last written cannot have
    # written it, so a terminal status still on the file belongs to the
    # superseded attempt. Only the launch time is compared; a worker started
    # before the manifest keeps the classification its record already earns.
    superseded_manifest = (
        alive is not True
        and worker_alive is True
        and _worker_launched_after_manifest(record, manifest)
    )
    deferred_outcome = manifest_status in TERMINAL_MANIFEST_STATUSES and (
        alive is True or superseded_manifest
    )
    interruption = None
    interruption_commits = 0
    if manifest_status not in TERMINAL_MANIFEST_STATUSES:
        interruption, interruption_commits = _interruption_evidence(
            record,
            phase=phase,
            process_alive=alive,
            liveness_proven=liveness_proven,
            exit_record=ended_exit,
        )
        commits_beyond_base = interruption_commits
    # A worker whose end the run itself recorded — the supervisor's exit
    # record, not an inference from a vanished pid — and whose worktree carries
    # commits past its base while the manifest still reads a working status
    # delivered work whose verdict word was never written. The end is a
    # receipt, the worktree holds the work, and nothing about the stop is
    # ambiguous, so the run reads as its own state rather than as a block: the
    # coordinator replaces the missing verdict and the work lands through the
    # ordinary gate. An interruption claims a death nothing recorded and stays
    # its own reading, because a killed worker's remedy is its surviving
    # session rather than a verdict word.
    exited_unfinished = False
    if (
        alive is False
        and interruption is None
        and ended_exit is not None
        and manifest_status in NON_TERMINAL_MANIFEST_STATUSES
    ):
        commits_beyond_base = _commits_beyond_base(record)
        exited_unfinished = commits_beyond_base > 0
    review: dict[str, Any] | None = None
    review_error = ""
    if manifest_status == "complete" and not deferred_outcome:
        served_review = memo.get("review") if memo_fresh else None
        if isinstance(served_review, Mapping):
            stored_review = served_review.get("record")
            review = dict(stored_review) if isinstance(stored_review, Mapping) else None
            review_error = str(served_review.get("error") or "")
        else:
            review, review_error = _stored_review(record)
            memo["review"] = {"record": review, "error": review_error}
    review_complete = _review_is_complete(review)
    if manifest_status in TERMINAL_MANIFEST_STATUSES and not deferred_outcome:
        terminal_seconds = manifest.stat().st_mtime
        terminal_at = (
            datetime.fromtimestamp(terminal_seconds, tz=timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
        terminal_age_seconds = max(0, int(moment - terminal_seconds))

    # Classification order: the process is consulted before the manifest
    # reading. A worker whose process is alive is classified from that life and
    # never as unreadable, so a strictness added for manifests at rest cannot
    # misreport work in progress; the manifest and its status become
    # authoritative only once the process is gone.
    #
    # The liveness test itself is hoisted above the chain as one verdict, and
    # every reading that could call a run unreadable — a present-but-unparseable
    # manifest or an incomplete wait declaration — consults it here rather than
    # testing liveness for itself, so the guarantee cannot decay into per-arm
    # guards as manifest readings are added.
    process_gone = alive is not True
    # The same three-way reading the stalled detail states, taken here from the
    # same evidence so the two surfaces cannot call one run alive and gone at
    # once. The offer of a resume is gated on it rather than on ``process_gone``,
    # which is true of an unproven reading as well as of an observed end:
    # resuming on an unproven reading is the reading's most expensive misread,
    # because nobody observed the process the resume is predicated on.
    process_reading = _process_reading(
        alive, liveness_proven=liveness_proven, exit_record=ended_exit
    )
    marker = None
    needs_help_complete_value = None
    if phase == "queued":
        classification = "queued"
        detail = str(record.get("reason") or "waiting for a local lane slot")
        action = "wait for a local lane slot"
    elif interruption is not None:
        classification = INTERRUPTED_RUN_PHASE
        signal_name = interruption.get("signal_name")
        if signal_name:
            detail = (
                f"the worker process ended by {signal_name} "
                f"(signal {interruption['signal']}) before the run completed"
            )
        elif interruption["reason"] == "dead-pid-with-retained-work":
            detail = (
                "the worker process is gone with no recorded exit and the "
                f"worktree carries {interruption_commits} commit"
                f"{'s' if interruption_commits != 1 else ''} beyond the dispatch base"
            )
        else:
            detail = (
                "the worker process is gone with no recorded exit; its pointer "
                "had already recorded that no terminal event arrived"
            )
        action = "resolve the surviving session before choosing a recovery"
    elif exited_unfinished:
        classification = "exited-unfinished"
        detail = (
            f"the worker {_exit_record_end_phrase(ended_exit)} with "
            f"{commits_beyond_base} commit"
            f"{'s' if commits_beyond_base != 1 else ''} beyond its recorded "
            f"base, but the manifest at {manifest} still reads "
            f"{manifest_status!r}; the work is committed and no verdict word "
            "says so"
        )
        action = (
            f"reckon crew repair-status --run {run_id} --status complete "
            "--reason <the verdict the worker reached>"
        )
    elif manifest_unwritten:
        classification = "running"
        if _carries_orientation_write(manifest_text, manifest_data):
            # The first write of every dispatch, read while the worker is still
            # filling in the rest of the manifest. There is no verdict to repair
            # and no placeholder to replace; the run is simply early.
            detail = (
                f"the manifest at {manifest} carries the run's orientation write "
                "and no status yet; the worker is early in its turn"
            )
            action = f"reckon crew observe --run {run_id}"
        else:
            detail = (
                f"the manifest template at {manifest} is present but its status "
                "placeholder was never replaced"
            )
            action = (
                f"reckon crew resume --run {run_id} --advice "
                "write the manifest's current status before continuing"
            )
        if not manifest_present or manifest_error:
            # A live run whose manifest is absent or unreadable carries no
            # verdict to repair, so the reader is pointed at the run rather
            # than at a status line that does not exist.
            detail = (
                f"the run is live and has no written verdict at {manifest}; "
                "the worker has not delivered a status yet"
            )
            action = f"reckon crew observe --run {run_id}"
    elif deferred_outcome:
        classification = "running"
        detail = "the process is alive"
        action = f"reckon crew observe --run {run_id}"
    elif manifest_status == "complete":
        if _pointer_role(record) == REVIEW_ROLE:
            # A review run's deliverable is the review it wrote for another
            # run, so it is not itself awaiting review. The exemption is the
            # role the run carried, not the presence of a stored review: a run
            # that never had a review attached still reads as scoring when its
            # role could have had one, and the reflex keeps dispatching for it.
            # Without this arm the scoring branch composes a dispatch whose
            # source node is this run, whose review run completes and scores in
            # turn — an unbounded chain of reviews reviewing reviews, each one
            # a real dispatch against a real member.
            classification = "promotable"
            detail = (
                "the worker manifest reports completion; the run is the "
                f"{REVIEW_ROLE} it dispatched with, so the review it wrote is "
                "its deliverable and no review of this run is required"
            )
            action = (
                f"promote the completed {REVIEW_ROLE} run once its verdict is "
                "read; the run is not itself reviewed"
            )
        elif review_complete:
            classification = "promotable"
            detail = (
                "the worker manifest reports completion and an independent "
                "parsed review is attached; the run is ready for promotion"
            )
            commits = _canonical_commits(_review_tree(record), manifest_commits)
            base = str(record.get("base_sha") or "").strip()
            if len(commits) == 1 and base and same_revision(commits[0], base):
                # A run that changed nothing records its dispatch base as its
                # only commit. Promotion refuses a citation of the base — the
                # base predates the run — so the offer names the declaration
                # a commitless run promotes under instead of the citation
                # that reproduces the refusal.
                action = (
                    f"reckon crew complete --run {run_id} --gate not-run "
                    "--no-commit '<why the run produced no commit>' "
                    "--outcome '<what the run produced>'"
                )
            else:
                action = f"reckon crew complete --run {run_id} --gate <verdict>"
                for commit in commits:
                    action += f" --commit {commit}"
        else:
            classification = "scoring"
            if review_error:
                review_detail = f"the stored review could not be read: {review_error}"
            elif review is None:
                review_detail = "no independent review is attached"
            else:
                review_detail = (
                    f"the attached review is {review.get('status') or 'incomplete'}"
                )
            detail = (
                "the worker manifest reports completion, but "
                f"{review_detail}; an independent review must be produced before promotion"
            )
            action = _review_dispatch_action(record)
    elif manifest_status == "blocked":
        classification = "blocked"
        # A blocked transition explains itself from the best source available,
        # in order: the worker's own escape-hatch question (already parsed and
        # complete — the sentence a coordinator can answer in one turn), then
        # the manifest's blockers, then a generic fallback. A bare-punctuation
        # result (a block-scalar indicator misread as its value, upstream)
        # explains nothing, so it is treated as absent too.
        needs_help_complete = isinstance(needs_help, Mapping) and bool(
            needs_help.get("complete")
        )
        headline = str(needs_help.get("headline") or "") if needs_help_complete else ""
        blocker = "; ".join(manifest_blockers)
        reason_text = headline or blocker or "the manifest reports a blocker"
        if not re.search(r"[A-Za-z0-9]", reason_text):
            reason_text = "the manifest reports a blocker"
        needs_help_complete_value = needs_help_complete
        detail = f"the worker manifest reports blocked: {reason_text}"
        # The manifest says what the worker was doing when it stopped; a
        # provider refusal says when anything can be attempted at all. When
        # both are present the manifest arm must not crowd the refusal out:
        # the refusal names the condition that gates recovery, so it is added
        # with its reset and the reader is told which must clear first. The
        # classification and the manifest reason both stay — a NEEDS-HELP
        # question on a spent lane still needs its answer, and an operator
        # simply cannot act on it until the lane clears.
        if refusal_block:
            lane = (
                f"backend {refusal_block['backend']!r} refused the turn on a "
                f"{refusal_block['limit_kind']}; reset {refusal_block['resets_at']}"
            )
            detail += (
                f"; the provider refusal must clear first — {lane} — no resume "
                "may be attempted before it does"
            )
        if needs_help_complete:
            marker = "?"
            action = f"reckon crew resume --run {run_id} --advice <answer>"
        else:
            marker = "!"
            action = f"read {manifest}; resolve the blocker before resuming the run"
        if refusal_block:
            # The lane, not the worker, owns the stop: a resume attempted before
            # the reset is refused on budget, so the offered resume is gated on
            # the lane clearing rather than proposed as work the operator can do
            # today. The recovery sweep resumes blocked runs, so the same command
            # stays the correct next action under that gate.
            action += " once the lane clears"
    elif manifest_status == "failed":
        classification = "failed"
        failure = "; ".join(manifest_blockers) or "the worker manifest reports failure"
        detail = f"the worker manifest reports failed: {failure}"
        action = (
            f"read {manifest} and launch log {record.get('stderr_path')}; "
            "repair or redispatch the run"
        )
    elif wait is not None and wait["valid"]:
        classification = WAITING_STATUS
        if (
            wait_observation
            and wait_observation["state"] == "met"
            and process_reading == "process gone"
        ):
            detail = (
                f"ready to resume: {wait['condition']} reported "
                f"{wait_observation['observed']!r}, a declared terminal state"
            )
            action = f"reckon crew resume --run {run_id} --advice continue"
        elif wait_observation and wait_observation["state"] == "met":
            # The condition is met, but nothing observed the worker's process
            # end. A live worker is still writing the run and a second worker on
            # it would collide with the first; a worker whose liveness nothing
            # established is not a death, so the offer would rest on a reading
            # nobody took. Both wait on the process rather than offering the
            # resume, and the row names which reading it is holding.
            detail = (
                f"waiting on {wait['condition']}: the probe reported "
                f"{wait_observation['observed']!r}, a declared terminal state, "
                f"but the process reading is {process_reading!r}, so no lift is "
                "offered until the process is observed to have ended"
            )
            action = (
                f"the recovery sweep lifts run {run_id} once its process is "
                "observed to have ended"
            )
        else:
            observation_detail = (
                wait_observation["detail"]
                if wait_observation is not None
                else "the condition probe did not answer"
            )
            detail = (
                f"waiting {wait['age_seconds']}s on {wait['condition']}; "
                f"{observation_detail}; terminal when the probe reports "
                f"{', '.join(wait['terminal'])}"
            )
            action = (
                f"the recovery sweep will resume run {run_id} when the condition "
                "test reports a terminal state"
            )
        if wait.get("wait_key_defect"):
            # The declaration lifted this run once already and came back
            # unchanged, so its terminal state is not ending anything. The
            # sweep already refuses a second lift for the same declaration; the
            # row says why rather than reading as an ordinary pending wait.
            detail = (
                f"{wait['wait_key_defect']} (waiting {wait['age_seconds']}s on "
                f"{wait['condition']})"
            )
            action = (
                f"edit the wait declaration in {manifest} so its terminal list "
                "names a state the probe cannot report while the job is live"
            )
    elif wait is not None and process_gone:
        classification = "unreadable"
        detail = (
            f"the manifest at {manifest} declares an external wait but is "
            f"incomplete: {manifest_error}"
        )
        action = f"repair the waiting declaration in {manifest} before resuming"
    elif phase == "stopped":
        classification = "stopped"
        detail = "the run was intentionally stopped"
        action = (
            f"inspect the worktree at {record.get('worktree')} and discard when safe"
        )
    elif budget_hold and alive is not True:
        # A rate-limit window that rejected the turn is the clearest case of
        # the who-lifts-it rule: the request was refused on a window the
        # provider resets on its own cadence, so time lifts the hold and no
        # person is needed. The paused verdict names the reset as its wake.
        # Deliberately not routed through the sweep claim the refusal arm makes:
        # a rejected window is not a formal refusal, so the sweep is not the
        # mechanism that lifts it — the reset is, and that is what is named.
        # A live process is never classified paused on this signal: it is still
        # running, and if it goes quiet the stall gate names the rejected
        # window as a wait rather than a hang. This arm owns the dead-process
        # reading, where a vanished run's last word was the rejection.
        classification = "paused"
        hold = (
            f"{budget_hold['limit_kind']} window refusals on backend "
            f"{budget_hold['backend']!r} reset {budget_hold['resets_at']}"
        )
        detail = (
            f"paused: {hold}; the hold ages out when the window resets and "
            "the run proceeds from there"
        )
        action = (
            f"resume run {run_id} once the window reset at "
            f"{budget_hold['resets_at']} lifts the hold"
        )
    elif refusal_block:
        # A refusal stays blocked rather than paused even when the limit has a
        # reset: the recovery sweep auto-resumes only runs classified blocked
        # (resumption gates on it), so a paused refusal would wait for a reset
        # nothing acts on. The who-lifts-it rule is therefore applied to the
        # window hold that carries its own expiry — the budget_hold arm above —
        # while a prose or retry exhaustion refusal remains a decision the
        # coordinator must make: it is not a wait that lifts itself.
        classification = "blocked"
        block = (
            f"backend {refusal_block['backend']!r} refused the turn on a "
            f"{refusal_block['limit_kind']}; reset {refusal_block['resets_at']}"
        )
        # The block states what was delivered so a reader does not conclude
        # nothing happened. A run killed with no manifest has nothing to show;
        # one whose manifest never reached a verdict still names its delivery
        # in the file, and pointing at it is the difference between a blocked
        # run and a vanished one.
        if not manifest_file_present:
            delivery = "no manifest was delivered and nothing has landed yet"
        else:
            delivery = (
                "the in-progress manifest at "
                f"{manifest} records what was already delivered"
            )
        detail = f"blocked: {block}; {delivery}"
        action = f"reckon crew resume --run {run_id} once the limit lifts"
    elif alive is False and exhaustion_block:
        # A dead run whose unmetered lane ended its retries in refusal is the
        # same stop as a metered budget refusal: the lane owns it, so the row
        # names the lane and offers resume rather than reading as abandonment.
        # The terminal-error shape sets it apart from the mid-flight retry arm
        # below, which names the retry count of a run still in flight when it
        # died — the exhausted run's own lane already refused, so the reading
        # keeps the refusal phrasing instead.
        classification = "blocked"
        block = (
            f"backend {exhaustion_block['backend']!r} refused the turn on a "
            f"{exhaustion_block['limit_kind']} (consumer queue backpressure) "
            f"after {exhaustion_block['retries']} retries"
        )
        if not manifest_file_present:
            delivery = "no manifest was delivered and nothing has landed yet"
        else:
            delivery = (
                "the in-progress manifest at "
                f"{manifest} records what was already delivered"
            )
        detail = f"blocked: {block}; {delivery}"
        action = f"reckon crew resume --run {run_id} once the lane recovers"
    elif alive is False and retry_block:
        # A dead process whose stream ended mid-retry is a lane kill, not a
        # vanished worker: the budget block names the retry count, the process
        # table says the worker is gone, and the lane that refused is the most
        # triageable stop a fleet can suffer. Liveness is the verdict, never the
        # count alone — a live worker mid-retry-burst is exactly the
        # two-runs-that-succeeded case and reads running, not blocked — and
        # phase is not consulted, because a finished or killed run can still
        # carry a starting phase in its pointer. The next action offers resume
        # rather than discard because the lane, not the worker, owns the stop.
        classification = "blocked"
        block = (
            f"backend {retry_block['backend']!r} rate-limited the run "
            f"{retry_block['retries']} times and its process died mid-retry "
            f"({retry_block['limit_kind']})"
        )
        if not manifest_file_present:
            delivery = "no manifest was delivered and nothing has landed yet"
        else:
            delivery = (
                "the in-progress manifest at "
                f"{manifest} records what was already delivered"
            )
        detail = f"blocked: {block}; {delivery}"
        action = f"reckon crew resume --run {run_id} once the lane recovers"
    elif background_wait:
        # A vanished process is not the same fact as a crashed one: the run
        # directory itself says it was waiting on background work when it
        # ended, so it resumes rather than reading as abandoned and inviting a
        # redispatch that throws away an intact session. Whether it blocks or
        # pauses is the who-lifts-it rule: a run whose in-progress manifest
        # names committed work is parked on its own job and resumes when that
        # job ends, so it pauses; one with nothing committed needs a reader to
        # decide, so it stays blocked.
        if not manifest_file_present:
            delivery = "no manifest was delivered and nothing has landed yet"
        else:
            delivery = (
                "the in-progress manifest at "
                f"{manifest} records what was already delivered"
            )
        if manifest_commits:
            # The who-lifts-it rule, on the parked case: the run is waiting on
            # its own background job, its committed work is safe in the tree,
            # and the job ends on its own — so it pauses and names the end of
            # that work as the wake. The resume action is the follow-through
            # once the job ends, not the reason it paused.
            classification = "paused"
            detail = (
                f"paused: {background_wait}; the committed work is safe and the "
                "run resumes when the background work it was waiting on ends"
            )
            action = (
                f"resume run {run_id} when the background work it was waiting on ends"
            )
        else:
            classification = "blocked"
            detail = f"blocked: {background_wait}; {delivery}"
            action = f"reckon crew resume --run {run_id}"
    elif manifest_error and manifest_present and process_gone:
        # The third manifest outcome next to absent and readable-and-terminal:
        # a file that is present but that no supported reader can parse is
        # neither a delivered record nor an absence. The name states what the
        # reader is to do, and the refusal text (the parse error, naming the
        # format the file declared and why it was rejected) travels in the same
        # manifest_error channel the abandoned arm used so the operator's next
        # question is answerable one turn before the run can be judged.
        # A positively live process outranks this reading: the worker is still
        # in flight and its half-written or mid-write manifest is a condition of
        # that work, not an unreadable delivery, so the run reads running and a
        # reader answers where it is rather than reporting it unreadable.
        classification = "unreadable"
        detail = (
            f"the manifest at {manifest} is present but could not be read: "
            f"{manifest_error}"
        )
        # The named object is the manifest: the file is what needs repair, and
        # the abandoned instruction (which points at the launch log and offers
        # redispatch) must never read as the remedy for a file that exists.
        action = (
            f"the manifest at {manifest} cannot be read — repair or replace "
            "it before judging the run"
        )
    elif manifest_status in NON_TERMINAL_MANIFEST_STATUSES:
        # A worker-reported working status is evidence of life, not death. What
        # the process table says now happened after the worker's last word, so
        # the row reads working — the status stays on it rather than being
        # dropped — and never abandoned, whatever state the process is in.
        classification = "running"
        if alive is True:
            detail = (
                f"the worker manifest reports it is still working: {manifest_status}"
            )
        else:
            detail = (
                f"the worker manifest reports it was still working "
                f"({manifest_status}) when the process ended; the run is not "
                "reported dead"
            )
        action = f"reckon crew observe --run {run_id}"
    elif alive is False and _commits_beyond_base(record):
        # Committed work is proof the worker delivered, and the fact lives in
        # git rather than in any manifest format, so it survives a missing or
        # unreported manifest. A dead process with commits past its base to
        # show is not a vanished worker; it reads running and names the
        # committed work as what survived.
        commits_beyond_base = _commits_beyond_base(record)
        classification = "running"
        detail = (
            f"the worktree at {record.get('worktree')} carries "
            f"{commits_beyond_base} commit"
            f"{'s' if commits_beyond_base != 1 else ''} beyond its recorded "
            "base; the delivered work survives in git"
        )
        action = (
            f"inspect the worktree at {record.get('worktree')}; the committed "
            "work is safe and can be promoted or resumed once a manifest "
            "documents it"
        )
    elif alive is False and admission_refusal is not None:
        # A run the backend refused at admission is named for what it is rather
        # than folded into the abandoned bucket. The process is gone and no
        # manifest was delivered in both cases, but here the stop is that no
        # model ever served a turn — a fact the stream states and the generic
        # bucket cannot, so a reader is spared diagnosing a vanish as the lane
        # fault they already know about. The narrow arm sits beside the
        # abandoned reading and takes nothing from it: a genuine vanish carries
        # none of these marks and still reads abandoned.
        classification = "refused-at-admission"
        detail = (
            "refused at admission: the backend returned no turn "
            f"({admission_refusal['reason']!r}, terminal reason "
            f"{admission_refusal['terminal_reason']!r}) with every token "
            "counter zero; no model was reached"
        )
        action = (
            f"read the refusing stream {record.get('log_path')} and launch log "
            f"{record.get('stderr_path')}; the run never reached a model, and a "
            "resume replaces the pointer, so keep the stream as the durable "
            "record of the refusal"
        )
    elif terminal and alive is False:
        # Abandoned requires positive proof of death: the process table says
        # the worker is gone AND nothing eligible for promotion was delivered.
        # The stored phase alone is the last writer's label, not evidence, so
        # it only participates when the process verdict confirms it.
        classification = "abandoned"
        if manifest_derived:
            delivery = "only a recovery-derived manifest exists"
        elif not manifest_present:
            delivery = "no manifest was delivered"
        else:
            # A present-but-unreadable manifest never reaches this arm: it is
            # intercepted above as its own outcome before the terminal reading
            # can fold it into abandoned.
            delivery = f"the manifest status {manifest_status!r} is not usable"
        detail = (
            f"the stored phase is terminal but {delivery}; nothing is eligible "
            "for promotion"
        )
        action = (
            f"reckon crew resume --run {run_id} --advice "
            f"{shlex.quote(f'review {manifest} and replace it with a worker-written manifest')}"
            if manifest_derived
            else (
                f"read launch log {record.get('stderr_path')}; inspect the worktree at "
                f"{record.get('worktree')} and redispatch if needed"
            )
        )
    elif terminal:
        # A terminal stored phase is not a dead run while the process table
        # has not confirmed death. An alive process outranks the stored phase,
        # and a pid whose liveness cannot be checked is no proof of death
        # either, so the run is never called abandoned here and its action
        # never advises redispatch — duplicating a live worker is the cost
        # this arm exists to stop.
        if alive is True:
            classification = "running"
            detail = "the process is alive despite the terminal stored phase"
        else:
            classification = "running"
            detail = (
                "the stored phase is terminal but process liveness could not be "
                "proven; the pointer is left in place pending a manifest or "
                "evidence of death"
            )
        action = f"reckon crew observe --run {run_id}"
    elif phase == "launch-failed" or (
        ended_exit is not None and _exit_record_is_launch_failure(ended_exit)
    ):
        # A launch that never wrote a stream record reached no model, so this
        # is an infrastructure fault rather than a worker turn. It sits on its
        # own state so a reader sees it apart from a working run, and the lift
        # refuses it until a person acts. The exit record decides this from the
        # run directory, so a run whose pointer never reached the launch-failed
        # phase is read the same way as one the launcher labelled.
        failures = list(record.get("launch_failures") or ())
        latest = failures[-1] if failures else {}
        tail = str(latest.get("stderr_tail") or "").strip().splitlines()
        cause = tail[-1] if tail else "the process exited before any turn"
        classification = "launch-failed"
        if failures:
            detail = (
                f"the launch for backend "
                f"{latest.get('backend') or record.get('backend')!r} "
                f"exited with status {latest.get('exit_status')} before writing any "
                f"stream record ({cause}); {len(failures)} launch failure"
                f"{'s' if len(failures) != 1 else ''} recorded; no model was reached"
            )
        else:
            # The phase and the failure list are written by the launcher and the
            # exit record by the supervisor, so a pointer can hold the phase with
            # neither of the others behind it. Nothing was recorded to name then,
            # and naming an end anyway would be an invention.
            recorded_end = (
                _exit_record_end_phrase(ended_exit)
                if ended_exit is not None
                else "ended without a recorded exit"
            )
            detail = (
                f"the launch for backend {record.get('backend')!r} "
                f"{recorded_end} before writing any "
                f"stream record ({cause}); no model was reached"
            )
        action = (
            f"fix the command and PATH for backend "
            f"{latest.get('backend') or record.get('backend')!r}, then resume "
            f"{run_id} by hand — the lift loop stays stopped until then"
        )
    elif _stranded_launch(record, now_seconds=moment):
        # A launch cut off between composing its record and spawning a worker
        # leaves a pointer holding a pre-spawn phase and nothing else: no pid,
        # no worker record, no stream, no launch log. Nothing about it is in
        # flight and nothing about it ends, so without this reading it holds
        # whatever claim it took for as long as the pointer lives. The
        # launch-failed word is the one that already says no model was reached;
        # the clause adds what that arm cannot, that not even an end was
        # recorded, because here there was nothing to record one from.
        classification = "launch-failed"
        detail = (
            "a stranded launch: the pointer has held the pre-spawn phase "
            f"{str(record.get('phase') or '')!r} for more than "
            f"{STRANDED_LAUNCH_BOUND_SECONDS}s with no worker record, stream or "
            "launch log in its run directory, so the launch was cut off before "
            "it spawned and nothing recorded an end"
        )
        action = (
            f"compose run {run_id} again; the pointer holds a claim no run "
            "backs, and a review it claimed is released by the reflex"
        )
    elif alive is True:
        classification = "running"
        detail = "the process is alive"
        action = f"reckon crew observe --run {run_id}"
    elif alive is False:
        # The deliverable is read before the process, so this arm reaches only
        # a run with nothing to show. Every manifest reading that a killed
        # worker can leave behind — a complete status behind a parsed review, a
        # non-terminal status, an unreadable file, committed work past base,
        # a refusal or a lane stop in the stream — is arbitrated above this
        # point and never falls here, because a dead process says nothing about
        # what the run delivered before it died.
        classification = "abandoned"
        if ended_exit is not None:
            # The end is recorded rather than inferred from a vanished pid, so
            # the row states how the process ended and when, and the reader is
            # not left to reconstruct it from a launch log.
            detail = (
                f"the worker process {_exit_record_end_phrase(ended_exit)} "
                f"(recorded at {ended_exit.get('exited_at') or 'an unrecorded moment'}) "
                "without a complete manifest; nothing is eligible for promotion"
            )
        else:
            detail = (
                "the process is gone without a complete manifest; nothing is eligible "
                "for promotion"
            )
        action = (
            f"read launch log {record.get('stderr_path')}; the worktree at "
            f"{record.get('worktree')} is left in place for review and is never "
            "force-removed"
        )
    else:
        classification = "running"
        detail = (
            "an in-harness run: liveness belongs to the calling harness, so it "
            "is reported as running until a manifest appears"
        )
        action = f"reckon crew observe --run {run_id}"

    session_resolution = None
    resume_remedy = None
    if classification in RESUMPTION_READING_CLASSIFICATIONS:
        session_resolution = _blocked_session_resolution(record, run_id)
    if classification == INTERRUPTED_RUN_PHASE and session_resolution is not None:
        resume_remedy = _resume_remedy(session_resolution, run_id)
        if resume_remedy is not None:
            action = resume_remedy["command"]
            detail = (
                f"{detail}; session {resume_remedy['session_id']!r} survives in "
                f"the {resume_remedy['source']} record"
            )
        else:
            absent_evidence = str(
                session_resolution.get("detail")
                or "no session id was found in the available run evidence"
            )
            action = (
                f"inspect the worktree at {record.get('worktree')}, then redispatch "
                "the unfinished work"
            )
            detail = f"{detail}; redispatch is required because {absent_evidence}"
    if session_resolution is not None and (
        refusal_block is not None
        or retry_block is not None
        or exhaustion_block is not None
    ):
        resume_remedy = _resume_remedy(session_resolution, run_id)
        if resume_remedy is None:
            absent_evidence = str(
                session_resolution.get("detail")
                or "no session id was found in the available run evidence"
            )
            detail = f"{detail}; no resume remedy: {absent_evidence}"
            if action.startswith("reckon crew resume"):
                action = (
                    f"inspect the worktree at {record.get('worktree')} and launch "
                    "log; no session id is available to resume"
                )
    if classification in {"stopped", "abandoned"} and session_resolution is not None:
        resume_remedy = _resume_remedy(session_resolution, run_id)
        if resume_remedy is not None:
            # The run is over as a process but its session still holds every
            # turn, so the remedy is to continue it rather than discard or
            # redispatch the work it had already done. An arm whose own advice
            # is already a resume keeps it: the run holding only a
            # recovery-derived manifest must still be told to replace that
            # artifact, which is part of resuming rather than an alternative to
            # it, and the surviving session is named beside that advice.
            if not action.startswith("reckon crew resume"):
                action = resume_remedy["command"]
            detail = (
                f"{detail}; session {resume_remedy['session_id']!r} survives in "
                f"the {resume_remedy['source']} record"
            )

    hold = refusal_block or exhaustion_block or retry_block or budget_hold
    if classification == INTERRUPTED_RUN_PHASE:
        recovery_classification = INTERRUPTED_RUN_PHASE
    elif manifest_unwritten:
        # A live worker between its orientation write and its first status has
        # not failed to deliver, and the phase is already read from its stream;
        # the recovery word is read from the same evidence, or a run whose
        # stream is growing renders unwritten and a reader is sent to resume
        # work in flight. Motion is what buys the reading — see
        # :func:`_orientation_write_of_a_run_in_motion`.
        if _orientation_write_of_a_run_in_motion(
            record,
            alive=alive,
            worker_alive=worker_alive,
            manifest_text=manifest_text,
            manifest_data=manifest_data,
        ):
            recovery_classification = classification
        else:
            recovery_classification = "unwritten"
    elif classification in {"blocked", "paused"} and hold is not None:
        recovery_classification = "held"
    elif classification == "blocked" and needs_help_complete_value:
        recovery_classification = "needs-help"
    elif (
        classification == WAITING_STATUS
        and wait_observation is not None
        and wait_observation.get("state") == "met"
        and process_reading == "process gone"
    ):
        # Ready is the one classification that reads as an offer, and the
        # recovery a reader acts on is chosen from it. A met condition whose
        # worker's end nothing observed is therefore a wait: the run is held by
        # a process no reading has seen end, so it classifies as waiting and
        # the remedy stays the wait's own.
        recovery_classification = "ready"
    elif classification == WAITING_STATUS and wait and wait.get("overdue"):
        recovery_classification = "wait-aged"
    else:
        recovery_classification = classification
    recovery_verb = RECOVERY_VERBS[recovery_classification]
    if resume_remedy is not None and recovery_classification in {
        INTERRUPTED_RUN_PHASE,
        "stopped",
        "abandoned",
    }:
        # These arms otherwise advise disposing of the run — discard it, or
        # redispatch its work. A session that survives means the turns come
        # back with it, so continuing is the remedy.
        recovery_verb = "resume"

    lifting_condition = None
    if classification == WAITING_STATUS and wait is not None:
        lifting_condition = (
            f"{wait['condition']} reports one of {', '.join(wait['terminal'])}"
        )
    elif classification == "paused":
        if budget_hold is not None:
            lifting_condition = (
                f"the {budget_hold['limit_kind']} window resets at "
                f"{budget_hold['resets_at']}"
            )
        elif background_wait:
            lifting_condition = "the background work named by the row ends"
        else:
            lifting_condition = DEFAULT_LIFTING_CONDITIONS["paused"]

    timing = _budget_timing(record, now_seconds=now_seconds)
    timing.update(_budget_overrun_cause(record, timing, now_seconds=now_seconds))
    observed_phase = _observed_phase(
        phase,
        alive=alive,
        worker_alive=worker_alive,
        worker_record_names_pid=_worker_record_pid(record) is not None,
        ended_exit=ended_exit,
        manifest_status=manifest_status,
        commits_beyond_base=commits_beyond_base,
        # Read only where the label is still pre-spawn, which is the one case
        # the answer can change: every other phase the classifier reaches
        # already reads as the work it names, and the run's streams are not
        # read to confirm a label that says working.
        stream_shows_work=(
            phase in _PRE_SPAWN_PHASES and _newest_stream_shows_work(record)
        ),
    )
    lane_cause, stream_ended_at = (
        _terminal_lane_signal(record) if alive is False else (None, None)
    )
    exit_ended_at = (
        str(ended_exit.get("exited_at") or "") if ended_exit is not None else ""
    )
    lane_ended_at = next(
        (
            stamp
            for stamp in (exit_ended_at, stream_ended_at, terminal_at)
            if parse_utc(stamp)
        ),
        None,
    )
    identity = ledger.normalize_identity(record)
    classified = {
        "lane": identity.get("lane"),
        "model_key": identity.get("model_key"),
        "run_id": run_id,
        "backend": str(record.get("backend") or ""),
        "lane_cause": lane_cause,
        "lane_ended_at": lane_ended_at,
        "lane_event": None,
        "project": record.get("project"),
        # Several coordinator sessions share one project, so every read of a
        # run has to say whose it is. Without it a session reading the live
        # view cannot tell its own fleet from a peer's, and acting on a peer's
        # row is worse than not seeing it.
        "session": record.get("session"),
        "plan": (record.get("node") or {}).get("plan"),
        "node": (record.get("node") or {}).get("id"),
        "classification": classification,
        # Cause and remedy are separate from the compatibility lifecycle
        # grouping above. This pair is the authoritative instruction surface:
        # readers act on the verb and use the classification to understand why.
        "recovery_classification": recovery_classification,
        "recovery": recovery_verb,
        "lifting_condition": lifting_condition,
        "resets_at": (
            str(hold.get("resets_at") or "unknown")
            if recovery_classification == "held" and hold is not None
            else None
        ),
        # The phase the run's own evidence supports. The launcher's label is
        # kept beside it under ``stored_phase``: it is what the pointer last
        # recorded, and a reader comparing the two sees why a run left the
        # pre-spawn bucket without anything having run ``observe``.
        "phase": observed_phase,
        "stored_phase": phase,
        # The stored phase is the last launcher's label; the effective phase is
        # what the run's own evidence supports, so a run whose pointer never
        # advanced past starting reads from its stream and its process; it does
        # not sit in a pre-spawn label for its whole life.
        "effective_phase": (
            INTERRUPTED_RUN_PHASE
            if classification == INTERRUPTED_RUN_PHASE
            else observed_phase
        ),
        "interruption": interruption,
        # The run directory's own account of the worker's exit, when it was
        # consulted: carried whole so a reader sees the receipt — signal, exit
        # code, whether a model was reached, and when the supervisor wrote it —
        # rather than a verdict with no record behind it. None when no record
        # exists or a live process outranked it.
        "exit_record": ended_exit,
        "process_alive": alive,
        # Whether a line runs under the worker, carried beside its own liveness
        # because the two are one reading taken at one seam. None is "nothing
        # was asked": no pid this host may look at, or no live worker to look
        # under, so a reader never sees an absence where a check never ran.
        "process_descendant_alive": descendant_alive,
        # False when the stored answer was carried because the launching host
        # could not be shown to be this host, or there is no pid to ask about.
        # An unproven answer is not death, so a reader needing certainty reads
        # this field rather than treating a stale stored value as a verdict.
        "liveness_proven": liveness_proven,
        "manifest_present": manifest_present,
        "manifest_file_present": manifest_file_present,
        "manifest_fresh": manifest_present,
        "manifest_path": str(manifest) if str(manifest) != "." else "",
        # A living worker's complete or failed report remains on disk but is
        # not exposed as an outcome. The single-event watcher consumes this
        # field, so returning the raw report here would call the run terminal
        # while the classification and ticker correctly call it live.
        "manifest_status": None if deferred_outcome else manifest_status or None,
        # Keep the worker's raw spelling alongside the effective status. A
        # live process defers terminal-looking placeholders, while the one-shot
        # watcher still needs to recognise a fresh completion written by the
        # resumed attempt it is waiting for.
        "manifest_reported_status": manifest_reported_status or None,
        "manifest_derived": manifest_derived,
        "manifest_commits": manifest_commits,
        # Committed work past the recorded base, read from git on the abandoned
        # tail only. A surface that would otherwise read the same run dead
        # consults this field, so classification and the pane never disagree.
        "commits_beyond_base": commits_beyond_base,
        # The refusal text when a present manifest could not be read, carried on
        # the row so a surface that discards nothing has it one field away.
        "manifest_error": manifest_error or None,
        # A content digest of the manifest as read, so a watcher can tell a
        # rewrite that changed something from a touch that did not. None when
        # no manifest was present or readable, so absence never looks like a
        # digest to compare against.
        "manifest_digest": manifest_digest,
        # Review presence and readability are separate facts. An emitted review
        # that did not parse is evidence to repair, never an absent review that
        # can be silently regenerated without showing what the reviewer wrote.
        "review_present": review is not None or bool(review_error),
        "review_status": (
            "unreadable"
            if review_error
            else str(review.get("status") or "") or None
            if review is not None
            else None
        ),
        "review_error": review_error or None,
        "terminal_at": terminal_at,
        "terminal_age_seconds": terminal_age_seconds,
        "log_age_seconds": age,
        "log_fresh": None if age is None else age <= stale_after_seconds,
        **timing,
        "worktree": record.get("worktree"),
        "detail": detail,
        "next_action": action,
        # Set only for a blocked run: "?" when the escape-hatch question is
        # complete enough that `reckon crew resume --advice` can answer it,
        # "!" when the reader has to read the manifest itself. The fact behind
        # it travels too so the renderer derives the glyph instead of persisting it.
        "marker": marker,
        "needs_help_complete": needs_help_complete_value,
        "external_wait": wait,
        "wait_age_seconds": wait.get("age_seconds") if wait else None,
        "wait_overdue": wait.get("overdue") if wait else None,
        "wait_condition_state": (
            wait_observation.get("state") if wait_observation is not None else None
        ),
        "wait_observed": (
            wait_observation.get("observed") if wait_observation is not None else None
        ),
        # A declaration that lifted its run and came back unchanged is the
        # defect the lift loop's own stop cannot name; carried on the row so a
        # reader sees why the run is still parked rather than inferring it.
        "wait_key_defect": wait.get("wait_key_defect") or None if wait else None,
    }
    if session_resolution is not None:
        classified["session_resolution"] = session_resolution
    if resume_remedy is not None:
        classified["resume_remedy"] = resume_remedy
    # Lifecycle and fleet attention are distinct vocabularies. Publish both
    # from this observation so consumers never reread a stream or process to
    # derive the fleet's verdict, while lifecycle callers retain their contract.
    classified["fleet_verdict"] = _watch_verdict(
        record, classified, moment=moment, stall_seconds=stale_after_seconds
    )
    settled = classified["fleet_verdict"]["state"]
    if settled in FLEET_SETTLED_STATES:
        # The ledger's word also names the lifecycle reading, so a committed
        # completion cannot read as two different outcomes on one row.
        classified["classification"] = settled
    # The memo is written from the key the reads were made under, so a reader
    # that finds this file again serves it only while every input still holds
    # the identity it had here. The stream's own state travels whatever the key
    # says: it is where the next read resumes from, and the records it has
    # already folded are the same records whether or not anything else moved.
    memo["key"] = memo_key
    memo["inputs"] = memo_inputs
    _write_classification_memo(record, memo)
    return classified


# Commits-beyond-base per (worktree, base), keyed against the worktree head it
# was taken at. A tree that has not moved answers from here, so an unchanged
# poll spawns no git; a moved head is recounted. Placeholder head identities
# (no tree, no git dir, no head) are never cached, because they name a state
# that will change once the tree or its git directory appears.
_COMMITS_BEYOND_BASE_CACHE: dict[tuple[str, str], tuple[str, int]] = {}

# A live producer classifies the whole fleet on every poll, so this cache gains
# an entry per (worktree, base) it has ever classified. A worktree that has been
# reclaimed leaves its entries behind, and without a bound a long-lived producer
# accumulates one per run's worktree for as long as it runs. Past this many
# entries the cache drops the counts whose worktree is no longer on disk, then
# the oldest of what remains, so it stays bounded by the live fleet rather than
# by every run the producer has ever seen.
_COMMITS_BEYOND_BASE_CACHE_LIMIT = 256


def _evict_gone_worktrees() -> None:
    """Drop cached commit counts whose worktree is no longer on disk.

    A reclaimed worktree cannot move its head again, so its count can never be
    answered from the cache; the entry is dead weight the moment the directory
    goes. Removing it is the cheapest half of keeping the cache bounded, done
    only when the cache is at its ceiling so an ordinary poll pays nothing for
    it.
    """
    cache = _COMMITS_BEYOND_BASE_CACHE
    for key in [key for key in cache if not Path(key[0]).is_dir()]:
        cache.pop(key, None)


def _remember_commits(key: tuple[str, str], head: str, count: int) -> None:
    """Store one commit count, keeping the cache bounded first."""
    cache = _COMMITS_BEYOND_BASE_CACHE
    if len(cache) >= _COMMITS_BEYOND_BASE_CACHE_LIMIT and key not in cache:
        _evict_gone_worktrees()
        # Still full of live worktrees: drop the oldest so a fleet larger than
        # the ceiling evicts rather than growing without bound.
        while len(cache) >= _COMMITS_BEYOND_BASE_CACHE_LIMIT:
            cache.pop(next(iter(cache)), None)
    cache[key] = (head, count)


def _commits_beyond_base(record: Mapping[str, Any]) -> int:
    """Count commits in the worktree past the pointer's recorded base.

    The count lives in git, so it survives any manifest format: a worktree
    whose history carries commits after the base the run launched from
    delivered work, whatever the manifest says, and that fact is never erased
    by a missing or unreported manifest. Zero when the worktree or base is
    absent or the count cannot be read — an unreadable tree proves nothing, so
    it must not fabricate a rescue.

    The count is a function of the worktree's revision, which is read from the
    git directory as files rather than by a subprocess, so it is cached against
    that revision. A run whose tree has not moved is answered without spawning
    git again; when the head moves the count is taken afresh.
    """
    worktree_value = str(record.get("worktree") or "").strip()
    base = str(record.get("base_sha") or record.get("base") or "").strip()
    if not worktree_value or not base:
        return 0
    worktree = Path(worktree_value)
    if not worktree.is_dir():
        return 0
    head = _worktree_head_identity(worktree)
    cacheable = head not in {"no-tree", "no-git", "no-head"}
    key = (str(worktree), base)
    if cacheable:
        cached = _COMMITS_BEYOND_BASE_CACHE.get(key)
        if cached is not None and cached[0] == head:
            return cached[1]
    count = subprocess.run(
        ["git", "rev-list", "--count", f"{base}..HEAD"],
        cwd=worktree,
        capture_output=True,
        check=False,
    )
    if count.returncode != 0:
        return 0
    try:
        resolved = max(0, int(count.stdout.decode().strip()))
    except (ValueError, UnicodeDecodeError):
        return 0
    if cacheable:
        _remember_commits(key, head, resolved)
    return resolved


def _worktree_diff_paths(record: Mapping[str, Any]) -> list[str]:
    """Return the base-to-worktree path census used for recovery evidence."""
    worktree_value = str(record.get("worktree") or "").strip()
    base = str(record.get("base_sha") or record.get("base") or "").strip()
    if not worktree_value or not base:
        return []
    worktree = Path(worktree_value)
    if not worktree.is_dir():
        return []
    tracked = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", "-z", base, "--"],
        cwd=worktree,
        capture_output=True,
        check=False,
    )
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=worktree,
        capture_output=True,
        check=False,
    )
    if tracked.returncode or untracked.returncode:
        return []
    paths = {
        os.fsdecode(raw)
        for raw in (*tracked.stdout.split(b"\0"), *untracked.stdout.split(b"\0"))
        if raw
    }
    return sorted(paths)


def _derived_manifest_text(record: Mapping[str, Any], paths: list[str]) -> str:
    """Render evidence that recovery found without claiming worker delivery."""
    node = str((record.get("node") or {}).get("id") or record.get("run_id") or "")
    final_message = " ".join(str(record.get("final_message") or "").split())
    changed = ", ".join(paths) or "none"
    evidence = (
        f"final message: {final_message}" if final_message else "final message: none"
    )
    return (
        f"node: {node}\n"
        "status: derived\n"
        "derived: true\n"
        "derived_reason: terminal run omitted its worker manifest\n"
        "commits: none\n"
        f"changed_paths: {changed}\n"
        "tests: not verified — worker manifest missing\n"
        "test_logs: none\n"
        "baseline_suite: none\n"
        "after_suite: none\n"
        "artifacts: none\n"
        f"evidence_inputs: {evidence}\n"
        "follow_ons: none\n"
        "blockers: replace this derived artifact with a worker-written manifest\n"
    )


def _derive_missing_manifest(
    record: Mapping[str, Any], *, config: Mapping[str, Any] | None
) -> Mapping[str, Any]:
    """Preserve terminal evidence without turning it into delivered work."""
    fences = (config or {}).get("fences") or {}
    if fences.get("manifest_required", True) is False:
        return record
    if str(record.get("phase") or "") not in {"complete", "failed"}:
        return record
    manifest_value = str(record.get("manifest_path") or "")
    if not manifest_value:
        return record
    manifest = Path(manifest_value)
    if manifest.exists():
        return record
    if _commits_beyond_base(record):
        # The work is already committed past the recorded base: git is the
        # evidence, and no recovery artifact should be fabricated over it with
        # a "commits: none" that the history contradicts.
        return record
    paths = _worktree_diff_paths(record)
    final_message = str(record.get("final_message") or "").strip()
    if not paths and not final_message:
        return record
    manifest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with manifest.open("x", encoding="utf-8") as handle:
            handle.write(_derived_manifest_text(record, paths))
    except FileExistsError:
        # Worker delivery won the race and remains authoritative.
        return record

    run_id = str(record.get("run_id") or "")

    def record_gap(pointer: dict[str, Any]) -> dict[str, Any]:
        pointer["delivery_gap"] = {
            "kind": "missing-worker-manifest",
            "derived_manifest_path": str(manifest),
            "derived_at": _utc_now(),
            "final_message_present": bool(final_message),
            "changed_paths": paths,
        }
        return pointer

    return _mutate_pointer(run_id, record_gap) if run_id else record


def closure_disposition_valid(disposition: str, classification: str) -> bool:
    """Whether a recorded closure disposition excuses a pointer from the fences.

    This is the single definition of ``reconciled`` shared by the closure drain
    and the dispatch fence, so a pointer the drain counts as reconciled is never
    refused by dispatch, and a pointer either surface still calls unreconciled
    is refused on both. A ``handed-off`` disposition remains valid until the
    receiving session reconciles the pointer; ``still-working`` excuses only a
    pointer whose current classification is still ``running``. Any missing,
    malformed or unknown disposition, or a disposition outlived by its run, is
    not valid.
    """
    return disposition == "handed-off" or (
        disposition == "still-working" and classification == "running"
    )


def _partition_session_rows(
    rows: Iterable[Mapping[str, Any]], session: str | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Separate one coordinator's rows from visible peer-session rows.

    Omitting a session preserves the project-wide interpretation: every row is
    counted and there is no peer partition. Supplying one uses the dispatching
    session already persisted on each pointer, so aiming the fence adds no
    ownership state of its own. A legacy row with no recorded owner remains in
    the counted set: absence cannot prove that a live pointer belongs to a peer.
    """
    copied = [dict(row) for row in rows]
    if session is None:
        return copied, []
    own: list[dict[str, Any]] = []
    peers: list[dict[str, Any]] = []
    for row in copied:
        owner = str(row.get("session") or "")
        (own if not owner or owner == session else peers).append(row)
    return own, peers


def overdue_unreconciled_runs(
    *,
    project: str,
    grace: str,
    now_seconds: float | None = None,
) -> list[dict[str, Any]]:
    """Return actionable terminal pointers older than the configured grace.

    A pointer is listed only when it is terminal past the grace AND not excused
    by a recorded closure disposition: the same predicate the closure drain
    uses, so a past-grace pointer that carries no valid disposition is still
    refused here (the forgotten-work case) while one the drain counts as
    reconciled is never refused.
    """
    if not grace:
        return []
    grace_seconds = parse_duration(grace)
    rows = []
    for pointer in list_live(project=project):
        row = classify_pointer(pointer, now_seconds=now_seconds)
        age = row.get("terminal_age_seconds")
        if (
            row["classification"]
            in {"scoring", "promotable", "completed_unpromoted", "blocked", "paused"}
            and isinstance(age, int)
            and age > grace_seconds
        ):
            recorded = pointer.get("closure_disposition")
            disposition = (
                str(recorded.get("kind") or "") if isinstance(recorded, Mapping) else ""
            )
            if not closure_disposition_valid(disposition, row["classification"]):
                rows.append(row)
    return rows


def _utc_seconds() -> float:
    """Current time as epoch seconds, matching a file mtime's clock."""
    return datetime.now(tz=timezone.utc).timestamp()


@contextmanager
def _watch_registration(project: str, stall_window: str):
    """Register a watcher together with the process responsible for reaping it."""
    with _project_watch_claim(project, stall_window) as (acquired, watcher):
        if acquired:
            parent_pid = os.getppid()
            watcher.update(
                {
                    "parent_pid": parent_pid,
                    "parent_start_time": _process_start_time(parent_pid),
                }
            )
            # The record is written through the seat handle the claim holds,
            # never by reopening the path: an unlink in the moment between
            # taking the seat and this write would make the reopen raise
            # FileNotFoundError and end the producer before it polls once.
            _write_watch_record(runs._WATCH_SEAT_HANDLES[project], watcher)
        yield acquired, watcher


UNWATCH_SEAT_WAIT_SECONDS = 2.0


def unwatch(project: str) -> dict[str, Any]:
    """Stop the local watcher, refusing remote and unresponsive seat holders."""
    path = watch_lock_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    lease = runs.watch_host_lease(project)
    holder = lease.holder()
    if holder is not None and holder.host != socket.gethostname():
        raise CrewError(
            f"refusing to unwatch {project!r}: producer seat held by "
            f"{holder.host} pid {holder.pid} job {holder.job or 'unknown'}"
        )
    with path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            watcher = _read_watch_record(handle)
            if runs._seat_names_a_foreign_host(watcher):
                raise CrewError(
                    f"refusing to unwatch {project!r}: producer seat names "
                    f"{watcher['host']} pid {watcher.get('pid')}"
                ) from None
            registered_project = str(watcher.get("project") or "")
            if registered_project != project:
                raise CrewError(
                    f"refusing to stop watcher for project {project!r}: "
                    f"the locked registration names {registered_project!r}"
                )
            try:
                pid = int(watcher.get("pid"))
            except (TypeError, ValueError) as exc:
                raise CrewError(
                    f"refusing to stop watcher for project {project!r}: "
                    "the locked registration has no valid pid"
                ) from exc

            # A watcher has no run directory, so the watch directory the seat
            # registration sits in is the sender file's home. The shared writer
            # owns the attribution and outcome records, so unwatch names that
            # directory and the project the watcher serves rather than writing
            # an attribution of its own: the project rides its own field, which
            # a reader of the shared directory can act on without parsing a
            # message.
            try:
                _signal_process_group(
                    pid,
                    watcher.get("pid_start_time"),
                    run_dir=path.parent,
                    reason="unwatch",
                    project=project,
                )
            except ProcessLookupError:
                stopped = False
                reason = "watcher-exited"
                detail = (
                    f"watcher pid {pid} exited before it could be signalled; "
                    "its registration was released"
                )
            else:
                stopped = True
                reason = "stopped"
                detail = f"stopped registered watcher pid {pid}"

            # The watcher owns this lock until its process exits. Taking it
            # before clearing the record makes registration release observable
            # to a subsequent arming command, without replacing the lock inode.
            deadline = time.monotonic() + UNWATCH_SEAT_WAIT_SECONDS
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise CrewError(
                            f"refusing to unwatch {project!r}: producer seat held "
                            f"by {watcher.get('host') or socket.gethostname()} "
                            f"pid {pid} beyond {UNWATCH_SEAT_WAIT_SECONDS:g}s"
                        ) from None
                    time.sleep(0.05)
            _write_watch_record(handle, {})
            if holder is not None:
                lease.release_holder(holder)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return {
                "project": project,
                "stopped": stopped,
                "registration_released": True,
                "reason": reason,
                "detail": detail,
                "watcher": watcher,
            }

        watcher = _read_watch_record(handle)
        _write_watch_record(handle, {})
        if holder is not None:
            lease.release_holder(holder)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return {
            "project": project,
            "stopped": False,
            "registration_released": True,
            "reason": "nothing-to-stop",
            "detail": f"project {project!r} has no registered watcher to stop",
            "watcher": watcher,
        }


def agent_label(pointer: Mapping[str, Any]) -> str:
    """Compact `model/effort` — or `alias·effort` — for the ticker.

    Read from the configuration persisted at dispatch rather than from current
    flight config, because a later config change must not silently restate what
    ran. The alias and its effort spelling are display decisions frozen at
    dispatch, so an aliased run renders the alias in place of the model it
    shortens; the composition is the renderer's so the two cannot drift. A run
    dispatched before aliases existed carries no alias and keeps the
    precomposed `model/effort` form it rendered then. Absent fields are simply
    omitted: a partial label is still useful and an invented one is not.
    """
    agent = pointer.get("agent")
    if not isinstance(agent, Mapping):
        return ""
    alias = str(agent.get("alias") or "").strip()
    if not alias:
        model = str(agent.get("model") or "").strip()
        effort = str(agent.get("effort") or "").strip()
        if model and effort:
            return f"{model}/{effort}"
        return model or effort
    return _agent_label(agent)


def _pointer_role(pointer: Mapping[str, Any]) -> str:
    """The dispatch role a run carried, from its own record.

    Read from the persisted pointer rather than current config for the same
    reason :func:`agent_label` reads its agent block from the record: a later
    role change must not restate what actually ran. Dispatch writes the role on
    the record root and on the node, so either spelling is accepted. The display
    narrowing (``documentation`` to ``docs``, unknown to the marker) happens in
    the renderer where the column lives; the snapshot threads the raw spelling.
    """
    role = str(pointer.get("role") or "").strip()
    if not role:
        role = str(((pointer.get("node") or {}) or {}).get("role") or "").strip()
    return role


# A state that needs action always may explain itself, and so may a member of
# the waiting family: the clause on a waiting row names what lifts it and on a
# blocked row names what a reader can do, so the explained set is the action
# set plus the self-lifting family — an overdue wait is explained by both
# routes at once. An unreadable manifest is one of the actionable states,
# because the refusal text naming the rejected format is the one sentence a
# reader needs before repairing the file.
EXPLAINED_STATES = frozenset(
    NEEDS_ACTION
    | WAITING_STATES
    | {"unreadable", "unwritten", "ended-without-manifest"}
)


def _promote_record_holds(record: Mapping[str, Any]) -> bool:
    """Whether this live pointer's completed run has a committed ledger row.

    Promotion appends the run's ledger row and then removes the live pointer,
    so for the length of that window the pointer still exists while the work
    has already landed. A classifier that reads only the pointer sees a
    completed manifest whose review no longer matches the moved head and calls
    the run unpromoted — a settled completion reported as unfinished work. The
    ledger row settles the run even when it declares no repository change, so
    row presence, rather than its commit list, answers this question.

    The row is read from the run's own repository, the root promotion wrote it
    under. Promotion writes the per-run file before it touches the aggregate,
    so a single stat answers whether the row was committed.
    An unreadable or absent row answers False — the run then takes the word its
    pointer earns, which is the safe direction because the alternative promises
    a landing nothing recorded.
    """
    from reckon import ledger as ledger_module

    run_id = str(record.get("run_id") or "")
    project = str(record.get("project") or "")
    # The row is read from the run's own repository, so a pointer that records
    # none cannot say where the row would be. Resolving a default root instead
    # would answer a promotion from a directory the run does not own — a row
    # another run or a fixture left there would read as this run's landing.
    repo = str(record.get("repo") or "")
    if not run_id or not project or not repo:
        return False
    try:
        return ledger_module.run_path(project, run_id, repo).is_file()
    except (OSError, ValueError, ledger_module.LedgerError):
        return False


def _recorded_pointer_word(record: Mapping[str, Any]) -> str:
    """Name a settled pointer's committed work or recorded completion.

    The row existence check already established settlement. A row that cannot
    be decoded cannot establish a code landing, so its safe word is recorded.
    """
    from reckon import ledger as ledger_module

    try:
        path = ledger_module.run_path(
            str(record.get("project") or ""),
            str(record.get("run_id") or ""),
            str(record.get("repo") or ""),
        )
        row = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ledger_module.LedgerError):
        return "recorded"
    return (
        "promoted"
        if isinstance(row, Mapping)
        and row.get("run_id") == record.get("run_id")
        and row.get("commits")
        else "recorded"
    )


def _stall_window_seconds(row: Mapping[str, Any], stall_seconds: int) -> int:
    """The window a quiet run is judged against before it reads stalled.

    A run whose worker process is alive with a live process under it is not
    idle: the child is the job the worker is waiting on — a build, a scheduler
    reservation, a probe — and the worker's own stream stays silent for as long
    as the child takes, so the silence says nothing about whether the worker is
    hung. For that shape the window extends to the run's own time budget, the
    allowance the run declared for exactly this work; past it the run's own
    fence speaks. The extension is never a narrowing, so a budget shorter than
    the window leaves the window where it was.

    A run whose worker has no live process under it keeps the window, and that
    is the deliberate narrowing: a live worker with nothing running beneath it
    is the case a stall most often means is hung.
    """
    if (
        row.get("process_alive") is not True
        or row.get("process_descendant_alive") is not True
    ):
        return stall_seconds
    budget_seconds = _int_or_none(row.get("budget_seconds"))
    if budget_seconds is None:
        return stall_seconds
    return max(stall_seconds, budget_seconds)


def _stall_reading(
    state: str,
    detail: str,
    *,
    quiet: int | None,
    window: int,
    pause_reason: str | None,
    process_state: str | None,
) -> tuple[str, str]:
    """Apply the stream-silence reading to a non-terminal state.

    The only part of a verdict that moves with the clock rather than with a
    file: a run that was working and has gone quiet past its window is paused
    when something explains the silence and stalled when nothing does. It is a
    pure function of the state, the silence and the window so a producer that
    reuses a snapshot can re-derive exactly this decision from stored inputs,
    without reading a stream or a manifest again: the silence is recomputed from
    the stream's own stat and the rest is carried on the verdict.
    """
    if state not in ("dispatched", "working"):
        return state, detail
    if quiet is None or quiet <= window:
        return state, detail
    if pause_reason is not None:
        return (
            WAITING_STATUS,
            f"paused: sitting in {pause_reason} for {quiet}s; it lifts itself",
        )
    return "stalled", f"{process_state}, quiet {quiet // 60}m"


def _watch_verdict(
    pointer: Mapping[str, Any],
    row: Mapping[str, Any],
    *,
    moment: float,
    stall_seconds: int,
) -> dict[str, Any]:
    """Complete individual and grouped verdicts with the fleet vocabulary.

    Producers carry this result without probing processes, streams or manifests
    for a second judgement.
    """
    if row.get("lane_event"):
        state = "lane-event"
        previous = row.get("fleet_verdict")
        return {
            **(previous if isinstance(previous, Mapping) else {}),
            "state": state,
            "detail": str(row.get("detail") or ""),
            "recovery_classification": state,
            "recovery": str(row.get("recovery") or "inspect"),
            "lifting_condition": None,
        }
    stored_phase = str(pointer.get("phase") or "")
    # The stored phase is the launcher's label; the row carries the phase the
    # run's own evidence supports, so a pointer that never advanced past
    # starting does not pin the run in a pre-spawn bucket while it works.
    phase = str(row.get("effective_phase") or stored_phase)
    classification = str(row.get("classification") or "")
    alive = row.get("process_alive")

    if _promote_record_holds(pointer):
        # A committed row outranks every reading of the pointer, including a
        # report-only completion with no code commit. The pointer remains for
        # a short window after that row lands; both outcomes are settled there.
        state = _recorded_pointer_word(pointer)
        return {
            "state": state,
            "detail": "",
            "recovery_classification": state,
            "recovery": "",
            "lifting_condition": None,
        }

    # The working bucket is keyed on a process that is genuinely still alive,
    # never on the record phase alone: a run whose process died at any phase it
    # held — the starting phase included — has stopped working regardless of the
    # label the last writer left behind. classify_pointer has already checked
    # the process table, so a dead process falls through to the abandoned state
    # a coordinator must act on instead of the stale working label. A manifest
    # that has reached a verdict likewise cannot keep a run in working, so the
    # terminal readings are arbitrated before any working state is chosen. The
    # classifier also defers complete and failed reports while the pointer says
    # their process is alive, so this reducer consumes that decision instead of
    # deriving a second verdict from the manifest.
    if classification == "scoring":
        state = "completed_unpromoted"
    elif classification in {"promotable", "completed_unpromoted"}:
        state = "complete"
    elif classification == WAITING_STATUS:
        # A declared external wait stays in the waiting family even when it has
        # aged past its expectation — the run has not failed, so the fleet keeps
        # counting it as waiting rather than blocked. The age is the news, and
        # the news is carried by the action marker on the row a reader sees.
        state = "wait-aged" if row.get("wait_overdue") else WAITING_STATUS
    elif classification == "paused":
        # A paused run is a member of the waiting family: nothing needs a
        # person, so it renders under the same calm verb as a declared external
        # wait and its classifier detail names what lifts it. Rendering it as
        # its own grid word would need a second routing route with no reader
        # benefit — the distinction that matters is that it is not blocked.
        state = WAITING_STATUS
    elif classification == INTERRUPTED_RUN_PHASE:
        # The compatibility watch vocabulary does not yet expose interruptions
        # as their own column. Keep the run in the needs-action bucket while the
        # row's recovery classification and action retain the precise cause.
        state = "blocked"
    elif classification in {"blocked", "failed"}:
        # A provider refusal blocks even though no manifest reached a verdict:
        # the process is gone, but the stop is triageable and resumable once
        # the limit lifts, so it reads as a block rather than an abandonment.
        state = classification
    elif classification == "unreadable":
        # A manifest that is present but unreadable is neither a delivery nor
        # an absence, so the run reads as unreadable rather than falling into
        # the abandoned bucket the liveness checks below would assign it.
        state = "unreadable"
    elif classification == "exited-unfinished":
        # The worker's recorded exit ended the run with its work committed
        # while the manifest still reads a working status. Nothing about the
        # stop is unresolved, so the row is neither a block nor an abortion:
        # it reads as its own state and the clause names the verdict word the
        # record still needs.
        state = "exited-unfinished"
    elif str(row.get("recovery_classification") or "") == "unwritten":
        # The compatibility state stays non-terminal while the typed surface
        # names that the worker never replaced its template. This keeps a
        # placeholder from satisfying a terminal fence without inventing a
        # second attention vocabulary in the run registry.
        state = "running"
    elif phase == "stopped":
        state = "stopped"
    elif alive is False:
        # Abandoned means the worker died with nothing of the run surviving it.
        # A dead process whose manifest reported it was still working, or whose
        # worktree carries commits past its base, left work that outlived the
        # process: the classifier reads it running, and this reducer must not
        # paint the same run dead or the pane would disagree with the reader.
        if classification == "running" and (
            (row.get("manifest_status") or "") in NON_TERMINAL_MANIFEST_STATUSES
            or row.get("commits_beyond_base")
        ):
            state = "working"
        else:
            state = "abandoned"
    elif classification == "running" or phase in {"working", "running"}:
        state = "dispatched" if phase == "starting" else "working"
    else:
        state = classification or phase or "unknown"

    # A dead worker is a row on the snapshot that observes the death, never one
    # held behind the stall window. The deferrals above read a non-terminal
    # manifest or retained commits as the worker's own last word, and that word
    # was written before the process ended; the process table has now falsified
    # it, so the deferral stops here. Which end this was is the stream's to say:
    # a last record of result is a turn that ran to its own conclusion, whose
    # remedy is to continue it, while any other last record is a death
    # mid-turn, whose cause a reader has to see before choosing a recovery.
    # The row then reads as the classifier's interrupted run — the reading it
    # already owns for a worker that died — rather than as a second vocabulary
    # this reducer would have to keep in step.
    death_reason = None
    ended_without_manifest = False
    if alive is False and state in ("dispatched", "working"):
        last_record_type = _newest_stream_last_record_type(pointer)
        if last_record_type == STREAM_RESULT_RECORD_TYPE:
            # The worker's process ended after a successful result record and
            # before a terminal manifest: its turn concluded and the record that
            # says so is on disk. Nothing was lost, the session is resumable, and
            # the reading names that rather than the mid-turn death a stalled row
            # would report — a resumable turn has no session to conclude rather
            # than one whose turn was cut off.
            ended_without_manifest = True
            state = "blocked"
        elif last_record_type is not None:
            death_reason = _process_exit_reason(pointer, last_record_type)
            state = "blocked"

    detail = str(row.get("detail") or "")
    for prefix in (
        "the worker manifest reports blocked: ",
        "the worker manifest reports failed: ",
    ):
        if detail.startswith(prefix):
            detail = detail[len(prefix) :]
            break

    # The stream-silence reading is the only part of a verdict that moves with
    # the clock rather than with a file, so it is isolated behind a pure
    # helper and its inputs are exposed on the verdict below. A producer that
    # reuses a snapshot recomputes the silence from the stream's own stat and
    # re-derives exactly this decision without reading anything again.
    stall_base_state = state
    stall_base_detail = detail
    quiet_seconds = None
    stall_window = _stall_window_seconds(row, stall_seconds)
    stall_pause_reason = None
    stall_process_state = None
    if death_reason is not None:
        # The classifier's clause for this record says the run was still
        # working when the process ended, which is the reading this branch
        # exists to replace; the death row states the end it observed instead.
        detail = death_reason
    elif state in ("dispatched", "working"):
        # A run stops progressing whether it dies during dispatch or mid-work,
        # so the stall check has to reach every non-terminal state a pointer
        # can sit in — gating it on "working" alone left a run killed before
        # its phase ever advanced past "starting" permanently exempt.
        quiet_seconds = _run_stream_quiet_seconds(pointer, now_seconds=moment)
        # A quiet stream is a hang only when nothing is waiting. An alive
        # worker sitting in a bounded wait — a sleep, a peer read, a task
        # wait, a rejected window, or a rate-limit retry loop — wakes itself,
        # so it pauses rather than stalling; a genuinely hung process with none
        # of those still stalls and is not weakened here. The stall word covers
        # three situations whose remedies differ: a live worker in a long quiet
        # step needs nothing, a dead one needs a resume, and one whose liveness
        # nothing established needs the check a reader would otherwise run by
        # hand. Death is claimed only where something observed it, and two
        # things can: a pid checked on this host and found dead, or the
        # supervisor's exit record.
        stall_pause_reason = _stall_wait_reason(pointer)
        stall_process_state = _process_reading(
            alive,
            liveness_proven=row.get("liveness_proven") is True,
            exit_record=row.get("exit_record"),
        )
        state, detail = _stall_reading(
            state,
            "",
            quiet=quiet_seconds,
            window=stall_window,
            pause_reason=stall_pause_reason,
            process_state=stall_process_state,
        )
        stall_base_detail = ""
    elif state not in EXPLAINED_STATES:
        # Named as the states that MAY explain themselves rather than the ones
        # that may not. An allow-list of states to clear leaves every state
        # added later carrying whatever the classifier attached, which makes
        # routine progress read as a warning.
        detail = ""

    recovery_classification = str(row.get("recovery_classification") or state)
    recovery_verb = str(row.get("recovery") or "")
    lifting_condition = row.get("lifting_condition")
    if death_reason is not None:
        # A death row carries the classifier's own word for a worker that died,
        # so the cause and remedy a reader sees match what every other surface
        # already calls it rather than a second vocabulary composed here.
        recovery_classification = INTERRUPTED_RUN_PHASE
        recovery_verb = RECOVERY_VERBS[INTERRUPTED_RUN_PHASE]
    elif ended_without_manifest:
        # The classifier's clause for this pointer is about the commits that
        # survived the process; this state is about the end itself, so the
        # reading names the result record that says the turn ended. The end is
        # the clause's own first words because the row is cut to its head: a
        # reader who sees only that much still learns which end this was, and
        # the remedy that follows from it.
        recovery_classification = "ended-without-manifest"
        recovery_verb = RECOVERY_VERBS["ended-without-manifest"]
        lifting_condition = None
        detail = (
            "turn ended: the worker's process exited after a successful result "
            "record and no terminal manifest followed; the run is resumable "
            "rather than stalled"
        )
    elif state == "stalled":
        recovery_classification = "stalled"
        recovery_verb = RECOVERY_VERBS["stalled"]
        lifting_condition = None
    elif state == "wait-aged":
        recovery_classification = "wait-aged"
        recovery_verb = RECOVERY_VERBS["wait-aged"]
    elif state == WAITING_STATUS and classification == "running":
        recovery_classification = "paused"
        recovery_verb = RECOVERY_VERBS["paused"]
        lifting_condition = detail

    return {
        "state": state,
        "detail": detail,
        "recovery_classification": recovery_classification,
        "recovery": recovery_verb,
        "lifting_condition": lifting_condition,
        # The stream-silence inputs, carried so a producer that reuses a
        # snapshot can re-derive the same reading from the stream's stat alone.
        # ``stall_base_state`` is None for a verdict the silence reading never
        # touched, which is what marks a snapshot whose state needs no refresh.
        "stall_base_state": stall_base_state if quiet_seconds is not None else None,
        "stall_base_detail": "" if quiet_seconds is not None else None,
        "stall_window_seconds": stall_window,
        "stall_pause_reason": stall_pause_reason,
        "stall_process_state": stall_process_state,
        "stall_quiet_seconds": quiet_seconds,
    }


def _quiet_clock_latest(record: Mapping[str, Any], *, moment: float) -> float:
    """The latest write instant a run's stream evidence offers, by stat alone.

    The same clock ``runs._stream_quiet_seconds`` falls back to: the pointer's
    own log, then the pointer's mtime, then the run's creation. Read here as an
    absolute instant rather than a delta so a producer reusing a snapshot can
    recompute the silence at a later moment without reading the file again.
    """
    stream = Path(str(record.get("log_path") or ""))
    try:
        if stream.is_file():
            return stream.stat().st_mtime
    except OSError:
        pass
    run_id = str(record.get("run_id") or "")
    if run_id:
        pointer = runs.pointer_path(run_id)
        try:
            if pointer.is_file():
                return pointer.stat().st_mtime
        except OSError:
            pass
    created = parse_utc(str(record.get("created_at") or ""))
    return moment if created is None else created.timestamp()


def _snapshot_reuse_key(record: Mapping[str, Any]) -> str | None:
    """The identity of every input a run's classification is a function of.

    The classification's own composition, as :func:`_classification_inputs`
    resolves it: the pointer, manifest, stream, the run's exit, worker and
    attempt records, the review store's candidates for this run, and the
    worktree's git head. A review landing on a reviewer's target, or its head
    moving, drops the snapshot exactly as it moves the classification. The
    promotion ledger row joins the composition because a promotion writes it
    while the pointer still exists and no other input moves with it. Liveness
    is deliberately not here: it is not a file, so the reuse path reads it
    fresh through the shared host-gated reader.
    """
    run_id = str(record.get("run_id") or "")
    if not run_id:
        return None
    log = Path(str(record.get("log_path") or ""))
    parts = [_classification_key(_classification_inputs(record, log))]
    # The newest stream a run has may differ from the pointer's own log once a
    # resume or a lane change writes beside it; its identity joins the key so a
    # record appended to a resumed stream drops the snapshot.
    found = _record_newest_stream(record)
    parts.append(
        f"stream={_file_identity(found[0]) if found is not None else 'absent'}"
    )
    project = str(record.get("project") or "")
    repo = str(record.get("repo") or "")
    if project and repo:
        from reckon import ledger as ledger_module

        try:
            promote = ledger_module.run_path(project, run_id, repo)
        except (OSError, ValueError):
            promote = None
        parts.append(
            f"promote={_file_identity(promote) if promote is not None else 'absent'}"
        )
    return "|".join(parts)


_SNAPSHOT_CACHE: dict[str, tuple[str, dict[str, Any]]] = {}
_SNAPSHOT_CACHE_LIMIT = 256


def _remember_snapshot(
    run_id: str,
    key: str,
    snapshot: dict[str, Any],
    *,
    cache: dict[str, tuple[str, dict[str, Any]]] | None = None,
) -> None:
    """Store one run's snapshot, keeping the cache bounded.

    Re-storing a run moves it to the most-recent position: a plain assignment
    keeps a key at its original insertion place, so a run stored early and
    updated every poll would sit at the front and be the first evicted by a
    busy process once the cache filled — the entry the poll just wrote.
    """
    store = _SNAPSHOT_CACHE if cache is None else cache
    store.pop(run_id, None)
    store[run_id] = (key, snapshot)
    if len(store) <= _SNAPSHOT_CACHE_LIMIT:
        return
    for rid in list(store):
        if rid != run_id and not runs.run_dir(rid).is_dir():
            del store[rid]
    while len(store) > _SNAPSHOT_CACHE_LIMIT:
        store.pop(next(iter(store)))


def _fresh_liveness(pointer: Mapping[str, Any]) -> tuple[Any, Any, Any]:
    """The process readings a snapshot reports, taken fresh from the process table.

    The same host-gated reading classify_pointer composes: liveness through
    ``local_liveness``, and — only where this host issued the pid — whether
    anything still runs beneath the worker. A signal-0 probe, not a storage
    read, so a poll over a run whose files have not moved takes it rather than
    trusting the reading the previous poll happened to observe.
    """
    alive, proven = local_liveness(pointer)
    descendant: Any = None
    if proven and alive is True:
        worker_pid = (
            _worker_record_pid(pointer)
            if _worker_record_liveness(pointer) is True
            else None
        )
        if worker_pid is None:
            worker_pid = _int_or_none(pointer.get("pid"))
        descendant = _live_descendant(worker_pid) if worker_pid is not None else None
    return (alive, proven, descendant)


def _refresh_snapshot(
    snapshot: Mapping[str, Any], *, moment: float, stall_seconds: int
) -> dict[str, Any]:
    """Re-derive a reused snapshot's clock-derived fields from its stats.

    Everything a snapshot carries was read from files that have not moved, so
    only the silence — the one reading that grows with the clock — is
    recomputed, from the stream instant and the attempt clock already recorded
    on the snapshot and the moment this poll reports. The state and detail are
    then re-derived through the same helper the full recompute uses, so a
    reused snapshot can never disagree with one classified afresh at the same
    moment.

    The window the silence is judged against is recomputed from the calling
    producer's own ``stall_seconds`` rather than read from the frozen snapshot.
    A snapshot is served under a reuse key that does not carry the window, so
    two producers watching one unchanged run with different windows reach the
    same entry; each must judge the run against its own window or one caller's
    verdict leaks into the other's. Everything :func:`_stall_window_seconds`
    reads travels on the snapshot — the liveness pair and the declared budget —
    so the recomputation sees exactly the inputs the full classification saw,
    and the reuse key is left unchanged.
    """
    refreshed = dict(snapshot)
    window = _stall_window_seconds(snapshot, stall_seconds)
    refreshed["stall_window_seconds"] = window
    stream_seconds = snapshot.get("stall_stream_seconds")
    launch_seconds = snapshot.get("stall_launch_seconds")
    quiet: int | None = None
    if stream_seconds is not None or launch_seconds is not None:
        candidates = [
            moment - seconds
            for seconds in (stream_seconds, launch_seconds)
            if seconds is not None
        ]
        quiet = max(0, int(min(candidates)))
    refreshed["quiet_seconds"] = quiet
    base_state = snapshot.get("stall_base_state")
    if base_state is not None:
        state, detail = _stall_reading(
            str(base_state),
            str(snapshot.get("stall_base_detail") or ""),
            quiet=quiet,
            window=window,
            pause_reason=snapshot.get("stall_pause_reason"),
            process_state=snapshot.get("stall_process_state"),
        )
        refreshed["state"] = state
        refreshed["detail"] = detail
    return refreshed


def _compute_watch_snapshot(
    pointer: Mapping[str, Any], *, moment: float, stall_seconds: int
) -> dict[str, Any]:
    """Reduce one pointer to the state and reason a ticker compares."""
    row = classify_pointer(
        pointer,
        now_seconds=moment,
        stale_after_seconds=stall_seconds,
    )
    verdict = row["fleet_verdict"]

    # The absolute instants the silence is measured between, resolved once
    # here so a reused snapshot can recompute it from the stream's stat at a
    # later moment without re-listing the run directory.
    found = _record_newest_stream(pointer)
    if found is not None:
        stream_seconds: float | None = found[1]
        stall_stream_path: str | None = str(found[0])
    else:
        stream_seconds = _quiet_clock_latest(pointer, moment=moment)
        stall_stream_path = None
    attempt_started = _attempt_started_seconds(pointer)
    launch_seconds = (
        attempt_started
        if attempt_started is not None
        else _quiet_clock_latest(pointer, moment=moment)
    )

    # What ran it, as facts rather than a display string. The alias and effort
    # spelling were decided at dispatch and frozen onto the pointer; a later
    # configuration edit must not restate what ran, so the facts are read from
    # the record. Composition is the renderer's, so the model and effort travel
    # separately and the monitor decides how they read.
    agent_map = (
        pointer.get("agent") if isinstance(pointer.get("agent"), Mapping) else {}
    )
    return {
        "run_id": str(row.get("run_id") or ""),
        "node": str(row.get("node") or row.get("run_id") or "unknown"),
        # The project the run belongs to, carried on the snapshot so the
        # departure fold can resolve the ledger that decides its word when its
        # caller supplies no reader. A snapshot written before this field existed
        # carries none, which leaves a departure's record unknown and therefore
        # departed rather than promised as promoted.
        "project": str(pointer.get("project") or ""),
        # The dispatching session, so a reader can tell its own fleet from a
        # peer's on a stream that is necessarily project-wide.
        "session": str(pointer.get("session") or ""),
        "lane": row.get("lane"),
        "model_key": row.get("model_key"),
        "backend": str(agent_map.get("backend") or "").strip(),
        "model": str(agent_map.get("model") or "").strip(),
        "effort": str(agent_map.get("effort") or "").strip(),
        "alias": str(agent_map.get("alias") or "").strip(),
        # What kind of work it is, on the record the same way. Read beside the
        # agent because the two describe the same run and are reduced the same
        # way — the snapshot carries the raw spelling and the renderer narrows
        # it to fit its column.
        "role": _pointer_role(pointer),
        # Whether this run is a shadow of a committed primary. Dispatch decides
        # shadowship at launch and writes the lineage onto the pointer; the
        # renderer dims a shadow row end to end from that fact, so the snapshot
        # carries it under its own name rather than as a flattened display flag.
        "lineage": pointer.get("lineage"),
        "state": verdict["state"],
        "classification": row["classification"],
        "process_alive": row["process_alive"],
        "liveness_proven": row["liveness_proven"],
        # The descendant reading the stall window is widened by, carried so a
        # producer that reuses a snapshot can compare the reading it was built
        # from with a fresh one and drop the entry when the child ends.
        "process_descendant_alive": row.get("process_descendant_alive"),
        # The declared allowance the stall window widens by when a child is
        # live under the worker, carried so a producer that reuses this
        # snapshot recomputes the same window the full classification did
        # rather than reading the one frozen onto the entry.
        "budget_seconds": row.get("budget_seconds"),
        "recovery_classification": verdict["recovery_classification"],
        "recovery": verdict["recovery"],
        "lifting_condition": verdict.get("lifting_condition"),
        "resets_at": row.get("resets_at"),
        "next_action": row.get("next_action"),
        # The full, untruncated reason. The bounded clause a reader can act on
        # is derived from it at render time, so nothing here is shaped for the
        # grid before it is stored.
        "detail": verdict["detail"],
        # The fact a block's glyph is derived from. Only a "blocked" state
        # carries a marker; a run entering any other state has nothing for the
        # reader to answer or read a manifest for.
        "needs_help_complete": row.get("needs_help_complete"),
        "wait_overdue": row.get("wait_overdue"),
        # The rest of a declared wait travels beside its probe's verdict:
        # whether the condition probe has run at all and what it last observed,
        # the horizon the wait was declared against, and the brief a resumed
        # worker reads. A run that declares no wait has taken no measurement, so
        # None is the explicit unmeasured state and a zero never stands in for
        # one.
        "wait_condition_state": row.get("wait_condition_state"),
        "wait_observed": row.get("wait_observed"),
        "expected_horizon_seconds": (row.get("external_wait") or {}).get(
            "expected_horizon_seconds"
        ),
        "resume_brief": (row.get("external_wait") or {}).get("resume_brief"),
        # The manifest facts the fold needs to detect a rewrite: the effective
        # status (empty while a live process defers a terminal report, so a
        # deferred report never reads like a verdict), the commit list, and the
        # content digest that distinguishes a changed rewrite from a touch.
        "manifest_status": str(row.get("manifest_status") or ""),
        "manifest_commits": list(row.get("manifest_commits") or []),
        "manifest_digest": row.get("manifest_digest"),
        # The clock-derived inputs, carried so a producer that reuses this
        # snapshot recomputes the silence from the stream's stat alone rather
        # than re-reading or re-listing anything.
        "quiet_seconds": verdict.get("stall_quiet_seconds"),
        "stall_base_state": verdict.get("stall_base_state"),
        "stall_base_detail": verdict.get("stall_base_detail"),
        "stall_window_seconds": verdict.get("stall_window_seconds"),
        "stall_pause_reason": verdict.get("stall_pause_reason"),
        "stall_process_state": verdict.get("stall_process_state"),
        "stall_stream_seconds": stream_seconds,
        "stall_launch_seconds": launch_seconds,
        "stall_stream_path": stall_stream_path,
    }


def _watch_snapshot(
    pointer: Mapping[str, Any],
    *,
    moment: float,
    stall_seconds: int,
    cache: dict[str, tuple[str, dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Reduce one pointer to the state and reason a ticker compares.

    A run whose classification inputs are all unchanged since the last poll
    serves its previous snapshot: only the silence is recomputed, from the
    stream's stat and the poll's own moment. Any change to an input, or a
    liveness change the snapshot reports, drops the entry and a full
    classification is taken again. ``cache`` is the producer's own store, so a
    cache never carries an answer across two independently armed watchers; the
    module-level cache serves callers that pass none.
    """
    store = _SNAPSHOT_CACHE if cache is None else cache
    run_id = str(pointer.get("run_id") or "")
    served = store.get(run_id)
    key = _snapshot_reuse_key(pointer)
    if run_id and served is not None and key is not None and served[0] == key:
        stored = served[1]
        # A snapshot is reused only while the process reading it was built from
        # still holds. Liveness is not a file: a worker can die, or a child it
        # was waiting on can end, with every file untouched, and the row must
        # change on the poll that observes it. The probe is taken every poll,
        # through the same host-gated reader classify_pointer uses.
        if _fresh_liveness(pointer) == (
            stored.get("process_alive"),
            stored.get("liveness_proven"),
            stored.get("process_descendant_alive"),
        ):
            return _refresh_snapshot(
                stored, moment=moment, stall_seconds=stall_seconds
            )
    snapshot = _compute_watch_snapshot(
        pointer, moment=moment, stall_seconds=stall_seconds
    )
    refreshed = _refresh_snapshot(
        snapshot, moment=moment, stall_seconds=stall_seconds
    )
    if run_id and key is not None:
        _remember_snapshot(run_id, key, refreshed, cache=store)
    return refreshed


# The ordinary three buckets remain unchanged when no external wait exists. A
# waiting bucket appears while at least one declared condition is outstanding,
# keeping healthy waits out of both work-in-progress and needs-action figures.
# Every snapshot belongs to exactly one bucket, so the figures still add up.
FLEET_WORKING_STATES = ("dispatched", "working", "running")
# Both words say that the ledger settled the run. One records landed commits;
# the other records a completed run with no repository commit to claim.
FLEET_SETTLED_STATES = frozenset({"promoted", "recorded"})
# ``departed`` is the word a departure with no resolvable ledger carries: the
# run has gone and no record says whether it landed. It sits with the delivered
# family here so the state vocabulary names every word the fold can emit, while
# a departing run is still dropped from the counted fleet before the counts are
# taken — the word is known, not counted.
FLEET_UNPROMOTED_STATES = ("complete", "completed_unpromoted", "departed")
FLEET_WAITING_STATES = tuple(sorted(WAITING_STATES))
# The blocked bucket is the action set minus the waiting family. The action set
# is the marker set — every state whose row a reader should look at, an overdue
# wait included — but the counter says what kind of run this is, and an overdue
# wait is still waiting. Count and marker therefore separate on that one
# member: what the number reports as blocked is what a reader must act on
# excluding a run that is legitimately still in the waiting column.
FLEET_BLOCKED_STATES = tuple(sorted(NEEDS_ACTION - WAITING_STATES))


def _fleet_counts(
    snapshots: Mapping[str, Mapping[str, Any]], *, session: str | None = None
) -> dict[str, int]:
    """Partition the fleet into working, blocked, delivered, and waiting work.

    ``working`` is what a reader means by a live worker. ``blocked`` is
    everything that has stopped progressing and needs the coordinator, a stall
    or a failure included. ``unpromoted`` is delivered work waiting on a gate.
    ``waiting`` is a run whose declared external condition remains outstanding.
    A run that leaves the fleet is in none of them.

    ``session`` narrows the counted population to the runs that session owns,
    using the ownership already persisted on each snapshot; omitted, the
    partition covers the whole fleet, preserving the project-wide reading. A
    legacy snapshot with no recorded owner stays in the counted set: absence
    cannot prove that a live pointer belongs to a peer.
    """
    rows, _peers = _partition_session_rows(snapshots.values(), session)
    states = [str(snapshot.get("state") or "") for snapshot in rows]
    held = sum(
        str(snapshot.get("recovery_classification") or "") == "held"
        for snapshot in rows
    )
    counts = {
        "working": sum(state in FLEET_WORKING_STATES for state in states),
        "blocked": sum(
            str(snapshot.get("state") or "") in FLEET_BLOCKED_STATES
            and str(snapshot.get("recovery_classification") or "") != "held"
            for snapshot in rows
        ),
        "unpromoted": sum(state in FLEET_UNPROMOTED_STATES for state in states),
    }
    waiting = (
        sum(
            str(snapshot.get("state") or "") in FLEET_WAITING_STATES
            and str(snapshot.get("recovery_classification") or "") != "held"
            for snapshot in rows
        )
        + held
    )
    if waiting:
        counts["waiting"] = waiting
    return counts


def _manifest_rewritten(
    previous: Mapping[str, Any], current: Mapping[str, Any]
) -> bool:
    """Whether the report beneath an unchanged verdict was replaced.

    A terminal manifest is the worker's own report and the fold emits one
    state change per verdict, so a worker that replaces that report — the
    measured case: an empty-commit failed placeholder overwritten eighteen
    minutes later by the real failed manifest — must still surface, or the
    coordinator holds the first reading forever. The signal is the content
    digest, not the mtime: a digest fires only when the rewrite changed
    something, while mtime alone fires on a touch of identical content, and a
    transition without news is the noise this display exists to avoid. The
    accepted cost is the opposite hole, a legitimate rewrite to byte-identical
    content goes unseen — it carries no news to deliver, so there is nothing
    a reader should be woken for.

    Both readings must be an effective terminal verdict: a live process's
    terminal report is deferred, so its rewrites stay silent until the process
    dies, and an in-progress manifest is progress, not a verdict.
    """
    if str(previous.get("manifest_status") or "") not in TERMINAL_MANIFEST_STATUSES:
        return False
    if str(current.get("manifest_status") or "") not in TERMINAL_MANIFEST_STATUSES:
        return False
    previous_digest = previous.get("manifest_digest")
    current_digest = current.get("manifest_digest")
    return bool(
        previous_digest and current_digest and previous_digest != current_digest
    )


class _LedgerRunWordReader:
    """Resolve settled words for departures without decoding the whole ledger."""

    def __init__(self, project: str) -> None:
        self.project = project

    def for_runs(self, departures: Iterable[str] | None = None) -> Mapping[str, str]:
        from reckon import ledger as ledger_module

        try:
            recorded = ledger_module.run_ids(self.project)
        except (OSError, ValueError, ledger_module.LedgerError):
            return {}
        targets = recorded if departures is None else recorded.intersection(departures)
        words: dict[str, str] = {}
        aggregate_only: set[str] = set()
        for run_id in targets:
            try:
                path = ledger_module.run_path(self.project, run_id)
                row = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                aggregate_only.add(run_id)
                continue
            except (OSError, ValueError, ledger_module.LedgerError):
                words[run_id] = "recorded"
                continue
            words[run_id] = (
                "promoted"
                if isinstance(row, Mapping)
                and row.get("run_id") == run_id
                and row.get("commits")
                else "recorded"
            )
        if aggregate_only:
            try:
                rows, _version = ledger_module.read_records(
                    self.project, with_figures=False
                )
                words.update(
                    {
                        str(row["run_id"]): (
                            "promoted" if row.get("commits") else "recorded"
                        )
                        for row in rows
                        if row.get("run_id") in aggregate_only
                    }
                )
            except (OSError, ValueError, ledger_module.LedgerError):
                pass
            words.update(dict.fromkeys(aggregate_only - words.keys(), "recorded"))
        return words

    def __call__(self) -> Mapping[str, str]:
        return self.for_runs()


def _ledger_run_id_reader(project: str) -> _LedgerRunWordReader:
    """Read the ledger only when a departure needs its committed word."""
    return _LedgerRunWordReader(project)


def _departure_recorded_run_ids(
    known: Mapping[str, Mapping[str, Any]],
    departures: Sequence[str],
    ledger_run_ids: Callable[[], Iterable[str] | Mapping[str, str]] | None,
) -> dict[str, str] | None:
    """The recorded departure words a fold resolves against.

    An id-only reader means its ids are promotions, preserving the existing
    direct-call contract. When no reader is supplied, one is resolved from the
    departing run's own project, because a caller holding no
    reader — the published fleet stream builds its transitions without one — has
    no way to tell a promotion from a pointer that vanished, and a word chosen
    without that fact promises a landing nobody recorded. Resolving it here
    rather than at the call site keeps the word's authority: whatever supplied
    the reader, promotion still requires a recorded row.

    A departure whose snapshot names no project leaves the record unknown rather
    than empty, and unknown is answered by the ``departed`` word, never by a
    promotion: the alternative asserts a fact no reader established.
    """
    reader = ledger_run_ids
    if reader is None:
        for run_id in departures:
            project = str(known[run_id].get("project") or "")
            if project:
                reader = _ledger_run_id_reader(project)
                break
    if reader is None:
        return None
    recorded = (
        reader.for_runs(departures)
        if isinstance(reader, _LedgerRunWordReader)
        else reader()
    )
    if isinstance(recorded, Mapping):
        return {str(run_id): str(word) for run_id, word in recorded.items()}
    return {str(run_id): "promoted" for run_id in recorded}


def _departure_word(run_id: str, recorded: Mapping[str, str] | None) -> str:
    """The word a departing run's absence carries.

    A recorded ledger row has first claim, because it settles the run whether
    it carries landed commits or a declared commitless completion. Failing
    that, a marker the run's directory holds from a deliberate discard names the
    departure discarded whatever else is known: the discard is a fact the run's
    own home records, so it outranks a ledger that cannot be resolved. Only when
    no such marker exists does an unresolvable ledger decide the word — the
    caller supplies no reader and the run names no project to resolve one from —
    and then the departure reads ``departed``, which promises neither a landing
    nor a withdrawal. A ledger that resolves and records no row leaves the run
    the bare withdrawal a reaped or hand-removed pointer earns.
    """
    if recorded is not None and run_id in recorded:
        return recorded[run_id]
    if _discard_recorded(run_id):
        return "discarded"
    if recorded is None:
        return "departed"
    return "withdrawn"


def _discard_recorded(run_id: str) -> bool:
    """Whether the run directory holds a marker a deliberate discard left.

    The run's own home is read only for a departure and only for a run the
    ledger does not record, so an ordinary observation touches no run
    directory. An unreadable or absent marker answers False: a departure the
    fleet cannot corroborate takes the word that promises nothing.
    """
    from reckon.crew.promotion import discard_record_path

    try:
        return discard_record_path(run_id).is_file()
    except OSError:
        return False


def fleet_transitions(
    known: Mapping[str, Mapping[str, Any]],
    current: Mapping[str, Mapping[str, Any]],
    *,
    ledger_run_ids: Callable[[], Iterable[str] | Mapping[str, str]] | None = None,
) -> tuple[
    list[tuple[dict[str, Any], str | None, str, dict[str, int]]],
    dict[str, dict[str, Any]],
]:
    """Fold one fleet observation into ordered transitions and the next state.

    The counts travel per transition, recomputed after each one is applied,
    because a line's numbers are read as the fleet *at that line*. Stamping one
    batch-wide count on every line of a multi-transition poll describes the end
    of the batch instead: a promotion would report the fleet it had already
    left, and three simultaneous landings would all claim the third one's
    totals.

    Departures first, then arrivals, then state changes — a run removed only by
    its own departure leaves the fleet before the next dispatch is counted into
    its slot, which is the order a reader infers from the numbers. A manifest
    rewrite that leaves the state unchanged is folded after the state changes of
    the same observation: its classification word did not move, so nothing else
    about the fold could have either. A run worded from its terminal ledger row
    settles there: the completion is announced once, a later pointer
    reading cannot move it back to ``dispatched``, and the pointer's own
    disappearance — a gc reap included — emits nothing further for the run.
    """
    if ledger_run_ids is None:
        # The published-stream fold supplies no ledger reader: it is the tick
        # the producer runs to append its transitions to the stream, and the
        # guard in ``_publish_watch_stream`` defers the whole tick when the
        # resolved configuration will not load, so a following reader gets the
        # previous image rather than a transition priced against a layer nobody
        # could read. This read is what lets that guard fire. The reader is
        # strict here only; every other caller of the fold either supplies a
        # ledger reader (the seat's own ticker, which pre-reads the rates and
        # keeps its degradation) or is a direct test of the fold.
        quota_weight.backend_rate_statuses(strict=True)

    running = {run_id: dict(snapshot) for run_id, snapshot in known.items()}
    changes: list[tuple[Mapping[str, Any], str | None, str]] = []

    departures = [item for item in known if item not in current]
    # A run leaves the fleet for reasons a pointer cannot tell apart on its own:
    # a completion that wrote its ledger row, a deliberate discard that left its
    # marker in the run directory, and a pointer that vanished with nothing
    # recorded behind it — a reaped pointer, a file removed by hand. A reader
    # acts on the word, and each of the three asks for a different response, so
    # the fold resolves all three. A settled word is read from the ledger alone:
    # promoted for recorded commits, recorded for a commitless completion. A discard marker
    # in the run directory names the departure discarded. With neither, a ledger
    # that resolves and records no row leaves the word withdrawn, while a ledger
    # that cannot be resolved leaves it departed — the honest unknown, which
    # promises neither a landing nor a withdrawal. A promotion is never inferred
    # from a missing row's absence, so an unrecorded departure cannot read as
    # work that landed. The ledger is read at most once per observation and only
    # when something departed; a reader the caller cannot supply is resolved
    # from the departing run's own project, and a departure whose snapshot names
    # no project at all still reads departed, because the alternative asserts a
    # fact no reader established.
    if departures:
        recorded = _departure_recorded_run_ids(known, departures, ledger_run_ids)
    else:
        recorded = {}
    for run_id in departures:
        if str(known[run_id].get("state") or "") in FLEET_SETTLED_STATES:
            # The run already settled on its terminal ledger row: the landing
            # was announced once, so the pointer's later disappearance — a gc
            # reap included — is not news and emits nothing for the run. The
            # slot is still given up, so a genuine re-dispatch of the same id
            # is read as an arrival rather than suppressed by a stale memory.
            running.pop(run_id, None)
            continue
        # A departure is its own fact and inherits no clause or marker from the
        # state it left. Carrying one forward reports a block on the line
        # announcing that the block is over.
        departed = {**known[run_id], "detail": "", "needs_help_complete": None}
        changes.append((departed, str(known[run_id]["state"]), _departure_word(
            run_id, recorded
        )))
    for run_id in (item for item in current if item not in known):
        changes.append(
            (
                {
                    **current[run_id],
                    "state": "dispatched",
                    "detail": "",
                    "needs_help_complete": None,
                },
                None,
                "dispatched",
            )
        )
    for run_id in (item for item in current if item in known):
        previous = str(known[run_id]["state"])
        state = str(current[run_id]["state"])
        if previous in FLEET_SETTLED_STATES:
            # A terminal ledger row settles the run: once worded promoted, the
            # run stays promoted whatever the live pointer later reads. The
            # landing was announced once, when the row was written, so a stale
            # or superseded pointer that reads ``dispatched`` is not news and
            # cannot drive the row back — holding the promoted word is what
            # stops the row flapping between the two.
            continue
        previous_recovery = str(
            known[run_id].get("recovery_classification") or previous
        )
        current_recovery = str(current[run_id].get("recovery_classification") or state)
        if state != previous or current_recovery != previous_recovery:
            changes.append((current[run_id], previous, state))
        elif _manifest_rewritten(known[run_id], current[run_id]):
            # The classification word did not move but the report it sits on
            # did. The emitted snapshot is marked so the event builder records
            # the rewrite as its own kind; the run's memory keeps the clean
            # copy so the marker never leaks into a later departure.
            rewritten = dict(current[run_id])
            rewritten["manifest_rewritten"] = True
            changes.append((rewritten, previous, state))
            running[run_id] = dict(current[run_id])

    events: list[tuple[dict[str, Any], str | None, str, dict[str, int]]] = []
    for snapshot, previous, state in changes:
        run_id = str(snapshot.get("run_id") or "")
        # A run present in the fleet is remembered, so the next observation
        # compares it against itself rather than reading it as an arrival. A
        # run absent from the fleet has departed and gives up its slot. A run
        # whose terminal ledger row has worded it promoted keeps that word in
        # the memory too: the landing was announced once, and a later pointer
        # reading must not replace it, so the row cannot flap back to
        # ``dispatched`` while the pointer lingers.
        if run_id not in current:
            running.pop(run_id, None)
        elif (
            run_id in running
            and str(running[run_id].get("state") or "") in FLEET_SETTLED_STATES
        ):
            # Settled on its terminal ledger row: hold the promoted memory
            # rather than adopting a later pointer reading.
            pass
        elif not snapshot.get("manifest_rewritten"):
            running[run_id] = dict(snapshot)
        events.append((dict(snapshot), previous, state, _fleet_counts(running)))
    return events, running


def _watch_transition(
    project: str,
    *,
    kind: str,
    snapshot: Mapping[str, Any],
    previous: str | None,
    current: str,
    counts: Mapping[str, int],
    spend_runs: Sequence[Mapping[str, Any]] | None = None,
    rate_statuses: Mapping[str, Any] | None = None,
    streams_root: str | Path | None = None,
) -> dict[str, Any]:
    """Build one lossless transition object for text or JSON rendering.

    This is the surface the events log persists, so it carries facts only: the
    model, effort, alias and backend separately, the full untruncated detail,
    the structured fact a block's glyph is derived from, and the run's
    cumulative spend — wall seconds, model seconds, charged tokens, generation
    rate and notional cost as separate numeric facts. No composed label, no
    pre-claused reason and no display glyph are written here — the monitor
    derives those from these facts, so the log stays re-renderable.

    ``spend_runs`` is the record set the accumulator folds (the live fleet, or
    rows already settled); omitted, the project's own live pointers are read.
    ``rate_statuses`` maps a backend to its dated rate standing for the notional
    cost figure; omitted, the resolved configuration is read.
    """
    event = {
        "project": project,
        "event": kind,
        "observed_at": _utc_now(),
        "run_id": snapshot.get("run_id"),
        "node": snapshot.get("node"),
        "session": snapshot.get("session") or "",
        "role": snapshot.get("role") or "",
        # The shadow lineage the snapshot carried from the pointer, threaded
        # through the field-by-field rebuild so the events log records the same
        # fact the renderer reads to dim the row.
        "lineage": snapshot.get("lineage"),
        "backend": str(snapshot.get("backend") or ""),
        "model": str(snapshot.get("model") or ""),
        "effort": str(snapshot.get("effort") or ""),
        "alias": str(snapshot.get("alias") or ""),
        "from_state": previous,
        "to_state": current,
        "classification": snapshot.get("classification"),
        "process_alive": snapshot.get("process_alive"),
        "liveness_proven": snapshot.get("liveness_proven"),
        "working": counts["working"],
        "blocked": counts["blocked"],
        "unpromoted": counts["unpromoted"],
        "detail": str(snapshot.get("detail") or ""),
        "recovery_classification": str(
            snapshot.get("recovery_classification") or current
        ),
        "recovery": str(snapshot.get("recovery") or ""),
        "lifting_condition": snapshot.get("lifting_condition"),
        "resets_at": snapshot.get("resets_at"),
        "next_action": snapshot.get("next_action"),
        # The declared wait's own facts, beside the condition that lifts it: the
        # probe's verdict and last observation, the horizon the wait was
        # declared against, and the brief a resumed worker reads. Carried whole
        # so a renderer can tell a probe that never ran from one still pending,
        # and can hold the wait's age against its horizon without re-deriving
        # either. None is the explicit unmeasured state — a run that declares no
        # wait has taken no measurement, so no zero stands in for one.
        "wait_condition_state": snapshot.get("wait_condition_state"),
        "wait_observed": snapshot.get("wait_observed"),
        "wait_overdue": snapshot.get("wait_overdue"),
        "expected_horizon_seconds": snapshot.get("expected_horizon_seconds"),
        "resume_brief": snapshot.get("resume_brief"),
        "needs_help_complete": snapshot.get("needs_help_complete"),
    }
    if snapshot.get("manifest_rewritten"):
        # A rewrite of the report beneath an unchanged verdict: from_state and
        # to_state are the same word, so the distinct kind is what separates
        # this event from one where the classification moved. The new report's
        # facts ride along, so a reader sees what changed rather than only that
        # something changed.
        event["event"] = "manifest-rewritten"
        event["manifest_status"] = str(snapshot.get("manifest_status") or "")
        event["manifest_commits"] = list(snapshot.get("manifest_commits") or [])
        event["commit_count"] = len(event["manifest_commits"])
    if "waiting" in counts or previous in WAITING_STATES or current in WAITING_STATES:
        event["waiting"] = counts.get("waiting", 0)
    recorded = (
        _recorded_transition_spend(project, str(snapshot.get("run_id") or ""))
        if kind == "transition" and current in FLEET_SETTLED_STATES
        else None
    )
    event.update(
        recorded
        if recorded is not None
        else _spend_facts(
            project,
            snapshot,
            spend_runs=spend_runs,
            rate_statuses=rate_statuses,
            streams_root=streams_root,
        )
    )
    return event


def _recorded_transition_spend(project: str, run_id: str) -> dict[str, Any] | None:
    """Read a settled run's spend from the row committed before pointer unlink.

    The live-pointer fold is empty by the time this transition is published.
    The ledger's throughput block is the measurement promotion already
    resolved, so the transition copies its numeric facts without another fold.
    A missing block stays unknown; a measured zero remains numeric zero.
    """
    if not run_id:
        return None
    from reckon import ledger as ledger_module

    try:
        path = ledger_module.run_path(project, run_id)
        if path.is_file():
            row = json.loads(path.read_text(encoding="utf-8"))
        else:
            rows, _version = ledger_module.read_records(project, with_figures=False)
            row = next((item for item in rows if item.get("run_id") == run_id), None)
    except (OSError, ValueError, ledger_module.LedgerError):
        return None
    if not isinstance(row, Mapping) or row.get("run_id") != run_id:
        return None
    throughput = row.get("throughput")
    throughput = throughput if isinstance(throughput, Mapping) else {}
    budget = row.get("budget")
    budget = budget if isinstance(budget, Mapping) else {}

    def measured(value: Any) -> int | float | None:
        return (
            value
            if isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            else None
        )

    input_tokens = measured(throughput.get("cumulative_input_tokens"))
    output_tokens = measured(throughput.get("generated_tokens"))
    return {
        "spend_folded_run_count": 1,
        "spend_measured_stream_count": None,
        "spend_unmeasured_stream_count": None,
        "spend_wall_seconds": measured(throughput.get("elapsed_seconds")),
        "spend_model_seconds": measured(throughput.get("generation_seconds")),
        "spend_machine_seconds": measured(throughput.get("machine_seconds")),
        "spend_charged_tokens": (
            input_tokens + output_tokens
            if input_tokens is not None and output_tokens is not None
            else None
        ),
        "spend_generation_rate": measured(throughput.get("tokens_per_second")),
        "spend_notional_cost_usd": measured(budget.get("cost_usd_cumulative")),
    }


def _spend_facts(
    project: str,
    snapshot: Mapping[str, Any],
    *,
    spend_runs: Sequence[Mapping[str, Any]] | None = None,
    rate_statuses: Mapping[str, Any] | None = None,
    streams_root: str | Path | None = None,
) -> dict[str, Any]:
    """The transition's cumulative spend as separate, re-renderable facts.

    Every figure is written numerically — never as a pre-formatted string — and
    ``None`` is the explicit unmeasured state for each derived figure, because a
    zero would assert a measurement that was never taken. Tokens are the total a
    meter charges (input including cache reads, plus output); the rate is
    generated output over model seconds.
    """
    run_id = str(snapshot.get("run_id") or "")
    facts: dict[str, Any] = {
        "spend_wall_seconds": None,
        "spend_model_seconds": None,
        "spend_machine_seconds": None,
        "spend_charged_tokens": None,
        "spend_generation_rate": None,
        "spend_notional_cost_usd": None,
        "spend_folded_run_count": 0,
        "spend_measured_stream_count": 0,
        "spend_unmeasured_stream_count": 0,
    }
    if not run_id:
        return facts
    rows = spend_runs
    if rows is None:
        rows = runs._list_live_records(project=project)
    spend = metering.accumulate_run_spend(rows, run_id, streams_root=streams_root)
    if not isinstance(spend, metering.AccumulatedRunSpend):
        return facts
    measured = spend.measured_stream_count > 0
    facts.update(
        {
            "spend_folded_run_count": spend.folded_run_count,
            "spend_measured_stream_count": spend.measured_stream_count,
            "spend_unmeasured_stream_count": spend.unmeasured_stream_count,
            "spend_wall_seconds": spend.elapsed_seconds,
            "spend_model_seconds": spend.generation_seconds,
            "spend_machine_seconds": spend.machine_seconds,
            "spend_charged_tokens": spend.total_charged_tokens if measured else None,
            "spend_generation_rate": _generation_rate(spend, measured),
            "spend_notional_cost_usd": _notional_cost(snapshot, spend, rate_statuses),
        }
    )
    return facts


def _generation_rate(
    spend: metering.AccumulatedRunSpend, measured: bool
) -> float | None:
    """Generated tokens over model seconds, or None when either is unknown.

    A measured zero model span is still a span a rate could be divided from, so
    only a strictly positive span rates a denominator; an unmeasured chain never
    fabricates a rate from a zero it did not observe.
    """
    if (
        not measured
        or spend.generation_seconds is None
        or spend.generation_seconds <= 0
    ):
        return None
    return spend.cumulative_output_tokens / spend.generation_seconds


def _notional_cost(
    snapshot: Mapping[str, Any],
    spend: metering.AccumulatedRunSpend,
    rate_statuses: Mapping[str, Any] | None,
) -> float | None:
    """The run's notional dollar figure from declared rates, or None unpriced.

    The figure is computed from public rates and measured tokens, never read
    from the harness — which prices whatever model name it was told to speak and
    therefore ranks the free local lane as the most expensive backend. A lane
    with no dated rate pair stays explicitly unpriced, and a chain with no
    measured stream stays unmeasured.
    """
    if spend.measured_stream_count == 0:
        return None
    if rate_statuses is None:
        rate_statuses = quota_weight.backend_rate_statuses()
    status = rate_statuses.get(str(snapshot.get("backend") or ""))
    priced = bool(getattr(status, "priced", False))
    rate = getattr(status, "rate", None) if priced else None
    if rate is None:
        return None
    return round(
        spend.cumulative_input_tokens / 1_000_000 * rate.input_per_million
        + spend.cumulative_output_tokens / 1_000_000 * rate.output_per_million,
        2,
    )


def _scoped_watch_event(event: Mapping[str, Any], session: str) -> dict[str, Any]:
    """Re-derive a transition's figures over one session's live pointers.

    The watcher seat is project-global and the stream it writes carries the
    whole fleet's totals, so a follower cannot ask for per-session figures on
    the wire; the population is re-selected here at render time from the same
    live pointers, using the ownership already persisted on each pointer. A
    legacy pointer with no recorded owner stays counted, matching the dispatch
    fence.
    """
    project = str(event.get("project") or "")
    stall_seconds = parse_duration(DEFAULT_WATCH_STALL_WINDOW)
    moment = _utc_seconds()
    pointers = runs._list_live_records(project=project) if project else ()
    current = {
        str(pointer.get("run_id") or ""): _watch_snapshot(
            pointer, moment=moment, stall_seconds=stall_seconds
        )
        for pointer in pointers
        if pointer.get("run_id")
    }
    counts = _fleet_counts(current, session=session)
    scoped = dict(event)
    scoped["working"] = counts["working"]
    scoped["blocked"] = counts["blocked"]
    scoped["unpromoted"] = counts["unpromoted"]
    previous = scoped.get("from_state")
    state = scoped.get("to_state")
    if "waiting" in counts or previous in WAITING_STATES or state in WAITING_STATES:
        scoped["waiting"] = counts.get("waiting", 0)
    elif "waiting" in scoped:
        del scoped["waiting"]
    return scoped


def format_watch_transition(
    event: Mapping[str, Any],
    *,
    with_session: bool = False,
    ticker: Ticker | None = None,
    session: str | None = None,
) -> str:
    """Render one transition as the compact human-facing watch line.

    ``ticker`` supplies a caller's own grid — the CLI passes one carrying the
    reader's width, theme and colour choice. Omitted, a fresh plain grid renders
    for this call alone, because there is no terminal to detect: the pane is a
    pipe, so colour is a decision a caller makes rather than one this module can
    infer. A grid holds per-run state, so a fresh one keeps a row's text a
    function of the row, not of which rows a shared instance rendered before it;
    a caller that wants the age of a bucket's oldest member across a stream
    passes its own grid for the whole stream.

    ``session`` re-scopes the line's figures to the runs that session owns. A
    session-scoped follower relays a project-wide stream whose every event
    carries the fleet's totals; re-selecting the population before rendering is
    what makes its trailing figures describe the runs it is following. Omitted,
    the line shows the figures the event arrived with, unchanged.
    """
    if event.get("legacy"):
        return str(event.get("rendered") or "")
    if session is not None:
        event = _scoped_watch_event(event, session)
    return (ticker or Ticker()).render(event, with_session=with_session)


def _refuse_unresolvable_watch(project: str) -> None:
    """Refuse to arm a watcher whose project routes to a missing backend.

    A watcher that cannot resolve a backend it may be asked to lift reads as
    armed and loses every park it lifts, leaving a 0-byte stream per tick while
    the pointer stays working. The check runs before the registration is taken,
    so the seat is never held by a watcher that cannot do its job.
    """
    from reckon.crew.dispatch import assert_routable_backends_resolvable

    assert_routable_backends_resolvable(project, _resolved_review_config(project, None))


def _recreate_unlinked_registration(project: str, watcher: Mapping[str, Any]) -> bool:
    """Restore the seat record when an unlink took its path out from under us.

    The seat record is the file ``crew unwatch`` opens to find the producer it
    must stop. Its record holds the advisory lock the process table reads
    liveness from and carries the pid unwatch signals. A record unlinked under a
    live producer leaves nothing at the path,
    so a later ``unwatch`` opens a fresh inode, takes the lock the producer
    believes it still holds, and reports there is nothing to stop while the
    producer runs on unwatched — it can only be reached by pid. Writing the
    record back to its own path on the producer's next wake-up restores what the
    path is for, so unwatch finds the producer again.

    A path that a replacement producer has meanwhile taken is left alone and
    this returns False, so a superseded producer ends rather than overwrite a
    seat that is no longer its own.
    """
    path = watch_lock_path(project)
    handle = runs._WATCH_SEAT_HANDLES.get(project)
    if handle is None:
        return True
    try:
        held = os.fstat(handle.fileno())
    except OSError:
        return False
    try:
        current = os.stat(path)
    except FileNotFoundError:
        current = None
    if current is not None:
        return (current.st_dev, current.st_ino) == (held.st_dev, held.st_ino)

    try:
        replacement = path.open("a+b")
    except OSError:
        return False
    try:
        fcntl.flock(replacement.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # Another producer holds this path: the seat is no longer ours.
        replacement.close()
        return False
    _write_watch_record(replacement, dict(watcher))
    runs._WATCH_SEAT_HANDLES[project] = replacement
    return True


# An idle producer — one whose project has no live run pointer — doubles its
# poll interval on each wake-up, from the base it was armed with to this
# ceiling, so a project nobody is watching costs one wake a minute rather than
# one a second. A wake that sees a live run returns the interval to the base in
# the same pass. The value is written into the registration as
# ``poll_interval_seconds``, which is what a reader sees.
IDLE_POLL_INTERVAL_CAP_SECONDS = 30.0


def watch_ticker(
    project: str,
    *,
    stall_window: str = DEFAULT_WATCH_STALL_WINDOW,
    poll_interval: float = 1.0,
    sleeper: Callable[[float], None] = time.sleep,
    signal_run: Callable[..., None] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield a baseline and then every observed fleet state transition.

    Each tick also ends any review that has already delivered and kept
    streaming past its grace, so a reviewer whose record is stored and whose
    manifest is a verdict does not go on holding a lane the fleet needs. The
    stop is the tick's own concern rather than a yielded event: it changes run
    state, and a caller reading transitions is watching for a different thing.
    """
    _refuse_unresolvable_watch(project)
    stall_seconds = parse_duration(stall_window)
    known: dict[str, dict[str, Any]] = {}
    fleet_seen = False
    # Rate standings move on their own cadence, so one resolution serves the
    # whole watch session rather than a config read per transition.
    rate_statuses = quota_weight.backend_rate_statuses()

    # The producer holds a lease, not a lifetime: every live follower renews it
    # on its wait pass. A lease whose instant is absent is not a lapsed one — the
    # registration may simply have gone missing, which a later wake-up recreates
    # — so only a recorded instant that has fallen a full interval behind ends
    # the seat. The sleep is bounded by the lease's remaining time so an exit
    # lands within a second of the lapse however far the interval has backed off.
    lease_seconds = producer_lease_seconds()

    def _lease_remaining() -> float | None:
        renewed = watch_lease_renewed_at(project)
        if renewed is None:
            return None
        return lease_seconds - (_utc_seconds() - renewed)

    def _wait(interval: float) -> bool:
        """Sleep, bounded by the lease; report whether the seat has lapsed."""
        remaining = _lease_remaining()
        if remaining is not None and remaining <= 0:
            return True
        if sleeper is not time.sleep:
            # A supplied sleeper may advance a simulated clock or return
            # immediately. Give it one bounded call per poll interval so a
            # simulated long sleep cannot skip the lease renewal entirely.
            sleeper(
                max(
                    0.0,
                    min(interval, remaining, LEASE_RENEW_SECONDS)
                    if remaining is not None
                    else min(interval, LEASE_RENEW_SECONDS),
                )
            )
            if not runs.renew_watch_host_lease(project):
                return True
            remaining = _lease_remaining()
            return remaining is not None and remaining <= 0
        left = interval
        while True:
            remaining = _lease_remaining()
            if remaining is not None and remaining <= 0:
                return True
            step = min(left, LEASE_RENEW_SECONDS)
            if remaining is not None:
                step = min(step, remaining)
            sleeper(max(0.0, step))
            if not runs.renew_watch_host_lease(project):
                return True
            left -= step
            if left <= 0:
                break
        remaining = _lease_remaining()
        return remaining is not None and remaining <= 0

    with _watch_registration(project, stall_window) as (acquired, watcher):
        if not acquired:
            yield {
                "project": project,
                "event": "watcher-live",
                "run_id": None,
                "classification": "watcher_live",
                "next_action": "wait for the live project watcher to report",
                "watcher_live": True,
                "watcher": watcher,
            }
            return

        # The interval actually slept grows while the project has no live run
        # pointer and returns to the base on the first wake that sees one. The
        # producer records it each pass so a reader sees how far it has backed
        # off without asking the process.
        poll_interval_current = poll_interval
        # The snapshots one armed watcher carries between its polls. It lives
        # only as long as this watcher, so an independently armed producer
        # starting later can never be served an earlier watcher's reading.
        snapshot_cache: dict[str, tuple[str, dict[str, Any]]] = {}
        while True:
            if not runs.renew_watch_host_lease(project):
                return
            # An unlinked seat record is rewritten before anything else, so a
            # producer whose file was removed is findable by unwatch again, and
            # one superseded by a replacement producer ends here.
            if not _recreate_unlinked_registration(project, watcher):
                return
            remaining = _lease_remaining()
            if remaining is not None and remaining <= 0:
                return
            # Every stream the tick reads is counted, so the registration
            # carries what this poll actually parsed. An unchanged fleet
            # resumes from every cursor and parses nothing, which is the value
            # a reader uses to see the poll is stat-only rather than re-reading
            # the whole of every transcript. The admission check's own reads are
            # counted and reset on the same beat, so the two counters describe
            # one poll each.
            from reckon import _backends

            _backends.take_parsed_stream_bytes()
            take_admission_stream_bytes()
            pointers = list_live(project=project)
            _stop_delivered_reviews(pointers, signal_run=signal_run)
            moment = _utc_seconds()
            current = {
                str(pointer.get("run_id") or ""): _watch_snapshot(
                    pointer, moment=moment, stall_seconds=stall_seconds,
                    cache=snapshot_cache,
                )
                for pointer in pointers
                if pointer.get("run_id")
            }
            if current:
                # A wake that sees a live run ends any back-off: the producer's
                # interval is its base again from this pass.
                poll_interval_current = poll_interval
            update_watch_registration(
                project,
                poll_interval_seconds=poll_interval_current,
                bytes_parsed_last_poll=_backends.take_parsed_stream_bytes(),
            )
            if not current and not fleet_seen:
                if _wait(poll_interval_current):
                    return
                poll_interval_current = min(
                    poll_interval_current * 2.0, IDLE_POLL_INTERVAL_CAP_SECONDS
                )
                continue

            counts = _fleet_counts(current)
            if not fleet_seen:
                fleet_seen = True
                known = {run_id: dict(snapshot) for run_id, snapshot in current.items()}
                for snapshot in current.values():
                    yield _watch_transition(
                        project,
                        kind="baseline",
                        snapshot=snapshot,
                        previous=None,
                        current=str(snapshot["state"]),
                        counts=counts,
                        spend_runs=pointers,
                        rate_statuses=rate_statuses,
                    )
                continue

            folded, next_known = fleet_transitions(
                known, current, ledger_run_ids=_ledger_run_id_reader(project)
            )
            events = [
                _watch_transition(
                    project,
                    kind="transition",
                    snapshot=snapshot,
                    previous=previous,
                    current=state,
                    counts=event_counts,
                    spend_runs=pointers,
                    rate_statuses=rate_statuses,
                )
                for snapshot, previous, state, event_counts in folded
            ]
            known = next_known
            if events:
                yield from events
                if not current:
                    return
                continue
            if _wait(poll_interval_current):
                return
            if not current:
                poll_interval_current = min(
                    poll_interval_current * 2.0, IDLE_POLL_INTERVAL_CAP_SECONDS
                )


def watch_follow(
    project: str,
    *,
    stall_window: str = DEFAULT_WATCH_STALL_WINDOW,
    poll_interval: float = 1.0,
    sleeper: Callable[[float], None] = time.sleep,
    transitions: bool = False,
) -> Iterator[dict[str, Any]]:
    """Yield each newly terminal run, or the full transition stream on request.

    An empty project remains armed until its first pointer appears. Once a
    fleet has appeared, removing its last pointer ends the stream. Terminal
    and stalled run ids are remembered so an unreconciled pointer cannot
    repeatedly wake the watcher or hide a later run.
    """
    if transitions:
        yield from watch_ticker(
            project,
            stall_window=stall_window,
            poll_interval=poll_interval,
            sleeper=sleeper,
        )
        return

    stall_seconds = parse_duration(stall_window)
    reported_runs: set[str] = set()
    fleet_seen = False

    with _watch_registration(project, stall_window) as (acquired, watcher):
        if not acquired:
            yield {
                "project": project,
                "event": "watcher-live",
                "run_id": None,
                "classification": "watcher_live",
                "next_action": "wait for the live project watcher to report",
                "watcher_live": True,
                "watcher": watcher,
            }
            return

        while True:
            pointers = list_live(project=project)
            if not pointers:
                if fleet_seen:
                    return
                sleeper(poll_interval)
                continue
            fleet_seen = True

            moment = _utc_seconds()
            classified = [
                (pointer, classify_pointer(pointer, now_seconds=moment))
                for pointer in pointers
            ]
            for _pointer, row in classified:
                run_id = str(row.get("run_id") or "")
                if run_id not in reported_runs and row.get("manifest_status") in {
                    "complete",
                    "blocked",
                    "failed",
                }:
                    reported_runs.add(run_id)
                    yield {"project": project, "event": "terminal", **row}
                    break
            else:
                for pointer, row in classified:
                    run_id = str(row.get("run_id") or "")
                    if run_id not in reported_runs and row.get(
                        "manifest_status"
                    ) not in {"complete", "blocked", "failed"}:
                        quiet = _run_stream_quiet_seconds(pointer, now_seconds=moment)
                        # A quiet stream sleeping in a bounded wait is paused,
                        # not stalled, so it must not wake the follower the way
                        # a hang does — the same correction the ticker applies.
                        # Only a live process can be sitting in the wait: a dead
                        # one followed a bounded call no further and is a lost
                        # run the follower must still report.
                        live = row.get("process_alive") is True
                        if quiet > _stall_window_seconds(row, stall_seconds) and (
                            not live or _stall_wait_reason(pointer) is None
                        ):
                            reported_runs.add(run_id)
                            yield {
                                "project": project,
                                "event": "stalled",
                                **row,
                                "stalled_for_seconds": quiet,
                            }
                            break
                else:
                    sleeper(poll_interval)


def recover(
    *,
    project: str | None = None,
    config: Mapping[str, Any] | None = None,
    launcher: Callable[..., Any] | None = None,
    dispatch_reviews: bool = False,
) -> dict[str, Any]:
    """Classify live pointers; launch reviews only with --dispatch-reviews and --project.

    Each pointer is re-observed first, so the classification rests on the
    current stream and process table rather than on whatever the last writer
    believed. What gets repaired is the *record*: no worktree is removed, no
    process is reaped, and no run is promoted on this command's initiative — a
    completed-but-unpromoted run is reported with its manifest path so the
    orchestrator can promote it deliberately.

    Review dispatch requires ``dispatch_reviews`` and a named project. It
    reaches only runs whose dispatching session still has a live follower;
    otherwise the review remains with its coordinator. The sweep prefers the
    local lane unless the reviewed node explicitly declared another backend.
    """
    from reckon.crew.dispatch import observe

    if dispatch_reviews and not project:
        raise CrewError("review dispatch requires --project with --dispatch-reviews")

    reports = []
    scoring: list[dict[str, Any]] = []
    for pointer in list_live():
        if project and str(pointer.get("project") or "") != project:
            continue
        run_id = str(pointer.get("run_id") or "")
        observed: Mapping[str, Any] = pointer
        unreadable = ""
        if run_id:
            try:
                observed = observe(run_id, config=config)
            except CrewError as exc:
                unreadable = str(exc)
        observed = _derive_missing_manifest(observed, config=config)
        report = classify_pointer(observed)
        if unreadable:
            report["detail"] = f"{report['detail']} (stream unreadable — {unreadable})"
        reports.append(report)
        if report["classification"] == "scoring":
            scoring.append(observed)
    reports = group_terminal_lane_events(reports)
    counts = {
        name: sum(1 for item in reports if item["classification"] == name)
        for name in (
            "running",
            "scoring",
            "promotable",
            "completed_unpromoted",
            INTERRUPTED_RUN_PHASE,
            "abandoned",
            "lane-event",
        )
    }
    for name in ("waiting", "paused", "stopped", "blocked", "failed", "unreadable"):
        count = sum(1 for item in reports if item["classification"] == name)
        if count:
            counts[name] = count
    reflex = []
    awaiting_coordinator = []
    if dispatch_reviews:
        for record in scoring:
            session = str(record.get("session") or "")
            if not session or not runs.follower_state(project, session).get("live"):
                awaiting_coordinator.append(
                    {
                        "run_id": str(record.get("run_id") or ""),
                        "status": "awaiting-coordinator",
                        "reason": f"dispatching session {session or '<missing>'!r} is not live",
                    }
                )
                continue
            reflex.append(
                dispatch_review_for_run(
                    record, config=config, launcher=launcher, prefer_local=True
                )
            )
    return {
        "runs": reports,
        "counts": counts,
        "classes": list(RECOVERY_CLASSES),
        "reviews_dispatched": [
            r["review_run_id"] for r in reflex if r.get("dispatched")
        ],
        "reviews_awaiting_lane": [
            r["run_id"] for r in reflex if r.get("awaiting_lane")
        ],
        "reviews_refused": [r for r in reflex if r.get("refused")],
        "reviews_awaiting_coordinator": awaiting_coordinator,
    }
