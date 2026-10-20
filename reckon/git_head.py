"""Read a checkout's HEAD identity as files, without spawning git.

Every long-running reckon process asks once a second whether its code has
moved, and that question is answered here by reading ``HEAD`` and the ref it
names. The readers are stdlib-only, so a process that needs the head identity
loads no derivation module to get it: the follower's per-second read, the
coordinator prompt hook's freshness check and the served-code reader all reach
this module rather than the date cache beside it.

A linked worktree keeps its own ``HEAD`` in a private git directory and its
branch refs in the directory that directory names in ``commondir``, so both
are followed here. ``_head_identity`` returns ``None`` when the reading cannot
be made, which means the caller must resolve the head itself rather than
treating the checkout as unmoved.
"""

from __future__ import annotations

from pathlib import Path


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
    """A file's stat identity, or None when it cannot be read.

    Two readings of the same path compare equal exactly while the file has not
    been written or replaced, which is the whole of what a memo against it
    needs.
    """

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
