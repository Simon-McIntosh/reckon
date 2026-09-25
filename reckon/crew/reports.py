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
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path, PurePosixPath
from typing import Any, TypedDict

import yaml

from reckon import ledger
from reckon.crew.node import NEEDS_HELP_FIELDS, NEEDS_HELP_MARKER, CrewError, TaskNode
from reckon.crew.runs import _utc_now

# ── Worker reports ──────────────────────────────────────────────────────────

_MANIFEST_LIST_KEYS = (
    "commits",
    "changed_paths",
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


def _as_list(value: Any) -> list[str]:
    """Split a manifest field into items, treating explicit nothing as empty."""
    if value is None:
        return []
    if isinstance(value, list):
        items = [str(item).strip() for item in value]
    else:
        text = str(value).strip()
        if text.startswith("[") and text.endswith("]"):
            # A bracketed list decodes to its elements; splitting a bracketed
            # value on commas leaves the bracket and quote characters in the
            # items, which then travel verbatim into rendered commands.
            items = _decode_bracketed_list(text)
        else:
            items = [part.strip() for part in re.split(r"[,\n]", str(value))]
    return [item for item in items if item and item.lower() not in _NONE_VALUES]


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
            r"^(tried|options|leaning|cost-if-wrong)\s*:\s*(.*)$", stripped, re.I
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


def audit_manifest(
    text: str,
    node: TaskNode | None = None,
    *,
    suite_armed: bool = False,
    worktree: Path | None = None,
    repository: Path | None = None,
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
    status = str(manifest.get("status", "")).lower()
    if status not in ("complete", "blocked", "failed"):
        findings.append(f"status {status!r} is not complete, blocked or failed")
    if status == "complete" and not manifest["commits"]:
        findings.append("status is complete but no commit is recorded")
    if status == "complete" and not manifest.get("tests"):
        findings.append("status is complete but no test result is recorded")
    if status == "complete" and suite_armed:
        for name in ("baseline_suite", "after_suite"):
            observation = manifest.get(name)
            if observation is None:
                findings.append(f"status is complete but {name} is missing")
                continue
            if not isinstance(observation, dict):
                findings.append(f"{name} must be an inline JSON object")
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
                findings.extend(
                    ledger.failure_attribution_missing_fields(
                        attribution,
                        after_observation
                        if isinstance(after_observation, dict)
                        else None,
                    )
                )
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
    return {"manifest": manifest, "findings": findings, "ok": not findings}


def _normalized_scope_path(text: Any) -> PurePosixPath:
    """Return a repository-relative path without redundancy but with ``..`` intact."""
    return PurePosixPath(str(text).strip())


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
    mapping promotion applies — a declaration naming a location outside the
    repository resolves to no root and so rejects a repository path at both
    surfaces. The worktree defaults to the working directory, which is where
    the write-time audit runs.
    """
    tree = Path.cwd() if worktree is None else Path(worktree)
    repo = tree if repository is None else Path(repository)
    target = _normalized_scope_path(changed)
    for root in _declared_scope_roots(declared, worktree=tree, repository=repo):
        if target == root or root in target.parents:
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
