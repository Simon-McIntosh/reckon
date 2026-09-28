"""The reproducible sentence counter for the nine reference files.

The counts are derived from the files at run time rather than written down, so
the fragment cannot carry a figure that has drifted from the text it describes.
Import COUNTS, or run this module to print the table.

Method: fenced code blocks and headings are dropped (neither carries a rule
stated in prose), table pipes and list markers become spaces, common
abbreviations are protected, and the remainder is split on sentence-final
punctuation followed by whitespace.
"""

from __future__ import annotations

import re
from pathlib import Path

REFERENCES = Path(__file__).resolve().parents[4] / "skills" / "reckon-build" / "references"

FILES = (
    "conditional-guidance.md",
    "effort-routing.md",
    "lane-routing.md",
    "outage-recovery.md",
    "worker-backends.md",
    "worker-protocol.md",
    "worker-verification.md",
    "orchestrator-harness/claude-code.md",
    "orchestrator-harness/codex-cli.md",
)

ABBREVIATIONS = r"\b(e\.g|i\.e|vs|cf|etc|Dr|Mr|Ms|Inc|No)\."


def strip_fences(text: str) -> str:
    kept: list[str] = []
    fenced = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            kept.append(line)
    return "\n".join(kept)


def sentences(text: str) -> list[str]:
    text = strip_fences(text)
    text = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    text = text.replace("|", " ")
    text = re.sub(r"^\s*[-*\d]+\.?\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(ABBREVIATIONS, r"\1<DOT>", text)
    parts = re.split(r"(?<=[.!?])\s+", text)
    return [p.replace("<DOT>", ".").strip() for p in parts if p.strip()]


def count_sentences(path: Path) -> int:
    return len(sentences(path.read_text(encoding="utf-8")))


COUNTS = {name: count_sentences(REFERENCES / name) for name in FILES}


if __name__ == "__main__":
    for name in FILES:
        print(f"{name}\t{COUNTS[name]}")
    print("TOTAL\t", sum(COUNTS.values()))