"""Install the coordinator-obligations hook into a Claude Code settings file.

The obligations hook script binds a coordinator session to the crew duties
reckon derives for it. Binding the script to the harness means registering one
command under three harness events: ``UserPromptSubmit`` and ``SessionStart``
run it in prompt mode, and ``Stop`` runs it in stop mode.

The settings fragment is data. :func:`build_hook_snippet` composes it, and
:func:`install_hook_settings` is the surface a caller drives:

* The default call is a dry run. It builds the fragment, prints it as JSON and
  returns it, and it never opens the settings file — so a dry run cannot change
  a file that already exists.
* A merge happens only under ``write=True``, into the path the caller names
  (the user-scope ``~/.claude/settings.json`` when the caller names none). The
  merge preserves every existing key and every existing hook group, and appends
  the three entries.
* An install refuses when a command it would add already exists anywhere in the
  target's hooks, naming the event and the command it found. The refusal is
  raised before anything is written, so it leaves the file byte-identical.
* The write is atomic: the new document is composed inside the target's
  directory and moved into place, and a settings file that changed between the
  read and the write is not overwritten.

Standard-library only, and it imports neither the obligations hook nor any other
module of this package: the caller may be the CLI before the package's own
imports are exercised, and the fragment names the hook script by path.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
import time
from pathlib import Path
from typing import IO, Any

# The hook script the fragment binds. It ships beside this module, which is how
# the fragment resolves its absolute path.
HOOK_SCRIPT_NAME = "coordinator_obligations.py"

# The script's two modes and the harness events each one serves. Prompt mode
# opens every turn with the current duties; stop mode holds the turn open while
# unacknowledged duties remain.
PROMPT_MODE = "prompt"
STOP_MODE = "stop"
PROMPT_EVENTS = ("UserPromptSubmit", "SessionStart")
STOP_EVENT = "Stop"


class HookInstallError(RuntimeError):
    """Raised when a settings file cannot carry the hook fragment."""


def hook_script_path() -> Path:
    """Return the obligations hook script shipped beside this module."""
    return Path(__file__).resolve().with_name(HOOK_SCRIPT_NAME)


def user_settings_path() -> Path:
    """Return the user-scope settings file the hooks install into."""
    return Path("~/.claude/settings.json").expanduser()


def build_hook_snippet(script_path: Path | str | None = None) -> dict[str, Any]:
    """Return the settings fragment, as it would be merged, for both hook modes."""
    script = Path(script_path) if script_path is not None else hook_script_path()
    prompt_command = shlex.join([str(script), "--hook", PROMPT_MODE])
    stop_command = shlex.join([str(script), "--hook", STOP_MODE])
    entries: dict[str, Any] = {
        event: [_command_group(prompt_command)] for event in PROMPT_EVENTS
    }
    entries[STOP_EVENT] = [_command_group(stop_command)]
    return {"hooks": entries}


def install_hook_settings(
    settings_path: Path | str | None = None,
    *,
    write: bool = False,
    script_path: Path | str | None = None,
    stream: IO[str] | None = None,
) -> dict[str, Any]:
    """Print and return the hook fragment, merging it only when asked to write.

    A dry run — ``write=False``, the default — prints the fragment it would
    merge and opens no file. A merge reads ``settings_path`` (the user-scope
    settings file when none is named), refuses a duplicate command, and writes
    atomically, preserving every other key.

    The return value is always the document that was printed, so a caller can
    report on the merge without re-reading the file.
    """
    snippet = build_hook_snippet(script_path)
    if not write:
        _print(snippet, stream)
        return snippet
    target = _settings_target(settings_path)
    original = _read_bytes(target)
    merged = _merge_settings(_parse_settings(original, target), snippet, target)
    _write_settings(target, merged, original)
    _print(merged, stream)
    return merged


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
) -> dict[str, Any]:
    """Return ``settings`` plus the fragment; refuse a command already present."""
    conflict = _conflict(settings, snippet)
    if conflict is not None:
        event, command = conflict
        raise HookInstallError(
            f"refusing to install the obligations hook into {path}: "
            f"{event} already carries the command {command!r}"
        )
    existing = settings.get("hooks")
    hooks = {} if existing is None else existing
    if not isinstance(hooks, dict):
        raise HookInstallError(
            f"cannot update harness settings {path}: hooks must be an object"
        )
    updated = dict(hooks)
    for event, groups in snippet["hooks"].items():
        current = updated.get(event)
        if current is None:
            current = []
        elif not isinstance(current, list):
            raise HookInstallError(
                f"cannot update harness settings {path}: {event} must be a list"
            )
        updated[event] = [*current, *groups]
    merged = dict(settings)
    merged["hooks"] = updated
    return merged


def _conflict(
    settings: dict[str, Any], snippet: dict[str, Any]
) -> tuple[str, str] | None:
    """Return the event and command already carrying a snippet command, or None."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return None
    wanted = {
        command
        for groups in snippet["hooks"].values()
        for group in groups
        for command in _group_commands(group)
    }
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            continue
        for group in groups:
            for command in _group_commands(group):
                if command in wanted:
                    return event, command
    return None


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
