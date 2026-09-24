"""Draw the read-only follower's owner check, and what the early return skips.

Left panel: measured seconds from the owner's death to the follower leaving the
process table, three samples each for a read-only follower and for the holder of
the session's registration, against the gate's five-second bound.

Right panel: the wait-pass decision the two followers take. The holder always
reached the owner check; the read-only follower returned before it, because the
registration it did not hold stood in for the owner it did have.
"""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt

# Measured on this login node by driving the real follower command under a
# temporary configuration home: two owner stubs arm two followers for one
# session, the read-only follower's owner is killed while the holder's owner
# stays alive, and the process table is sampled until the follower is gone.
READ_ONLY_SAMPLES = [0.811, 0.839, 0.905]
HOLDER_SAMPLES = [0.185, 0.242, 0.185]
GATE_BOUND_SECONDS = 5.0

FIXED = "#1f77b4"
MUTATED = "#c44e52"
INK = "#222222"
MUTED_INK = "#666666"


def left_panel(ax) -> None:
    rows = [
        ("read-only follower", READ_ONLY_SAMPLES, FIXED),
        ("holder", HOLDER_SAMPLES, "#4c9f70"),
    ]
    for index, (label, samples, colour) in enumerate(rows):
        y = index
        for sample in samples:
            ax.plot(
                [0, sample],
                [y, y],
                color=colour,
                linewidth=7,
                alpha=0.85,
                solid_capstyle="butt",
            )
        ax.text(
            max(samples) + 0.12,
            y,
            f"{max(samples):.2f} s max",
            va="center",
            fontsize=9,
            color=INK,
        )
        ax.text(-0.12, y, label, va="center", ha="right", fontsize=10, color=INK)
    ax.axvline(GATE_BOUND_SECONDS, color=MUTED_INK, linestyle="--", linewidth=1.2)
    ax.text(
        GATE_BOUND_SECONDS - 0.06,
        1.55,
        "gate bound 5.0 s",
        ha="right",
        fontsize=9,
        color=MUTED_INK,
    )
    ax.set_xlim(-2.6, 6.2)
    ax.set_ylim(-0.6, 1.9)
    ax.set_yticks([])
    ax.set_xlabel("seconds from the owner's death to the follower leaving")
    ax.set_title("Both leave on the next wait pass (landed)", fontsize=11, color=INK)
    for spine in ("top", "right", "left"):
        ax.spines[spine].set_visible(False)


def right_panel(ax) -> None:
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 10)
    ax.axis("off")

    def tick(y: float) -> None:
        ax.text(0.4, y, "wait-pass tick", fontsize=9, color=INK, va="center")
        ax.annotate(
            "",
            xy=(2.6, y),
            xytext=(1.95, y),
            arrowprops={"arrowstyle": "-|>", "color": INK, "linewidth": 1.2},
        )
        ax.text(2.75, y, "owner alive?", fontsize=9, color=INK, va="center")

    # Holder: no branch stands in front of the check.
    y_holder = 7.9
    tick(y_holder)
    ax.annotate(
        "",
        xy=(6.9, y_holder),
        xytext=(5.55, y_holder),
        arrowprops={"arrowstyle": "-|>", "color": "#4c9f70", "linewidth": 1.4},
    )
    ax.text(7.0, y_holder, "check\n→ leave", fontsize=9, color="#2f6b4a", va="center")
    ax.text(0.4, y_holder + 0.85, "holder", fontsize=10, color="#2f6b4a")

    # Read-only follower under the early return: the lock it does not hold ends
    # the pass before the owner question is ever asked.
    y_reader = 4.2
    tick(y_reader)
    ax.annotate(
        "",
        xy=(4.1, y_reader + 0.75),
        xytext=(3.4, y_reader + 0.1),
        arrowprops={"arrowstyle": "-|>", "color": MUTATED, "linewidth": 1.4},
    )
    ax.text(
        4.2, y_reader + 1.0, "not held → return", fontsize=9, color=MUTATED, va="center"
    )
    ax.annotate(
        "",
        xy=(2.2, y_reader - 0.85),
        xytext=(2.2, y_reader - 0.15),
        arrowprops={"arrowstyle": "-|>", "color": MUTATED, "linewidth": 1.4},
    )
    ax.text(
        2.35,
        y_reader - 1.15,
        "next tick, forever",
        fontsize=9,
        color=MUTATED,
        va="center",
    )
    ax.text(0.4, y_reader + 0.85, "read-only follower", fontsize=10, color=INK)

    ax.text(
        5.1,
        y_reader - 0.35,
        "the owner is never\nasked about",
        fontsize=9,
        color=MUTED_INK,
        va="center",
    )
    ax.plot(
        [3.55, 5.0],
        [y_reader - 0.55, y_reader - 0.55],
        color=MUTED_INK,
        linestyle=":",
        linewidth=1.0,
    )

    ax.set_title("What the early return skips", fontsize=11, color=INK)


def main() -> None:
    fig, (left, right) = plt.subplots(
        1, 2, figsize=(12.4, 4.6), gridspec_kw={"width_ratios": [1.15, 1.0]}
    )
    left_panel(left)
    right_panel(right)
    fig.suptitle(
        "A read-only follower checks its owner on every wait pass, lock or no lock",
        fontsize=12.5,
        color=INK,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    out = (
        __file__.removesuffix("owner_check_independent_of_the_lock.py")
        + "owner_check_independent_of_the_lock.png"
    )
    fig.savefig(out, dpi=160)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
