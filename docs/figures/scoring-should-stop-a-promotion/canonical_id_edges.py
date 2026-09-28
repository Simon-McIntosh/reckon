"""Which spellings the reduction touches, and what each then compares as.

Three rows pass through one rule. A spelling written from a working directory —
absolute, or opening with ``.`` or ``..`` — is reduced onto its last ``tests``
component; a plain relative spelling is kept whole, because a component above
the anchor that belongs to the path is not the working directory's
contribution. Row 1 is one test under two spellings and must reduce to one id;
row 2 is a different test and must stay a different id; row 3 is retirement
prose naming an id under a shifted spelling, which must match the canonical id.
Measured: before the fix, row 2's failure is not counted against row 1 (count 0
where 1 is required), and row 3's total is capped at 5 where it must stay 90.
"""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt

ROWS = (
    (
        "two spellings of one test",
        (
            "tests/x.py::t                        (base arm, at the repository root)\n"
            "../../home/…/added/tests/x.py::t     (head arm, run from elsewhere)"
        ),
        "one id   tests/x.py::t",
        "#1b2a4a",
        "count 0, total uncapped",
    ),
    (
        "a package's own test directory",
        "pkg/tests/x.py::t                    (plain relative, kept whole)",
        "a second id   pkg/tests/x.py::t",
        "#8a3b12",
        "count 1, the added failure is reported",
    ),
    (
        "retirement prose, spelled as a gate log printed it",
        "…green apart from ../../home/…/added/tests/x.py::t, retired by name",
        "matches the canonical id",
        "#2f6b3f",
        "retired; total stays 90, not capped at 5",
    ),
)

RULE = (
    "Rule: absolute, or opening with '.' or '..' → reduced onto the last tests "
    "component; a plain relative spelling → kept whole."
)
NOTE = (
    "A retained working-directory prefix is what merged row 2 into row 1; both sides of "
    "the retirement match now reduce the same way."
)


def main() -> None:
    fig, ax = plt.subplots(figsize=(14.5, 6.2))
    ax.set_axis_off()
    ax.text(
        0.0,
        1.10,
        "Which spellings the reduction touches, and what each then compares as",
        clip_on=False,
        fontsize=13.5,
        weight="bold",
        color="#1b2a4a",
        ha="left",
        va="bottom",
    )

    left_x, left_w = 0.0, 0.52
    mid_x, mid_w = 0.62, 0.38
    top = 0.78
    row_h = 0.17

    ax.text(
        left_x,
        top + row_h + 0.05,
        "spelling the log or the manifest carries",
        fontsize=10.5,
        weight="bold",
        color="#444",
    )
    ax.text(
        mid_x,
        top + row_h + 0.05,
        "form the comparison uses",
        fontsize=10.5,
        weight="bold",
        color="#444",
    )

    for index, (label, spelling, outcome, colour, consequence) in enumerate(ROWS):
        y = top - index * (row_h + 0.10)
        ax.text(
            left_x,
            y + row_h + 0.018,
            label,
            fontsize=10.5,
            weight="bold",
            color="#444",
        )
        ax.add_patch(
            plt.Rectangle(
                (left_x, y),
                left_w,
                row_h,
                fill=True,
                facecolor="#f4f6fa",
                edgecolor=colour,
                linewidth=1.1,
            )
        )
        ax.text(
            left_x + 0.012,
            y + row_h / 2,
            spelling,
            ha="left",
            va="center",
            fontsize=9.0,
            family="monospace",
            color=colour,
        )
        ax.annotate(
            "",
            xy=(mid_x - 0.015, y + row_h / 2),
            xytext=(left_x + left_w + 0.02, y + row_h / 2),
            arrowprops={"arrowstyle": "-|>", "color": "#3c5a99", "linewidth": 1.4},
        )
        ax.add_patch(
            plt.Rectangle(
                (mid_x, y),
                mid_w,
                row_h,
                fill=True,
                facecolor="#eef3fb",
                edgecolor="#3c5a99",
                linewidth=1.2,
            )
        )
        ax.text(
            mid_x + 0.014,
            y + row_h * 0.62,
            outcome,
            ha="left",
            va="center",
            fontsize=10.0,
            family="monospace",
            color="#1b2a4a",
        )
        ax.text(
            mid_x + 0.014,
            y + row_h * 0.24,
            consequence,
            ha="left",
            va="center",
            fontsize=9.0,
            color=colour,
        )

    ax.text(0.0, 0.10, RULE, fontsize=10.5, color="#1b2a4a", ha="left", va="top")
    ax.text(0.0, 0.025, NOTE, fontsize=10.5, color="#2f6b3f", ha="left", va="top")

    fig.savefig("canonical_id_edges.png", dpi=170, bbox_inches="tight")
    print("wrote canonical_id_edges.png")


if __name__ == "__main__":
    main()
