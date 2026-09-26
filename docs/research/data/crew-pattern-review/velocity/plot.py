"""Plot observed seven-day durable additions without filling censored days."""

import datetime as dt
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.ticker import StrMethodFormatter

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[4]
FIGURES = ROOT / "docs/figures/crew-pattern-review-velocity"


def main():
    data = json.loads((HERE / "summary.json").read_text())
    days = sorted({cell["day"] for cell in data["daily_lane_output"]})
    dates = [dt.datetime.fromisoformat(day) for day in days]
    lanes = ["clive", "codex", "claude", "native", "mixed", "unattributed"]
    colours = ["#006d77", "#3157a4", "#b15d22", "#673d87", "#8a6634", "#666666"]
    fig, axes = plt.subplots(
        2, 1, figsize=(12, 7.5), sharex=True, gridspec_kw={"height_ratios": [2, 1]}
    )
    mature_end = dt.datetime.fromisoformat(
        data["window"]["complete_seven_day_followup_through"]
    )
    for lane, colour in zip(lanes, colours, strict=True):
        values, gross = [], []
        for day in days:
            cells = [
                c
                for c in data["daily_lane_output"]
                if c["day"] == day and c["lane"] == lane
            ]
            values.append(
                sum(c["durable_product_lines_seven_days"] for c in cells)
                if day <= mature_end.date().isoformat()
                else float("nan")
            )
            gross.append(sum(c["product_additions"] for c in cells))
        if any(value > 0 for value in gross):
            label = lane if lane != "unattributed" else "unattributed to a crew lane"
            style = "--" if lane in {"mixed", "unattributed"} else "-"
            axes[0].plot(
                dates,
                values,
                label=label,
                color=colour,
                linestyle=style,
                marker="o",
                markersize=3,
            )
            axes[1].plot(dates, gross, color=colour, linestyle=style, linewidth=1.5)
    for ax in axes:
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(axis="y", color="#dddddd", linewidth=0.6)
        ax.set_ylim(bottom=0)
        ax.yaxis.set_major_formatter(StrMethodFormatter("{x:,.0f}"))
        ax.axvspan(
            mature_end, dates[-1] + dt.timedelta(hours=10), color="#eee6d6", alpha=0.65
        )
        ax.margins(x=0.015)
    axes[0].set_title(
        "Durable product additions by primary landing day and crew lane",
        loc="left",
        fontsize=15,
        pad=18,
    )
    axes[0].set_ylabel("Lines surviving seven days")
    axes[0].legend(frameon=False, fontsize=9, ncol=3, loc="upper left")
    axes[0].text(
        0.99,
        0.70,
        "Seven-day result unavailable\nfor later additions",
        transform=axes[0].transAxes,
        ha="right",
        va="top",
        fontsize=10,
        color="#503d22",
    )
    axes[1].set_title(
        "Gross additions for context (source + tests)", loc="left", fontsize=11
    )
    axes[1].set_ylabel("Lines added")
    axes[1].xaxis.set_major_locator(mdates.DayLocator(interval=2))
    axes[1].xaxis.set_major_formatter(mdates.DateFormatter("Sep %d"))
    fig.text(
        0.08,
        0.012,
        "UTC; cutoff Sep 26, 10:00. Sep 19 has only a partial eligible cohort. September 26 is a partial landing day.\n"
        "First-parent merge diffs counted once. Unattributed and mixed lanes are retained; deleted-and-readded lines get a new birth.",
        fontsize=9,
        color="#333333",
    )
    fig.tight_layout(rect=(0, 0.07, 1, 1))
    FIGURES.mkdir(parents=True, exist_ok=True)
    path = FIGURES / "durable-product-lines.png"
    fig.savefig(path, dpi=160)
    plt.close(fig)
    print(path)


if __name__ == "__main__":
    main()
