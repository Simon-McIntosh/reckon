"""Where the tail read starts, per size of the record the stream ends with.

The reader answers "which record did this run end on" from the end of the
measured stream, and the question this figure settles is whether that answer
survives a last record bigger than the window it reads: the left panel draws
each read's extent against the bytes it had to reach back over, and the right
panel the total bytes each answer cost. It is generated from the reader itself,
so the plotted read ranges are the ones the code took rather than a rendering
of its intent.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from reckon.crew import recovery  # noqa: E402

OUT = Path(__file__).with_suffix(".png")
STREAM_DIR = Path(os.environ.get("TMPDIR", "/tmp")) / "death-row-tail-window-streams"
WINDOW = recovery._STREAM_TAIL_BYTES

CASES = [
    ("1 KiB", 1 * 1024),
    ("64.5 KiB", 64_500),
    ("65.5 KiB", 65_500),
    ("512 KiB", 512 * 1024),
]


def final_record(padding: int) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "x" * padding}]},
        }
    )


def build(name: str, padding: int) -> Path:
    STREAM_DIR.mkdir(parents=True, exist_ok=True)
    path = STREAM_DIR / f"{padding}.jsonl"
    prefix = [
        {"type": "thread.started"},
        {"type": "turn.started"},
        {"type": "assistant", "message": {"content": []}},
    ]
    path.write_text(
        "".join(json.dumps(r) + "\n" for r in prefix) + final_record(padding) + "\n",
        encoding="utf-8",
    )
    return path


def measure(path: Path) -> tuple[list[tuple[int, int]], str | None]:
    """The (offset, length) of every read the reader took, and its answer."""
    reads: list[tuple[int, int]] = []
    original = Path.open

    class Handle:
        def __init__(self, inner):
            self._inner = inner

        def read(self, *args, **kwargs) -> bytes:
            data = self._inner.read(*args, **kwargs)
            reads.append((self._inner.tell() - len(data), len(data)))
            return data

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return bool(self._inner.__exit__(*exc))

        def __getattr__(self, name):
            return getattr(self._inner, name)

    Path.open = lambda self, *a, **k: Handle(original(self, *a, **k))
    try:
        answer = recovery._newest_stream_last_record_type({"log_path": str(path)})
    finally:
        Path.open = original
    return reads, answer


figure, (left, right) = plt.subplots(
    1, 2, figsize=(12.4, 5.0), width_ratios=[1.55, 1.0]
)
rows = []
for index, (label, padding) in enumerate(CASES):
    path = build(label, padding)
    reads, answer = measure(path)
    reads = [r for r in reads if r[1] > 0]
    line = len(final_record(padding).encode())
    rows.append((label, path.stat().st_size, line, reads, answer))

for index, (label, size, line, reads, answer) in enumerate(rows):
    y = -index
    left.barh(y, size, height=0.52, color="#dfe6ee", edgecolor="#9fb0c2")
    left.barh(y, line, height=0.52, color="#f0b27a", edgecolor="#b9772e")
    for start, length in reads:
        left.barh(
            y,
            length,
            left=start,
            height=0.26,
            color="#2f6f9f" if length < WINDOW else "#7a3f9f",
            edgecolor="none",
            alpha=0.85,
        )
    left.text(size + 8192, y, answer or "no last record", va="center", fontsize=9, family="monospace")
    left.text(
        -8192,
        y,
        f"{label} record\nfile {size / 1024:.0f} KiB",
        ha="right",
        va="center",
        fontsize=9,
    )

left.axvline(WINDOW, color="#444444", linestyle="--", linewidth=1.1)
left.axvline(0, color="#444444", linestyle="--", linewidth=1.1)
left.text(WINDOW * 1.03, len(rows) - 0.75, "64 KiB window", fontsize=9, color="#444444")
left.set_xlim(-150_000, max(row[1] for row in rows) * 1.15)
left.set_ylim(-len(rows) + 0.4, len(rows) - 0.35)
left.set_yticks([])
left.set_xlabel("byte offset in the stream")
left.set_title(
    "the read starts where its window does\n"
    "(grey: stream, orange: the record the stream ends with,\n"
    "blue/violet bars: one read each)",
    fontsize=10,
)

totals = [sum(length for _, length in row[3]) for row in rows]
bars = right.bar(
    [row[0] for row in rows],
    [total / 1024 for total in totals],
    color=["#2f6f9f", "#2f6f9f", "#7a3f9f", "#7a3f9f"],
)
for bar, total in zip(bars, totals, strict=True):
    right.text(
        bar.get_x() + bar.get_width() / 2,
        total / 1024 + 4,
        f"{total / 1024:.0f} KiB",
        ha="center",
        fontsize=9,
    )
right.axhline(WINDOW / 1024, color="#444444", linestyle="--", linewidth=1.1)
right.text(-0.42, WINDOW / 1024 + 12, "one window", fontsize=9, color="#444444", ha="left")
right.set_ylabel("bytes read to answer (KiB)")
right.set_ylim(0, max(totals) / 1024 * 1.22)
right.set_title("the cost of the answer", fontsize=10)

figure.suptitle(
    "Reading the record a killed worker's stream ends with, whatever its size",
    fontsize=12,
)
figure.tight_layout(rect=(0, 0, 1, 0.95))
figure.savefig(OUT, dpi=140)
print(f"wrote {OUT}")
for label, size, line, reads, answer in rows:
    print(
        f"{label:>9}  file={size:7d}B  final-line={line:7d}B  "
        f"reads={[(s, l) for s, l in reads]}  answer={answer!r}"
    )