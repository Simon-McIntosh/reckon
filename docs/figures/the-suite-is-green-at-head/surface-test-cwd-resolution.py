"""Draw how a ``-c`` child picks its ``reckon`` tree, before and after the fix.

Both panels show the same search order a ``python -c`` child walks: the empty
entry its working directory contributes, then PYTHONPATH, then site-packages.
The first entry that holds a ``reckon`` package wins, and the panel's caption
names the tree that resulted. Before the fix the child inherits the caller's
directory, so a launch from another checkout wins the race; after the fix the
child's directory is pinned to the checkout under test, so the tree under test
wins whatever the caller's directory is.
"""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch

ROWS = (
    "'' (child cwd)",
    "PYTHONPATH",
    "site-packages",
)


def _panel(ax, title, entries, winner, caption, winner_ok):
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 10)
    ax.axis("off")
    ax.set_title(title, fontsize=11, fontweight="bold", pad=10)
    for index, (label, detail) in enumerate(entries):
        y = 8.4 - index * 2.1
        is_winner = index == winner
        face = "#cdeccd" if (is_winner and winner_ok) else "#f4c7c3" if is_winner else "#eeeeee"
        edge = "#2e7d32" if (is_winner and winner_ok) else "#c0392b" if is_winner else "#bdbdbd"
        ax.add_patch(
            FancyBboxPatch(
                (0.4, y - 0.75),
                9.2,
                1.6,
                boxstyle="round,pad=0.12",
                linewidth=2.2 if is_winner else 1.0,
                facecolor=face,
                edgecolor=edge,
            )
        )
        ax.text(
            0.75,
            y + 0.28,
            label,
            fontsize=10,
            fontweight="bold" if is_winner else "normal",
            va="center",
        )
        ax.text(0.75, y - 0.42, detail, fontsize=8.5, color="#444444", va="center")
        if is_winner:
            ax.text(9.35, y + 0.05, "first hit", fontsize=8.5, color=edge, ha="right", va="center")
    ax.text(5.0, 0.6, caption, fontsize=9, ha="center", va="center", wrap=True)


def main() -> None:
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.6))

    _panel(
        axes[0],
        "before — the child inherits the caller's directory",
        (
            ("'' (child cwd)", "other checkout — holds its own reckon package"),
            ("PYTHONPATH", "checkout under test — reached only second"),
            ("site-packages", "installed reckon"),
        ),
        winner=0,
        caption="resolves <other-checkout>/reckon → measured against this fixture → FAIL",
        winner_ok=False,
    )
    _panel(
        axes[1],
        "after — the child's cwd is pinned to the checkout root",
        (
            ("'' (child cwd)", "checkout under test — pinned by the launch"),
            ("PYTHONPATH", "checkout under test — the same tree"),
            ("site-packages", "installed reckon"),
        ),
        winner=0,
        caption="resolves <checkout-under-test>/reckon → matches this fixture → PASS",
        winner_ok=True,
    )

    fig.suptitle(
        "Which reckon a `python -c` child imports, by sys.path search order",
        fontsize=12.5,
        fontweight="bold",
    )
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig.savefig(
        "surface-test-cwd-resolution.png",
        dpi=150,
    )


if __name__ == "__main__":
    main()