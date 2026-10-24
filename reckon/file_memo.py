"""Memoise per-file parses against the file's stat identity.

Discovery reads the same plan files many times per request and again on every
request, and each open costs milliseconds on a shared filesystem. A parse is a
pure function of the file's bytes, so its result is kept until the file's
device, inode, size, mtime or ctime moves — any write changes at least one.

Callers receive a deep copy, so mutating a returned value never poisons the
entry another request will read. A caller that reads many documents once and
will not change them opens a :func:`read_only_region`, which trades that copy
for a read-only view of the stored value: the copy is what a reader pays per
hand-out, and a caller that changes nothing gains nothing from it.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import os
import threading
from collections import OrderedDict
from collections.abc import Callable, Hashable, Iterable, Iterator, Mapping
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Self

_MAX_ENTRIES = 50_000

_Signature = tuple[int, int, int, int, int]
#: A stat signature, or a content digest when the caller opted into
#: ``by_content``. The two are never compared with each other: one entry's
#: identity kind follows from the kind its caller asked for.
_Identity = _Signature | str
_CACHE: OrderedDict[tuple[str, str, Hashable], tuple[_Identity, object]] = OrderedDict()
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
    later reader. Copying is the way to a mutable value: ``copy.copy`` returns a
    plain mapping and ``copy.deepcopy`` a plain, fully mutable document.
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

    def __copy__(self) -> dict[str, Any]:
        return dict(self)

    def __deepcopy__(self, memo: dict[int, Any] | None = None) -> dict[str, Any]:
        return _plain_copy(self, memo)


class _ReadOnlyList(list):
    """A parsed document's list, handed out inside a region; a change raises.

    Copying yields a mutable value: ``copy.copy`` a plain list, ``mutating`` it
    leaves the view untouched, and ``copy.deepcopy`` a plain, fully mutable
    list.
    """

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

    def __copy__(self) -> list[Any]:
        return list(self)

    def __deepcopy__(self, memo: dict[int, Any] | None = None) -> list[Any]:
        return _plain_copy(self, memo)


class _ReadOnlyTuple(tuple):
    """A document's tuple, handed out inside a region; its containers refuse.

    A tuple is already immutable, so nothing here refuses a change to the tuple
    itself; it exists so the containers a tuple can hold — the members a tuple
    is otherwise a window onto — are wrapped. Copying yields a plain tuple.
    """

    __slots__ = ()

    def __new__(cls, source: Iterable[Any]) -> Self:
        return super().__new__(cls, (_read_only(value) for value in source))

    def __copy__(self) -> tuple[Any, ...]:
        return tuple(self)

    def __deepcopy__(self, memo: dict[int, Any] | None = None) -> tuple[Any, ...]:
        return _plain_copy(self, memo)


class _ReadOnlySet(set):
    """A document's set, handed out inside a region; a change to it raises."""

    __slots__ = ()

    def __init__(self, source: Iterable[Any]) -> None:
        super().__init__(_read_only(value) for value in source)

    def _refuse(self, *_arguments: Any, **_keywords: Any) -> None:
        raise TypeError("a document handed out inside a read-only region is read-only")

    __iand__ = _refuse
    __ior__ = _refuse
    __isub__ = _refuse
    __ixor__ = _refuse
    add = _refuse
    clear = _refuse
    difference_update = _refuse
    discard = _refuse
    intersection_update = _refuse
    pop = _refuse
    remove = _refuse
    symmetric_difference_update = _refuse
    update = _refuse

    def __copy__(self) -> set[Any]:
        return set(self)

    def __deepcopy__(self, memo: dict[int, Any] | None = None) -> set[Any]:
        return _plain_copy(self, memo)


def _read_only(value: Any) -> Any:
    """Return ``value`` as a read-only view, rebuilding each container once.

    A mapping is rebuilt as a :class:`_ReadOnlyDict`, a list as a
    :class:`_ReadOnlyList`, a tuple as a :class:`_ReadOnlyTuple` and a set as a
    :class:`_ReadOnlySet`, each carrying the value's own items so every read is
    unchanged; a frozenset, which cannot hold a mutable member, is rebuilt as a
    plain frozenset of read-only members, and anything else is returned as it
    is. The rebuild is the region's one added cost, and :func:`_handout` does it
    once per document per region rather than once per hand-out.
    """

    if isinstance(value, (_ReadOnlyDict, _ReadOnlyList, _ReadOnlyTuple, _ReadOnlySet)):
        return value
    if isinstance(value, dict):
        return _ReadOnlyDict(value)
    if isinstance(value, list):
        return _ReadOnlyList(value)
    if isinstance(value, tuple):
        return _ReadOnlyTuple(value)
    if isinstance(value, set):
        return _ReadOnlySet(value)
    if isinstance(value, frozenset):
        return frozenset(_read_only(member) for member in value)
    return value


def _plain_copy(value: Any, memo: dict[int, Any] | None = None) -> Any:
    """A plain, fully mutable copy of a read-only view.

    Every wrapped container is rebuilt as its plain counterpart and every leaf
    kept as it is, so a caller that copies a document handed out inside a region
    gets back an ordinary value it may change; the view it copied from is
    untouched, and the memo behind it stays unchanged. ``memo`` carries the
    objects :func:`copy.deepcopy` already copied, so a view that names the same
    container twice is copied once.
    """

    seen = {} if memo is None else memo
    existing = seen.get(id(value))
    if existing is not None:
        return existing
    if isinstance(value, _ReadOnlyDict):
        copied: Any = {}
        seen[id(value)] = copied
        for key, leaf in value.items():
            copied[key] = _plain_copy(leaf, seen)
        return copied
    if isinstance(value, _ReadOnlyList):
        copied = []
        seen[id(value)] = copied
        copied.extend(_plain_copy(leaf, seen) for leaf in value)
        return copied
    if isinstance(value, _ReadOnlyTuple):
        copied = tuple(_plain_copy(leaf, seen) for leaf in value)
        seen[id(value)] = copied
        return copied
    if isinstance(value, _ReadOnlySet):
        copied = set()
        seen[id(value)] = copied
        copied.update(_plain_copy(member, seen) for member in value)
        return copied
    return copy.deepcopy(value, seen)


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

    return signature_from_stat(os.stat(path))


