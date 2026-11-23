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

* a suite arm whose revision is prose or an abbreviation, which cannot be
  compared with the base;
* a gate command naming an absolute path under the temporary directory, which
  is cleared before the run's record is ever read;
* a gate command whose first program word is an option rather than a program,
  which a shell cannot start, so the landing's replay stalls before measuring.

A gate command the run's own gate declaration supplies is judged in place of
the recorded line: the declaration states what the arms actually ran, and a
worker that recorded an option there has that declaration as its repair.

A commit entry is not judged here. Both readers resolve an entry to the ids it
carries, so a citation written as ``<sha> <subject>`` reads as the revision it
names on either side and needs no rule to keep the two agreeing.

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

# A revision that is the beginning of an object id: hexadecimal, and shorter than
# either hash. It is the id's shape, so it is an abbreviation rather than a name.
_OBJECT_ID_PREFIX = re.compile(r"(?:[0-9a-f]{4,39}|[0-9a-f]{41,63})")

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
    faults.extend(
        fault
        for name in SUITE_ARMS
        if (fault := _arm_revision_fault(name, manifest.get(name))) is not None
    )
    faults.extend(_gate_command_faults(manifest))
    return faults


def with_declared_gate(
    manifest: Mapping[str, Any], declared_command: str = ""
) -> Mapping[str, Any]:
    """The manifest as the rules should judge it, its gate command declared
    through ``crew declare-gate`` standing in place of the recorded line.

    A declaration states the command the run's arms actually ran, so it is the
    command the lander replays; a recorded line that names no program is the
    worker's to repair by recording the literal command or by declaring the one
    that ran. Calling the rules on the substituted mapping keeps them judging
    one gate command, whichever surface supplied it.
    """
    declared = str(declared_command or "").strip()
    if not declared:
        return manifest
    return {**manifest, "tests": declared}


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


def gate_program_reason(command: str, detail: str) -> str:
    """The refusal for a gate command naming no program a shell can start."""
    return (
        f"the recorded gate command {command!r} names no program a shell can "
        f"start: {detail}. The lander replays this text through a shell, which "
        "cannot start it, so the landing stalls without running the check. "
        "Record the literal command that ran, naming the program as well as its "
        "arguments (for example `.venv/bin/python -m pytest tests/test_a.py`), "
        "or declare the command that ran with `crew declare-gate --command "
        "'<the literal command>'`"
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


def _arm_revision_fault(name: str, observation: Any) -> Fault | None:
    """The refusal for an arm revision that is a spelling rather than an id.

    An arm whose revision is absent, or declared ``none``, is left to the
    completeness rules: what is wrong there is a missing measurement, not a
    value that cannot be compared. An arm whose revision is a full id is left to
    the comparison that measures whether it matches: a full revision that names
    a different commit is a claim the gate can test against the tree, and it
    heals rather than refuses. A value that is neither is refused only when its
    form is one no comparison can use — prose, or the beginning of an object id.
    Any other token is left to the comparison, which may well match it: a
    record may name the base by object id, and the manifest alone cannot tell
    which spellings the run's own record carries.
    """
    if not isinstance(observation, Mapping):
        return None
    revision = str(observation.get("revision") or "").strip()
    if revision.lower() in _NONE_VALUES or is_full_revision(revision):
        return None
    if _is_prose(revision) or _OBJECT_ID_PREFIX.fullmatch(revision.lower()):
        return Fault(field=name, reason=arm_revision_reason(name, revision))
    return None


def _is_prose(revision: str) -> bool:
    """Whether a revision is written as more than one word, or as an aside."""
    return len(revision.split()) > 1 or any(char in revision for char in "()[]")


def _gate_command_faults(manifest: Mapping[str, Any]) -> list[Fault]:
    """The refusals one gate command earns: a temp-tree path, then no program."""
    command = str(manifest.get("tests") or "")
    if not command.strip():
        return []
    faults = []
    root = tempfile.gettempdir()
    for word in _command_words(command):
        if word.startswith(os.sep) and resolves_under(word, root):
            faults.append(Fault(field="tests", reason=gate_command_reason(word)))
            break
    program = _program_fault(command)
    if program is not None:
        faults.append(program)
    return faults


def _program_fault(command: str) -> Fault | None:
    """The refusal for a gate command whose first word is an option, not a program.

    The lander hands the recorded text to a shell, and a shell reads a leading
    option as an argument list with no program to receive it: it exits without
    running any check. What is judged here is only that shape, because the
    manifest alone cannot say which programs a tree holds — whether a word that
    does look like a program resolves to an executable is the promotion's own
    question, answered against the run's checkout.
    """
    words, detail = _startable_words(command)
    if words is None:
        return Fault(field="tests", reason=gate_program_reason(command, detail))
    from reckon.crew.promotion_gate import _program_token_index

    index = _program_token_index(words)
    if index is None:
        return Fault(
            field="tests",
            reason=gate_program_reason(
                command,
                "it carries only variable assignments, so no program is left to start",
            ),
        )
    program = words[index]
    if not program.startswith("-"):
        return None
    return Fault(
        field="tests",
        reason=gate_program_reason(
            command,
            f"its first program word {program!r} is an option rather than the "
            "program that ran the check",
        ),
    )


def _startable_words(command: str) -> tuple[list[str] | None, str]:
    """The command's words as a shell splits them, or None and why it cannot."""
    import shlex

    try:
        return shlex.split(command), ""
    except ValueError as unparseable:
        return None, f"it does not tokenise as a shell command ({unparseable})"


def _command_words(command: str) -> list[str]:
    """The words of a command line, each stripped of quoting and assignment prefixes."""
    words = []
    for raw in command.split():
        trimmed = raw.strip(_WORD_TRIM)
        if "=" in trimmed:
            trimmed = trimmed.rsplit("=", 1)[-1].strip(_WORD_TRIM)
        words.append(trimmed)
    return words
