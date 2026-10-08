from __future__ import annotations

# Imports below the definitions resolve sibling cycles after names are bound.
# ruff: noqa: E402
import os
import re
import shlex
import shutil
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from reckon import (
    ledger,
)
from reckon.crew.node import (
    CrewError,
)
from reckon.crew.routing import (
    _git,
)
from reckon.crew.runs import (
    drain,  # noqa: F401 - importable so a caller can substitute the fleet reading's drain
    run_dir,
)

_PRESERVED_GATE_LOG_NAME = "gate.log"


_REPLAY_GATE_LOG_NAME = "verify-gate.log"


_REPLAY_BOUND_DEFAULT_SECONDS = 300.0


_REPLAY_BOUND_HEADROOM = 2.0


_REPLAY_BOUND_CEILING_SECONDS = 1800.0


_GATE_LOG_DURATION = re.compile(r"\bin (\d+(?:\.\d+)?)s\b")


def _replay_log_header(
    *,
    replay_command: str,
    checkout: Path,
    checkout_revision: str,
    integrated_revision: str,
    rewritten_roots: Sequence[str] = (),
) -> list[str]:
    """The header lines a re-run log opens with, in the gate log's own shape.

    The header is what makes the log self-describing for a reader who has only
    the file: which revision was measured, in which tree, and the exact text
    the shell was handed — which differs from the stored command whenever a
    recorded worktree root was rewritten.
    """
    lines = [
        (
            f"# replayed revision: {checkout_revision or 'unknown'} "
            f"(integrated {integrated_revision or 'unknown'})"
        ),
        f"# cwd of the replay: {checkout}",
        f"# command: {replay_command}",
    ]
    lines.extend(
        f"# worktree root rewritten: {root} -> {checkout}" for root in rewritten_roots
    )
    return lines