def signature_from_stat(stat: os.stat_result) -> _Signature:
    """Return the stat identity that any write to the observed file changes.

    The one place the identity's fields and their order are written. A caller
    that has already observed the file — a shared walk that must not stat it
    again — builds the same identity here rather than naming the fields itself.
    """

    return (
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
    )


def file_revision(stat: os.stat_result) -> str:
    """Return the revision token a served file is addressed by.

    Derived from the file's stat identity — inode, size and modification time —
    so a rewritten file yields a new token and an untouched one yields the same
    token across processes. It is the same value the ETag is built from, so a
    URL carrying it as ``?rev=`` and the response's validator agree by
    construction. The index row carries it and the file routes check it, so a
    caller holding a stat identity builds the token here rather than elsewhere.
    """

    return f"{stat.st_ino:x}-{stat.st_size:x}-{stat.st_mtime_ns:x}"


def content_digest(path: Path | str) -> str:
    """Return a byte digest, sharing an unchanged file's one physical read.

    The row is keyed on the file's bytes rather than its stat identity: a
    rewrite that restores the same bytes — a checkout or a rebuild that only
    touches the mtime — reuses the parsed row, while any byte change re-parses
    it. A shared filesystem also reports a different device number for the same
    file on each host, which a byte digest is blind to, so the served process
    and the readers on other hosts share one persisted index instead of each
    re-parsing the whole project. The process cache is revalidated by the file's
    full stat identity; a changed file is hashed again.

    A parse keyed on this digest is shared by every path holding byte-identical
    content, so the same document reached through a second tree costs one hash
    rather than a second parse. Reading the whole file is dearer than a stat,
    so only a caller that knows its content repeats across paths asks for it —
    see ``memoized(..., by_content=True)``.

    An HTML document's decoded text is stored as it is hashed, so a caller that
    parses the document after keying on this digest decodes the bytes this read
    already returned rather than reading the file a second time. The text is
    stored only under the signature its bytes were read at: the signature is
    taken before the read and checked again before the text is handed over, so
    a write landing in between makes the store re-read rather than pair one
    file's bytes with another's stat identity.
    """

    target = Path(path)

    def read() -> str:
        signature = file_signature(target)
        content = target.read_bytes()
        if target.suffix == ".html":

            def decoded() -> str:
                try:
                    if file_signature(target) == signature:
                        return content.decode("utf-8", errors="replace")
                    return target.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    return ""

            memoized("plan_text", target, decoded)
        return hashlib.sha256(content).hexdigest()

    return memoized("content_digest", target, read)


def memoized[T](
    kind: str,
    path: Path | str,
    compute: Callable[[], T],
    *,
    variant: Hashable = None,
    mutable: bool = False,
    by_content: bool = False,
) -> T:
    """Return ``compute()`` for ``path``, reusing it while the file is unchanged.

    ``kind`` names the parse so two parsers of one file never share an entry;
    ``variant`` distinguishes calls whose arguments change the result. A file
    that cannot be stat'ed is parsed uncached, so its error path is unchanged.

    ``by_content`` swaps the stat identity for the file's content digest, so
    byte-identical content at two paths is parsed once. Every caller that does
    not ask for it keeps the stat signature and pays no hash.

    Inside a :func:`read_only_region` the value is stat'ed and decoded once and
    handed to the caller as a read-only view of it, so mutating what is read is
    refused; ``mutable=True`` asks for a mutable copy at that hand-over, for a
    caller that will change what it is given.
    """

    region = _REGION.get()
    if by_content:
        try:
            identity: _Identity = content_digest(path)
        except OSError:
            return compute()
        key = (kind, f"content:{identity}", variant)
    else:
        try:
            identity = file_signature(path)
        except OSError:
            return compute()
        key = (kind, os.fspath(path), variant)
    if region is not None and key in region:
        return _handout(region[key], mutable)  # type: ignore[return-value]
    with _LOCK:
        entry = _CACHE.get(key)
        if entry is not None and entry[0] == identity:
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
            _CACHE[key] = (identity, value)
            _CACHE.move_to_end(key)
            while len(_CACHE) > _MAX_ENTRIES:
                _CACHE.popitem(last=False)
        region[key] = [value, None]
        return _handout(region[key], mutable)  # type: ignore[return-value]
    try:
        unchanged = (
            content_digest(path) == identity
            if by_content
            else file_signature(path) == identity
        )
    except OSError:
        unchanged = False
    if unchanged:
        # A write that landed mid-parse leaves the entry unstored, so the
        # next reader parses the new bytes rather than a torn read.
        with _LOCK:
            _CACHE[key] = (identity, copy.deepcopy(value))
            _CACHE.move_to_end(key)
            while len(_CACHE) > _MAX_ENTRIES:
                _CACHE.popitem(last=False)
    return value


def clear() -> None:
    """Drop every memoised parse."""

    with _LOCK:
        _CACHE.clear()
