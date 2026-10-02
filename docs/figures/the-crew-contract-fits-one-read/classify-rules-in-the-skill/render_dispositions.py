"""Render the disposition figure from the classification table itself.

The fragment states counts that the table carries, so the figure is drawn from
the table rather than transcribed: run this after editing the fragment.

Run:  python3 render_dispositions.py
"""

from __future__ import annotations

import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
FRAGMENT = ROOT / "docs" / "evidence" / "fragments" / "the-crew-contract-fits-one-read" / "classify-rules-in-the-skill.html"
SVG = HERE / "dispositions.svg"

LABELS = (
    ("keep", "keep"),
    ("merge", "merge into a surviving row"),
    ("delete-as-enforced", "delete as enforced"),
)
FILLS = {"keep": "#3b6ea5", "merge": "#c98a2b", "delete-as-enforced": "#4a4a4a"}


def counts(fragment: str) -> dict[str, int]:
    out = {"keep": 0, "merge": 0, "delete-as-enforced": 0}
    for m in re.finditer(r"<tr>(.*?)</tr>", fragment, re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", m.group(1), re.S)
        if len(cells) < 7:
            continue
        disposition = re.sub(r"<[^>]+>", "", cells[5])
        disposition = disposition.replace("&quot;", '"').strip()
        if disposition.startswith("merge"):
            out["merge"] += 1
        elif disposition == "keep":
            out["keep"] += 1
        elif disposition.startswith("delete as enforced"):
            out["delete-as-enforced"] += 1
        else:
            raise SystemExit(f"unrecognised disposition: {disposition!r}")
    return out


def main() -> int:
    text = FRAGMENT.read_text(encoding="utf-8")
    n = counts(text)
    total = sum(n.values())
    width, height = 720, 300
    left, span = 250, 434
    scale = span / max(n.values())
    rows = []
    y = 66
    for key, label in LABELS:
        value = n[key]
        rows.append(
            f'<text x="238" y="{y + 16}" text-anchor="end" font-family="sans-serif" font-size="13" fill="#222">{label}</text>\n'
            f'<rect x="{left}" y="{y}" width="{scale * value:.1f}" height="26" fill="{FILLS[key]}" rx="2"/>\n'
            f'<text x="{left + scale * value + 8:.1f}" y="{y + 18}" font-family="sans-serif" font-size="13" fill="#222">{value}</text>'
        )
        y += 56
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Binding-sentence dispositions: keep {n["keep"]}, merge {n["merge"]}, '
        f'delete as enforced {n["delete-as-enforced"]}, over {total} rows">\n'
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>\n'
        f'<text x="20" y="30" font-family="sans-serif" font-size="16" fill="#111">'
        f'skills/reckon-build/SKILL.md — {total} rows</text>\n'
        f'<text x="20" y="48" font-family="sans-serif" font-size="12" fill="#555">'
        f'539 sentences, 261 candidates at head 5670458ab; every candidate is a row or a listed reject</text>\n'
        + "\n".join(rows)
        + f'\n<line x1="{left}" y1="54" x2="{left}" y2="{y - 22}" stroke="#ccc"/>\n</svg>\n'
    )
    SVG.write_text(svg, encoding="utf-8")
    print(f"wrote {SVG} with {n} over {total} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())