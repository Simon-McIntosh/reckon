"""The failed-launch test's assertion before and after the launcher wrap.

Left: the test asked for OSError while the launcher wrap had already converted
it to CrewError, so the assertion missed. Right: the test asks for the refusal
and pins the conversion by its cause, so an unwrapped launcher fails the test.
"""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

RED = "#c0392b"
GREEN = "#1e8449"
GREY = "#566573"
INK = "#212f3d"

fig, axes = plt.subplots(1, 2, figsize=(12.0, 4.6))
fig.suptitle(
    "The failed-launch assertion now pins the refusal and its cause",
    fontsize=13,
    color=INK,
    fontweight="bold",
)


def box(ax, x, y, w, h, text, color, *, fs=9.5, fc="white"):
    ax.add_patch(
        plt.Rectangle(
            (x, y), w, h, facecolor=fc, edgecolor=color, linewidth=1.8, zorder=2
        )
    )
    ax.text(
        x + w / 2,
        y + h / 2,
        text,
        ha="center",
        va="center",
        fontsize=fs,
        color=INK,
        zorder=3,
        wrap=True,
    )


def arrow(ax, x0, y0, x1, y1, color, **kw):
    ax.annotate(
        "",
        xy=(x1, y1),
        xytext=(x0, y0),
        arrowprops=dict(arrowstyle="-|>", color=color, lw=1.8, **kw),
        zorder=1,
    )


# ---- left panel: before, the assertion misses ----
ax = axes[0]
ax.set_xlim(0, 10)
ax.set_ylim(0, 10)
ax.axis("off")
ax.set_title(
    "before: pytest.raises(OSError)", color=RED, fontsize=11, fontweight="bold"
)
box(ax, 0.4, 7.4, 9.2, 1.5, 'launcher raises\nOSError("no such executable")', GREY)
box(
    ax,
    0.4,
    4.7,
    9.2,
    1.6,
    "dispatch wraps the spawn ->\nCrewError (__cause__ = OSError)",
    GREY,
)
box(
    ax,
    0.4,
    1.5,
    9.2,
    1.7,
    "pytest.raises(OSError)\nmatches nothing -> TEST FAILS",
    RED,
    fc="#fdecea",
    fs=10,
)
arrow(ax, 5.0, 7.4, 5.0, 6.3, GREY)
arrow(ax, 3.0, 4.7, 3.0, 3.2, GREY)
ax.text(4.6, 3.85, "CrewError is not OSError", color=RED, fontsize=9, style="italic")

# ---- right panel: after, the assertion pins the cause ----
ax = axes[1]
ax.set_xlim(0, 10)
ax.set_ylim(0, 10)
ax.axis("off")
ax.set_title(
    "after: pytest.raises(crew.CrewError) + __cause__",
    color=GREEN,
    fontsize=11,
    fontweight="bold",
)
box(ax, 0.4, 7.4, 9.2, 1.5, 'launcher raises\nOSError("no such executable")', GREY)
box(
    ax,
    0.4,
    4.7,
    9.2,
    1.6,
    "dispatch wraps the spawn ->\nCrewError (__cause__ = OSError)",
    GREY,
)
box(
    ax,
    0.4,
    1.5,
    9.2,
    2.4,
    "pytest.raises(crew.CrewError) matches;\n"
    '__cause__ is OSError("no such executable")\n-> TEST PASSES',
    GREEN,
    fc="#eafaf1",
    fs=10,
)
arrow(ax, 5.0, 7.4, 5.0, 6.3, GREY)
arrow(ax, 5.0, 4.7, 5.0, 3.9, GREEN)
ax.text(
    5.0,
    0.55,
    "unwrap the launcher call and the OSError propagates -> the test fails (negative control)",
    ha="center",
    color=INK,
    fontsize=8.5,
    style="italic",
)

fig.tight_layout(rect=(0, 0, 1, 0.94))
out = (
    "docs/figures/the-suite-is-green-at-head/failed-launch-assertion-pins-the-cause.png"
)
fig.savefig(out, dpi=140)
print("wrote", out)
