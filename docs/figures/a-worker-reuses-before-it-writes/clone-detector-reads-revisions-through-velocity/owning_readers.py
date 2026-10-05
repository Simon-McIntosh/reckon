"""Render the before/after ownership of the clone detector's revision readers.

Left panel: the detector read revisions through its own archive reader, its own
git runner and its own cache resolver, beside the counter's separate ls-tree and
blob list. Right panel: one tree lister takes a prefix and a suffix, one public
sources reader composes it with the batched blob reader, and both consumers —
and ``_plan_documents`` — reach that one reader.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

plt.style.use("data-ink")

DUPLICATE = "#b0564f"   # a private copy that this node retires
OWNER = "#2f7d78"       # the one owner the work routes through
CONSUMER = "#4a4a4a"    # a caller
NEUTRAL = "#9a9a9a"

FIG_W, FIG_H = 14.0, 7.4


def _box(ax, x, y, w, h, text, color, *, linestyle="-", fontsize=15):
    ax.add_patch(
        FancyBboxPatch(
            (x, y),
            w,
            h,
            boxstyle="round,pad=0.02,rounding_size=0.04",
            linewidth=1.6 if linestyle == "-" else 1.2,
            edgecolor=color,
            linestyle=linestyle,
            facecolor="white",
        )
    )
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center",
            fontsize=fontsize, color=color, linespacing=1.35)


def _arrow(ax, p, q, color=NEUTRAL):
    ax.add_patch(
        FancyArrowPatch(p, q, arrowstyle="-|>", mutation_scale=16,
                        linewidth=1.4, color=color, shrinkA=2, shrinkB=2)
    )


def _panel(ax, title):
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 10)
    ax.axis("off")
    ax.text(5, 9.6, title, ha="center", va="center", fontsize=20, color="#222222")


def render(out: Path) -> None:
    fig, (left, right) = plt.subplots(1, 2, figsize=(FIG_W, FIG_H))
    _panel(left, "before")
    _panel(right, "after")

    # ---- before: two readers, private copies beside velocity's owners ----
    _box(left, 0.4, 8.2, 4.3, 0.9, "clone detector\n(clones.py)", CONSUMER)
    _box(left, 5.3, 8.2, 4.3, 0.9, "interface counter\n(interface_counts.py)", CONSUMER)
    _box(left, 0.4, 6.3, 4.3, 0.9, "revised_python\ngit archive + tarfile", DUPLICATE)
    _box(left, 0.4, 5.0, 4.3, 0.9, "_git private runner", DUPLICATE)
    _box(left, 0.4, 3.7, 4.3, 0.9, "_cache_root private", DUPLICATE)
    _box(left, 5.3, 6.3, 4.3, 0.9, "read_trees\nls-tree + read_blobs", DUPLICATE, linestyle="--")
    _arrow(left, (2.55, 8.2), (2.55, 7.2), DUPLICATE)
    _arrow(left, (2.55, 6.3), (2.55, 5.9), DUPLICATE)
    _arrow(left, (2.55, 5.0), (2.55, 4.6), DUPLICATE)
    _arrow(left, (7.45, 8.2), (7.45, 7.2), DUPLICATE)
    left.text(5.0, 2.6, "two revision readers over one tree",
             ha="center", va="center", fontsize=15, color=DUPLICATE)

    # ---- after: one reader, one runner, one cascade ----
    _box(right, 0.4, 8.2, 4.3, 0.9, "clone detector\n(clones.py)", CONSUMER)
    _box(right, 5.3, 8.2, 4.3, 0.9, "interface counter\n(interface_counts.py)", CONSUMER)
    _box(right, 0.4, 6.6, 4.3, 0.95, "read_sources(prefix='')", OWNER)
    _box(right, 5.3, 6.6, 4.3, 0.95, "read_sources(prefix='reckon')", OWNER)
    _box(right, 2.85, 4.6, 4.3, 1.0, "_tree_paths\nls-tree, prefix + suffix", OWNER)
    _box(right, 2.85, 3.0, 4.3, 1.0, "read_blobs\ncat-file --batch", OWNER)
    _box(right, 0.4, 1.1, 4.3, 1.0, "run_git\none process runner", OWNER)
    _box(right, 5.3, 1.1, 4.3, 1.0, "_store.cache_root\none cascade", OWNER)
    _arrow(right, (2.55, 8.2), (2.55, 7.55), CONSUMER)
    _arrow(right, (7.45, 8.2), (7.45, 7.55), CONSUMER)
    _arrow(right, (2.55, 6.6), (3.7, 5.6), OWNER)
    _arrow(right, (7.45, 6.6), (6.3, 5.6), OWNER)
    _arrow(right, (5.0, 4.6), (5.0, 4.0), OWNER)
    right.text(5.0, 0.35, "one revision reader; one runner; one cascade",
               ha="center", va="center", fontsize=15, color=OWNER)

    fig.subplots_adjust(left=0.02, right=0.98, top=0.98, bottom=0.02, wspace=0.06)
    fig.savefig(out, dpi=100)
    plt.close(fig)


if __name__ == "__main__":
    render(Path(__file__).with_name("owning_readers.png"))
