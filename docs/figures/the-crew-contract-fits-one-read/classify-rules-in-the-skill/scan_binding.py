"""Reproducible binding-sentence counter for skills/reckon-build/SKILL.md.

The fragment carries counts that must not drift from the text they describe, so
the numbers are derived from the file at run time rather than written down by
hand.

Method: fenced code blocks and headings are dropped (neither states a rule in
prose); blockquote markers, list markers, table pipes and inline emphasis are
neutralised so a sentence-final full stop inside bold or a code span still ends a
sentence; common abbreviations are protected; lines are then split on
sentence-final punctuation followed by whitespace, and each sentence is attributed
to the line its first character sits on. A sentence is a *binding candidate*.

Run:  python3 scan_binding.py [path-to-SKILL.md]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

KEYWORDS = re.compile(
    r"\b(must|never|always|do not|don't|only|is an|name its|refus\w*|mandatory"
    r"|non-negotiable|required|shall|no longer|cannot)\b",
    re.IGNORECASE,
)

# Sentence-opening verbs that read as an instruction to the coordinator or worker.
IMPERATIVE_VERBS = set(
    """
    add append apply arm ask attach audit avoid bring build call capture carry
    check choose cite claim classify clean clear close collapse commit compare
    complete confirm consider consult copy count cover create cut declare
    delegate delete derive dispatch dispose document drain drive drop end
    ensure enter escalate eschew exclude execute expand expect extract fail
    fetch file fix fold follow give halt hand hold honour include inspect
    install integrate invoke keep land launch lead leave let limit lock look
    make mark merge move name note observe open operate order own pack park
    pass pause perform pick place plan point post prefer prepare preserve
    promote proofread propose protect prove provide publish pull push put quote
    raise read reap re-arm record recover reduce refer refuse register release
    rely remove render reopen repeat replace report request require reserve
    reset resolve resort respect respond restart resume retry return reuse
    revert revisit rewrite route run satisfy scan schedule screen search seed
    send separate set settle ship show shrink sign skip split stage stall start
    stop store strike strip structure submit subscribe surface survive sweep
    switch tail take test throw trace track treat trigger trim trust turn
    update urge use validate verify wait wake walk want watch weigh wire write
    yield
    """.split()
)

ABBREVIATIONS = re.compile(r"\b(e\.g|i\.e|vs|cf|etc|Dr|Mr|Ms|Inc|No|alt)\.")


def clean(line: str) -> str:
    line = line.strip()
    line = re.sub(r"^>\s?", "", line)
    line = re.sub(r"^\s*[-*+]\s+", "", line)
    line = re.sub(r"^\s*\d+\.\s+", "", line)
    line = line.replace("|", " ")
    line = re.sub(r"[*_`]", "", line)
    return line.strip()


def sentences_with_lines(text: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    pending = ""
    pending_start = 0
    fenced = False
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if raw.lstrip().startswith("```"):
            fenced = not fenced
            if pending.strip():
                out.append((pending_start, pending.replace("<DOT>", ".").strip()))
            pending = ""
            continue
        if fenced:
            continue
        if raw.lstrip().startswith("#"):
            if pending.strip():
                out.append((pending_start, pending.replace("<DOT>", ".").strip()))
            pending = ""
            continue
        line = clean(raw)
        if not line:
            if pending.strip():
                out.append((pending_start, pending.replace("<DOT>", ".").strip()))
            pending = ""
            continue
        combined = f"{pending} {line}".strip() if pending else line
        protected = ABBREVIATIONS.sub(r"\1<DOT>", combined)
        parts = re.split(r"(?<=[.!?])\s+", protected)
        if len(parts) == 1:
            pending = combined
            if pending_start == 0:
                pending_start = lineno
            continue
        first_start = pending_start if pending else lineno
        for index, part in enumerate(parts[:-1]):
            if part.strip():
                start = first_start if index == 0 else lineno
                out.append((start, part.replace("<DOT>", ".").strip()))
        pending = parts[-1].replace("<DOT>", ".").strip()
        pending_start = lineno
    if pending.strip():
        out.append((pending_start, pending.replace("<DOT>", ".").strip()))
    return out


def is_candidate(sentence: str) -> bool:
    if KEYWORDS.search(sentence):
        return True
    first = re.match(r"([A-Za-z][A-Za-z-]*)", sentence)
    return bool(first) and first.group(1).lower() in IMPERATIVE_VERBS


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        Path(__file__).resolve().parents[4] / "skills" / "reckon-build" / "SKILL.md"
    )
    text = Path(path).read_text(encoding="utf-8")
    sentences = sentences_with_lines(text)
    candidates = [(ln, s) for ln, s in sentences if is_candidate(s)]
    print(f"file\t{path}")
    print(f"sentences\t{len(sentences)}")
    print(f"candidates\t{len(candidates)}")
    for ln, s in candidates:
        print(f"{ln}\t{s}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main()) 
