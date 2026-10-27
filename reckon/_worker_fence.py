from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from reckon._store import write_json_atomically


class BackendError(Exception):
    """A backend cannot be translated into a runnable invocation.

    Raised for a backend whose command has no dialect, for a launch kind that
    cannot be spawned, and for a missing command — never for a *reported*
    condition such as an unavailable binary, which the caller decides about.
    """


# ── Filesystem fence ────────────────────────────────────────────────────────

# Each run owns the filesystem location its harness reads and writes its own
# state from: the claude-shaped harness its configuration, transcripts and
# session files, codex its configuration and session rollouts. Without a
# per-run home the harness writes into the operator's own dot directory, which
# the fence makes read-only — so the per-run home is what lets the fence be
# switched on for real workers rather than only in the stub. The variable,
# the run folder and the operator folder are named per dialect because the two
# harnesses disagree on all three.
_HARNESS_HOME = {
    "claude": ("CLAUDE_CONFIG_DIR", "harness", ".claude"),
    "codex": ("CODEX_HOME", "codex-home", ".codex"),
}

# The codex credential bound read-only into a run's codex home. Bound rather
# than copied: a credential written under the run directory would be writable
# by the worker, so the login the operator refreshes elsewhere could be
# shadowed by its own stale copy.
CODEX_AUTH_FILENAME = "auth.json"

# The claude credential a subscription lane binds into its run's harness home.
# Named the same as the operator's file because the harness looks for the fixed
# ``.credentials.json`` name under ``CLAUDE_CONFIG_DIR``, so the bind's
# destination is that name inside the run home rather than the operator's path.
CLAUDE_CREDENTIAL_FILENAME = ".credentials.json"

# The harness command the fence composes. Named once so the argv a reader sees
# and the capability a refusal names are the same string.
FENCE_BINARY = "bwrap"

# Each fenced worker runs in a pid namespace of its own, with a procfs mounted
# for that namespace over the host's. kill(2) resolves a pid in the caller's
# namespace, so a worker can signal only the processes it started: a cleanup
# command that selects more than it meant to, such as `ps -e` combined with
# `--ppid` (ps unions its selectors), reaches nothing outside the worker's own
# tree. The fresh /proc keeps os.getpid() and /proc/<pid> in one numbering,
# which tools that read /proc for their own process rely on. The procfs mount
# must follow the root bind, which is why these flags sit after it.
PID_NAMESPACE_ARGS = ("--unshare-pid", "--proc", "/proc")

# The flight-config key a project uses to name the MCP servers its workers may
# start. Read from the resolved flight configuration at seed time: a project
# naming no servers seeds the setting that disables every project ``.mcp.json``
# server, so a worker starts only the user-scope servers — reckon among them —
# rather than inheriting each server the checkout happens to register.
WORKER_MCP_SERVERS = "worker_mcp_servers"

# The two harness settings the seed writes into a run home's ``settings.json``.
# ``enableAllProjectMcpServers`` is the switch the harness reads for the
# project's ``.mcp.json`` servers as a group, and ``enabledMcpjsonServers`` is
# the per-server allow list that names the ones this run may start. Named once
# so the writer and a reader agree on the spelling the harness itself reads.
PROJECT_MCP_SERVERS_ENABLED_KEY = "enableAllProjectMcpServers"
ENABLED_PROJECT_MCP_SERVERS_KEY = "enabledMcpjsonServers"


def _probe_user_namespace() -> subprocess.CompletedProcess[str]:
    """Run one throwaway fence to prove a user namespace can be created.

    The probe is the fence itself in miniature: the same binary, the same
    namespace the real composition needs, and a command that does nothing.
    A host that can run this can build the fence; a host that cannot cannot
    seal a worker either, so the probe measures exactly the capability the
    refusal names rather than a proxy for it.
    """
    return subprocess.run(
        [FENCE_BINARY, "--unshare-user", "--dev-bind", "/", "/", "--", "/bin/true"],
        capture_output=True,
        text=True,
        check=False,
    )


# The namespace probe forks a process, so its verdict is remembered per probe
# callable: whether this process can create a user namespace cannot change while
# it lives, and a fleet of dispatches in one process then pays for the fork
# once rather than once per run.
_fence_probe_verdicts: dict[object, tuple[str, str] | None] = {}


def fence_capability_problem(
    probe: Callable[[], subprocess.CompletedProcess[str]] | None = None,
) -> tuple[str, str] | None:
    """Return the capability that stops the fence, or None when it can be built.

    The fence is bubblewrap over a user namespace, so it has two prerequisites:
    the binary must be on PATH and the kernel must let the process create a user
    namespace. Either missing leaves a fence that cannot be composed, and a
    fence that cannot be composed is refused rather than silently dropped — a
    launch that proceeds unfenced still reports a protection it does not have.
    The probe is injectable so a caller can pin it, and every test of the
    refusal drives the default one by hiding the binary or replacing the probe.

    The namespace probe forks a process, and dispatch asks this once per run, so
    the verdict is memoised on the probe that produced it: whether this process
    can make a user namespace cannot change while it lives, and a repeat
    dispatch in one process then pays nothing for the check. The PATH lookup is
    left unmemoised because it is a directory scan rather than a fork, and a
    caller that hides the binary relies on it reading the current PATH.

    The returned pair is ``(capability, detail)``: a short name for what is
    missing and the refusal text that names it for the operator.
    """
    if shutil.which(FENCE_BINARY) is None:
        return (
            FENCE_BINARY,
            f"{FENCE_BINARY} is not on PATH, so the fence cannot be built",
        )
    run = probe or _probe_user_namespace
    if run in _fence_probe_verdicts:
        return _fence_probe_verdicts[run]
    try:
        result = run()
    except OSError as exc:
        verdict: tuple[str, str] | None = (
            "user namespace",
            f"the user-namespace probe could not run: {exc}",
        )
    else:
        if result.returncode != 0:
            reported = (result.stderr or "").strip()
            detail = reported or f"the probe exited {result.returncode}"
            verdict = (
                "user namespace",
                f"a user namespace cannot be created: {detail}",
            )
        else:
            verdict = None
    _fence_probe_verdicts[run] = verdict
    return verdict


def harness_home(dialect_name: str, run_directory: str | Path) -> Path | None:
    """Return the config home a run owns for its harness, or None.

    The run directory is the parent of the node's manifest — the one place a
    worker is always granted to write — so the harness home sits inside it and
    a run's sessions are found under the run rather than in the operator's dot
    directory. A dialect with no harness home of its own returns None, and no
    environment is invented for it.

    A run directory that is not absolute anchors nothing, so it returns None
    too. Such a path is not a location at all: it is whatever directory each
    process that touches it happens to run in, so the home is created beside
    the seeding process and the variable that names it resolves against the
    harness's own working directory — the worktree — putting a run's harness
    state in the tree the node was dispatched to work in. A caller with no
    manifest path to derive a home from gets no home, the same as one whose
    run directory does not exist yet, rather than a home whose location is
    decided by a working directory.
    """
    declared = _HARNESS_HOME.get(dialect_name)
    if declared is None:
        return None
    directory = Path(run_directory)
    if not directory.is_absolute():
        return None
    return directory / declared[1]


