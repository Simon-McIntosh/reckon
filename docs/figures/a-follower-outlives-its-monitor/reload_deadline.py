"""Draw the follower's lifetime deadline across one source-change reload.

Two panels share one axis of seconds measured from the original arm. Each panel
has two rows: the top row is the follower image that is running, the bottom row
is the lifetime deadline itself. The upper panel is the carried deadline: the
instant is fixed at the arm, the follower hands it to the replacement image in
the environment, and the arming ends at that instant however many images it has
been through. The lower panel is the defect the change removes: the replacement
re-anchors the deadline at its own start, so the six seconds run again from the
reload and the arming is still alive at the gate's eight-second bound.

The axis is mechanism time in seconds from the arm. The marks are measured with
a driver that arms the real follower over a temporary RECKON_HOME, bumps the
source stamp that triggers a clean reload, and timestamps the image replacement
and the exit. The arm itself costs about 3.5 s of interpreter and import on this
login node, which is arrival rather than the lifetime and so is not on the axis.

Run: python docs/figures/a-follower-outlives-its-monitor/reload_deadline.py
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

# Measured on this login node by the driver named in the module docstring.
RELOAD_AT = 1.03  # os.execv replaces the image
CARRIED_END = 5.08  # the carried deadline ends the arming
ARM_COST = 3.51  # interpreter + import, paid again by the replacement
RESTARTED_END = RELOAD_AT + ARM_COST + 6.0  # a fresh six seconds from the reload
GATE_BOUND = 8.0  # the gate's window, measured from the original arm
AXIS_END = 11.6  # a little past the restarted deadline, for the marker

IMAGE_OLD = "#2f6fb3"
IMAGE_NEW = "#8fb0d1"
DEADLINE = "#c0392b"
BOUND = "#5b5b5b"
ROW_IMAGE = 1.0
ROW_DEADLINE = 2.0


def _row(
    axis: plt.Axes, y: float, start: float, end: float, color: str, height: float = 0.5
) -> None:
    axis.add_patch(
        Rectangle(
            (start, y - height / 2),
            end - start,
            height,
            facecolor=color,
            edgecolor="none",
        )
    )


def _deadline(axis: plt.Axes, start: float, end: float) -> None:
    axis.annotate(
        "",
        xy=(end, ROW_DEADLINE),
        xytext=(start, ROW_DEADLINE),
        arrowprops={"arrowstyle": "-|>", "color": DEADLINE, "linewidth": 2.0},
        annotation_clip=False,
    )


def _common(axis: plt.Axes) -> None:
    axis.set_xlim(-0.4, AXIS_END + 0.6)
    axis.set_ylim(0.35, 2.75)
    axis.set_yticks([ROW_IMAGE, ROW_DEADLINE])
    axis.set_yticklabels(["image", "deadline"], fontsize=9)
    axis.spines[["top", "right", "left"]].set_visible(False)
    axis.tick_params(axis="y", length=0)
    axis.grid(axis="x", color="#eeeeee", linewidth=0.8)
    axis.set_axisbelow(True)
    axis.axvline(RELOAD_AT, color="#999999", linestyle=":", linewidth=1.2)
    axis.axvline(GATE_BOUND, color=BOUND, linestyle="--", linewidth=1.2)
    axis.text(
        RELOAD_AT,
        2.63,
        f"reload {RELOAD_AT:.1f} s",
        fontsize=8.5,
        color="#5b5b5b",
        ha="center",
    )
    axis.text(
        GATE_BOUND + 0.12,
        2.63,
        f"gate bound {GATE_BOUND:.0f} s",
        fontsize=8.5,
        color=BOUND,
        ha="left",
    )


def main() -> None:
    figure, (upper, lower) = plt.subplots(2, 1, figsize=(8.6, 4.8), sharex=True)

    for axis in (upper, lower):
        _common(axis)
        _row(axis, ROW_IMAGE, 0.0, RELOAD_AT, IMAGE_OLD)
        _row(axis, ROW_IMAGE, RELOAD_AT, AXIS_END, IMAGE_NEW)
        axis.set_xlabel("seconds from the original arm", fontsize=9)

    # Upper: one deadline, fixed at the arm and closed at the same instant
    # regardless of how many images the process has been through.
    _deadline(upper, 0.0, CARRIED_END)
    upper.text(
        0.08,
        2.17,
        "fixed at the arm, carried in the environment",
        fontsize=8.5,
        color=DEADLINE,
    )
    upper.text(
        CARRIED_END - 0.12,
        1.62,
        f"ends at {CARRIED_END:.1f} s",
        fontsize=8.5,
        color=DEADLINE,
        ha="right",
    )
    upper.set_title(
        "deadline carried across the reload — ends inside the bound",
        fontsize=10,
        loc="left",
    )

    # Lower: the replacement anchors the deadline at its own start, so a new
    # six seconds begins at the reload and the arming outlives the bound.
    _deadline(lower, RELOAD_AT, RESTARTED_END)
    lower.text(
        RELOAD_AT + 0.12,
        2.17,
        "re-anchored at the replacement's own start",
        fontsize=8.5,
        color=DEADLINE,
    )
    lower.text(
        RESTARTED_END - 0.12,
        1.62,
        f"ends at {RESTARTED_END:.1f} s",
        fontsize=8.5,
        color=DEADLINE,
        ha="right",
    )
    lower.annotate(
        "still alive here",
        xy=(GATE_BOUND, ROW_IMAGE),
        xytext=(GATE_BOUND, 0.55),
        fontsize=8.5,
        color="#444444",
        ha="center",
        arrowprops={"arrowstyle": "-|>", "color": "#444444", "linewidth": 1.1},
    )
    lower.set_title(
        "deadline re-anchored after the reload — the removed defect",
        fontsize=10,
        loc="left",
    )

    figure.suptitle(
        "A follower outlives its monitor: one arming, two outcomes", fontsize=11.5
    )
    figure.tight_layout(rect=(0, 0, 1, 0.95))
    out = Path(__file__).with_suffix(".png")
    figure.savefig(out, dpi=160)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
