"""Whether a shadow's stream read the primary's answer it is measured against.

A shadow dispatched against a primary that already merged shares the object
store with the integration branch, so its stream can read the landed commit,
the primary's changed files where they are checked out in the main checkout,
the primary's run directory, its review records, or the worktree it held --
without leaving any footprint in the ledger row. The row cannot say which
shadow was void, so the promotion path scans the stream here and marks the
row, and the mark excludes it from every calibration slice.

:func:`stream_tool_calls` reads a run's ordered JSONL streams in either
backend dialect. :func:`shadow_primary_read` decides whether any call resolves
the primary's answer and names the single most specific reason the stream
shows.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path

# Git subcommands that read repository content or history. Naming a shadow
# primary's landed commit with one of these reveals the answer the shadow is
# measured against. The content-revealing subset additionally gates a
# changed-path read: naming a primary file with a bare status or ref query
# resolves nothing about its content, so it is not a read of the answer.
_SHADOW_GIT_READ_VERBS = frozenset(
    {
        "show",
        "log",
        "diff",
        "cat-file",
        "blame",
        "rev-list",
        "ls-tree",
        "archive",
        "grep",
        "rev-parse",
        "describe",
        "merge-base",
    }
)
_SHADOW_GIT_CONTENT_READ_VERBS = frozenset(
    {"show", "log", "diff", "cat-file", "blame", "ls-tree", "archive"}
)
# A git object abbreviation is a naming from seven hex digits up. Matching the
# full sha and every prefix of that length keeps ``git show 7572d83`` (the
# abbreviation a landed commit is routinely cited by) on an equal footing with
# its full form, while a shorter run of hex is too likely to be fragmentary
# to read as a reference.
_SHADOW_SHA_PREFIX_MIN = 7
# Tool names whose payload is a file path rather than a command line.
_SHADOW_PATH_CALL_KINDS = frozenset({"read", "grep", "glob"})


def _sha_prefixes(commit: str) -> tuple[str, ...]:
    """Return every abbreviation of a commit sha that names it as a target."""
    sha = str(commit or "").strip()
    if not re.fullmatch(r"[0-9a-f]+", sha) or len(sha) <= _SHADOW_SHA_PREFIX_MIN:
        return (sha,) if sha else ()
    return tuple(sha[:length] for length in range(_SHADOW_SHA_PREFIX_MIN, len(sha) + 1))


def stream_tool_calls(paths: Iterable[str | Path]) -> list[tuple[str, str]]:
    """Extract a run's tool calls from its ordered JSONL streams.

    Bash invocations become ``("bash", command)``; file-path tools become
    ``("read", path)`` and friends, so a caller can distinguish a read of a
    file from a command that merely mentions it. An unparsable line or a call
    without a payload is skipped: a malformed record answers nothing rather
    than poisoning the whole stream.

    Two stream dialects are read, because the backends record a shell call
    differently and a scan written for one sees nothing in the other. A message
    stream carries tool calls as ``message.content`` blocks, a bash call under
    the ``Bash`` tool and a file read under a path tool. A codex stream carries
    them as completed items, a shell call as a ``command_execution`` whose
    ``command`` is the same text a bash call would hold. Reading only the first
    dialect leaves every codex run unscanned and silently clean.
    """
    calls: list[tuple[str, str]] = []
    for path in paths:
        with Path(path).open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if not isinstance(event, Mapping):
                    continue
                item = event.get("item")
                if (
                    event.get("type") == "item.completed"
                    and isinstance(item, Mapping)
                    and str(item.get("type") or "") == "command_execution"
                    and str(item.get("command") or "").strip()
                ):
                    calls.append(("bash", str(item["command"]).strip()))
                    continue
                message = event.get("message")
                if not isinstance(message, Mapping):
                    continue
                content = message.get("content")
                if not isinstance(content, list):
                    continue
                for item in content:
                    if not isinstance(item, Mapping):
                        continue
                    name = str(item.get("name") or "").strip()
                    payload = item.get("input")
                    if not isinstance(payload, Mapping):
                        continue
                    if name == "Bash" and str(payload.get("command") or "").strip():
                        calls.append(("bash", str(payload["command"]).strip()))
                    elif (
                        name.lower() in _SHADOW_PATH_CALL_KINDS
                        and str(payload.get("file_path") or "").strip()
                    ):
                        calls.append((name.lower(), str(payload["file_path"]).strip()))
    return calls


def shadow_primary_read(
    calls: Iterable[tuple[str, str]],
    *,
    primary_commit: str,
    primary_paths: Iterable[str] = (),
    repo_root: str = "",
    worktree: str = "",
    primary_run_dir: str = "",
    primary_review_paths: Iterable[str] = (),
    primary_worktree: str = "",
) -> str | None:
    """Return how a shadow's tool calls read its primary's answer, or None.

    Five reads give the answer away. Naming the primary's landed commit in a
    git read (the object store is shared with the integration branch, so the
    commit resolves from anywhere) and reading a primary changed file where it
    is checked out at that landed state — the main checkout — are the two the
    landed result itself betrays. The other three are the primary's own
    records, which hold its reasoning and its numbers rather than its files:
    its run directory (manifest, prompt, stream), its review records, and its
    worktree. A change to a shared path that merely mentions it (running the
    primary's own tests on the shadow's copy of that file, or reading the
    shadow's own worktree via a ``git -C`` against it) resolves nothing about
    the landed answer and never triggers. The main-checkout context is decided
    per git invocation: an explicit ``-C <dir>`` wins over the working
    directory the stream's earlier commands left behind, so a changed-path read
    that names the worktree stays clean even when the command also contains a
    stray ``cd`` into the main checkout.

    The verdict names the single most specific reason the stream shows, and is
    decided by reason rather than by the call order the stream happens to
    record: a stream that reads both a changed path and the primary's run
    directory is a changed-path read. An empty string for any surface means the
    caller could not resolve it and it is not looked for.
    """
    prefixes = _sha_prefixes(str(primary_commit or "").strip())
    paths = tuple(str(path).strip() for path in primary_paths if str(path).strip())
    main_root = str(repo_root or "").strip().rstrip("/")
    work_root = str(worktree or "").strip().rstrip("/")
    run_directory = str(primary_run_dir or "").strip().rstrip("/")
    review_records = tuple(
        str(path).strip() for path in primary_review_paths if str(path).strip()
    )
    primary_work = str(primary_worktree or "").strip().rstrip("/")

    found: set[str] = set()
    cwd = ""
    variables: dict[str, str] = {}
    for kind, payload in calls:
        if kind == "bash":
            cwd, variables = _absorb_shell_state(payload, cwd, variables)
            if _command_reads_primary_commit(payload, prefixes):
                found.add("primary_commit_read")
            if _command_reads_primary_path(
                payload,
                paths,
                main_root=main_root,
                cwd=cwd,
                variables=variables,
            ):
                found.add("primary_changed_path_read")
            if run_directory and _command_references_under(
                payload, run_directory, variables
            ):
                found.add("primary_run_directory_read")
            if review_records and _command_references_any(
                payload, review_records, variables
            ):
                found.add("primary_review_read")
            if primary_work and _command_references_under(
                payload, primary_work, variables, exclude=work_root
            ):
                found.add("primary_worktree_read")
        elif kind in _SHADOW_PATH_CALL_KINDS:
            if _path_reads_primary_changed(
                payload, paths, main_root=main_root, work_root=work_root
            ):
                found.add("primary_changed_path_read")
            if run_directory and _path_under(payload, run_directory):
                found.add("primary_run_directory_read")
            if any(_path_under(payload, record) for record in review_records):
                found.add("primary_review_read")
            if (
                primary_work
                and _path_under(payload, primary_work)
                and not (work_root and _path_under(payload, work_root))
            ):
                found.add("primary_worktree_read")
    for reason in _SHADOW_READ_REASON_PRIORITY:
        if reason in found:
            return reason
    return None


# The specific-read reasons, most specific first. A stream that shows several
# is named by the first: a copy of the landed result is a stronger statement
# than a read of the records around it.
_SHADOW_READ_REASON_PRIORITY = (
    "primary_commit_read",
    "primary_changed_path_read",
    "primary_run_directory_read",
    "primary_review_read",
    "primary_worktree_read",
)


def _path_under(path: str, root: str) -> bool:
    """Whether ``path`` names ``root`` itself or a path beneath it."""
    return bool(root) and (path == root or path.startswith(root + "/"))


def _expand_variables(text: str, variables: Mapping[str, str]) -> str:
    """Substitute tracked shell variables, leaving unknown ones in place."""
    if not variables:
        return text
    return re.sub(
        r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?",
        lambda match: variables.get(match.group(1), match.group(0)),
        text,
    )


# Characters that continue a path name, so a match ending on one of them is a
# different path and must not read as the target: ``.../r-x`` must not match
# inside ``.../r-x-extra``.
_PATH_NAME_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
)


def _text_references_under(text: str, root: str) -> bool:
    """Whether ``text`` names ``root`` at a path boundary."""
    if not root:
        return False
    start = 0
    while True:
        index = text.find(root, start)
        if index < 0:
            return False
        following = text[index + len(root) : index + len(root) + 1]
        if following == "" or following not in _PATH_NAME_CHARS:
            return True
        start = index + 1


def _command_references_under(
    command: str,
    root: str,
    variables: Mapping[str, str],
    *,
    exclude: str = "",
) -> bool:
    """Whether a shell command names a path under ``root``.

    ``exclude`` is the caller's own root: a command that names a path the
    caller's own root contains is reading its own copy, not the caller's peer,
    so a target that lives under the excluded root never reads as a peer's.
    """
    if not root or (exclude and root.startswith(exclude + "/")):
        return False
    return _text_references_under(_expand_variables(command, variables), root)


def _command_references_any(
    command: str, targets: tuple[str, ...], variables: Mapping[str, str]
) -> bool:
    """Whether a shell command names any of ``targets`` at a path boundary."""
    expanded = _expand_variables(command, variables)
    return any(_text_references_under(expanded, target) for target in targets)


_CD_TARGET = re.compile(r"(?:^|&&|;|\||\n)\s*cd\s+([^\s;&|]+)")
_VAR_ASSIGN = re.compile(r"(?:^|;|&&|\s)\s*([A-Za-z_][A-Za-z0-9_]*)=(/[\S]+)")


def _expand_path(token: str, variables: Mapping[str, str]) -> str:
    """Resolve a path token that may reference a shell variable, or ''."""
    expanded = re.sub(
        r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?",
        lambda match: variables.get(match.group(1), ""),
        token.strip(),
    )
    return expanded.strip("\"'")


def _absorb_shell_state(
    command: str, cwd: str, variables: Mapping[str, str]
) -> tuple[str, dict[str, str]]:
    """Fold a command's ``cd`` and path assignments into tracked shell state."""
    tracked = dict(variables)
    for match in _VAR_ASSIGN.finditer(command):
        tracked[match.group(1)] = match.group(2).strip("\"'")
    for match in _CD_TARGET.finditer(command):
        resolved = _expand_path(match.group(1), tracked)
        if resolved.startswith("/"):
            cwd = resolved.rstrip("/")
    return cwd, tracked


# One git invocation: an optional ``git -C <dir>`` prefix, a verb, and the
# rest. The rest stops at a command separator so a later pipe or join cannot
# smuggle an unrelated path into a read it does not target.
_GIT_INVOCATION = re.compile(
    r"\bgit(?:\s+-C\s+([^\s;&|]+))?\s+([a-z][a-z-]*)([^;&|\n]*)"
)


def _git_invocations(
    command: str, variables: Mapping[str, str] | None = None
) -> list[tuple[str, str, str]]:
    """Return (verb, resolved -C directory or '', argument-text) per git call."""
    known = variables or {}
    resolved: list[tuple[str, str, str]] = []
    for match in _GIT_INVOCATION.finditer(command):
        target = "" if match.group(1) is None else match.group(1)
        if target:
            target = _expand_path(target, known)
            if not target.startswith("/"):
                target = ""
        resolved.append((match.group(2), target, match.group(3)))
    return resolved


def _command_reads_primary_commit(
    command: str, prefixes: tuple[str, ...], variables: Mapping[str, str] | None = None
) -> bool:
    if not prefixes:
        return False
    prefix = r"\b" + re.escape(prefixes[0])
    for verb, _target, args in _git_invocations(command, variables):
        if verb in _SHADOW_GIT_READ_VERBS and re.search(prefix, args):
            return True
    return False


def _command_reads_primary_path(
    command: str,
    paths: tuple[str, ...],
    *,
    main_root: str,
    cwd: str,
    variables: Mapping[str, str],
) -> bool:
    if not paths or not main_root:
        return False
    for verb, target, args in _git_invocations(command, variables):
        if verb not in _SHADOW_GIT_CONTENT_READ_VERBS:
            continue
        context = target or cwd
        if context != main_root:
            continue
        if _path_tokens_matching(args, paths):
            return True
    return False


def _path_tokens_matching(args: str, paths: tuple[str, ...]) -> bool:
    """Whether a git argument list names a changed path as a target.

    A path is named when it is an argument on its own, or a ``revision:path``
    object reference whose path half equals it. An argument that merely embeds
    the path inside another directory (``HEAD:$W/reckon/crew/recovery.py`` for
    a worktree variable) names a different object and never matches.
    """
    for raw in args.split():
        token = raw.strip("\"'")
        candidate = token.split(":", 1)[1] if ":" in token else token
        if candidate in paths:
            return True
    return False


def _path_reads_primary_changed(
    path: str,
    paths: tuple[str, ...],
    *,
    main_root: str,
    work_root: str,
) -> bool:
    if not paths or not main_root:
        return False
    if work_root and path.startswith(work_root + "/"):
        return False
    if not path.startswith(main_root + "/"):
        return False
    relative = path[len(main_root) :].lstrip("/")
    return relative in paths
