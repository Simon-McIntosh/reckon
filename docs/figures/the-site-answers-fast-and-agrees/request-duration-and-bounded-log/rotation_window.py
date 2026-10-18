"""Draw the interleaving a log rotation must not lose an access line to.

A rotation copies the over-bound live file to one predecessor and then empties
the live file. A line written between those two steps is in the predecessor only
if the copy already passed its offset, and in the emptied file not at all, so it
is lost. The figure is a schematic of that window and of the lock that closes
it: the rotator holds the lock across the copy and the empty, and an access line
written in the interval waits for it instead of landing in the window.

The axis is order, not a measured duration — no request was timed for this
figure — so it carries no scale and the segments are named.

Run: python rotation_window.py  (writes rotation_window.svg beside this file)
"""

from pathlib import Path

WIDTH, HEIGHT = 1400, 440
X0, X_ATTEMPT, X_COPY_END, X_LOCK_FREE, X_KEPT = 300, 520, 760, 860, 900
ROW_ROTATOR, ROW_LOCKED, ROW_UNLOCKED = 130, 230, 330
BAR = 26
INK, MUTED, KEPT, LOST = "#1f2328", "#929292", "#1a7f37", "#b42318"
TEXTS: list[tuple[float, float, str, int, str]] = []


def text(x, y, body, *, size=16, fill=INK, anchor="start"):
    TEXTS.append((x, y, body, size, anchor))
    return (
        f'<text x="{x}" y="{y}" font-size="{size}" fill="{fill}" '
        f'text-anchor="{anchor}">{body}</text>'
    )


def bar(x0, x1, y, *, fill, caption=None, caption_fill="#ffffff"):
    shape = (
        f'<rect x="{x0}" y="{y - BAR / 2}" width="{x1 - x0}" height="{BAR}" '
        f'fill="{fill}" />'
    )
    if caption:
        shape += text((x0 + x1) / 2, y + 5, caption)
    return shape


def cross(x, y, *, fill):
    d = 11
    return (
        f'<path d="M {x - d} {y - d} L {x + d} {y + d} M {x - d} {y + d} '
        f'L {x + d} {y + d - 2 * d}" stroke="{fill}" stroke-width="4" />'
    )


def build() -> str:
    axis = (WIDTH - 260, ROW_UNLOCKED + 60)
    body = [
        (
            f'<line x1="{X0}" y1="{axis[1]}" x2="{axis[0]}" y2="{axis[1]}" '
            f'stroke="{MUTED}" stroke-width="1.2" />'
        ),
        text(axis[0], axis[1] + 28, "time", size=14, fill=MUTED, anchor="end"),
        (
            f'<line x1="{X_LOCK_FREE}" y1="{ROW_ROTATOR - 46}" x2="{X_LOCK_FREE}" '
            f'y2="{ROW_UNLOCKED + 40}" stroke="{MUTED}" stroke-width="1.2" '
            f'stroke-dasharray="4 4" />'
        ),
        text(X_LOCK_FREE + 12, ROW_ROTATOR - 54, "lock released", size=14, fill=MUTED),
        text(X0 - 24, ROW_ROTATOR + 5, "rotation thread", size=15, anchor="end"),
        text(
            X0 - 24, ROW_LOCKED + 5, "access line, shared lock", size=15, anchor="end"
        ),
        text(X0 - 24, ROW_UNLOCKED + 5, "access line, no lock", size=15, anchor="end"),
        bar(
            X0,
            X_COPY_END - 26,
            ROW_ROTATOR,
            fill=MUTED,
            caption="copy the over-bound file",
        ),
        cross(X_COPY_END, ROW_ROTATOR, fill=INK),
        text(X_COPY_END - 150, ROW_ROTATOR + 46, "empty the live file", size=14),
        text(X_ATTEMPT, ROW_LOCKED - 42, "write attempted", size=14, anchor="middle"),
        bar(
            X_ATTEMPT, X_LOCK_FREE, ROW_LOCKED, fill=MUTED, caption="waits on the lock"
        ),
        cross(X_ATTEMPT, ROW_LOCKED, fill=INK),
        f'<circle cx="{X_KEPT}" cy="{ROW_LOCKED}" r="9" fill="{KEPT}" />',
        text(
            X_KEPT + 18,
            ROW_LOCKED + 5,
            "written after the empty — kept",
            size=14,
            fill=KEPT,
        ),
        cross(X_ATTEMPT, ROW_UNLOCKED, fill=LOST),
        text(
            X_ATTEMPT + 22,
            ROW_UNLOCKED + 5,
            "written into the window — lost to the empty",
            size=14,
            fill=LOST,
        ),
    ]
    return "\n".join(body)


def check() -> None:
    boxes = []
    for x, y, body, size, anchor in TEXTS:
        if "<" in body or "&" in body:
            raise SystemExit(f"a label carries markup: {body!r}")
        width = len(body) * size * 0.55
        left = (
            x - width if anchor == "end" else x - width / 2 if anchor == "middle" else x
        )
        box = (left, y - size, left + width, y + size * 0.3)
        if box[0] < 0 or box[2] > WIDTH or box[1] < 0 or box[3] > HEIGHT:
            raise SystemExit(f"text off the canvas: {body!r} at {box}")
        boxes.append((box, body))
    for i, (a, abody) in enumerate(boxes):
        for b, bbody in boxes[i + 1 :]:
            if a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]:
                raise SystemExit(f"labels overlap: {abody!r} and {bbody!r}")


def main() -> None:
    body = build()
    check()
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" '
        f'height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}">\n{body}\n</svg>\n'
    )
    out = Path(__file__).with_suffix(".svg")
    out.write_text(svg, encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
