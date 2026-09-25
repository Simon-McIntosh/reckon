"""Duration of the five event-wait tests, fixed sleep against observed event.

The bars on the left are the per-test ``--durations=0`` call times recorded
before and after the two test files stopped spending fixed sleeps and started
awaiting an observed event. The dashed line is half the base time, which is the
margin the change had to clear; the text panel on the right carries the two
bounds that must still fail when violated.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

CASES = [
    ("discarded run\nnot recreated", 48.93, 13.03),
    ("delegated launch\nboundary snapshot", 43.15, 7.05),
    ("sigkill to the\nworker recorded", 42.55, 7.97),
    ("sigkill to dispatch\nleaves marker", 39.30, 8.11),
    ("every tree armed\nwithin the bound", 15.03, 0.06),
]


def main() -> None:
    labels = [label for label, _, _ in CASES]
    base = [before for _, before, _ in CASES]
    after = [now for _, _, now in CASES]

    positions = range(len(CASES))
    width = 0.38

    fig, (chart, notes) = plt.subplots(
        1, 2, figsize=(13.5, 6.2), gridspec_kw={"width_ratios": [3.0, 1.15]}
    )

    chart.bar(
        [p - width / 2 for p in positions],
        base,
        width,
        label="fixed sleep",
        color="#b4534b",
    )
    chart.bar(
        [p + width / 2 for p in positions],
        after,
        width,
        label="observed event",
        color="#3d7d5a",
    )
    chart.plot(
        positions,
        [value / 2 for value in base],
        linestyle="--",
        linewidth=1.2,
        color="#444444",
        label="half the base time",
    )

    for index, (_, before, now) in enumerate(CASES):
        chart.text(
            index - width / 2,
            before + 3,
            f"{before:.1f}",
            ha="center",
            va="bottom",
            fontsize=8,
        )
        chart.text(
            index + width / 2,
            now + 3,
            f"{now:.2f}" if now < 1 else f"{now:.1f}",
            ha="center",
            va="bottom",
            fontsize=8,
        )

    chart.set_xticks(list(positions))
    chart.set_xticklabels(labels, fontsize=8.5)
    chart.set_ylabel("call time (s)")
    chart.set_ylim(0, 56)
    chart.set_title(
        "Five event-wait tests: fixed sleep vs observed event (serial --durations=0)",
        fontsize=11,
    )
    chart.legend(fontsize=9, loc="upper right")
    chart.grid(axis="y", alpha=0.25)
    chart.axhline(0, color="#999999", linewidth=0.8)

    notes.axis("off")
    notes.set_title("Both bounds still fail when violated", fontsize=10.5)
    notes.text(
        0.0,
        0.96,
        "\n".join(
            [
                "dispatch return bound",
                "  DISPATCH_EXIT_BOUND = 5.0 s",
                "  a 6 s sleep injected in a scratch",
                "  copy of the dispatch path trips it:",
                "  exit_after 10.70 s vs 8.04 s allowed",
                "",
                "arming bound",
                "  _ARMED_WITHIN_S = 20.0 s",
                "  holding every tree open past it",
                "  trips it:",
                "  0 of 3 trees armed within 20.0 s",
                "",
                "the shim now signals the charged scan",
                "is in progress and holds it until the",
                "case releases it; the patched watch",
                "constructor signals the open arming",
                "window the same way.",
            ]
        ),
        ha="left",
        va="top",
        fontsize=8.5,
        family="monospace",
        transform=notes.transAxes,
    )

    fig.tight_layout()
    out = Path(__file__).with_suffix(".png")
    fig.savefig(out, dpi=150)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()