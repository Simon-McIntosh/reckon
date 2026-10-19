from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from reckon import metadata_index
from reckon._store import write_json_atomically


@dataclass(frozen=True)
class _GitCreationEntry:
    head: str
    times: dict[str, int]


_GIT_CREATION_CACHE: dict[tuple[str, str], _GitCreationEntry] = {}
_GIT_CREATION_SCHEMA = "reckon.git-creation-map"
_GIT_CREATION_SCHEMA_VERSION = 1


def _git_creation_cache_path(cache_key: tuple[str, str]) -> Path:
    """Return the disposable persisted-map path for one repository docs root."""

    return metadata_index._persisted_cache_path(
        "git-creation", _GIT_CREATION_SCHEMA_VERSION, *cache_key
    )


def _git_creation_payload(cache_key: tuple[str, str], entry: _GitCreationEntry) -> dict:
    repo, rel_docs = cache_key
    core = {
        "schema": _GIT_CREATION_SCHEMA,
        "version": _GIT_CREATION_SCHEMA_VERSION,
        "repository": repo,
        "docs": rel_docs,
        "head": entry.head,
        "times": entry.times,
    }
    checksum = hashlib.sha256(
        json.dumps(core, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {**core, "checksum": checksum}


def _load_git_creation_cache(
    cache_key: tuple[str, str],
) -> _GitCreationEntry | None:
    """Load a complete, current-schema persisted map or decline it entirely."""
    from reckon.serve import LOGGER

    if not (Path(cache_key[0]) / ".git").exists():
        return None
    path = _git_creation_cache_path(cache_key)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("Ignoring unreadable Git creation cache %s: %s", path, exc)
        return None
    if not isinstance(raw, dict):
        LOGGER.warning("Ignoring invalid Git creation cache %s", path)
        return None

    times = raw.get("times")
    valid_times = isinstance(times, dict) and all(
        isinstance(name, str)
        and isinstance(timestamp, int)
        and not isinstance(timestamp, bool)
        and timestamp >= 0
        for name, timestamp in times.items()
    )
    valid_identity = (
        raw.get("schema") == _GIT_CREATION_SCHEMA
        and raw.get("version") == _GIT_CREATION_SCHEMA_VERSION
        and raw.get("repository") == cache_key[0]
        and raw.get("docs") == cache_key[1]
        and isinstance(raw.get("head"), str)
        and bool(raw["head"])
    )
    if not valid_times or not valid_identity:
        LOGGER.warning("Ignoring incompatible Git creation cache %s", path)
        return None

    core = {key: raw[key] for key in raw if key != "checksum"}
    checksum = hashlib.sha256(
        json.dumps(core, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if raw.get("checksum") != checksum:
        LOGGER.warning("Ignoring corrupt Git creation cache %s", path)
        return None
    return _GitCreationEntry(head=raw["head"], times=dict(times))


def _store_git_creation_cache(
    cache_key: tuple[str, str], entry: _GitCreationEntry
) -> None:
    """Atomically persist one validated creation map without affecting service."""
    from reckon.serve import LOGGER

    if not (Path(cache_key[0]) / ".git").exists():
        return
    path = _git_creation_cache_path(cache_key)
    try:
        write_json_atomically(
            path,
            _git_creation_payload(cache_key, entry),
            indent=2,
            sort_keys=True,
            fsync=False,
            mode=0o600,
        )
    except OSError as exc:
        LOGGER.warning("Could not persist Git creation cache %s: %s", path, exc)


def _run_git(
    args: list[str],
    repo_dir: Path,
    *,
    operation: str,
    report_failure: bool = False,
) -> subprocess.CompletedProcess[str] | None:
    """Run one bounded Git query and make every failure observable.

    The spawn runs in the C locale whatever the caller's environment asks for,
    so a caller that has to read git's own message — to tell git's verdict
    about the tree apart from a failure — matches one text wherever the server
    runs.

    A non-zero exit returns ``None``, as it always has. A caller that needs the
    exit code and the stderr to classify that exit sets ``report_failure`` and
    receives the completed process instead. A timeout and a spawn error are
    answers about the machine rather than about the repository and stay
    ``None`` for every caller, so neither can be mistaken for git's verdict.
    """
    from reckon.serve import LOGGER

    environment = {**os.environ, "LC_ALL": "C"}
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            cwd=repo_dir,
            timeout=10,
            env=environment,
            check=False,
        )
    except subprocess.TimeoutExpired:
        LOGGER.warning("Git %s timed out after 10 seconds in %s", operation, repo_dir)
        return None
    except OSError as exc:
        LOGGER.warning("Git %s could not start in %s: %s", operation, repo_dir, exc)
        return None
    if result.returncode != 0:
        detail = result.stderr.strip()
        suffix = f": {detail}" if detail else ""
        LOGGER.warning(
            "Git %s failed with exit code %d in %s%s",
            operation,
            result.returncode,
            repo_dir,
            suffix,
        )
        return result if report_failure else None
    return result


_GIT_HEAD_MEMO: dict[str, tuple[tuple, str]] = {}


def _git_directory(repo_dir: Path) -> Path | None:
    """A checkout's git directory, following the pointer a worktree writes.

    A normal checkout's ``.git`` is the directory itself; a linked worktree's
    is a file naming it. Reading both forms here keeps the HEAD memo working
    whether the docs tree is served from a main checkout or a worktree.
    """

    marker = repo_dir / ".git"
    if marker.is_dir():
        return marker
    try:
        text = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    prefix = "gitdir:"
    if not text.startswith(prefix):
        return None
    target = Path(text[len(prefix) :].strip())
    if not target.is_absolute():
        target = repo_dir / target
    return target


def _file_identity(path: Path) -> tuple[int, int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_ino, stat.st_size)


def _common_git_directory(git_dir: Path) -> Path:
    """The git directory that holds a worktree's shared refs.

    A linked worktree's own git directory names the main checkout's git
    directory in ``commondir``; branch refs live there rather than in the
    worktree's private directory. Without following it a worktree's ref file
    reads as absent, so a new commit would leave the memo's sha frozen.
    """

    try:
        text = (git_dir / "commondir").read_text(encoding="utf-8").strip()
    except OSError:
        return git_dir
    if not text:
        return git_dir
    target = Path(text)
    if not target.is_absolute():
        target = git_dir / target
    return target


def _head_identity(repo_dir: Path) -> tuple | None:
    """An identity that changes exactly when the resolved commit changes.

    Reads the git directory's ``HEAD`` and the reference file it names as
    files rather than by running git, so a warm lookup proves the commit has
    not moved without starting a process. The reference file's presence is
    part of the identity, so a loose ref written by a new commit changes it
    even when ``HEAD`` itself does not. A linked worktree resolves its ref
    through the common git directory, where its shared refs actually live.
    ``None`` means the identity cannot be read, and a caller must resolve the
    head itself.
    """

    git_dir = _git_directory(repo_dir)
    if git_dir is None:
        return None
    head_path = git_dir / "HEAD"
    try:
        head_text = head_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    ref_name = head_text[4:].strip() if head_text.startswith("ref:") else ""
    ref_identity = (
        _file_identity(_common_git_directory(git_dir) / ref_name) if ref_name else None
    )
    return (_file_identity(head_path), head_text, ref_name, ref_identity)


#: Roots git has answered as outside any repository, keyed by path to the
#: directory stamp at which that answer was given. A docs tree served from an
#: archive, an extraction or a mount without its repository pays one failed
#: spawn to learn that, then every later age and HEAD lookup returns no commit
#: without starting git again.
_GIT_OUTSIDE_MEMO: dict[str, tuple[int, int, int]] = {}


def _root_outside_repository(repo_dir: Path) -> bool:
    """True when this root was confirmed outside any repository at its stamp.

    The stamp is the directory's own identity, so a ``.git`` later created at
    the root moves it and the next lookup resolves afresh rather than reusing
    a remembered answer.
    """

    stamp = _file_identity(repo_dir)
    if stamp is None:
        return False
    return _GIT_OUTSIDE_MEMO.get(str(repo_dir)) == stamp


def _remember_root_outside_repository(repo_dir: Path) -> None:
    """Record a root as outside any repository, keyed by its directory stamp."""

    if _git_directory(repo_dir) is not None:
        return
    stamp = _file_identity(repo_dir)
    if stamp is not None:
        _GIT_OUTSIDE_MEMO[str(repo_dir)] = stamp


def _git_says_outside_repository(result: subprocess.CompletedProcess[str]) -> bool:
    """True when a failed exit is git's own answer that no repository is here.

    The message is matched as the C locale spells it, which every spawn asks
    for, so the answer does not depend on the operator's language and a
    translated message cannot be read as a different failure.
    """

    return "not a git repository" in (result.stderr or "").lower()


def _git_head(repo_dir: Path) -> str | None:
    """The repository's HEAD commit, resolved once per commit.

    The sha is memoised on the identity of ``HEAD`` and the ref it names, so
    repeated lookups against an unchanged checkout reuse the first answer and
    start no git process. A new commit changes that identity, and the next
    lookup resolves afresh. When git answers that the root is under no
    repository at all, that answer is remembered, so a tree outside any
    repository resolves once and every later age and HEAD lookup returns no
    commit without spawning again. Any other failure — a timeout, a spawn
    error, or an exit saying something else — is not an answer about the tree
    and is never remembered.
    """
    from reckon.serve import LOGGER

    key = str(repo_dir)
    identity = _head_identity(repo_dir)
    if identity is not None:
        memo = _GIT_HEAD_MEMO.get(key)
        if memo is not None and memo[0] == identity:
            return memo[1]
    elif _root_outside_repository(repo_dir):
        return None
    result = _run_git(
        ["git", "rev-parse", "HEAD"],
        repo_dir,
        operation="HEAD lookup",
        report_failure=True,
    )
    if result is None or result.returncode != 0:
        if result is not None and _git_says_outside_repository(result):
            _remember_root_outside_repository(repo_dir)
        return None
    head = result.stdout.strip()
    if not head:
        LOGGER.warning("Git HEAD lookup returned no commit in %s", repo_dir)
        return None
    if identity is not None:
        _GIT_HEAD_MEMO[key] = (identity, head)
    return head


def _parse_first_committed(output: str) -> dict[str, int]:
    times: dict[str, int] = {}
    timestamp: int | None = None
    for raw in output.splitlines():
        line = raw.strip()
        if line.startswith("COMMIT "):
            try:
                timestamp = int(line[7:])
            except ValueError:
                timestamp = None
        elif line and timestamp is not None:
            # Git emits newest commits first, so the last add event is the first.
            times[line] = timestamp
    return times


def _git_first_committed(repo_dir: Path, docs_dir: Path) -> dict[str, int]:
    """Return {repo-relative-path: unix_ts} for the first commit of each HTML file."""
    from reckon.serve import LOGGER

    try:
        rel_docs = str(docs_dir.relative_to(repo_dir))
    except ValueError:
        LOGGER.warning("Docs directory %s is outside repository %s", docs_dir, repo_dir)
        return {}

    cache_key = (str(repo_dir.resolve()), rel_docs)
    cached = _GIT_CREATION_CACHE.get(cache_key)
    if cached is None:
        cached = _load_git_creation_cache(cache_key)
        if cached is not None:
            _GIT_CREATION_CACHE[cache_key] = cached
    head = _git_head(repo_dir)
    if head is None:
        return dict(cached.times) if cached else {}
    if cached and cached.head == head:
        return dict(cached.times)

    args = ["git", "log"]
    if cached:
        args.append(f"{cached.head}..{head}")
    args.extend(
        [
            "--diff-filter=A",
            "--format=COMMIT %at",
            "--name-only",
            "--",
            rel_docs,
        ]
    )
    result = _run_git(args, repo_dir, operation="history lookup")
    if result is None:
        return dict(cached.times) if cached else {}

    times = dict(cached.times) if cached else {}
    additions = _parse_first_committed(result.stdout)
    for path, timestamp in additions.items():
        times.setdefault(path, timestamp)
    entry = _GitCreationEntry(head=head, times=times)
    _GIT_CREATION_CACHE[cache_key] = entry
    _store_git_creation_cache(cache_key, entry)
    return dict(times)


_GIT_LAST_MODIFIED_CACHE: dict[tuple[str, str], _GitCreationEntry] = {}
_GIT_LAST_MODIFIED_SCHEMA = "reckon.git-last-modified-map"
_GIT_LAST_MODIFIED_SCHEMA_VERSION = 1


def _git_last_modified_cache_path(cache_key: tuple[str, str]) -> Path:
    return metadata_index._persisted_cache_path(
        "git-last-modified", _GIT_LAST_MODIFIED_SCHEMA_VERSION, *cache_key
    )


def _git_last_modified_payload(
    cache_key: tuple[str, str], entry: _GitCreationEntry
) -> dict:
    repo, rel_docs = cache_key
    core = {
        "schema": _GIT_LAST_MODIFIED_SCHEMA,
        "version": _GIT_LAST_MODIFIED_SCHEMA_VERSION,
        "repository": repo,
        "docs": rel_docs,
        "head": entry.head,
        "times": entry.times,
    }
    checksum = hashlib.sha256(
        json.dumps(core, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {**core, "checksum": checksum}


def _load_git_last_modified_cache(
    cache_key: tuple[str, str],
) -> _GitCreationEntry | None:
    """Load a complete, current-schema persisted map or decline it entirely."""
    from reckon.serve import LOGGER

    if not (Path(cache_key[0]) / ".git").exists():
        return None
    path = _git_last_modified_cache_path(cache_key)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("Ignoring unreadable Git last-modified cache %s: %s", path, exc)
        return None
    if not isinstance(raw, dict):
        LOGGER.warning("Ignoring invalid Git last-modified cache %s", path)
        return None

    times = raw.get("times")
    valid_times = isinstance(times, dict) and all(
        isinstance(name, str)
        and isinstance(timestamp, int)
        and not isinstance(timestamp, bool)
        and timestamp >= 0
        for name, timestamp in times.items()
    )
    valid_identity = (
        raw.get("schema") == _GIT_LAST_MODIFIED_SCHEMA
        and raw.get("version") == _GIT_LAST_MODIFIED_SCHEMA_VERSION
        and raw.get("repository") == cache_key[0]
        and raw.get("docs") == cache_key[1]
        and isinstance(raw.get("head"), str)
        and bool(raw["head"])
    )
    if not valid_times or not valid_identity:
        LOGGER.warning("Ignoring incompatible Git last-modified cache %s", path)
        return None

    core = {key: raw[key] for key in raw if key != "checksum"}
    checksum = hashlib.sha256(
        json.dumps(core, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if raw.get("checksum") != checksum:
        LOGGER.warning("Ignoring corrupt Git last-modified cache %s", path)
        return None
    return _GitCreationEntry(head=raw["head"], times=dict(times))


def _store_git_last_modified_cache(
    cache_key: tuple[str, str], entry: _GitCreationEntry
) -> None:
    """Atomically persist one validated last-modified map without affecting service."""
    from reckon.serve import LOGGER

    if not (Path(cache_key[0]) / ".git").exists():
        return
    path = _git_last_modified_cache_path(cache_key)
    try:
        write_json_atomically(
            path,
            _git_last_modified_payload(cache_key, entry),
            indent=2,
            sort_keys=True,
            fsync=False,
            mode=0o600,
        )
    except OSError as exc:
        LOGGER.warning("Could not persist Git last-modified cache %s: %s", path, exc)


def _parse_last_committed(output: str) -> dict[str, int]:
    times: dict[str, int] = {}
    timestamp: int | None = None
    for raw in output.splitlines():
        line = raw.strip()
        if line.startswith("COMMIT "):
            try:
                timestamp = int(line[7:])
            except ValueError:
                timestamp = None
        elif line and timestamp is not None:
            # Git emits newest commits first, so the first time a path
            # appears is its most recently touching commit.
            times.setdefault(line, timestamp)
    return times


def _git_last_committed(repo_dir: Path, docs_dir: Path) -> dict[str, int]:
    """Return {repo-relative-path: unix_ts} for the most recent commit touching each file."""
    from reckon.serve import LOGGER

    try:
        rel_docs = str(docs_dir.relative_to(repo_dir))
    except ValueError:
        LOGGER.warning("Docs directory %s is outside repository %s", docs_dir, repo_dir)
        return {}

    cache_key = (str(repo_dir.resolve()), rel_docs)
    cached = _GIT_LAST_MODIFIED_CACHE.get(cache_key)
    if cached is None:
        cached = _load_git_last_modified_cache(cache_key)
        if cached is not None:
            _GIT_LAST_MODIFIED_CACHE[cache_key] = cached
    head = _git_head(repo_dir)
    if head is None:
        return dict(cached.times) if cached else {}
    if cached and cached.head == head:
        return dict(cached.times)

    args = ["git", "log"]
    if cached:
        args.append(f"{cached.head}..{head}")
    args.extend(["--format=COMMIT %at", "--name-only", "--", rel_docs])
    result = _run_git(args, repo_dir, operation="last-modified lookup")
    if result is None:
        return dict(cached.times) if cached else {}

    # Commits in the new range are the freshest touch for any path they name;
    # only paths untouched since the cached head keep their cached time.
    times = _parse_last_committed(result.stdout)
    if cached:
        for path, timestamp in cached.times.items():
            times.setdefault(path, timestamp)
    entry = _GitCreationEntry(head=head, times=times)
    _GIT_LAST_MODIFIED_CACHE[cache_key] = entry
    _store_git_last_modified_cache(cache_key, entry)
    return dict(times)
