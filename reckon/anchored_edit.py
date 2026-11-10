"""Line-anchored edits over MCP, confined to the roots the server was given.

A model that retypes a `old_string` it has misremembered spends a turn on the
refusal and another re-reading the file before it can retry. An anchor is a
line number plus three hex characters of that line's SHA-1: it is short, the
tool verifies it before writing anything, and a stale anchor comes back with
the current lines so the retry needs no re-read.

Two tools are served:

``read_anchored(path, offset, limit)``
    Renders lines as ``N:HHH|text``. The limit keeps a call under about 400
    lines, and a truncated read says how many lines remain.

``edit_anchored(path, edits)``
    Applies replace, insert_before, insert_after and delete operations, each
    addressed by an anchor of the form ``N:HHH`` or a bare ``HHH`` when exactly
    one line in the file carries that hash. Every anchor is verified, and every
    overlap detected, before a byte is written; a refusal therefore leaves the
    file byte-identical and returns fresh anchors for the region in question.

Confinement is the server's own. An MCP server runs outside a worker's command
sandbox, so a path is resolved through every link and checked against the
``--root`` arguments the process was started with. A server started with no
root refuses every path. The check reuses the fence's ``_within_any`` rather
than growing a third write-root rule beside it.

The file is replaced atomically through the project's shared writer, which
preserves the mode; this module preserves the file's line endings and whether
it ends in a newline.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from mcp.server.fastmcp import FastMCP

    _HAS_MCP = True
except ImportError:  # pragma: no cover - the package is a declared dependency
    _HAS_MCP = False
    FastMCP = None  # type: ignore[assignment,misc]

from reckon._store import write_atomically
from reckon._worker_fence import _within_any

DEFAULT_LIMIT = 400
HASH_CHARS = 3
ANCHOR_FORM = (
    "N:HHH, or HHH when exactly one line in the file carries that hash "
    "(HHH is three hex characters of the line's SHA-1)"
)
OPS = frozenset({"replace", "insert_before", "insert_after", "delete"})
REGION_CONTEXT = 2


def _line_hash(content: bytes) -> str:
    """The anchor hash of one line: three hex characters of its SHA-1.

    The digest is an identity for the bytes, never a security property, so the
    costlier construction SHA-1 would otherwise demand is explicitly declined.
    """
    return hashlib.sha1(content, usedforsecurity=False).hexdigest()[:HASH_CHARS]


@dataclass(frozen=True)
class _Line:
    content: bytes
    ending: bytes


def _split_lines(data: bytes) -> list[_Line]:
    """Split bytes into content and terminator, preserving both exactly."""
    lines: list[_Line] = []
    start = 0
    while start < len(data):
        newline = data.find(b"\n", start)
        if newline == -1:
            lines.append(_Line(data[start:], b""))
            break
        content = data[start:newline]
        ending = b"\n"
        if content.endswith(b"\r"):
            content = content[:-1]
            ending = b"\r\n"
        lines.append(_Line(content, ending))
        start = newline + 1
    return lines


def _dominant_ending(lines: list[_Line]) -> bytes:
    """The terminator a newly inserted line takes: the file's prevailing one."""
    crlf = sum(1 for line in lines if line.ending == b"\r\n")
    if crlf and crlf * 2 >= sum(1 for line in lines if line.ending):
        return b"\r\n"
    return b"\n"


def _split_text(text: str) -> list[str]:
    """Split supplied edit text into lines, without inventing a trailing one."""
    if text == "":
        return []
    parts = [part.removesuffix("\r") for part in text.split("\n")]
    if parts and parts[-1] == "":
        parts.pop()
    return parts


def _decode(content: bytes) -> str:
    return content.decode("utf-8", errors="backslashreplace")


def _render(lines: list[_Line], first: int, last: int) -> str:
    """Render 1-based inclusive line numbers as ``N:HHH|text``."""
    return "\n".join(
        f"{number}:{_line_hash(lines[number - 1].content)}|{_decode(lines[number - 1].content)}"
        for number in range(first, last + 1)
    )


