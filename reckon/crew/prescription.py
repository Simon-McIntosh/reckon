"""Judge whether a node is prescribed enough for the locally served lane.

The lead ruling of 2026-09-21 06:35Z reopens the local lane for *prescribed*
implementation nodes only: one artifact, with the file and line named, a
numeric gate with its before value stated, a literal negative-control
mutation, and a thirty-minute fence. This module states that rule once, as a
judgement over a node record, so a dispatcher and its callers read one
definition rather than restating the ruling in prose that drifts from it.

Purity is deliberate. The ruling is a property of the node record and nothing
else, so the verdict is computable before any worktree, plan file or flight
configuration exists: a dispatcher can refuse an unprescribed node the moment
it is shaped, and a caller can judge a synthesised node with no repository, no
plan and no environment to read.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

from reckon.crew.node import (
    NEGATIVE_CONTROL_NONE,
    CrewError,
    TaskNode,
    is_test_path,
    parse_duration,
)

# The five properties the ruling names, in the order it names them. Order is
# part of the contract a reader meets: a node producing more than one artifact
# is refused before its gate is judged.
PRESCRIBED_PROPERTIES = (
    "one-artifact",
    "file-and-line",
    "numeric-gate",
    "literal-negative-control",
    "time-fence",
)

# A path:line reference — a file name carrying an extension, a colon, and at
# least one digit of line number. The path may be relative or absolute and may
# carry directory separators. What makes the reference a place a reader can
# open is the line number, so a bare file name mentioned in passing does not
# satisfy it.
FILE_LINE_RE = re.compile(r"[A-Za-z0-9_][\w.+-]*(?:/[\w.+-]+)*\.[A-Za-z0-9_]+:\d+")

# A gate's before value is stated when the measure names the baseline it was
# measured against. A bare count with no baseline reads as a target rather than
# a delta, so the verdict cannot tell a node that moved a number from one that
# merely aims at one.
BASELINE_RE = re.compile(
    r"\b(?:before|baseline|previously|currently|existing|measured|"
    r"was|were|at present|pre-change)\b",
    re.IGNORECASE,
)

# The fence the ruling sets: at most thirty minutes. Anything longer is more
# than one artifact's worth of literal work and belongs on a metered lane.
MAX_FENCE_SECONDS = 30 * 60

_NONE_PREFIX = f"{NEGATIVE_CONTROL_NONE}:"


def is_landing_path(path: str, plan: str) -> bool:
    """Return whether a write path is a plan's shared landing record.

    Every node on a plan appends its landing record to the evidence record and
    its section entry to the plan file, so those two paths sit in every node's
    write scope and are owned by none of them. They are record rather than
    artifact: counting them as artifacts would refuse every node dispatched on
    a plan, the prescribed pilot included. The spelling is derived from the
    plan slug alone so the judgement stays pure, and the pair it names is the
    same pair ``reckon.crew.dispatch`` grants as shared landing paths.
    """
    parts = PurePosixPath(str(path)).parts
    if not parts or not plan:
        return False
    name = parts[-1]
    if name == f"{plan}-landed.html":
        return True
    return name == f"{plan}.html" and "plans" in parts[:-1]


def artifact_paths(node: TaskNode) -> list[str]:
    """Return the declared write paths that are the node's own artifacts.

    A test file is the check beside an artifact rather than a second artifact,
    and the two landing records are shared by every node on the plan. What
    remains is the set of paths that count against the one-artifact property.
    """
    return [
        str(path)
        for path in node.write_paths
        if not is_test_path(str(path)) and not is_landing_path(str(path), node.plan)
    ]


def judge_prescribed(node: TaskNode) -> dict[str, Any]:
    """Judge a node against the ruling, naming every property it fails.

    Every property is reported rather than stopping at the first, so a caller
    reshaping a node sees the whole list in one pass. The verdict is advisory
    here and binding wherever dispatch consults it: a node that is not
    prescribed is one the local lane is not open for.
    """
    failures: list[str] = []
    detail: dict[str, str] = {}

    def fail(prop: str, reason: str) -> None:
        failures.append(prop)
        detail[prop] = reason

    artifacts = artifact_paths(node)
    if len(artifacts) > 1:
        fail(
            "one-artifact",
            f"the node names {len(artifacts)} non-test artifacts "
            f"({', '.join(artifacts)}); a prescribed node is one artifact, so "
            "split it into one node per artifact",
        )

    located = f"{node.goal}\n{node.done_when}"
    if not FILE_LINE_RE.search(located):
        fail(
            "file-and-line",
            "neither the goal nor the done-when names a file and line "
            "(path:line); name the place the change lands so the worker reads "
            "the change rather than searching for it",
        )

    if not re.search(r"\d", node.done_when) or not BASELINE_RE.search(node.done_when):
        fail(
            "numeric-gate",
            "the done-when states no numeric gate with its before value; name "
            "the count the gate produces and the baseline it is measured "
            "against, so a reader can tell a moved number from an aimed one",
        )

    declaration = str(node.negative_control or "").strip()
    if not declaration or declaration.lower().startswith(_NONE_PREFIX):
        fail(
            "literal-negative-control",
            "the node declares no literal negative-control mutation; name the "
            "mutation that must make its check fail, or the lane cannot tell a "
            "check that fired from one that never ran",
        )

    budget = str(node.time_budget or "").strip()
    try:
        seconds = parse_duration(budget)
    except CrewError as exc:
        fail("time-fence", str(exc))
    else:
        if seconds > MAX_FENCE_SECONDS:
            fail(
                "time-fence",
                f"the fence {budget!r} exceeds the thirty-minute limit a "
                "prescribed node carries; split the work rather than overrun it",
            )

    return {"prescribed": not failures, "failures": failures, "detail": detail}
