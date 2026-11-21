"""An evidence fragment's count column holds a count, not a revision.

A landing fragment records its arms in a table, and the columns people write are
``arm``, ``revision``, ``failed`` and ``result``. When the revision column is
dropped the sha does not disappear: it stays in the cell it was in, so the
figure now reads as the number of failures for that arm. One fragment was found
carrying a sha under the heading ``failed``, which is why this check exists.

The rule is narrow on purpose: a cell whose whole text is a commit id must not
sit under a heading that names a count. Every other fragment in the repository
already satisfies it, so the two cases stand against one rule — a synthesised
fragment the checker reports, and the committed corpus it stays quiet about.
"""

from __future__ import annotations

import re
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
FRAGMENT_ROOT = REPOSITORY / "docs" / "evidence" / "fragments"

# A commit id written whole in a cell.
COMMIT_ID = re.compile(r"^[0-9a-f]{40}$")
# A heading that names a count rather than a revision.
COUNT_HEADING = re.compile(
    r"^(failed|failure|failures|errors?|passed|count)$", re.IGNORECASE
)
_TABLE = re.compile(r"<table\b[^>]*>.*?</table>", re.DOTALL)
_HEADING = re.compile(r"<th\b[^>]*>(.*?)</th>", re.DOTALL)
_CELL = re.compile(r"<td\b[^>]*>(.*?)</td>", re.DOTALL)
_TAG = re.compile(r"<[^>]+>")


def _text(fragment: str) -> str:
    """Return the fragment's text with its markup removed and whitespace folded."""
    return " ".join(_TAG.sub(" ", fragment).split())


def _count_column_findings(root: Path) -> list[str]:
    """Return one line per cell in ``root`` that reads a commit id as a count."""
    findings: list[str] = []
    for path in sorted(root.rglob("*.html")):
        document = path.read_text(encoding="utf-8", errors="replace")
        for table in _TABLE.findall(document):
            headings = [_text(cell) for cell in _HEADING.findall(table)]
            if not headings:
                continue
            for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", table, re.DOTALL):
                for index, cell in enumerate(_text(c) for c in _CELL.findall(row)):
                    if index >= len(headings):
                        continue
                    if COMMIT_ID.match(cell) and COUNT_HEADING.match(headings[index]):
                        findings.append(
                            f"{path}: a commit id {cell!r} sits under the count "
                            f"heading {headings[index]!r}"
                        )
    return findings


def test_a_commit_id_under_a_count_heading_is_reported(tmp_path: Path) -> None:
    fragment = tmp_path / "plan" / "node.html"
    fragment.parent.mkdir(parents=True)
    fragment.write_text(
        "<table><thead><tr><th>arm</th><th>failed</th><th>result</th></tr></thead>"
        "<tbody><tr><td>base run</td>"
        f"<td><code>{'a' * 40}</code></td><td>1 passed</td></tr></tbody></table>",
        encoding="utf-8",
    )

    findings = _count_column_findings(tmp_path)
    assert len(findings) == 1
    assert "'failed'" in findings[0]


def test_no_committed_fragment_reads_a_commit_id_as_a_count() -> None:
    assert FRAGMENT_ROOT.is_dir()
    assert _count_column_findings(FRAGMENT_ROOT) == []
