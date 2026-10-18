"""Memoise per-file parses against the file's stat identity.

Discovery reads the same plan files many times per request and again on every
request, and each open costs milliseconds on a shared filesystem. A parse is a
pure function of the file's bytes, so its result is kept until the file's
device, inode, size, mtime or ctime moves — any write changes at least one.

Callers receive a deep copy, so mutating a returned value never poisons the
entry another request will read.
"""

from __future__ import annotations

import contextlib
import copy
import os
import threading
from collections import OrderedDict
from collections.abc import Callable, Hashable, Iterator
from contextvars import ContextVar
from pathlib import Path

_MAX_ENTRIES = 50_000

_Signature = tuple[int, int, int, int, int]
_CACHE: OrderedDict[tuple[str, str, Hashable], tuple[_Signature, object]] = (
    OrderedDict()
)
_LOCK = threading.Lock()

# A read-only region in which a memoised value is shared rather than copied.
# Within the region each document is stat'ed once, decoded once, and handed to
# the caller itself; a caller that must mutate what it reads passes
# ``mutable=True`` and is handed a copy at that hand-over. Outside a region the
# memo keeps returning the copy it always has.
_REGION: ContextVar[dict[tuple[str, str, Hashable], object] | None] = ContextVar(
    "file_memo_region", default=None
)


@contextlib.contextmanager
def read_only_region() -> Iterator[None]:
    """Open a region in which :func:`memoized` shares its stored value.

    A reader inside the region must not mutate a value it is handed; one that
    must asks for it with ``mutable=True``. The region is per-thread-task, so a
    concurrent reader outside it is unaffected.
    """

    token = _REGION.set({})
    try:
        yield
    finally:
        _REGION.reset(token)


def file_signature(path: Path | str) -> _Signature:
    """Return the stat identity that any write to ``path`` changes."""

    stat = os.stat(path)
    return (
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
    )


def memoized[T](
    kind: str,
    path: Path | str,
    compute: Callable[[], T],
    *,
    variant: Hashable = None,
    mutable: bool = False,
) -> T:
    """Return ``compute()`` for ``path``, reusing it while the file is unchanged.

    ``kind`` names the parse so two parsers of one file never share an entry;
    ``variant`` distinguishes calls whose arguments change the result. A file
    that cannot be stat'ed is parsed uncached, so its error path is unchanged.

    Inside a :func:`read_only_region` the value is stat'ed and decoded once and
    handed over without copying; ``mutable=True`` asks for a copy at that
    hand-over, for a caller that will change what it is given.
    """

    region = _REGION.get()
    key = (kind, os.fspath(path), variant)
    if region is not None and key in region:
        value = region[key]
        return copy.deepcopy(value) if mutable else value  # type: ignore[return-value]
    try:
        signature = file_signature(path)
    except OSError:
        return compute()
    with _LOCK:
        entry = _CACHE.get(key)
        if entry is not None and entry[0] == signature:
            _CACHE.move_to_end(key)
            value = entry[1]
            if region is not None:
                region[key] = value
                return copy.deepcopy(value) if mutable else value  # type: ignore[return-value]
            return copy.deepcopy(value)  # type: ignore[return-value]
    value = compute()
    if region is not None:
        # One stat and one decode for the region: the computed value is stored
        # itself, and the region hands it on from here, so a second reader of
        # the same document inside the region neither stats nor decodes again.
        with _LOCK:
            _CACHE[key] = (signature, value)
            _CACHE.move_to_end(key)
            while len(_CACHE) > _MAX_ENTRIES:
                _CACHE.popitem(last=False)
        region[key] = value
        return copy.deepcopy(value) if mutable else value
    try:
        unchanged = file_signature(path) == signature
    except OSError:
        unchanged = False
    if unchanged:
        # A write that landed mid-parse leaves the entry unstored, so the
        # next reader parses the new bytes rather than a torn read.
        with _LOCK:
            _CACHE[key] = (signature, copy.deepcopy(value))
            _CACHE.move_to_end(key)
            while len(_CACHE) > _MAX_ENTRIES:
                _CACHE.popitem(last=False)
    return value


def clear() -> None:
    """Drop every memoised parse."""

    with _LOCK:
        _CACHE.clear()
