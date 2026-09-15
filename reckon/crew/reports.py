from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Iterator
from typing import Any, TypedDict

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


# The manifest field vocabulary. A text body whose parsed top-level fields are
# all outside this set is incidental prose wearing a ``key: value`` shape — the
# body carries no field a manifest carries, so its status cannot be determined.
_MANIFEST_FIELD_KEYS = frozenset(_MANIFEST_LIST_KEYS) | frozenset(
    {
        "node",
        "status",
        "tests",
        "needs_help",
        "derived",
        "baseline_suite",
        "after_suite",
        "failure_attribution",
        "orientation_worktree",
        "orientation_base_sha",
        "orientation_write_paths",
    }
)

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


def _parse_text_manifest(text: str, *, path: str | None = None) -> dict[str, Any]:
    """Read the tolerant ``key: value`` text form, keeping unknown keys.

    A top-level line carrying a second manifest key after its value is refused
    here rather than misparsed: the tolerant reader would otherwise fold the
    whole remainder of the line into the first key's value and leave the later
    field absent (an empty list), which reads as a protection the worker never
    wrote. ``path`` names the file in the refusal.
    """
    fields: dict[str, Any] = {}
    key = None
    block_key: str | None = None
    block_lines: list[str] = []

    def flush_block() -> None:
        nonlocal block_key, block_lines
        if block_key is not None:
            fields[block_key] = "\n".join(block_lines).strip()
        block_key = None
        block_lines = []

    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if block_key is not None:
            # A blank or indented line continues the block; only a line with
            # content starting at column 0 is a new top-level entry.
            if line == "" or raw[:1] in (" ", "\t"):
                block_lines.append(line)
                continue
            flush_block()
        if raw[:1] in (" ", "\t"):
            # Indented content continues the current key only as a list item;
            # an indented key line is nested data and must not be read at the
            # top level, where it silently replaces the node fact the surfaces
            # present as runnable.
            if key and line.startswith(("-", "*")):
                addition = line.lstrip("-* ").strip()
                fields[key] = f"{fields[key]}, {addition}" if fields[key] else addition
            continue
        match = re.match(r"^([a-z][a-z0-9_-]*)\s*:\s*(.*)$", line, re.IGNORECASE)
        if match:
            key = match.group(1).lower().replace("-", "_")
            value = match.group(2).strip()
            embedded = _embedded_manifest_key(key, value)
            if embedded:
                raise ManifestParseError(
                    _two_keys_on_one_line_message(path, line_no, line, key, embedded)
                )
            if _is_block_indicator(value):
                fields.setdefault(key, "")
                block_key = key
                block_lines = []
            else:
                fields[key] = value
        elif key and line.startswith(("-", "*")):
            addition = line.lstrip("-* ").strip()
            fields[key] = f"{fields[key]}, {addition}" if fields[key] else addition
    flush_block()
    return fields


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
    if status is not None and str(status).strip():
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
        f"cannot read manifest{where}: status {word!r} is not a recognised "
        f"manifest status (recognised: {recognised}); expected a JSON object "
        "or the 'key: value' text form carrying one of those statuses"
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
        allowed = set(node.write_paths)
        stray = sorted(
            path for path in manifest["changed_paths"] if path not in allowed
        )
        if stray:
            findings.append(
                "changed paths outside the write scope: " + ", ".join(stray)
            )
    return {"manifest": manifest, "findings": findings, "ok": not findings}


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
    written_by: str = "reckon-ship",
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
    invocation = f"/reckon-ship {slug}" + (f" {section}" if section else "")
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
