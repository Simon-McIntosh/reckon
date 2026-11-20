"""Reconcile an evidence fragment's figures against the logs it cites.

A fragment states what a measurement showed and names the log it came from.
Nothing in the delivery path compared the two, so a record could quote a
figure no run produced: a head column carrying the base revision, a timing
from a superseded arm, an attribution no log line carried. The comparison
therefore runs inside ``crew check --manifest``, which reads the node's own
fragment and the manifest beside it while the worker still holds the turn.

Both facts it compares are read through the recognisers this repository
already owns, so the module holds no second pattern that could drift from the
audit that later reads the same log:

* the revision is :func:`reckon.crew.review.recorded_log_revision`, which reads
  the label the fleet's capture scripts write, in any case and at any length a
  manifest can be compared against;
* the result line is the last line matching
  :data:`reckon.crew.review.RESULT_COUNT_RE`, the same recogniser the gate's
  own receipt reads.

The check has three parts, and each names the record it could not reconcile:

* every log the manifest cites must name its revision and its result;
* the fragment must name that revision;
* the fragment must quote that result beside that revision, so a base figure
  cannot stand under a head revision.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

from reckon.crew.reports import (
    _GATE_LOG_REVISION,
    _RECORDED_REVISION,
    _first_recorded_line,
    _names_the_same_revision,
)
from reckon.crew.review import RESULT_COUNT_RE

#: How far a quoted result may sit from the revision it belongs to.
NEARBY_CHARACTERS = 2000


class ManifestUnreadableError(ValueError):
    """A manifest the check cannot read, named so a reader can repair it."""


@dataclass(frozen=True)
class LogRecord:
    """The two facts a measurement log states about itself."""

    path: Path
    revision: str
    summary: str


def result_line(text: str) -> str | None:
    """The last runner result line in a log, or ``None`` when it states none.

    A pytest run prints one result line at its end, so the last is the one the
    capture's exit status belongs to. A line naming a test (``FAILED …::…``) is
    skipped even when its reason quotes a count, which is the rule the gate's
    own receipt reads by.
    """
    found = None
    for raw in text.splitlines():
        line = raw.strip().strip("=").strip()
        if not line or "::" in line or line.startswith(("FAILED", "ERROR")):
            continue
        if RESULT_COUNT_RE.search(line):
            found = line
    return found


def log_record(path: Path) -> LogRecord:
    """Read one log's revision and result, or say which fact it lacks.

    The revision is read the way the gate log audit reads it: the header line
    after the fence's launch notice, matched by the fleet's own label pattern,
    so a log passes here and there for the same reason.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    named = _GATE_LOG_REVISION.search(_first_recorded_line(text))
    if named is None:
        raise ValueError(f"{path} names no revision on its first line")
    summary = result_line(text)
    if summary is None:
        raise ValueError(f"{path} names no runner result line")
    return LogRecord(path=path, revision=named.group(1), summary=summary)


def _stated_revision(flat: str, revision: str) -> str | None:
    """The fragment's spelling of a log's revision, abbreviations included."""
    for token in _RECORDED_REVISION.finditer(flat):
        if _names_the_same_revision(token.group(0), revision):
            return token.group(0)
    return None


def manifest_field(manifest: str, key: str) -> str | None:
    """The value of one manifest line, or None when the line is absent."""
    for line in manifest.splitlines():
        if line.startswith(f"{key}:"):
            return line.split(":", 1)[1].strip()
    return None


def arm_fields(manifest: str, key: str) -> dict:
    """One suite arm's record, refused rather than guessed when malformed."""
    value = manifest_field(manifest, key)
    if value is None:
        raise ManifestUnreadableError(f"the manifest carries no {key} line")
    try:
        arm = json.loads(value)
    except json.JSONDecodeError as error:
        raise ManifestUnreadableError(
            f"the manifest's {key} is not one JSON object: {error.msg} "
            f"(character {error.pos})"
        ) from error
    if not isinstance(arm, dict) or not isinstance(arm.get("log_path"), str):
        raise ManifestUnreadableError(f"the manifest's {key} names no log_path")
    return arm


def cited_log_paths(manifest: str) -> list[Path]:
    """Every log the manifest's arms and test log list name, in order."""
    paths: list[Path] = [
        Path(arm_fields(manifest, key)["log_path"])
        for key in ("baseline_suite", "after_suite")
        if manifest_field(manifest, key) is not None
    ]
    listing = (manifest_field(manifest, "test_logs") or "").split(",")
    paths.extend(
        Path(entry.strip())
        for entry in listing
        if entry.strip() and Path(entry.strip()).suffix in {".log", ".txt"}
    )
    seen: list[Path] = []
    for path in paths:
        if path not in seen:
            seen.append(path)
    return seen


def _nearby(flat: str, summary: str, revision: str) -> bool:
    """Whether any occurrence of a quoted result stands beside its revision."""
    start = 0
    while True:
        position = flat.find(summary, start)
        if position < 0:
            return False
        window = flat[
            max(0, position - NEARBY_CHARACTERS) : position + NEARBY_CHARACTERS
        ]
        if revision in window:
            return True
        start = position + 1


def reconcile(fragment: str, manifest: str) -> list[str]:
    """Every figure the fragment and its cited logs disagree about."""
    flat = " ".join(fragment.split())
    failures: list[str] = []
    records: list[LogRecord] = []

    for path in cited_log_paths(manifest):
        if not path.is_file():
            failures.append(f"cited log is absent: {path}")
            continue
        try:
            records.append(log_record(path))
        except ValueError as error:
            failures.append(str(error))

    for record in records:
        stated = _stated_revision(flat, record.revision)
        if stated is None:
            failures.append(
                f"{record.path.name}: the fragment names no revision "
                f"{record.revision[:12]}"
            )
            continue
        if record.summary not in flat:
            failures.append(
                f"{record.path.name}: the fragment quotes no {record.summary!r}"
            )
            continue
        if not _nearby(flat, record.summary, stated):
            failures.append(
                f"{record.path.name}: {record.summary!r} is not stated beside "
                f"the {record.revision[:12]} it belongs to"
            )
    return failures


def main(argv: list[str] | None = None) -> int:
    """Run the check over one fragment and the manifest that cites its logs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fragment", type=Path)
    parser.add_argument("manifest", type=Path)
    arguments = parser.parse_args(argv)
    fragment = arguments.fragment.read_text(encoding="utf-8")
    manifest = arguments.manifest.read_text(encoding="utf-8")
    try:
        failures = reconcile(fragment, manifest)
    except ManifestUnreadableError as error:
        print(f"FAIL {error}")
        return 1
    for failure in failures:
        print(f"FAIL {failure}")
    if failures:
        return 1
    print("PASS every figure the fragment quotes is the one its log recorded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
