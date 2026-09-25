"""Install the coordinator-obligations hook into a Claude Code settings file.

The obligations hook script binds a coordinator session to the crew duties
reckon derives for it; the worker stop hook binds a dispatched worker's turn end
to its run manifest. Binding both to the harness means registering them under
three harness events: ``UserPromptSubmit`` and ``SessionStart`` run the
obligations hook in prompt mode, and ``Stop`` carries one entry for each hook,
the obligations hook in stop mode and the worker stop hook with no argument. The
two stop entries do not interfere: each resolves the session it belongs to, and
writes nothing for a session that is not its own.

The settings fragment is data. :func:`build_hook_snippet` composes it, and
:func:`install_hook_settings` is the surface a caller drives:

* The default call is a dry run. It builds the fragment, prints it as JSON and
  returns it, and it never opens the settings file — so a dry run cannot change
  a file that already exists.
* A merge happens only under ``write=True``, into the path the caller names
  (the user-scope ``~/.claude/settings.json`` when the caller names none). The
  merge preserves every existing key and every existing hook group, and adds the
  fragment's entries.
* An entry already registered under its own event is skipped, not duplicated:
  the existing entry keeps its bytes exactly as the operator wrote it, and every
  other entry is still added. So installing over a settings file that already
  carries one hook of the fragment — the worker stop hook, say — still binds the
  rest, and a second install of the whole fragment changes nothing at all. The
  result names both lists, so a caller can report what it did without comparing
  documents itself.
* The write is atomic: the new document is composed inside the target's
  directory and moved into place, and a settings file that changed between the
  read and the write is not overwritten.

The obligations hook imports this package, so its command names the interpreter
that can import it: the checkout's own ``.venv/bin/python``, resolved beside
this module. Launched instead through the script's own ``env python3`` shebang
the hook fails with ``No module named 'reckon'`` and does nothing, so the
command is what binds the hook to the package it reads its duties from. The
worker stop hook is standard-library only and keeps its bare script command.

The merge counts an entry whose command runs the same hook script, with the same
arguments, as registered whatever interpreter launches it: a settings file
installed before the interpreter was added is not given a second copy.

Standard-library only, and it imports neither hook script nor any other module
of this package: the caller may be the CLI before the package's own imports are
exercised, and the fragment names each hook script by path.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

# The two hook scripts the fragment binds. Both ship beside this module, which
# is how the fragment resolves each absolute path: the snippet names the scripts
# of the checkout that composed it.
COORDINATOR_HOOK_SCRIPT_NAME = "coordinator_obligations.py"
WORKER_STOP_SCRIPT_NAME = "worker_stop.py"

# The obligations hook's two modes and the harness events each one serves.
# Prompt mode opens every turn with the current duties; stop mode holds the turn
# open while unacknowledged duties remain. The worker stop hook takes no mode:
# it resolves the run it belongs to from the environment and the payload's
# working directory.
PROMPT_MODE = "prompt"
STOP_MODE = "stop"
PROMPT_EVENTS = ("UserPromptSubmit", "SessionStart")
STOP_EVENT = "Stop"

# What identifies a command as one of this fragment's entries: the hook script it
# runs, whatever interpreter launches it and however the command is quoted.
HOOK_SCRIPT_NAMES = (COORDINATOR_HOOK_SCRIPT_NAME, WORKER_STOP_SCRIPT_NAME)


class HookInstallError(RuntimeError):
    """Raised when a settings file cannot carry the hook fragment."""


@dataclass(frozen=True)
class HookInstallResult:
    """What an install composed, and what it did to each entry.

    ``document`` is the JSON that was printed and, when anything was added,
    written. ``added`` and ``skipped`` name each entry as ``"<event>: <command>"``.
    A dry run reports every entry as one a merge would add: it reads no file, so
    it cannot know of an entry already installed.
    """

    document: dict[str, Any]
    added: tuple[str, ...]
    skipped: tuple[str, ...]


def hook_script_path() -> Path:
    """Return the obligations hook script shipped beside this module."""
    return Path(__file__).resolve().with_name(COORDINATOR_HOOK_SCRIPT_NAME)


def worker_stop_script_path() -> Path:
    """Return the worker stop hook script shipped beside this module."""
    return Path(__file__).resolve().with_name(WORKER_STOP_SCRIPT_NAME)


def interpreter_path() -> Path:
    """Return the interpreter the coordinator hook commands run under.

    Resolved from the checkout that carries this module, so the command names
    the environment of the same repository as the script it launches: the
    obligations hook imports this package, and no other interpreter resolves
    that import.
    """
    return Path(__file__).resolve().parents[2] / ".venv" / "bin" / "python"


def user_settings_path() -> Path:
    """Return the user-scope settings file the hooks install into."""
    return Path("~/.claude/settings.json").expanduser()


def build_hook_snippet(script_path: Path | str | None = None) -> dict[str, Any]:
    """Return the settings fragment, as it would be merged, for every hook script."""
    script = Path(script_path) if script_path is not None else hook_script_path()
    interpreter = str(interpreter_path())
    prompt_command = shlex.join([interpreter, str(script), "--hook", PROMPT_MODE])
    stop_command = shlex.join([interpreter, str(script), "--hook", STOP_MODE])
    worker_stop_command = shlex.join([str(worker_stop_script_path())])
    entries: dict[str, Any] = {
        event: [_command_group(prompt_command)] for event in PROMPT_EVENTS
    }
    entries[STOP_EVENT] = [
        _command_group(stop_command),
        _command_group(worker_stop_command),
    ]
    return {"hooks": entries}


def install_hook_settings(
    settings_path: Path | str | None = None,
    *,
    write: bool = False,
    script_path: Path | str | None = None,
    stream: IO[str] | None = None,
) -> HookInstallResult:
    """Print the hook fragment, merging it only when asked to write.

    A dry run — ``write=False``, the default — prints the fragment it would
    merge and opens no file, so it cannot tell an entry already installed from
    one that is not and reports every entry as one a merge would add.

    A merge reads ``settings_path`` (the user-scope settings file when none is
    named), appends every entry not already registered under its own event, and
    writes atomically, preserving every other key. A merge with nothing to add
    writes nothing at all, leaving the file's bytes and its mode exactly as
    they were.

    The result carries the document that was printed and the two entry lists,
    so a caller can report what the merge did without comparing documents.
    """
    snippet = build_hook_snippet(script_path)
    if not write:
        _print(snippet, stream)
        return HookInstallResult(
            document=snippet, added=tuple(_entry_labels(snippet)), skipped=()
        )
    target = _settings_target(settings_path)
    original = _read_bytes(target)
    merged, added, skipped = _merge_settings(
        _parse_settings(original, target), snippet, target
    )
    if added:
        _write_settings(target, merged, original)
    _print(merged, stream)
    return HookInstallResult(
        document=merged, added=tuple(added), skipped=tuple(skipped)
    )


def _settings_target(settings_path: Path | str | None) -> Path:
    source = user_settings_path() if settings_path is None else Path(settings_path)
    return source.expanduser()


def _command_group(command: str) -> dict[str, Any]:
    return {"hooks": [{"type": "command", "command": command}]}


def _print(payload: dict[str, Any], stream: IO[str] | None) -> None:
    out = stream if stream is not None else sys.stdout
    print(_render(payload), file=out)


def _render(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise HookInstallError(f"cannot read harness settings {path}: {exc}") from exc


def _parse_settings(original: bytes | None, path: Path) -> dict[str, Any]:
    if original is None:
        return {}
    try:
        loaded = json.loads(original)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HookInstallError(f"cannot parse harness settings {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise HookInstallError(
            f"cannot update harness settings {path}: root must be an object"
        )
    return loaded


def _merge_settings(
    settings: dict[str, Any], snippet: dict[str, Any], path: Path
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Return ``settings``, the entries added and the entries skipped.

    An entry is skipped when every command it carries is already registered
    under the same event: the groups already holding them are left exactly as
    they are, and only the entries the file lacks are appended. An event the
    fragment does not name is untouched, whatever it holds.
    """
    existing = settings.get("hooks")
    hooks = {} if existing is None else existing
    if not isinstance(hooks, dict):
        raise HookInstallError(
            f"cannot update harness settings {path}: hooks must be an object"
        )
    updated = dict(hooks)
    added: list[str] = []
    skipped: list[str] = []
    for event, groups in snippet["hooks"].items():
        current = updated.get(event)
        if current is None:
            current = []
        elif not isinstance(current, list):
            raise HookInstallError(
                f"cannot update harness settings {path}: {event} must be a list"
            )
        registered = _registered_identities(current)
        kept = list(current)
        for group in groups:
            commands = _group_commands(group)
            labels = [f"{event}: {command}" for command in commands]
            identities = [_command_identity(command) for command in commands]
            if commands and all(identity in registered for identity in identities):
                skipped.extend(labels)
                continue
            kept.append(group)
            added.extend(labels)
        updated[event] = kept
    merged = dict(settings)
    merged["hooks"] = updated
    return merged, added, skipped


