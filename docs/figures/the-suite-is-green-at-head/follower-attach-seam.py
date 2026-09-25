"""Draw how a follower's attach offset decides which records it can read.

A follower sets its read offset to the stream's length at the instant it
attaches, so a record already written sits behind that offset and is never
rendered. Two timelines: appending an event and hoping it lands after the
attach (top), and probing with throwaway records until one is rendered before
seeding the records under measurement (bottom).
"""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

INK = "#1b1b1f"
LOST = "#b4453c"
HELD = "#2f7d4f"
ATTACH = "#3a6ea5"


def _timeline(ax, title: str) -> None:
    ax.set_title(title, fontsize=11, loc="left", color=INK)
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 3)
    ax.axis("off")
    ax.annotate(
        "",
        xy=(9.8, 0.6),
        xytext=(0.2, 0.6),
        arrowprops={"arrowstyle": "-|>", "color": INK, "lw": 1.2},
    )
    ax.text(9.9, 0.6, "time", fontsize=8, color=INK, va="center")


def _attach(ax, x: float, label: str) -> None:
    ax.plot([x, x], [0.6, 2.7], color=ATTACH, lw=1.6, ls="--")
    ax.text(x, 2.75, label, fontsize=8, color=ATTACH, ha="center")


def _record(ax, x: float, label: str, color: str) -> None:
    ax.plot([x], [1.5], marker="o", ms=7, color=color)
    ax.text(x, 1.9, label, fontsize=8, color=color, ha="center")


def main() -> None:
    fig, (top, bottom) = plt.subplots(2, 1, figsize=(9.0, 5.0))
    fig.suptitle(
        "A follower reads only what lands after its attach offset",
        fontsize=12,
        color=INK,
        x=0.02,
        ha="left",
    )

    _timeline(top, "The base form: one append, racing the attach")
    _record(top, 3.0, "event-0 appended", LOST)
    _attach(top, 4.3, "follower attaches:\noffset := stream length")
    top.annotate(
        "event-0 is behind the offset — never rendered",
        xy=(3.0, 1.5),
        xytext=(5.2, 1.1),
        fontsize=8,
        color=LOST,
        arrowprops={"arrowstyle": "->", "color": LOST, "lw": 1.0},
    )

    _timeline(bottom, "The repaired form: probe until one is rendered, then seed")
    _record(bottom, 2.2, "probe-1", LOST)
    _attach(bottom, 3.6, "follower attaches:\noffset := stream length")
    _record(bottom, 5.0, "probe-2 rendered\n— attach proven", HELD)
    for i, x in enumerate((6.4, 7.0, 7.6, 8.2), start=1):
        _record(bottom, x, f"e{i}" if i < 4 else "e…", HELD)
    bottom.text(
        6.4,
        0.25,
        "measured transitions seeded after the attach are all read",
        fontsize=8,
        color=HELD,
    )

    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig.savefig(
        "docs/figures/the-suite-is-green-at-head/follower-attach-seam.png",
        dpi=180,
        facecolor="white",
    )


if __name__ == "__main__":
    main()
