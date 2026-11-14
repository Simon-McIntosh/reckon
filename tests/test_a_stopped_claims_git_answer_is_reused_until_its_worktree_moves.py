"""A stopped claim's git answer is asked again only when its worktree moves.

Judging a stopped claim costs two subprocesses in its worktree: which commits
the integration ref and the recorded base cannot reach, and which declared
paths hold uncommitted changes. A stopped run has no worker, so between two
reads its worktree almost never moves and the second read repeats the first's
answers. This file holds that the repeat is avoided, and — the important half —
that every case where the answer could have changed still asks git: a commit,
an edited declared file, an edited tracked file under a declared directory, and
a command that failed.

Every fixture synthesises its repository and points ``RECKON_HOME`` at a
temporary directory. Each test asserts the real live-pointer directory is
untouched afterwards, because an isolated read does not prove an isolated
write.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import node as node_module
from reckon.crew import recovery_watch


def _directory_snapshot(path: Path) -> tuple[str, ...]:
    if not path.is_dir():
        return ()
    return tuple(sorted(item.name for item in path.iterdir()))


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    live_directory = crew.live_dir()
    before = _directory_snapshot(live_directory)
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    _forget_in_process_answers()
    yield config_home
    _forget_in_process_answers()
    assert _directory_snapshot(live_directory) == before


def _forget_in_process_answers() -> None:
    """Empty the per-process memos, leaving the stored answers in place."""
    node_module._CLAIM_COMMIT_ANSWERS.clear()
    node_module._CLAIM_STATUS_ANSWERS.clear()


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _repository(root: Path) -> Path:
    (root / "package").mkdir(parents=True)
    (root / "package" / "target.py").write_text("value = 1\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "package"],
        ["commit", "-q", "-m", "chore: seed fixture"],
    ):
        _git(root, *arguments)
    return root


def _worktree(repo: Path, path: Path) -> Path:
    _git(repo, "worktree", "add", "--detach", "-q", str(path), "HEAD")
    return path


def _reaped_pid() -> int:
    """Return a process id that has certainly exited and been collected."""
    worker = subprocess.Popen(["true"])
    worker.wait()
    return worker.pid


def _pointer(
    *,
    repo: Path,
    worktree: Path,
    paths: tuple[str, ...],
    base_sha: str,
) -> dict:
    return {
        "run_id": "r-owner",
        "project": "project-a",
        "repo": str(repo.resolve()),
        "worktree": str(worktree),
        "phase": "working",
        "pid": _reaped_pid(),
        "base_sha": base_sha,
        "node": {
            "id": "owner-node",
            "plan": "project-a-plan",
            "write_paths": list(paths),
        },
    }


class _Counter:
    """Count the git commands a disposition spends, by their verb."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, cwd, *arguments):
        self.calls.append(tuple(arguments))
        return self._original(cwd, *arguments)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> _Counter:
        self._original = node_module._git_answer
        monkeypatch.setattr(node_module, "_git_answer", self)
        return self

    def reset(self) -> None:
        self.calls = []

    def verbs(self) -> list[str]:
        return [arguments[0] for arguments in self.calls]


@pytest.fixture()
def counted(monkeypatch: pytest.MonkeyPatch) -> _Counter:
    return _Counter().install(monkeypatch)


def _stopped_claim(tmp_path: Path) -> tuple[Path, Path, dict]:
    repo = _repository(tmp_path / "shared")
    worktree = _worktree(repo, tmp_path / "linked")
    pointer = _pointer(
        repo=repo,
        worktree=worktree,
        paths=("package/target.py",),
        base_sha=_git(repo, "rev-parse", "HEAD"),
    )
    return repo, worktree, pointer


def test_two_dispositions_of_an_unmoved_worktree_ask_git_once(
    home: Path, tmp_path: Path, counted: _Counter
) -> None:
    """An unchanged stopped worktree answers its second disposition for free."""
    _, _, pointer = _stopped_claim(tmp_path)

    first = node_module.claim_disposition(pointer)
    second = node_module.claim_disposition(pointer)

    assert counted.verbs() == ["status", "rev-list"], counted.calls
    assert first.binding is False
    assert second == first


def test_a_fresh_process_answers_the_commit_question_from_the_store(
    home: Path, tmp_path: Path, counted: _Counter
) -> None:
    """The stored answer serves a process that has never asked git anything.

    The commit answer is a function of three object ids, so §44's store under
    the crew home can serve it. The uncommitted-change answer reads the working
    tree and is still asked, which is what proves git is reachable in this run
    rather than disabled.
    """
    _, _, pointer = _stopped_claim(tmp_path)
    node_module.claim_disposition(pointer)
    stored = json.loads(
        (home / "crew" / "git-object-answers.json").read_text(encoding="utf-8")
    )
    assert stored["unreachable"], "the commit answer was never stored"

    _forget_in_process_answers()
    counted.reset()
    disposition = node_module.claim_disposition(pointer)

    assert counted.verbs() == ["status"], counted.calls
    assert disposition.binding is False