def _entry_labels(snippet: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    for event, groups in snippet["hooks"].items():
        for group in groups:
            labels.extend(f"{event}: {command}" for command in _group_commands(group))
    return labels


def _registered_identities(groups: list[Any]) -> set[str]:
    """Return the identity of every hook already registered under one event."""
    return {
        _command_identity(command)
        for group in groups
        for command in _group_commands(group)
    }


def _command_identity(command: str) -> str:
    """Return what the command runs, past the interpreter that launches it.

    Which interpreter launches a hook is how the fragment was composed rather
    than which hook the command is, so the interpreter is not part of a
    command's identity when a hook script of this fragment follows it: the form
    this module now writes and the bare form an earlier install left in a
    settings file are one entry, and quoting is normalised with them.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return command
    if len(tokens) >= 2 and Path(tokens[1]).name in HOOK_SCRIPT_NAMES:
        tokens = tokens[1:]
    return shlex.join(tokens) if tokens else command


def _group_commands(group: Any) -> list[str]:
    if not isinstance(group, dict):
        return []
    hooks = group.get("hooks")
    if not isinstance(hooks, list):
        return []
    return [
        str(hook["command"])
        for hook in hooks
        if isinstance(hook, dict) and hook.get("command") is not None
    ]


def _write_settings(
    path: Path, payload: dict[str, Any], original: bytes | None
) -> None:
    """Write the document atomically, refusing to overwrite a concurrent edit."""
    encoded = (_render(payload) + "\n").encode()
    if original is None:
        if path.exists():
            raise HookInstallError(
                f"harness settings appeared while being updated: {path}"
            )
        mode = None
    else:
        try:
            current = path.read_bytes()
        except OSError as exc:
            raise HookInstallError(
                f"cannot re-read harness settings {path}: {exc}"
            ) from exc
        if current != original:
            raise HookInstallError(
                f"harness settings changed while being updated: {path}"
            )
        mode = path.stat().st_mode

    temporary = path.with_name(f".{path.name}.reckon-{os.getpid()}-{time.time_ns()}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_bytes(encoded)
        if mode is not None:
            temporary.chmod(mode)
        os.replace(temporary, path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise HookInstallError(f"cannot write harness settings {path}: {exc}") from exc
