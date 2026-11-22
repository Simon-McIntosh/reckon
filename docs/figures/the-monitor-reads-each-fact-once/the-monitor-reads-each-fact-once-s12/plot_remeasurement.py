"""The cutover window re-measured after the signature mirror, and its per-project cost.

The first panel pair repeats the earlier figure's form on two later windows, so
the two readings compare like with like: per-arm read volume on a log axis and
per-arm CPU on a linear one, the cutover bound drawn dotted. The second figure
answers why the later windows did not improve: the payload-free signature mirror
answers for eleven of the thirteen projects, and the two whose mirror count
disagrees with their stored run count fall back to the whole-store scan, which is
what the two outliers cost.

Every number drawn is read from the captures committed beside this script, so the
figure cannot state a figure the logs do not carry. The save refuses a figure
whose text runs off the canvas or overlaps another label.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from plot_cutover_window import (  # noqa: E402 - sibling style and checks
    BOUND_CPU_PERCENT,
    BOUND_MB_PER_MIN,
    DPI,
    HEIGHT_IN,
    KIND_ORDER,
    LABEL_PT,
    SUBJECT,
    SUBJECT_LIGHT,
    TICK_PT,
    WIDTH_IN,
    _check_labels,
    _panel,
    read_capture,
)

WINDOWS = (
    ("window 3, 13:00:18Z", HERE / "captures" / "window-3.txt"),
    ("window 4, 13:04:19Z", HERE / "captures" / "window-4.txt"),
)
PROBE = HERE / "captures" / "signature-probe.txt"

# The two colours name which answer the project's signature got, and keep that
# meaning across the figure: the feature colour is the fallback, grey the mirror.
FALLBACK = "#b8327e"

SIGNATURE = re.compile(
    r"^SIGNATURE project=(?P<project>\S+) rows=(?P<rows>\d+) rchar_bytes=(?P<rchar>\d+)"
)
COUNTS = re.compile(
    r"^(?P<project>\S+)\s+(?P<agree>True|False)\s+(?P<stored>\d+)\s+(?P<present>\d+)\s*$"
)


def read_signature_probe(path: Path) -> list[dict]:
    """Per-project store counts and the bytes one signature read costs."""
    rows: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if match := COUNTS.match(line):
            rows[match["project"]] = {
                "project": match["project"],
                "agree": match["agree"] == "True",
                "stored": int(match["stored"]),
                "present": int(match["present"]),
            }
        elif match := SIGNATURE.match(line):
            row = rows.setdefault(match["project"], {"project": match["project"]})
            row["rchar"] = int(match["rchar"])
    complete = [row for row in rows.values() if "rchar" in row and "stored" in row]
    if not complete:
        raise SystemExit(f"{path} carries no signature rows")
    return sorted(complete, key=lambda row: row["rchar"])


def windows_figure() -> None:
    readings = [(label, read_capture(path)) for label, path in WINDOWS]
    figure, (left, right) = plt.subplots(1, 2, figsize=(WIDTH_IN, HEIGHT_IN), dpi=DPI)
    _panel(
        left,
        [readings[0][1][f"{kind}_mb"] for kind in KIND_ORDER],
        [readings[1][1][f"{kind}_mb"] for kind in KIND_ORDER],
        BOUND_MB_PER_MIN,
        "read volume [MB/min, log scale]",
        True,
        (1.0, 400000.0),
    )
    _panel(
        right,
        [readings[0][1][f"{kind}_cpu"] for kind in KIND_ORDER],
        [readings[1][1][f"{kind}_cpu"] for kind in KIND_ORDER],
        BOUND_CPU_PERCENT,
        "CPU [% of one core]",
        False,
        (0.0, 185.0),
    )
    right.tick_params(axis="y", labelleft=False, left=False)
    figure.legend(
        handles=[
            Patch(color=SUBJECT, label=readings[0][0]),
            Patch(color=SUBJECT_LIGHT, label=readings[1][0]),
        ],
        loc="upper center",
        ncol=2,
        frameon=False,
        fontsize=TICK_PT,
        bbox_to_anchor=(0.5, 1.02),
    )
    _check_labels(figure)
    figure.savefig(
        HERE / "cutover-window-after-the-signature-mirror.svg",
        bbox_inches="tight",
        facecolor="white",
    )
    plt.close(figure)


def signature_figure() -> None:
    rows = read_signature_probe(PROBE)
    figure, axis = plt.subplots(figsize=(WIDTH_IN, HEIGHT_IN), dpi=DPI)
    positions = np.arange(len(rows), dtype=float)
    megabytes = [row["rchar"] / 1e6 for row in rows]
    colours = [SUBJECT if row["agree"] else FALLBACK for row in rows]
    axis.barh(positions, megabytes, height=0.62, color=colours, linewidth=0, zorder=2)
    axis.set_yticks(positions)
    axis.set_yticklabels([row["project"] for row in rows], fontsize=TICK_PT)
    axis.set_xscale("log")
    axis.set_xlim(0.03, 400.0)
    axis.set_xlabel("bytes read per signature call [MB, log scale]", fontsize=LABEL_PT)
    axis.tick_params(axis="x", labelsize=TICK_PT)
    axis.spines[["top", "right"]].set_visible(False)
    for position, row in zip(positions, rows, strict=True):
        label = f"{row['rchar'] / 1e6:,.2f} MB"
        if not row["agree"]:
            label += f"  ·  stored {row['stored']}, present {row['present']}"
        axis.annotate(
            label,
            xy=(row["rchar"] / 1e6, position),
            xytext=(5, 0),
            textcoords="offset points",
            color=SUBJECT if row["agree"] else FALLBACK,
            fontsize=TICK_PT,
            ha="left",
            va="center",
        )
    axis.set_ylim(len(rows) - 0.4, -0.6)
    figure.legend(
        handles=[
            Patch(color=SUBJECT, label="answered from the signature mirror"),
            Patch(color=FALLBACK, label="fell back to the whole-store scan"),
        ],
        loc="upper center",
        ncol=2,
        frameon=False,
        fontsize=TICK_PT,
        bbox_to_anchor=(0.5, 1.02),
    )
    _check_labels(figure)
    figure.savefig(
        HERE / "signature-mirror-per-project.svg",
        bbox_inches="tight",
        facecolor="white",
    )
    plt.close(figure)


def main() -> None:
    plt.style.use("data-ink")
    windows_figure()
    signature_figure()


if __name__ == "__main__":
    main()
