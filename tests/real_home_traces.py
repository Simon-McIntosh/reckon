"""Needle-filtered checks that a test wrote nothing into a shared directory.

A test that runs under a temporary configuration home should also prove it
wrote nothing into the real one. Comparing a shared directory against an
earlier snapshot cannot make that claim: peer sessions dispatch, arm followers
and rewrite rosters continuously, so the tree moves without anything being
wrong and the guard fails on a peer's write, at base as well as at head. A
guard that fails on someone else's write cannot say whether this test wrote,
and a reader learns to ignore it.

What can make the claim is searching, on the same root before and after the
case, for an identifier only this test mints -- a run id, node id, project or
session -- and reporting a file only when its name or its content carries one.
A peer's own entry names the peer, so it is never a hit. Both a path's own name
and its text are read, so a pointer filed under the run id and a registration
whose body records the session are each caught.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path


def _read_bytes(path: Path) -> bytes:
    """The file's own bytes, or nothing where it cannot be read.

    Bytes rather than text because the files a guard may be pointed at are not
    all text: the shared crew store is a 355 MB SQLite file, and decoding a file
    that size into a str costs several times its own size in memory once the
    undecodable runs widen into replacement characters. A search over bytes
    holds the file's own size and nothing more. A file that has gone, or that
    cannot be opened, reads as no hit -- the guard asks whether this test left a
    trace, and one it cannot read is not one it wrote.
    """
    try:
        return path.read_bytes()
    except OSError:
        return b""


def traces_naming(
    root: Path, subtrees: Sequence[str], needles: Iterable[str]
) -> list[str]:
    """Files under ``root``'s ``subtrees`` whose name or content carries a needle.

    ``root`` is a shared directory -- the real configuration home, the shared
    crew home, a checkout -- and ``subtrees`` names the relative subdirectories
    to search, which may not exist yet. ``needles`` are identifiers the calling
    test minted; an empty needle is ignored, so a caller may pass a run id that
    is not always known without inventing a match. Only a file whose own name or
    text carries a needle is returned, so a peer's entry is never reported.

    Use it on the same root before and after the case and require both readings
    equal, or require the after reading empty.
    """
    wanted = [needle for needle in needles if needle]
    wanted_bytes = [needle.encode("utf-8") for needle in wanted]
    hits: list[str] = []
    for subtree in subtrees:
        target = root / subtree
        if target.is_file():
            # A subtree may name a single file a writer touches directly, such
            # as the home's own budget-lifts.json, so a guard against a leaked
            # write can point at that file without walking the whole tree.
            if any(needle in target.name for needle in wanted):
                hits.append(str(target))
                continue
            data = _read_bytes(target)
            if any(needle in data for needle in wanted_bytes):
                hits.append(str(target))
            continue
        if not target.is_dir():
            continue
        for path in target.rglob("*"):
            if not path.is_file():
                continue
            named = any(needle in path.name for needle in wanted)
            if named:
                hits.append(str(path))
                continue
            data = _read_bytes(path)
            if any(needle in data for needle in wanted_bytes):
                hits.append(str(path))
    return hits
