"""Render the session-id capture path as an SVG diagram.

Shows where a run's session id can be attached — reuse at dispatch, or the
run's own stream at observation — and the named absence the capture path
records when neither supplies one.
"""

from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).parent

W, H = 940, 470
BG = "#0f1720"
FG = "#e6edf3"
MUTED = "#93a1b1"
BOX = "#1b2733"
EDGE = "#4b5b6b"
OK = "#2f9e6b"
ABS = "#b5762a"

parts: list[str] = [
    f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
    'font-family="ui-sans-serif,system-ui,sans-serif" font-size="14">',
    "<defs>"
    '<marker id="a" markerWidth="9" markerHeight="9" refX="7" refY="3" '
    f'orient="auto"><path d="M0,0 L7,3 L0,6 z" fill="{EDGE}"/></marker>'
    "</defs>",
    f'<rect width="{W}" height="{H}" fill="{BG}"/>',
    f'<text x="24" y="34" fill="{FG}" font-weight="600" '
    'font-size="18">Where a run&#x27;s session id is captured '
    "&#x2014; or why it was not</text>",
]


def box(x: int, y: int, w: int, h: int, label: str, sub: list[str],
        color: str = BOX, border: str = EDGE) -> None:
    """Append one labelled box with its sub-lines."""
    parts.extend(
        [
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" '
            f'fill="{color}" stroke="{border}"/>',
            f'<text x="{x + w / 2:.0f}" y="{y + 26}" fill="{FG}" '
            f'text-anchor="middle" font-weight="600">{label}</text>',
        ]
    )
    for i, line in enumerate(sub):
        parts.append(
            f'<text x="{x + w / 2:.0f}" y="{y + 48 + i * 17}" fill="{MUTED}" '
            f'text-anchor="middle" font-size="12">{line}</text>'
        )


box(40, 80, 210, 96, "1. dispatch", ["reuse a stored session for", "this configuration"])
box(40, 230, 210, 96, "2. observation", ["fold the run's own stream", "into its pointer"])
box(360, 80, 230, 96, "session_id recorded", ["resume is reachable"], OK, OK)
box(360, 230, 230, 96, "session_id_absent", ["point + reason, not a null"], ABS, ABS)
box(680, 80, 220, 96, "resume", ["the session survives everywhere"])
box(680, 230, 220, 96, "redispatch remainder", ["after worktree inspection"])
box(680, 366, 220, 84, "harness launch or", ["unreadable stream:",
                                             "point named"])

for x1, y1, x2, y2 in [
    (250, 128, 360, 128),
    (250, 278, 360, 278),
    (590, 128, 680, 128),
    (475, 176, 475, 230),
    (590, 278, 680, 278),
    (790, 326, 790, 366),
]:
    parts.append(
        f'<path d="M {x1} {y1} L {x2} {y2}" stroke="{EDGE}" stroke-width="1.5" '
        'fill="none" marker-end="url(#a)"/>'
    )

parts.append(
    f'<text x="24" y="{H - 16}" fill="{MUTED}" '
    'font-size="12">measured 2026-09-23: 44 of 50 live pointers carried no '
    "recorded id; 43 of them had a session id already present in the "
    "run&#x27;s own stream</text>"
)
parts.append("</svg>")
(HERE / "session-id-capture.svg").write_text("\n".join(parts))
print(f"wrote {HERE / 'session-id-capture.svg'}")