def _write_replay_log(
    log_path: str | Path | None,
    *,
    header: Sequence[str],
    output: str,
    exit_status: int | None,
    cut_short: str | None = None,
) -> Path | None:
    """Write a re-run's captured output under the run directory, or None.

    The text lands in the capture convention the fleet's gate logs use — header
    lines, the command's own output, and a terminal ``EXIT=<n>`` record — so
    the gate-log readers parse a re-run exactly as they parse the log it was
    replayed from. A re-run killed by the bound has no status to record, so no
    ``EXIT=`` line is written and the absence is the fact; the ``cut_short``
    statement says outright that it was stopped, so the log's three bare header
    lines are not left to read as a gate that simply printed nothing.

    Best-effort, like the cited-log preservation beside it: a log that cannot
    be written leaves the verdict untouched and reports no path.
    """
    if log_path is None:
        return None
    path = Path(log_path).expanduser()
    parts = [str(line) for line in header]
    body = str(output or "").rstrip("\n")
    if body.strip():
        parts.append(body)
    if cut_short:
        parts.append(f"# cut short: {cut_short}")
    if exit_status is not None:
        parts.append(f"EXIT={exit_status}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(parts) + "\n", encoding="utf-8")
    except OSError:
        return None
    return path


def _preserve_cited_gate_log(
    run_id: str,
    gate_check: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Copy a cited gate log into the run directory so the row cites a durable path.

    ``crew complete --gate-log-path`` records where a log *lies*, and a worker's
    log routinely lies somewhere that will not survive: ``/tmp`` is reaped, and a
    worktree's gitignored output directory goes with the worktree the promotion
    releases. The ledger row then cites a path that resolves to nothing — every
    check a reader runs on the row passes, and the evidence is gone.
    Compensating by hand does not scale: two coordinators independently copied 27
    gate logs into a reports directory before citing them.

    The run directory outlives the worktree and is pruned only by ``crew gc`` on
    a retention window, so a copy placed there is the durable form of the cited
    log. A cited log already inside the run directory is therefore returned
    unchanged — nothing needs copying, and a second copy would only duplicate it.

    A check that cites a digest with no log path has nothing to copy, so it is
    returned as given rather than dropped: the digest is the citation, and only
    the path is ever rewritten here.

    Preservation is best-effort and changes no verdict. Promotion may run from a
    machine the worker's log never reached, so a cited path that does not resolve
    has no text to contradict the verdict and must not turn a promotion into a
    refusal; a copy that cannot be written leaves the citation exactly as given,
    for the same reason. The gate verdict and the exit status are the caller's to
    decide, and this step reads neither.
    """
    if not isinstance(gate_check, Mapping):
        return None
    raw = str(gate_check.get("log_path") or "").strip()
    if not raw:
        return dict(gate_check)
    source = Path(raw).expanduser()
    if not source.is_file():
        return dict(gate_check)
    directory = run_dir(run_id)
    try:
        inside = source.resolve().is_relative_to(directory.resolve())
    except (OSError, RuntimeError, ValueError):
        inside = False
    if inside:
        return dict(gate_check)
    destination = directory / _PRESERVED_GATE_LOG_NAME
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    except OSError:
        return dict(gate_check)
    return {**dict(gate_check), "log_path": str(destination)}


_GATE_COMMAND_PLACEHOLDER = re.compile(r"<[^\s<>][^<>]*[^\s<>]>")


_GATE_COMMAND_ELLIPSIS = re.compile(r"(?:^|(?<=\s))\.\.\.(?=\s|$)|…")


_GATE_COMMAND_PARENTHETICAL = re.compile(r"(?:^|(?<=\s))\([^()]*\)(?=\s|$)")


_GATE_COMMAND_SHELL_OPERATORS = frozenset("&|;$><=*?!`\"'")


def gate_command_prose(command: str) -> tuple[str, str] | None:
    """The shape in a gate command that a shell cannot execute, or None.

    Returns the offending token class beside the token itself, so a refusal can
    name both. The classes are the three a description is written in: an
    angle-bracket placeholder, an ellipsis standing for the rest of a list, and
    a parenthetical selection written as prose. Anything else is admitted: this
    judges only the shapes that cannot run, never the spelling of a command
    that can, because a false refusal costs a coordinator a promotion.
    """
    text = str(command or "").strip()
    if not text:
        return None
    found = _GATE_COMMAND_PLACEHOLDER.search(text)
    if found is not None:
        return "angle-bracket placeholder", found.group(0)
    found = _GATE_COMMAND_ELLIPSIS.search(text)
    if found is not None:
        return "ellipsis", found.group(0).strip()
    for found in _GATE_COMMAND_PARENTHETICAL.finditer(text):
        group = found.group(0)
        inner = group[1:-1]
        if len(inner.split()) < 2:
            continue
        if any(character in inner for character in _GATE_COMMAND_SHELL_OPERATORS):
            continue
        return "parenthetical prose selection", group
    return None


def _require_runnable_gate_command(
    run_id: str,
    gate_check: Mapping[str, Any] | None,
) -> None:
    """Refuse a promotion that records a gate command nothing can re-execute.

    The command a promotion records is not narrative: the integration re-run
    executes it verbatim through a shell, whatever the verdict on the row, so a
    text that describes the check rather than running it makes the re-run
    report an invented failure. Refused here, before any store is written, so
    the record never holds a command that reads as evidence and executes as
    gibberish. A readable summary of the check belongs in the log header or in
    the outcome line, both of which are free text.
    """
    if not isinstance(gate_check, Mapping):
        return
    command = str(gate_check.get("command") or "").strip()
    prose = gate_command_prose(command)
    if prose is None:
        return
    token_class, token = prose
    raise CrewError(
        f"run {run_id!r} records the gate command {command!r}, which carries a "
        f"{token_class} ({token!r}): that describes the check rather than "
        "running it, so the integration re-run would execute the description, "
        "exit non-zero and record a failure the merge did not cause. Record the "
        "command itself — the literal file list, not a description of it — and "
        "put the readable summary in the gate log header or in --outcome"
    )


_SHELL_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _require_executable_gate_command(
    run_id: str,
    gate_check: Mapping[str, Any] | None,
) -> None:
    """Refuse a recorded gate command whose first token names no executable.

    The recorded command is what the integration re-run executes, so it has to
    name a program that can start. The command is tokenised the way a shell
    would tokenise it, leading ``NAME=value`` assignments are skipped, and the
    first remaining token either carries a path separator — and must then be an
    existing executable file — or is looked up on ``PATH``. A command written
    as prose can satisfy every shape check that refuses placeholders, ellipses
    and prose parentheticals and still name no program at all, so the token
    itself is resolved here and a promotion recording such a command is refused
    before the row is written.

    Refusal reaches only a command whose program cannot be resolved from here:
    the lookup is the same ``PATH`` the promotion process runs under, and a
    command that resolves still promotes on whatever the other gate checks
    make of its evidence.
    """
    if not isinstance(gate_check, Mapping):
        return
    command = str(gate_check.get("command") or "").strip()
    if not command:
        return
    try:
        argv = shlex.split(command)
    except ValueError as unparseable:
        raise CrewError(
            f"run {run_id!r} records the gate command {command!r}, which does "
            f"not parse as a shell command ({unparseable}): the integration "
            "re-run would execute the text a shell cannot tokenise. Found: a "
            "command that does not parse. Record the literal command that ran, "
            "quoting its arguments, and put any readable summary in the gate "
            "log header or in --outcome"
        ) from unparseable
    while argv and _SHELL_ASSIGNMENT.match(argv[0]):
        argv = argv[1:]
    if not argv:
        raise CrewError(
            f"run {run_id!r} records the gate command {command!r}, which names "
            "no program: it carries only shell variable assignments. Found: a "
            "command whose first token names no executable. Record the literal "
            "command that ran, and put any readable summary in the gate log "
            "header or in --outcome"
        )
    program = argv[0]
    if "/" in program:
        candidate = Path(program).expanduser()
        try:
            runnable = candidate.is_file() and os.access(candidate, os.X_OK)
        except OSError:
            runnable = False
    else:
        runnable = shutil.which(program) is not None
    if runnable:
        return
    raise CrewError(
        f"run {run_id!r} records the gate command {command!r}, whose program "
        f"{program!r} names no executable — it is neither an existing "
        "executable file nor a command found on PATH, so the integration "
        "re-run would fail before running the check. Found: a first token that "
        "names no executable. Record the literal command that ran — the real "
        "program the check was started with — and put any readable summary in "
        "the gate log header or in --outcome"
    )


def _paths_differing_between(
    repository: Path,
    integrated: str,
    checkout: str,
    changed_paths: Sequence[str] | None,
) -> list[str] | None:
    """Return the run's changed paths whose content differs between two revisions.

    ``None`` reports the paths as unknown, from either route: a caller that
    states no paths cannot establish that the commits after the merge leave
    that work untouched, and a comparison that cannot be taken reports the same
    unknown rather than an empty result, because a failed probe establishes
    nothing. An empty list means the comparison ran and every stated path is
    identical between the two revisions, the condition under which a checkout
    past the merge may still be measured.
    """
    if changed_paths is None:
        return None
    paths = [str(path) for path in changed_paths if str(path).strip()]
    if not paths:
        return []
    probe = _git(
        repository,
        "diff",
        "--name-only",
        integrated,
        checkout,
        "--",
        *paths,
        check=False,
    )
    if probe.returncode:
        return None
    return [line.strip() for line in probe.stdout.splitlines() if line.strip()]


def _cited_changed_paths(repository: Path, commits: Any) -> tuple[str, ...] | None:
    """The repository paths a run's own cited commits changed, or None.

    Read from each cited commit's own diff against its first parent, in the
    checkout the re-run will measure, so a merge charges the paths it resolved
    rather than the branch's whole span. A citation that does not resolve
    leaves the paths unknown — the caller then refuses a checkout past the
    integrated revision rather than guessing which paths are safe to ignore.
    """
    revisions = [str(commit) for commit in (commits or ()) if str(commit).strip()]
    if not revisions:
        return None
    paths: list[str] = []
    seen: set[str] = set()
    for revision in revisions:
        probe = _git(
            repository, "diff", "--name-only", f"{revision}^", revision, check=False
        )
        if probe.returncode:
            return None
        for line in probe.stdout.splitlines():
            path = line.strip()
            if path and path not in seen:
                seen.add(path)
                paths.append(path)
    return tuple(paths)


def _merged_gate_finding(
    base_verdict: str,
    integrated_verdict: str,
    *,
    integrated_revision: str,
    exit_status: int | None,
    reason: str | None,
    failure_ids: Sequence[str] = (),
    timed_out: bool = False,
    replay_elapsed_seconds: float | None = None,
) -> dict[str, Any] | None:
    """The divergence a merge-time re-run exists to surface, or None.

    A finding exists exactly when a gate that passed at the worker's base is
    not passed by the tree that ships. A base that was not already green is
    never a finding — the merge cannot turn a red gate red — so the check
    cannot manufacture a merge finding where the worker's own gate was
    already failing. Anything other than ``passed`` at the integrated tree
    (failed, or a re-run that could not establish a pass) is surfaced, because
    a base-green gate the merged tree does not re-confirm is the silent gap
    this mechanism exists to close.

    ``failure_ids`` are the failing tests the replay's own log enumerated. They
    are carried beside the verdict when the log named any, so the coordinator
    the finding reaches can act on the divergence without reopening the log;
    an empty sequence writes no key, because a re-run whose output named no
    test id measured no id rather than a zero.

    A re-run stopped by its bound carries ``timed_out`` and the seconds it ran
    before it was stopped, so the finding states the timeout and its duration
    in fields rather than only inside the reason sentence.
    """
    if base_verdict != "passed" or integrated_verdict == "passed":
        return None
    finding: dict[str, Any] = {
        "base_verdict": "passed",
        "integrated_verdict": integrated_verdict,
        "integrated_revision": integrated_revision,
        "exit_status": exit_status,
        "reason": reason,
        "message": (
            f"the gate passed at the worker's base but reports "
            f"{integrated_verdict} on the integrated revision "
            f"{integrated_revision[:12]}: the merge changes what this node's "
            "gate proves, so a base-green run alone is not enough to push. "
            "Re-run the node's gate on the merged tree, land the code the "
            "merged tree requires, or record explicitly why this finding is "
            "accepted"
        ),
    }
    if failure_ids:
        finding["failure_ids"] = list(failure_ids)
    if timed_out:
        finding["timed_out"] = True
        if replay_elapsed_seconds is not None:
            finding["replay_elapsed_seconds"] = round(replay_elapsed_seconds, 2)
    return finding


def _recorded_worktree_roots(row: Mapping[str, Any]) -> tuple[str, ...]:
    """The worker worktree roots a promoted row records, in the order written.

    Promotion releases the run's worktree but keeps the audit of what it
    released, so ``release.worktree_audit.worktrees[].path`` is the durable
    record of the directory a stored gate command was authored against. A row
    written before the audit existed carries no path at all, and a top-level
    ``worktree`` is read too so either shape reaches the rewrite.
    """
    roots: list[str] = []

    def add(value: Any) -> None:
        text = str(value or "").strip()
        if text and text not in roots:
            roots.append(text)

    release = row.get("release")
    audit = release.get("worktree_audit") if isinstance(release, Mapping) else None
    if isinstance(audit, Mapping):
        for entry in audit.get("worktrees") or ():
            if isinstance(entry, Mapping):
                add(entry.get("path"))
    add(row.get("worktree"))
    return tuple(roots)


def _rewrite_worktree_roots(
    command: str,
    *,
    roots: Sequence[str | Path],
    checkout: Path,
) -> tuple[str, tuple[str, ...]]:
    """Rewrite recorded worker worktree roots in a command to the checkout.

    A stored gate command routinely pins the worker's own worktree: ``env -C
    <worktree>`` decides where the gate runs, and its test files are named by
    absolute path under that tree. Promotion releases the worktree, so a
    replay of the recorded text either cannot start — ``env`` reports a removed
    directory with status 125, which a reader takes for the gate failing — or,
    while the tree survives, measures a stale copy of the repository instead of
    the tree that ships. Both are the wrong tree, so every recorded root is
    rewritten to the checkout being replayed.

    A match is taken only at a whole path segment: the character after the root
    must end the token or be a separator, so a root that is a string prefix of
    a longer path is left alone. A root that already resolves to the checkout
    is skipped, so a re-run against the tree the gate ran in is byte-identical
    to the recorded text. Returns the command to execute beside the roots the
    rewrite actually replaced.
    """
    rewritten: list[str] = []
    text = str(command or "")
    checkout_text = str(checkout)
    for raw in roots:
        candidate = str(raw or "").strip().rstrip("/")
        if not candidate:
            continue
        try:
            already = Path(candidate).resolve() == checkout
        except (OSError, RuntimeError, ValueError):
            already = False
        if already:
            continue
        pattern = re.compile(re.escape(candidate) + r"(?=$|[/\s'\"])")
        text, replacements = pattern.subn(checkout_text, text)
        if replacements:
            rewritten.append(candidate)
    return text, tuple(rewritten)


def _recorded_gate_seconds(
    run_id: str, gate_check: Mapping[str, Any] | None
) -> float | None:
    """The duration the promoted run's own gate log records, in seconds, or None.

    The log is read from the row's cited path when that resolves, and from the
    copy promotion preserves under the run directory beside it otherwise, so a
    run promoted on a machine that released the worktree still carries a
    duration. A log that is absent, unreadable, or carries no runner duration
    reports None rather than a number, and None keeps the fixed default bound:
    an unmeasured duration is not a zero, and a zero would derive a bound no
    gate could meet.
    """
    candidates: list[Path] = []
    if isinstance(gate_check, Mapping):
        raw = str(gate_check.get("log_path") or "").strip()
        if raw:
            candidates.append(Path(raw).expanduser())
    candidates.append(run_dir(run_id) / _PRESERVED_GATE_LOG_NAME)
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        matches = _GATE_LOG_DURATION.findall(text)
        if matches:
            try:
                return float(matches[-1])
            except ValueError:
                continue
    return None


def _replay_bound(
    timeout_seconds: float | None,
    recorded_gate_seconds: float | None,
) -> tuple[float, str]:
    """The bound a re-run executes under, and which input set it.

    An explicit timeout is taken as given: a caller who names a bound has
    judged the gate for itself. Otherwise a recorded gate duration derives one
    with headroom, held between the historical default and the ceiling, and a
    run whose log records no duration keeps the default. The source is
    returned beside the value so the recorded report can say which of the
    three applied: a bound a reader cannot trace to an input is a number they
    cannot act on when a replay times out.
    """
    if timeout_seconds is not None:
        return float(timeout_seconds), "explicit"
    if recorded_gate_seconds is not None and recorded_gate_seconds > 0:
        derived = recorded_gate_seconds * _REPLAY_BOUND_HEADROOM
        clamped = min(
            max(derived, _REPLAY_BOUND_DEFAULT_SECONDS),
            _REPLAY_BOUND_CEILING_SECONDS,
        )
        return clamped, "derived"
    return _REPLAY_BOUND_DEFAULT_SECONDS, "default"


def _replay_cut_short_reason(
    *,
    bound_seconds: float,
    bound_source: str,
    elapsed_seconds: float,
) -> str:
    """The statement a re-run stopped by its bound leaves behind.

    It names the bound, which input set it, and the seconds the replay ran
    before it was stopped, so the same sentence serves the report's ``reason``,
    the finding, and the log's cut-short line: a reader who has only the log can
    tell a stopped replay from one that ran and printed nothing.
    """
    origin = {
        "explicit": "named by the caller",
        "derived": "derived from the run's recorded gate duration",
        "default": "the default, as no gate duration is recorded",
    }[bound_source]
    return (
        f"the gate did not finish within the {bound_seconds:g}s re-run bound "
        f"({origin}) and was stopped after {elapsed_seconds:.2f}s"
    )


def rerun_gate_at_integrated_revision(
    *,
    repository: Path,
    gate_check: Mapping[str, Any] | None,
    base_verdict: str = "passed",
    integrated_revision: str = "HEAD",
    timeout_seconds: float | None = None,
    command: str | None = None,
    changed_paths: Sequence[str] | None = None,
    worktree_roots: Sequence[str | Path] = (),
    replay_log_path: str | Path | None = None,
    recorded_gate_seconds: float | None = None,
) -> dict[str, Any]:
    """Re-run one gate against the tree that ships, and compare its verdict.

    A worker's gate runs against the base revision its worktree branched from,
    so a coordinator may name the merged revision while the checkout has moved
    on past it: bookkeeping commits land on the branch, and refusing every
    checkout that is not exactly the integrated revision sends the coordinator
    to re-check-out a tree the gate does not depend on. The re-run therefore
    also accepts a checkout the integrated revision is an ancestor of, but only
    when none of the run's own changed paths differs between the two — the
    extra commits are then unable to change what the gate measures. A checkout
    whose extra commits touch one of those paths is refused, and a checkout
    whose paths cannot be compared at all is refused too — whether the run
    states no paths or the comparison itself fails — because an unknown scope
    is not an empty one. ``changed_paths_differing`` reports the comparison's
    result: the differing paths, an empty list for a comparison that ran and
    found none, and null for one that was never taken.

    The command is executed only when the repository's tree is acceptable: a
    run against any other tree verifies the wrong tree, so it never executes
    and the reason is stated. The base verdict is taken as given — it is the
    gate the run already recorded, which this check tests rather than
    re-creates.

    A gate that did not run, or did not finish within the bound, is reported
    as ``not-run`` with its reason, never as passed: an unmeasured re-run must
    not read as a verified one.

    ``ok`` is true only when the re-run finished and passed. A re-run that
    exits by timeout, or otherwise ends without a status, reports ``ok: false``
    with a finding naming the timeout and the seconds it ran before it was cut
    short, so the one field a caller reads first cannot call an unmeasured
    re-run a success. Its log carries a ``# cut short:`` line saying the bound
    stopped it rather than ending on header lines a reader takes for a gate
    that printed nothing. A re-run that completes keeps its exit status,
    verdict and finding unchanged, and reports ``ok: false`` when that verdict
    is failed, so the first field a caller reads answers whether the merged
    tree passed rather than only whether the command returned.

    The bound the re-run executes under resolves from three inputs, and the
    report names which applied and its value: an explicit ``timeout_seconds``
    as given (``explicit``); otherwise the duration in
    ``recorded_gate_seconds``, the run's own gate log read by the caller, with
    headroom and held between the historical default and a ceiling
    (``derived``); otherwise the historical default (``default``). A run whose
    gate legitimately took longer than the default is therefore replayed
    under a bound that fits it rather than reported ``not-run`` forever.

    An explicit ``command`` is run in place of the run's stored gate command,
    so a caller can measure a wider suite — a whole-repository one — than the
    node's own gate, and can do so on a run that stored none. The report names
    the command actually executed and whether it came from the option or the
    stored row, so a reader can tell a supplied command from the recorded one.

    A recorded command routinely pins the worker's own worktree — ``env -C
    <worktree>`` decides where the gate runs and its test files are named by
    absolute path under that tree — and promotion releases that worktree, so
    replaying the text verbatim either cannot start (``env`` reports a removed
    directory with status 125, which a reader takes for the gate failing) or
    measures a stale copy of the repository instead of the tree that ships.
    Each root in ``worktree_roots`` is therefore rewritten to the checkout
    being replayed before the command executes, and ``worktree_roots_rewritten``
    names the ones the rewrite replaced.

    The re-run's own output is kept when ``replay_log_path`` is given: the
    command that ran, the captured stdout and stderr, and the exit status go to
    that path under a header naming the revision, the tree, and the command,
    and ``log_path`` cites the file the text landed in. A replay whose output
    was discarded could only be read as an exit status — measured 2026-09-28, a
    replayed gate recorded exit 1 and neither the failing test ids nor the
    runner's stderr survived anywhere.
    """
    base = str(base_verdict).strip().lower()
    if base not in ledger.GATE_VERDICTS:
        raise CrewError(
            f"base gate verdict {base_verdict!r} is not one of "
            f"{', '.join(ledger.GATE_VERDICTS)}; the base verdict is the gate "
            "the run already recorded, which this re-run is compared against"
        )
    stored_command = str((gate_check or {}).get("command") or "").strip()
    bound_seconds, bound_source = _replay_bound(timeout_seconds, recorded_gate_seconds)
    supplied = str(command or "").strip()
    command = supplied or stored_command
    command_source = "option" if supplied else ("stored" if stored_command else None)
    integrated = _commit_canonical_id(repository, str(integrated_revision))
    checkout = _commit_canonical_id(repository, "HEAD")
    on_integrated = bool(integrated and checkout and integrated == checkout)
    descends = bool(
        not on_integrated
        and integrated
        and checkout
        and _revision_is_ancestor(repository, integrated, checkout)
    )
    differing = (
        _paths_differing_between(repository, integrated, checkout, changed_paths)
        if descends
        else None
    )
    report: dict[str, Any] = {
        "base_verdict": base,
        "integrated_verdict": "not-run",
        "integrated_revision": integrated or str(integrated_revision),
        "checkout_revision": checkout or "",
        "checkout_on_integrated_revision": on_integrated,
        "checkout_descends_from_integrated_revision": descends,
        "changed_paths_differing": list(differing) if differing is not None else None,
        "gate_command": command or None,
        "gate_command_source": command_source,
        "replay_bound_source": bound_source,
        "replay_bound_seconds": bound_seconds,
        "recorded_gate_seconds": recorded_gate_seconds,
        "ran": False,
        "exit_status": None,
        "timed_out": False,
        "replay_elapsed_seconds": None,
        "ok": True,
        "log_path": None,
        "worktree_roots_rewritten": [],
        "reason": None,
        "finding": None,
    }
    replay_text: str | None = None
    prose = gate_command_prose(command)
    if not command:
        reason = "no gate command is stored to re-run"
    elif prose is not None:
        token_class, token = prose
        reason = (
            f"the gate command {command!r} carries a {token_class} ({token!r}), "
            "so it describes the check rather than running it. A shell cannot "
            "execute the description, and executing it would report a failure "
            "the integrated revision did not cause"
        )
    elif integrated is None:
        reason = (
            f"integrated revision {integrated_revision!r} does not resolve to "
            "a commit in the repository"
        )
    elif not checkout:
        reason = "the repository has no resolvable HEAD to run the gate against"
    elif not on_integrated and not descends:
        reason = (
            f"the checkout is at {checkout[:12]}, not the integrated revision "
            f"{integrated[:12]}, and does not descend from it: a gate run here "
            "would verify the wrong tree. Check the integrated revision out, "
            "or name the revision the checkout actually carries"
        )
    elif not on_integrated and differing is None:
        reason = (
            f"the checkout is at {checkout[:12]}, past the integrated revision "
            f"{integrated[:12]}, and the run's changed paths could not be "
            "compared between the two: either the run does not state what it "
            "changed or the comparison could not be taken, and an unknown "
            "comparison is not an empty one, so nothing establishes that the "
            "commits after the merge leave what the gate measures untouched. A "
            "gate run here would verify the wrong tree. Check the integrated "
            "revision out, or state the paths the run changed"
        )
    elif not on_integrated and differing:
        reason = (
            f"the checkout is at {checkout[:12]}, past the integrated revision "
            f"{integrated[:12]}, and the commits between them change "
            + ", ".join(differing[:5])
            + ": a gate run here would verify the wrong tree, because this "
            "run's merge moved past paths the gate measures. Check the "
            "integrated revision out, or re-run the gate on a checkout whose "
            "changed paths are unchanged"
        )
    else:
        reason = None
        checkout_root = Path(repository).expanduser().resolve()
        replayed, rewritten_roots = _rewrite_worktree_roots(
            command, roots=worktree_roots, checkout=checkout_root
        )
        report["worktree_roots_rewritten"] = list(rewritten_roots)
        header = _replay_log_header(
            replay_command=replayed,
            checkout=checkout_root,
            checkout_revision=report["checkout_revision"],
            integrated_revision=report["integrated_revision"],
            rewritten_roots=rewritten_roots,
        )
        started = time.monotonic()
        try:
            result = subprocess.run(
                ["sh", "-c", replayed],
                cwd=str(repository),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
                timeout=bound_seconds,
            )
        except subprocess.TimeoutExpired as expired:
            elapsed = time.monotonic() - started
            report.update(ran=True, timed_out=True, replay_elapsed_seconds=elapsed)
            partial = expired.output if isinstance(expired.output, str) else ""
            written = _write_replay_log(
                replay_log_path,
                header=header,
                output=partial,
                exit_status=None,
                cut_short=_replay_cut_short_reason(
                    bound_seconds=bound_seconds,
                    bound_source=bound_source,
                    elapsed_seconds=elapsed,
                ),
            )
            if written is not None:
                report["log_path"] = str(written)
            replay_text = partial
        else:
            report["ran"] = True
            report["exit_status"] = result.returncode
            report["integrated_verdict"] = (
                "passed" if result.returncode == 0 else "failed"
            )
            replay_text = result.stdout or ""
            written = _write_replay_log(
                replay_log_path,
                header=header,
                output=replay_text,
                exit_status=result.returncode,
            )
            if written is not None:
                report["log_path"] = str(written)
    if reason is not None:
        report["reason"] = reason
    elif report["timed_out"]:
        report["reason"] = _replay_cut_short_reason(
            bound_seconds=bound_seconds,
            bound_source=bound_source,
            elapsed_seconds=report["replay_elapsed_seconds"],
        )
    report["finding"] = _merged_gate_finding(
        report["base_verdict"],
        report["integrated_verdict"],
        integrated_revision=report["integrated_revision"],
        exit_status=report["exit_status"],
        reason=report["reason"],
        failure_ids=tuple(sorted(_control_failure_ids(replay_text)))
        if replay_text
        else (),
        timed_out=report["timed_out"],
        replay_elapsed_seconds=report["replay_elapsed_seconds"],
    )
    # ``ok`` reads the verdict, not the measurement: only a replay that finished
    # and passed is a success. A replay cut short by the bound, or one that never
    # started, produced no status; one that completed failing carries a red
    # verdict. A caller taking ok at its word on either would conclude the
    # opposite of the fact.
    report["ok"] = report["integrated_verdict"] == "passed"
    return report


def record_gate_rerun_at_integrated_revision(
    *,
    project: str,
    run_id: str,
    repository: Path,
    integrated_revision: str = "HEAD",
    timeout_seconds: float | None = None,
    root: str | Path | None = None,
    command: str | None = None,
) -> dict[str, Any]:
    """Production caller: re-run a run's gate at the integrated revision and record it.

    A run's gate is measured at the base its worktree branched from, so a
    contract that lands after that base never binds the run; only the merged
    tree can tell whether a base-green gate still holds. This is the surface a
    coordinator reaches after merging: it reads the run's stored gate command
    and base verdict from its committed ledger row, re-runs that command
    against the merged checkout, writes the full re-run report back onto the
    run's ledger row (finding present or absent, so a reader sees the merged
    tree was re-checked either way), keeps the shadow store in agreement, and
    commits the edit in one landing.

    A ``command`` supplied here replaces the stored gate command for the
    re-run, so a coordinator can measure a suite wider than the node's own
    gate — or measure a run that stored no gate command at all — against the
    merged head. The report records which command ran and whether it came from
    the option or the stored row. An omitted ``timeout_seconds`` derives the
    re-run bound from the duration the run's own gate log records, so a gate
    that legitimately ran longer than the fixed default is replayed under a
    bound that fits it; an explicit value is taken as given. Either way the
    report records which bound applied and its value. A stored command that pins the worker's
    released worktree has that root rewritten to the checkout being replayed,
    so a gate that ran correctly in its worker's tree is not recorded as
    failing here because the directory it named is gone. The re-run's captured
    output is written under this run's directory beside the preserved gate log
    and its path is recorded on the report, so a failure carries the text that
    explains it rather than only an exit status. The payload's ``ok`` reports
    whether the re-run finished and passed, so a replay cut short by its bound
    and a gate the merged tree fails both reach a coordinator as failures
    rather than as successes.
    """
    checkout = Path(repository).expanduser().resolve()
    ledger_root = root if root is not None else checkout
    probe = _git(checkout, "rev-parse", "--is-inside-work-tree", check=False)
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        raise CrewError(
            f"run {run_id!r} gate cannot be re-run at the integrated revision: "
            f"{checkout} is not a git worktree, so the recorded report could not "
            "be committed; run `reckon crew verify-gate` against a checkout that "
            "is a git worktree"
        )
    data, version = ledger.load(project, root=ledger_root)
    row = next(
        (item for item in data["runs"] if str(item.get("run_id") or "") == run_id),
        None,
    )
    if row is None:
        raise CrewError(
            f"run {run_id!r} has no row in the {project!r} ledger, so its gate "
            "cannot be re-run at the integrated revision; a promoted run's gate "
            "comes from its committed row. Run `reckon crew complete --run "
            f"{run_id}` to promote it first, then rerun this"
        )
    stored_gate_check = row.get("gate_check")
    recorded_gate_seconds = _recorded_gate_seconds(
        run_id,
        stored_gate_check if isinstance(stored_gate_check, Mapping) else None,
    )
    report = rerun_gate_at_integrated_revision(
        repository=checkout,
        gate_check=stored_gate_check if isinstance(stored_gate_check, Mapping) else None,
        base_verdict=str(row.get("gate") or "passed"),
        integrated_revision=integrated_revision,
        timeout_seconds=timeout_seconds,
        command=command,
        changed_paths=_cited_changed_paths(checkout, row.get("commits")),
        worktree_roots=_recorded_worktree_roots(row),
        replay_log_path=run_dir(run_id) / _REPLAY_GATE_LOG_NAME,
        recorded_gate_seconds=recorded_gate_seconds,
    )
    record_path = ledger.run_path(project, run_id, ledger_root)
    if record_path.is_file():
        _update_run_record(record_path, run_id, {"integrated_gate_check": report})
        new_version = None
    else:
        record_path = ledger.ledger_path(project, ledger_root)
        patched = [dict(item) for item in data["runs"]]
        for index, item in enumerate(patched):
            if str(item.get("run_id") or "") == run_id:
                patched[index]["integrated_gate_check"] = report
                break
        new_version = ledger.write(
            project, {**data, "runs": patched}, version, ledger_root
        )
    from reckon import run_store

    store_synopsis = run_store.import_ledger(project, root=ledger_root)
    landing = _commit_landing_writes(
        run_id=run_id,
        verdict=str(report.get("integrated_verdict") or "not-run"),
        checkout=checkout,
        paths=[record_path],
        subject=f"record({run_id}): re-run gate at integrated {integrated_revision}",
        body=(
            "Re-run the run's stored gate command against the merged tree and "
            "record the report on its ledger row, so a gate the integrated "
            "revision no longer satisfies is recorded against the run rather "
            "than only printed."
        ),
    )
    return {
        "run_id": run_id,
        "project": project,
        "ledger_path": str(record_path),
        "ledger_version": new_version,
        "checkout_on_integrated_revision": report.get(
            "checkout_on_integrated_revision"
        ),
        "checkout_revision": report.get("checkout_revision"),
        "report": report,
        # The CLI publishes this payload behind its own success flag, so a
        # replay that measured nothing must carry the failure here or the
        # caller reads ok on a check that never produced a status.
        "ok": report["ok"],
        "finding": report.get("finding"),
        "landing": landing,
        "store_synopsis": store_synopsis,
    }



from reckon.crew.promotion_checks import (
    _control_failure_ids,
)
from reckon.crew.promotion_evidence import (
    _commit_canonical_id,
)
from reckon.crew.promotion_release import (
    _update_run_record,
)
from reckon.crew.promotion_scope import (
    _commit_landing_writes,
    _revision_is_ancestor,
)
