"""Draw each module's estimated token size against the worker-window bound.

A module is read in one worker window, and one test holds every module to a
bound on its estimated token size, computed from its byte length by the same
estimator the routing code uses. The figure shows what the two-concern move did
to that budget: the largest module fell from above the bound to below it, the
code it gave up now sits in two new modules of its own, and two modules the
move did not touch stay above the bound with the portion past it marked.

Every value is a measurement, read from the module files at the head revision
and at the base revision, and carries no scale of its own beyond the token
estimate, which is stated on the axis.

Run: python module_budget.py  (writes module_budget.svg beside this file)
"""

from pathlib import Path

WIDTH, HEIGHT = 1400, 560
X0, X1 = 360, 1330
MAX_TOKENS = 60_000
BOUND = 50_000
BAR = 24
ROWS = [130, 190, 250, 310, 390, 450]
INK, MUTED, PAST = "#1f2328", "#929292", "#b42318"
FULL, LIGHT, CARRIED = "#2f6f9f", "#a8c6dd", "#5f96bd"

# (row y, label, estimated tokens, fill, past-the-bound mark)
SERIES = [
    (ROWS[0], "serve.py, before", 54_746, LIGHT, False),
    (ROWS[1], "serve.py, after", 44_178, FULL, False),
    (ROWS[2], "change_streams.py", 5_725, CARRIED, False),
    (ROWS[3], "git_dates.py", 5_605, CARRIED, False),
    (ROWS[4], "crew/review.py", 51_584, MUTED, True),
    (ROWS[5], "ledger.py", 51_666, MUTED, True),
]
TICKS = [0, 20_000, 40_000, 60_000]


def x(tokens: int) -> float:
    return X0 + (X1 - X0) * tokens / MAX_TOKENS


def bar(y: float, tokens: int, fill: str) -> str:
    return (
        f'<rect x="{X0}" y="{y - BAR / 2:.1f}" width="{x(tokens) - X0:.1f}" '
        f'height="{BAR}" fill="{fill}" />'
    )


def text(x_: float, y: float, body: str, *, size: int = 14, fill: str = INK,
         anchor: str = "start") -> str:
    return (
        f'<text x="{x_:.0f}" y="{y:.0f}" font-size="{size}" fill="{fill}" '
        f'text-anchor="{anchor}">{body}</text>'
    )


def label(y: float, body: str) -> str:
    return text(X0 - 16, y + 5, body, size=16, anchor="end")


parts = [
    f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" '
    f'viewBox="0 0 {WIDTH} {HEIGHT}">'
]

# The axis carries a scale, so the values are gridded by ticks along it and the
# bars are read against those ticks rather than against a background.
parts.append(f'<line x1="{X0}" y1="{ROWS[-1] + 40}" x2="{X1}" y2="{ROWS[-1] + 40}" '
             f'stroke="{MUTED}" stroke-width="1.2" />')
for tick in TICKS:
    parts.append(f'<line x1="{x(tick):.1f}" y1="{ROWS[-1] + 40}" '
                 f'x2="{x(tick):.1f}" y2="{ROWS[-1] + 46}" stroke="{MUTED}" '
                 f'stroke-width="1.2" />')
    parts.append(text(x(tick), ROWS[-1] + 70, f"{tick // 1000:,}k", fill=MUTED,
                      anchor="middle"))
parts.append(text(X1, ROWS[-1] + 96, "estimated tokens", size=16, anchor="end"))

# The bound is the limit the population is read against, so it is drawn first
# and every bar sits over it.
parts.append(f'<line x1="{x(BOUND):.1f}" y1="{ROWS[0] - 46}" '
             f'x2="{x(BOUND):.1f}" y2="{ROWS[-1] + 40}" stroke="{MUTED}" '
             f'stroke-width="1.2" stroke-dasharray="4 4" />')
parts.append(text(x(BOUND) - 8, ROWS[0] - 54, "50,000 bound", fill=MUTED,
                  anchor="end"))
parts.append(f'<rect x="{X0}" y="{ROWS[0] - 82}" width="14" '
             f'height="14" fill="{PAST}" />')
parts.append(text(X0 + 22, ROWS[0] - 70, "past the bound", fill=PAST))

for y, name, tokens, fill, past in SERIES:
    parts.append(label(y, name))
    parts.append(bar(y, tokens, fill))
    if past:
        parts.append(
            f'<rect x="{x(BOUND):.1f}" y="{y - BAR / 2:.1f}" '
            f'width="{x(tokens) - x(BOUND):.1f}" height="{BAR}" fill="{PAST}" />'
        )
    parts.append(text(x(tokens) + 10, y + 5, f"{tokens:,}"))

parts.append("</svg>\n")
Path(__file__).with_suffix(".svg").write_text("\n".join(parts), encoding="utf-8")
print("wrote", Path(__file__).with_suffix(".svg").name)