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

import ast
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
REMEASURE = (HERE / "captures" / "window-3.txt", HERE / "captures" / "window-4.txt")
PROBE = HERE / "captures" / "signature-probe.txt"

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
DSTATE = re.compile(r"^DSTATE sample=\d+ hits=(?P<hits>\[.*\])$")
PER_PROCESS = re.compile(r"^per_process=(?P<rows>\[.*\])$")


def _words(count: int) -> str:
    """A small count as the prose spells it, so a phrase can be derived."""
    return {0: "no", 1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six"}.get(
        count, str(count)
    )


def window(path: Path) -> dict:
    """One capture's totals, per-kind figures, control delta and D-state samples.

    ``dstate`` counts the samples carrying any monitor process, and
    ``dstate_station`` counts the samples carrying the station itself, which the
    capture names in its per-process rows. The two are kept apart because
    attributing the first to the station measures a different thing.
    """
    figures: dict = {"kinds": {}}
    samples: list[list[str]] = []
    station_pid = None
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
        elif match := DSTATE.match(line):
            samples.append([str(hit.split(":")[0]) for hit in ast.literal_eval(match["hits"])])
        elif match := PER_PROCESS.match(line):
            for row in ast.literal_eval(match["rows"]):
                if row.get("kind") == "station":
                    station_pid = str(row["pid"])
    figures["dstate_samples"] = samples
    figures["dstate"] = sum(1 for hits in samples if hits)
    figures["station_pid"] = station_pid
    figures["dstate_station"] = (
        sum(1 for hits in samples if station_pid in hits) if station_pid else 0
    )
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
    assert "four of its six samples" in text and counts[1] == 4, (
        f"the second capture has {counts[1]} D-state samples and the evidence states four"
    )
    assert "one of six" in text and counts[0] == 1, (
        f"the first capture has {counts[0]} D-state samples and the evidence states one"
    )


def test_every_window_is_above_both_bounds() -> None:
    for path in CAPTURES:
        figures = window(path)
        assert figures["total_mb"] > BOUND_MB_PER_MIN, (
            f"{path.name} is inside the read bound"
        )
        assert figures["cpu"] > BOUND_CPU_PERCENT, (
            f"{path.name} is inside the CPU bound"
        )
        assert figures["kinds"]["station"]["mb"] > BOUND_MB_PER_MIN, (
            f"{path.name}: the station alone is inside the bound"
        )


def test_the_fragment_names_the_gate_that_guards_it() -> None:
    """A record that cites a guard must cite one a reader can run."""
    text = prose()
    assert "test_evidence_matches_the_captures.py" in text, (
        "the evidence names no committed gate, so a reader cannot run the check "
        "it claims"
    )
    assert "test_window_reconciles_with_the_evidence.py" not in text, (
        "the evidence still cites the run-directory gate this module replaced"
    )
    assert "4 passed in 1.48s" not in text, (
        "the evidence still quotes the superseded gate's result as its own"
    )


def test_the_remeasurement_quotes_every_later_window() -> None:
    """The later windows are the after arm; each figure they carry is in the prose."""
    text = prose()
    flat = " ".join(text.split())
    for path in REMEASURE:
        figures = window(path)
        for value in (
            figures["total_mb"],
            figures["kinds"]["station"]["mb"],
            figures["kinds"]["watch"]["mb"],
            figures["kinds"]["follow"]["mb"],
        ):
            assert f"{value:,.0f}" in text, (
                f"{path.name}: the evidence states no figure of {value:,.0f} MB/min"
            )
        assert f"{figures['cpu']:.2f}" in text, (
            f"{path.name}: the evidence states no total CPU of {figures['cpu']:.2f} %"
        )
    first, second = (window(path) for path in REMEASURE)
    for name, figures in (("3", first), ("4", second)):
        assert figures["station_pid"], (
            f"window {name}'s capture names no station process, so the station's own "
            f"D-state count cannot be read"
        )
        assert figures["dstate_station"] <= figures["dstate"], (
            f"window {name}: the station reads as D in more samples than carry any "
            f"monitor process"
        )
        assert f"{_words(figures['dstate'])} of window {name}'s six samples" in flat, (
            f"window {name}'s capture records a monitor process in state D in "
            f"{figures['dstate']} of six samples and the evidence does not say so"
        )
    assert (
        f"the station itself among them in {_words(first['dstate_station'])} and "
        f"{_words(second['dstate_station'])} of those samples"
    ) in flat, (
        f"the capture puts the station in state D in {first['dstate_station']} and "
        f"{second['dstate_station']} samples and the evidence states another count"
    )
    assert "the station appears in state D in" not in flat, (
        "the evidence still attributes the fleet-wide D-state count to the station alone"
    )


def test_every_remeasured_window_is_above_both_bounds() -> None:
    for path in REMEASURE:
        figures = window(path)
        assert figures["total_mb"] > BOUND_MB_PER_MIN, (
            f"{path.name} is inside the read bound"
        )
        assert figures["cpu"] > BOUND_CPU_PERCENT, (
            f"{path.name} is inside the CPU bound"
        )
        assert figures["kinds"]["station"]["mb"] > BOUND_MB_PER_MIN, (
            f"{path.name}: the station alone is inside the bound"
        )


def test_the_fragment_quotes_the_signature_probe_it_diagnoses_with() -> None:
    sys.path.insert(0, str(HERE))
    from plot_remeasurement import read_signature_probe

    rows = read_signature_probe(PROBE)
    fallbacks = [row for row in rows if not row["agree"]]
    assert {row["project"] for row in fallbacks} == {"reckon", "imas-efit"}, (
        "the capture's disagreeing projects are not the two the evidence names"
    )
    assert sum(1 for row in rows if row["agree"]) == 11, (
        "the capture answers for a different number of projects than the eleven stated"
    )
    text = prose()
    for row in fallbacks:
        assert f"{row['rchar']:,}" in text, (
            f"the evidence states no call cost of {row['rchar']:,} B for {row['project']}"
        )


def test_the_remeasurement_figure_reads_the_same_numbers_as_the_capture() -> None:
    sys.path.insert(0, str(HERE))
    from plot_remeasurement import WINDOWS, read_capture

    assert [path for _, path in WINDOWS] == list(REMEASURE), (
        "the re-measurement figure draws captures other than the committed pair"
    )
    for path in REMEASURE:
        drawn = read_capture(path)
        recorded = window(path)
        for kind, figures in recorded["kinds"].items():
            assert drawn[f"{kind}_mb"] == figures["mb"], (
                f"{path.name}: the figure would draw {drawn[f'{kind}_mb']} MB/min for "
                f"{kind} where the capture records {figures['mb']}"
            )
            assert drawn[f"{kind}_cpu"] == figures["cpu"]


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
