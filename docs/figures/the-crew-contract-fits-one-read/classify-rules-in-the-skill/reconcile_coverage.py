"""Check that every scanner candidate is a row or a listed reject in the fragment.

The earlier coverage pass keyed candidates by line range, which silently collapsed
candidates that share a line; this check keys a row to a candidate by sentence text
instead, so a candidate with no row of its own cannot hide behind a sibling on the
same line.

Run:  python3 reconcile_coverage.py
"""

from __future__ import annotations

import html
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
SCAN = HERE / "scan_binding.py"
FRAGMENT = ROOT / "docs" / "evidence" / "fragments" / "the-crew-contract-fits-one-read" / "classify-rules-in-the-skill.html"


def tokens(text: str) -> list[str]:
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.findall(r"[A-Za-z0-9]+", text.lower())


def scan_candidates() -> list[tuple[int, str]]:
    out = subprocess.run([sys.executable, str(SCAN)], capture_output=True, text=True).stdout
    return [(int(l.split("\t", 1)[0]), l.split("\t", 1)[1].strip())
            for l in out.splitlines() if re.match(r"^\d+\t", l)]


def table_rows(fragment: str) -> list[str]:
    out = []
    for m in re.finditer(r"<tr>(.*?)</tr>", fragment, re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", m.group(1), re.S)
        if len(cells) >= 7:
            out.append(cells[1].split("<br>")[0])
    return out


def rejects(fragment: str) -> list[str]:
    i = fragment.find('id="rejected"')
    j = fragment.find("</ul>", i)
    return re.findall(r"<li>(.*?)</li>", fragment[i:j], re.S)


def contains(hay: list[str], needle: list[str]) -> bool:
    if not needle or len(needle) > len(hay):
        return False
    for k in range(len(hay) - len(needle) + 1):
        if hay[k:k + len(needle)] == needle:
            return True
    return False


def covered(row_tokens: list[list[str]], cand: list[str]) -> bool:
    head = cand[:10]
    for rt in row_tokens:
        if contains(rt, head):
            return True
        if contains(cand, rt[:10]):
            return True
    return False


def main() -> int:
    fragment = FRAGMENT.read_text(encoding="utf-8")
    row_tokens = [tokens(r) for r in table_rows(fragment)]
    reject_tokens = [tokens(r) for r in rejects(fragment)]

    cands = scan_candidates()
    missing = []
    for line, text in cands:
        c = tokens(text)
        if covered(row_tokens, c) or covered(reject_tokens, c):
            continue
        missing.append((line, text))
    print(f"candidates {len(cands)}  rows {len(row_tokens)}  rejects {len(reject_tokens)}")
    print(f"uncovered {len(missing)}")
    for line, text in missing:
        print(f"  {line}\t{text[:150]}")
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())