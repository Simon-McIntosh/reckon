#!/usr/bin/env python3
"""Count promoted review rows whose recorded head is not the head they were dispatched to read.

A review row's ``review.reviewed_head_sha`` is meant to name the revision the
review read. When the delivery predicate accepts a predecessor round's record as
the current run's delivery, the row instead carries another round's head and
score, and every reader of review totals inherits the wrong number. This census
is read-only: it asks, across every mounted project's committed ledger, how many
promoted run reviews carry a recorded head that is not the head they were
dispatched to read.

The dispatched head is taken from the strongest source the row carries: a
declared ``.at-<head>`` write path first, then the head suffix the review's node
id mints. A row whose recorded head does not begin with the dispatched head is
mismatched; a row carrying no dispatched head from any source is unclassifiable.

Run it with::

    PYTHONPATH=$PWD .venv/bin/python scripts/review_rows_with_another_rounds_verdict.py

It writes nothing. ``--json`` prints the census payload instead of a summary so
the document's figures can be regenerated from one run. ``--control`` runs the
unclassifiable-path control on its own: it pushes a synthetic row with no
dispatched head through the same classifier and reports whether the row lands in
the unclassifiable list, so the document's zero unclassifiable rows rests on a
path shown to fire rather than on a path never exercised.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# The script lives under ``scripts/``, outside the package, so the repository
# root is placed on the path to import the owners the census shares with the
# product: the delivery predicate that reads a record path's head, the writer's
# own node-id reader, and the registry reader — rather than re-reading a mounts
# file or re-parsing a node id a second time.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from reckon.crew.recovery_review_dispatch import _review_record_subject  # noqa: E402
from reckon.crew.recovery_vocabulary import REVIEW_NODE_PREFIX  # noqa: E402
from reckon.crew.repair import _ROUND_HEAD_DIGITS, round_node_source  # noqa: E402
from reckon.flight import FlightConfigError, mounted_project_docs  # noqa: E402

_REVIEW_ROLE = "review"
_RUNS_DIRNAME = "runs"
_STATE_DIRNAME = "state"


def _node_head_suffix(node_id: str) -> str:
    """The head a review node id mints, read through the writer's own reader.

    ``round_node_source`` strips the review prefix and, when present, the
    ``-<head[:12]>`` suffix ``round_node_id`` writes, and returns the source.
    The suffix is therefore the tail separating the prefix from that source: it
    is recovered as ``raw[len(source) + 1:]`` where ``raw`` is the node id
    without the prefix, and only when ``raw`` is exactly the source, one
    separator and the writer's digit count. Deriving it this way keeps one
    grammar — the writer's, read back through its own inverse — rather than a
    second regex that could drift from ``round_node_id``.
    """
    text = str(node_id or "")
    if not text.startswith(REVIEW_NODE_PREFIX):
        return ""
    raw = text[len(REVIEW_NODE_PREFIX) :]
    source = round_node_source(text, REVIEW_NODE_PREFIX)
    if len(raw) != len(source) + _ROUND_HEAD_DIGITS + 1 or not raw.startswith(
        source + "-"
    ):
        return ""
    return raw[len(source) + 1 :]


def _dispatched_head(record: object) -> str:
    """The head a review row was dispatched to read, or empty when it names none.

    The strongest source is a declared ``.at-<head>`` write path, read through
    the delivery code's own predicate. Falling back, the head suffix the node id
    mints names the same revision when the dispatch composed a head-keyed id; a
    hand dispatch under a coordinator's own id carries neither, and is left
    unresolved rather than guessed.
    """
    if not isinstance(record, dict):
        return ""
    node_definition = record.get("node_definition")
    write_paths = (node_definition or {}).get("write_paths") or []
    for declared in write_paths:
        _run_id, head = _review_record_subject(Path(str(declared)))
        if head:
            return head
    node = record.get("node")
    node_id = node if isinstance(node, str) else str((node or {}).get("id") or "")
    return _node_head_suffix(node_id)


def _recorded_head(record: object) -> str:
    """The head a review row's own verdict names, or empty."""
    if not isinstance(record, dict):
        return ""
    review = record.get("review")
    if not isinstance(review, dict):
        return ""
    return str(review.get("reviewed_head_sha") or "").strip()