def seed_harness_home(
    home: Path,
    *,
    dialect_name: str,
    operator_home: str | Path,
    declaration: Iterable[Mapping[str, Any]] = (),
    adjacent_declaration: Iterable[Mapping[str, Any]] = (),
    resume_session: str | None = None,
    worker_mcp_servers: Iterable[str] | None = None,
) -> None:
    """Create a run's harness home carrying what its harness reads there.

    A harness that starts in a bare directory loads neither the operator's
    hooks nor their instruction files, so a fenced worker silently loses the
    guards and the standing guidance the operator's own home declares — and a
    codex worker loses everything, because codex reads ``AGENTS.md`` and never
    ``CLAUDE.md``. The declaration is the operator-harness-home file list the
    backend's flight entry names (``harness_home_files``), each entry a path
    relative to the operator's harness home plus an optional JSON key filter.

    ``adjacent_declaration`` is the second table (``harness_home_adjacent_files``):
    files that live in the operator's HOME DIRECTORY rather than in the harness
    config directory, of which ``~/.claude.json`` is the only shipped case. Its
    entries resolve their source against the home directory and their
    destination against the run home, so a file the harness keeps beside its
    config directory reaches the run home without either reading or writing the
    home root. Because the harness writes such a file itself, an existing copy
    is authoritative for its own keys and only the declared keys are merged in,
    rather than the copy being skipped like a config-dir file.

    Three properties bound the copy, all read from the operator's home and
    never written back. A config-dir file already in the run home is never
    overwritten, so a resumed run keeps its own state. The operator home is
    never modified — only read. And a credential is never copied: the login the
    run needs is bound writable by the fence (:func:`_harness_credential_binds`),
    and the declarations name no credential file, so the run directory never
    holds a writable copy of the operator's login.

    A resumed run also needs the session's own transcript beside its home,
    because the harness looks for it under the home its variable names; the
    transcript is copied at the same relative path, and a fresh launch (no
    session) copies none.

    ``worker_mcp_servers`` names the project's worker-visible MCP servers. For
    the claude-shaped harness, whose settings file governs them, the run home's
    settings then records that the project's ``.mcp.json`` servers start only
    when the project asks for them, rather than every worker inheriting each
    server the checkout registers. Passing ``None`` states that no project
    configuration governs this launch — a bare composition with no flight
    layer — and then no record is written, because a project the configuration
    never named is not a project whose servers this run is dropping.
    """
    home.mkdir(parents=True, exist_ok=True)
    declared = _HARNESS_HOME.get(dialect_name)
    if declared is None:
        return
    # An existing settings.json belongs to the run that wrote it, so the MCP
    # record is written only into a file this seed creates. A resumed run keeps
    # its own file, which already carries the record written when the run home
    # was first seeded — the same home, so the record survives the resume, and a
    # run's own state is never overwritten.
    settings_preexisting = (home / "settings.json").exists()
    source_home = Path(operator_home) / declared[2]
    for entry in declaration or ():
        relative = entry.get("path")
        if not isinstance(relative, str) or not relative:
            continue
        source = source_home / relative
        if not source.exists():
            continue
        _seed_harness_entry(source, home / relative, entry.get("keys"))
    for entry in adjacent_declaration or ():
        relative = entry.get("path")
        if not isinstance(relative, str) or not relative:
            continue
        source = Path(operator_home) / relative
        if not source.exists():
            continue
        _merge_harness_entry(source, home / relative, entry.get("keys"))
    if resume_session:
        _seed_harness_session(home, source_home, str(resume_session))
    if (
        dialect_name == "claude"
        and not settings_preexisting
        and worker_mcp_servers is not None
    ):
        _seed_worker_mcp_settings(home, worker_mcp_servers)


class WorkerMcpServersError(ValueError):
    """A project's ``worker_mcp_servers`` declaration is not a list.

    Raised rather than read as an empty declaration, because a bare string
    would iterate character by character and seed one server per letter while
    reporting, to the operator and to the flight check alike, that the project
    asked for nothing.
    """

    constraint = "must be a list of MCP server names"

    def __init__(self) -> None:
        super().__init__(f"{WORKER_MCP_SERVERS}: {self.constraint}")


def _declared_worker_mcp_servers(config: Mapping[str, Any] | None) -> list[str]:
    """Return the worker MCP servers a resolved flight config names, in order.

    Read from the config under :data:`WORKER_MCP_SERVERS`. Absence names no
    server, so a project that asks for nothing starts none of the checkout's
    own. A value that is present but is not a list of server names is refused
    by name: a bare string would read as one server per character, a mapping
    would seed its keys, and a list holding a non-string would seed a value the
    operator never wrote as a name. Duplicates and empty names are dropped so
    the seeded record names each server once.
    """
    if not isinstance(config, Mapping):
        return []
    declared = config.get(WORKER_MCP_SERVERS)
    if declared is None:
        return []
    if not isinstance(declared, list) or not all(
        isinstance(name, str) for name in declared
    ):
        raise WorkerMcpServersError()
    names: list[str] = []
    for name in declared:
        if name and name not in names:
            names.append(name)
    return names


def _seed_worker_mcp_settings(home: Path, worker_mcp_servers: Iterable[str]) -> None:
    """Record in the run home's settings which MCP servers its harness starts.

    A worker whose harness starts in a run home with no settings inherits every
    server the checkout's ``.mcp.json`` registers — about twenty on one project
    measured in 2026-10-08, each paying a process and a launch that fails in a
    third of worktrees. So the seeded settings disables the project's servers as
    a group and enables only the ones the project names.

    The write is called only for a settings.json this seed created, so the
    hooks the operator carries are updated in place and nothing is lost; a run's
    own settings.json is never touched. reckon is a user-scope server carried in
    ``.claude.json`` and is not governed by this key, so it stays enabled without
    being named here.
    """
    path = home / "settings.json"
    settings = dict(_load_json_mapping(path) or {})
    settings[PROJECT_MCP_SERVERS_ENABLED_KEY] = False
    settings[ENABLED_PROJECT_MCP_SERVERS_KEY] = list(worker_mcp_servers)
    mode = _permission_bits(path)
    path.write_text(json.dumps(settings, indent=2, sort_keys=True) + "\n")
    path.chmod(0o644 if mode is None else mode | 0o200)


def _merge_harness_entry(
    source: Path, destination: Path, keys: Iterable[str] | None
) -> None:
    """Merge one declared home-root file into the run home's own copy.

    The harness writes this file itself — its projects, its oauth account, its
    own server list — so an existing run copy is authoritative for its own keys
    and is never replaced, and a fresh run home gains only the declared keys.
    Unlike a config-dir entry, this one merges rather than skipping an existing
    destination, because the run's harness is expected to have written one and
    the declared keys (the operator's MCP declarations) must survive that. A
    destination the code cannot read as a JSON object is left alone rather than
    clobbered.
    """
    merged = (
        _load_json_mapping(source)
        if keys is None
        else _filter_top_level_keys(source, keys)
    )
    if merged is None:
        return
    existing: dict[str, Any] = {}
    existing_mode: int | None = None
    if destination.exists():
        loaded = _load_json_mapping(destination)
        if loaded is None:
            return
        existing = dict(loaded)
        existing_mode = _permission_bits(destination)
    for key, value in merged.items():
        if isinstance(value, Mapping) and isinstance(existing.get(key), Mapping):
            existing[key] = {**existing[key], **value}
        else:
            existing[key] = value
    destination.parent.mkdir(parents=True, exist_ok=True)
    _write_private_json(
        destination,
        existing,
        0o600 if existing_mode is None else min(0o600, existing_mode),
    )


def _permission_bits(path: Path) -> int | None:
    """Return ``path``'s permission bits, or None if it cannot be read."""
    try:
        return path.stat().st_mode & 0o777
    except OSError:
        return None


def _write_private_json(
    destination: Path, payload: Mapping[str, Any], mode: int = 0o600
) -> None:
    """Write a JSON object private from creation, at ``mode``.

    The payload is the operator's own home configuration, so the run's copy
    must never be readable beyond ``0o600`` at any instant. The shared writer
    creates its temporary private and applies ``mode`` to that temporary before
    the rename, so the destination is only ever the private inode or the file it
    replaces, and a narrowed ceiling never leaves a wider window behind.

    ``mode`` is the ceiling applied to the final file; it is never wider than
    ``0o600`` because the caller derives it as the narrower of ``0o600`` and any
    existing destination's own mode.
    """
    write_json_atomically(
        destination,
        payload,
        indent=2,
        sort_keys=True,
        fsync=False,
        mode=mode,
    )


