"""Draw the pre-dispatch skill-load baseline chart from the baseline log.

Reads the log's per-session block so the chart cannot drift from the
measurement it illustrates.

    python3 docs/research/data/crew-contract-baseline/figure.py <log> <png>
"""

from __future__ import annotations

import re
import statistics
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

SESSION_RE = re.compile(
    r"^\s+([0-9a-f-]{36})\s+total=\s*(\d+)\s+loads=(\d+)\s+\[.*?\](.*)$"
)


def parse(log_path):
    rows = []
    with open(log_path, errors="ignore") as handle:
        for line in handle:
            match = SESSION_RE.match(line)
            if match:
                sid, total, loads, tail = match.groups()
                rows.append(
                    {
                        "session": sid[:8],
                        "total": int(total),
                        "loads": int(loads),
                        "partial": "PARTIAL" in tail,
                    }
                )
    return rows


def main(argv):
    rows = parse(argv[1] if len(argv) > 1 else "baseline.log")
    rows.sort(key=lambda r: -r["total"])
    complete = [r["total"] for r in rows if not r["partial"]]
    median = statistics.median(complete)

    labels = [r["session"] for r in rows]
    values = [r["total"] for r in rows]
    colors = ["#c0c0c0" if r["partial"] else "#3b6ea5" for r in rows]

    fig, ax = plt.subplots(figsize=(9, 5.2))
    bars = ax.bar(labels, values, color=colors, width=0.62)
    ax.axhline(
        median,
        color="#b03030",
        linestyle="--",
        linewidth=1.4,
        label=f"median of complete sessions: {median:,.0f}",
    )
    for bar, row in zip(bars, rows, strict=True):
        if row["partial"] and row["total"] == 0:
            ax.annotate(
                "unmeasured",
                (bar.get_x() + bar.get_width() / 2, 0),
                textcoords="offset points",
                xytext=(0, 6),
                ha="center",
                rotation=90,
                fontsize=8,
                color="#606060",
            )
            continue
        ax.annotate(
            f"{row['total']:,}",
            (bar.get_x() + bar.get_width() / 2, row["total"]),
            textcoords="offset points",
            xytext=(0, 4),
            ha="center",
            fontsize=9,
        )
    ax.set_ylabel("input tokens")
    ax.set_xlabel("coordinator session")
    ax.set_title(
        "Input tokens spent loading the reckon-build skill and its references\n"
        "before the first `reckon crew dispatch`"
    )
    ax.legend(frameon=False, loc="upper right")
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    out = argv[2] if len(argv) > 2 else "baseline-per-session.png"
    fig.savefig(out, dpi=140)
    print(f"wrote {out}")
    print(f"complete sessions {len(complete)}  median {median:.1f}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
