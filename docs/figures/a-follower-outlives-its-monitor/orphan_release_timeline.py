"""Draw the follower release on a common time axis, beside the defect it removes.

Two panels share one time axis measured from the arm. The upper panel is the
release path: the consumer is killed, the follower sees a parent it did not
record on its next wait pass, releases the registration by leaving, and prints
nothing. The lower panel is the same kill with the consumer check deleted: the
orphan keeps the lock, so the session's registration is never free and the next
follower cannot take it.

The x-axis is mechanism time rather than wall clock: the follower acts within
one poll pass of the kill, and the control window is the ten seconds the gate
allows. The gate's own wall clock (interpreter start and import) is stated in
the caption instead, because it is arrival rather than the release.

Run: python docs/figures/a-follower-outlives-its-monitor/orphan_release_timeline.py
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

POLL_SECONDS = 0.1
KILL_AT = 2.0
RELEASE_AT = KILL_AT + POLL_SECONDS
WINDOW_SECONDS = 10.0
CONTROL_END = KILL_AT + 10.6
LANES = ("consumer (the parent)", "registration lock", "follower (its own exit)")

HELD = "#2f6fb3"
FREE = "#c9d6e6"
GONE = "#b3b3b3"
DENIED = "#c0392b"


def _lane(axis, y: float, start: float, end: float, color: str, label: str) -> None:
    axis.add_patch(
        Rectangle((start, y), end - start, 0.6, facecolor=color, edgecolor="white")
    )
    axis.text(
        (start + end) / 2,
        y + 0.3,
        label,
        ha="center",
        va="center",
        color="white",
        fontsize=9,
    )


def _panel(axis, title: str) -> None:
    axis.set_ylim(-0.2, len(LANES) + 0.5)
    axis.set_yticks([i + 0.3 for i in range(len(LANES))])
    axis.set_yticklabels(LANES, fontsize=9)
    axis.set_title(title, fontsize=11, loc="left")
    axis.set_xlim(-0.4, CONTROL_END + 0.6)
    axis.axvline(KILL_AT, color="black", linestyle="--", linewidth=1.5)
    axis.text(
        KILL_AT, len(LANES) - 0.05, " SIGKILL to the consumer", fontsize=9, va="top"
    )


def main() -> None:
    figure, (release, defect) = plt.subplots(2, 1, figsize=(11, 6.4), sharex=True)

    _panel(release, "(a) with the consumer check: the orphan releases and leaves")
    release.axvspan(KILL_AT, KILL_AT + WINDOW_SECONDS, color=HELD, alpha=0.06)
    release.text(
        KILL_AT + WINDOW_SECONDS / 2,
        len(LANES) + 0.25,
        "gate window: release observed within 10 s of the kill",
        ha="center",
        va="center",
        fontsize=8.5,
        color=HELD,
    )
    _lane(release, 2, 0.0, KILL_AT, GONE, "alive")
    _lane(release, 1, 0.0, RELEASE_AT, HELD, "held")
    _lane(release, 0, 0.0, RELEASE_AT, HELD, "armed")
    release.annotate(
        "",
        xy=(RELEASE_AT, 0.9),
        xytext=(RELEASE_AT, 0.3),
        arrowprops={"arrowstyle": "->", "color": "black"},
    )
    release.text(
        RELEASE_AT + 0.08,
        0.6,
        "one wait pass: the parent is gone, so it releases and exits 0, printing nothing",
        fontsize=8.5,
        va="center",
    )
    release.text(
        RELEASE_AT + 0.05,
        1.3,
        "lock free",
        fontsize=8.5,
        color=HELD,
        va="center",
    )

    _panel(defect, "(b) with the consumer check deleted: the orphan keeps the lock")
    _lane(defect, 2, 0.0, KILL_AT, GONE, "alive")
    _lane(defect, 1, 0.0, CONTROL_END, DENIED, "still held")
    _lane(defect, 0, 0.0, CONTROL_END, DENIED, "still running, 10 s after the kill")
    defect.text(
        CONTROL_END,
        len(LANES) + 0.25,
        "no successor can take the registration; every dispatch is refused watcher-required",
        fontsize=8.5,
        ha="right",
        va="center",
        color=DENIED,
    )

    for axis in (release, defect):
        axis.set_yticks([])
        for side in ("top", "right", "left"):
            axis.spines[side].set_visible(False)
    defect.set_xlabel("time from the arm (s, mechanism scale)", fontsize=9)

    figure.suptitle(
        "A follower outlives the process it reports to only while it holds the lock",
        fontsize=12,
    )
    figure.tight_layout(rect=(0, 0, 1, 0.96))
    out = Path(__file__).with_suffix(".png")
    figure.savefig(out, dpi=150)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
