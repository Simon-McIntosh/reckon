"""Draw the two-writer schedule the ledger write test forces, either side of the lock.

Each pane is one timeline of the same forced interleaving: writer A is held at
the seam between its per-run file write and its envelope write, and writer B
writes during A's pause. The left pane is the writer without a lock, where B
reads the project mid-write and is answered with the conflict-of-history
refusal; the right pane is the writer under the lock, where B is refused on the
version before it writes anything.
"""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch

A = "#1f4e79"
B = "#8c4a13"
BAD = "#a11b1b"
GOOD = "#1c6b3c"


def pane(ax, title: str, steps: list[tuple[str, str, str]], verdict: str) -> None:
    ax.set_title(title, fontsize=12, fontweight="bold", loc="left", pad=14)
    ax.set_xlim(0, 10)
    ax.set_ylim(-0.4, len(steps) + 1.4)
    ax.axis("off")
    for index, (who, text, colour) in enumerate(steps):
        y = len(steps) - index
        ax.add_patch(
            FancyBboxPatch(
                (1.0, y - 0.34),
                8.2,
                0.68,
                boxstyle="round,pad=0.06",
                linewidth=1.4,
                edgecolor=colour,
                facecolor="#f7f7f5",
            )
        )
        ax.text(0.75, y, who, ha="right", va="center", fontsize=10,
                fontweight="bold", color=colour)
        ax.text(1.25, y, text, ha="left", va="center", fontsize=10)
    ax.text(5.0, -0.05, verdict, ha="center", va="top", fontsize=10.5,
            fontweight="bold", color=GOOD if verdict.startswith("reads") else BAD)


def main() -> None:
    fig, (left, right) = plt.subplots(1, 2, figsize=(15.5, 6.4))

    pane(
        left,
        "Without a lock across the write",
        [
            ("A", "reads the ledger at version 1", A),
            ("A", "rewrites the run file for its edited row", A),
            ("A", "held at the seam: file written, envelope not yet", A),
            ("B", "reads mid-write: the two copies disagree", B),
            ("B", "refused as conflicting history, before any version check", BAD),
            ("B", "wrote nothing", B),
            ("A", "resumes and commits at version 1", A),
        ],
        "no version conflict is raised: the arriving writer is told the ledger is corrupt",
    )

    pane(
        right,
        "With one lock held across the write",
        [
            ("A", "takes the project's write lock", A),
            ("A", "reads the ledger at version 1", A),
            ("A", "rewrites the run file for its edited row", A),
            ("A", "held at the seam, still holding the lock", A),
            ("B", "blocks on the lock; it cannot write mid-write", B),
            ("A", "writes its envelope at version 1 and commits", A),
            ("A", "releases the lock", A),
            ("B", "takes the released lock and is refused on version 1", B),
            ("B", "returns the version conflict having written nothing", B),
        ],
        "reads: exactly one writer is refused, on the version",
    )

    fig.suptitle(
        "Two writers prepared from one revision, forced into the same interleaving",
        fontsize=13,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.savefig("two-writers-interleave.svg", bbox_inches="tight", transparent=True)
    fig.savefig("two-writers-interleave.png", bbox_inches="tight", dpi=150)
    print("wrote two-writers-interleave.svg and .png")


if __name__ == "__main__":
    main()