def _load_json_mapping(path: Path) -> dict[str, Any] | None:
    """Return a JSON object read from ``path``, or None if it is not one."""
    try:
        loaded = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return dict(loaded) if isinstance(loaded, Mapping) else None


def _seed_harness_entry(
    source: Path, destination: Path, keys: Iterable[str] | None
) -> None:
    """Copy one declared operator file or directory into the run home.

    A file already present is left untouched, so seeding is idempotent and a
    resumed run never loses state it wrote itself. A filtered file is rewritten
    from the operator's copy down to the named top-level JSON keys, which is how
    ``settings.json`` ships its hooks without its credentials, environment or
    permissions.
    """
    if destination.exists():
        return
    if source.is_dir():
        _copy_tree_without_overwrite(source, destination)
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    if keys is not None:
        filtered = _filter_top_level_keys(source, keys)
        if filtered is None:
            return
        filtered = _bind_reckon_hook_commands(filtered)
        destination.write_text(json.dumps(filtered, indent=2, sort_keys=True) + "\n")
    else:
        destination.write_bytes(source.read_bytes())
    _copy_writable_mode(source, destination)


def _filter_top_level_keys(source: Path, keys: Iterable[str]) -> dict[str, Any] | None:
    """Return the operator file's JSON reduced to ``keys``, or None if unreadable.

    A file that is not a readable JSON object seeds nothing rather than a
    malformed settings file a harness might then refuse to start with.
    """
    try:
        loaded = json.loads(source.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(loaded, Mapping):
        return None
    return {key: loaded[key] for key in keys if key in loaded}


def _bind_reckon_hook_commands(settings: dict[str, Any]) -> dict[str, Any]:
    """Bind every reckon hook command in a copied settings dict to its interpreter.

    The operator's settings file carries whatever their last install left: a
    bare ``python3 hook.py`` a shell resolves to the system interpreter, or the
    checkout-bound form. A hook that imports reckon needs the checkout's own
    interpreter — system python is older than the modules those hooks import —
    so a run home seeded from such a file is passed through the installer's
    recogniser, the same one ``reckon sync`` drives, before it is written. The
    recogniser rewrites only the commands whose script imports reckon and
    returns everything else, including a hook that is not reckon's, unchanged.
    A settings dict with no ``hooks`` object is returned as-is.
    """
    if not isinstance(settings.get("hooks"), dict):
        return settings
    from reckon.hooks.install import upgrade_registered_commands

    installed, _ = upgrade_registered_commands(settings)
    return installed


def _copy_tree_without_overwrite(source: Path, destination: Path) -> None:
    """Copy a directory tree, creating every directory and no existing file."""
    destination.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        target = destination / relative
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif not target.exists():
            target.write_bytes(path.read_bytes())
            _copy_writable_mode(path, target)


def _copy_writable_mode(source: Path, destination: Path) -> None:
    """Grant the copied file the operator's own mode, forced writable.

    The run home is the worker's own, so a file copied in from a read-only
    operator home must still be writable there — the harness updates its own
    configuration and the worker may too.
    """
    try:
        mode = source.stat().st_mode & 0o777
    except OSError:
        return
    destination.chmod(mode | 0o200)


def _seed_harness_session(home: Path, source_home: Path, session_id: str) -> None:
    """Copy a session's transcript from the operator home into the run home.

    A resume names a session recorded in the operator's home, and the harness
    looks for it under the home it was pointed at — the run's own — so a resume
    without the transcript beside it reports no such conversation. The
    transcript is copied at the same relative path so the harness's own lookup
    finds it. Only a resume copies one: a fresh launch that copied a transcript
    would resurrect a session nobody asked for.
    """
    if not session_id or not source_home.is_dir():
        return
    for path in sorted(source_home.rglob(f"*{session_id}*")):
        if not path.is_file():
            continue
        destination = home / path.relative_to(source_home)
        if destination.exists():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(path.read_bytes())
        _copy_writable_mode(path, destination)


def write_lock_directory(home: str | Path | None = None) -> Path:
    """Return the lock namespace a plan write serialises through.

    Every plan write holds an exclusive lock on a path-hashed file under the
    reckon config home, so a worker that cannot write there cannot write a plan
    at all — including the copy in its own worktree, which is the write a
    fenced worker exists to make. The fence therefore grants this one subtree
    writable and leaves the rest of the config home read-only.

    Resolution mirrors the writer's (:func:`reckon._store._config_home`), which
    is what makes the grant land on the directory the writer will open:
    ``RECKON_HOME`` wins, then ``<home>/.config/reckon``, then the legacy
    ``<home>/docs-server``.
    """
    env = os.environ.get("RECKON_HOME")
    if env:
        return Path(env).expanduser() / "locks"
    root = Path(home) if home is not None else Path.home()
    candidate = root / ".config" / "reckon"
    base = candidate if candidate.exists() else root / "docs-server"
    return base / "locks"


def seed_write_lock_namespace(home: str | Path | None = None) -> Path | None:
    """Create the lock directory a plan write opens its lock file in.

    The fence binds this directory writable, so it has to exist before the bind
    is composed, and the namespaces beneath it are the writer's own business —
    the grant is a writable subtree, not a writable file. The config home
    itself is never created: a home that does not exist is not sealed either,
    and there is then nothing for the grant to re-open.
    """
    locks = write_lock_directory(home)
    if not locks.parent.is_dir():
        return None
    locks.mkdir(parents=True, exist_ok=True)
    return locks


def create_write_roots(roots: Iterable[str | Path]) -> None:
    """Create every directory a fence is about to bind writable.

    bubblewrap binds a writable root by *source* path, so a root whose
    directory does not exist aborts the launch with ``Can't find source path``
    before the worker starts at all — and every root is at risk, not only a
    node's declared write paths: a manifest whose run directory has not been
    created yet is bound for the same reason and fails the same way.

    Every entry is a directory by construction: a declared path that names a
    file was already resolved to the directory that file will be created in, so
    declaring ``report.md`` can never create a directory by that name. An
    existing root is left exactly as it is.
    """
    for raw in roots:
        path = resolved_destination(raw)
        if path.exists():
            continue
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise BackendError(f"cannot create the write root {path}: {exc}") from exc


def codex_auth_source(home: str | Path | None = None) -> Path | None:
    """Return the operator's codex login to bind into a run, or None.

    Absent login is None and no bind is composed, so a machine without the
    credential produces a fence that is short one file rather than one that
    refuses to start.
    """
    root = Path(home) if home is not None else Path.home()
    candidate = root / ".codex" / CODEX_AUTH_FILENAME
    return candidate if candidate.is_file() else None


def claude_credential_source(home: str | Path | None = None) -> Path | None:
    """Return the operator's claude subscription login to bind, or None.

    Absent login is None and no bind is composed, so a machine without the
    credential produces a fence that is short one file rather than one that
    refuses to start. The local clive lane authenticates against its own
    server, so it never reaches this source.
    """
    root = Path(home) if home is not None else Path.home()
    candidate = root / ".claude" / CLAUDE_CREDENTIAL_FILENAME
    return candidate if candidate.is_file() else None


# The per-run record of the operator's codex login: the size the file held when
# the launch composed the writable bind, and the size read back afterwards. The
# writable bind is what lets a token refresh survive the run, and it is also
# what lets a run blank the operator's only login — a zero on the size read
# after the run is that loss. It is reported rather than left to be discovered
# when the operator's next launch fails authentication, since nothing else
# observes the file. Sizes only: the credential's contents are never read,
# copied or printed.
CODEX_LOGIN_RECORD_NAME = "codex-login.json"

# The run's stderr log, where a post-run observation reports a login loss so a
# reader of the run's live output is told without opening the run's record.
RUN_STDERR_LOG_NAME = "stderr.log"


def codex_login_record_path(run_directory: str | Path) -> Path:
    """Return the run's login-size record path, whether or not it exists."""
    return Path(run_directory) / CODEX_LOGIN_RECORD_NAME


def codex_login_size(path: str | Path) -> int | None:
    """Return the login file's size without opening it, or None if unreadable."""
    try:
        return Path(path).stat().st_size
    except OSError:
        return None


def record_codex_login_size(
    run_directory: str | Path, source: str | Path
) -> dict[str, Any] | None:
    """Record the login's size before a fenced launch, if not already recorded.

    Written once per run: the size at the first composition is the one the
    post-run reading is compared against, and a later composition — a resume,
    or a preview of one taken after the damage — must not overwrite it with a
    size measured on the wrong side of the run. A source that cannot be
    measured leaves no record, because comparing against an unknown before-size
    would report every later reading as unchanged.
    """
    destination = codex_login_record_path(run_directory)
    if destination.exists():
        return None
    size_before = codex_login_size(source)
    if size_before is None:
        return None
    record = {
        "path": str(Path(source)),
        "size_before": size_before,
        "size_after": None,
        "truncated": False,
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    write_json_atomically(destination, record, fsync=False)
    return record


def codex_login_truncation_detail(report: Mapping[str, Any]) -> str:
    """The one-line report a truncated login produces, for a record or a log."""
    return (
        f"the operator's codex login {report.get('path')} held "
        f"{report.get('size_before')} bytes before the run and "
        f"{report.get('size_after')} bytes after it; the writable bind let the "
        "run blank it"
    )


def observe_codex_login(
    run_directory: str | Path, *, now: datetime | None = None
) -> dict[str, Any] | None:
    """Read the login's size after a run and report a truncation, or None.

    The check compares the size the run recorded before its launch with the
    size on this reading, two ``stat`` calls: it never opens, reads, copies or
    prints the credential. A zero after a non-zero start is the truncation this
    reports. A file back to a non-zero size clears a report taken while a
    rewrite was mid-flight, because a token refresh truncates and writes the
    same file in place, so a reading caught between the two is momentary and a
    refresh that completed is not a loss.

    The report is written into the run's own login record and, the first time a
    truncation is seen, appended to the run's stderr log. Runs with no record —
    every lane that binds no codex credential, and every run composed before
    this check existed — are never reported on.
    """
    destination = codex_login_record_path(run_directory)
    if not destination.is_file():
        return None
    try:
        record = json.loads(destination.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, Mapping):
        return None
    try:
        size_before = int(record.get("size_before") or 0)
    except (TypeError, ValueError):
        return None
    size_after = codex_login_size(str(record.get("path") or ""))
    if size_after is None:
        return None
    moment = (now or datetime.now(UTC)).isoformat()
    if size_after == 0 and size_before > 0:
        report = dict(record)
        report["size_after"] = 0
        report["truncated"] = True
        report["detected_at"] = record.get("detected_at") or moment
        write_json_atomically(destination, report, fsync=False)
        if not record.get("truncated"):
            _report_codex_login_truncation(run_directory, report)
        return report
    if record.get("truncated"):
        healed = dict(record)
        healed["size_after"] = size_after
        healed["truncated"] = False
        healed["healed_at"] = moment
        write_json_atomically(destination, healed, fsync=False)
        return None
    if record.get("size_after") != size_after:
        updated = dict(record)
        updated["size_after"] = size_after
        write_json_atomically(destination, updated, fsync=False)
    return None


def _report_codex_login_truncation(
    run_directory: str | Path, report: Mapping[str, Any]
) -> None:
    """Append one truncation report to the run's stderr log.

    The append is how a reader of the run's live output learns of the loss; the
    record is the durable copy. A log the process cannot append to is not fatal
    to the report, which the record still carries.
    """
    destination = Path(run_directory) / RUN_STDERR_LOG_NAME
    try:
        with destination.open("a", encoding="utf-8") as handle:
            handle.write(codex_login_truncation_detail(report) + "\n")
    except OSError:
        return


# The flight keys that tune the fence's protected set. ``protected_paths``
# names paths a layer adds; ``unprotected_paths`` names defaults a layer leaves
# writable. Declared here beside the built-in default so a reader sees the
# composer and its two inputs together.
PROTECTED_PATHS_KEY = "protected_paths"
UNPROTECTED_PATHS_KEY = "unprotected_paths"


def _named_protected_paths(home: str | Path | None = None) -> list[Path]:
    """Return every path in the fence's shipped default set, home-relative.

    The set is derived from the home directory rather than stored absolute, so
    the same declaration fences a test's temp home and the operator's real one.
    Main checkouts under ``Code`` are expanded to the immediate children that
    are themselves git repositories, which admits every checkout a worker could
    reach through the shared editable install. The worktree pool
    (``Code/.reckon-worktrees``) is named in its own right, so every worktree in
    it is sealed — a fenced worker may write only its *own* worktree, which
    :func:`fence_argv` re-binds writable after the pool's read-only overlay.

    This is the default a host or project layer augments or trims through the
    flight keys; it is never replaced by a layer, so a layer that omits one of
    these from ``protected_paths`` leaves it protected. Every named path is
    returned whether or not it exists on this machine, because a path missing
    from one existence check alone is still one the fence intends to seal; only
    :func:`_default_protected_paths` narrows the set to what is on disk.
    """
    root = Path(home) if home is not None else Path.home()
    named = [
        root / ".claude",
        root / ".claude.json",
        root / ".codex",
        root / ".config" / "reckon",
        root / ".agents",
        root / "Code" / "dotfiles",
        root / ".ssh",
        root / ".gitconfig",
        root / ".config" / "git",
        root / ".config" / "gh",
        root / ".netrc",
        root / "public",
        root / ".local" / "bin",
        root / "Code" / ".reckon-worktrees",
    ]
    checkouts = root / "Code"
    if checkouts.is_dir():
        named.extend(
            child
            for child in sorted(checkouts.iterdir(), key=lambda path: path.name)
            if (child / ".git").exists()
        )
    # ``Code/dotfiles`` is named above and is also a checkout, so the same path
    # can arrive twice; a duplicate read-only overlay is harmless to bubblewrap
    # but doubles the argv and reads as a mistake. Preserve order, drop repeats.
    return list(dict.fromkeys(named))


def _default_protected_paths(home: str | Path | None = None) -> list[Path]:
    """Return the shipped default protected paths this machine has.

    A named default with no file behind it seals nothing — there is no inode to
    mount and no bytes to seal — so it is left out of the composed set, exactly
    as a path the operator never had. A default that is merely missing for an
    instant while the fence is built is waited for before this narrowing is
    consulted; see :func:`protected_read_only_binds`.
    """
    return [path for path in _named_protected_paths(home) if path.exists()]


def _resolve_declared_path(
    entry: object, home: str | Path | None = None
) -> Path | None:
    """Resolve one flight-key path entry against the operator's home.

    An entry may be absolute, ``~``-relative or relative to the home the fence
    itself resolves against, so a layer writes a path the same way the shipped
    default is written and the fence reads both through one rule.
    """
    text = str(entry).strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if not path.is_absolute():
        root = Path(home) if home is not None else Path.home()
        path = root / path
    return path


def _declared_paths(
    config: Mapping[str, Any] | None, key: str, home: str | Path | None = None
) -> list[Path]:
    """Return one flight key's declared paths, resolved against ``home``."""
    if not isinstance(config, Mapping):
        return []
    declared = config.get(key) or ()
    if isinstance(declared, (str, bytes)):
        declared = [declared]
    resolved: list[Path] = []
    for entry in declared:
        path = _resolve_declared_path(entry, home)
        if path is not None:
            resolved.append(path)
    return resolved


def declared_protected_paths(
    home: str | Path | None = None, config: Mapping[str, Any] | None = None
) -> list[Path]:
    """Return every path the fence intends to seal, whether or not it exists.

    The intent composes three things: the shipped default
    (:func:`_named_protected_paths`), the extra paths a host or project layer
    names under ``protected_paths``, and the defaults a layer names under
    ``unprotected_paths``. The default is always present, so a layer augments
    it, but can never replace it, and the only way to drop a default is to name
    it under ``unprotected_paths`` — a reduction is then a deliberate, named
    act rather than a side effect of an edited ``protected_paths`` list.

    Composition keeps the intent whole rather than reporting only what is on
    disk at this instant: a path being rewritten as the fence is composed can
    be absent for a moment while remaining one the fence must seal, and one a
    layer adds is a declaration the fence checks rather than filters away. Both
    reach :func:`protected_read_only_binds` as intent; they part company only
    for a path that stays absent past its bounded wait.
    """
    named = _named_protected_paths(home)
    additions = _declared_paths(config, PROTECTED_PATHS_KEY, home)
    composed = list(dict.fromkeys([*named, *additions]))
    removals = _declared_paths(config, UNPROTECTED_PATHS_KEY, home)
    if removals:
        removal_targets = {str(resolved_destination(path)) for path in removals}
        composed = [
            path
            for path in composed
            if str(resolved_destination(path)) not in removal_targets
        ]
    return composed


def protected_paths(
    home: str | Path | None = None, config: Mapping[str, Any] | None = None
) -> list[Path]:
    """Return the composed protected set narrowed to the paths on disk now.

    Only a path with a file behind it can be sealed read-only, so this is the
    set a caller can assert about without waiting; the fence itself composes
    from :func:`declared_protected_paths`, because a path momentarily absent
    while it is rewritten still has to be sealed.
    """
    return [path for path in declared_protected_paths(home, config) if path.exists()]


def fence_unprotected_paths(
    home: str | Path | None = None, config: Mapping[str, Any] | None = None
) -> list[Path]:
    """Return the defaults the fence leaves out of its protected set.

    Only a default counts: ``unprotected_paths`` names a default to remove, and
    an entry naming something the fence never protected changes nothing. The
    result is the list a run whose fence leaves out a default carries on its
    record, and it is empty — so nothing is recorded — for a fence that removes
    nothing.
    """
    removals = _declared_paths(config, UNPROTECTED_PATHS_KEY, home)
    if not removals:
        return []
    removal_targets = {str(resolved_destination(path)) for path in removals}
    return [
        path
        for path in _default_protected_paths(home)
        if str(resolved_destination(path)) in removal_targets
    ]


def protected_checkouts(
    home: str | Path | None = None, config: Mapping[str, Any] | None = None
) -> list[Path]:
    """Return the protected paths that are themselves git checkouts.

    A main checkout under ``Code`` carries a ``.git`` entry; the other protected
    paths — the dot directories and stores — do not. Only these are the trees a
    fence must never grant writable, because a re-bind of one re-opens the very
    git metadata the fence exists to keep closed.
    """
    return [path for path in protected_paths(home, config) if (path / ".git").exists()]


def _fenced_worktree_refusal(worktree: Path, checkouts: Sequence[Path]) -> Path | None:
    """Return the protected checkout ``worktree`` must not be composed against.

    A fence overlays each protected checkout read-only and then re-binds the
    run's write roots writable where they fall inside one. A worktree that *is*
    a protected checkout, or that is a git working tree lying inside one, is
    re-opened by that grant together with its own git metadata, so the fence
    would hand the worker a checkout it is meant to seal. Dispatch places a
    worktree under the separate worktrees root, outside every checkout, so this
    only arises if the fence is pointed at a checkout — and then it refuses
    rather than compose a fence that grants one.

    A directory that merely sits inside a protected checkout without being a
    git working tree is not a checkout and is left to compose: it is the shape
    a run's own tree takes when the tree stands under a checkout, and the
    re-bind of it is the ordinary grant into a protected tree.
    """
    target = resolved_destination(worktree)
    for checkout in checkouts:
        checkout_target = resolved_destination(checkout)
        if target == checkout_target:
            return checkout_target
        if target.is_relative_to(checkout_target) and (target / ".git").exists():
            return checkout_target
    return None


def _within_any(path: Path, roots: Sequence[Path]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


def resolved_destination(path: Path) -> Path:
    """Return the path a mount destination must name, with no link in it.

    bubblewrap cannot create a mount point anywhere below a symlink: the
    destination ``~/.config/reckon`` is refused with ``Can't mkdir`` when
    ``~/.config`` is itself a link, even though ``reckon`` is an ordinary
    directory. Resolving the whole ancestry gives the path the kernel actually
    composes the mount at, and binding there seals the same bytes because a
    write through a link path lands on its target.

    ``Path.resolve`` removes every link in the ancestry, so its result is the
    path the real filesystem holds; a path with a nonexistent ancestor is
    returned as far as it resolves, which callers treat as a missing file.
    """
    return Path(path).resolve()


# How long the fence waits for a protected bind source that is absent when the
# fence is composed. A writer that replaces a file by rename leaves its name
# missing for an instant, and a protected path bound during that instant would
# abort the launch instead of sealing it. Every absent source waits together,
# so a path this machine does not have at all costs one interval rather than
# one each.
PROTECTED_BIND_WAIT_ATTEMPTS = 10
PROTECTED_BIND_WAIT_INTERVAL_SECONDS = 0.05


def _await_protected_sources(absent: Sequence[Path]) -> list[Path]:
    """Wait, bounded, for protected bind sources that are absent to appear.

    Returns the paths still absent once the wait is over. One shared wait
    serves every absent source, so a composition is delayed by at most
    ``PROTECTED_BIND_WAIT_ATTEMPTS`` intervals however many are missing, and a
    path that appears at any point during it is no longer reported.
    """
    pending = list(absent)
    for _ in range(PROTECTED_BIND_WAIT_ATTEMPTS):
        if not pending:
            break
        time.sleep(PROTECTED_BIND_WAIT_INTERVAL_SECONDS)
        pending = [path for path in pending if not path.exists()]
    return pending


def protected_read_only_binds(
    protected: Sequence[Path],
    *,
    required: Sequence[Path] = (),
) -> list[tuple[Path, Path]]:
    """Return the source/destination pairs that overlay the protected paths.

    Every protected path is resolved through its whole ancestry, not only when
    the path is itself a symlink, because a link anywhere above it makes the
    destination unmountable and aborts the launch before the worker starts. Both
    shapes reach this list — the composed set is filtered on ``Path.exists()``
    by :func:`protected_paths`, which follows links — and two are live on this
    workstation's home: ``~/.gitconfig`` is a link into ``Code/dotfiles``, and a
    protected path below a symlinked ``~/.config`` is a plain directory with a
    link above it.

    Two consequences of resolving, both handled here. A resolved target already
    inside another protected path needs no overlay of its own: that path is
    sealed in its own right, so one of its own would be a redundant read-only
    bind. And a target that does not exist is not sealed — a link into nothing
    seals nothing, and there is no file to mount.

    A target that is absent here is one whose existence check lost a race with
    a writer that replaces files by rename, so it is waited for, bounded and
    together, before it is judged: an absent path that appears during that wait
    is bound exactly as one that was never absent. A path that stays absent
    afterwards parts company by what it is. A path in ``required`` — one a
    layer named under ``protected_paths`` — is a promise the fence cannot keep
    without a file behind the name, so it refuses the launch, naming it, rather
    than launching with the path writable. A shipped default that stays absent
    is left out instead: a path this machine does not have is not a protection
    the fence can lose, and refusing on it would stop every launch on a host
    with, say, no ``~/.netrc``.

    The destination is always the resolved path, so nothing this returns names
    a symlink anywhere in its ancestry.
    """
    targets = list(dict.fromkeys(resolved_destination(path) for path in protected))
    absent = [target for target in targets if not target.exists()]
    if absent:
        still_absent = set(_await_protected_sources(absent))
        required_targets = {str(resolved_destination(path)) for path in required}
        refused = [
            str(target)
            for target in targets
            if target in still_absent and str(target) in required_targets
        ]
        if refused:
            raise BackendError(
                f"protected path {refused[0]} is still absent after waiting "
                f"{PROTECTED_BIND_WAIT_ATTEMPTS} times at "
                f"{PROTECTED_BIND_WAIT_INTERVAL_SECONDS}s intervals; refusing to "
                "launch with a declared protected path unsealed"
            )
    resolved = [target for target in targets if target.exists()]
    pairs: list[tuple[Path, Path]] = []
    for target in resolved:
        others = [other for other in resolved if other != target]
        if _within_any(target, others):
            continue
        pairs.append((target, target))
    return pairs


def worktree_git_write_roots(worktree: str | Path) -> list[Path]:
    """Return the git directories a commit in ``worktree`` must be able to write.

    A linked worktree keeps its own git directory — HEAD, index and reflog —
    under the main checkout's ``.git``, and it stores the objects it writes in
    that repository's shared object store. Both live below a path the fence
    seals, so a worker asked to commit in its own worktree finds them read-only
    unless the fence re-opens exactly them.

    Only the object store is named from the common directory, never the common
    directory itself: objects are content-addressed and append-only, so a write
    there cannot rewrite another tree's history, while ``refs/heads``, the
    common index and the main working tree stay sealed. A path that is not a
    linked worktree — the main checkout itself, whose git directory *is* the
    common directory — returns nothing, because its refs and index are exactly
    what the fence exists to keep closed. A path that git cannot resolve
    returns nothing rather than refusing a launch over an unreadable root.
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--absolute-git-dir", "--git-common-dir"],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    lines = proc.stdout.splitlines()
    if len(lines) < 2:
        return []
    git_directory = Path(lines[0])
    common_directory = Path(lines[1])
    if not common_directory.is_absolute():
        common_directory = Path(worktree) / common_directory
    git_directory = git_directory.resolve()
    common_directory = common_directory.resolve()
    if git_directory == common_directory:
        return []
    roots = [git_directory]
    objects = common_directory / "objects"
    if objects.is_dir():
        roots.append(objects)
    return roots


def worker_scratch_seal_root(
    writable_roots: Iterable[str | Path],
) -> Path | None:
    """Return the run-scratch root to seal read-only, or None when none applies.

    Every run's scratch directory is a direct child of one node-local root
    (:func:`reckon.crew.dispatch_launch.worker_scratch_root`), so a worker that
    can write the root can write and delete every sibling run's scratch — a
    shared resource one run's cleanup destroyed for dozens of others. The root
    is therefore sealed like a protected path, and the run's *own* scratch
    directory re-bound writable after it, so a run writes only beneath its own.

    The root is sealed only when the fence is handing it a writable root inside
    that root, which every real launch does: dispatch, resume and lane change
    grant ``worker_scratch_dir(run_id)`` as the run's ``TMPDIR``. A composition
    with no such grant is not fencing a run's scratch, so it neither seals nor
    creates the root — a preview invents nothing, and a launch never given a
    scratch directory owns none to protect.

    The dispatch package is imported here rather than at module load: reckon's
    backend translation imports this module while the crew package is still
    loading, so a module-level import would close a cycle.
    """
    from reckon.crew.dispatch_launch import worker_scratch_root

    root = worker_scratch_root()
    root_target = resolved_destination(root)
    for raw in writable_roots:
        if resolved_destination(Path(raw)).is_relative_to(root_target):
            return root
    return None


# The node's own temp root. Pinned rather than read from ``TMPDIR`` for the
# same reason the worker scratch root is (``dispatch_launch`` pins
# ``WORKER_SCRATCH_ROOT_DEFAULT`` to ``/tmp``): the allocator may point
# ``TMPDIR`` at shared storage, while every run's scratch and every session's
# temporary files sit on this node-local root shared by one uid.
NODE_TMP_ROOT = Path("/tmp")  # noqa: S108 - the node's own tmp, never shared

# The marker directory codex's linux sandbox creates beneath its temp root —
# the node's own ``/tmp`` when ``TMPDIR`` is not set — to hold the synthetic
# bubblewrap mount targets and their registry lock. Named once so the runtime
# path the fence keeps writable and the directory codex writes agree on the
# spelling.
CODEX_SANDBOX_MARKER_DIRNAME = ".codex"


def node_tmp_runtime_paths() -> list[Path]:
    """Return the node-/tmp paths a fenced worker's launch must still write.

    The fence seals the worker scratch root read-only, but every other path
    beneath the node's own ``/tmp`` stays writable to every session on the node
    under one uid: Python's ``tempfile`` default, pytest's
    ``/tmp/pytest-of-<user>``, and the fleet's runtime sockets. One run's cleanup
    reached them on 2026-10-18 with ``rm -rf /tmp/tmp*`` and removed files it did
    not own, so the whole node tmp is now sealed read-only too.

    Only the run's own scratch directory — its ``TMPDIR``, re-bound writable by
    the ordinary grant in :func:`fence_argv` — and the runtime paths named here
    stay writable beneath the seal. Every entry names a place beneath the node
    tmp that the launch writes, and one that is not there when the fence is
    composed is created before the argv is, rather than left out: the paths that
    are absent are precisely the ones the harness creates for itself, so
    dropping them is what would leave a lane writing against the seal. The
    harness home needs no entry here: it sits under the run directory, which is
    already a write root, not under ``/tmp``.

    The list is one named set enumerated beside :func:`worker_scratch_seal_root`
    so a reader sees both seals and the runtime carve-outs together.
    """
    from reckon.crew.fleet_supervisor import runtime_directory

    return [
        # srun's fleet sockets: the host shims that launch a nested worker write
        # their request/ack FIFOs and zellij sockets beneath the fleet runtime
        # directory (``/tmp/<uid>-fleet``); a fenced worker that launches nested
        # work through that shim writes there.
        runtime_directory(),
        # codex's sandbox mount registry: codex's linux sandbox creates its
        # synthetic bubblewrap mount marker directory and lock here, beneath the
        # harness temp root, so a codex launch that reaches its sandbox keeps it
        # writable.
        NODE_TMP_ROOT / CODEX_SANDBOX_MARKER_DIRNAME,
    ]


def create_node_tmp_runtime_paths() -> list[Path]:
    """Create and return the node-/tmp runtime paths the fence must grant writable.

    bubblewrap binds a writable root by *source* path, so a runtime path that
    does not exist when the argv is composed composes no grant at all rather
    than a grant that fails the launch. The path that most often does not exist
    is codex's sandbox marker directory: codex's linux sandbox creates it
    lazily, on the first launch that reaches the sandbox, so on a node where
    codex has never run the read-only overlay of ``/tmp`` leaves the lane
    nowhere to create it and the launch fails against the whole node tmp being
    read-only. A grant that is present only on the nodes where the harness has
    already run is a grant the fence cannot rely on, so every enumerated path is
    created here, before the argv is composed, exactly as the launch would
    create it.

    Every enumerated path is created through :func:`create_write_roots`, the
    module's one rule for creating the directories a fence is about to bind
    writable — the same resolved-destination existence check, ``mkdir`` and
    ``BackendError`` the ordinary write roots go through, so the rule has one
    copy rather than two that can drift. A path that is already there is left
    exactly as it is: the enumerated set also covers a runtime path some other
    process created, which may be a socket or a file rather than a directory,
    and the fence neither replaces one with a directory nor refuses over it —
    it is granted writable as it stands, which is what the launch needs and what
    the pre-seal composition did. What stays specific to this function is the
    returned list, resolved the way :func:`fence_argv`'s grant loop consumes it.
    """
    paths = node_tmp_runtime_paths()
    create_write_roots(paths)
    return [resolved_destination(path) for path in paths]


def node_tmp_seal_root() -> Path | None:
    """Return the node's own ``/tmp`` to seal read-only, or None when it is absent.

    The seam :func:`fence_argv` consults for the node-tmp seal, so the seal's
    presence is a single decision a caller (or a test's negative control) can
    inject, exactly as :func:`worker_scratch_seal_root` decides the scratch
    seal. A node tmp that does not exist has nothing to seal and is not a
    directory the fence should invent, so it composes no overlay.
    """
    root = resolved_destination(NODE_TMP_ROOT)
    return root if root.is_dir() else None


def shared_store_seal_roots() -> list[Path]:
    """Return the shared delivery stores the fence seals read-only.

    Two roots hold artifacts every run shares: the reports root
    (:func:`reckon.crew.runs.reports_dir`) and the review store
    (:func:`reckon.crew.review.review_store_root`). The fence dev-binds ``/``
    dev-writable and overlays only the declared protected paths read-only, so a
    worker handed either root writable could write — and delete — every other
    project's and every other run's reports and reviews. One run's cleanup did
    exactly that on 2026-10-16, removing the reports root whole.

    Both roots are sealed like the worker scratch root: overlaid read-only
    among the protected paths, with only the run's *declared* paths beneath them
    re-bound writable by the ordinary grant that follows. A store root that does
    not exist is left out — there is no inode to seal, and a composition invents
    neither the reports root nor the review store.

    A root that already lies inside another protected path is still returned
    here; :func:`protected_read_only_binds` drops the redundant overlay, and the
    seal it stands for is still in force through the path that carries it.
    """
    from reckon.crew.review import review_store_root
    from reckon.crew.runs import reports_dir

    return [
        candidate
        for candidate in (reports_dir(), review_store_root())
        if candidate.exists()
    ]


def fence_writable_roots(
    writable_directories: Iterable[str | Path],
) -> list[Path]:
    """Return the declared roots that stay writable inside the fence.

    A declared root at or above a sealed root is skipped by
    :func:`fence_argv`'s grant loop rather than re-opening the node temp root or
    a shared delivery store through its seal; a root *beneath* a seal is the
    run's own declared path and stays writable. The backend's own sandbox is
    composed around this same set before the fence wraps the launch, and codex
    prepares each root it is told writable — so it has to be handed only what
    the fence actually keeps writable, or a root the fence holds read-only
    refuses the whole launch.

    Two kinds of root are sealed and so withheld here. The node's own ``/tmp``
    is sealed read-only (:func:`node_tmp_seal_root`), so a launch that names it
    writable — the ambient temp root :func:`reckon._backends.delivery_write_roots`
    adds — must not be handed it; a root beneath it, such as the run's own
    scratch or a runtime path the launch writes, is kept. The shared delivery
    stores are sealed the same way (:func:`shared_store_seal_roots`), and a
    declared path beneath one is kept.

    Each root is resolved the way the grant loop resolves it, so the two agree
    on which seal a root falls under even through a symlinked ancestry.
    """
    sealed = shared_store_seal_roots()
    node_tmp = node_tmp_seal_root()
    if node_tmp is not None:
        sealed = [*sealed, node_tmp]
    roots: list[Path] = []
    for directory in writable_directories:
        target = resolved_destination(directory)
        if any(seal == target or seal.is_relative_to(target) for seal in sealed):
            continue
        roots.append(target)
    return roots


def fence_argv(
    argv: Sequence[str],
    *,
    writable_directories: Iterable[str | Path] = (),
    worktree: str | Path | None = None,
    manifest_path: str | Path | None = None,
    home: str | Path | None = None,
    read_only_binds: Iterable[tuple[str | Path, str | Path]] = (),
    read_write_binds: Iterable[tuple[str | Path, str | Path]] = (),
    masked_paths: Iterable[str | Path] = (),
    config: Mapping[str, Any] | None = None,
) -> list[str]:
    """Wrap a launch argv so protected paths are read-only to the worker.

    The whole filesystem is dev-bind-mounted writable, each existing protected
    path is then overlaid read-only, and the run's own write roots are re-bound
    writable where they fall inside a protected path. bubblewrap applies its
    mounts in argv order, so a later bind over an earlier read-only overlay is
    what keeps a worker's run directory and worktree writable while everything
    else under the same protected root stays sealed.

    A write root is the run's declared writable directories, its worktree, and
    the run directory the manifest lives in — the last because a worker that
    cannot write its own manifest has delivered nothing. A linked worktree also
    needs its own git directory and its repository's shared object store
    writable, or a worker cannot commit the work it was dispatched to do; see
    :func:`worktree_git_write_roots`.

    A protected path is bound at its resolved target rather than at its own
    path, because bubblewrap cannot create a mount point below a symlink; see
    :func:`protected_read_only_binds`, which also waits, bounded, for a path
    that is momentarily absent while it is rewritten and refuses the launch
    for one a layer declares but never appears. The writable roots are resolved the same
    way, for two reasons: containment is tested against the paths the overlays
    actually land on, so a grant inside a symlinked protected tree is still
    re-opened writable rather than silently lost, and the grant's own
    destination has no link in it either — a grant below a symlinked directory
    would otherwise abort the launch just as a protected path did.

    ``read_only_binds`` and ``read_write_binds`` are source/destination pairs
    mounted last: a writable grant re-binds a whole subtree, so a file mounted
    underneath one is exposed correctly only when it is mounted after that
    grant. ``read_write_binds`` carry a single file the worker must be able to
    write back — the codex login, which the harness rewrites in place on a
    token refresh — and are mounted after the read-only pair so the writable
    one is the last word on its path.

    ``masked_paths`` are a run's readable surfaces to hide outright rather
    than merely seal: each existing path is unmounted by what is mounted over
    it, so a directory the run would otherwise read as its own records is
    overlaid with an empty tmpfs and a file is bound to ``/dev/null``, and the
    path reads as absent. They are emitted after the protected overlays — those
    overlay an ancestor directory and would otherwise re-expose the path they
    cover — and before the writable grants, which never contain a masked path
    because a mask is another run's surface and a grant is this run's own. A
    path that does not exist has nothing to hide and composes no mount, so a
    caller may hand the whole resolved set without filtering.

    When a writable root falls beneath the shared run-scratch root, that root is
    sealed read-only among the protected paths and created first, so the run's
    own scratch directory — re-bound writable by the ordinary grant below, which
    comes after the overlay — is the only scratch a worker can write or delete;
    see :func:`worker_scratch_seal_root`. A sibling run's scratch stays readable
    but read-only.

    The node's whole ``/tmp`` is sealed read-only as well, so a run's cleanup
    cannot reach another session's temporary files there. Only the run's own
    scratch (its ``TMPDIR``) and the runtime paths its launch writes — the
    fleet's sockets and codex's sandbox marker directory — are re-opened
    writable beneath it; each of those is created first when it is not there,
    because a path the launch creates for itself would otherwise compose no
    grant and leave its lane writing against the seal; see
    :func:`node_tmp_runtime_paths` and :func:`create_node_tmp_runtime_paths`.

    The two shared delivery stores — the reports root and the review store — are
    sealed the same way: overlaid read-only among the protected paths, with only
    the run's declared paths beneath them re-bound writable by the ordinary
    grant that follows, and each sealed store root skipped by that grant so the
    whole store is never re-opened; see :func:`shared_store_seal_roots`. A
    worker can still read every report.

    Every root the caller hands is created before the argv is composed, because
    bubblewrap binds a writable root by *source* path and refuses that root the
    same way when it cannot find it. A root this function adds itself — the
    worktree and its git directories — is not created here: a composition is
    also run for a plan that has not been dispatched yet, and a preview must
    invent nothing. Each handed root is a directory by construction, so
    declaring a file can never create a directory of that name.
    """
    protected = declared_protected_paths(home, config)
    roots: list[Path] = [Path(path) for path in writable_directories]
    if worktree is not None:
        roots.append(Path(worktree))
        roots.extend(worktree_git_write_roots(worktree))
    if manifest_path is not None:
        roots.append(Path(manifest_path).parent)
    # The scratch root is sealed read-only and the run's own scratch directory
    # is re-bound writable below, because every run's scratch sits directly
    # beneath the one shared root; see :func:`worker_scratch_seal_root`. It is
    # created before it is sealed, as every other handed root is, so the bind
    # never refuses a launch over a directory that does not exist yet.
    scratch_seal = worker_scratch_seal_root(roots)
    if scratch_seal is not None:
        try:
            scratch_seal.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise BackendError(
                f"cannot create the worker scratch root {scratch_seal} to seal "
                f"it read-only: {exc}"
            ) from exc
        protected = [*protected, scratch_seal]

    # The two shared delivery stores — the reports root and the review store —
    # are sealed read-only and only the run's declared paths beneath them are
    # re-bound writable by the grant loop below, so a worker can write and
    # delete only what it declared; see :func:`shared_store_seal_roots`. Handing
    # either root writable would re-open every other project's and run's
    # reports and reviews through the seal, so a sealed store root is skipped
    # by the grant loop. A store root is not created here: one that does not
    # exist has nothing to seal, and is not a directory the fence should invent.
    store_seals = shared_store_seal_roots()
    protected = [*protected, *store_seals]

    # The node's own ``/tmp`` is sealed read-only too, so a run's stray cleanup
    # cannot reach another session's temporary files there — Python's
    # ``tempfile`` default, pytest's ``/tmp/pytest-of-<user>``, the fleet's
    # runtime sockets; see :func:`node_tmp_runtime_paths`. The run's own scratch
    # directory (its ``TMPDIR``) and the enumerated runtime paths a launch writes
    # are the only paths re-opened writable beneath it, both by the grant loop
    # below. Composed only for a node tmp that exists, as every protected path
    # is, and held apart from the protected set so its overlay does not dedupe
    # the protected paths beneath it; see the emission of ``node_tmp_seal``.
    node_tmp = node_tmp_seal_root()
    node_tmp_seal: list[tuple[Path, Path]] = []
    if node_tmp is not None:
        node_tmp_seal = [(node_tmp, node_tmp)]
        # Created, not merely filtered: a runtime path the launch is expected to
        # create itself composes no grant while it is absent, which leaves the
        # lane it belongs to writing against the seal; see
        # :func:`create_node_tmp_runtime_paths`.
        roots.extend(create_node_tmp_runtime_paths())

    binds = protected_read_only_binds(
        protected,
        required=_declared_paths(config, PROTECTED_PATHS_KEY, home),
    )
    sealed = [destination for _source, destination in binds]
    sealed += [destination for _source, destination in node_tmp_seal]
    if worktree is not None:
        checkout = _fenced_worktree_refusal(
            Path(worktree), protected_checkouts(home, config)
        )
        if checkout is not None:
            raise BackendError(
                f"refusing to fence worktree {resolved_destination(worktree)}: it "
                f"is the protected checkout {checkout}, or a git working tree "
                "inside it, so the fence's writable re-bind would re-open the "
                "checkout it is meant to seal"
            )
    fenced = [FENCE_BINARY, "--dev-bind", "/", "/", *PID_NAMESPACE_ARGS]
    for source, destination in binds:
        fenced += ["--ro-bind", str(source), str(destination)]
    # The node tmp is emitted as its own overlay rather than folded into the
    # protected set, so it never suppresses a protected path's own bind: a
    # protected path beneath it keeps its explicit --ro-bind even though the
    # seal already covers it. A redundant read-only bind of a sub-path costs
    # bwrap nothing, and a protected path's own bind is what several readers of
    # the composed argv expect to find, the vanished-bind retry among them.
    for source, destination in node_tmp_seal:
        fenced += ["--ro-bind", str(source), str(destination)]
    # A masked path is hidden, not sealed: the protected overlay above still
    # exposes its content read-only, so a directory gets an empty tmpfs and a
    # file gets /dev/null, both of which read as absent. Emitted after the
    # protected overlays and before the writable grants, and only for a path
    # that exists — a surface that is not there holds nothing to read.
    for raw in masked_paths:
        target = resolved_destination(raw)
        if not target.exists():
            continue
        if target.is_dir():
            fenced += ["--tmpfs", str(target)]
        else:
            fenced += ["--ro-bind", os.devnull, str(target)]
    # A bind whose destination is not there yet is created by bubblewrap at
    # launch, and creating it needs a writable parent. A subscription lane's
    # credential is mounted into the run's harness home rather than seeded
    # there, so the home has to be a grant of its own: without one the mount
    # aborts the launch before the worker starts, which is what the seal
    # exposed once the harness home stopped being covered by a grant of the
    # ambient temp root above it.
    read_only_binds = list(read_only_binds)
    read_write_binds = list(read_write_binds)
    for _source, destination in (*read_only_binds, *read_write_binds):
        target = resolved_destination(destination)
        if not target.exists():
            roots.append(target.parent)

    granted: set[str] = set()
    # The handed roots minus the sealed delivery stores, resolved the way this
    # loop resolves them, so the rule that decides which roots the fence binds
    # writable is the same one the dialect is handed and an argv's ``--add-dir``
    # agrees with a bind emitted here; see :func:`fence_writable_roots`.
    for target in fence_writable_roots(roots):
        key = str(target)
        if key in granted or not _within_any(target, sealed):
            continue
        # A grant is the last word on its own path, so a handed root at or
        # above the worker scratch root would re-open it whole — the seal is
        # only ever reached through a root beneath it. The scratch root carries
        # this before the node tmp is sealed too: dispatch hands the ambient
        # ``TMPDIR`` (:func:`reckon._backends.delivery_write_roots`) as well as
        # the run's own scratch directory, and where the crew home itself sits
        # under that temp root the ambient root is an ancestor of both stores.
        if scratch_seal is not None and (
            scratch_seal == target or scratch_seal.is_relative_to(target)
        ):
            continue
        # A root that is not there holds nothing to re-open, and a bind whose
        # source is absent refuses the launch outright, so it composes no
        # grant. The caller creates the roots it means to hand before it
        # composes the fence, and a root it has not created yet is granted on
        # the composition after it does.
        if not target.exists():
            continue
        granted.add(key)
        fenced += ["--bind", key, key]
    for source, destination in read_only_binds:
        fenced += ["--ro-bind", str(source), str(destination)]
    for source, destination in read_write_binds:
        fenced += ["--bind", str(source), str(destination)]
    fenced.append("--")
    fenced += list(argv)
    return fenced
