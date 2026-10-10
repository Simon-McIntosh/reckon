# ruff: noqa: I001, UP035
from __future__ import annotations

import fcntl
import importlib
import json
import re
import shutil
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from reckon import ledger, review_tiers
from reckon._timestamps import parse_utc
from reckon.crew import lane_document as _lane_document
from reckon.crew import plan_review, runs
from reckon.crew import review as review_module
from reckon.crew import review_need
from reckon.crew.node import (
    CrewError,
)
from reckon.crew.reports import (
    ManifestParseError,
    parse_manifest,
)
from reckon.crew.runs import (
    _mutate_pointer,
    _utc_now,
    list_live,
    read_pointer,
)



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


from .recovery_classification import (  # noqa: E402
    classify_pointer,
)
from .recovery_liveness import (  # noqa: E402
    STRANDED_LAUNCH_BOUND_SECONDS,
    _exit_record_is_launch_failure,
    _run_exit_record,
    _stranded_launch,
    _worker_record,
)
from .recovery_review_acceptance import (  # noqa: E402
    _review_accepts_promotion,
)
from .recovery_review_delivery import (  # noqa: E402
    _is_review_run,
    _resolved_reviewed_run_id,
    _review_store_record_paths,
)
from .recovery_review_subject import (  # noqa: E402
    _composed_review_lane,
    _is_review_node,
    _resolve_commit,
    _review_dispatch_fields,
    _review_dispatch_tokens,
    _review_is_complete,
    _review_tree,
    _reviewed_run_head,
    _stored_review,
    review_described_head,
    same_revision,
)
from .recovery_vocabulary import (  # noqa: E402
    PLAN_REVIEW_NODE_PREFIX,
)
