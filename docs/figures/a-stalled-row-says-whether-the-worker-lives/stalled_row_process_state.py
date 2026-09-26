"""Draw what a stalled row now says about the run's process.

Three runs cross the same stall window with the same quiet time and different
remedies: one whose worker process is still alive and thinking, one whose
process this host checked and found gone, and one whose liveness no reading
here can establish. The row names which is which, in words, beside the minutes
it has been quiet — so the distinction reaches a reader who cannot see a
colour, a death is never claimed that nobody observed, and a coordinator acts
on the reading rather than on the word "stalled" alone.

The top panel is the time each run spent quiet against the window; the bottom
panel is the classifier's own composition, with the exact reason clause the
pane renders for each case, measured from the renderer.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

WINDOW = 900.0
QUIET = 1919.0
MINUTES = int(QUIET) // 60

LIVE_COLOUR = "#2f7d3a"
GONE_COLOUR = "#a4342c"
UNKNOWN_COLOUR = "#8a6d1f"
WINDOW_COLOUR = "#7a7a7a"
STREAM_COLOUR = "#4a6fa5"

FIGURES = Path(__file__).parent


def main() -> None:
    fig, (timeline, clause) = plt.subplots(
        2, 1, figsize=(11.0, 7.2), gridspec_kw={"height_ratios": [1.0, 1.35]}
    )
    fig.suptitle(
        "A stalled row says whether the worker's process lives",
        fontsize=13,
        y=0.975,
    )

    # ── Top: quiet time against the quiet 15 m window ─────────────────────
    rows = [
        (1.2, "worker process alive", LIVE_COLOUR),
        (0.6, "liveness unknown on this host", UNKNOWN_COLOUR),
        (0.0, "worker process gone", GONE_COLOUR),
    ]
    for y, label, colour in rows:
        timeline.hlines(y, 0, QUIET, color=colour, lw=6, alpha=0.25)
        timeline.plot(
            [QUIET],
            [y],
            marker="o",
            ms=9,
            color=colour,
            zorder=4,
        )
        timeline.annotate(
            f"{label}\nquiet {MINUTES} m, row enters stalled",
            xy=(QUIET, y),
            xytext=(QUIET + 180, y),
            va="center",
            fontsize=10,
            color=colour,
            arrowprops={"arrowstyle": "->", "color": colour, "lw": 1.2},
        )
        # The stalled row's own word, on the bar it belongs to.
        timeline.text(
            60,
            y + 0.30,
            "→ stalled",
            fontsize=9,
            color=colour,
            va="bottom",
        )

    timeline.axvline(WINDOW, color=WINDOW_COLOUR, lw=1.4, ls="--")
    timeline.text(
        WINDOW + 25,
        1.92,
        f"stall window {int(WINDOW)} s\n(quiet past it reads stalled)",
        fontsize=9,
        color=WINDOW_COLOUR,
        va="top",
    )
    timeline.set_xlim(-60, QUIET + 1500)
    timeline.set_ylim(-0.75, 1.95)
    timeline.set_yticks([])
    timeline.set_xlabel("quiet time on the run's newest stream (seconds)")
    timeline.set_title(
        "The same word, the same quiet time: what differs is the process",
        fontsize=11,
    )
    for side in ("top", "right", "left"):
        timeline.spines[side].set_visible(False)

    # ── Bottom: the classifier's composition and the rendered clause ──────
    clause.axis("off")
    clause.text(
        0.01,
        0.94,
        "the stalled branch of the classifier's reducer",
        fontsize=10,
        color="#333333",
    )
    steps = [
        ("quiet time from the run's own stream", f"{int(QUIET)} s  →  {MINUTES} m"),
        (
            "process verdict already on the row\n(process_alive, liveness_proven)",
            (
                "True              →  alive\n"
                "False, checked here →  process gone\n"
                "no pid, other host  →  liveness unknown"
            ),
        ),
        ("stalled detail the pane renders", "process state, quiet <n>m"),
    ]
    y = 0.80
    for left, right in steps:
        clause.text(0.01, y, left, fontsize=10, va="top", family="monospace")
        clause.text(0.46, y, right, fontsize=10, va="top", family="monospace")
        y -= 0.235

    y -= 0.03
    clause.text(
        0.01,
        y,
        "rendered rows (208-column pane, measured through format_watch_transition):",
        fontsize=10,
        va="top",
    )
    y -= 0.14
    rows_text = [
        (
            "r-quiet-held     working → stalled   alive, quiet 31m",
            LIVE_COLOUR,
        ),
        (
            "r-quiet-vacant   working → stalled   process gone, quiet 31m",
            GONE_COLOUR,
        ),
        (
            "r-quiet-unlogged working → stalled   liveness unknown, quiet 31m",
            UNKNOWN_COLOUR,
        ),
        (
            "at the base revision:  working → stalled   stream quiet for 1919s",
            WINDOW_COLOUR,
        ),
    ]
    for text, colour in rows_text:
        clause.text(
            0.04, y, text, fontsize=10, va="top", family="monospace", color=colour
        )
        y -= 0.115

    fig.tight_layout(rect=(0, 0, 1, 0.955))
    target = FIGURES / "stalled_row_process_state.png"
    fig.savefig(target, dpi=140)
    print(f"wrote {target}")


if __name__ == "__main__":
    main()
