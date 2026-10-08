#!/usr/bin/env python3
"""Stop hook: refuse a crew worker's turn end while its run manifest is unfinished
or its worktree still holds uncommitted changes.

A Claude Code worker that ends its turn with a non-terminal manifest costs a
manual resume: the process ends, the record still says ``in-progress``, and a
coordinator cannot tell completion from a truncated turn. This hook makes the
manifest a precondition of the terminal stop.

The hook reads the harness's Stop payload on stdin. It resolves the run manifest
two ways, in order: from ``RECKON_MANIFEST`` (exported into every dispatched
worker's environment), else by matching the payload's working directory against
the live crew run pointers on this host. When no run resolves it writes nothing
and exits 0, so a coordinator or an interactive session is never affected.

When a run does resolve, the hook blocks in four cases: the manifest is absent,
its top-level ``status:`` line does not name a terminal value, it is terminal
but was last written before this attempt began — states that the writer was an
earlier attempt, not the worker now stopping — or the run's worktree holds an
uncommitted change to a tracked file, or an uncommitted file that is not
ignored, outside the run directory, which is the dirt a worktree release would
otherwise strand. A ``status: waiting`` is a further acceptable ending when the
declaration beside it is one the fleet's own reader honours — the worker has
named what it is parked on and what ends the park — and is refused, naming the
missing field. Every refusal names the manifest path, or the worktree and each
uncommitted path, and what is missing.

A dirty stop is refused until the paths are committed or discarded; the
``.venv`` and ``.env`` provisioning links are exempt, and so is everything
under the run directory. A ``blocked`` or ``failed`` manifest may end dirty,
but only once it names every uncommitted path — the state of a worker whose
mess is exactly why it stopped.

So a worker that genuinely cannot finish is never trapped: the hook blocks at
most three times per stop chain, counting in a file in the run directory. The
chain is delimited by the payload's ``stop_hook_active`` flag: a fresh stop
(``False``) resets the count, so a run resumed after its predecessor exhausted
the cap gets its own three refusals rather than inheriting a spent counter. The
stop allowed once the cap is reached is not silent: the hook writes the terminal
record itself, setting the manifest's top-level status to ``blocked`` and
appending the blocker line that names why, so a capped run never reads as an
ordinary stop. A dirty stop that reaches the cap records every uncommitted path
in the manifest before the stop is allowed, so even the cap does not end a turn
dirty and unnamed.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

_bootstrap_path = Path(__file__).with_name("interpreter_bootstrap.py")
_bootstrap_spec = importlib.util.spec_from_file_location(
    "interpreter_bootstrap", _bootstrap_path
)
_bootstrap = importlib.util.module_from_spec(_bootstrap_spec)
_bootstrap_spec.loader.exec_module(_bootstrap)
_bootstrap_error = _bootstrap.ensure_interpreter(__file__)
if _bootstrap_error:
    raise SystemExit(_bootstrap_error)

TERMINAL_STATUSES = frozenset({"complete", "blocked", "failed"})
DIRTY_END_STATUSES = frozenset({"blocked", "failed"})
WAITING_STATUS = "waiting"
BLOCK_LIMIT = 3
COUNTER_NAME = ".worker_stop_blocks"
PROVISIONING_LINKS = frozenset({".venv", ".env"})


def _config_home() -> Path:
    """Resolve the reckon config home the way the reckon package does."""
    env = os.environ.get("RECKON_HOME")
    if env:
        return Path(env).expanduser()
    xdg = Path.home() / ".config" / "reckon"
    if xdg.exists():
        return xdg
    return Path.home() / "docs-server"


def _live_records() -> Iterator[dict[str, Any]]:
    """Every readable live crew run record on this host, in path order."""
    live_dir = _config_home() / "crew" / "live"
    if not live_dir.is_dir():
        return
    for path in sorted(live_dir.glob("*.json")):
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(record, dict):
            yield record


def _resolved_path(raw: Any) -> Path | None:
    """``raw`` as an absolute path, or None when it names nothing usable."""
    if not raw:
        return None
    try:
        return Path(str(raw)).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _manifest_from_pointer(cwd: Path) -> Path | None:
    """Manifest of the live crew run whose worktree holds ``cwd``.

    Matched when ``cwd`` is the worktree or any directory inside it, so a
    worker running in a subdirectory of its own worktree still resolves its run.
    """
    for record in _live_records():
        resolved = _resolved_path(record.get("worktree"))
        if resolved is None:
            continue
        if cwd != resolved and not cwd.is_relative_to(resolved):
            continue
        manifest = record.get("manifest_path")
        if manifest:
            return Path(str(manifest)).expanduser()
    return None


def _worktree_for(payload: dict[str, Any], manifest: Path) -> Path | None:
    """The worktree whose cleanliness this stop answers for, or None.

    The live run record is the source of truth: it names the worktree the run
    was dispatched into whether or not the stopping process stands inside it.
    Without a record, the git worktree holding the payload's cwd is the
    fallback; a directory that is in no git worktree at all resolves to None,
    and nothing is refused on its account.
    """
    wanted = _resolved_path(manifest)
    if wanted is not None:
        for record in _live_records():
            if _resolved_path(record.get("manifest_path")) == wanted:
                resolved = _resolved_path(record.get("worktree"))
                if resolved is not None:
                    return resolved
    cwd = _resolved_path(payload.get("cwd") or os.getcwd())
    if cwd is None:
        return None
    return _git_toplevel(cwd)


def _git_output(argv: list[str]) -> str | None:
    """Stdout of one git invocation, or None when git cannot answer.

    An answer that never arrived is not evidence of a dirty tree: the callers
    read None as nothing to refuse, because a stop hook that blocked on its own
    failure would be the trap it exists to prevent.
    """
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _git_toplevel(cwd: Path) -> Path | None:
    """The root of the git worktree holding ``cwd``, or None."""
    out = _git_output(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"])
    if out is None or not out.strip():
        return None
    return _resolved_path(out.strip())


def _is_under(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _uncommitted_paths(worktree: Path, run_dir: Path) -> list[str] | None:
    """Relative paths ``worktree`` holds uncommitted, outside the run directory.

    Read from git's own porcelain report, so a tracked modification and an
    untracked file that is not ignored are both named while ignored files are
    not. The provisioning links a worktree is handed at creation and everything
    under the run directory are exempt. ``None`` when git cannot answer.
    """
    out = _git_output(["git", "-C", str(worktree), "status", "--porcelain", "-z"])
    if out is None:
        return None
    run_root = _resolved_path(run_dir) or run_dir
    paths: list[str] = []
    fields = out.split("\0")
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if not entry:
            continue
        code = entry[:2]
        if code[:1] in "RC" or code[1:2] in "RC":
            # A rename or copy carries its origin path in the following field.
            index += 1
        relative = entry[3:]
        if not relative:
            continue
        stripped = relative.rstrip("/")
        if stripped in PROVISIONING_LINKS:
            continue
        if _is_under(worktree / stripped, run_root):
            continue
        paths.append(relative)
    return paths


def _paths_not_named(manifest: Path, worktree: Path, paths: Sequence[str]) -> list[str]:
    """Of ``paths``, those the manifest text does not name.

    A path counts as named when its worktree-relative spelling or its absolute
    spelling appears in the manifest, so a worker may record it either way.
    """
    try:
        text = manifest.read_text()
    except OSError:
        return list(paths)
    unnamed = []
    for relative in paths:
        stripped = relative.rstrip("/")
        if stripped in text or str(worktree / stripped) in text:
            continue
        unnamed.append(relative)
    return unnamed


def resolve_manifest(payload: dict[str, Any]) -> Path | None:
    """The run manifest for a Stop payload, or None when no run resolves."""
    env = os.environ.get("RECKON_MANIFEST")
    if env and env.strip():
        return Path(env).expanduser()
    cwd_raw = payload.get("cwd") or os.getcwd()
    try:
        cwd = Path(str(cwd_raw)).expanduser().resolve()
    except OSError:
        return None
    return _manifest_from_pointer(cwd)


def read_status(manifest: Path) -> str | None:
    """The manifest's top-level ``status:`` value, or None.

    Only a line starting at column zero counts. A ``status:`` line indented
    under another key is a nested value, not the manifest's own status.
    """
    if not manifest.is_file():
        return None
    try:
        text = manifest.read_text()
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("status:"):
            return line.split(":", 1)[1].strip() or None
    return None


def _manifest_predates_attempt(manifest: Path) -> bool:
    """Whether a manifest was last written before this worker attempt began."""
    raw = os.environ.get("RECKON_ATTEMPT_STARTED_AT", "").strip()
    if not raw or raw.endswith("z") or not manifest.is_file():
        return False
    if __package__ in (None, ""):
        # The registered hook command is this file's own path, so a bare-script
        # launch has not put the checkout that ships it on sys.path yet.
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from reckon._timestamps import parse_utc

    attempt_started_at = parse_utc(raw)
    if attempt_started_at is None:
        return False
    try:
        return manifest.stat().st_mtime_ns < int(
            attempt_started_at.timestamp() * 1_000_000_000
        )
    except (OSError, OverflowError):
        return False


def _wait_declaration(manifest: Path) -> tuple[bool, str]:
    """Whether the manifest's declared external wait is one the fleet acts on.

    Read through the classifier's own reader rather than a second copy of the
    wait grammar: a declaration the fleet would refuse as incomplete is not a
    state a turn may end on, and one reader means the hook and the classifier
    cannot disagree about what a parked worker is. Imported lazily, so an
    ordinary stop that declares no wait pays nothing for the validator.
    """
    if __package__ in (None, ""):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    try:
        from reckon.crew.recovery import _manifest_wait
        from reckon.crew.reports import ManifestParseError, parse_manifest
    except ImportError as exc:
        return False, f"the wait reader could not be loaded ({exc})"
    try:
        data = parse_manifest(manifest.read_text(encoding="utf-8"))
    except (OSError, ManifestParseError, ValueError) as exc:
        return False, f"the manifest could not be read as a wait ({exc})"
    try:
        wait = _manifest_wait(
            data,
            manifest,
            now_seconds=time.time(),
            stale_after_seconds=0,
        )
    except Exception as exc:  # noqa: BLE001 - an unreadable wait is a refusal, not a crash
        return False, f"the wait declaration could not be validated ({exc})"
    if wait is None:
        return False, (
            "the wait fields name no actionable wait; wait_condition, "
            "wait_probe (or wait_file), wait_terminal and resume_brief must "
            "declare a condition that can end"
        )
    if not wait["valid"]:
        return False, str(wait.get("error") or "the wait declaration is incomplete")
    return True, ""


def _write_terminal_record(
    manifest: Path,
    *,
    keep_status: bool = False,
    withheld: Sequence[str] = (),
) -> None:
    """Record why a capped stop chain ended, preserving every other line.

    Sets the manifest's top-level status to ``blocked`` unless ``keep_status``
    holds the worker's own status, which is the case when the status side of
    the stop was already acceptable and only the worktree was dirty. Creates
    the manifest when it is absent. ``withheld`` names the uncommitted paths a
    capped stop is ending with, so a dirty end is never silent about what it
    leaves behind. Written atomically through a temp file in the same
    directory."""
    blocker = (
        "blocker: turn ended with uncommitted paths after 3 refusals"
        if keep_status and withheld
        else "blocker: turn ended without a terminal manifest after 3 refusals"
    )
    try:
        text = manifest.read_text()
    except OSError:
        text = ""
    lines = text.splitlines()
    out: list[str] = []
    replaced = False
    for line in lines:
        if not replaced and line.startswith("status:"):
            out.append(line if keep_status else "status: blocked")
            replaced = True
        else:
            out.append(line)
    if not replaced and not keep_status:
        out.insert(0, "status: blocked")
    if blocker not in out:
        out.append(blocker)
    if withheld:
        named = "uncommitted_paths: " + json.dumps(list(withheld))
        if named not in out:
            out.append(named)
    try:
        manifest.parent.mkdir(parents=True, exist_ok=True)
        from reckon._store import write_atomically

        write_atomically(
            manifest, lambda handle: handle.write("\n".join(out) + "\n"), fsync=False
        )
    except OSError:
        pass


def decide(payload: dict[str, Any]) -> tuple[bool, str | None]:
    """Return ``(blocked, reason)`` for one Stop payload.

    A stop is allowed only when the manifest resolves, its status is terminal
    or a declared wait the fleet's reader honours, and the run's worktree is
    clean — or the manifest is blocked or failed and names every uncommitted
    path. ``blocked`` is True when the stop is refused. When the stop is
    allowed it is False and ``reason`` is None, and the caller writes no
    output.
    """
    manifest = resolve_manifest(payload)
    if manifest is None:
        return False, None

    status = read_status(manifest)
    predates_attempt = _manifest_predates_attempt(manifest)
    # A declared wait is a finished turn: the worker has named what it is
    # parked on and what ends the park, so the record is complete and no resume
    # is owed until the condition lifts. Only a declaration the fleet's own
    # reader honours counts — a malformed one is refused with the missing field
    # named, so the next attempt can repair it rather than being forced to
    # write a status it cannot honestly claim.
    wait_refusal = ""
    allowed_status = False
    if status in TERMINAL_STATUSES and not predates_attempt:
        allowed_status = True
    elif status == WAITING_STATUS and not predates_attempt:
        declared, wait_refusal = _wait_declaration(manifest)
        allowed_status = declared

    worktree = _worktree_for(payload, manifest)
    uncommitted: list[str] = []
    if worktree is not None:
        found = _uncommitted_paths(worktree, manifest.parent)
        if found:
            uncommitted = found
    unnamed: list[str] = []
    if uncommitted and status in DIRTY_END_STATUSES:
        unnamed = _paths_not_named(manifest, worktree, uncommitted)
    # A blocked or failed worker may end dirty once its manifest names every
    # uncommitted path; every other stop must be clean before it may end.
    unclean = bool(uncommitted) and (status not in DIRTY_END_STATUSES or bool(unnamed))
    if allowed_status and not unclean:
        return False, None

    counter = manifest.parent / COUNTER_NAME
    if payload.get("stop_hook_active"):
        try:
            count = int(counter.read_text().strip() or "0")
        except (OSError, ValueError):
            count = 0
    else:
        count = 0
    if count >= BLOCK_LIMIT:
        _write_terminal_record(
            manifest,
            keep_status=allowed_status,
            withheld=uncommitted if unclean else (),
        )
        return False, None
    try:
        counter.parent.mkdir(parents=True, exist_ok=True)
        counter.write_text(f"{count + 1}\n")
    except OSError:
        pass

    if allowed_status:
        reason = (
            f"worker stop refused: the worktree {worktree} holds uncommitted "
            f"changes outside the run directory: {', '.join(uncommitted)}. "
            "Commit each path or discard it before the turn can end."
        )
    else:
        if predates_attempt:
            what = "predates this attempt"
        elif status == WAITING_STATUS:
            what = f"declares status 'waiting' but {wait_refusal}"
        else:
            what = "is absent" if status is None else f"has status '{status}'"
        reason = (
            f"worker stop refused: the run manifest {manifest} {what}; it must "
            "be present with a status of complete, blocked or failed — or "
            "declare a waiting status with a well-formed wait block — before "
            "the turn can end."
        )
        if unclean:
            reason += (
                f" The worktree {worktree} also holds uncommitted changes "
                f"outside the run directory: {', '.join(uncommitted)}; commit "
                "each path or discard it."
            )
    if unclean and status in DIRTY_END_STATUSES and unnamed:
        reason += (
            " A blocked or failed manifest may end dirty only once it names "
            f"every uncommitted path; not named: {', '.join(unnamed)}."
        )
    reason += f" Refusal {count + 1} of {BLOCK_LIMIT}."
    return True, reason


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    blocked, reason = decide(payload)
    if not blocked:
        return 0

    sys.stdout.write(json.dumps({"decision": "block", "reason": reason}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
