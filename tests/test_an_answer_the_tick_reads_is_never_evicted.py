"""A steady pass neither evicts nor rewrites a git answer it reads again.

The crew home kept every tree's git answers in one file, capped at 512 answers
per tree. A tree at the cap evicted the sortedly-first answer for each new one,
so a tick's working set past 512 in one tree made each new answer evict an
answer the same tick — or the next — needed again. Every eviction cost a git
call, a lock and a rewrite of the whole store, and the rewrite gave the file a
new stat identity, so every reader in every process parsed it again.

The answers are now one file per tree under one directory. A tree's answers are
written only to that tree's file, an answer is never evicted while its tree
stands, and a reader holds each tree's file under that file's own stat identity.

The cases hold that: a second pass over 600 abbreviated revisions in one tree
writes no file and asks git nothing; a new answer in one tree leaves another
tree's file identity- and byte-identical; and a legacy single-file store's
answers are served and the file is then removed.
"""

from __future__ import annotations

import json
import os
import subprocess
from importlib import import_module
from pathlib import Path
from typing import Any, Self

import pytest

from reckon.worker_git_shim import RUN_ID_ENV
from tests.helpers.git import git as _git

watch_module = import_module("reckon.crew.recovery_watch")
subject_module = import_module("reckon.crew.recovery_review_subject")

COMMITS = 600
LEGACY_VERSION = 1


def _git_environment() -> dict[str, str]:
    """The child environment a fixture's own git runs; the shim's id is left out."""
    return {key: value for key, value in os.environ.items() if key != RUN_ID_ENV}


def _seed_repository(root: Path, commits: int) -> list[str]:
    """A repository of empty commits, with its object ids newest first.

    The commits are imported in one pass rather than made one subprocess at a
    time: the fixture needs 600 real objects, and the case measures the answer
    store rather than git's startup.
    """
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    stream: list[str] = []
    for index in range(commits):
        message = f"commit {index}"
        stream.extend(
            [
                "commit refs/heads/main",
                f"committer Fixture <fixture@example.invalid> {1_700_000_000 + index} +0000",
                f"data {len(message.encode())}",
                message,
                "",
            ]
        )
    subprocess.run(
        ["git", "fast-import", "--quiet"],
        cwd=root,
        input="\n".join(stream) + "\n",
        text=True,
        check=True,
        env=_git_environment(),
    )
    return _git(root, "rev-list", "HEAD").split()


class _GitCalls:
    """Record every ``git`` argv the bound modules hand to ``subprocess.run``."""

    def __init__(self, *modules: Any) -> None:
        self._modules = modules
        self.commands: list[list[str]] = []

    def __enter__(self) -> Self:
        self._real = {id(module): module.subprocess for module in self._modules}
        recorder = self
        real = self._real

        class _Recording:
            def __init__(self, module: Any) -> None:
                self._module = module

            def run(self, argv: Any, *args: Any, **kwargs: Any) -> Any:
                recorder.commands.append([str(part) for part in argv])
                return real[id(self._module)].run(argv, *args, **kwargs)

            def __getattr__(self, name: str) -> Any:
                return getattr(real[id(self._module)], name)

        for module in self._modules:
            module.subprocess = _Recording(module)
        return self

    def __exit__(self, *exc: object) -> None:
        for module in self._modules:
            module.subprocess = self._real[id(module)]

    @property
    def git_calls(self) -> list[list[str]]:
        """The calls whose argv starts with ``git``."""
        return [command for command in self.commands if command[:1] == ["git"]]


def _answer_files() -> list[Path]:
    """Every file the answer store holds: each tree's, and a legacy single file."""
    directory = watch_module._git_answer_store_dir()
    files = list(directory.iterdir()) if directory.is_dir() else []
    legacy = watch_module._git_answer_legacy_path()
    if legacy.is_file():
        files.append(legacy)
    return files


def _snapshot() -> dict[str, tuple[int, int, int]]:
    """Each answer file's (inode, modification time, size), one entry per file."""
    state: dict[str, tuple[int, int, int]] = {}
    for path in _answer_files():
        stat = path.stat()
        state[str(path)] = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
    return state


def _writes(
    before: dict[str, tuple[int, int, int]], after: dict[str, tuple[int, int, int]]
) -> tuple[dict[str, tuple[int, int, int]], int]:
    """The files a pass wrote — changed or appeared — and the bytes they cost."""
    written = {
        path: entry for path, entry in after.items() if before.get(path) != entry
    }
    return written, sum(entry[2] for entry in written.values())


def _clear_in_process_cache() -> None:
    subject_module._RESOLVE_COMMIT_CACHE.clear()


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary crew home the answer store resolves under."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


@pytest.fixture()
def tree(tmp_path: Path) -> tuple[Path, list[str]]:
    """A repository of 600 commits and their object ids, newest first."""
    repository = tmp_path / "repo"
    return repository, _seed_repository(repository, COMMITS)