def _region(lines: list[_Line], first: int, last: int) -> str:
    """Render a window around 1-based ``first``..``last``, clamped to the file."""
    low = max(1, first - REGION_CONTEXT)
    high = min(len(lines), last + REGION_CONTEXT)
    return _render(lines, low, high)


def _refusal(*parts: str) -> str:
    return "\n".join(("refused:", *parts))


@dataclass(frozen=True)
class _Edit:
    index: int
    op: str
    text: list[str]


class AnchoredEditor:
    """The two tools, over one set of confinement roots."""

    def __init__(self, roots: list[str] | list[Path] | tuple[Path, ...] = ()) -> None:
        self._roots = tuple(Path(root).resolve() for root in roots)

    # ── confinement ────────────────────────────────────────────────────────

    def _resolve(self, path: str) -> Path:
        return Path(path).expanduser().resolve()

    def _outside(self, resolved: Path) -> bool:
        """Whether a resolved path escapes every root it was started with.

        With no roots this is True for every path, so a server started without
        ``--root`` can read and write nothing.
        """
        return not self._roots or not _within_any(resolved, self._roots)

    def _hash_matches(self, content: bytes, expected: str) -> bool:
        """Whether the line's hash is the one the anchor names."""
        return _line_hash(content) == expected.lower()

    def _outside_refusal(self, path: str, resolved: Path) -> str:
        roots = ", ".join(str(root) for root in self._roots) or "(none given)"
        return _refusal(
            f"{path} resolves to {resolved}, which is outside every --root this "
            f"server was started with: {roots}."
        )

    # ── anchors ────────────────────────────────────────────────────────────

    def _locate(
        self, anchor: str, lines: list[_Line], label: str
    ) -> tuple[int | None, str]:
        """Resolve one anchor to a 0-based index, or say why it cannot be."""
        text = anchor.strip()
        number: int | None = None
        digest = text
        if ":" in text:
            head, _, tail = text.partition(":")
            digest = tail
            if not head.isdigit():
                return None, (
                    f"malformed anchor {text!r}: the line number must be digits. "
                    f"Accepted anchors are {ANCHOR_FORM}."
                )
            number = int(head)
        if len(digest) != HASH_CHARS or any(
            c not in "0123456789abcdefABCDEF" for c in digest
        ):
            return None, (
                f"malformed anchor {text!r}: {digest!r} is not {HASH_CHARS} hex "
                f"characters. Accepted anchors are {ANCHOR_FORM}."
            )
        digest = digest.lower()

        if number is not None:
            if not 1 <= number <= len(lines):
                return None, (
                    f"anchor {text!r} names line {number}, but {label} has "
                    f"{len(lines)} lines. Accepted anchors are "
                    f"{ANCHOR_FORM}."
                )
            if not self._hash_matches(lines[number - 1].content, digest):
                actual = _line_hash(lines[number - 1].content)
                return None, (
                    f"stale anchor {text!r}: line {number} now hashes to {actual}; "
                    f"nothing was written. Current anchors around line {number}:\n"
                    + _region(lines, number, number)
                )
            return number - 1, ""

        matches = [
            index
            for index, line in enumerate(lines)
            if _line_hash(line.content) == digest
        ]
        if not matches:
            return None, (
                f"stale anchor {text!r}: no line in {label} carries that hash; "
                "nothing was written. Call read_anchored for the current anchors."
            )
        if len(matches) > 1:
            carried = ", ".join(str(index + 1) for index in matches)
            return None, (
                f"ambiguous anchor {text!r}: {len(matches)} lines carry that hash "
                f"({carried}). Write it as N:{digest} with the line you mean; "
                f"accepted anchors are {ANCHOR_FORM}."
            )
        return matches[0], ""

    # ── read ───────────────────────────────────────────────────────────────

    def read(self, path: str, offset: int = 1, limit: int = DEFAULT_LIMIT) -> str:
        """Render a file's lines with their anchors."""
        try:
            resolved = self._resolve(path)
        except (OSError, ValueError) as error:
            return _refusal(f"{path!r} is not a usable path: {error}")
        if self._outside(resolved):
            return self._outside_refusal(path, resolved)
        try:
            lines = _split_lines(resolved.read_bytes())
        except OSError as error:
            return _refusal(f"cannot read {path}: {error}")
        if not lines:
            return f"{path} has 0 lines."
        if offset < 1 or offset > len(lines):
            return _refusal(
                f"offset {offset} is out of range: {path} has {len(lines)} lines "
                "and offset is 1-based."
            )
        if limit < 1:
            return _refusal(f"limit {limit} must be at least 1.")
        last = min(len(lines), offset + limit - 1)
        body = _render(lines, offset, last)
        remaining = len(lines) - last
        if remaining:
            body += (
                f"\n-- {remaining} more lines; call read_anchored with "
                f"offset={last + 1} for the rest"
            )
        return body

    # ── edit ───────────────────────────────────────────────────────────────

    def edit(self, path: str, edits: list[dict[str, Any]]) -> str:
        """Apply anchored edits atomically, refusing the whole call on any fault."""
        try:
            resolved = self._resolve(path)
        except (OSError, ValueError) as error:
            return _refusal(f"{path!r} is not a usable path: {error}")
        if self._outside(resolved):
            return self._outside_refusal(path, resolved)
        if not edits:
            return _refusal("no edits were given.")
        try:
            original = resolved.read_bytes()
        except OSError as error:
            return _refusal(f"cannot read {path}: {error}")
        lines = _split_lines(original)

        problems: list[str] = []
        before: dict[int, list[str]] = defaultdict(list)
        after: dict[int, list[str]] = defaultdict(list)
        replacements: dict[int, list[str]] = {}
        deletions: set[int] = set()
        claims: dict[int, int] = {}

        for number, raw in enumerate(edits, 1):
            parsed = self._parse_edit(raw, number, lines, path, problems)
            if parsed is None:
                continue
            if parsed.op == "insert_before":
                before[parsed.index].extend(parsed.text)
                continue
            if parsed.op == "insert_after":
                after[parsed.index].extend(parsed.text)
                continue
            if parsed.index in claims:
                problems.append(
                    f"edits {claims[parsed.index]} and {number} both change line "
                    f"{parsed.index + 1}; overlapping edits are refused."
                )
                continue
            claims[parsed.index] = number
            if parsed.op == "delete":
                deletions.add(parsed.index)
            else:
                replacements[parsed.index] = parsed.text

        if problems:
            return _refusal(
                f"{len(problems)} problem(s); nothing was written to {path}.",
                *problems,
            )

        return self._apply(
            resolved,
            path,
            len(edits),
            original,
            lines,
            before,
            after,
            replacements,
            deletions,
        )

    def _parse_edit(
        self,
        raw: Any,
        number: int,
        lines: list[_Line],
        path: str,
        problems: list[str],
    ) -> _Edit | None:
        if not isinstance(raw, dict):
            problems.append(
                f"edit {number}: expected an object with anchor, op and optional text."
            )
            return None
        anchor = raw.get("anchor")
        op = raw.get("op")
        text = raw.get("text", "")
        if not isinstance(anchor, str) or not anchor.strip():
            problems.append(f"edit {number}: anchor is required, as {ANCHOR_FORM}.")
            return None
        if op not in OPS:
            problems.append(
                f"edit {number}: op {op!r} is not one of {', '.join(sorted(OPS))}."
            )
            return None
        if not isinstance(text, str):
            problems.append(f"edit {number}: text must be a string.")
            return None
        new_lines = _split_text(text)
        if op != "delete" and not new_lines:
            problems.append(
                f"edit {number}: op {op!r} needs text (a delete needs none)."
            )
            return None
        index, refusal = self._locate(anchor, lines, path)
        if index is None:
            problems.append(f"edit {number}: {refusal}")
            return None
        return _Edit(index, op, new_lines)

    def _apply(
        self,
        resolved: Path,
        path: str,
        edit_count: int,
        original: bytes,
        lines: list[_Line],
        before: dict[int, list[str]],
        after: dict[int, list[str]],
        replacements: dict[int, list[str]],
        deletions: set[int],
    ) -> str:
        ending = _dominant_ending(lines)
        had_final_newline = original.endswith(b"\n")
        out: list[_Line] = []
        affected: list[int] = []

        def added(texts: list[str]) -> None:
            for text in texts:
                affected.append(len(out))
                out.append(_Line(text.encode("utf-8"), ending))

        for index, line in enumerate(lines):
            if index in before:
                added(before[index])
            if index in deletions:
                affected.append(len(out))
            elif index in replacements:
                added(replacements[index])
            else:
                out.append(line)
            if index in after:
                added(after[index])

        # The terminator policy: a new line takes the file's prevailing ending,
        # and the file keeps or loses its final newline exactly as it had one.
        for position in range(len(out) - 1):
            if not out[position].ending:
                out[position] = _Line(out[position].content, ending)
        if out:
            last = out[-1]
            wanted = ending if had_final_newline else b""
            if last.ending != wanted:
                out[-1] = _Line(last.content, ending if wanted else b"")

        region = _region(out, min(affected) + 1, max(affected) + 1) if affected else ""

        new_bytes = b"".join(line.content + line.ending for line in out)
        try:
            mode = os.stat(resolved).st_mode & 0o7777
            write_atomically(
                resolved,
                lambda handle: handle.write(new_bytes),
                mode=mode,
                binary=True,
            )
        except OSError as error:
            return _refusal(f"cannot write {path}: {error}")

        summary = f"edited {path}: {edit_count} edit(s) applied."
        if region:
            return f"{summary}\nCurrent anchors for the changed region:\n{region}"
        return summary


