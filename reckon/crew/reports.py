"""Read a worker's manifest through a declared schema, not by scanning lines.

A worker writes its manifest as ``key: value`` text, commonly with prose around
it and Markdown decoration on the ``key:`` lines. That text form is YAML's own
shape — a ``|`` block scalar, a quoted scalar, an indented body, a ``- `` list —
so the reader declares a schema of fields and parses each field's body with a
real parser, :func:`yaml.compose`. Seven point repairs to the previous line
scanner did not generalise, because the scanner inferred a field's *type* from
how it happened to join and split a string: a comma in a manifest body then
applies to the wrong things. Measured cases the schema removes: a comma inside a
commit subject manufactured a fourth revision; a nested mapping arrived as the
empty string; a quoted item in a flow list was cut on the comma it had quoted
to protect; and a nested key was read out of its parent.

``yaml.compose`` rather than ``yaml.safe_load``: ``compose`` decodes the
structure — a sequence is a list, an indented body is a mapping, a quoted scalar
loses its quotes — while leaving every scalar's literal *text* alone, so an
identifier that happens to look numeric is never re-typed. ``compose`` is what
keeps the reader's two fixed defects fixed: ``1e5`` is an object id, not a
float, and a value's type comes from the schema rather than from splitting.

A list field is commonly written with its entries labelled — ``test_logs:``
followed by ``gate_after: <path>`` lines, or a ``commits`` block of
sha-and-subject lines carrying no bullet — and that text composes as a mapping
where the field declares a list. What a mapping in a list field means is one
item per line, each item the worker's own text for that line in the order
written. Both alternatives are worse than reading it: refusing it turned 27
delivered manifests on disk into refusals the previous line-scanning reader had
accepted, and taking it as the mapping itself hands a consumer a dict's keys
where paths and object ids belong.

Tolerance is preserved deliberately. A body whose composed shape the field
accepts is that shape; a body that is not well-formed YAML, or whose shape the
field does not declare, falls back to the text the worker wrote whenever the
field accepts text. A field that accepts neither the composed shape nor text is
refused, naming the field and the shapes the schema declares, because a wrong
value carried forward as a plausible one is worse than an honest refusal. A
mapping that composes as another mapping's key is unhashable, and it is read as
its own text rather than raising: no manifest a worker delivered may crash the
reader.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path, PurePath, PurePosixPath
from typing import Any, TypedDict

import yaml

from reckon import doccheck, ledger
from reckon.crew import review as review_module
from reckon.crew.node import (
    NEEDS_HELP_FIELDS,
    NEEDS_HELP_MARKER,
    CrewError,
    TaskNode,
    is_test_path,
    negative_control_is_none,
    parse_duration,
)
from reckon.crew.runs import _utc_now

# ── Worker reports ──────────────────────────────────────────────────────────

_MANIFEST_LIST_KEYS = (
    "commits",
    "changed_paths",
    "retry_failures",
    "test_logs",
    "artifacts",
    "evidence_inputs",
    "follow_ons",
    "blockers",
)
_NONE_VALUES = {"", "none", "n/a", "-", "nil"}

# The status spellings a worker writes. This is the single statement of the
# vocabulary: the classifier decides "was the worker working, not done" against
# these same sets, and the reader refuses a status word that is in neither set
# rather than carrying it forward as a state. A declared wait is its own
# outcome (recovery's WAITING_STATUS is the ``waiting`` member), and an
# unrecognised spelling is not proof of work, so neither is a member of the
# terminal or non-terminal sets.
TERMINAL_MANIFEST_STATUSES = frozenset({"complete", "blocked", "failed"})
NON_TERMINAL_MANIFEST_STATUSES = frozenset(
    {"in-progress", "in_progress", "running", "pending"}
)
# Every status value the reader accepts: terminal, still working, a declared
# wait, or a recovery artifact. ``derived`` is written by recovery's own
# manifest-fabrication (never by a worker) and the classifier reads it back
# through the ``derived`` field, so the reader must recognise it as a status
# rather than refuse the recovery pipeline's own file. A status outside this
# set and outside the unsubstituted template below leaves the outcome
# undetermined and the manifest is refused rather than the word being carried
# forward as a state.
MANIFEST_STATUSES = (
    TERMINAL_MANIFEST_STATUSES | NON_TERMINAL_MANIFEST_STATUSES | {"waiting", "derived"}
)


def manifest_status_is_template(value: Any) -> bool:
    """Whether a status still carries the dispatch contract's placeholder.

    An unsubstituted template is evidence the worker never wrote a verdict, not
    a fourth spelling of one, so it outlives the reader and reaches the
    classifier's unwritten handling rather than being refused as an unrecognised
    word.
    """
    status = str(value or "").strip().lower()
    choices = {part.strip(" <>\t") for part in status.split("|")}
    return "|" in status and choices == set(TERMINAL_MANIFEST_STATUSES)


# The declared schema: every manifest field and the shapes it accepts, as the
# single statement of what each field's value *is*. The parser is driven by this
# mapping rather than by the spelling of a line, so a field's type is what the
# schema says and not whatever joining-and-splitting a string happened to
# produce. ``text`` is one literal scalar; ``list`` is an ordered set of literal
# items; ``mapping`` is a nested key/value body. A field that carries more than
# one shape in the manifests on disk declares all of them — ``tests`` is commonly
# a sentence and sometimes a nested result map, and ``evidence_inputs`` is a list
# in the text form and a mapping in the JSON form.
_TEXT_SHAPE = frozenset({"text"})
_LIST_SHAPE = frozenset({"list"})
_MAPPING_SHAPE = frozenset({"mapping"})
_MANIFEST_SCHEMA: dict[str, frozenset[str]] = {
    **dict.fromkeys((*_MANIFEST_LIST_KEYS, "orientation_write_paths"), _LIST_SHAPE),
    "evidence_inputs": frozenset({"list", "mapping"}),
    "tests": frozenset({"text", "mapping"}),
    "failure_attribution": frozenset({"text", "mapping"}),
    "baseline_suite": frozenset({"text", "mapping"}),
    "after_suite": frozenset({"text", "mapping"}),
    "node": _TEXT_SHAPE,
    "status": _TEXT_SHAPE,
    "needs_help": _TEXT_SHAPE,
    "derived": _TEXT_SHAPE,
    "orientation_worktree": _TEXT_SHAPE,
    "orientation_base_sha": _TEXT_SHAPE,
    "measurement_module": _TEXT_SHAPE,
    "measurement_cwd": _TEXT_SHAPE,
}

# A text body whose parsed top-level fields are all outside the schema is
# incidental prose wearing a ``key: value`` shape — the body carries no field a
# manifest carries, so its status cannot be determined.
_MANIFEST_FIELD_KEYS = frozenset(_MANIFEST_SCHEMA)

# Fields whose items are identifiers a later command resolves, so their
# spelling must survive the parse. YAML's implicit typing would rewrite an
# identifier that resembles a number, and the reader has twice had to undo that
# (``1e5`` written as ``100000.0``); composing their flow sequences as literal
# text also keeps a quoted item whole, comma and all.
_IDENTIFIER_FIELDS = frozenset({"commits"})

# A manifest field whose presence alongside a missing status key still leaves
# the outcome undetermined. The list-attribute fields are exempt from this: a
# bare ``commits:``-shaped excerpt is a fragment of a manifest being read for
# its attributes, not a manifest that failed to declare a verdict, and the
# nested-key handling pins that such a fragment keeps parsing.
_OUTCOME_MANIFEST_FIELDS = (
    _MANIFEST_FIELD_KEYS
    - frozenset(_MANIFEST_LIST_KEYS)
    - frozenset({"status", "derived"})
)


# A line whose value is one of these has no value on that line at all — the
# indented body below it is the value, YAML block-scalar style. Returning the
# indicator itself is how a parse failure became a display that lied: a
# blocked run's reason once read as a single "|" character.
_BLOCK_SCALAR_RE = re.compile(r"^[|>][+-]?$")


def _is_block_indicator(value: str) -> bool:
    return bool(_BLOCK_SCALAR_RE.match(value)) or value in ('"', "'")


# A scalar a worker surrounded with one matching quote pair carries the quotes
# as presentation, YAML-style, not as part of the value. A worker that writes
# ``status: "complete"`` is stating a recognised verdict; leaving the quotes on
# the value turns it into an unrecognised status word, and the reader refuses
# the whole manifest — one field's formatting costs every field. The pair is
# stripped wherever a top-level scalar is read, before the status vocabulary,
# the block indicator and the embedded-key check all see it. Only a matching
# pair is removed, so an apostrophe inside a word keeps its place and a lone
# quote is left as written rather than half-stripped.
_QUOTE_CHARS = ('"', "'")


def _strip_matching_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in _QUOTE_CHARS:
        return value[1:-1]
    return value


def _is_quoted_scalar(value: str) -> bool:
    """Whether a value is one scalar the worker surrounded with a quote pair."""
    return len(value) >= 2 and value[0] == value[-1] and value[0] in _QUOTE_CHARS


# A manifest field key appearing later on the same line as another key's value.
# The tolerant reader captures the whole remainder of the line as the first
# key's value, so ``commits: ...; changed_paths: ...`` turns the commit into a
# corrupted string and the changed paths into an empty list — the shape the
# promotion guard for changed paths without a commit was built to catch. That
# pair is unambiguously structural because both fields are machine attributes.
# Other manifest words may occur naturally inside free prose, so matching the
# whole vocabulary would invent a top-level field that the author never wrote.
# A value that is itself a JSON literal is read whole because its keys are
# nested data rather than another top-level field.
_MANIFEST_KEY_ALTERNATION = "|".join(
    re.escape(key) for key in sorted(_MANIFEST_FIELD_KEYS, key=len, reverse=True)
)
_MANIFEST_KEY_ON_LINE_RE = re.compile(
    rf"\b({_MANIFEST_KEY_ALTERNATION})\s*:", re.IGNORECASE
)
_MARKDOWN_MANIFEST_FIELD_RE = re.compile(
    rf"^\s*(?:[-*]\s+)?(?:\*\*)?"
    rf"(?P<key>{_MANIFEST_KEY_ALTERNATION})(?:\*\*)?\s*:"
    rf"(?:\*\*)?\s*(?P<value>.*)$",
    re.IGNORECASE,
)
_SAME_LINE_FIELD_PAIRS = {"commits": frozenset({"changed_paths"})}


def _embedded_manifest_key(first: str, value: str) -> str | None:
    """Return the manifest key a top-level value carries, or None."""
    if not value:
        return None
    try:
        json.loads(value)
    except (TypeError, json.JSONDecodeError):
        pass
    else:
        return None
    permitted_seconds = _SAME_LINE_FIELD_PAIRS.get(first, ())
    for match in _MANIFEST_KEY_ON_LINE_RE.finditer(value):
        second = match.group(1).lower()
        if second in permitted_seconds:
            return second
    return None


def _two_keys_on_one_line_message(
    path: str | None, line_no: int, line: str, first: str, second: str
) -> str:
    where = f" at {path}" if path else ""
    return (
        f"cannot read manifest{where}: line {line_no} ({line!r}) carries two "
        f"keys on one line — '{first}:' is followed by '{second}:' before the "
        "line ends; the format is one key per line, and reading both would turn "
        "the earlier value into a corrupted string and the later key into an "
        "empty list, so the line is refused rather than silently misparsed"
    )


class ManifestParseError(CrewError, ValueError):
    """A manifest the reader can read as neither supported format.

    Subclasses both :class:`CrewError` and :class:`ValueError` so the refusal
    lands loudly on the classification and CLI surfaces (which catch
    ``CrewError``) and is still accepted by the promotion guards (which
    tolerate ``ValueError`` around a worker-authored file).
    """


class SuiteObservation(TypedDict):
    """One machine-readable suite result carried by a worker manifest."""

    revision: str
    command: str
    exit_status: int | None
    log_path: str
    log_digest: str
    completed: bool | None
    failure_count: int | None
    failure_ids: list[str] | None


def parse_manifest(text: str, *, path: str | None = None) -> dict[str, Any]:
    """Parse a worker manifest into structured fields.

    Two formats are read. A body whose first character is ``{`` or ``[`` is a
    JSON document and must be a JSON object; any other body is the tolerant
    ``key: value`` text form a worker writes around prose. A body that
    declares itself JSON and is not a readable object raises
    :class:`ManifestParseError` rather than falling back to the text reader —
    the text reader would return a well-formed-looking partial mapping, which
    is how a JSON manifest carrying ``"status": "complete"`` once came back
    with eight recognised keys and no status. A body whose status cannot be
    determined raises the same error: a non-blank text body that yields no
    ``key: value`` field at all — its ``status`` and ``commits`` under
    markdown headings, say — a body whose parsed fields are all incidental
    prose rather than manifest fields, a body whose parsed fields read as
    manifest fields but omit the status key altogether (a report wearing the
    ``node:`` shape), and a status that names a word no part of the system
    recognises. Each of these once fell back to the normalised partial mapping
    with no usable status, which the classifier read as a dead worker. Unknown
    keys are kept in both forms so nothing a worker took the trouble to state
    is silently dropped.

    Tolerant on purpose for the text form: a worker writes prose around its
    manifest and a strict parser would reject a delivered report over
    formatting.
    """
    if text.lstrip().startswith(("{", "[")):
        fields = _read_json_manifest(text, path=path)
    else:
        fields = _parse_text_manifest(text, path=path)
        if not fields and text.strip():
            # A non-blank text body from which no ``key: value`` field could be
            # read at all — a markdown-heading layout, say, where ``status`` and
            # ``commits`` sit under headings rather than at column 0 — parses to
            # an empty field set and then to the normalised shape with no
            # status and no commits, which the classifier reads as a vanished
            # worker. Such a body raises rather than half-succeeding.
            raise ManifestParseError(_unreadable_text_manifest_message(path))
    fields = _refuse_undetermined_status(fields, path)
    fields = _normalise_manifest_fields(fields)
    fields["needs_help"] = parse_needs_help(text) if NEEDS_HELP_MARKER in text else None
    return fields


def _line_indent(raw: str) -> int:
    """Return the number of leading whitespace characters on a line's raw form."""
    return len(raw) - len(raw.lstrip(" \t"))


def _parse_text_manifest(text: str, *, path: str | None = None) -> dict[str, Any]:
    """Read the tolerant ``key: value`` text form, keeping unknown keys.

    The least-indented recognised manifest field establishes the top-level
    column. This preserves a whole manifest presented inside an indented
    Markdown block while preventing a key indented beneath a parent from being
    promoted into the top-level mapping. That same column is the block scalar's
    floor: a block body is written deeper than the fields around it, so a
    following field ends the block. Anchoring the end at column zero instead
    folds every field after a block scalar into its value whenever the manifest
    is wholly indented, because there the fields themselves begin with
    whitespace; the field that quietly empties is the commit list a promotion
    reads.

    A top-level line carrying a second manifest key after its value is refused
    here rather than misparsed: the tolerant reader would otherwise fold the
    whole remainder of the line into the first key's value and leave the later
    field absent (an empty list), which reads as a protection the worker never
    wrote. ``path`` names the file in the refusal.
    """
    fields: dict[str, Any] = {}
    key: str | None = None
    body_lines: list[str] = []
    body_is_block = False
    manifest_indent = min(
        (
            _line_indent(raw)
            for raw in text.splitlines()
            if _MARKDOWN_MANIFEST_FIELD_RE.match(raw)
        ),
        default=0,
    )

    def flush() -> None:
        nonlocal key, body_lines, body_is_block
        if key is not None:
            fields[key] = _read_field_body(key, body_lines, body_is_block, path=path)
        key = None
        body_lines = []
        body_is_block = False

    def read_field(raw: str, line: str) -> re.Match[str] | None:
        # Workers commonly present manifest fields as Markdown list items or
        # emphasize their keys. Restrict the decorated form to the manifest
        # vocabulary so a prose bullet containing a colon stays prose.
        indent = _line_indent(raw)
        if indent != manifest_indent:
            return None
        decorated = _MARKDOWN_MANIFEST_FIELD_RE.match(raw)
        if decorated:
            return decorated
        return re.match(
            r"^(?P<key>[a-z][a-z0-9_-]*)\s*:\s*(?P<value>.*)$",
            line,
            re.IGNORECASE,
        )

    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        match = read_field(raw, line)
        if key is not None and match is None:
            # The body of the field just opened. It continues over blank lines,
            # over lines indented past the column the manifest's own fields
            # occupy, and over ``- item`` lines at the column itself; a field
            # line at that column ends it and is read below. The block ends at
            # the field column rather than at column zero because a manifest is
            # commonly presented wholly indented, where a trailing block would
            # otherwise swallow every field after it — the field that quietly
            # empties there is the commit list a promotion reads. The body is
            # parsed as a whole when the field closes, so its inner commas and
            # its nesting follow the declared shape rather than a join-and-split
            # of the lines.
            if (
                line == ""
                or _line_indent(raw) > manifest_indent
                or line.startswith(("-", "*"))
            ):
                body_lines.append(line)
            # A prose line inside the body belongs to no field and is dropped,
            # but it does not close the field it sits in: the old reader kept
            # the key open, so a list item written after a stray prose line
            # still reached its key.
            continue
        flush()
        if match:
            key = match.group("key").lower().replace("-", "_")
            raw_value = match.group("value").strip()
            embedded = _embedded_manifest_key(key, _strip_matching_quotes(raw_value))
            if embedded:
                raise ManifestParseError(
                    _two_keys_on_one_line_message(
                        path, line_no=line_no, line=line, first=key, second=embedded
                    )
                )
            if key == "status" and re.fullmatch(r"`[^`]+`", raw_value):
                raw_value = raw_value[1:-1].strip()
            if raw_value == "":
                fields.setdefault(key, "")
                continue
            if _is_block_indicator(_strip_matching_quotes(raw_value)):
                fields.setdefault(key, "")
                body_is_block = True
                continue
            fields[key] = _read_inline_value(key, raw_value)
            key = None
    flush()
    return fields


def _shape_of(value: Any) -> str:
    """The composed shape of a field value, for comparison with the schema."""
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "mapping"
    return "text"


def _span_source_text(source: str, first: Any, last: Any) -> str:
    """The worker's own text between two composed nodes, whitespace-normalised."""
    return " ".join(source[first.start_mark.index : last.end_mark.index].split())


def _decode_yaml_node(node: Any, source: str) -> Any:
    """Decode a composed YAML node, keeping every scalar's literal text.

    A sequence becomes a list, a mapping becomes a dict, and a scalar becomes
    ``node.value`` — the text the worker wrote, after YAML removes the quoting it
    applied. Reading ``node.value`` rather than the resolved Python object is
    what stops an object identifier that resembles a number (``1e5``) being
    written as one (``100000.0``): the schema, not YAML's implicit typing,
    decides a field's type.

    ``source`` is the composed text, which a key that is not a scalar composes
    into more than a string and so cannot become a dict key. Such a key stands in
    as its own span of the source rather than raising out of the reader.
    """
    if isinstance(node, yaml.SequenceNode):
        return [_decode_yaml_node(child, source) for child in node.value]
    if isinstance(node, yaml.MappingNode):
        return {
            _decode_mapping_key(key, source): _decode_yaml_node(value, source)
            for key, value in node.value
        }
    return node.value


def _decode_mapping_key(node: Any, source: str) -> Any:
    """A mapping key, taken as its own text when it is not hashable."""
    key = _decode_yaml_node(node, source)
    return (
        _span_source_text(source, node, node) if isinstance(key, (dict, list)) else key
    )


def _compose_node(text: str) -> Any:
    """The composed node for a manifest body, or None if it will not compose.

    A body that will not compose returns None rather than raising: a worker
    writes prose around its manifest, and the caller falls back to the text as
    written wherever the field's schema accepts text.
    """
    try:
        return yaml.compose(text)
    except yaml.YAMLError:
        return None


def _compose_document(text: str) -> Any:
    """Parse a manifest body, returning None when it is not well-formed YAML.

    ``yaml.compose`` is used rather than ``safe_load`` so the parse yields the
    structure while leaving implicit scalar typing alone.
    """
    node = _compose_node(text)
    if node is None:
        return None
    return _decode_yaml_node(node, text)


def _literal_list_items(text: str) -> list[str] | None:
    """A field body's items as the worker wrote them, each one literal text.

    Two shapes are read, and an item is literal text in both. A sequence's items
    are the writer's items, and an item that YAML types as a mapping is not the
    writer's intent for a field whose items are identifiers: a commit subject
    carrying a colon composes as a one-key mapping, and reading that as a dict
    hands a later command a mapping where an object id belongs. A mapping's
    entries are the lines a worker labelled its list with, so each entry is one
    item, keeping its label attached to the value it was written for rather than
    splitting the pair on its colon. In both shapes the writer's own span of the
    source stands in for the item, which is what keeps it whole however it is
    punctuated.
    """
    node = _compose_node(text)
    if isinstance(node, yaml.MappingNode):
        return [_span_source_text(text, key, value) for key, value in node.value]
    if not isinstance(node, yaml.SequenceNode):
        return None
    items = []
    for child in node.value:
        if isinstance(child, yaml.ScalarNode):
            items.append(child.value)
        else:
            items.append(_span_source_text(text, child, child))
    return items


def _body_document(lines: list[str]) -> str:
    """Dedent a field's body into a document the parser can read.

    The body is collected at the column the worker wrote it at, which for a
    wholly indented or tab-indented manifest is not column zero. Tabs are
    expanded first because YAML forbids them for indentation, and the common
    indent is then removed so the body stands as its own document.
    """
    expanded = [line.expandtabs(8) for line in lines]
    indents = [len(line) - len(line.lstrip()) for line in expanded if line.strip()]
    floor = min(indents, default=0)
    dedented = [line[floor:] if line.strip() else "" for line in expanded]
    return "\n".join(dedented).strip("\n")


def _joined_body_text(lines: list[str]) -> str:
    """Join a body written as list items into prose, dropping the bullets."""
    parts = [line.lstrip("-* ").strip() for line in lines if line.strip()]
    return ", ".join(parts)


def _schema_shape_message(
    path: str | None = None,
    field: str = "",
    shape: str = "",
    allowed: frozenset[str] = frozenset(),
) -> str:
    where = f" at {path}" if path else ""
    accepts = " or ".join(sorted(allowed))
    return (
        f"cannot read manifest{where}: field '{field}' carries a {shape} where "
        f"the manifest schema accepts {accepts}; the field is refused rather "
        "than carried forward as a plausible value"
    )


def _read_field_body(
    name: str, lines: list[str], is_block: bool, *, path: str | None
) -> Any:
    """Build a field's value from the body written beneath its key."""
    allowed = _MANIFEST_SCHEMA.get(name)
    if is_block:
        # ``|`` or ``>``: the body is the value's text, which is what a worker
        # writes for a prose field whatever shape the schema declares.
        return "\n".join(lines).strip()
    if not any(line.strip() for line in lines):
        return ""
    body = _body_document(lines)
    if name in _IDENTIFIER_FIELDS or allowed == _LIST_SHAPE:
        # A list field's items are the text the worker wrote, not whatever YAML
        # types that text to be: an item carrying a colon composes as a mapping,
        # and so does a field whose entries the worker labelled. Both are read
        # as one item per line — an object id, a path and a labelled entry are
        # not dicts, and a dict's keys where they belong is a silent misread.
        items = _literal_list_items(body)
        if items is not None:
            return items
    value = _compose_document(body)
    if value is None:
        # Not well-formed YAML: prose, kept as written.
        return (
            _joined_body_text(lines)
            if allowed == _LIST_SHAPE
            else "\n".join(lines).strip()
        )
    shape = _shape_of(value)
    if allowed is None or shape in allowed:
        return value
    if shape == "mapping":
        # A nested mapping under a field whose schema accepts text alone is the
        # malformed pair this reader refuses, rather than flattening it into a
        # comma list or emptying it to a blank — either of which reads as a
        # protection the worker never wrote. A list field does not reach here:
        # its mapping body was read as one item per labelled line above.
        raise ManifestParseError(_schema_shape_message(path, name, shape, allowed))
    return _joined_body_text(lines) if shape == "list" else "\n".join(lines).strip()


def _read_inline_value(name: str, raw_value: str) -> Any:
    """Build a field's value from the text written after its key on one line.

    An inline value is one scalar in this format, so it is returned as the
    worker's literal text with one matching quote pair removed; a ``list``
    field's comma-separated items are split downstream by :func:`_as_list`. The
    exception is a flow sequence in a field whose schema declares its items to
    be identifiers rather than data: there the sequence is composed through the
    parser as literal text, so a quoted item keeps the comma it quoted to
    protect instead of being cut on it, while every other field's flow list keeps
    the numeric decoding it has always had, where ``[1e5]`` is the number it
    names.
    """
    if name in _IDENTIFIER_FIELDS and raw_value.startswith("["):
        items = _literal_list_items(raw_value)
        if items is not None:
            return items
    return _strip_matching_quotes(raw_value)


def _read_json_manifest(text: str, *, path: str | None) -> dict[str, Any]:
    """Read a JSON manifest body, refusing anything that is not an object."""
    try:
        raw = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ManifestParseError(_unreadable_manifest_message(path)) from exc
    if not isinstance(raw, dict):
        raise ManifestParseError(_unreadable_manifest_message(path))
    return raw


def _unreadable_manifest_message(path: str | None) -> str:
    where = f" at {path}" if path else ""
    return (
        f"cannot read manifest{where}: the body starts as JSON but is not a "
        "well-formed JSON object; expected a JSON object or the "
        "'key: value' text form"
    )


def _unreadable_text_manifest_message(path: str | None) -> str:
    where = f" at {path}" if path else ""
    return (
        f"cannot read manifest{where}: the body is present but no 'key: value' "
        "field could be read, so its status cannot be determined; expected a "
        "JSON object or the 'key: value' text form"
    )


def _refuse_undetermined_status(
    fields: dict[str, Any], path: str | None
) -> dict[str, Any]:
    """Raise when the parsed fields still leave the status undetermined.

    Three shapes are undetermined rather than merely absent. A body whose
    parsed top-level fields are all outside the manifest vocabulary is prose
    wearing a ``key: value`` shape, not a manifest; a body whose parsed fields
    read as manifest fields but omit the status key entirely is a report
    wearing a manifest shape, not a worker verdict; and a status naming a word
    no part of the system recognises would be carried forward as a state it
    never was. All three raise. A bare list-attribute excerpt (``commits:``
    without a status, say) keeps parsing — it is a fragment of a manifest being
    read for its attributes, not a verdictless manifest — and a body declaring
    itself a recovery artifact through a truthy ``derived`` field keeps parsing
    too, because recovery fabricates that shape without a status when it
    preserves a terminal run's evidence. A well-formed terminal, non-terminal
    or waiting status — and an unsubstituted terminal template, which the
    classifier reads as unwritten rather than refused — keeps parsing.
    """
    if fields and not (set(fields) & _MANIFEST_FIELD_KEYS):
        raise ManifestParseError(
            _incidental_prose_manifest_message(path, sorted(fields))
        )
    if "status" not in fields and not _declares_derived(fields):
        outcome_fields = set(fields) & _OUTCOME_MANIFEST_FIELDS
        if outcome_fields:
            raise ManifestParseError(
                _missing_status_manifest_message(path, sorted(outcome_fields))
            )
    status = fields.get("status")
    if status is not None:
        if (
            not manifest_status_is_template(status)
            and str(status).strip().lower() not in MANIFEST_STATUSES
        ):
            raise ManifestParseError(
                _unknown_status_word_manifest_message(path, status)
            )
    return fields


def _incidental_prose_manifest_message(path: str | None, keys: list[str]) -> str:
    where = f" at {path}" if path else ""
    return (
        f"cannot read manifest{where}: the body reads as fields "
        f"({', '.join(keys)}) but none of them is a manifest field, so its "
        "status cannot be determined; expected a JSON object or the "
        "'key: value' text form carrying a status line"
    )


def _unknown_status_word_manifest_message(path: str | None, word: object) -> str:
    where = f" at {path}" if path else ""
    recognised = ", ".join(sorted(MANIFEST_STATUSES))
    return (
        f"refused manifest{where}: field 'status' rejected value {word!r} "
        f"because it is not a recognised manifest status (recognised: "
        f"{recognised}); the manifest structure and all remaining fields "
        "parsed successfully"
    )


def _missing_status_manifest_message(path: str | None, keys: list[str]) -> str:
    where = f" at {path}" if path else ""
    return (
        f"cannot read manifest{where}: the body reads as manifest fields "
        f"({', '.join(keys)}) but carries no status key, so its status cannot "
        "be determined; expected a JSON object or the 'key: value' text form "
        "carrying a status line"
    )


def _declares_derived(fields: dict[str, Any]) -> bool:
    """Whether the body declares itself a recovery artifact via ``derived``.

    Mirrors recovery's truthy reading so the reader never refuses the artifact
    recovery fabricates to preserve a terminal run whose worker omitted its
    manifest: that body carries no status key, only ``derived`` and whatever
    evidence existed alongside it.
    """
    return str(fields.get("derived") or "").strip().lower() in {"1", "true", "yes"}


def _normalise_manifest_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Apply the typed post-processing shared by both manifest formats."""
    has_retry_failures = "retry_failures" in fields
    for name in _MANIFEST_LIST_KEYS:
        value = fields.get(name)
        fields[name] = (
            _coerce_commit_identifiers(value)
            if name == "commits"
            else _coerce_list_field(value)
        )
    for name in ("baseline_suite", "after_suite"):
        fields[name] = _typed_suite_observation(fields.get(name))
    fields["failure_attribution"] = _typed_failure_attribution(
        fields.get("failure_attribution")
    )
    retry_failures = fields["retry_failures"]
    fields["retry_counted_failures"] = (
        counted_retry_failures(retry_failures, fields["failure_attribution"])
        if has_retry_failures and isinstance(retry_failures, list)
        else None
    )
    return fields


def _coerce_list_field(value: Any) -> Any:
    """Type a list-carrying field, keeping a structured value intact.

    A JSON manifest may carry a dict where the text form carries a comma- or
    newline-separated list (structured ``evidence_inputs`` is the real case);
    splitting a dict's repr would mangle it, so only a list or a string is
    split.
    """
    if value is None or isinstance(value, (list, str)):
        return _as_list(value)
    return value


def _coerce_commit_identifiers(value: Any) -> Any:
    """Type commit identifiers without interpreting their spelling as numbers."""
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("[") and text.endswith("]"):
            return _split_bracketed_list(text)
    return _coerce_list_field(value)


def _typed_suite_observation(value: Any) -> SuiteObservation | str | None:
    """Type a suite observation whether it arrived as text or a JSON object."""
    if isinstance(value, dict):
        return _validate_suite_observation(value)
    return _parse_suite_observation(value)


def _parse_suite_observation(value: Any) -> SuiteObservation | str | None:
    """Decode an inline JSON observation while preserving malformed evidence."""
    if value is None or str(value).strip().lower() in _NONE_VALUES:
        return None
    try:
        raw = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return str(value)
    if not isinstance(raw, dict):
        return str(value)
    return _validate_suite_observation(raw)


def _validate_suite_observation(raw: dict[str, Any]) -> SuiteObservation:
    """Type the fields of an already-decoded suite observation."""
    exit_status = raw.get("exit_status")
    if isinstance(exit_status, bool) or not isinstance(exit_status, int):
        exit_status = None
    completed = raw.get("completed")
    if not isinstance(completed, bool):
        completed = None
    failure_count = raw.get("failure_count")
    if (
        isinstance(failure_count, bool)
        or not isinstance(failure_count, int)
        or failure_count < 0
    ):
        failure_count = None
    failure_ids = raw.get("failure_ids")
    if not isinstance(failure_ids, list) or any(
        not isinstance(item, str) or not item.strip() for item in failure_ids
    ):
        failure_ids = None

    def string_field(name: str) -> str:
        candidate = raw.get(name)
        return candidate.strip() if isinstance(candidate, str) else ""

    return {
        "revision": string_field("revision"),
        "command": string_field("command"),
        "exit_status": exit_status,
        "log_path": string_field("log_path"),
        "log_digest": string_field("log_digest"),
        "completed": completed,
        "failure_count": failure_count,
        "failure_ids": failure_ids,
    }


# ── A suite record keyed by arm is refused where its author can fix it ───────
# The manifest template defines one flat record per suite arm, carrying
# ``revision``, ``command``, ``exit_status``, ``log_path``, ``completed`` and
# ``failure_ids`` at its top level. A worker whose gate has several arms
# naturally records an object keyed by arm instead, and validation folds that
# object into a canonical observation whose every field is empty: the arm then
# reads as one that declares no completion, and the refusal arrives at
# promotion, after the worker's process has ended and only a coordinator can
# restructure the record. The shape is judged here so its author sees the
# finding while the fix is still theirs.


def _is_flat_suite_record(observation: Mapping[str, Any]) -> bool:
    """Whether a parsed observation carries a flat record's required fields.

    Presence is asked of the canonical observation every parsed suite field
    arrives in, because validation keeps what it could type and empties the
    rest: a field the record does not genuinely carry is an empty string or
    ``None``. ``log_digest`` stands in for ``log_path`` exactly as the manifest
    template and the field-level checks allow, so a record citing only a digest
    is flat.
    """
    if not str(observation.get("revision") or "").strip():
        return False
    if not str(observation.get("command") or "").strip():
        return False
    if observation.get("exit_status") is None:
        return False
    if not (
        str(observation.get("log_path") or "").strip()
        or str(observation.get("log_digest") or "").strip()
    ):
        return False
    if observation.get("completed") is None:
        return False
    return observation.get("failure_ids") is not None


def _flat_suite_record_finding(name: str, observation: Mapping[str, Any]) -> str | None:
    """Refuse a suite observation that is not one flat record.

    A record keyed by arm or by measurement carries none of the flat record's
    required fields at its top level, and the finding names the field and the
    keys a flat record needs so its author can restructure it before
    delivering. A record carrying them is left to the field-level checks, which
    judge the values themselves.
    """
    if _is_flat_suite_record(observation):
        return None
    return (
        f"{name} is not a flat suite record: a flat record carries revision, "
        "command, exit_status, log_path (or log_digest), completed and "
        "failure_ids at its top level"
    )


def _typed_failure_attribution(value: Any) -> dict[str, str] | str | None:
    """Type a failure attribution whether it arrived as text or a JSON object."""
    if isinstance(value, dict):
        return _validate_failure_attribution(value)
    return _parse_failure_attribution(value)


def _parse_failure_attribution(value: Any) -> dict[str, str] | str | None:
    """Decode an inline JSON failure-id -> candidate-commit map.

    Tolerant like :func:`_parse_suite_observation`: malformed evidence is kept
    as a string rather than silently dropped, and an entry with a non-string
    key or value is skipped rather than rejecting the whole manifest.
    """
    if value is None or str(value).strip().lower() in _NONE_VALUES:
        return None
    try:
        raw = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return str(value)
    if not isinstance(raw, dict):
        return str(value)
    return _validate_failure_attribution(raw)


def _validate_failure_attribution(raw: dict[str, Any]) -> dict[str, str]:
    """Type the entries of an already-decoded failure attribution map."""
    attribution: dict[str, str] = {}
    for failure_id, commit in raw.items():
        if not isinstance(failure_id, str) or not failure_id.strip():
            continue
        if not isinstance(commit, str) or not commit.strip():
            continue
        attribution[failure_id.strip()] = commit.strip()
    return attribution


def counted_retry_failures(
    failure_ids: Iterable[str], attribution: Mapping[str, str] | str | None
) -> int:
    """Count failures unless an explicit scaffolding cause exempts them."""
    marks = attribution if isinstance(attribution, Mapping) else {}
    return sum(
        not (
            isinstance(mark := marks.get(failure_id), str)
            and mark.startswith("scaffolding: ")
            and mark.removeprefix("scaffolding: ").strip()
        )
        for failure_id in failure_ids
    )


def _as_list(value: Any) -> list[str]:
    """Split a manifest field into items, treating explicit nothing as empty."""
    if value is None:
        return []
    if isinstance(value, list):
        items = [str(item).strip() for item in value]
    else:
        bracketed = _leading_bracketed_list(str(value).strip())
        if bracketed is not None:
            # A bracketed list decodes to its elements; splitting a bracketed
            # value on commas leaves the bracket and quote characters in the
            # items, which then travel verbatim into rendered commands. Prose
            # beside the list annotates it, so it is not an item of it.
            items = _decode_bracketed_list(bracketed)
        else:
            items = [part.strip() for part in re.split(r"[,\n]", str(value))]
    return [item for item in items if item and item.lower() not in _NONE_VALUES]


def _leading_bracketed_list(text: str) -> str | None:
    """Return a value's leading bracketed list, or ``None`` when it has none.

    A writer annotates a list in place — ``changed_paths: [] (a review writes no
    repository path)`` — and the annotation explains the value rather than
    being an element of it. Read whole, the explanation becomes an item, and in
    a path field that item is a changed path the writer never named. The
    closing bracket is found outside any quoted element, so a bracket or quote
    inside one does not end the list.
    """
    if not text.startswith("["):
        return None
    quote = ""
    depth = 0
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return text[: index + 1]
    return None


def _decode_bracketed_list(text: str) -> list[str]:
    """Decode a bracketed list value into clean, unquoted elements."""
    try:
        raw = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        raw = None
    if isinstance(raw, list):
        return [str(item).strip() for item in raw]
    return _split_bracketed_list(text)


def _split_bracketed_list(text: str) -> list[str]:
    """Split bracketed text without interpreting an item's scalar type."""
    return [part.strip().strip("\"'") for part in text[1:-1].split(",") if part.strip()]


def parse_needs_help(text: str) -> dict[str, Any]:
    """Parse an escape-hatch report, naming any of the four fields missing.

    A vague "I'm stuck" wastes as much time as thrashing, so the four fields are
    required: together they turn a plea into a decision brief the orchestrator
    can answer in one turn.
    """
    lines = text.splitlines()
    headline = ""
    for line in lines:
        if NEEDS_HELP_MARKER in line:
            headline = line.split(NEEDS_HELP_MARKER, 1)[1].strip()
            break
    fields: dict[str, str] = {}
    current: str | None = None
    for line in lines:
        stripped = line.strip()
        match = re.match(
            r"^(tried|options|leaning|cost-if-wrong)\s*:\s*(.*)$",
            stripped,
            re.IGNORECASE,
        )
        if match:
            current = match.group(1).lower()
            fields[current] = match.group(2).strip()
        elif current and stripped:
            fields[current] = f"{fields[current]} {stripped}".strip()
    missing = [name for name in NEEDS_HELP_FIELDS if not fields.get(name)]
    return {
        "headline": headline,
        "fields": {name: fields.get(name, "") for name in NEEDS_HELP_FIELDS},
        "missing": missing,
        "complete": not missing and bool(headline),
    }


def _role_owes_a_commit(node: TaskNode | None) -> bool:
    """Whether the manifest's role has repository work a commit would record.

    A review delivers the record it stores beside the run it read and an
    investigation delivers findings, so neither has a commit to cite and a
    manifest that says so is complete. Asking one of them refuses the manifest
    the run's own delivery satisfies. The role set is promotion's, imported
    rather than restated so the check a worker runs and the gate promotion
    applies hours later cannot disagree about which roles promote with no
    commits; an unknown role keeps the finding, so a manifest the audit cannot
    attribute a role to is judged as a working run.
    """
    if node is None:
        return True
    from reckon.crew.promotion import _COMMITLESS_ROLES

    return str(node.role or "").strip() not in _COMMITLESS_ROLES


# ── A control log is judged where its writer can still repair it ─────────────

# The manifest key a worker writes when the log its declaration is discharged
# by cannot be named as a path at the write. The promotion gate judges the log
# itself, so a recorded reason keeps a genuine control from costing a
# corrective node, and the reason stays on the delivered record for whoever
# reads it next rather than being silent.
NEGATIVE_CONTROL_WAIVER_FIELD = "negative_control_waiver"

# The capture convention writes a command's own status as a final ``EXIT=<n>``
# line, so only an end-of-log record is read: a status token inside the
# runner's own output is not the evidence the convention names.
_EXIT_RECORD = re.compile(r"EXIT=(-?\d+)")


def _terminal_exit_status(log_text: str) -> int | None:
    """The status a capture wrote as its final ``EXIT=<n>`` line, or ``None``."""
    for raw_line in reversed(log_text.splitlines()):
        line = raw_line.strip()
        if not line:
            continue
        match = _EXIT_RECORD.fullmatch(line)
        return int(match.group(1)) if match else None
    return None


def _read_control_log(
    delivered: str, *, manifest_path: Path | None
) -> tuple[str | None, Path]:
    """Read a named control log, resolving a relative path beside its manifest.

    A worker commonly writes the path relative to the manifest it delivered, so
    the manifest's own directory is what a relative value resolves against.
    Resolving against the calling process's working directory instead would
    judge one manifest two ways depending on where the check was invoked.
    """
    path = Path(delivered).expanduser()
    if not path.is_absolute() and manifest_path is not None:
        path = manifest_path.expanduser().parent / path
    try:
        return path.read_text(encoding="utf-8"), path
    except (OSError, UnicodeError):
        return None, path


# The arms whose gate log the audit can compare against a revision the manifest
# itself records. The control log is judged by its exit status instead: no
# manifest field records the revision that mutation was applied at, so a first
# line has nothing to be compared against there.
_GATE_LOG_ARMS = (("baseline_suite", "base"), ("after_suite", "head"))

# A gate log names the revision it ran at behind one of the labels the fleet's
# capture scripts write — ``rev=<sha>``, ``revision: <sha>``, ``# arm=base
# revision=<sha> tree=...``. The label is required rather than any hexadecimal
# run: a first line naming only a tree or a working directory carries digits
# that a bare hex scan would read as a revision, and reading a timestamp as the
# revision is how this check would pass a log that names none.
_GATE_LOG_REVISION = re.compile(
    r"\brev(?:ision)?\b[^0-9A-Za-z]{0,8}([0-9a-fA-F]{7,64})"
)

# A revision the audit can hold a first line against: a hexadecimal object id
# as the manifest records it. A symbolic value is left alone rather than
# compared, because no abbreviation of ``<output of git rev-parse HEAD>`` or of
# a branch name can be told from a log naming nothing.
_RECORDED_REVISION = re.compile(r"[0-9a-fA-F]{7,64}")


def _names_the_same_revision(left: str, right: str) -> bool:
    """Whether two revision spellings name one commit, abbreviation included."""
    first, second = left.lower(), right.lower()
    return first.startswith(second) or second.startswith(first)


def _gate_log_revision_findings(
    manifest: dict[str, Any],
    *,
    manifest_path: Path | None,
) -> list[str]:
    """Judge each arm's gate log against the revision that arm records.

    The manifest template requires a gate log's first line to name the revision
    it ran at, the tree and the command, and nothing checked it: a log called
    for one arm could carry another arm's run — measured as a log named for the
    new file alone carrying the whole population — and a claim about one arm
    then rested on a different log than the one cited. Of the three facts the
    template names, the revision is the one the audit can hold against a
    record, because each suite arm carries the revision its own run measured on.
    The finding states the full requirement; the comparison is the revision.

    A log that cannot be read is left to the readers that judge citations and
    temporary paths, since whether a log resolves is a different question from
    what its first line says. A manifest that records a symbolic revision is
    not compared, and one that cites no log for an arm is not judged. A
    relative path resolves beside the manifest, the same way the control log's
    does, so one manifest is not judged two ways by where the check is called.
    """
    findings: list[str] = []
    for key, arm in _GATE_LOG_ARMS:
        observation = manifest.get(key)
        if not isinstance(observation, dict):
            continue
        recorded = str(observation.get("revision") or "").strip()
        cited = str(observation.get("log_path") or "").strip()
        if not recorded or not cited:
            continue
        if not _RECORDED_REVISION.fullmatch(recorded):
            continue
        text, resolved = _read_control_log(cited, manifest_path=manifest_path)
        if text is None:
            continue
        first_line = next(
            (line.strip() for line in text.splitlines() if line.strip()), ""
        )
        named = [match.group(1) for match in _GATE_LOG_REVISION.finditer(first_line)]
        if not named:
            findings.append(
                f"{key} gate log {str(resolved)!r} names no revision on its "
                "first line: the manifest template requires a gate log's first "
                "line to name the revision it ran at, the tree and the command, "
                "and this log's does not"
            )
            continue
        if not any(_names_the_same_revision(token, recorded) for token in named):
            findings.append(
                f"{key} gate log {str(resolved)!r} names {named[0]} on its first "
                f"line, but the {arm} arm of this manifest records {recorded}: "
                "the log named for one arm carries another arm's run"
            )
    return findings


# ── A finding with no declared severity cannot be acted on ───────────────────
# A review run's whole deliverable is the record it stores, and a finding in
# that record says whether it blocks the reviewed node's landing by opening
# with one of the declared severities. A finding that states none is recorded
# without the key rather than defaulted, which keeps a judgement nobody made
# out of the record — and leaves the gate that reads the record unable to tell
# a blocking defect from a follow-on. Judging the record at the write, while
# the reviewer still holds its turn, is what lets the reviewer restate the
# severities instead of costing a corrective node hours later. The record is
# read through the review store's own reader so this check and every other
# consumer see one parse of one file.

_HEAD_KEYED_RECORD_NAME = re.compile(r"\.at-[0-9A-Fa-f]{7,64}\Z")


def _record_name_reviewed_run(name: str) -> str:
    """The reviewed run a store record's name keys, or ``""`` when it names none.

    The store spells a record's name two ways — ``<reviewed run id>.json`` and
    the head-keyed ``<reviewed run id>.at-<head>.json`` — so the reviewed run
    is the stem before either suffix, and a declared path whose name is neither
    is not a record this check reads.
    """
    stem = Path(name).stem
    match = _HEAD_KEYED_RECORD_NAME.search(stem)
    return stem[: match.start()] if match else stem


def _node_is_a_reviewer(node: TaskNode) -> bool:
    """Whether a node's role is the review role recovery mints for a review."""
    from reckon.crew.recovery import REVIEW_ROLE

    return str(node.role or "").strip() == REVIEW_ROLE


def _severity_stated(finding: dict[str, Any]) -> bool:
    """Whether a stored finding carries one of the declared severities.

    The store records a severity in its declared spelling, and only then: a
    finding that states none has no key here. A value outside the vocabulary —
    is not a severity any gate can read, so it is judged as absent rather than
    as a declaration. The vocabulary decision is :func:`declared_severity`'s,
    so this audit cannot disagree with the repair reflex about a finding.
    """
    return review_module.declared_severity(finding) is not None


def _reviewer_store_records(
    node: TaskNode | None,
) -> list[tuple[Path, str, dict[str, Any]]]:
    """The stored records a reviewer's own declaration grants, once each.

    The dispatch grants the review store's record paths on the node, so the
    records to judge are read from those declarations: the project is the
    directory beneath the store root and the reviewed run is the id the
    record's own name keys. The legacy and head-keyed declarations name one
    run, and the reader selects one record for it, so the record is returned
    once however many spellings the dispatch granted. A declared record path
    that is not a record this store would read, or that cannot be read yet, is
    not a finding of its own — a review that has not stored its record is
    judged by the gates that require one, and the checks built on this only
    report what a readable record says.

    A node that is not a review is not judged: its deliverable is repository
    work, and a path that happens to sit under the store root belongs to
    another run's delivery.
    """
    if node is None or not _node_is_a_reviewer(node):
        return []
    store_root = review_module.review_store_root()
    records: list[tuple[Path, str, dict[str, Any]]] = []
    read: set[tuple[str, str]] = set()
    for declared in node.write_paths or ():
        candidate = Path(str(declared)).expanduser()
        if candidate.parent.parent != store_root:
            continue
        reviewed_run_id = _record_name_reviewed_run(candidate.name)
        if not reviewed_run_id:
            continue
        key = (candidate.parent.name, reviewed_run_id)
        if key in read:
            continue
        read.add(key)
        try:
            stored_at, record = review_module.stored_record(*key)
        except (OSError, ValueError):
            # A store file that will not read is left to the readers that
            # judge the store itself; this check reports what a readable
            # record says, never a guess about an unreadable one.
            continue
        if record is None:
            continue
        records.append(
            (stored_at or candidate, reviewed_run_id, ledger.normalize_identity(record))
        )
    return records


def _unmarked_finding_severity_findings(node: TaskNode | None) -> list[str]:
    """Report a review run's stored findings that declare no severity.

    A finding that declares no severity cannot be told from a follow-on by the
    gate that reads the stored record, so the reviewer's judgement is missing
    rather than negative, and the reviewer is the only party that can restate
    it. Each unmarked finding is reported on its own, naming the finding's file
    and line, so the reviewer can restate the severities in the same turn;
    nothing is rewritten here, because the fix belongs in the record's next
    write by the party that authored it.
    """
    findings: list[str] = []
    for record_path, _run_id, record in _reviewer_store_records(node):
        findings.extend(_unmarked_findings_in_record(record, record_path))
    return findings


def _unresolved_revision_findings(node: TaskNode | None) -> list[str]:
    """Report a review run's stored revisions that name no commit.

    A record's base and head are typed by hand, and a sha that lost characters
    mid-value still looks like one, so the pair is resolved against the
    reviewed run's own repository before the review run may complete. The
    refusal names the stored value beside the head the reviewed worktree
    actually carries, so the reviewer corrects its own record in the same turn
    — the alternative is a clean promotion here and a reviewed run that finds
    no review of its revision hours later, which buys a whole second review.
    """
    findings: list[str] = []
    for record_path, _run_id, record in _reviewer_store_records(node):
        refusal = review_module.unresolved_reviewed_revision(record)
        if refusal:
            findings.append(f"review record {str(record_path)!r} {refusal}")
    return findings


def _unmarked_findings_in_record(
    record: dict[str, Any], record_path: Path
) -> list[str]:
    """The audit findings for each stored finding that declares no severity.

    The findings are the record's own when it carries them, and otherwise the
    ones its verbatim emitted text parses to, so a record written through the
    store and one carrying only the reviewer's emission yield one list. A
    malformed entry is skipped rather than given an invented location: a
    finding that names no file and no line cannot be restated by the reviewer,
    and the store's own readers drop it on the same rule.
    """
    entries = record.get("findings")
    if not isinstance(entries, list):
        raw_text = str(record.get("raw_text") or "")
        entries = (
            review_module.parse_review(raw_text).get("findings", []) if raw_text else []
        )
    declared = ", ".join(review_module.FINDING_SEVERITIES)
    findings: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict) or _severity_stated(entry):
            continue
        file = str(entry.get("file") or "").strip()
        line = str(entry.get("line") or "").strip()
        if not file or not line:
            continue
        findings.append(
            f"review record {str(record_path)!r} carries finding {file}:{line} "
            f"with no declared severity: a finding says whether it blocks by "
            f"opening with one of {declared}, so an unstated severity leaves "
            "the gate that reads this record unable to tell a blocking defect "
            "from a follow-on — restate the severity on the finding and store "
            "the record again"
        )
    return findings


def _control_log_findings(
    manifest: dict[str, Any],
    node: TaskNode | None,
    *,
    manifest_path: Path | None,
) -> list[str]:
    """Judge the control log a node's declaration is discharged by, at the write.

    A node whose write paths include a test file declares the mutation that
    check must fail against, and its manifest discharges the declaration by
    naming the log that mutation produced. That field is judged here, while its
    writer still holds a turn, rather than only hours later when a malformed
    value costs a corrective node instead of an edit. The value is required to
    be the bare path to a readable file whose terminal record is a non-zero
    ``EXIT=<code>``: a field that is empty, carries anything besides a path, or
    names a log that does not resolve, records no exit status, or exited zero
    states too little to show the mutation ever failed.

    A node that declared no control, and one whose declaration is ``none`` with
    its reason, have nothing to discharge and are not judged here. A manifest
    that records a waiver reason is left for a person to judge rather than
    refused, because the honesty of that reason is not a fact this reader can
    establish. The log's wording is deliberately not compared against the
    declaration: a declaration pasted into a log satisfies that comparison
    while proving nothing, so the match stays a human judgement rather than a
    test that manufactures agreement.
    """
    if node is None:
        return []
    test_paths = sorted(
        str(path) for path in node.write_paths or () if is_test_path(str(path))
    )
    if not test_paths:
        return []
    declaration = str(node.negative_control or "").strip()
    if not declaration or negative_control_is_none(declaration):
        return []
    if str(manifest.get("status", "")).strip().lower() != "complete":
        return []
    if str(manifest.get(NEGATIVE_CONTROL_WAIVER_FIELD) or "").strip():
        return []
    value = manifest.get("negative_control_log")
    if value is not None and not isinstance(value, str):
        shorthand = (
            "negative_control_log must be the bare path to the log the node's "
            f"mutation produced; it carries a {type(value).__name__} "
            f"({str(value)!r})"
        )
        return [shorthand]
    delivered = str(value or "").strip()
    if not delivered:
        empty = (
            "negative_control_log is empty: the node writes a check "
            f"({', '.join(test_paths)}) and declares the mutation "
            f"{declaration!r}, but no log path is recorded — name the log that "
            "mutation produced, or record why it cannot be named on a "
            f"{NEGATIVE_CONTROL_WAIVER_FIELD}: line"
        )
        return [empty]
    text, resolved = _read_control_log(delivered, manifest_path=manifest_path)
    if text is None:
        unresolved = (
            f"negative_control_log {delivered!r} does not resolve to a readable "
            "file, so the mutation it was to evidence was never shown to fail"
        )
        return [unresolved]
    status = _terminal_exit_status(text)
    if status is None:
        unrecorded = (
            f"negative_control_log {str(resolved)!r} records no exit status: its "
            "last non-blank line is not a bare EXIT=<code> line, so whether its "
            "run failed is unknown"
        )
        return [unrecorded]
    if status == 0:
        passed = (
            f"negative_control_log {str(resolved)!r} records EXIT=0, so the "
            "control did not fail and shows nothing about the mutation it was "
            "to evidence"
        )
        return [passed]
    return []


# ── A citation that names no object is not evidence ──────────────────────────
# The ``commits`` field is a citation list: the revisions the run created, and
# the pointer a coordinator follows to the work. A citation that resolves to no
# object is worse than an unpopulated field, because it reads as evidence and
# points at nothing — and a value assembled rather than copied passes every
# shape check while naming nothing, so the store is asked about the value as
# cited and never about its form. This is the same reading promotion applies,
# taken at the write. Measured on a delivered manifest: a cited object id
# differed from the real commit in a single nibble and reached the coordinator,
# because nothing between the worker's own check and the promotion read the
# store.

# Punctuation a citation can be wrapped in by the prose or list syntax around
# it, stripped before the token is asked about.
_CITATION_EDGE_PUNCTUATION = "[](){}<>'\",;.:…*`"


def _opens_with_an_absence_word(value: str) -> bool:
    """Whether a text value opens with an absence word standing alone.

    The word must end at the value's edge or at any character that is not a
    letter, digit or underscore, so ``none — the scope is outside the
    repository`` is the declaration it is while ``nonesuch`` is a longer word
    and is read as the citation attempt it looks like. The vocabulary is this
    module's own statement of what an explicit nothing looks like in a
    manifest field.
    """
    stripped = value.strip()
    return any(
        re.match(rf"{re.escape(word)}(?!\w)", stripped, re.IGNORECASE)
        for word in _NONE_VALUES
        if word
    )


def _citation_tokens(entry: str) -> list[str]:
    """The commit-id shaped tokens one citation entry carries, in order.

    An entry is the worker's own line — a single revision, several revisions
    separated by spaces or commas, or a revision followed by the subject it was
    committed under — so the ids are read as the tokens they are rather than
    the entry being resolved whole, which would refuse an honest entry holding
    more than one. A token that is not hexadecimal object-id shaped is left
    alone; the shape only decides what is worth asking about, the store still
    decides what resolves.
    """
    tokens: list[str] = []
    for raw in entry.split():
        token = raw.strip(_CITATION_EDGE_PUNCTUATION)
        if token and _RECORDED_REVISION.fullmatch(token) and token not in tokens:
            tokens.append(token)
    return tokens


def _commit_resolves_in(root: Path, revision: str) -> bool:
    """Report whether one revision names a commit object in one repository."""
    from reckon.crew.recovery import _resolve_commit

    return bool(_resolve_commit(root, revision))


def _citation_stores(
    *, worktree: Path | None, repository: Path | None
) -> tuple[Path, ...]:
    """The stores a citation may resolve in, each one shown able to answer.

    The run records the worktree it worked in and the repository that worktree
    belongs to, and either shares the object store the citations name, so a
    citation resolves when any recorded tree can find it. A candidate is only
    used once git reports a repository there: a pointer into a tree that is not
    a repository leaves the check unarmed, because asking a store that cannot
    answer would report every citation as unresolvable and measure the absence
    of a store rather than of a commit.
    """
    stores: list[Path] = []
    for candidate in (repository, worktree):
        if candidate is None:
            continue
        root = Path(candidate)
        if not root.is_dir() or root in stores:
            continue
        probe = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--quiet", "--git-dir"],
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.returncode == 0:
            stores.append(root)
    return tuple(stores)


def _commit_citation_findings(
    manifest: dict[str, Any],
    *,
    worktree: Path | None = None,
    repository: Path | None = None,
) -> list[str]:
    """Report each ``commits`` citation none of the run's stores can resolve.

    The field opens with a declaration when it opens with an absence word — a
    report-only node writes ``none — the store is outside the repository`` —
    and that is a statement about the run rather than a citation, so the whole
    field is left alone. Every other entry is read token by token: each token
    the stores cannot resolve is reported by value, and an entry carrying no
    commit-id shaped token is left to the readers that judge prose.
    """
    entries = [str(entry).strip() for entry in (manifest.get("commits") or ())]
    entries = [entry for entry in entries if entry]
    if not entries or _opens_with_an_absence_word(entries[0]):
        return []
    stores = _citation_stores(worktree=worktree, repository=repository)
    if not stores:
        return []
    findings: list[str] = []
    for entry in entries:
        unresolved = [
            token
            for token in _citation_tokens(entry)
            if not any(_commit_resolves_in(store, token) for store in stores)
        ]
        if not unresolved:
            continue
        named = ", ".join(repr(token) for token in unresolved)
        verb = "does not resolve" if len(unresolved) == 1 else "do not resolve"
        named_stores = ", ".join(str(store) for store in stores)
        findings.append(
            f"commits entry {entry!r} cites {named}, which {verb} to a commit "
            f"object in the run repository ({named_stores})"
        )
    return findings


def _wait_declaration_refusal(
    manifest: dict[str, Any], manifest_path: Path | None
) -> str:
    """The reason a waiting manifest's declaration is refused, or "".

    The declaration is read by the fleet's own wait reader rather than by a
    second copy of the grammar, so the audit cannot disagree with the
    classifier and the worker stop hook about what a parked worker declared.
    A declaration the reader honours keeps its state: the audit's other
    findings for the fleet's own manifest fields are reported beside it as
    attachments. A declaration the reader refuses is refused here too, with the
    reader's own error text, so a malformed wait block is never read as an
    accepted state.
    """
    from reckon.crew.recovery import _manifest_wait

    try:
        wait = _manifest_wait(
            manifest,
            manifest_path or Path("manifest.md"),
            now_seconds=time.time(),
            stale_after_seconds=0,
        )
    except Exception as exc:  # noqa: BLE001 - an unreadable wait refuses, never crashes
        return f"the wait declaration could not be validated: {exc}"
    if wait is None:
        return (
            "status 'waiting' holds no wait declaration the fleet can act on: "
            "wait_condition, wait_probe (or wait_file), wait_terminal and "
            "resume_brief. A waiting manifest must declare a condition that "
            "can end"
        )
    if not wait["valid"]:
        return (
            f"status 'waiting' but the wait declaration is incomplete: {wait['error']}"
        )
    return ""


# The expiry verbs a blocker uses when it claims the attempt's clock has run
# out. Narrow on purpose: a blocker that merely names a fence — the write
# fence, the evidence fence — is not a claim about the time fence, so the
# phrase must carry the expiry beside the noun.
_FENCE_EXPIRY = re.compile(
    r"(?i)\b(?:time[ -]fence|fence|deadline)\b[^\n]{0,80}?"
    r"\b(?:expired|expires|spent|exhausted|reached|passed|elapsed|exceeded|out of time)\b"
)

# The fence statement the dispatch prompt writes verbatim, as a manifest
# records it when the worker quotes its own fence back instead of only naming
# it. Both the launch instant and the deadline are ISO-8601 UTC.
_FENCE_STATEMENT = re.compile(
    r"Launched ([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z) UTC; "
    r"deadline ([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z)"
)


def _instant(text: str) -> float | None:
    """The POSIX instant an ISO-8601 ``Z`` stamp names, or None."""
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _utc_stamp(seconds: float) -> str:
    """Render a POSIX instant the way attempt records and fences write it."""
    return (
        datetime.fromtimestamp(seconds, tz=UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _recorded_manifest_instant(manifest_path: Path | None, fallback: float) -> float:
    """When the manifest was recorded, from its own stat identity, or ``fallback``.

    The manifest's last write is the moment the blocker was delivered, which is
    what a fence claim is judged against; a manifest the reader cannot stat
    falls back to the moment of the audit rather than silently exempting the
    claim.
    """
    if manifest_path is None:
        return fallback
    try:
        return manifest_path.stat().st_mtime
    except OSError:
        return fallback


def _attempt_deadline_seconds(
    text: str, node: TaskNode | None, manifest_path: Path | None
) -> float | None:
    """The attempt's recorded deadline, or None when nothing records one.

    Two records are read, in order. The fence statement a manifest quotes back
    names its deadline directly. Failing that, the attempt record a supervisor
    writes beside the manifest names the attempt's launch instant, and the
    node's declared budget turns that into the deadline — the same arithmetic
    the prompt's fence states, from the same record.
    """
    recorded = _FENCE_STATEMENT.search(text)
    if recorded is not None:
        return _instant(recorded.group(2))
    budget = str(getattr(node, "time_budget", "") or "")
    if not budget or manifest_path is None:
        return None
    try:
        marker = json.loads(
            (manifest_path.parent / "attempt.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    if not isinstance(marker, Mapping):
        return None
    started = _instant(str(marker.get("attempt_started_at") or ""))
    if started is None:
        return None
    try:
        return started + parse_duration(budget)
    except CrewError:
        return None


def _time_fence_claim_findings(
    text: str,
    manifest: Mapping[str, Any],
    node: TaskNode | None,
    manifest_path: Path | None,
    *,
    now: float | None = None,
) -> list[str]:
    """A blocked manifest whose blocker blames a fence that had not yet expired.

    A worker that writes ``status: blocked`` citing the time fence ends its run
    on a claim about the clock, and a coordinator that takes the claim at its
    word redispatch or abandons work the worker was entitled to keep going
    with. The claim is checked against the attempt's own record: when the
    manifest was recorded before the deadline the attempt ran under, the fence
    had not expired and the blocker misstates its cause — a defect, reported
    rather than accepted as a blocker. A manifest that records no deadline, or
    one whose blocker makes no fence claim, is left alone: an unknown clock is
    not a defect.
    """
    status = str(manifest.get("status", "")).lower()
    if status not in ("blocked", "failed"):
        return []
    claim = "\n".join(str(entry) for entry in manifest.get("blockers") or ())
    if not _FENCE_EXPIRY.search(claim):
        return []
    deadline = _attempt_deadline_seconds(text, node, manifest_path)
    if deadline is None:
        return []
    recorded = _recorded_manifest_instant(
        manifest_path, now if now is not None else time.time()
    )
    if recorded >= deadline:
        return []
    return [
        (
            "the blocker cites an expired time fence, but the attempt's recorded "
            f"deadline {_utc_stamp(deadline)} had not passed when the manifest was "
            f"recorded at {_utc_stamp(recorded)}: a defect, not a blocker"
        )
    ]


# The subtree a plan's landing fragments live under. A fragment is one HTML
# document directly beneath it, and the composed record audits are keyed on the
# plan the fragment's own directory names.
_FRAGMENT_SUBTREE_PARTS = ("docs", "evidence", "fragments")


def _fragment_plan_of(changed: Any) -> str | None:
    """Return the plan whose fragment directory a changed path writes into.

    The match is on the path's own segments rather than a string prefix, so an
    absolute declaration and a repository-relative spelling of the same
    fragment both resolve. A path under ``docs/evidence/fragments`` that is not
    a fragment document answers ``None`` — a deeper path is not one of the
    documents the composition reads.
    """

    parts = _normalized_scope_path(changed).parts
    for index in range(len(parts) - 4):
        if tuple(parts[index : index + 3]) != _FRAGMENT_SUBTREE_PARTS:
            continue
        if len(parts) - index == 5 and parts[index + 4].endswith(".html"):
            return parts[index + 3]
        return None
    return None


def _landing_record_beside(root: Path, plan: str) -> Path | None:
    """The landing record a plan's fragments compose into, when one exists.

    The archived spelling is canonical and the live one is accepted because
    fragments live beside either. ``None`` means the plan has no landed record
    yet, which is the state of nearly every plan: the record is synthesised
    once, at closure, and the caller audits the fragments on their own until
    then.
    """

    evidence = root / "docs" / "evidence"
    for candidate in (
        evidence / "archive" / f"{plan}-landed.html",
        evidence / f"{plan}-landed.html",
    ):
        if candidate.is_file():
            return candidate
    return None


def _composed_record_id_findings(
    changed_paths: Iterable[Any],
    *,
    worktree: Path | None,
    repository: Path | None,
) -> list[str]:
    """Report duplicate element ids the run's own changed fragments form.

    A collision is caught where it is written: composing a plan's record
    concatenates its fragments into one document, so two fragments that reuse
    an ``id`` leave every later anchor unreachable, and nothing else on the
    crew path reads that composition. The record is composed from the run's own
    worktree, where the changed fragment and its siblings both sit, and each
    duplicate is reported in the wording ``audit-doc`` gives, naming the
    fragments that carry it.

    A run that changes no fragment is not audited: a composed record the run
    did not touch is not its defect, and refusing it would make a collision
    already merged into every later run's way. A duplicate is reported only
    where one of its occurrences lies in a fragment the run changed, on the same
    reasoning: a collision carried entirely by the plan's older fragments was
    merged before this run and no run can clear it from inside its own write
    scope. A plan whose landing record does not exist yet is audited over its
    fragments instead, because the record is synthesised once, at closure:
    until then the fragments are the composition, and two of them that reuse an
    id collide with each other.
    """

    changed_fragments: dict[str, set[str]] = {}
    for path in changed_paths:
        plan = _fragment_plan_of(path)
        if plan is not None:
            changed_fragments.setdefault(plan, set()).add(
                _normalized_scope_path(path).name
            )
    if not changed_fragments:
        return []
    root = worktree if worktree is not None else repository
    if root is None:
        return []
    findings: list[str] = []
    tree = Path(root)
    for plan in sorted(changed_fragments):
        record = _landing_record_beside(tree, plan)
        plan_findings = (
            doccheck.audit_composed_record_ids(
                record, plan, changed_fragments=changed_fragments[plan]
            )
            if record is not None
            else doccheck.audit_fragment_ids(
                tree.joinpath(*_FRAGMENT_SUBTREE_PARTS, plan),
                changed_fragments=changed_fragments[plan],
            )
        )
        findings.extend(
            f"[{finding.code}] {finding.message}" for finding in plan_findings
        )
    return findings


def audit_manifest(
    text: str,
    node: TaskNode | None = None,
    *,
    suite_armed: bool = False,
    worktree: Path | None = None,
    repository: Path | None = None,
    manifest_path: Path | None = None,
) -> dict[str, Any]:
    """Judge a delivered manifest: is it complete, and does it stay in scope?"""
    try:
        manifest = parse_manifest(text)
    except ManifestParseError as exc:
        # An unreadable manifest is a finding, not an exception: the audit is
        # itself a reader of the file and must survive a body no reader can
        # judge, reporting the refusal instead of escaping it to the caller.
        return {
            "manifest": {},
            "findings": [f"manifest could not be read: {exc}"],
            "ok": False,
        }
    findings: list[str] = []
    retry_failures = manifest["retry_failures"]
    if not isinstance(retry_failures, list):
        findings.append("retry_failures must be a list of failure ids")
        retry_failures = []
    resolved_manifest_path = manifest_path
    if resolved_manifest_path is None and node is not None and node.manifest_path:
        resolved_manifest_path = Path(node.manifest_path).expanduser()
    status = str(manifest.get("status", "")).lower()
    if status == "waiting":
        # A parked worker's declaration is judged by the fleet's own wait
        # reader — the one the classifier and the worker stop hook share — so
        # the audit cannot demand a status the other two accept. A declaration
        # the reader honours keeps its waiting state, and any unrelated finding
        # (a gate log naming no revision, an out-of-scope path) is reported
        # beside that state as an attachment rather than replacing it. A
        # declaration the reader refuses is refused here too, with the reader's
        # own error, so a malformed wait block never reads as an accepted state.
        refusal = _wait_declaration_refusal(manifest, resolved_manifest_path)
        if refusal:
            findings.append(refusal)
    elif status not in ("complete", "blocked", "failed"):
        findings.append(f"status {status!r} is not complete, blocked or failed")
    if status == "complete" and _role_owes_a_commit(node):
        # One predicate decides citation for both readers, so the field cannot
        # read as a citation at the gate and as no citation here — a count is
        # not a citation. Imported at the use because promotion imports this
        # module.
        from reckon.crew.promotion import _manifest_cites_a_commit

        role = "" if node is None else str(node.role or "").strip()
        if not _manifest_cites_a_commit(manifest, {"role": role}, text):
            findings.append("status is complete but no commit is recorded")
    if status == "complete" and not manifest.get("tests"):
        findings.append("status is complete but no test result is recorded")
    # Judged whatever the status and whether or not the run is armed: a
    # manifest that records its suite arms as an object keyed by arm normalises
    # to an observation whose fields are all empty, and promotion refuses it
    # hours later as an arm that declares no completion — past the point where
    # its author can restructure the record. check-manifest reads this finding
    # while the worker still holds a turn.
    for name in ("baseline_suite", "after_suite"):
        observation = manifest.get(name)
        if isinstance(observation, dict):
            shape = _flat_suite_record_finding(name, observation)
            if shape:
                findings.append(shape)
    if status == "complete" and suite_armed:
        for name in ("baseline_suite", "after_suite"):
            observation = manifest.get(name)
            if observation is None:
                findings.append(f"status is complete but {name} is missing")
                continue
            if not isinstance(observation, dict):
                findings.append(f"{name} must be an inline JSON object")
                continue
            if not _is_flat_suite_record(observation):
                # Already refused above, while the shape was still its
                # author's to change; the field-level checks below describe
                # values a record keyed by arm never carried at its top level.
                continue
            findings.extend(
                f"{name}.{field} is missing"
                for field in ("revision", "command")
                if not observation[field]
            )
            if observation["exit_status"] is None:
                findings.append(f"{name}.exit_status is missing or not an integer")
            if observation["completed"] is not True:
                findings.append(
                    f"{name}.completed is not true; the suite result is absent"
                )
            if observation["failure_count"] is None:
                findings.append(
                    f"{name}.failure_count is missing or not a non-negative integer"
                )
            if observation["failure_ids"] is None:
                findings.append(f"{name}.failure_ids is missing or not a string list")
            elif observation["failure_count"] is not None and observation[
                "failure_count"
            ] != len(observation["failure_ids"]):
                findings.append(f"{name}.failure_count does not match failure_ids")
            if not observation["log_path"] and not observation["log_digest"]:
                findings.append(f"{name} needs log_path or log_digest")
        attribution = manifest.get("failure_attribution")
        if attribution is not None:
            if not isinstance(attribution, dict):
                findings.append("failure_attribution must be an inline JSON object")
            else:
                after_observation = manifest.get("after_suite")
                after_ids = (
                    set(after_observation.get("failure_ids") or ())
                    if isinstance(after_observation, dict)
                    else set()
                )
                overlap = set(retry_failures) & after_ids
                if overlap:
                    findings.append(
                        "retry_failures ids must differ from after_suite.failure_ids"
                    )
                findings.extend(
                    ledger.failure_attribution_missing_fields(
                        {
                            key: value
                            for key, value in attribution.items()
                            if key not in retry_failures
                        },
                        after_observation
                        if isinstance(after_observation, dict)
                        else None,
                    )
                )
    findings.extend(
        _control_log_findings(manifest, node, manifest_path=resolved_manifest_path)
    )
    findings.extend(
        _time_fence_claim_findings(text, manifest, node, resolved_manifest_path)
    )
    findings.extend(
        _gate_log_revision_findings(manifest, manifest_path=resolved_manifest_path)
    )
    findings.extend(
        _commit_citation_findings(manifest, worktree=worktree, repository=repository)
    )
    findings.extend(_unmarked_finding_severity_findings(node))
    findings.extend(_unresolved_revision_findings(node))
    if node is not None and manifest["changed_paths"]:
        declared = tuple(node.write_paths or ())
        stray = sorted(
            path
            for path in manifest["changed_paths"]
            if not path_within_declared_scope(
                path, declared, worktree=worktree, repository=repository
            )
        )
        if stray:
            findings.append(
                "changed paths outside the write scope: " + ", ".join(stray)
            )
    findings.extend(
        _composed_record_id_findings(
            manifest["changed_paths"] or (),
            worktree=worktree,
            repository=repository,
        )
    )
    return {"manifest": manifest, "findings": findings, "ok": not findings}


def _normalized_scope_path(text: Any) -> PurePosixPath:
    """Return a repository-relative path without redundancy but with ``..`` intact."""
    return PurePosixPath(str(text).strip())


# The revision key the review store folds into a record's name: a record
# written where the head the review read is named sits beside the record a
# dispatch granted, as ``<stem>.at-<revision><suffix>``. Seven to 64 hex
# characters is the store's own spelling, so a neighbour that merely resembles
# a head-keyed record is not admitted by the declaration beside it.
_HEAD_KEYED_SUFFIX = re.compile(r"^\.at-[0-9A-Fa-f]{7,64}$")


def _head_keyed_beside(changed: PurePath, declared: PurePath) -> bool:
    """Whether ``changed`` is the head-keyed record written beside ``declared``.

    The two are one deliverable under two names — the record itself and the
    copy keyed by the revision the review read — so a dispatch that grants the
    record grants the copy the store reads back by its head. The names must
    share a directory and a stem, which is what keeps the rule from admitting a
    differently named neighbour.
    """
    if changed.parent != declared.parent or changed.suffix != declared.suffix:
        return False
    stem = declared.name[: -len(declared.suffix)] if declared.suffix else declared.name
    tail = changed.name[: -len(changed.suffix)] if declared.suffix else changed.name
    return tail.startswith(stem) and bool(
        _HEAD_KEYED_SUFFIX.fullmatch(tail[len(stem) :])
    )


def path_within_declared_scope(
    changed: Any,
    declared: Iterable[str],
    *,
    worktree: Path | None = None,
    repository: Path | None = None,
) -> bool:
    """Return whether a changed path is contained by a declared write root.

    Containment is the rule promotion applies when it audits a delivered run,
    and the write-time audit judges a manifest earlier on the same terms.
    Judging by exact membership instead would refuse a file under a directory
    dispatch granted, sending a worker to repair a manifest promotion would
    have accepted — a stricter contract at the earlier surface, which is the
    drift between the two checks this function exists to remove. A changed
    path is in scope when it equals a declared path or lies beneath one, so
    both a file declaration and a directory declaration are honoured.

    A declaration may be written absolutely, naming a directory or file by its
    path on disk. Comparing such a declaration literally would never match the
    repository-relative path a manifest records, so a scope dispatch granted
    absolutely would read as stray here and as in-scope at promotion. Each
    declaration is therefore resolved into a repository-relative root against
    the worktree and the repository before the comparison, which is the same
    mapping promotion applies. The worktree defaults to the working directory,
    which is where the write-time audit runs.

    A declaration naming a location outside the repository resolves to no
    repository-relative root. Dropping it would leave the declaration out of
    the comparison altogether, so the path it granted reads as stray here
    however plainly the dispatch named it — a review's store record is the real
    case, since its deliverable is stored beside the run it read rather than
    inside the repository. Such a declaration is compared as the absolute path
    it already is, by equality or containment, so the declaration still decides
    what it grants.
    """
    tree = Path.cwd() if worktree is None else Path(worktree)
    repo = tree if repository is None else Path(repository)
    declarations = tuple(str(item).strip() for item in declared)
    target = _normalized_scope_path(changed)
    for root in _declared_scope_roots(declarations, worktree=tree, repository=repo):
        if target == root or root in target.parents or _head_keyed_beside(target, root):
            return True
    return _within_an_absolute_declaration(changed, declarations)


def _within_an_absolute_declaration(changed: Any, declared: Iterable[str]) -> bool:
    """Judge a changed path against the declarations written as absolute paths.

    A relative changed path is judged against the repository mapping alone: an
    absolute declaration outside the repository describes a location no
    repository-relative path can name, so no comparison between the two says
    anything. An absolute changed path, by contrast, is the location itself,
    and is in scope when it is the declared path, lies beneath it, or is the
    head-keyed record written beside it.
    """
    candidate = Path(str(changed).strip()).expanduser()
    if not candidate.is_absolute():
        return False
    resolved = candidate.resolve()
    for declaration in declared:
        root = Path(declaration).expanduser()
        if not root.is_absolute():
            continue
        resolved_root = root.resolve()
        if (
            resolved == resolved_root
            or resolved.is_relative_to(resolved_root)
            or _head_keyed_beside(resolved, resolved_root)
        ):
            return True
    return False


def _declared_scope_roots(
    declared: Iterable[str], *, worktree: Path, repository: Path
) -> tuple[PurePosixPath, ...]:
    """Resolve declared write paths into repository-relative roots.

    Delegates to the mapping promotion already applies so both surfaces judge a
    declaration the same way rather than growing a second rule that can drift
    from it. The import is deferred because promotion imports this module.
    """
    from reckon.crew.promotion import _repository_scope_paths

    return tuple(
        PurePosixPath(root.as_posix())
        for root in _repository_scope_paths(
            declared, worktree=worktree, repository=repository
        )
    )


def report_log_paths_under_temp_root(
    manifest: dict[str, Any],
    *,
    name: str,
) -> list[dict[str, str]]:
    """Report the log paths a manifest cites that resolve under the temp root.

    A promoted run record cites a gate command, an exit status and a log
    path, and the log path is what a later reader opens to check the claim.
    The run directory persists with the run; the platform temporary directory
    is a small allocation cleared without notice. A citation resolving there
    can point at a file that no longer exists, and nothing then distinguishes
    it from a citation to a file that was never written. This check reports
    such citations, naming the manifest, the key and the offending path. It
    reports and returns; it never refuses, rewrites, relocates or touches
    anything, because a coordinator promoting a run whose logs are already
    written cannot make them durable, and refusing would strand the record.

    A path is under the root when its resolved form is contained by the
    resolved root, so a relative spelling or a path reaching the root through
    a symbolic link is caught rather than only a literal prefix. The root is
    what the running system reports as the temporary directory, read at call
    time rather than written as a literal. A cited path is judged by where it
    points rather than by whether it exists, because a missing durable path
    and a missing temporary path are different findings.
    """
    root = os.path.realpath(tempfile.gettempdir())
    findings: list[dict[str, str]] = []
    for key, path in _cited_log_paths(manifest):
        if _resolves_under(path, root):
            findings.append({"manifest": name, "key": key, "path": path})
    return findings


def _cited_log_paths(manifest: dict[str, Any]) -> Iterator[tuple[str, str]]:
    """Yield ``(manifest key, cited path)`` for every log a manifest cites."""
    for path in manifest.get("test_logs") or []:
        yield "test_logs", str(path)
    for key in ("baseline_suite", "after_suite"):
        observation = manifest.get(key)
        if isinstance(observation, dict):
            log_path = observation.get("log_path")
            if log_path:
                yield f"{key}.log_path", str(log_path)


def _resolves_under(path: str, root: str) -> bool:
    """Whether a path's resolved form is contained by the resolved root."""
    try:
        return os.path.commonpath((os.path.realpath(path), root)) == root
    except (OSError, ValueError):
        return False


def followup_ops_from_manifest(
    text: str,
    *,
    slug: str,
    section: str = "",
    written_by: str = "reckon-build",
    now: str | None = None,
) -> list[dict[str, Any]]:
    """Turn a manifest's candidate follow-ons into plan followup append ops.

    This is the worker end of the continuation chain. A worker fenced out of
    work it discovered has nowhere to put it but prose, where it is lost; an op
    per candidate carries it into plan state, and the one-line invocation keeps
    the live plan as the only place guidance lives. An unreadable manifest names
    no follow-ons: the refusal lands on the classification surfaces, and this
    reader stays tolerant so nothing downstream of it crashes.
    """
    try:
        manifest = parse_manifest(text)
    except ManifestParseError:
        return []
    stamp = now or _utc_now()
    invocation = f"/reckon-build {slug}" + (f" {section}" if section else "")
    ops: list[dict[str, Any]] = []
    for index, candidate in enumerate(manifest["follow_ons"], start=1):
        ops.append(
            {
                "op": "append",
                "target": "followups",
                "item": {
                    "id": f"f-{re.sub(r'[^a-z0-9]+', '-', slug.lower())}-{stamp.replace(':', '').replace('-', '')}-{index}",
                    "status": "open",
                    "written_by": written_by,
                    "written_at": stamp,
                    "title": candidate[:120],
                    "body": (
                        f"<p>Found by a worker on {slug} and fenced out of its "
                        f"write scope: {candidate}</p>"
                    ),
                    "recommends_skill": invocation,
                    "prompt": invocation,
                },
            }
        )
    return ops
