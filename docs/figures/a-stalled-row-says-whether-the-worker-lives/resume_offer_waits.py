"""Draw what the sweep renders for a met wait, by process reading.

A run parked on an external condition is resumed when the condition reports
terminal, and the row that says so is what a coordinator acts on. The
condition is only half the question: a worker still writing its run must not
be offered a resume, because the offer starts a second worker on the same run,
and a reading nobody took is not a death either. So the offer, its action and
the row's recovery classification are gated on the three-way process reading
the stalled detail already states, and every other reading renders the same
event as a wait that names the reading it is holding.

The rows in the bottom panel are not transcribed: this script builds the
fixture pointers and renders them through the classifier, the watcher
snapshot, the transition the events log persists and
``format_watch_transition`` at the pane width, so the figure is drawn from the
renderer under test. The base-revision rows are measured by running this same
script against the base tree with the node's test module present, and handed
back in through ``RESUME_OFFER_BASE_ROWS`` (a JSON file of ``{run_id: clause}``)
&mdash; which is what lets the comparison show the reading the gate replaced
rather than a remembered one.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, Rectangle

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

LIVE_RUN_ID = "r-parked-held"
GONE_RUN_ID = "r-parked-vacant"
UNLOGGED_RUN_ID = "r-parked-unlogged"

OFFER_COLOUR = "#2f7d3a"
WAIT_COLOUR = "#a4342c"
UNKNOWN_COLOUR = "#8a6d1f"
BASE_COLOUR = "#6f6f6f"
INK = "#2b2b2b"

FIGURES = Path(__file__).parent


def _measured_rows() -> list[tuple[str, str, str]]:
    """One clause per process reading, measured through the renderer."""
    os.environ.setdefault(
        "RECKON_HOME", tempfile.mkdtemp(prefix="resume-offer-figure-")
    )
    from tests import test_resume_offer_needs_a_dead_process as offers
    from tests.test_a_live_run_never_reads_dead import _absent_pid

    cases = [
        ("live worker, process checked here", LIVE_RUN_ID, os.getpid(), WAIT_COLOUR),
        (
            "worker's end observed on this host",
            GONE_RUN_ID,
            _absent_pid(),
            OFFER_COLOUR,
        ),
        ("no pid recorded, nothing observed", UNLOGGED_RUN_ID, None, UNKNOWN_COLOUR),
    ]
    with tempfile.TemporaryDirectory(prefix="resume-offer-figure-tree-") as scratch:
        root = Path(scratch)
        measured = []
        for label, run_id, pid, colour in cases:
            case_root = root / run_id
            case_root.mkdir(parents=True, exist_ok=True)
            pointer, moment, _ = offers._parked_pointer(case_root, run_id, pid=pid)
            row, snapshot, line = offers._render_parked_row(pointer, moment)
            clause = str(row["fleet_verdict"]["detail"])
            print(
                f"{run_id}\tstate={snapshot['state']}\trecovery={row['recovery']}\t{clause}"
            )
            print(f"{run_id}\tline\t{line}")
            measured.append((label, clause, colour))
        dump = os.environ.get("RESUME_OFFER_ROWS_OUT", "").strip()
        if dump:
            Path(dump).write_text(
                json.dumps({label: clause for label, clause, _ in measured}, indent=2),
                encoding="utf-8",
            )
        return measured


def _box(fig, x, y, w, h, text, colour, *, fontsize=8.5) -> None:
    fig.add_artist(
        Rectangle(
            (x, y),
            w,
            h,
            transform=fig.transFigure,
            linewidth=1.1,
            edgecolor=colour,
            facecolor="white",
        )
    )
    fig.text(
        x + w / 2,
        y + h / 2,
        text,
        ha="center",
        va="center",
        fontsize=fontsize,
        color=colour,
    )


def _arrow(fig, start, end, colour) -> None:
    fig.add_artist(
        FancyArrowPatch(
            start,
            end,
            transform=fig.transFigure,
            arrowstyle="-|>",
            mutation_scale=12,
            linewidth=1.2,
            color=colour,
            connectionstyle="arc3,rad=0.0",
        )
    )


def main() -> None:
    measured = _measured_rows()
    base_path = os.environ.get("RESUME_OFFER_BASE_ROWS", "").strip()
    base_rows: dict[str, str] = {}
    if base_path:
        base_rows = json.loads(Path(base_path).read_text(encoding="utf-8"))

    fig = plt.figure(figsize=(12.5, 9.0))
    fig.suptitle(
        "A resume offer waits until the worker's process is observed to have ended",
        fontsize=13,
        y=0.975,
    )

    # ── Gate: one condition, three process readings, two outcomes ─────────
    fig.text(
        0.03,
        0.94,
        "the wait-met arm: the condition has reported terminal, and the process reading decides",
        fontsize=10.5,
        color=INK,
        va="top",
    )
    _box(
        fig,
        0.03,
        0.795,
        0.15,
        0.09,
        "sweep reads\nthe run:\nwait_condition\nstate == 'met'",
        WAIT_COLOUR,
        fontsize=8,
    )
    _arrow(fig, (0.18, 0.84), (0.235, 0.84), INK)
    _box(fig, 0.24, 0.81, 0.10, 0.06, "process\nreading", INK, fontsize=9)
    outcomes = [
        (
            0.865,
            "alive",
            WAIT_COLOUR,
            "waiting: the reason names the condition and the reading held",
        ),
        (
            0.755,
            "liveness unknown",
            UNKNOWN_COLOUR,
            "waiting: the reading is named, so the withheld offer is legible",
        ),
        (
            0.645,
            "process gone",
            OFFER_COLOUR,
            "ready to resume: <condition> declared terminal  ·  recovery = ready, action = resume",
        ),
    ]
    for y, label, colour, text in outcomes:
        _arrow(fig, (0.34, 0.84), (0.48, y + 0.018), colour)
        fig.text(0.355, y + 0.032, label, fontsize=8.5, color=colour, va="bottom")
        _box(fig, 0.48, y - 0.018, 0.49, 0.072, text, colour, fontsize=8.5)
    fig.text(
        0.03,
        0.60,
        "at the base revision no reading was consulted: an alive and an unproven run read each get\n"
        '"ready to resume: <condition> …" beside recovery = ready — the offer the gate withholds.',
        fontsize=8.5,
        color=BASE_COLOUR,
        va="top",
    )

    # ── The clause the classifier composes ────────────────────────────────
    fig.text(
        0.03,
        0.525,
        "the clause the classifier composes, and the action that rides with it",
        fontsize=10.5,
        color=INK,
        va="top",
    )
    clause_left = 0.03
    clause_right = 0.165
    lines = [
        ("reading", "detail  ·  action  ·  recovery"),
        (
            "process gone",
            "ready to resume: <condition> reported '<observed>', a declared terminal state",
        ),
        (
            "",
            "action: reckon crew resume --run <id> --advice continue   ·   recovery = ready",
        ),
        (
            "alive /",
            "waiting on <condition>: the probe reported '<observed>', a declared terminal",
        ),
        (
            "unknown",
            "state, but the process reading is '<reading>', so no lift is offered until the process",
        ),
        ("", "is observed to have ended   ·   recovery = waiting"),
    ]
    y = 0.495
    for left, right in lines:
        fig.text(
            clause_left, y, left, fontsize=8.5, va="top", family="monospace", color=INK
        )
        fig.text(
            clause_right,
            y,
            right,
            fontsize=8.5,
            va="top",
            family="monospace",
            color=INK,
        )
        y -= 0.028

    # ── The rows as the pane renders them ─────────────────────────────────
    fig.text(
        0.03,
        0.325,
        "rendered clauses, measured through classify_pointer / _watch_transition / format_watch_transition at 208 columns",
        fontsize=10.5,
        color=INK,
        va="top",
    )

    def _clause_lines(clause: str, width: int = 138) -> list[str]:
        words = clause.split(" ")
        out, line = [], ""
        for word in words:
            candidate = f"{line} {word}".strip()
            if len(candidate) > width and line:
                out.append(line)
                line = word
            else:
                line = candidate
        if line:
            out.append(line)
        return out

    y = 0.294
    for label, clause, colour in measured:
        fig.text(0.03, y, f"{label}:", fontsize=8.5, color=colour, va="top")
        for index, line in enumerate(_clause_lines(clause)):
            fig.text(
                0.055,
                y - 0.023 - index * 0.020,
                line,
                fontsize=8,
                color=colour,
                va="top",
                family="monospace",
            )
        y -= 0.065
    if base_rows:
        distinct = sorted(set(base_rows.values()))
        fig.text(
            0.03,
            y,
            "at the base revision (all three readings):",
            fontsize=8.5,
            color=BASE_COLOUR,
            va="top",
        )
        for index, clause in enumerate(distinct):
            fig.text(
                0.055,
                y - 0.023 - index * 0.020,
                clause,
                fontsize=8,
                color=BASE_COLOUR,
                va="top",
                family="monospace",
            )

    out = FIGURES / "resume_offer_waits.png"
    fig.savefig(out, dpi=140)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
