"""Reconcile an evidence fragment's figures against the logs it cites.

A fragment states what a measurement showed and names the log it came from.
Nothing in the delivery path compared the two, so a record could quote a
figure no run produced: a head column carrying the base revision, a timing
from a superseded arm, an attribution no log line carried. Each of those was
measured in this repository's own evidence, which is why the comparison lives
here rather than in a hand-run script; a check a reader can run is one a
reviewer can repeat.

The check has three parts, and each names the record it could not reconcile:

* every log the manifest cites must carry a revision header and a pytest
  summary;
* the fragment must state that revision and that summary beside each other,
  so a base figure cannot stand under a head revision;
* every figure the fragment quotes must be recorded by some cited log, so a
  stale number cannot outlive the run that produced it.

A fragment is prose and a log is text, so the comparison uses the two facts a
log makes repeatable: the revision in its header and the runner's own summary
line. Everything else in a log is incidental.

``python -m reckon.crew.fragment_reconcile <fragment.html> <manifest.md>``
runs. A fragment whose figures its logs record prints one PASS line and exits
zero; anything else prints one FAIL line per disagreement and exits one.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path

#: The revision a gate or suite log's header names.
REVISION_LINE = re.compile(r"^# revision: (?P<revision>[0-9a-f]{40})", re.MULTILINE)
#: The runner's own summary, the one figure a log states about itself.
SUMMARY_LINE = re.compile(
    r"^(?:=+ )?(?P<summary>\d+ (?:passed|failed)(?:, \d+ (?:failed|passed))?"
    r"(?:, \d+ \w+)? in [\d.]+s)",
    re.MULTILINE,
)
#: How far a quoted summary may sit from the revision it belongs to.
NEARBY_CHARACTERS = 2000
#: The same summary as it appears inside a fragment's prose or table cell.
QUOTED_SUMMARY = re.compile(
    r"\d+ (?:passed|failed)(?:, \d+ (?:failed|passed))?(?:, \d+ \w+)? in [\d.]+s"
)


class ManifestUnreadableError(ValueError):
    """A manifest the check cannot read, named so a reader can repair it."""


@dataclass(frozen=True)
class LogRecord:
    """The two facts a measurement log states about itself."""

    path: Path
    revision: str
    summary: str


def log_record(path: Path) -> LogRecord:
    """Read one log's revision and summary, or say which fact it lacks."""
    text = path.read_text(encoding="utf-8", errors="replace")
    revisions = REVISION_LINE.findall(text)
    if not revisions:
        raise ValueError(f"{path} carries no '# revision:' header line")
    summaries = SUMMARY_LINE.findall(text)
    if not summaries:
        raise ValueError(f"{path} carries no pytest summary line")
    return LogRecord(path=path, revision=revisions[0], summary=summaries[-1])


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
            f"the manifest's {key} is not one JSON object: {error.msg}"
            f" (character {error.pos})"
        ) from error
    if not isinstance(arm, dict) or not isinstance(arm.get("log_path"), str):
        raise ManifestUnreadableError(f"the manifest's {key} names no log_path")
    return arm


def cited_log_paths(manifest: str) -> list[Path]:
    """Every log the record and its arms name, in manifest order."""
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
    return paths


def _nearby(flat: str, summary: str, revision: str) -> bool:
    """Whether any occurrence of a quoted summary stands beside its revision."""
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
        if record.revision not in flat:
            failures.append(
                f"{record.path.name}: the fragment names no revision"
                f" {record.revision[:12]}"
            )
            continue
        if record.summary not in flat:
            failures.append(
                f"{record.path.name}: the fragment quotes no {record.summary!r}"
            )
            continue
        if not _nearby(flat, record.summary, record.revision):
            failures.append(
                f"{record.path.name}: {record.summary!r} is not stated beside"
                f" the {record.revision[:12]} it belongs to"
            )

    recorded = {record.summary for record in records}
    failures.extend(
        f"the fragment quotes {quoted!r}, which no cited log recorded"
        for quoted in QUOTED_SUMMARY.findall(fragment)
        if quoted not in recorded
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
