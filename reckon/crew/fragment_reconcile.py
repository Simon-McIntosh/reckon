"""Reconcile an evidence fragment's figures against the logs it cites.

A fragment states what a measurement showed and names the log it came from.
Nothing in the delivery path compared the two, so a record could quote a
figure no run produced: a head column carrying the base revision, a timing
from a superseded arm, an attribution no log line carried. The comparison
therefore runs inside ``crew check --manifest``, which reads the node's own
fragment and the manifest beside it while the worker still holds the turn.

Every fact it compares is read through the reader this repository already
owns, so the module holds no second pattern that could drift from the audit
that later reads the same log:

* the result line is :func:`reckon.crew.review.pytest_summary_line`, which
  the gate's own receipt counts failures from;
* the revision is the gate log audit's readers,
  :func:`reckon.crew.reports._first_recorded_line` and
  :data:`reckon.crew.reports._GATE_LOG_REVISION`, which skip the fence's launch
  notice and read the label the fleet's capture scripts write as written;
* the arms are the typed objects :func:`reckon.crew.reports.parse_manifest`
  already returns, so a manifest written as JSON is read like any other.

The check has three parts, and each names the record it could not reconcile:

* every log the manifest cites must name its revision and its result;
* the fragment must name that revision;
* the fragment must quote that result beside that revision, so a base figure
  cannot stand under a head revision.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

from reckon.crew.reports import (
    _GATE_LOG_REVISION,
    _RECORDED_REVISION,
    ManifestParseError,
    _first_recorded_line,
    _names_the_same_revision,
    parse_manifest,
)
from reckon.crew.review import pytest_summary_line

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
    summary = pytest_summary_line(text)
    if summary is None:
        raise ValueError(f"{path} names no runner result line")
    return LogRecord(path=path, revision=named.group(1), summary=summary)


def _stated_revision(flat: str, revision: str) -> str | None:
    """The fragment's spelling of a log's revision, abbreviations included."""
    for token in _RECORDED_REVISION.finditer(flat):
        if _names_the_same_revision(token.group(0), revision):
            return token.group(0)
    return None


def _parsed(manifest: str) -> dict:
    """The manifest's typed fields, refused rather than guessed when unreadable."""
    try:
        return parse_manifest(manifest)
    except ManifestParseError as error:
        raise ManifestUnreadableError(
            f"the manifest could not be read: {error}"
        ) from error


def arms(manifest: str) -> list[dict]:
    """Every suite arm the manifest records, each refused if it is not an object."""
    fields = _parsed(manifest)
    found: list[dict] = []
    for key in ("baseline_suite", "after_suite"):
        value = fields.get(key)
        if value in (None, "", {}):
            continue
        if not isinstance(value, dict):
            raise ManifestUnreadableError(
                f"the manifest's {key} is not one JSON object"
            )
        found.append(value)
    return found


def cited_log_paths(manifest: str) -> list[Path]:
    """Every log the manifest's arms and test log list name, in order."""
    paths: list[Path] = [
        Path(arm["log_path"]) for arm in arms(manifest) if arm.get("log_path")
    ]
    listing = _parsed(manifest).get("test_logs") or []
    if isinstance(listing, str):
        listing = listing.split(",")
    paths.extend(
        Path(entry.strip())
        for entry in listing
        if str(entry).strip() and Path(str(entry).strip()).suffix in {".log", ".txt"}
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
