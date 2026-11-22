"""Hold the evidence fragment to the captures it quotes.

The fragment states per-window read volume, CPU, a positive-control delta and
D-state counts. This module re-reads the captured logs committed beside it and
asserts the fragment still carries those figures, so an edit to the prose that
drifts from the log reddens the gate instead of passing silently. It also
asserts the figure's own reader agrees with the capture, so the chart cannot
draw a number the log does not carry.

The capture pair is the evidence; this reconciles the record against it.
"""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
FRAGMENT = (
    REPO
    / "docs/evidence/fragments/the-monitor-reads-each-fact-once"
    / "the-monitor-reads-each-fact-once-s12.html"
)
CAPTURES = (HERE / "captures" / "window-1.txt", HERE / "captures" / "window-2.txt")

BOUND_MB_PER_MIN = 20.0
BOUND_CPU_PERCENT = 5.0

TOTAL = re.compile(
    r"^TOTAL rchar_MB_per_min=(?P<rchar>[\d.]+) .*cpu_pct_of_one_core=(?P<cpu>[\d.]+)"
)
KIND = re.compile(
    r"^BY_KIND (?P<kind>\w+) count=\d+ rchar_MB_per_min=(?P<rchar>[\d.]+)"
    r" read_bytes_MB=[\d.]+ cpu_pct=(?P<cpu>[\d.]+)"
)
CONTROL = re.compile(r"^CONTROL .*rchar_delta=(?P<delta>\d+)")
DSTATE = re.compile(r"^DSTATE sample=\d+ hits=\['")


def window(path: Path) -> dict:
    """One capture's totals, per-kind figures, control delta and D-state hits."""
    figures: dict = {"kinds": {}}
    dstate = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if match := TOTAL.match(line):
            figures["total_mb"] = float(match["rchar"])
            figures["cpu"] = float(match["cpu"])
        elif match := KIND.match(line):
            figures["kinds"][match["kind"]] = {
                "mb": float(match["rchar"]),
                "cpu": float(match["cpu"]),
            }
        elif match := CONTROL.match(line):
            figures["control_delta"] = int(match["delta"])
        elif DSTATE.match(line):
            dstate += 1
    figures["dstate"] = dstate
    assert path.read_text(encoding="utf-8").rstrip().endswith("EXIT=0"), (
        f"{path.name} did not end with a clean status"
    )
    return figures


def prose() -> str:
    """The fragment's rendered text, tags stripped and entities decoded."""
    text = FRAGMENT.read_text(encoding="utf-8")
    text = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(text).replace("\u00a0", " ")


def test_the_fragment_quotes_the_control_delta_each_capture_records() -> None:
    captured = {window(path)["control_delta"] for path in CAPTURES}
    quoted = {int(value) for value in re.findall(r"rchar_delta=(\d+)", prose())}
    assert captured == quoted, (
        f"the capture records the control deltas {sorted(captured)} but the "
        f"evidence quotes {sorted(quoted)}"
    )


def test_the_fragment_quotes_every_window_total_and_station_figure() -> None:
    text = prose()
    for path in CAPTURES:
        figures = window(path)
        assert f"{figures['total_mb']:,.0f}" in text, (
            f"{path.name}: the evidence states no total of {figures['total_mb']:,.0f} MB/min"
        )
        assert f"{figures['kinds']['station']['mb']:,.0f}" in text, (
            f"{path.name}: the evidence states no station figure of "
            f"{figures['kinds']['station']['mb']:,.0f} MB/min"
        )
        assert f"{figures['kinds']['watch']['mb']:,.0f}" in text, (
            f"{path.name}: the evidence states no producer figure of "
            f"{figures['kinds']['watch']['mb']:,.0f} MB/min"
        )
        assert f"{figures['kinds']['follow']['mb']:,.0f}" in text, (
            f"{path.name}: the evidence states no follower figure of "
            f"{figures['kinds']['follow']['mb']:,.0f} MB/min"
        )


def test_the_fragment_quotes_the_d_state_counts_the_samples_record() -> None:
    counts = [window(path)["dstate"] for path in CAPTURES]
    text = " ".join(prose().split())
    assert f"four of its six samples" in text and counts[1] == 4, (
        f"the second capture has {counts[1]} D-state samples and the evidence states four"
    )
    assert f"one of six" in text and counts[0] == 1, (
        f"the first capture has {counts[0]} D-state samples and the evidence states one"
    )


def test_every_window_is_above_both_bounds() -> None:
    for path in CAPTURES:
        figures = window(path)
        assert figures["total_mb"] > BOUND_MB_PER_MIN, f"{path.name} is inside the read bound"
        assert figures["cpu"] > BOUND_CPU_PERCENT, f"{path.name} is inside the CPU bound"
        assert figures["kinds"]["station"]["mb"] > BOUND_MB_PER_MIN, (
            f"{path.name}: the station alone is inside the bound"
        )


def test_the_figure_reads_the_same_numbers_as_the_capture() -> None:
    sys.path.insert(0, str(HERE))
    from plot_cutover_window import read_capture

    for path in CAPTURES:
        drawn = read_capture(path)
        recorded = window(path)
        for kind, figures in recorded["kinds"].items():
            assert drawn[f"{kind}_mb"] == figures["mb"], (
                f"{path.name}: the figure would draw {drawn[f'{kind}_mb']} MB/min for "
                f"{kind} where the capture records {figures['mb']}"
            )
            assert drawn[f"{kind}_cpu"] == figures["cpu"]