def _recorded_total(record: object) -> object:
    """The score a review row's verdict records, or ``None``."""
    if not isinstance(record, dict):
        return None
    review = record.get("review")
    if not isinstance(review, dict):
        return None
    return review.get("total")


def _landing_commit(repo: Path, relative: str) -> str:
    """The short hash of the commit that last landed ``relative`` in ``repo``.

    An empty result is reported as empty rather than as a placeholder: a row
    whose landing commit cannot be resolved is a fact about the read, and a
    guessed hash would be evidence of nothing.
    """
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), "log", "-1", "--format=%h", "--", relative],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return ""
    return completed.stdout.strip().splitlines()[0] if completed.stdout.strip() else ""


def _head_revision(repo: Path) -> str:
    """The revision ``repo`` stands at, or empty when it cannot be read."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return ""
    head = completed.stdout.strip()
    return head if re.fullmatch(r"[0-9A-Fa-f]{40,64}", head) else ""


def _review_record_files(runs_dir: Path) -> list[Path]:
    return sorted(runs_dir.glob("*.json"))


def _classify(project: str, repo: Path, runs_dir: Path) -> dict[str, object]:
    """The census for one project: counts, and the rows behind each count.

    Shared by the portfolio census and the unclassifiable-path control, so the
    control proves the same code path the census relies on rather than a parallel
    copy of it.
    """
    reviews = 0
    mismatched: list[dict[str, object]] = []
    unclassifiable: list[dict[str, object]] = []
    for path in _review_record_files(runs_dir):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict) or record.get("role") != _REVIEW_ROLE:
            continue
        recorded = _recorded_head(record)
        if not recorded:
            # A review row carrying no verdict cannot carry another round's
            # verdict; it is not a promoted review with a recorded head.
            continue
        reviews += 1
        dispatched = _dispatched_head(record)
        run_id = str(record.get("run_id") or path.stem)
        if not dispatched:
            unclassifiable.append(
                {
                    "run_id": run_id,
                    "recorded_head": recorded,
                    "project": project,
                    "landing_commit": _landing_commit(repo, _relative(repo, path)),
                }
            )
            continue
        if not recorded.lower().startswith(dispatched.lower()):
            mismatched.append(
                {
                    "run_id": run_id,
                    "dispatched_head": dispatched,
                    "recorded_head": recorded,
                    "total": _recorded_total(record),
                    "promoted_by": record.get("promoted_by"),
                    "landing_commit": _landing_commit(repo, _relative(repo, path)),
                }
            )
    return {
        "project": project,
        "revision": _head_revision(repo),
        "reviews": reviews,
        "mismatched": mismatched,
        "unclassifiable": unclassifiable,
    }


def _relative(repo: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(repo.resolve()))
    except ValueError:
        return str(path)


def _census(mounts: dict[str, Path]) -> dict[str, object]:
    """The census across every mounted project, in project-name order."""
    projects: list[dict[str, object]] = []
    for project in sorted(mounts):
        mount = Path(mounts[project])
        repo = mount.parent
        runs_dir = mount / _STATE_DIRNAME / project / _RUNS_DIRNAME
        if not runs_dir.is_dir():
            continue
        projects.append(_classify(project, repo, runs_dir))
    totals = {
        "reviews": sum(p["reviews"] for p in projects),  # type: ignore[arg-type]
        "mismatched": sum(len(p["mismatched"]) for p in projects),  # type: ignore[arg-type]
        "unclassifiable": sum(len(p["unclassifiable"]) for p in projects),  # type: ignore[arg-type]
        "projects": len(projects),
    }
    return {"projects": projects, "totals": totals}


def _summary(payload: dict[str, object]) -> str:
    projects = payload["projects"]  # type: ignore[assignment]
    totals = payload["totals"]  # type: ignore[index]
    lines = [
        "Promoted review rows whose recorded head is not the dispatched head",
        "",
        f"{'project':<28}{'reviews':>9}{'mismatched':>12}{'unclassifiable':>16}",
    ]
    lines.extend(
        f"{row['project']:<28}{row['reviews']:>9}"
        f"{len(row['mismatched']):>12}{len(row['unclassifiable']):>16}"
        for row in projects
    )
    lines.append(
        f"{'TOTAL':<28}{totals['reviews']:>9}"
        f"{totals['mismatched']:>12}{totals['unclassifiable']:>16}"
    )
    lines.append("")
    lines.extend(
        f"MISMATCH {item['run_id']} dispatched={item['dispatched_head']} "
        f"recorded={item['recorded_head']} total={item['total']} "
        f"promoted_by={item['promoted_by']} landed={item['landing_commit']}"
        for row in projects
        for item in row["mismatched"]
    )
    lines.extend(
        f"UNCLASSIFIABLE {item['run_id']} recorded={item['recorded_head']} "
        f"landed={item['landing_commit']}"
        for row in projects
        for item in row["unclassifiable"]
    )
    return "\n".join(lines)


def _unclassifiable_control() -> int:
    """Show that a row with no dispatched head lands in the unclassifiable list.

    A synthetic promoted run-review row is written under a temporary runs
    directory — never a real ledger — and pushed through ``_classify``, the same
    function the census calls. The row carries a recorded verdict so the row is
    in the population, but declares only a legacy write path (no ``.at-``) and
    its node id carries no head suffix, so no dispatched head is resolvable and
    the row must be listed as unclassifiable. The classification is printed and
    the exit is non-zero when the row did not land there, so a green run is the
    control firing.
    """
    run_id = "r-20261018T000000000000-review-of-synthetic-row"
    row = {
        "run_id": run_id,
        "role": _REVIEW_ROLE,
        "node": "review-of-synthetic-row",
        "node_definition": {
            "write_paths": [
                "/nonexistent/synthetic/review-records/r-synthetic-row.json"
            ]
        },
        "review": {
            "status": "parsed",
            "reviewed_head_sha": "0123456789abcdef0123456789abcdef01234567",
            "total": 42,
        },
        "promoted_by": None,
    }
    base = Path(tempfile.mkdtemp(prefix="review-census-control-"))
    try:
        runs_dir = base / _STATE_DIRNAME / "synthetic" / _RUNS_DIRNAME
        runs_dir.mkdir(parents=True)
        (runs_dir / f"{run_id}.json").write_text(json.dumps(row), encoding="utf-8")
        result = _classify("synthetic", base, runs_dir)
        landed = [item["run_id"] for item in result["unclassifiable"]]
        print(f"control row: {run_id}")
        print(f"node id: {row['node']} (no head suffix)")
        print(f"write paths: {row['node_definition']['write_paths']} (no .at- path)")
        print(
            f"reviews={result['reviews']} mismatched={len(result['mismatched'])} "
            f"unclassifiable={len(result['unclassifiable'])}"
        )
        print(f"unclassifiable rows: {landed}")
    finally:
        shutil.rmtree(base, ignore_errors=True)
    if landed == [run_id]:
        print("CONTROL FIRED: the row landed in the unclassifiable list")
        return 0
    print("CONTROL FAILED: the row did not land in the unclassifiable list")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json", action="store_true", help="print the census payload as JSON"
    )
    parser.add_argument(
        "--control",
        action="store_true",
        help="run the unclassifiable-path control and exit",
    )
    args = parser.parse_args(argv)
    if args.control:
        return _unclassifiable_control()
    try:
        mounts = mounted_project_docs()
    except FlightConfigError as exc:
        print(f"mount registry unreadable: {exc}", file=sys.stderr)
        return 2
    payload = _census(mounts)
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(_summary(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
