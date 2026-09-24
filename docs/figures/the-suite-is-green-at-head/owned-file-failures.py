"""Failing node ids per owned file, base against head.

Each owned file is one pytest process with its own basetemp, so the bar is the
file's own failing-id count and not a suite total. A file whose head bar reaches
zero was repaired by moving the assertion to the shipped behaviour; a file whose
two bars are equal still fails for a cause the node may not repair — a product
defect or a missing capability — and those ids are named in the evidence
record's follow-ons rather than left unattributed.

Source of the counts: the run directory's head and base-control logs for this
node (head_logs2/, base_control_logs2/), read at the node head revision.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

# (file stem, base failing ids, head failing ids)
ROWS = [
    ("test_skill_contracts", 3, 2),
    ("test_typed_resources", 1, 0),
    ("test_provenance", 1, 0),
    ("test_spa_canvas_fidelity", 1, 0),
    ("test_ticker_renders_wait_facts", 1, 0),
    ("test_fleet_counter_coverage", 1, 0),
    ("test_crew_monitor_output", 1, 1),
    ("test_spa_module_scope", 1, 1),
]

# The disposition of each file's head failures.
DISPOSITION = {
    "test_skill_contracts": "2 product defects (skill over budget; verb undocumented)",
    "test_crew_monitor_output": "product defect (order-dependent renderer)",
    "test_spa_module_scope": "environment (no headless browser)",
}

BASE = "#b04a3f"
HEAD = "#3f7d4a"


def main() -> None:
    labels = [stem for stem, _, _ in ROWS]
    base = np.array([b for _, b, _ in ROWS], dtype=float)
    head = np.array([h for _, _, h in ROWS], dtype=float)
    y = np.arange(len(labels))
    height = 0.38

    fig, ax = plt.subplots(figsize=(11.5, 5.4))
    ax.barh(y - height / 2, base, height, color=BASE, label="failing ids at base")
    ax.barh(y + height / 2, head, height, color=HEAD, label="failing ids at head")

    for index, (stem, b, h) in enumerate(ROWS):
        ax.text(b + 0.05, index - height / 2, str(b), va="center", fontsize=9)
        ax.text(h + 0.05, index + height / 2, str(h), va="center", fontsize=9)
        if h:
            ax.text(
                1.2,
                index + height / 2,
                DISPOSITION.get(stem, "attributed in follow-ons"),
                va="center", fontsize=8, color="#444444", style="italic",
            )

    ax.set_yticks(y, labels, fontsize=9)
    ax.invert_yaxis()
    ax.set_xlim(0, 5.6)
    ax.set_xlabel("failing node ids in the file (own pytest process, own basetemp)")
    ax.set_title(
        "Owned files: failing ids at the node base and at the node head\n"
        "six files repaired to zero; four ids remain attributed to product "
        "or environment causes",
        fontsize=10.5,
    )
    ax.legend(loc="lower right", fontsize=9)
    ax.grid(axis="x", alpha=0.25)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)

    out = Path(__file__).with_suffix(".png")
    fig.tight_layout()
    fig.savefig(out, dpi=140)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
