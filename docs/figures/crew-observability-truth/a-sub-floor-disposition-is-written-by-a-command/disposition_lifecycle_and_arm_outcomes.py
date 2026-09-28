"""Draw the disposition lifecycle beside the three measured arms.

Left panel: where an obligation row goes when the command accepts or refuses.
Right panel: the same gate run against the given base, the change, and a scratch
copy whose read-back no longer checks the kind.

Run: <repo>/.venv/bin/python disposition_lifecycle_and_arm_outcomes.py
"""

from __future__ import annotations

import pathlib

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent

ACCEPT = "#d8ecd8"
REFUSE = "#f6d8d5"
ROW = "#dbe6f4"
INK = "#22303c"


def _box(ax, x, y, w, h, text, *, face, size=9.0, weight="normal"):
    ax.add_patch(
        FancyBboxPatch(
            (x, y),
            w,
            h,
            boxstyle="round,pad=0.012,rounding_size=0.02",
            linewidth=0.9,
            edgecolor=INK,
            facecolor=face,
        )
    )
    ax.text(
        x + w / 2,
        y + h / 2,
        text,
        ha="center",
        va="center",
        fontsize=size,
        color=INK,
        weight=weight,
        linespacing=1.35,
    )


def _arrow(ax, start, end, *, label="", rad=0.0, size=8.5):
    ax.add_patch(
        FancyArrowPatch(
            start,
            end,
            arrowstyle="-|>",
            mutation_scale=11,
            linewidth=1.0,
            color=INK,
            connectionstyle=f"arc3,rad={rad}",
        )
    )
    if label:
        ax.text(
            (start[0] + end[0]) / 2,
            (start[1] + end[1]) / 2,
            label,
            ha="center",
            va="center",
            fontsize=size,
            color=INK,
            backgroundcolor="white",
        )


def draw_lifecycle(ax) -> None:
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 10)
    ax.axis("off")
    ax.set_title(
        "One sub-floor dimension: the row it stands, and where the command sends it",
        fontsize=10.5,
        color=INK,
        pad=8,
    )

    _box(
        ax,
        1.9,
        8.35,
        6.2,
        1.5,
        "stored review — durability 5 against a declared floor of 10\n"
        "total 79/100: the review passes overall,\n"
        "so only the row carries the finding",
        face=ROW,
        size=8.6,
    )
    _box(
        ax,
        3.05,
        6.95,
        3.9,
        0.85,
        "obligation row stands\nsub-floor: durability",
        face=ROW,
        weight="bold",
    )
    _arrow(ax, (5.0, 8.55), (5.0, 7.8))

    ax.text(
        5.0,
        6.42,
        "reckon crew dispose --project <p> --run <r> --dimension durability --kind <k>",
        ha="center",
        va="center",
        fontsize=8.6,
        color=INK,
        family="monospace",
    )
    _arrow(ax, (5.0, 6.95), (5.0, 6.0))

    _arrow(ax, (4.35, 5.95), (2.5, 4.55), rad=0.12)
    _arrow(ax, (5.65, 5.95), (7.5, 4.55), rad=-0.12)
    ax.text(1.35, 5.62, "accepted: exit 0", fontsize=9, color=INK, weight="bold")
    ax.text(8.65, 5.62, "refused: exit 1", fontsize=9, color=INK, weight="bold", ha="right")

    _box(ax, 0.35, 3.25, 3.1, 1.2, "--kind folded\n--node <id>\n\nrow retired", face=ACCEPT)
    _box(ax, 3.7, 3.25, 2.6, 1.2, "--kind exempted\n--reason <text>\n\nrow retired", face=ACCEPT)

    _box(
        ax,
        6.7,
        2.35,
        3.05,
        2.1,
        "unknown dimension\n"
        "unknown kind\n"
        "fold naming no node\n"
        "exemption with no reason\n"
        "\n"
        "nothing written;\nrow stands",
        face=REFUSE,
        size=8.4,
    )
    ax.text(
        5.0,
        1.35,
        "the row is the reading a coordinator's obligations list makes of the store:\n"
        "an accepted disposition retires it, a refusal leaves it exactly where it was",
        ha="center",
        va="center",
        fontsize=8.6,
        color=INK,
        style="italic",
    )


def draw_arms(ax) -> None:
    arms = [
        ("base 5636e306\nno subcommand exists", 0, 8, "#c9c9c9"),
        ("change 857e514c\ncrew dispose", 48, 0, "#8fbf8f"),
        ("scratch copy, kind check removed", 46, 2, "#d98c85"),
    ]
    ys = [2.0, 1.0, 0.0]
    for (label, passed, failed, colour), y in zip(arms, ys, strict=True):
        ax.barh(y, passed, color=colour, height=0.5, edgecolor=INK, linewidth=0.8)
        if failed:
            ax.barh(
                y,
                failed,
                left=passed,
                color="#b23b30",
                height=0.5,
                edgecolor=INK,
                linewidth=0.8,
            )
        ax.text(-1.5, y, label, ha="right", va="center", fontsize=9, color=INK)
        ax.text(
            passed + failed + 1.5,
            y,
            f"{passed} passed, {failed} failed",
            ha="left",
            va="center",
            fontsize=9,
            color=INK,
        )

    ax.annotate(
        "the 8 command cases:\nno such command 'dispose'",
        xy=(9, 2.25),
        xytext=(14, 2.85),
        fontsize=8.4,
        color=INK,
        arrowprops={"arrowstyle": "->", "color": INK, "linewidth": 0.9},
    )
    ax.annotate(
        "unknown-kind refusal and\nread-back of a stored unknown kind",
        xy=(48.6, 0.0),
        xytext=(20, -0.5),
        fontsize=8.4,
        color=INK,
        arrowprops={"arrowstyle": "->", "color": INK, "linewidth": 0.9},
    )

    ax.set_xlim(0, 66)
    ax.set_ylim(-1.0, 3.1)
    ax.set_yticks([])
    ax.set_xlabel("cases in the gate: the new module's 10 plus 38 across the existing dispositions and obligations modules", fontsize=8.8)
    ax.set_title(
        "The gate run at three revisions (each arm its own log, EXIT= line last)",
        fontsize=10.5,
        color=INK,
        pad=8,
    )
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)


def main() -> None:
    fig, (left, right) = plt.subplots(1, 2, figsize=(15.5, 7.2), dpi=150)
    draw_lifecycle(left)
    draw_arms(right)
    fig.tight_layout(pad=2.0)
    out = HERE / "disposition_lifecycle_and_arm_outcomes.png"
    fig.savefig(out, facecolor="white")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()