def test_a_commit_moves_the_head_and_the_second_disposition_asks_again(
    home: Path, tmp_path: Path, counted: _Counter
) -> None:
    """A worktree that committed after the first read is reported as holding work."""
    _, worktree, pointer = _stopped_claim(tmp_path)
    first = node_module.claim_disposition(pointer)
    assert first.binding is False

    (worktree / "package" / "target.py").write_text("value = 2\n", encoding="utf-8")
    _git(worktree, "add", "package")
    _git(
        worktree,
        "-c",
        "user.email=w@example.invalid",
        "-c",
        "user.name=W",
        "commit",
        "-q",
        "-m",
        "feat: work nobody else has",
    )
    counted.reset()
    second = node_module.claim_disposition(pointer)

    assert "rev-list" in counted.verbs(), counted.calls
    assert second.binding is True
    assert any("commit(s) beyond its base" in item for item in second.unintegrated)


def test_an_edited_declared_file_asks_git_again(
    home: Path, tmp_path: Path, counted: _Counter
) -> None:
    """The uncommitted-change answer follows an edited declared file."""
    _, worktree, pointer = _stopped_claim(tmp_path)
    assert node_module.claim_disposition(pointer).binding is False

    (worktree / "package" / "target.py").write_text("value = 3\n", encoding="utf-8")
    counted.reset()
    second = node_module.claim_disposition(pointer)

    assert counted.verbs() == ["status"], counted.calls
    assert second.binding is True
    assert any("uncommitted change" in item for item in second.unintegrated)


def test_a_tracked_file_under_a_declared_directory_asks_git_every_time(
    home: Path, tmp_path: Path, counted: _Counter
) -> None:
    """A declared directory is never served from the memo.

    Editing a tracked file beneath a directory moves neither the directory's
    stat nor the index's, while git reports the edit; a reused clean answer
    would release a claim that still holds work.
    """
    repo = _repository(tmp_path / "shared")
    worktree = _worktree(repo, tmp_path / "linked")
    pointer = _pointer(
        repo=repo,
        worktree=worktree,
        paths=("package",),
        base_sha=_git(repo, "rev-parse", "HEAD"),
    )
    assert node_module.claim_disposition(pointer).binding is False

    (worktree / "package" / "target.py").write_text("value = 4\n", encoding="utf-8")
    counted.reset()
    second = node_module.claim_disposition(pointer)

    assert "status" in counted.verbs(), counted.calls
    assert second.binding is True
    assert any("uncommitted change" in item for item in second.unintegrated)


def test_a_failed_command_is_asked_again(
    home: Path, tmp_path: Path, counted: _Counter
) -> None:
    """An unanswerable comparison is never remembered. Nothing to reuse."""
    _, _, pointer = _stopped_claim(tmp_path)
    first = node_module.claim_disposition(pointer, integration_ref="develop")
    assert any("could not be compared" in item for item in first.unintegrated)

    counted.reset()
    second = node_module.claim_disposition(pointer, integration_ref="develop")

    assert counted.verbs().count("rev-list") == 1, counted.calls
    assert second == first


def test_a_stored_answer_is_not_served_for_a_moved_worktree(
    home: Path, tmp_path: Path, counted: _Counter
) -> None:
    """The store is keyed on the head, so a moved head reads as a new question."""
    _, worktree, pointer = _stopped_claim(tmp_path)
    node_module.claim_disposition(pointer)
    _forget_in_process_answers()

    (worktree / "package" / "target.py").write_text("value = 5\n", encoding="utf-8")
    _git(worktree, "add", "package")
    _git(
        worktree,
        "-c",
        "user.email=w@example.invalid",
        "-c",
        "user.name=W",
        "commit",
        "-q",
        "-m",
        "feat: a commit the first key never saw",
    )
    counted.reset()
    disposition = node_module.claim_disposition(pointer)

    assert counted.verbs().count("rev-list") == 1, counted.calls
    assert disposition.binding is True


def test_the_store_answers_the_same_commit_question_the_same_way(
    home: Path, tmp_path: Path
) -> None:
    """What the store serves is what git said, not a shape the caller must trust
    twice."""
    _, worktree, pointer = _stopped_claim(tmp_path)
    (worktree / "package" / "target.py").write_text("value = 6\n", encoding="utf-8")
    _git(worktree, "add", "package")
    _git(
        worktree,
        "-c",
        "user.email=w@example.invalid",
        "-c",
        "user.name=W",
        "commit",
        "-q",
        "-m",
        "feat: one commit past the base",
    )

    asked = node_module.claim_disposition(pointer)
    _forget_in_process_answers()
    served = node_module.claim_disposition(pointer)

    assert asked == served
    assert asked.binding is True


def test_disabling_the_reuse_asks_git_twice(
    home: Path, tmp_path: Path, counted: _Counter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The negative control: with neither memo nor store keeping an answer, the
    first case's worktree is asked about twice.

    This is the mutation the gate is declared against.
    """
    _, _, pointer = _stopped_claim(tmp_path)
    monkeypatch.setattr(
        node_module, "_remember_claim_commit_answer", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        node_module, "_remember_claim_status_answer", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        recovery_watch, "_remember_unreachable_answer", lambda *_a, **_k: None
    )

    node_module.claim_disposition(pointer)
    node_module.claim_disposition(pointer)

    assert counted.verbs() == ["status", "rev-list", "status", "rev-list"]
