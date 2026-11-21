from __future__ import annotations

# Imports below the definitions resolve sibling cycles after names are bound.
# ruff: noqa: E402
import contextlib
import json
import shlex
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from reckon import (
    ledger,
)
from reckon.crew.dispatch import (
    WORKER_SCRATCH_BUDGET_BYTES,
    remove_worker_scratch,
)
from reckon.crew.node import (
    CrewError,
    is_collectible_test_path,
)
from reckon.crew.reports import (
    parse_manifest,
)
from reckon.crew.routing import (
    RECLAIMABLE_CLASSES,
    WITHHELD_REASONS,
    _disposable_member_id,
    _inspect_workspace,
    _repository_tree_snapshot,
)
from reckon.crew.runs import (
    _live_worktree_claims,
    _manifest_freshness,
    _mutate_pointer,
    _repository_relative_scope,
    capture_run_session,
    drain,  # noqa: F401 - importable so a caller can substitute the fleet reading's drain
    pointer_path,
    read_pointer,
    run_dir,
)


def _read_json_object(path: Path) -> dict[str, Any]:
    """Read one small JSON object from the run directory, or return nothing.

    The run directory is written by several processes and may be removed under
    promotion at any moment, so every read here is best-effort: a file that is
    absent, unparsable, or not an object yields an empty mapping rather than an
    exception. The reconstruction below only ever defaults a field from what
    survives, so a missing record degrades to the same empty field a pointer
    that never carried the value would.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return dict(data) if isinstance(data, Mapping) else {}


def _project_for_repository(repository: Path) -> str:
    """The mounted project whose docs tree lives in this repository, if one does.

    A run rebuilt from its directory never carried the project on a pointer, so
    the project is resolved from the same mounted-project map every other scope
    lookup consults, by repository identity so a linked worktree resolves to the
    same project as its main checkout.
    """
    target = repository.resolve()
    from reckon.crew.routing import repository_projects

    try:
        projects = repository_projects(target)
    except (OSError, ValueError):
        return ""
    return str(projects[0]) if projects else ""


def _rebuild_record_from_run_directory(
    run_id: str, *, root: str | Path | None
) -> dict[str, Any] | None:
    """Reconstruct a run's record from its surviving run directory.

    A run whose live pointer was removed — or never written — keeps its run
    directory: ``prompt.txt``, ``stderr.log`` and ``stream.jsonl`` survive
    beside the supervisor, worker and attempt records the launch wrote, and the
    durable manifest the worker delivered. That is enough to complete the run:
    the directory names the repository and worktree the supervisor was given,
    the manifest names the delivery, and the stream supplies the measurement.
    Everything the directory cannot supply is left empty rather than guessed,
    so the guards downstream read an absent field the way they read a pointer
    that never carried it.

    Returns ``None`` when no run directory survives, which is the caller's own
    "no live run" case rather than a reconstruction failure.
    """
    directory = run_dir(run_id)
    if not directory.is_dir():
        return None
    supervisor = _read_json_object(directory / "supervisor.json")
    worker = _read_json_object(directory / _WORKER_RECORD_NAME)
    attempt_record = _read_json_object(directory / _ATTEMPT_RECORD_NAME)
    repo = str(supervisor.get("repo") or (root if root is not None else "") or "")
    worktree = str(supervisor.get("worktree") or repo)
    manifest = directory / "manifest.md"
    manifest_path = str(manifest) if manifest.is_file() else ""
    project = _project_for_repository(Path(repo)) if repo else ""
    record: dict[str, Any] = {
        "run_id": run_id,
        "project": project,
        "repo": repo,
        "worktree": worktree,
        "base_sha": "",
        "role": "",
        "launch": "",
        "created_at": str(attempt_record.get("attempt_started_at") or ""),
        "manifest_path": manifest_path,
        "node": {},
        "fenced": supervisor.get("fenced") is True,
        "rebuilt_from_run_directory": True,
    }
    # The pointer carries the run's assigned write scope, and a discard removes
    # it with the pointer. The durable manifest is the one scope declaration
    # that survives that removal: its ``changed_paths`` names the paths the run
    # says it delivered, so a rebuilt record presents that declaration in place
    # of the lost assignment. Without it a run citing its own commit is refused
    # for changing paths no surviving declaration contains, which is exactly the
    # discarded run that reached main through an outside-scope entry.
    if manifest.is_file():
        try:
            manifest_data = parse_manifest(manifest.read_text(encoding="utf-8"))
        except (OSError, KeyError, ValueError):
            manifest_data = {}
        declared = list(_changed_paths_inside_repository(manifest_data, record))
        if declared:
            record["node"] = {"write_paths": declared}
    attempt = attempt_record.get("attempt") or worker.get("attempt")
    if attempt is not None:
        record["attempt"] = attempt
    attempt_kind = attempt_record.get("attempt_kind")
    if attempt_kind:
        record["attempt_kind"] = attempt_kind
    if "backend" in worker:
        record["backend"] = worker.get("backend")
    return record


def _read_pointer_or_rebuild(run_id: str, *, root: str | Path | None) -> dict[str, Any]:
    """A run's pointer when one survives, else its record rebuilt from disk.

    Promotion is the one command that must complete a run from whatever
    classification of it survives: a run whose pointer has been removed is
    exactly the run ``crew complete`` was refusing, and its run directory still
    holds the delivery. The pointer keeps first claim — it is the launch's own
    record — and the run directory is read only in its absence.
    """
    if pointer_path(run_id).exists():
        return read_pointer(run_id)
    rebuilt = _rebuild_record_from_run_directory(run_id, root=root)
    if rebuilt is None:
        raise CrewError(
            f"no live run {run_id!r} (looked in {pointer_path(run_id)}) and no "
            "run directory survives to rebuild it from"
        )
    return rebuilt


def _complete_withdrawn_run(run_id: str, record: Mapping[str, Any]) -> dict[str, Any]:
    """Report and retire a run that names no project.

    A dispatch refused before a project is resolved leaves a run with an empty
    project: the pointer never carried one, or the reconstruction of a
    pointerless run directory finds no supervisor record to read one from.
    There is no project ledger to append a row to, so ``crew complete`` reports
    the withdrawal and writes no row rather than failing validation on the empty
    project name.

    The withdrawal still retires what a promotion retires. A run whose pointer
    survives is otherwise left reading as in flight, and nothing reconciles it
    — so the pointer is removed and the workspace released through the same
    release path a promotion uses, without the ledger row a promotion's release
    receipt would need a project to hold. The run's own record is returned so a
    reader sees what survived of the launch; ``status`` and ``withdrawn`` both
    state the word the fleet vocabulary already uses for a departure that
    records no landing.
    """
    from reckon.crew.promotion import _remove_promoted_pointer

    capture = capture_run_session(record)
    pointer_existed = _remove_promoted_pointer(run_id, record)
    release = _release_after_promotion(run_id, record)
    release.update(_retire_disposable_identity(record))
    return {
        "run_id": run_id,
        "project": "",
        "withdrawn": True,
        "status": "withdrawn",
        "promoted": False,
        "ledger_row_written": False,
        "pointer_removed": pointer_existed and not pointer_path(run_id).exists(),
        "reason": (
            "the run names no project, so its dispatch was refused before a "
            "project was resolved; the run is withdrawn, its pointer retired "
            "and its workspace released, and no ledger row is written"
        ),
        "release": release,
        "session_capture": capture,
        "record": dict(record),
    }


def _manifest_relative_path(value: Any, *, manifest_path: str) -> Path:
    """Resolve one manifest-cited path under the manifest's own directory.

    A worker writes its log paths relative to the run directory the manifest
    lives in, so a relative citation is anchored there rather than to whichever
    directory the promoting process happens to run in. An absolute path is
    taken as written.
    """
    path = Path(str(value)).expanduser()
    if not path.is_absolute() and manifest_path:
        return Path(manifest_path).expanduser().parent / path
    return path


def _command_from_gate_log(log_text: str) -> str:
    """The command a gate log's own header names, or the empty string, as a flag.

    The fleet's gate logs open with header lines that make the file
    self-describing, one of which states the exact text the shell was handed.
    Reading it back is how a run whose manifest describes its suite in prose
    still promotes on the command that actually ran. The header is read by the
    same reader the manifest evidence check applies to a cited test log, so a
    log reads identically wherever it is cited, and a recovered text that is
    itself prose is refused rather than filled: a promotion must be handed
    something a shell could be given.
    """
    recovered = _test_log_command(str(log_text or "")) or ""
    if recovered and not _gate_command_prose(recovered):
        return recovered
    return ""


def _review_role_run(record: Mapping[str, Any]) -> bool:
    """Whether this run is a reviewer, by its role or by its minted node id."""
    from reckon.crew.recovery_review_delivery import _is_review_run

    return _is_review_run(record)


def _review_record_evidence(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """The stored review record a review-role run promotes on, or None.

    A review run's deliverable is the record it stores for the run it read, so
    that record — its status, its findings and the report path it names — is
    the evidence a promotion of the run rests on. It is read through the same
    delivery reader the review loop's self-promotion uses, so the hand path and
    the automatic one cannot disagree about whether a review has delivered.
    A record whose status is outside the recognised set stands as no evidence.
    """
    project = str(record.get("project") or "").strip()
    if not project:
        return None
    from reckon.crew.recovery_review_delivery import _delivered_review_record
    from reckon.crew.review import RECOGNISED_REVIEW_STATUSES

    stored = _delivered_review_record(record, project)
    if stored is None:
        return None
    _, path = stored
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(body, Mapping):
        return None
    status = str(body.get("status") or "").strip()
    if status not in RECOGNISED_REVIEW_STATUSES:
        return None
    findings = body.get("findings")
    return {
        "run_id": str(record.get("run_id") or "").strip(),
        "path": str(path),
        "status": status,
        "findings": len(findings) if isinstance(findings, list) else 0,
        "report_path": str(body.get("report_path") or "").strip(),
    }


def _review_record_check(
    evidence: Mapping[str, Any], *, capture_log: bool = True
) -> dict[str, Any]:
    """The gate check a stored review record stands as.

    A review run changes no code, so there is no suite for the integration
    re-run to execute at the integrated head. What a reader can re-execute is
    that the record this promotion rests on is present and non-empty — that is
    the review run's whole check. The record is not the log: it is JSON, and no
    JSON record carries the terminal ``EXIT=<n>`` line every reader of a cited
    log reads the command's status from, so citing the record as its own
    capture states no status at all and the check refuses the derivation that
    produced it. The check's outcome is captured into the run's own log
    instead, which the record is named on, and that log is what the check
    cites.

    ``capture_log`` is false when the promotion already holds a log — the
    caller's flag, or one the manifest names — because that is the evidence the
    run re-executed and a log written beside it would be a file nothing points
    at. The check still reports its own exit status in that case, so a citation
    that disagrees with the record still refuses.
    """
    from reckon.crew.promotion_evidence import _derived_review_gate_log

    record_path = str(evidence["path"])
    command = f"test -s {shlex.quote(record_path)}"
    check: dict[str, Any] = {"command": command}
    check.update(
        _derived_review_gate_log(
            str(evidence.get("run_id") or ""),
            Path(record_path),
            command=command,
            status=str(evidence.get("status") or ""),
            findings=int(evidence.get("findings") or 0),
            capture=capture_log,
        )
    )
    return check


def _default_gate_log_from_manifest(
    manifest: Mapping[str, Any], *, manifest_path: str
) -> str:
    """The log a passing gate defaults to when the caller names none, or "".

    A run's own manifest already states which log carries its passing suite
    evidence, so the default is read from there rather than guessed. The
    ``after_suite`` arm's own log wins when the arm names one: that arm is the
    run's statement of what it measured at its head. Otherwise the first
    ``test_logs`` entry stands in, which is the order the worker wrote them in.

    The manifest's ``negative_control_log`` is never taken as a default under
    either rule. A negative control is a deliberately failing run kept to prove
    the check can fail, so its failing ids are by construction ones the head
    arm does not carry, and a gate read from it refuses every sound run. A
    manifest that offers no log but its negative control therefore has no
    default at all, and its refusal names the flag that supplies one.
    """
    negative = str(manifest.get("negative_control_log") or "").strip()
    negative_path = (
        _manifest_relative_path(negative, manifest_path=manifest_path).resolve()
        if negative
        else None
    )
    after = manifest.get("after_suite")
    if isinstance(after, Mapping):
        after_log = str(after.get("log_path") or "").strip()
        if after_log:
            resolved = _manifest_relative_path(after_log, manifest_path=manifest_path)
            if negative_path is None or resolved.resolve() != negative_path:
                return str(resolved)
    logs = [
        str(entry).strip()
        for entry in (manifest.get("test_logs") or [])
        if str(entry).strip()
    ]
    for entry in logs:
        resolved = _manifest_relative_path(entry, manifest_path=manifest_path)
        if negative_path is not None and resolved.resolve() == negative_path:
            continue
        return str(resolved)
    if logs and negative_path is not None:
        raise CrewError(
            "the manifest's only test log is its negative_control_log "
            f"({negative}), a deliberately failing run that cannot stand as a "
            "gate; pass --gate-log-path naming the log holding the run's "
            "passing gate evidence"
        )
    return ""


def _default_gate_evidence_from_manifest(
    record: Mapping[str, Any],
    *,
    verdict: str,
    gate_check: Mapping[str, Any] | None,
    commits: Sequence[str],
    no_commit_reason: str = "",
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Fill a passing promotion's gate flags from the worker's own manifest.

    A passing gate must carry the command that produced it, its exit status and
    its log, and a ledger row loses the work when a coordinator drops a commit
    the manifest already named. Reckon holds all of that the moment the worker
    delivers: the manifest states the gate command in ``tests``, the log path in
    ``test_logs``, its exit status on the log's own ``EXIT=`` line, and the
    commits in ``commits``. Each is taken only where the operator's flag left the
    value absent, so an explicit --gate-command or --commit always wins, and a
    manifest that states nothing leaves the guard's ordinary refusal untouched.
    Returns the gate-check mapping and the commit list unchanged for a
    non-passing gate, since neither is required there.
    """
    resolved_gate = dict(gate_check) if isinstance(gate_check, Mapping) else {}
    resolved_commits = tuple(str(sha).strip() for sha in commits if str(sha).strip())
    if str(verdict).strip().lower() != "passed":
        return resolved_gate, resolved_commits
    # A command and the log it ran against describe one execution, so a check
    # completed here — a caller's own half beside evidence this fill derived
    # from the manifest or the stored record — reads as a run nobody performed.
    # The completion is refused where it would happen, not up front: a
    # half-stated check this fill completes nothing for is a check missing a
    # field, and the gate's own refusal names that field. Raising up front
    # instead hides every other precondition the promotion judges beside it,
    # which is the one-refusal contract the rest of the promotion keeps.
    given_command = bool(str(resolved_gate.get("command") or "").strip())
    given_log = bool(
        str(resolved_gate.get("log_path") or "").strip()
        or str(resolved_gate.get("log_digest") or "").strip()
    )

    def refuse_a_completed_check() -> None:
        """Refuse filling the half a caller left out of a half-stated check."""
        if given_command != given_log:
            raise CrewError(
                "a gate check is a command and the evidence it ran against: "
                "pass --gate-command and --gate-log-path (or "
                "--gate-log-digest) together, or neither, because a command "
                "beside a log the promotion found elsewhere records a run "
                "that never happened"
            )

    def fill(key: str, value: Any) -> None:
        """Fill one check field, refusing a check completed from elsewhere."""
        refuse_a_completed_check()
        resolved_gate[key] = value

    manifest = _fresh_manifest(record)
    if manifest is None:
        return resolved_gate, resolved_commits
    manifest_path = str(record.get("manifest_path") or "")
    if not str(resolved_gate.get("log_path") or "").strip():
        default_log = _default_gate_log_from_manifest(
            manifest, manifest_path=manifest_path
        )
        if default_log:
            fill("log_path", default_log)
    log_path = str(resolved_gate.get("log_path") or "").strip()
    log_text = ""
    if log_path:
        try:
            log_text = Path(log_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            log_text = ""
    declared_command = str(manifest.get("tests") or "").strip()
    # A review run's evidence is the record it stored, so its manifest's
    # ``tests`` line describes a check rather than naming one — it may be a
    # list of the checks the node's fence named rather than a command at all.
    # The record stands as the check instead, so such a line never reaches the
    # executable-gate refusal on the list's own repr. It fills only what the
    # caller's flag left absent, so an explicit gate check the operator gives
    # is kept: that operator states the command and log they want re-executed,
    # and a derived one replacing it would discard the evidence they named.
    # A review run with no delivered record is left to the ordinary rules
    # below: a review that stores its record during promotion has nothing on
    # disk yet, and a prose line still reaches the executable-gate refusal.
    review_evidence: dict[str, Any] | None = None
    if _review_role_run(record):
        review_evidence = _review_record_evidence(record)
    if review_evidence is not None:
        # The stored record supplies both halves of the check, and the log it
        # captures is written where the promotion will cite it, so a half-stated
        # check is refused before that derivation rather than after it: a
        # promotion that refuses leaves no file behind it.
        refuse_a_completed_check()
        # The check's log is captured only where the promotion will cite it: a
        # caller's own flag, or a log the manifest already names, is the
        # evidence that run re-executed, and writing a second log beside it
        # would leave a file nothing points at.
        check = _review_record_check(
            review_evidence,
            capture_log=not str(resolved_gate.get("log_path") or "").strip(),
        )
        for key, value in check.items():
            if key == "exit_status":
                if resolved_gate.get(key) is None:
                    resolved_gate[key] = value
            elif not str(resolved_gate.get(key) or "").strip():
                fill(key, value)
    command_unset = not str(resolved_gate.get("command") or "").strip()
    if command_unset and declared_command and not _gate_command_prose(declared_command):
        fill("command", declared_command)
    # A worker that writes a description of its suite on the `tests:` line
    # rather than the command cannot have that text re-run at the integrated
    # head. The cited log's own header names the command that actually ran, so
    # the fill recovers it from there: the run then promotes on the evidence
    # already on disk rather than on the operator retyping the command, which
    # is the remedy the seven-day measurement of promoted manifests names. The
    # declared text is kept as the fallback only when the log names no command,
    # so a run with no recoverable evidence still reaches the ordinary refusal.
    if not str(resolved_gate.get("command") or "").strip():
        recovered = _command_from_gate_log(log_text)
        if recovered:
            fill("command", recovered)
        elif declared_command:
            fill("command", declared_command)
    if resolved_gate.get("exit_status") is None:
        recorded = _recorded_exit_status(log_text) if log_text else None
        if recorded is not None:
            resolved_gate["exit_status"] = recorded
    if not resolved_commits and not str(no_commit_reason).strip():
        declared = [
            str(sha).strip()
            for sha in (manifest.get("commits") or [])
            if str(sha).strip()
        ]
        if declared:
            resolved_commits = tuple(declared)
    return resolved_gate, resolved_commits


GATE_DECLARATION_KEY = "gate_declaration"

#: The keys a run's gate declaration may carry. A declaration names how the
#: run's arms were run: the command they ran and the partition they ran on.
_GATE_DECLARATION_FIELDS = ("command", "partition", "long_partition", "reason")


def gate_declaration(record: Mapping[str, Any]) -> dict[str, str]:
    """The command and partition a run's gate declared, or an empty mapping.

    A run whose arms are only comparable on a partition the project's own
    declaration does not name — a GPU gate on a project armed with a CPU
    command — states them on the run instead. The declaration is recorded by
    a supported verb and read here, so the comparison and the landing's own
    measurement both use the same declaration the coordinator stated.
    """
    raw = record.get(GATE_DECLARATION_KEY)
    if not isinstance(raw, Mapping):
        return {}
    return {key: str(raw.get(key) or "").strip() for key in _GATE_DECLARATION_FIELDS}


def _declared_command_text(record: Mapping[str, Any]) -> str:
    """The command the run's gate runs, declaration first, then the arming.

    A declaration names what the arms actually ran with, so it outranks the
    project's own arming for this run: only a run whose gate declared a command
    differing from the project's is a declared arm set.
    """
    declared = gate_declaration(record).get("command") or ""
    return " ".join(str(declared or record.get("suite_command") or "").split())


def _normalized_command(value: Any) -> str:
    """Return a command with runs of whitespace collapsed to single spaces.

    Two arms that differ only in spacing exercise the same command, so the
    comparison against the record's armed command is made on the normalised
    spelling rather than the raw one.
    """
    return " ".join(str(value or "").split())


def _command_words(value: Any) -> list[str]:
    """Split a recorded command into words, best-effort.

    A worker records a command as the shell string it ran, so quoting has to be
    honoured for two spellings of one command to meet. An unparsable string is
    split on whitespace rather than refused: the comparison that follows still
    distinguishes it from a command that parses.
    """
    text = str(value or "")
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def _is_environment_assignment(word: str) -> bool:
    """Whether a word is a leading ``NAME=value`` environment assignment."""
    name, separator, _ = word.partition("=")
    return bool(separator) and name.isidentifier()


def _leading_cd_tree(words: Sequence[str]) -> tuple[list[str], str | None]:
    """Drop a leading ``cd <tree> &&`` and name the tree it changed into.

    A worker commonly runs its arm from the arm's own tree, recording the
    ``cd`` it used. The tree itself does not change what the arm runs, so it is
    removed from the comparison and returned, because a relative interpreter or
    test path is resolved against it.
    """
    if len(words) >= 3 and words[0] == "cd" and words[2] == "&&":
        return list(words[3:]), words[1]
    return list(words), None


def _resolved_interpreter(word: str, tree: Path | None) -> str:
    """One spelling for one interpreter, whether relative or absolute.

    ``.venv/bin/python`` is the spelling the armed template carries and a
    worker commonly records the absolute path of the same executable. Resolved
    against the arm's own tree, both reach the one file the environment's
    interpreter is, so they compare equal. Two different environments stay two
    different paths and still refuse.
    """
    path = Path(word).expanduser()
    if not path.is_absolute():
        if tree is None:
            return word
        path = Path(tree) / path
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def _canonical_suite_command(value: Any, tree: Path | None) -> tuple[str, ...]:
    """The words a command runs, in a form two spellings of it share.

    A suite arm matches when it runs the same thing, not when it prints the
    same string. Three differences change the spelling without changing the
    run: the test files may be named in another order, the interpreter may be
    absolute where the template is relative, and the arm may change into its
    own tree first. Test files are compared as a set and the interpreter as one
    resolved path; every other word, including each flag, is compared in place.
    """
    words, changed_into = _leading_cd_tree(_command_words(value))
    interpreter = next(
        (
            index
            for index, word in enumerate(words)
            if not _is_environment_assignment(word)
            and Path(word).name.startswith("python")
        ),
        None,
    )
    if interpreter is not None:
        anchor = Path(changed_into).expanduser() if changed_into is not None else tree
        words[interpreter] = _resolved_interpreter(words[interpreter], anchor)
    test_files = sorted(
        word
        for word in words
        if not word.startswith("-") and is_collectible_test_path(word)
    )
    others = [
        word
        for word in words
        if word.startswith("-") or not is_collectible_test_path(word)
    ]
    return tuple(others + test_files)


def _same_tree(first: str | Path | None, second: str | Path | None) -> bool:
    """Whether two spellings name one tree, ignoring a symlinked home."""
    if first is None or second is None:
        return True
    try:
        return Path(first).expanduser().resolve() == Path(second).expanduser().resolve()
    except OSError:
        return str(first) == str(second)


def _arm_command_matches(
    arm_command: str, armed_commands: Sequence[str], tree: Path | None
) -> bool:
    """Whether an arm's command runs what one of the armed commands runs.

    The one tree difference that is accepted is a ``cd`` into the arm's own
    tree; a ``cd`` anywhere else runs another tree's tests, so it refuses.
    """
    _, changed_into = _leading_cd_tree(_command_words(arm_command))
    if changed_into is not None and not _same_tree(changed_into, tree):
        return False
    registered = {_canonical_suite_command(armed, tree) for armed in armed_commands}
    return _canonical_suite_command(arm_command, tree or changed_into) in registered


SCOPED_SUITE_PLACEHOLDER = "{node_test_files}"

#: The fields a suite arm's record states, in the flat shape a reader accepts.
#: An arm carrying none of them declares no measurement rather than a record.
_ARM_FIELDS = frozenset(
    {
        "revision",
        "command",
        "exit_status",
        "completed",
        "failure_count",
        "failure_ids",
        "log_path",
        "log_digest",
    }
)


def _scope_tree(record: Mapping[str, Any]) -> Path | None:
    """The tree whose object store answers what a revision holds, or None."""
    for key in ("worktree", "repo"):
        text = str(record.get(key) or "").strip()
        if text:
            return Path(text).expanduser()
    return None


def _run_head(record: Mapping[str, Any]) -> str:
    """The revision the run's work is delivered at, or empty when unreadable.

    An after arm is evidence about the tree the run delivers, so the revision it
    must record is the run's head. While the worktree is readable its ``HEAD``
    is that answer, and ``_run_head_for_review`` reads it. Once the worktree has
    been reclaimed the manifest's citations are the only evidence left, and
    ``_record_carried_head`` resolves them by ancestry — the same reader a
    review uses, so a review and a promotion of one run name one head whichever
    order the citations were written in. An empty answer means no head could be
    read, and the caller then holds no revision to compare against rather than
    refusing on one.
    """
    # Deferred: the review modules import this package's siblings, so the
    # import is made where it is used rather than at load.
    from reckon.crew.recovery_review_dispatch import (
        _record_carried_head,
        _run_head_for_review,
        _worktree_reclaimed,
    )

    if not _worktree_reclaimed(record):
        return _run_head_for_review(record)
    return _record_carried_head(record)


def _arm_revision_mismatch(
    record: Mapping[str, Any],
    arm_name: str,
    observation: Mapping[str, Any],
    head: str,
) -> list[str]:
    """Name an after arm recorded at a revision other than the run's head.

    An arm compares against the head only when both revisions answer: ``head``
    is read by the caller, and the revision the arm records must name a commit
    in the run's tree. A head that could not be read and a citation that names
    no commit are unreadable instruments rather than defects in the arm, so
    neither is named here and the arm's other fields still decide the pair.

    A citation that does resolve and differs from the head is the case this
    check exists for: the arm measured a tree the run no longer delivers. The
    one exception is a head that moved through commits the suite cannot see. A
    documentation or evidence commit leaves every module the suite imports and
    every test file it collects untouched, so the arm still describes the suite
    the head runs and re-running it would only repeat the same result. The
    paths are read as a conservative question rather than a proof: a commit
    touching any Python file or anything under the tests tree is treated as
    reaching the suite, because deciding otherwise needs an import graph this
    check does not carry, and an arm wrongly kept is a regression that lands.
    """
    if arm_name != "after_suite" or not head:
        return []
    recorded = str(observation.get("revision") or "").strip()
    if not recorded:
        return []
    # Deferred for the same reason as the head resolver above.
    from reckon.crew.recovery_review_subject import _resolve_commit, _review_tree

    tree = _review_tree(record)
    if tree is None:
        return []
    resolved = _resolve_commit(tree, recorded, cached=True)
    head_resolved = _resolve_commit(tree, head, cached=True)
    if not resolved or resolved == head_resolved:
        return []
    reaching = _suite_reaching_paths(tree, resolved, head_resolved)
    if reaching is None or reaching:
        return [f"{arm_name}.revision_matches_head"]
    return []


def _suite_reaching_paths(tree: Path, earlier: str, later: str) -> list[str] | None:
    """The paths between two revisions a suite's result can depend on.

    ``None`` means the diff could not be read, and the caller then treats the
    arm as invalidated rather than measuring nothing and calling it clean. The
    paths named are the ones a suite can be affected by: every Python file,
    because an import chain need not run through a test file; anything under
    the tests tree, because helpers, fixtures, data and configuration there
    change what pytest collects; and the pytest configuration files at the
    repository root. Documentation, plan HTML and other evidence paths are
    absent from the answer, which is what lets a later documentation commit
    leave an arm about the code untouched.
    """
    try:
        completed = subprocess.run(
            ["git", "-C", str(tree), "diff", "--name-only", f"{earlier}..{later}"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if completed.returncode:
        return None
    reaching: list[str] = []
    for line in completed.stdout.splitlines():
        path = line.strip()
        if not path:
            continue
        if (
            path.endswith(".py")
            or path.startswith("tests/")
            or path in {"pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini"}
        ):
            reaching.append(path)
    return reaching


def _paths_present_in_tree(
    tree: Path | None, revision: str, paths: Sequence[str]
) -> set[str] | None:
    """Which of ``paths`` the tree at ``revision`` holds, or None when unknown.

    A write path is not a file that exists: a node may delete the test it
    replaced or create one that is not there yet, and the same write scope then
    names a different file list in the base tree and in the head tree. Which
    one a revision holds is a fact about the revision, so it is read from the
    revision's own tree through the object store rather than from any working
    directory, which is the head's content whichever arm is being resolved.

    ``None`` means the probe could not answer — no tree, no revision, a
    revision the store does not hold, or output that does not line up with the
    request. Callers then resolve over the declared scope as written, which is
    the reading every run had before the per-tree rule, and the arm-level
    checks below still refuse a pair that measured nothing.
    """
    if tree is None or not revision or not paths or not tree.is_dir():
        return None
    # The revision itself is queried first: a name the store does not hold
    # answers "missing" for every path asked of it, which reads exactly like a
    # tree holding none of them.
    queries = [revision, *(f"{revision}:{path}" for path in paths)]
    try:
        result = subprocess.run(
            ["git", "-C", str(tree), "cat-file", "--batch-check"],
            input="\n".join(queries) + "\n",
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    answers = result.stdout.splitlines()
    if len(answers) != len(queries):
        return None
    if answers[0].rstrip().endswith(" missing") or " commit " not in f"{answers[0]} ":
        return None
    answers = answers[1:]
    # The answers are in request order, so a path is matched to its own answer
    # by position rather than by parsing the echoed name, which git may quote.
    return {
        path
        for path, answer in zip(paths, answers, strict=True)
        if not answer.rstrip().endswith(" missing")
    }


def _repository_write_paths(record: Mapping[str, Any]) -> list[str]:
    """The node's fenced write paths, repository-relative, in declared order.

    The paths are named relative to the run's tree because the same command runs
    against two trees — the base and the head — and an absolute path into one of
    them names the wrong tree in the other. A path outside that tree names the
    run rather than the repository, so it is dropped: a default role path under
    the run directory is not a file any suite in the tree can measure.
    """
    node = record.get("node")
    write_paths = node.get("write_paths") if isinstance(node, Mapping) else None
    if isinstance(write_paths, (str, bytes)) or not isinstance(write_paths, Sequence):
        return []
    repo_text = str(record.get("worktree") or record.get("repo") or "").strip()
    repo = Path(repo_text).expanduser() if repo_text else None
    relative_paths: list[str] = []
    for entry in write_paths:
        text = str(entry or "").strip()
        if not text:
            continue
        relative = _repository_relative_scope(text, repo)
        if relative is not None:
            relative_paths.append(relative)
    return relative_paths


def _node_test_files(record: Mapping[str, Any]) -> list[str]:
    """The node's fenced write paths pytest collects as tests, sorted.

    A scoped suite command names the tests a node's own change can reach rather
    than the whole suite, so the files it runs come from the fenced write scope
    the node was dispatched with. The command hands each path to pytest, so the
    rule here is pytest's collection rule rather than the dispatcher's broader
    trigger: a node scoped to a fixture under ``tests/`` is still writing part
    of a fenced check, and the fixture is not something pytest can run.
    """
    return sorted(
        {
            path
            for path in _repository_write_paths(record)
            if is_collectible_test_path(path)
        }
    )


def _census_tests(record: Mapping[str, Any]) -> list[str]:
    """The census tests the project declares for this run, in declared order.

    A census checks the whole tree rather than one node's code, so its file is
    reachable from no node's own write scope. The project names its census tests
    in its flight file and the run carries that list, so reckon hardcodes no
    test path of its own. A record carrying none declares no census, which
    resolves exactly as the scoped command did before census tests existed.
    """
    declared = record.get("census_tests")
    if isinstance(declared, (str, bytes)) or not isinstance(declared, Sequence):
        return []
    return [str(entry).strip() for entry in declared if str(entry or "").strip()]


def _writes_product_source(record: Mapping[str, Any]) -> bool:
    """Whether any fenced write path names source outside the docs tree.

    A census is worth running exactly when the node can change the tree a census
    reads. A node writing only documents and plans cannot add a runtime state
    writer or any other source the census inventories, so running the whole-tree
    census for it would spend its gate budget measuring work it did not touch.
    """
    return any(
        Path(path).parts[:1] != ("docs",) for path in _repository_write_paths(record)
    )


def _scoped_suite_command(
    record: Mapping[str, Any], *, revision: str | None = None
) -> str:
    """The armed suite command with its scoped placeholder resolved, or empty.

    A flight config may arm the project with a command carrying
    ``{node_test_files}``; the placeholder stands for the node's own fenced
    test files, so one declaration scopes every node to the tests its change
    can affect. A declaration with no placeholder is returned as written.

    A node writing product source also measures the project's declared census
    tests, because a census is drawn from the whole tree rather than from one
    node's scope, and a node that adds a runtime state writer would otherwise never run
    it. A node writing only documents and plans measures none of them.

    ``revision`` names the tree the command will run against, and the files are
    then only those that tree holds: a write path is not a file that exists, so
    a test the node deletes leaves the head arm and one it creates leaves the
    base arm. Without a revision the declaration is resolved over the whole
    declared scope, which is what a caller can state before either tree exists
    and what every run recorded before the per-tree rule carried.

    An empty result means the run has nothing to measure with: no declaration
    at all, or a scoped one over a scope — or a tree — holding no test file and
    no census the project declares. Promotion then records no suite observation
    for the pair, which is the same state an unarmed project is in.
    """
    declared = _declared_command_text(record)
    if not declared:
        return ""
    if SCOPED_SUITE_PLACEHOLDER not in declared:
        return declared
    files = _node_test_files(record)
    census = (
        [name for name in _census_tests(record) if name not in files]
        if _writes_product_source(record)
        else []
    )
    if revision is not None:
        present = _paths_present_in_tree(_scope_tree(record), revision, files + census)
        if present is not None:
            files = [name for name in files if name in present]
            census = [name for name in census if name in present]
    measured = files + census
    if not measured:
        return ""
    return " ".join(
        declared.replace(SCOPED_SUITE_PLACEHOLDER, " ".join(measured)).split()
    )


#: Exits that mean pytest ran no test at all: a usage error over a path it
#: cannot open, and a collection that found nothing to run. Either way the arm
#: observed no test, so it is no evidence that the other arm added no failure.
_NOTHING_COLLECTED_EXITS = (4, 5)


def _arm_collected_nothing(observation: Mapping[str, Any], arm_name: str) -> list[str]:
    """Name the arm when it exited having collected no test to run."""
    exit_status = observation.get("exit_status")
    failure_count = observation.get("failure_count")
    if isinstance(exit_status, bool) or not isinstance(exit_status, int):
        return []
    if exit_status not in _NOTHING_COLLECTED_EXITS:
        return []
    if isinstance(failure_count, bool) or not isinstance(failure_count, int):
        return []
    if failure_count:
        return []
    return [f"{arm_name}.collected_no_test"]


def _named_paths_absent_from_tree(
    record: Mapping[str, Any], observation: Mapping[str, Any]
) -> list[str]:
    """The scoped test files an arm's command names that its own tree lacks.

    A command naming a file the tree does not hold cannot have run it: pytest
    refuses the whole invocation, so the arm's empty failure list describes no
    test rather than a clean one. This is the shape a run dispatched before the
    per-tree rule records when its scope creates a test file, and the shape the
    old resolution produced at the head for a scope that deletes one.

    An unanswered probe reports nothing here: the arm-level refusal below still
    catches a pair that measured nothing through its collected-nothing check.
    """
    command = str(observation.get("command") or "")
    named = [path for path in _node_test_files(record) if path in command]
    if not named:
        return []
    revision = str(observation.get("revision") or "").strip()
    present = _paths_present_in_tree(_scope_tree(record), revision, named)
    if present is None:
        return []
    return [path for path in named if path not in present]


def _arm_measured_nothing(
    record: Mapping[str, Any], observation: Mapping[str, Any]
) -> bool:
    """Whether an arm's own tree left it no test file to run.

    The base arm of a scope whose tests are all created by the change is the
    ordinary case: there was nothing to measure against, so an empty baseline
    is the honest description of it rather than a refusal. The after arm is
    held to the opposite reading — a run measuring its own change with no test
    file left is the pair the per-tree rule exists to refuse.
    """
    declared = _declared_command_text(record)
    if SCOPED_SUITE_PLACEHOLDER not in declared:
        return False
    revision = str(observation.get("revision") or "").strip()
    if not _scoped_suite_command(record, revision=revision or None):
        return True
    return bool(_named_paths_absent_from_tree(record, observation))


def _arm_declares_no_measurement(observation: Any) -> bool:
    """Whether a suite arm states no measurement at all.

    ``baseline_suite: none`` parses to an empty record, and a mapping carrying
    no arm field is the same declaration. Anything stating a revision, a command
    or a count is a measurement to be judged on its own terms rather than a
    declaration that there is none.
    """
    if not isinstance(observation, Mapping):
        return True
    return not (set(observation) & _ARM_FIELDS)


def _base_tree_holds_no_test_file(record: Mapping[str, Any]) -> bool:
    """Whether the run's base revision holds none of the node's test files.

    Read from the base revision's own tree through the object store rather than
    from a working directory, which holds the head's content whichever arm is
    being asked about. An unanswered probe answers False, because an unprobed
    base is not a licence to accept an absent arm.
    """
    files = _node_test_files(record)
    base = str(record.get("base_sha") or "").strip()
    if not files or not base:
        return False
    present = _paths_present_in_tree(_scope_tree(record), base, files)
    return present is not None and not present


def _base_arm_measured_an_empty_suite(
    record: Mapping[str, Any], observation: Any
) -> bool:
    """Whether an absent base arm is the complete measurement of an empty scope.

    The pair is accepted only when all three hold: the declaration is scoped, so
    the base command is resolved per tree and can legitimately have named no
    file; the arm states no measurement of its own; and the base revision holds
    none of the tests the node is fenced to. A base tree that does hold them has
    a suite to run, and an arm declaring none there is missing evidence.
    """
    declared = " ".join(str(record.get("suite_command") or "").split())
    if SCOPED_SUITE_PLACEHOLDER not in declared:
        return False
    if not _arm_declares_no_measurement(observation):
        return False
    return _base_tree_holds_no_test_file(record)


def _empty_base_arm(command: str, revision: str) -> dict[str, Any]:
    """The base arm of a directly scoped suite whose tests the change creates.

    ``exit_status`` is pytest's exit for a collection that found nothing to run,
    which is what the command would have returned over the base tree: it names
    the measurement as empty rather than as a pass. There are zero failures
    because no test existed that could fail, and no log because no test ran.
    """
    return {
        "revision": revision,
        "command": command,
        "exit_status": 5,
        "completed": True,
        "failure_count": 0,
        "failure_ids": [],
        "log_path": None,
        "log_digest": None,
    }


def _arm_missing_fields(
    record: Mapping[str, Any],
    arm_name: str,
    observation: Mapping[str, Any],
    *,
    head: str = "",
) -> list[str]:
    """Name what an arm lacks to be evidence that it measured a suite.

    An arm is compared with the template resolved over its own tree, which is
    the command it was told to run once per-tree resolution landed. The same
    template over the node's whole declared scope is accepted too, because a
    run dispatched before that rule recorded that spelling and its pair is
    still evidence about the files its tree holds.

    The comparison is by what the command runs rather than by how it is
    spelled: the test files as a set in either order, the interpreter resolved
    to one path whether written relative or absolute, and a leading ``cd`` into
    the arm's own tree allowed. A different test file, flag or interpreter is
    still a different suite.

    The after arm is also held to the revision the run delivers: an arm recorded
    at a revision behind the run's head measured a tree the run no longer ships,
    so ``head`` — read by the caller — is compared with the revision the arm
    records.
    """
    declared = _declared_command_text(record)
    if SCOPED_SUITE_PLACEHOLDER not in declared:
        # An unscoped declaration has no tree-dependent part: both arms run
        # the one command as written.
        missing: list[str] = []
        arm_command = str(observation.get("command") or "").strip()
        if arm_command and not _arm_command_matches(
            arm_command, [declared], _scope_tree(record)
        ):
            missing.append(f"{arm_name}.command_matches_suite_command")
        missing.extend(_arm_collected_nothing(observation, arm_name))
        missing.extend(_arm_revision_mismatch(record, arm_name, observation, head))
        return missing
    if _arm_measured_nothing(record, observation):
        if arm_name == "after_suite":
            return [f"{arm_name}.command_names_no_file"]
        return []
    revision = str(observation.get("revision") or "").strip()
    accepted = [
        _scoped_suite_command(record, revision=revision or None),
        _scoped_suite_command(record),
    ]
    arm_command = str(observation.get("command") or "").strip()
    missing: list[str] = []
    if arm_command and not _arm_command_matches(
        arm_command, accepted, _scope_tree(record)
    ):
        missing.append(f"{arm_name}.command_matches_suite_command")
    missing.extend(_arm_collected_nothing(observation, arm_name))
    missing.extend(_arm_revision_mismatch(record, arm_name, observation, head))
    return missing


def _measured_arm_tree(
    record: Mapping[str, Any],
) -> tuple[Path, Path, str, str] | None:
    """The trees and revisions a measured arm pair runs over, or ``None``.

    The head tree is the run's own worktree — the tree its delivery ships — and
    the base tree is checked out at the run's base revision, so the pair the
    landing measures is the pair the run's own armed command was meant to
    measure. A record that names no worktree, no base revision, or no readable
    repository cannot be measured and the caller keeps the refusal it had.
    """
    worktree = str(record.get("worktree") or "").strip()
    repository = str(record.get("repo") or "").strip()
    base_sha = str(record.get("base_sha") or "").strip()
    tree = Path(worktree) if worktree else Path(repository)
    repo = Path(repository) if repository else tree
    if not (worktree and base_sha and tree.is_dir() and repo.is_dir()):
        return None
    return repo, tree, base_sha, str(worktree)


def _measure_missing_arms(
    run_id: str,
    record: Mapping[str, Any],
    head: str,
    command: str,
    *,
    partition: str = "",
    long_partition: str = "",
) -> dict[str, Any] | None:
    """Run the armed command at both revisions for a run whose arms cannot be used.

    A missing arm, an arm recorded at another revision and an arm naming another
    command all leave the run without the pair its own landing needs, and the
    refusal that used to follow cost the worker a resume over evidence the
    landing can take itself. The reading is the armed command at the base and at
    the head, submitted to the configured measurement partition, and it is
    recorded on the run as measured by the landing rather than replacing the
    run's own manifest.
    """
    from reckon.crew import landing_measure, queue_landing

    trees = _measured_arm_tree(record)
    if trees is None or not head:
        return None
    repo, tree, base_sha, _worktree = trees
    try:
        timeout = queue_landing._measure_timeout(record)
        config = queue_landing._measure_config(record)
        with landing_measure.base_checkout(repo, base_sha, timeout) as base_tree:
            return landing_measure.armed_observations(
                command=command,
                tree=tree,
                base_tree=base_tree,
                base_sha=base_sha,
                head_sha=head,
                logs_dir=run_dir(run_id) / "landing-measure" / "armed",
                test_files=_node_test_files(record),
                timeout=timeout,
                project=str(record.get("project") or ""),
                config=config,
                partition=partition,
                long_partition=long_partition,
            )
    except (OSError, ValueError, landing_measure.BaseCheckoutError):
        return None


def _waivable_arm_field(field: str) -> bool:
    """Whether a missing field is one an arm waiver may cover.

    The waiver is for missing or mismatched arms, so it names fields on an arm
    and nothing else: a missing manifest is not a waiverable arm, and a
    manifest too old to read is a delivery defect the waiver must not paper
    over.
    """
    return field.startswith(("baseline_suite.", "after_suite."))


def _a_held_arm_pair(baseline: Any, after: Any) -> bool:
    """Whether both arms are present to be held as the waiver's evidence.

    A waiver records the arms actually held and the delta they describe, so
    there must be a pair to record: two observations carrying a revision, a
    command and a failure list. Anything less is an absence the waiver cannot
    represent, and the ordinary refusal stands.
    """
    for arm in (baseline, after):
        if not isinstance(arm, Mapping):
            return False
        if not str(arm.get("command") or "").strip():
            return False
        if "failure_ids" not in arm:
            return False
    return True


def _record_arm_waiver(
    run_id: str,
    reason: str,
    waived_fields: Sequence[str],
    held_arms: Mapping[str, Any],
    *,
    dry_run: bool = False,
) -> None:
    """Keep the waiver's reason and the arms it held where a reader can open
    them.

    A waiver that recorded only that it happened would be indistinguishable
    from a run that measured nothing, which is exactly the state the delta
    exists to keep apart from a measured pair.
    """
    if dry_run:
        return
    record = {
        "reason": reason,
        "waived_fields": list(waived_fields),
        "held_arms": {
            name: dict(arm) if isinstance(arm, Mapping) else None
            for name, arm in held_arms.items()
        },
        "waiver_recorded_at": time.time(),
    }

    def amend(current: dict[str, Any]) -> dict[str, Any]:
        current["suite_delta_arm_waiver"] = record
        return current

    _mutate_pointer(run_id, amend)


def _record_measured_arms(run_id: str, head: str, measured: Mapping[str, Any]) -> None:
    """Keep a landing-measured arm pair where a later reader can open it.

    The pair is written beside the run's other landing evidence and onto the
    live pointer, so a promotion, a retry and a retry's coordinator all read one
    record of what the landing measured and at which head.
    """
    record = {"head": head, "measured_at": time.time(), **dict(measured)}

    def write(directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "measured-arms.json").write_text(
            json.dumps(record, indent=2, sort_keys=True), encoding="utf-8"
        )

    with contextlib.suppress(OSError):
        write(run_dir(run_id) / "landing-measure")

    def amend(pointer: dict[str, Any]) -> dict[str, Any]:
        pointer["suite_measured_arms"] = record
        return pointer

    with contextlib.suppress(CrewError, OSError, ValueError):
        _mutate_pointer(run_id, amend)


def _failed_measurement_gate(record: Mapping[str, Any], gate_verdict: str) -> bool:
    """Whether this measurement run's own gate ended in failure.

    Added suite failures are the measured result only when dispatch marked the
    run for measurement and its completion reports a failed gate. The suite
    pair alone cannot answer that: a passing gate can coexist with a failing
    paired suite arm.
    """
    marker = record.get("measure_only")
    measurement_run = isinstance(marker, Mapping) or bool(str(marker or "").strip())
    return measurement_run and str(gate_verdict).strip().lower() == "failed"


def _primary_test_scope(
    record: Mapping[str, Any], revision: str, command: str
) -> tuple[str, list[str], dict[str, Any]]:
    """Exclude named test files absent at the primary revision being measured.

    Keep the scope supplied to the runner consistent with its command: its
    missing-test support must not transplant head-only tests into primary.
    Unknown revision contents leave the command intact so the measurement
    still has to prove it ran, rather than asserting an empty baseline.
    """
    tree = _scope_tree(record)
    words = _command_words(command)
    named: dict[str, str] = {}
    for word in words:
        path = Path(word.partition("::")[0])
        if not is_collectible_test_path(str(path)):
            continue
        if path.is_absolute():
            if tree is None:
                continue
            try:
                path = path.relative_to(tree)
            except ValueError:
                continue
        named[word] = path.as_posix()
    candidates = list(dict.fromkeys(named.values()))
    present = _paths_present_in_tree(tree, revision, candidates)
    absent = [
        path for path in candidates if present is not None and path not in present
    ]
    resolved = _scoped_suite_command(record, revision=revision)
    if absent and SCOPED_SUITE_PLACEHOLDER not in _declared_command_text(record):
        # The measurement runner accepts one argv command with leading
        # assignments. Rebuilding a shell program would change its meaning.
        if any(character in command for character in ";&|<>`$\n"):
            return resolved, [], {**record, "base_sha": revision}
        retained = []
        leading = True
        for word in words:
            if named.get(word) in absent:
                continue
            if leading and _is_environment_assignment(word):
                name, _, value = word.partition("=")
                retained.append(f"{name}={shlex.quote(value)}")
                continue
            leading = False
            target = word
            if word in named:
                _, separator, selector = word.partition("::")
                target = named[word] + separator + selector
            retained.append(shlex.quote(target))
        resolved = (
            " ".join(retained) if any(path not in absent for path in candidates) else ""
        )
    node = dict(record.get("node") or {})
    node["write_paths"] = [
        path for path in _repository_write_paths(record) if path not in absent
    ]
    return resolved, absent, {**record, "base_sha": revision, "node": node}


def _merged_primary_baseline(
    run_id: str,
    record: Mapping[str, Any],
    *,
    head: str,
    command: str,
    dry_run: bool,
    waiver_reason: str = "",
) -> dict[str, Any] | None:
    """Measure the same suite at the primary revision this run actually merged.

    Keep the dispatch baseline intact. This additional observation explains
    inherited failures without claiming they failed before the run started.
    Reuse a receipt only for its immutable revision and command, checking its
    log before trusting its IDs. An interrupted reading excuses no failure.
    """
    import hashlib

    from reckon.crew import promotion_evidence

    primary = promotion_evidence._merged_primary_revision(record, head)
    if not primary:
        return None
    command, absent, primary_record = _primary_test_scope(record, primary, command)
    if not command:
        return None
    key = hashlib.sha256(json.dumps([primary, command]).encode()).hexdigest()
    receipt = run_dir(run_id) / "landing-measure" / f"primary-{key}.json"

    def usable(arm: Mapping[str, Any]) -> bool:
        if ledger.suite_observation_missing_fields(arm, name="primary_suite"):
            return False
        if arm.get("revision") != primary or arm.get("command") != command:
            return False
        try:
            text = Path(str(arm.get("log_path") or "")).read_text(encoding="utf-8")
        except OSError:
            return False
        described = promotion_evidence._text_the_receipt_describes(text)
        return (
            promotion_evidence._log_failure_count(described) is not None
            and not promotion_evidence._receipt_mismatches(
                arm, str(arm["log_path"]), text
            )
            and _recorded_exit_status(text) == arm["exit_status"]
        )

    cached = _read_json_object(receipt)
    if usable(cached):
        return {**cached, "absent_test_files": absent}
    if dry_run:
        return {
            "status": "would-measure",
            "revision": primary,
            "command": command,
            "absent_test_files": absent,
        }
    declaration = gate_declaration(record)
    measured = _measure_missing_arms(
        run_id,
        primary_record,
        head,
        command,
        partition=declaration.get("partition") or "all_debug",
        long_partition=declaration.get("long_partition") or "rigel",
    )
    arm = measured.get("baseline_suite") if isinstance(measured, Mapping) else None
    if not isinstance(arm, Mapping) or not usable(arm):
        if waiver_reason.strip():
            return {
                "status": "waived",
                "revision": primary,
                "command": command,
                "absent_test_files": absent,
                "waiver_reason": waiver_reason.strip(),
                "measurement_refusal": "cannot read completed primary suite evidence",
            }
        raise ledger.SuiteDeltaError(
            f"cannot read completed primary suite evidence at merged revision {primary}",
            missing_fields=["primary_suite"],
        )
    normalized = {
        **ledger.normalized_suite_observation(arm),
        "absent_test_files": absent,
    }
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps(normalized, sort_keys=True), encoding="utf-8")
    return normalized


#: The manifest key prefixes that open a labelled suite pair. A run armed on
#: more than one population records each population's arms apart, so a label's
#: pair is judged on its own rather than one population's baseline being read
#: against another's head.
_LABELLED_SUITE_PREFIXES = ("baseline_suite_", "after_suite_")


def labelled_suite_pairs(
    manifest: Mapping[str, Any] | None,
) -> list[tuple[str, Any, Any]]:
    """Every labelled suite pair a manifest records, ordered by label.

    A label opens a pair when either side is present, so a label carrying only
    one arm is still a pair to judge and the absent side is what the
    completeness check names. The unlabelled ``baseline_suite``/``after_suite``
    fields carry no label and are read by the ordinary path.

    A suffixed key is a label only where its value is a suite record. Manifests
    also carry plain annotations beside their arms under the same prefix — a
    result sentence, a log path, a note — and read as labels each of those is
    a pair with nothing on either side, which refused the landing of a run
    whose unlabelled arms were complete.
    """
    if not isinstance(manifest, Mapping):
        return []
    labels: set[str] = set()
    for key, value in manifest.items():
        if not isinstance(value, Mapping):
            continue
        for prefix in _LABELLED_SUITE_PREFIXES:
            if key.startswith(prefix) and key != prefix:
                labels.add(key[len(prefix) :])
    return [
        (
            label,
            manifest.get(f"baseline_suite_{label}"),
            manifest.get(f"after_suite_{label}"),
        )
        for label in sorted(labels)
    ]


def _evaluate_labelled_suite_delta(
    manifest: Mapping[str, Any],
    *,
    suite_command: str,
) -> dict[str, Any]:
    """Judge a manifest whose suite arms are recorded per label.

    Each label's pair is judged as the unlabelled pair is: both sides must be
    complete evidence, and the after arm must add no failure its own baseline
    does not name. A label missing a side is refused naming that side; a label
    whose after arm adds a failure is refused naming the label and the id, so
    the worker knows which population to repair. A clean set of pairs is a
    clean delta.
    """
    pairs = labelled_suite_pairs(manifest)
    missing: list[str] = []
    normalized: list[dict[str, Any]] = []
    added_by_label: dict[str, list[str]] = {}
    for label, baseline, after in pairs:
        pair_missing = [
            *ledger.suite_observation_missing_fields(
                baseline, name=f"baseline_suite_{label}"
            ),
            *ledger.suite_observation_missing_fields(
                after, name=f"after_suite_{label}"
            ),
        ]
        if pair_missing:
            missing.extend(pair_missing)
            continue
        added = ledger.added_failure_ids(baseline, after)
        if added:
            added_by_label[label] = added
        normalized.append(
            {
                "label": label,
                "baseline_suite": ledger.normalized_suite_observation(baseline),
                "after_suite": ledger.normalized_suite_observation(after),
                "added_failure_ids": added,
            }
        )
    if missing:
        raise ledger.SuiteDeltaError(
            "labelled suite arms require a complete baseline and after pair for "
            "every label; missing " + ", ".join(missing),
            missing_fields=missing,
        )
    if added_by_label:
        detail = "; ".join(
            f"after_suite_{label} names failures its baseline_suite_{label} "
            "does not: " + ", ".join(ids)
            for label, ids in added_by_label.items()
        )
        raise ledger.SuiteDeltaError(
            "armed promotion added suite failures: " + detail,
            added_failure_ids=[
                failure_id for ids in added_by_label.values() for failure_id in ids
            ],
        )
    first = normalized[0]
    return {
        "status": "clean",
        "suite_command": suite_command,
        "baseline_suite": first["baseline_suite"],
        "after_suite": first["after_suite"],
        "suite_pairs": normalized,
        "added_failure_ids": [],
        "failure_attribution": {},
        "inherited_failure_ids": {},
        "primary_suite": None,
        "waiver_reason": None,
        "waiver_covered_fields": [],
        "waived_arms": None,
        "measured_by": "run",
        "baseline_measured_nothing": False,
    }


def _evaluate_suite_delta(
    run_id: str,
    record: Mapping[str, Any],
    *,
    waiver_reason: str,
    gate_verdict: str = "",
    dry_run: bool = False,
) -> dict[str, Any] | None:
    """Validate an armed run's paired suite evidence and calculate its delta.

    An armed run that changed nothing against its base — a fresh manifest
    declaring no changed path and no commit beyond the base, over a worktree
    still sitting on that base — carries a delta of ``unchanged`` and needs no
    baseline and after pair. Every other run with missing suite evidence is
    refused as before, and an arm whose recorded command differs from the
    command the run was armed with measures a different suite, so it is
    refused as missing evidence too.

    The command compared against is the resolved one, and for a scoped
    declaration it is resolved over the tree each arm ran against: a write path
    is not a file that exists, so a test the node deletes leaves the head arm
    and one it creates leaves the base arm. A pair that ran other files
    measured a different suite and is refused. An arm left with no test file to
    run, or one that exited having collected none, is refused as unmeasured on
    the after side; an empty baseline is recorded as an empty baseline, because
    a scope whose tests the change creates has nothing to measure against.
    """
    suite_command = _scoped_suite_command(record)
    recorded_merge = str(record.get("recorded_merge") or "").strip()
    if recorded_merge:
        # The reviewed head is already carried by a merge on the primary
        # branch, landed by hand on the branch itself, so there is no delivery
        # left for a suite pair to compare: measuring one would re-run a suite
        # over a change the branch already carries. The row records the merge
        # that carried it and names the measurement as one it did not take.
        return {
            "status": "not-run",
            "suite_command": suite_command,
            "added_failure_ids": [],
            "recorded_merge": recorded_merge,
            "reason": (
                f"the reviewed head is already carried by merge {recorded_merge} "
                "on the primary branch, so this recording measures no suite delta"
            ),
        }
    if not suite_command:
        return None

    def _record_refusal(refusal: Mapping[str, Any]) -> None:
        # A dry run reaches the same refusal and writes none of it: the pointer
        # is the state a later promotion reads, so a dry run must not move it.
        if dry_run:
            return

        def amend(current: dict[str, Any]) -> dict[str, Any]:
            current["suite_delta_refusal"] = refusal
            return current

        _mutate_pointer(run_id, amend)

    manifest_present, fresh = _manifest_freshness(record)
    manifest: dict[str, Any] = {}
    if manifest_present and fresh:
        try:
            manifest = parse_manifest(
                Path(str(record["manifest_path"])).read_text(encoding="utf-8")
            )
        except (OSError, KeyError, ValueError):
            manifest = {}
    labelled_pairs = labelled_suite_pairs(manifest)
    if labelled_pairs and not (
        isinstance(manifest.get("baseline_suite"), Mapping)
        or isinstance(manifest.get("after_suite"), Mapping)
    ):
        # A manifest that records its arms per label has no unlabelled pair to
        # read; each label's pair is judged on its own so one population's
        # failures are never compared against another population's baseline.
        return _evaluate_labelled_suite_delta(manifest, suite_command=suite_command)
    head = _run_head(record)
    baseline = manifest.get("baseline_suite")
    after = manifest.get("after_suite")
    measured_by_landing = False
    missing: list[str] = []
    if not manifest_present:
        missing.append("manifest")
    elif not fresh:
        missing.append("fresh_manifest")
    # A scope whose tests the change creates has nothing to measure against: the
    # base tree holds none of the node's test files, so the base arm measured an
    # empty suite. That is a complete measurement of nothing rather than an
    # absent one, and an arm declaring it as ``none`` is accepted once the base
    # tree confirms it. It is recorded as pytest's own exit for a collection
    # that found nothing to run, with no log, because no test ran and no result
    # beyond that is being claimed.
    base_sha = str(record.get("base_sha") or "").strip()
    if _base_arm_measured_an_empty_suite(record, baseline):
        baseline = _empty_base_arm(suite_command, base_sha)
    else:
        missing.extend(
            ledger.suite_observation_missing_fields(baseline, name="baseline_suite")
        )
    missing.extend(ledger.suite_observation_missing_fields(after, name="after_suite"))
    for arm_name, observation in (
        ("baseline_suite", baseline),
        ("after_suite", after),
    ):
        if not isinstance(observation, Mapping):
            continue
        for field in _arm_missing_fields(record, arm_name, observation, head=head):
            if field not in missing:
                missing.append(field)
    if (
        isinstance(baseline, Mapping)
        and str(baseline.get("revision") or "").strip()
        and str(baseline["revision"]).strip() != base_sha
    ):
        missing.append("baseline_suite.revision_matches_base_sha")
    waived_arm_fields: list[str] = []
    waiver = str(waiver_reason).strip()
    if missing and waiver and not _run_changed_nothing(record):
        # A waiver for missing or mismatched arms is read before the
        # missing-fields refusal: the run holds a pair, it just does not meet
        # the comparison, and the coordinator has stated why. What it never
        # waives is an added failure, which is judged from the pair below.
        waived_arm_fields = [field for field in missing if _waivable_arm_field(field)]
        uncovered = [field for field in missing if field not in waived_arm_fields]
        if waived_arm_fields and not uncovered and _a_held_arm_pair(baseline, after):
            missing = []
            _record_arm_waiver(
                run_id,
                waiver,
                waived_arm_fields,
                {"baseline_suite": baseline, "after_suite": after},
                dry_run=dry_run,
            )
    if missing:
        if _run_changed_nothing(record):
            # An armed run that changed nothing has nothing for a suite pair to
            # measure: no baseline and after suite can differ over an empty
            # delta, so requiring the pair would refuse a run whose own tree
            # proves it is unchanged. The refusal below still fires for a run
            # that moved its head or declared a changed path.
            return {
                "status": "unchanged",
                "suite_command": suite_command,
                "added_failure_ids": [],
                "reason": (
                    "run changed nothing against its base: its fresh manifest "
                    "declares no changed path and no commit beyond the base, and "
                    "its worktree sits on the base with no tracked modification, "
                    "so no baseline and after suite pair is required"
                ),
            }
        if dry_run:
            # A dry run promises to write nothing and start nothing, and the
            # landing's own measurement runs the armed command over two trees
            # and can submit a scheduler job to do it. A run the landing can
            # measure is reported as one it would measure, every field still
            # unmeasured; a record naming no worktree, base or readable
            # repository keeps the refusal below, because that is what a real
            # landing falls back to when there is nothing to measure over.
            if _measured_arm_tree(record) is not None and head:
                return {
                    "status": "would-measure",
                    "suite_command": suite_command,
                    "missing_fields": missing,
                    "added_failure_ids": [],
                    "reason": (
                        "the run recorded no complete baseline and after suite "
                        "pair, so the landing would run the armed command over "
                        "the base and head trees itself; a dry run measures "
                        "nothing and starts nothing"
                    ),
                }
            measured_arms = None
        else:
            # A pair whose commands differ from the one the run was armed with
            # is measured here like a missing pair: the landing runs the armed
            # command at the base and at the head itself, so a worker that
            # gated on its own focused tests does not cost its coordinator a
            # waiver, and the comparison the gate makes is unchanged.
            declaration = gate_declaration(record)
            measured_arms = _measure_missing_arms(
                run_id,
                record,
                head,
                suite_command,
                partition=declaration.get("partition", ""),
                long_partition=declaration.get("long_partition", ""),
            )
        if measured_arms and not measured_arms.get("unreadable"):
            _record_measured_arms(run_id, head, measured_arms)
            baseline = measured_arms["baseline_suite"]
            after = measured_arms["after_suite"]
            measured_by_landing = True
            missing = []
    if missing:
        refusal = {
            "status": "refused",
            "suite_command": suite_command,
            "missing_fields": missing,
            "added_failure_ids": [],
            "held_arms": {
                "baseline_suite": baseline if isinstance(baseline, Mapping) else None,
                "after_suite": after if isinstance(after, Mapping) else None,
            },
        }
        uncounted = [
            name
            for name, observation in (
                ("baseline_suite", baseline),
                ("after_suite", after),
            )
            if ledger.suite_observation_is_uncounted(observation)
        ]
        detail = (
            "armed promotion requires complete baseline and after suite evidence; "
            "missing " + ", ".join(missing)
        )
        if uncounted:
            refusal["uncounted_arms"] = uncounted
            detail += (
                ". " + " and ".join(uncounted) + " never reached a pytest summary, "
                "so no failure count exists to compare: re-measure the arm, or "
                "state why it cannot count with --waive-suite-delta '<why>', "
                "which records the arm as unmeasured rather than as a passing one"
            )
        if any(field.endswith(".command_matches_suite_command") for field in missing):
            detail += (
                ". An arm that ran a different command states that command and "
                "the partition it ran on with `reckon crew declare-gate --run "
                f"{run_id} --command '<the command the arms ran>' --partition "
                "<the partition>`, and re-promote; a pair that cannot be "
                "re-measured is waived with --waive-suite-delta '<why>', which "
                "never waives an added failure"
            )
        if "after_suite.revision_matches_head" in missing and isinstance(
            after, Mapping
        ):
            recorded = str(after.get("revision") or "").strip()
            head_command = _scoped_suite_command(
                record, revision=head
            ) or _scoped_suite_command(record)
            refusal["after_suite_recorded_revision"] = recorded
            refusal["run_head"] = head
            if head_command:
                refusal["head_suite_command"] = head_command
            detail += (
                f". The after arm was recorded at {recorded}, which is "
                f"not the run's head {head}: re-run the armed command at that "
                "head and record the after arm at the head's revision"
            )
            if head_command:
                detail += f": {head_command}"
        _record_refusal(refusal)
        raise ledger.SuiteDeltaError(
            detail,
            missing_fields=missing,
        )
    normalized_baseline = ledger.normalized_suite_observation(
        baseline, unmeasured_reason=waiver
    )
    normalized_after = ledger.normalized_suite_observation(
        after, unmeasured_reason=waiver
    )
    # What an arm waiver held, in the normalised shape the row records. The
    # committed row is the durable copy: promotion deletes the pointer this
    # waiver was written to.
    waived_arms = (
        {
            "reason": waiver,
            "waived_fields": list(waived_arm_fields),
            "baseline_suite": normalized_baseline,
            "after_suite": normalized_after,
        }
        if waived_arm_fields
        else None
    )
    # "New" is defined exactly here: the single set-difference lives in
    # ledger.added_failure_ids, so the refusal and the per-failure attribution
    # share one notion of a newly added failure rather than a second expression
    # that could drift from it.
    added = ledger.added_failure_ids(normalized_baseline, normalized_after)
    primary_suite = None
    inherited: dict[str, str] = {}
    effective_baseline = normalized_baseline
    if added:
        primary_suite = _merged_primary_baseline(
            run_id,
            record,
            head=head,
            command=suite_command,
            dry_run=dry_run,
            waiver_reason=waiver,
        )
        if primary_suite and primary_suite.get("status") == "would-measure":
            return {
                "status": "would-measure",
                "suite_command": suite_command,
                "baseline_suite": normalized_baseline,
                "after_suite": normalized_after,
                "primary_suite": primary_suite,
                "added_failure_ids": added,
            }
        if primary_suite and primary_suite.get("status") != "waived":
            uncovered = ledger.added_failure_ids(primary_suite, {"failure_ids": added})
            inherited = {
                failure_id: primary_suite["revision"]
                for failure_id in added
                if failure_id not in uncovered
            }
            effective_baseline = {
                **normalized_baseline,
                "failure_ids": [*normalized_baseline["failure_ids"], *inherited],
            }
            added = ledger.added_failure_ids(effective_baseline, normalized_after)
    failed_measurement = _failed_measurement_gate(record, gate_verdict)
    # Surface the worker's attribution on the stored record: each added
    # failure id beside the candidate merged commit that introduced it.
    # An entry naming a pre-existing failure is dropped rather than stored.
    attribution = ledger.new_failure_attribution(
        effective_baseline, after, manifest.get("failure_attribution")
    )
    if str(record.get("role") or "").strip() == "test":
        unattributed = sorted(set(added) - set(attribution))
        if unattributed:
            missing_attribution = [
                f"failure_attribution[{failure_id}]" for failure_id in unattributed
            ]
            refusal = {
                "status": "refused",
                "suite_command": suite_command,
                "baseline_suite": normalized_baseline,
                "after_suite": normalized_after,
                "missing_fields": missing_attribution,
                "added_failure_ids": added,
                "failure_attribution": attribution,
                "inherited_failure_ids": inherited,
                "primary_suite": primary_suite,
            }
            _record_refusal(refusal)
            raise ledger.SuiteDeltaError(
                "test-role promotion requires a candidate commit for every "
                "added suite failure; missing " + ", ".join(missing_attribution),
                missing_fields=missing_attribution,
                added_failure_ids=added,
            )
    waiver = str(waiver_reason).strip()
    # A waiver that covered a missing or mismatched arm says nothing about a
    # failure the held arms add: it was given for the evidence the run could
    # not produce, not for a regression the pair reports. Waiving that would be
    # the one thing an arm waiver promises never to do, so an added failure is
    # refused even when the arm waiver was accepted. A waiver given for the
    # failure itself — no arm field covered — keeps its existing meaning.
    if added and not failed_measurement and (not waiver or waived_arm_fields):
        refusal = {
            "status": "refused",
            "suite_command": suite_command,
            "baseline_suite": normalized_baseline,
            "after_suite": normalized_after,
            "missing_fields": [],
            "added_failure_ids": added,
            "failure_attribution": attribution,
            "inherited_failure_ids": inherited,
            "primary_suite": primary_suite,
        }
        _record_refusal(refusal)
        detail = "armed promotion added suite failures: " + ", ".join(added)
        if waived_arm_fields:
            detail += (
                "; an arm waiver never waives an added failure, so repair the "
                "failure or record the arm the pair was actually run with"
            )
        raise ledger.SuiteDeltaError(detail, added_failure_ids=added)
    accepted_waiver_reason = waiver
    if added and failed_measurement and not accepted_waiver_reason:
        accepted_waiver_reason = (
            "the failed gate's added suite failures are the measured result "
            "for this measurement run"
        )
    return {
        # A pair waived for its arms is waived even when the pair adds no
        # failure: the status names how the evidence was accepted, not only
        # what it found.
        "status": "waived" if (added or waived_arm_fields) else "clean",
        "suite_command": suite_command,
        "baseline_suite": normalized_baseline,
        "after_suite": normalized_after,
        "added_failure_ids": added,
        "failure_attribution": attribution,
        "inherited_failure_ids": inherited,
        "primary_suite": primary_suite,
        "waiver_reason": accepted_waiver_reason or None,
        # Which arm fields a waiver covered. Empty for a measured pair, so a
        # reader can tell an arm set that was waived from one that matched.
        "waiver_covered_fields": waived_arm_fields,
        # The waived pair and the reason it was waived, or None for a pair
        # that needed no waiver.
        "waived_arms": waived_arms,
        # Whether these arms are the landing's own measurement rather than the
        # ones the run recorded. A reader comparing this pair with the run's
        # manifest needs to know which of the two it is looking at.
        "measured_by": "landing" if measured_by_landing else "run",
        # Whether the base tree held none of this scope's test files. The
        # delta is then measured against the empty set, which is the honest
        # baseline for a change that creates the tests it is measured by, and
        # the reader can tell it from a base that ran tests and found none
        # failing.
        "baseline_measured_nothing": isinstance(baseline, Mapping)
        and _arm_measured_nothing(record, baseline),
    }


def _release_terminal_manifest(record: Mapping[str, Any]) -> bool:
    """Return whether a terminal manifest was delivered for this attempt.

    A live process with no manifest at all is a recovery case, not a cleanup
    one, so signalling it here would race whatever is meant to observe it.
    """
    manifest_present, fresh = _manifest_freshness(record)
    if not manifest_present or not fresh:
        return False
    try:
        parsed = parse_manifest(
            Path(str(record["manifest_path"])).read_text(encoding="utf-8")
        )
    except (OSError, KeyError, ValueError):
        return False
    return str(parsed.get("status") or "").strip().lower() in {
        "complete",
        "blocked",
        "failed",
    }


def _resume_worktree_retention(
    record: Mapping[str, Any],
    recoverable_session: Mapping[str, str] | None,
    *,
    retained_at: str,
    discard: bool,
    gate: str,
) -> dict[str, str] | None:
    """Describe a worktree deliberately kept as a session's working directory.

    A completed run has closed its work and can release an integrated tree even
    when its session is still resolvable. Retention is for a promotion that did
    not close the work, or for a manifest that is not complete.
    """
    worktree = str(record.get("worktree") or "").strip()
    if recoverable_session is None or discard or not worktree:
        return None
    manifest = _fresh_manifest(record)
    complete_manifest = (
        manifest is not None
        and str(manifest.get("status") or "").strip().lower() == "complete"
    )
    if str(gate).strip().lower() not in {"blocked", "failed"} and complete_manifest:
        return None
    if not Path(worktree).is_dir():
        return None
    return {
        "classification": "retained-for-resume",
        "worktree": str(Path(worktree).resolve()),
        "session_id": str(recoverable_session["session_id"]),
        "session_source": str(recoverable_session["source"]),
        "retained_at": retained_at,
    }


def _worktree_audit(
    record: Mapping[str, Any],
    retention: Mapping[str, str] | None,
) -> dict[str, Any]:
    """Audit the promoted run's worktree, preserving retention as its own state.

    Artifact safety and session recoverability answer different questions. A
    clean reachable tree is normally reclaimable, and an unintegrated tree is
    normally withheld for its commit. When the tree is a recoverable session's
    working directory, neither label states why it is present, so the audit
    carries the artifact classification separately and presents the retention
    as the operative classification.
    """
    repo_value = str(record.get("repo") or "").strip()
    worktree_value = str(record.get("worktree") or "").strip()
    if not repo_value or not Path(repo_value).is_dir() or not worktree_value:
        return {"counts": {}, "worktrees": []}
    repo = Path(repo_value).resolve()
    worktree = Path(worktree_value).resolve()
    claims = _live_worktree_claims().get(worktree, ())
    # Both reads below charge a change only to the paths the run declared, so
    # the status walk stays inside the work it answered for. When the run
    # declared no root at all nothing can be charged to it, and a bounded read
    # would report every tree clean — so that one case keeps the whole-tree
    # read rather than hiding uncommitted work behind an empty root set.
    from reckon.crew.promotion_evidence import _declared_repository_roots

    declared = _declared_repository_roots(record, tree=worktree) or None
    snapshot = _repository_tree_snapshot(
        repo, roots=(worktree,), untracked_roots=declared
    )
    retained_path = (
        Path(str(retention["worktree"])).resolve() if retention is not None else None
    )
    rows: list[dict[str, Any]] = []
    for tree_state in snapshot.get("trees") or ():
        if not tree_state.get("available"):
            rows.append(dict(tree_state))
            continue
        path = Path(str(tree_state["path"])).resolve()
        if path == repo:
            continue
        shadow_record = record if path == retained_path and _is_shadow(record) else None
        inspected = _inspect_workspace(
            repo,
            path,
            "HEAD",
            claims,
            shadow_record,
            untracked_roots=declared,
        )
        row = {**tree_state, **inspected}
        if retention is not None and path == retained_path:
            row["artifact_classification"] = row["classification"]
            row["classification"] = "retained-for-resume"
            row["retention"] = dict(retention)
            row["reclaimable"] = False
            row["withheld"] = (
                "this worktree is the working directory of recoverable session "
                f"{retention['session_id']}"
            )
        else:
            classification = str(row["classification"])
            row["reclaimable"] = classification in RECLAIMABLE_CLASSES
            if not row["reclaimable"]:
                row["withheld"] = WITHHELD_REASONS.get(
                    classification, "unrecognised classification"
                )
        rows.append(row)
    names = {
        "integrated",
        "disposable",
        "dirty",
        "unintegrated",
        "live-referenced",
        "retained-for-resume",
    }
    return {
        "counts": {
            name: sum(row.get("classification") == name for row in rows)
            for name in sorted(names)
        },
        "worktrees": rows,
    }


def _release_scratch_when_release_raised(record: Mapping[str, Any]) -> dict[str, Any]:
    """The scratch outcome when the rest of a release step raised.

    Scratch removal does not depend on the worktree, the process or the
    repository, so it is attempted even when the surrounding release raised:
    leaving the directory to leak because a git command failed is the very
    outcome this release step exists to prevent. A release that raises must
    still tell a reader, because a result carrying no scratch field cannot
    distinguish a directory that was removed from one that was left behind.
    The fallback never raises: it reports the reason instead.
    """
    try:
        return remove_worker_scratch(
            str(record.get("run_id") or ""),
            recorded_path=record.get("scratch"),
            budget_bytes=WORKER_SCRATCH_BUDGET_BYTES,
        )
    except Exception as exc:  # noqa: BLE001 - the fallback must itself never raise
        return {
            "scratch_removed": False,
            "scratch_path": str(record.get("scratch") or "") or None,
            "scratch_withheld": f"scratch removal raised in the release fallback: {exc}",
            "scratch_bytes": None,
        }


def _manifest_reads_blocked(record: Mapping[str, Any]) -> bool:
    """Whether the run's manifest is a blocked delivery kept for resume.

    A blocked run stopped without finishing, so a resume may still continue the
    work. Its process group and its per-run identity are therefore part of what
    a resume finds, and the release keeps both rather than reclaiming them. The
    status is read from the delivered manifest, which is the same source the
    release already consults to decide whether a writer may be signalled.
    """
    manifest = _fresh_manifest(record)
    return (
        manifest is not None
        and str(manifest.get("status") or "").strip().lower() == "blocked"
    )


def _retire_disposable_identity(record: Mapping[str, Any]) -> dict[str, Any]:
    """Remove a run's own disposable roster identity once its work is accepted.

    A dispatch that names no member carries a per-run identity and registers no
    roster row, so the ordinary run retires nothing here: there is no row to
    remove. The row is removed when one exists — a run hand-registered, or
    dispatched by an earlier revision — so acceptance leaves no disposable
    identity standing in the committed roster, which is the same guarantee the
    dispatch side gives by registering none.

    Only the run's own disposable identity is eligible. A run that names a
    member explicitly carries no disposable identity, so the release reports
    nothing about one and adds no key a reader could mistake for a retirement
    that was considered. A blocked run is kept: it may still be resumed, and its
    identity is part of what a resume finds.

    The removal runs after the release receipt is committed and changes only
    the run store's roster, leaving promoted history intact.
    """
    run_id = str(record.get("run_id") or "")
    member_id = str(record.get("member") or "").strip()
    if not member_id or member_id != _disposable_member_id(run_id):
        return {}
    if _manifest_reads_blocked(record):
        return {
            "identity_retired": False,
            "identity_withheld": "manifest is blocked; identity kept for resume",
        }
    project = str(record.get("project") or "").strip()
    root = str(record.get("repo") or "").strip() or None
    if not project:
        return {
            "identity_retired": False,
            "identity_withheld": "the run names no project to retire the identity from",
        }
    try:
        for _attempt in range(8):
            data, version = ledger.load_auxiliary(project, root)
            if not any(str(entry.get("id")) == member_id for entry in data["members"]):
                return {
                    "identity_retired": False,
                    "identity_absent": True,
                    "identity_absent_member": member_id,
                }
            data["members"] = [
                entry for entry in data["members"] if str(entry.get("id")) != member_id
            ]
            try:
                ledger.write_members(
                    project,
                    data["members"],
                    version,
                    root=root,
                    allow_member_removal=True,
                )
            except ledger.LedgerError:
                continue
            return {"identity_retired": True, "identity_member": member_id}
    except Exception as exc:  # noqa: BLE001 - cleanup must never mask promotion
        return {
            "identity_retired": False,
            "identity_withheld": f"identity retirement raised: {exc}",
        }
    return {
        "identity_retired": False,
        "identity_withheld": "could not retire the disposable identity after retries",
    }


from reckon.crew.promotion_checks import (
    _fresh_manifest,
)
from reckon.crew.promotion_evidence import (
    _ATTEMPT_RECORD_NAME,
    _WORKER_RECORD_NAME,
    _recorded_exit_status,
    _run_changed_nothing,
)
from reckon.crew.promotion_release import (
    _release_after_promotion,
)
from reckon.crew.promotion_scope import (
    _changed_paths_inside_repository,
    _is_shadow,
)
from reckon.crew.reports import (
    _gate_command_prose,
    _test_log_command,
)