def test_a_second_pass_over_stored_answers_writes_nothing(
    crew_home: Path, tree: tuple[Path, list[str]]
) -> None:
    """600 stored abbreviations are answered twice with no write and no git."""
    repository, shas = tree
    candidates = [sha[:10] for sha in shas]

    before_first = _snapshot()
    with _GitCalls(watch_module, subject_module) as first:
        first_pass = [
            subject_module._resolve_commit(repository, name, cached=True)
            for name in candidates
        ]
    after_first = _snapshot()
    first_written, _ = _writes(before_first, after_first)
    assert first_pass == shas, (
        f"the first pass did not resolve 600 revisions: {sum(bool(sha) for sha in first_pass)}"
        " of them named a commit, and the pass is not asking about the objects the"
        " fixture made"
    )
    assert first_written, (
        "the first pass resolved 600 revisions and wrote no answer file, so the"
        " store the second pass is measured against is not the instrument this"
        " case believes it is"
    )
    assert len(first.git_calls) >= COMMITS - len(first_written), (
        f"the first pass spawned {len(first.git_calls)} git calls for 600 unanswered"
        " revisions; the recording stand-in is not seeing the calls it counts"
    )

    _clear_in_process_cache()
    before_second = _snapshot()
    with _GitCalls(watch_module, subject_module) as second:
        second_pass = [
            subject_module._resolve_commit(repository, name, cached=True)
            for name in candidates
        ]
    after_second = _snapshot()
    written, written_bytes = _writes(before_second, after_second)

    assert second_pass == shas, (
        "the second pass did not return the 600 shas the first pass returned"
    )
    assert written == {}, (
        f"the second pass wrote {len(written)} answer files ({written_bytes} bytes):"
        f" {sorted(written)}"
    )
    assert written_bytes == 0, f"the second pass wrote {written_bytes} bytes"
    assert second.git_calls == [], (
        f"the second pass asked git {len(second.git_calls)} times over stored"
        f" answers: {second.git_calls[:3]}"
    )


def test_a_new_answer_rewrites_only_its_own_trees_file(
    crew_home: Path, tmp_path: Path
) -> None:
    """One tree's new answer leaves the other tree's file alone."""
    first_repository = tmp_path / "first"
    first_shas = _seed_repository(first_repository, 4)
    second_repository = tmp_path / "second"
    second_shas = _seed_repository(second_repository, 4)

    with _GitCalls(watch_module, subject_module):
        for sha in first_shas[:2]:
            assert (
                subject_module._resolve_commit(first_repository, sha[:10], cached=True)
                == sha
            )
        for sha in second_shas[:2]:
            assert (
                subject_module._resolve_commit(second_repository, sha[:10], cached=True)
                == sha
            )

    first_path = watch_module._git_answer_tree_path(first_repository)
    second_path = watch_module._git_answer_tree_path(second_repository)
    before = _snapshot()
    assert str(first_path) in before and str(second_path) in before, (
        "the fixture stored answers for only one of its two trees"
    )
    second_contents = second_path.read_bytes()

    _clear_in_process_cache()
    new_candidate = first_shas[2][:10]
    with _GitCalls(watch_module, subject_module) as calls:
        resolved = subject_module._resolve_commit(
            first_repository, new_candidate, cached=True
        )
    after = _snapshot()
    written, written_bytes = _writes(before, after)

    assert resolved == first_shas[2], "the new candidate did not resolve to its commit"
    assert len(calls.git_calls) == 1, (
        f"answering one new candidate spawned {len(calls.git_calls)} git calls"
    )
    assert list(written) == [str(first_path)], (
        f"one tree's new answer wrote {sorted(written)} rather than only {first_path!s}"
    )
    assert written_bytes <= first_path.stat().st_size, (
        f"the write cost {written_bytes} bytes, more than the {first_path.stat().st_size}"
        " bytes of the file it wrote"
    )
    assert after[str(second_path)] == before[str(second_path)], (
        "the other tree's file changed identity when this tree's answer was written"
    )
    assert second_path.read_bytes() == second_contents, (
        "the other tree's file content changed when this tree's answer was written"
    )


def test_a_legacy_single_file_store_is_served_then_removed(
    crew_home: Path, tree: tuple[Path, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A legacy store's answers are served without git, and the file is removed."""
    repository, shas = tree
    candidate = shas[0][:10]
    legacy = watch_module._git_answer_legacy_path()
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(
        json.dumps(
            {
                "version": LEGACY_VERSION,
                "commits": {str(repository): {candidate: shas[0]}},
                "counts": {},
                "unreachable": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(watch_module, "_LEGACY_GIT_ANSWER_MIGRATED", set())
    _clear_in_process_cache()

    with _GitCalls(watch_module, subject_module) as calls:
        resolved = subject_module._resolve_commit(repository, candidate, cached=True)

    assert resolved == shas[0], "the legacy answer did not name its commit"
    assert calls.git_calls == [], (
        f"the legacy answer cost {len(calls.git_calls)} git calls: the single file"
        " was not read as an answer source"
    )
    assert not legacy.exists(), "the legacy single-file store was not removed"
    assert watch_module._git_answer_tree_path(repository).is_file(), (
        "the legacy answers were removed without being written to their tree's file"
    )
