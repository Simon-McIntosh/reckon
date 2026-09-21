"""Exposure of the promotion cohort to a scoring guard inside crew complete.

Left panel: how the cohort's failures split when the guard is flipped closed.
Right panel: which fixture files supply the scoring pointers the guard reaches.
"""

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = Path(__file__).resolve().parent

BASELINE_FAILED = 18
PROBE_FAILED = 37
NEWLY_FAILING = 19
COHORT_TESTS = 184

SOURCES = [
    ("tests/test_gate_evidence.py", 5),
    ("tests/test_crew_promotion.py", 10),
    ("tests/test_crew_test_role.py", 3),
    ("tests/test_a_short_commit_list_is_reported.py", 1),
]


def _composition(ax) -> None:
    labels = ["HEAD\n(guard absent)", "probe\n(guard closed)"]
    pre = [BASELINE_FAILED, BASELINE_FAILED]
    new = [0, NEWLY_FAILING]
    ax.bar(labels, pre, color="#9aa4b2", label="already failing at HEAD")
    ax.bar(
        labels, new, bottom=pre, color="#c0392b", label="failing only with the guard"
    )
    ax.text(
        0,
        BASELINE_FAILED / 2,
        str(BASELINE_FAILED),
        ha="center",
        va="center",
        color="white",
        fontsize=11,
        fontweight="bold",
    )
    ax.text(
        1,
        BASELINE_FAILED / 2,
        str(BASELINE_FAILED),
        ha="center",
        va="center",
        color="white",
        fontsize=11,
        fontweight="bold",
    )
    ax.text(
        1,
        BASELINE_FAILED + NEWLY_FAILING / 2,
        str(NEWLY_FAILING),
        ha="center",
        va="center",
        color="white",
        fontsize=11,
        fontweight="bold",
    )
    ax.set_ylabel("failing tests")
    ax.set_title(f"cohort of {COHORT_TESTS} tests, unchanged between runs", fontsize=9)
    ax.legend(fontsize=8, loc="upper left")
    ax.set_ylim(0, PROBE_FAILED + 8)


def _by_source(ax) -> None:
    names = [name.split("/")[-1] for name, _ in SOURCES]
    counts = [count for _, count in SOURCES]
    ax.barh(names, counts, color="#c0392b")
    for yi, count in enumerate(counts):
        ax.text(count + 0.15, yi, str(count), va="center", fontsize=9)
    ax.set_xlabel("newly failing tests")
    ax.set_title("fixture file that produced the scoring pointer", fontsize=9)
    ax.set_xlim(0, max(counts) + 2)


def main() -> None:
    fig, (left, right) = plt.subplots(
        1, 2, figsize=(9.2, 3.6), gridspec_kw={"width_ratios": [1, 1.5]}
    )
    _composition(left)
    _by_source(right)
    fig.suptitle(
        "a scoring guard in crew complete reaches 19 tests that pass without it",
        fontsize=10,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig.savefig(HERE / "exposure_counts.png", dpi=170)


if __name__ == "__main__":
    main()