# ── MCP server ─────────────────────────────────────────────────────────────

INSTRUCTIONS = (
    "Read files with read_anchored and change them with edit_anchored. Each "
    "line is shown as N:HHH|text where HHH is three hex characters of that "
    "line's SHA-1 of its content. Address an edit by N:HHH, or by HHH alone "
    "when exactly one line carries that hash. Every anchor is verified before "
    "anything is written, so a stale anchor refuses the whole call and returns "
    "the current lines; retry from those anchors instead of re-reading the "
    "file. Paths outside the roots this server was started with are refused."
)


def build_server(roots: list[str] | list[Path] | tuple[Path, ...] = ()) -> FastMCP:
    """A FastMCP server serving the two tools over one set of roots."""
    editor = AnchoredEditor(roots)
    server = FastMCP("reckon-anchored-edit", instructions=INSTRUCTIONS)

    @server.tool()
    def read_anchored(path: str, offset: int = 1, limit: int = DEFAULT_LIMIT) -> str:
        """Read a file as anchorable lines (N:HHH|text)."""
        return editor.read(path, offset=offset, limit=limit)

    @server.tool()
    def edit_anchored(path: str, edits: list[dict[str, Any]]) -> str:
        """Apply anchored edits atomically; each edit has anchor, op and text."""
        return editor.edit(path, edits)

    return server


def main(argv: list[str] | None = None) -> None:
    """Parse the roots and serve both tools over stdio."""
    if not _HAS_MCP or FastMCP is None:
        msg = "mcp package not found; it is a declared dependency of reckon."
        raise SystemExit(msg)
    parser = argparse.ArgumentParser(
        prog="reckon-anchored-edit",
        description=(
            "Line-anchored edit tools over MCP stdio. Every path is confined "
            "to the roots given with --root; with none, nothing is readable or "
            "writable."
        ),
    )
    parser.add_argument(
        "--root",
        action="append",
        default=[],
        metavar="DIR",
        help="a directory the server may read and write (repeatable)",
    )
    build_server(parser.parse_args(argv).root).run(transport="stdio")


if __name__ == "__main__":
    main()
