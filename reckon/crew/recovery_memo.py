# ruff: noqa: I001, UP035
from __future__ import annotations

import contextlib
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from reckon._timestamps import parse_utc
from reckon.crew import runs
from reckon.crew import review as review_module
from reckon.crew.node import (
    INTERRUPTED_RUN_PHASE,
    LOG_STALE_AFTER_SECONDS,
)
from .recovery_liveness import (
    ATTEMPT_RECORD_NAME,
    EXIT_RECORD_NAME,
    WORKER_RECORD_NAME,
)


# A burst is anchored to its first end, so a chain of nearby endings cannot
# silently join events whose first and last runs ended minutes apart.
LANE_EVENT_WINDOW_SECONDS = 30


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


from .recovery_liveness import (  # noqa: E402
    _record_newest_stream,
)
from .recovery_review_subject import (  # noqa: E402
    _review_tree,
)
from .recovery_wait import (  # noqa: E402
    _run_directory,
)
from .recovery_watch import (  # noqa: E402
    _watch_verdict,
)
