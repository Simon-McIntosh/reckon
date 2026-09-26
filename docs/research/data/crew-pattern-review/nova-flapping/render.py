"""Plot the measured weekly churn beside the changing contribution mix."""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import PercentFormatter


def main():
    directory = Path(__file__).resolve().parent
    data = json.loads((directory / "flapping.json").read_text())
    weeks = [row for row in data["weekly"] if row["code_commits"]]
    plt.rcParams.update(
        {
            "font.size": 12,
            "font.family": "DejaVu Sans",
            "svg.fonttype": "none",
            "svg.hashsalt": "nova-history-churn",
            "axes.spines.top": False,
            "axes.spines.right": False,
        }
    )
    figure, axes = plt.subplots(2, 1, figsize=(10.4, 7.4), sharex=True)
    x = list(range(len(weeks)))
    for root, colour, label in (
        ("nova", "#222222", "Source"),
        ("tests", "#176c9c", "Tests"),
    ):
        mature = [row[root]["mature_deletion_share"] for row in weeks]
        observed = [row[root]["observed_deletion_share"] for row in weeks]
        axes[0].plot(x[:-1], mature[:-1], "o-", color=colour, label=label)
        axes[0].plot(x[-2:], mature[-2:], ":", color=colour)
        axes[0].plot(x[-1], mature[-1], "o", markerfacecolor="white", color=colour)
        axes[0].plot(x[-1], observed[-1], "x", color=colour)
    axes[0].set_title("Seven-day deletion share of added lines", loc="left", pad=12)
    axes[0].set_ylim(0, 0.165)
    axes[0].yaxis.set_major_formatter(PercentFormatter(1))
    axes[0].legend(frameon=False, loc="upper right", ncol=2)
    axes[0].annotate(
        "Open circles: only Sep 19 before 10:00 UTC is mature\nCrosses: all additions, incomplete follow-up",
        xy=(5, 0.12804),
        xytext=(1.4, 0.151),
        fontsize=10,
        arrowprops={"arrowstyle": "-", "color": "#555555", "linewidth": 0.7},
    )
    local = [
        row["code_commits_by_lane"].get("local", 0) / row["code_commits"]
        for row in weeks
    ]
    axes[1].plot(x, local, "o-", color="#176c9c")
    axes[1].set_title(
        "Local lane share of source/test-changing landings", loc="left", pad=12
    )
    axes[1].set_ylim(-0.025, 0.82)
    axes[1].yaxis.set_major_formatter(PercentFormatter(1))
    for index, (share, row) in enumerate(zip(local, weeks, strict=True)):
        axes[1].annotate(
            f"{row['code_commits_by_lane'].get('local', 0)}/{row['code_commits']}",
            (index, share),
            xytext=(0, 10),
            textcoords="offset points",
            ha="center",
            fontsize=11,
        )
    for axis in axes:
        axis.axvline(3.5, color="#555555", linestyle="--", linewidth=0.8)
        axis.grid(axis="y", color="#dddddd", linewidth=0.6)
        axis.set_axisbelow(True)
    axes[1].text(3.55, 0.05, "Sep 12 study boundary", fontsize=10)
    axes[1].set_xticks(x, [row["start"][5:10] for row in weeks])
    axes[1].set_xlabel("Week beginning (UTC, 2026)")
    figure.tight_layout(pad=2.0)
    output = directory.parents[4] / "docs/figures/orchestrator-crew-pattern-studies"
    output.mkdir(parents=True, exist_ok=True)
    figure.savefig(output / "nova-churn.svg", metadata={"Date": None})
    figure.savefig(output / "nova-churn.png", dpi=150)
    plt.close(figure)
    print(output / "nova-churn.svg")


if __name__ == "__main__":
    main()
