"""The refusals a delivered manifest earns, stated once for both readers.

A worker can repair its own manifest only while it still holds a turn. So a
fault that promotion refuses hours later is worth nothing to the worker that
committed it: by then the turn has ended, the manifest is the delivery, and the
repair has to be made through the coordinator's own verbs. The check a worker
runs before delivery reads the same file, so it must refuse the same faults with
the same words, and it must be the same words rather than a second copy that can
drift away from promotion's.

This module holds the rules, and both readers ask it. Each rule returns a
:class:`Fault` naming the field and carrying the refusal text verbatim: the
audit appends ``fault.reason`` to its findings and promotion raises it as
``run <id> cannot be promoted: <reason>``, so the reason a worker reads before
delivery is the reason a coordinator reads at the gate.

What belongs here is a fault judged from the manifest alone, with no repository
measurement: whether the two readers agree is the whole point, and a rule that
had to run git would refuse differently depending on where it was called from.

Three rules live here, and each reason names the repair:

* a commit entry that carries more than the object id, such as a citation
  written as ``<sha> <subject>``, or several revisions in one entry;
* a suite arm whose revision is prose or an abbreviation, which cannot be
  compared with the base;
* a gate command naming an absolute path under the temporary directory, which
  is cleared before the run's record is ever read.

An unrecognised status is the fourth fault and is not restated here: both
readers refuse it through the manifest reader, which already raises with one
rejection message for both.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

SUITE_ARMS = ("baseline_suite", "after_suite")

# A recorded revision is a git object id, in either of the two hashes git writes.
FULL_REVISION = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")

_NONE_VALUES = {"", "none", "n/a", "-", "nil"}

# Characters a command's words pick up from shell quoting, a trailing clause, or
# an assignment's prefix. A path is judged after these are stripped.
_WORD_TRIM = "\"'`,;:()[]{}<>"


@dataclass(frozen=True)
class Fault:
    """One refusal a manifest earns, with the words both readers use."""

    field: str
    reason: str


def is_full_revision(revision: str) -> bool:
    """Whether a recorded revision is a commit object id rather than a spelling."""
    return FULL_REVISION.fullmatch(revision.strip().lower()) is not None


def manifest_faults(manifest: Mapping[str, Any]) -> list[Any]:
    """Every fault this module can judge from the manifest alone, in field order."""
    faults = []
    commit = _commit_fault(manifest)
    if commit is not None:
        faults.append(commit)
    faults.extend(
        fault
        for name in SUITE_ARMS
        if (fault := _arm_revision_fault(name, manifest.get(name))) is not None
    )
    gate = _gate_command_fault(manifest)
    if gate is not None:
        faults.append(gate)
    return faults


def commit_entry_reason(entry: str) -> str:
    """The refusal for a commit entry that is not a revision on its own."""
    return (
        f"commit entry {entry.strip()!r} is not a revision: an entry is the "
        "object id alone, so a citation written as '<sha> <subject>', or "
        "several revisions in one entry, cannot be resolved to a commit. "
        "Record one full object id per entry"
    )


def arm_revision_reason(name: str, revision: str) -> str:
    """The refusal for an arm whose revision is prose or an abbreviation."""
    return (
        f"{name}.revision {revision.strip()!r} is not a commit object id: an "
        "arm's revision is the id the command ran against, so a prose or "
        "abbreviated revision cannot be compared with the base it must match. "
        "Run the command at each revision, or re-point the arm with "
        f"`crew repair --arm {name} --revision <sha>`"
    )


def gate_command_reason(path: str) -> str:
    """The refusal for a gate command naming an absolute path in the temp root."""
    return (
        f"the gate command names {path!r}, an absolute path under the "
        "temporary directory, which is cleared without notice and is gone "
        "before any later reader re-runs the command. Name the test paths "
        "repository-relatively, or record the command that ran at the base "
        "revision"
    )


def resolves_under(path: str, root: str) -> bool:
    """Whether ``path`` points inside ``root`` once symbolic links are followed.

    Judged by where the path points rather than by whether it exists: a
    directory removed before delivery and one that was never written are the
    same citation, and both are refused.
    """
    candidate = os.path.realpath(os.path.expanduser(path))
    resolved_root = os.path.realpath(root)
    return candidate == resolved_root or candidate.startswith(resolved_root + os.sep)


def _commit_fault(manifest: Mapping[str, Any]) -> Fault | None:
    entries = manifest.get("commits")
    if isinstance(entries, str):
        entries = [entries]
    if not isinstance(entries, (list, tuple)):
        return None
    for entry in entries:
        text = str(entry).strip()
        if text and len(text.split()) > 1:
            return Fault(field="commits", reason=commit_entry_reason(text))
    return None


def _arm_revision_fault(name: str, observation: Any) -> Fault | None:
    """The refusal for an arm revision that is a spelling rather than an id.

    An arm whose revision is absent, or declared ``none``, is left to the
    completeness rules: what is wrong there is a missing measurement, not a
    value that cannot be compared. An arm whose revision is a full id is left to
    the comparison that measures whether it matches: a full revision that names
    a different commit is a claim the gate can test against the tree, and it
    heals rather than refuses.
    """
    if not isinstance(observation, Mapping):
        return None
    revision = str(observation.get("revision") or "").strip()
    if revision.lower() in _NONE_VALUES or is_full_revision(revision):
        return None
    return Fault(field=name, reason=arm_revision_reason(name, revision))


def _gate_command_fault(manifest: Mapping[str, Any]) -> Fault | None:
    command = str(manifest.get("tests") or "")
    if not command.strip():
        return None
    root = tempfile.gettempdir()
    for word in _command_words(command):
        if word.startswith(os.sep) and resolves_under(word, root):
            return Fault(field="tests", reason=gate_command_reason(word))
    return None


def _command_words(command: str) -> list[str]:
    """The words of a command line, each stripped of quoting and assignment prefixes."""
    words = []
    for raw in command.split():
        trimmed = raw.strip(_WORD_TRIM)
        if "=" in trimmed:
            trimmed = trimmed.rsplit("=", 1)[-1].strip(_WORD_TRIM)
        words.append(trimmed)
    return words
