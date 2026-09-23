"""Memoise per-file parses against the file's stat identity.

Discovery reads the same plan files many times per request and again on every
request, and each open costs milliseconds on a shared filesystem. A parse is a
pure function of the file's bytes, so its result is kept until the file's
device, inode, size, mtime or ctime moves — any write changes at least one.

Callers receive a deep copy, so mutating a returned value never poisons the
entry another request will read.
"""

from __future__ import annotations

import copy
import os
import threading
from collections import OrderedDict
from collections.abc import Callable, Hashable
from pathlib import Path

_MAX_ENTRIES = 50_000

_Signature = tuple[int, int, int, int, int]
_CACHE: OrderedDict[tuple[str, str, Hashable], tuple[_Signature, object]] = (
    OrderedDict()
)
_LOCK = threading.Lock()


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
) -> T:
    """Return ``compute()`` for ``path``, reusing it while the file is unchanged.

    ``kind`` names the parse so two parsers of one file never share an entry;
    ``variant`` distinguishes calls whose arguments change the result. A file
    that cannot be stat'ed is parsed uncached, so its error path is unchanged.
    """

    try:
        signature = file_signature(path)
    except OSError:
        return compute()
    key = (kind, os.fspath(path), variant)
    with _LOCK:
        entry = _CACHE.get(key)
        if entry is not None and entry[0] == signature:
            _CACHE.move_to_end(key)
            return copy.deepcopy(entry[1])  # type: ignore[return-value]
    value = compute()
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
