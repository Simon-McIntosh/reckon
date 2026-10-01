"""Draw the morning's timeline: SSH sessions, page loads, abandoned responses
and the two server processes, against the two commits to the edited document.

Every time below was read on 2026-10-01 from one of three sources: `last -F`
on the login node (SSH sessions), the shared server log (page loads and broken
pipes), and `systemctl --user show` on each host (server lifetimes). The figure
is a record of that measurement, so the times are data, not configuration.

Run: python timeline.py  (writes timeline.svg beside this file)
"""

from pathlib import Path
from xml.etree import ElementTree

START, END = "05:25", "06:40"
WIDTH, LEFT, RIGHT = 770, 240, 24
INK, MUTED, ALERT, CALM = "#1f2328", "#57606a", "#b42318", "#1a7f37"

SESSIONS = [  # (login, logout) from `last -F`; None means still logged in
    ("05:31:19", "06:09:05"),
    ("05:36:28", "06:14:15"),
    ("05:37:48", "06:14:15"),
    ("06:09:07", None),
    ("06:12:43", None),
    ("06:16:22", None),
    ("06:17:17", None),
]
PAGE_LOADS = [
    "05:31:23",
    "05:32:02",
    "06:11:18",
    "06:12:19",
    "06:18:59",
    "06:19:08",
    "06:19:12",
    "06:23:58",
    "06:24:10",
]
ABANDONED = ["05:31:30", "05:31:55", "06:12:35", "06:24:22", "06:24:26"]
DOC_COMMITS = [("05:41", "580d055"), ("06:07", "64d8eac")]
RESTART = "06:34:36"
DUPLICATE = ("05:36:28", "06:35:02")


def seconds(clock: str) -> int:
    parts = [int(p) for p in clock.split(":")] + [0]
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def x(clock: str) -> float:
    span = seconds(END) - seconds(START)
    return LEFT + (seconds(clock) - seconds(START)) / span * (WIDTH - LEFT - RIGHT)


def text(x0, y0, body, *, size=13, fill=INK, anchor="start", weight="400"):
    return (
        f'<text x="{x0:.1f}" y="{y0:.1f}" font-size="{size}" fill="{fill}" '
        f'text-anchor="{anchor}" font-weight="{weight}">{body}</text>'
    )


def line(x0, y0, x1, y1, *, stroke=INK, width=1.0):
    return (
        f'<line x1="{x0:.1f}" y1="{y0:.1f}" x2="{x1:.1f}" y2="{y1:.1f}" '
        f'stroke="{stroke}" stroke-width="{width}"/>'
    )


def draw() -> str:
    marks = []
    row = {
        "doc": 34,
        "ssh": 70,
        "loads": 150,
        "abandoned": 185,
        "server": 225,
        "duplicate": 262,
        "axis": 292,
    }

    marks.append(
        text(LEFT - 14, row["doc"] + 4, "commits to the mctb document", anchor="end")
    )
    for clock, sha in DOC_COMMITS:
        cx = x(clock)
        marks.append(
            f'<path d="M{cx:.1f},{row["doc"] - 6} l6,6 l-6,6 l-6,-6 z" fill="{INK}"/>'
        )
        marks.append(
            text(cx + 10, row["doc"] + 4, f"{clock} {sha}", size=12, fill=MUTED)
        )

    marks.append(
        text(LEFT - 14, row["ssh"] + 22, "SSH sessions from the user", anchor="end")
    )
    marks.append(
        text(
            LEFT - 14,
            row["ssh"] + 38,
            "machine (each line one login)",
            anchor="end",
            fill=MUTED,
            size=12,
        )
    )
    for lane, (login, logout) in enumerate(SESSIONS):
        y0 = row["ssh"] + lane * 9
        x0, x1 = x(login), x(logout or END)
        marks.append(line(x0, y0, x1, y0, stroke=MUTED, width=2.5))
        marks.append(line(x0, y0 - 3, x0, y0 + 3, stroke=INK, width=1.5))
        if logout:
            marks.append(line(x1, y0 - 4, x1, y0 + 4, stroke=ALERT, width=2))

    marks.append(
        text(LEFT - 14, row["loads"] + 4, "loads that reached the server", anchor="end")
    )
    for clock in PAGE_LOADS:
        cx = x(clock)
        marks.append(
            line(cx, row["loads"] - 7, cx, row["loads"] + 7, stroke=INK, width=1.5)
        )

    marks.append(
        text(
            LEFT - 14,
            row["abandoned"] + 4,
            "responses the client abandoned",
            anchor="end",
            fill=ALERT,
        )
    )
    for clock in ABANDONED:
        cx, cy = x(clock), row["abandoned"]
        marks.append(line(cx - 4, cy - 4, cx + 4, cy + 4, stroke=ALERT, width=2))
        marks.append(line(cx - 4, cy + 4, cx + 4, cy - 4, stroke=ALERT, width=2))

    marks.append(text(LEFT - 14, row["server"] + 4, "login-node server", anchor="end"))
    restart = x(RESTART)
    marks.append(
        line(LEFT, row["server"], restart, row["server"], stroke=ALERT, width=6)
    )
    marks.append(
        line(restart, row["server"], x(END), row["server"], stroke=CALM, width=6)
    )
    marks.append(
        text(
            LEFT + 4,
            row["server"] - 9,
            "code from 29 Sep, 190 reckon/ commits behind",
            size=12,
            fill=ALERT,
        )
    )
    marks.append(
        text(
            WIDTH - RIGHT,
            row["server"] - 9,
            "restarted 06:34:36",
            size=12,
            fill=CALM,
            anchor="end",
        )
    )

    marks.append(
        text(
            LEFT - 14,
            row["duplicate"] + 4,
            "duplicate on the compute node",
            anchor="end",
        )
    )
    d0, d1 = (x(c) for c in DUPLICATE)
    marks.append(
        line(d0, row["duplicate"], d1, row["duplicate"], stroke=MUTED, width=6)
    )
    marks.append(
        text(
            d0 + 4,
            row["duplicate"] - 9,
            "same unit, same log file, no clients",
            size=12,
            fill=MUTED,
        )
    )

    marks.append(line(LEFT, row["axis"], WIDTH - RIGHT, row["axis"], stroke=MUTED))
    for minute in range(seconds("05:30"), seconds(END) + 1, 600):
        clock = f"{minute // 3600:02d}:{minute % 3600 // 60:02d}"
        cx = x(clock)
        marks.append(line(cx, row["axis"], cx, row["axis"] + 5, stroke=MUTED))
        marks.append(
            text(cx, row["axis"] + 20, clock, size=12, fill=MUTED, anchor="middle")
        )

    height = row["axis"] + 32
    body = "\n  ".join(marks)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {height}" '
        f'width="{WIDTH}" height="{height}" font-family="system-ui, sans-serif">\n'
        f'  <rect width="{WIDTH}" height="{height}" fill="#ffffff"/>\n  {body}\n</svg>\n'
    )


if __name__ == "__main__":
    out = Path(__file__).with_name("timeline.svg")
    svg = draw()
    # A figure that does not parse as XML is not shipped; the input is the
    # string this script just generated, not untrusted data.
    ElementTree.fromstring(svg)  # noqa: S314
    out.write_text(svg, encoding="utf-8")
    print(f"wrote {out}")
