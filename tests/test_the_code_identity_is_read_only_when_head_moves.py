"""The code identity is read only when HEAD moves.

A long-running reckon process asks once a second whether its code has moved,
and the read runs ``git ls-tree`` twice and one ``git mktree`` per composed
tree. ``git mktree`` freshens an object that already exists by rewriting its
mtime, so a reader asking about an unmoved checkout rewrites one shared inode
every second from every node. The reading is held against a fingerprint of the
files ``HEAD`` resolves through, so the git reads run only when one of them
moves: a commit, a branch switch, a detach and ``git pack-refs`` each rewrite
one of them, and a commit touching nothing the identity names still moves the
fingerprint while leaving the identity itself unchanged.

Every git invocation the identity path makes is counted as it is spawned, so
"no git spawn" is measured rather than asserted. The identity is read through
the production call shape -- no source root named, so the root is the package's
parent -- which is the shape the followers, producers, MCP server and queue
admission all use.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from reckon import checkout_trust


@pytest.fixture()
def spawns(monkeypatch) -> list[tuple]:
    """Every git invocation spawned while the test runs, in order."""
    real_run = subprocess.run
    seen: list[tuple] = []

    def _run(argv, *args, **kwargs):
        if isinstance(argv, (list, tuple)) and argv and argv[0] == "git":
            seen.append(tuple(str(part) for part in argv))
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(checkout_trust.subprocess, "run", _run)
    return seen


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit(root: Path, relative: str, text: str, message: str) -> str:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    _git(root, "add", relative)
    _git(root, "commit", "-qm", message)
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture()
def source(tmp_path: Path, spawns: list, monkeypatch) -> Path:
    """A checkout carrying the package, the dispatch scripts and the catalogue."""
    monkeypatch.setattr(checkout_trust, "_identity_hold", {})
    root = tmp_path / "source"
    (root / "reckon").mkdir(parents=True)
    (root / "reckon" / "__init__.py").write_text("")
    (root / "reckon" / "example.py").write_text("VALUE = 'first'\n")
    (root / "skills").mkdir()
    (root / "skills" / "dispatch.txt").write_text("scripts\n")
    catalogue = root / checkout_trust.CATALOGUE_RELATIVE_PATH
    catalogue.parent.mkdir(parents=True)
    catalogue.write_text("version: 1\n")
    return _seeded(root)


def _seeded(root: Path) -> Path:
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Identity Test")
    _git(root, "config", "user.email", "identity@example.invalid")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "Seed source")
    return root


def _identity(root: Path) -> str:
    """The identity as a running reckon process reads it, with no root named."""
    return checkout_trust.source_code_identity(root / "reckon")


def _fresh_read(root: Path) -> str:
    """The identity derived from git again, with no reading held."""
    return checkout_trust._committed_reload_tree(root)


def test_the_identity_is_read_once_over_many_quiet_ticks(source, spawns) -> None:
    """(a) A quiet checkout is read once; every later tick spawns no git."""
    first = _identity(source)
    assert first == _fresh_read(source)
    assert len(spawns) > 0

    after_first = len(spawns)
    for _ in range(20):
        assert _identity(source) == first

    assert len(spawns) == after_first


def test_a_commit_yields_the_new_identity_on_the_next_read(source, spawns) -> None:
    """(b) A commit rewriting the ref is seen by the next read."""
    first = _identity(source)

    _commit(source, "reckon/example.py", "VALUE = 'second'\n", "Change the package")

    before = len(spawns)
    second = _identity(source)
    assert len(spawns) > before
    assert second != first
    assert second == _fresh_read(source)


def test_a_branch_switch_yields_the_new_identity_on_the_next_read(source) -> None:
    """(c) A branch switch is seen by the next read."""
    first = _identity(source)
    original = _git(source, "rev-parse", "--abbrev-ref", "HEAD")

    _git(source, "checkout", "-q", "-b", "other")
    _commit(source, "reckon/example.py", "VALUE = 'other'\n", "Change on other branch")
    other = _identity(source)
    assert other != first

    _git(source, "checkout", "-q", original)
    assert _identity(source) == first


def test_a_packed_ref_yields_the_new_identity_on_the_next_read(source, spawns) -> None:
    """(d) ``git pack-refs`` moves the fingerprint, and a later commit is still seen."""
    first = _identity(source)

    _git(source, "pack-refs", "--all")
    branch = _git(source, "rev-parse", "--abbrev-ref", "HEAD")
    assert (source / ".git" / "packed-refs").exists()
    assert not (source / ".git" / "refs" / "heads" / branch).exists()

    before_pack = len(spawns)
    assert _identity(source) == first
    assert len(spawns) > before_pack

    _commit(source, "reckon/example.py", "VALUE = 'third'\n", "Change again")
    after_pack = len(spawns)
    newest = _identity(source)
    assert len(spawns) > after_pack
    assert newest != first
    assert newest == _fresh_read(source)


def test_a_detached_head_yields_the_new_identity_on_the_next_read(source) -> None:
    """(e) A detach is seen by the next read."""
    first = _identity(source)
    first_commit = _git(source, "rev-parse", "HEAD")

    _commit(source, "reckon/example.py", "VALUE = 'moved'\n", "Change the package")
    assert _identity(source) != first

    _git(source, "checkout", "-q", "--detach", first_commit)
    assert _identity(source) == first


def test_a_worktree_is_stamped_through_its_gitdir(source, tmp_path, spawns) -> None:
    """(f) A worktree whose ``.git`` is a file is held through the gitdir it names."""
    linked = tmp_path / "linked"
    _git(source, "worktree", "add", "-q", str(linked), "-b", "linked")
    assert (linked / ".git").is_file()
    assert checkout_trust.head_stamp(linked) is not None

    first = checkout_trust.source_code_identity(linked / "reckon")
    assert first == _fresh_read(linked)

    after_first = len(spawns)
    for _ in range(5):
        assert checkout_trust.source_code_identity(linked / "reckon") == first
    assert len(spawns) == after_first

    _commit(linked, "reckon/example.py", "VALUE = 'linked'\n", "Change in the worktree")
    before = len(spawns)
    refreshed = checkout_trust.source_code_identity(linked / "reckon")
    assert len(spawns) > before
    assert refreshed == _fresh_read(linked)


def test_a_failed_read_is_not_held_and_the_next_read_runs_git(
    source, spawns, monkeypatch
) -> None:
    """(g) A read that did not complete is re-reported rather than held."""
    first = _identity(source)
    assert first == _fresh_read(source)

    real_run = subprocess.run
    held_runner = checkout_trust.subprocess.run

    def _timing_out(argv, *args, **kwargs):
        if isinstance(argv, (list, tuple)) and any(
            "model-catalogue" in str(part) for part in argv
        ):
            raise subprocess.TimeoutExpired(argv, checkout_trust.IMPORT_PROBE_TIMEOUT)
        return held_runner(argv, *args, **kwargs)

    _commit(source, "reckon/example.py", "VALUE = 'second'\n", "Change the package")
    before_failure = len(spawns)
    monkeypatch.setattr(checkout_trust.subprocess, "run", _timing_out)
    assert _identity(source) == checkout_trust.IDENTITY_UNKNOWN
    assert len(spawns) > before_failure

    before_retry = len(spawns)
    assert _identity(source) == checkout_trust.IDENTITY_UNKNOWN
    assert len(spawns) > before_retry

    monkeypatch.setattr(checkout_trust.subprocess, "run", real_run)
    recovered = _identity(source)
    assert recovered == _fresh_read(source)
    assert recovered != first


def test_a_commit_outside_the_package_moves_the_stamp_and_runs_the_reads(
    source, spawns
) -> None:
    """(h) An out-of-scope commit re-reads, and the identity is unchanged."""
    first = _identity(source)

    _commit(source, "NOTES.md", "outside every carried tree\n", "Notes outside")

    before = len(spawns)
    assert _identity(source) == first
    assert len(spawns) > before
