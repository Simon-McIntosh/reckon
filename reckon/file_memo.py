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
from collections.abc import Callable, Hashable, Iterable, Iterator, Mapping
from contextvars import ContextVar
from pathlib import Path
from typing import Any

_MAX_ENTRIES = 50_000

_Signature = tuple[int, int, int, int, int]
_CACHE: OrderedDict[tuple[str, str, Hashable], tuple[_Signature, object]] = (
    OrderedDict()
)
_LOCK = threading.Lock()

# A read-only region in which a memoised value is shared rather than copied.
# Within the region each document is stat'ed once, decoded once, and handed to
# the caller as a read-only view of the parsed value: the region refuses any
# change to what it hands out, so a caller that would mutate a shared document
# is stopped at the change rather than silently corrupting the process-wide memo
# for every later reader. A caller that must mutate what it reads passes
# ``mutable=True`` and is handed a mutable copy at that hand-over. Outside a
# region the memo keeps returning the copy it always has.
_REGION: ContextVar[dict[tuple[str, str, Hashable], list[Any]] | None] = ContextVar(
    "file_memo_region", default=None
)


class _ReadOnlyDict(dict):
    """A parsed document handed out inside a region; a change to it raises.

    It carries the document's own items, so reads are unchanged —
    ``dict(value)``, ``json.dumps`` and equality all see the document — while
    every mutating method refuses, so a caller that would change a shared
    document is detected at the change rather than poisoning the memo for every
    later reader.
    """

    __slots__ = ()

    def __init__(self, source: Mapping[str, Any]) -> None:
        super().__init__({key: _read_only(value) for key, value in source.items()})

    def _refuse(self, *_arguments: Any, **_keywords: Any) -> None:
        raise TypeError("a document handed out inside a read-only region is read-only")

    __setitem__ = _refuse
    __delitem__ = _refuse
    __ior__ = _refuse
    clear = _refuse
    pop = _refuse
    popitem = _refuse
    setdefault = _refuse
    update = _refuse


class _ReadOnlyList(list):
    """A parsed document's list, handed out inside a region; a change raises."""

    __slots__ = ()

    def __init__(self, source: Iterable[Any]) -> None:
        super().__init__(_read_only(value) for value in source)

    def _refuse(self, *_arguments: Any, **_keywords: Any) -> None:
        raise TypeError("a document handed out inside a read-only region is read-only")

    __setitem__ = _refuse
    __delitem__ = _refuse
    __iadd__ = _refuse
    __imul__ = _refuse
    append = _refuse
    clear = _refuse
    extend = _refuse
    insert = _refuse
    pop = _refuse
    remove = _refuse
    reverse = _refuse
    sort = _refuse


def _read_only(value: Any) -> Any:
    """Return ``value`` as a read-only view, rebuilding each container once.

    A mapping is rebuilt as a :class:`_ReadOnlyDict` and a list as a
    :class:`_ReadOnlyList`, both carrying the value's own items so every read is
    unchanged; anything else is returned as it is. The rebuild is the region's
    one added cost, and :func:`_handout` does it once per document per region
    rather than once per hand-out.
    """

    if isinstance(value, dict):
        return _ReadOnlyDict(value)
    if isinstance(value, list):
        return _ReadOnlyList(value)
    return value


def _handout(entry: list[Any], mutable: bool) -> Any:
    """The value to hand a region's caller: a mutable copy, or the cached view.

    ``entry`` is ``[value, view]``: the region's own value and, once built, the
    read-only view of it. A caller that asked for ``mutable`` gets a mutable
    copy of the value; every other caller shares the one view the region builds.
    """

    if mutable:
        return copy.deepcopy(entry[0])
    view = entry[1]
    if view is None:
        view = _read_only(entry[0])
        entry[1] = view
    return view


@contextlib.contextmanager
def read_only_region() -> Iterator[None]:
    """Open a region in which :func:`memoized` shares a read-only view.

    A reader inside the region is handed a view, so mutating what it reads
    raises rather than mutating the memo; a reader that must mutate asks for it
    with ``mutable=True`` and is handed a copy. The region is per-thread-task,
    so a concurrent reader outside it is unaffected.
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
    handed to the caller as a read-only view of it, so mutating what is read is
    refused; ``mutable=True`` asks for a mutable copy at that hand-over, for a
    caller that will change what it is given.
    """

    region = _REGION.get()
    key = (kind, os.fspath(path), variant)
    if region is not None and key in region:
        return _handout(region[key], mutable)  # type: ignore[return-value]
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
                region[key] = [value, None]
                return _handout(region[key], mutable)  # type: ignore[return-value]
            return copy.deepcopy(value)  # type: ignore[return-value]
    value = compute()
    if region is not None:
        # One stat and one decode for the region: the computed value is stored
        # itself, the region hands a read-only view of it on from here, so a
        # second reader of the same document inside the region neither stats nor
        # decodes again, and no caller can change the stored value.
        with _LOCK:
            _CACHE[key] = (signature, value)
            _CACHE.move_to_end(key)
            while len(_CACHE) > _MAX_ENTRIES:
                _CACHE.popitem(last=False)
        region[key] = [value, None]
        return _handout(region[key], mutable)  # type: ignore[return-value]
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
