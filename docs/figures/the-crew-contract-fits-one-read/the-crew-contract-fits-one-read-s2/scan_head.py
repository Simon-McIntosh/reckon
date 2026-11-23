"""Scan the build skill and its references into prose sentences and binding rules.

One binding sentence gets one row. The method is fixed here so the fragment's
counts cannot drift from the text they describe, and so a later re-run at a
new head reproduces them.

Method
  * fenced code blocks, headings, table rows and raw HTML lines are dropped --
    none of them states a rule in prose;
  * blockquote and list markers are removed; inline emphasis and code ticks are
    stripped so a full stop inside them still ends a sentence;
  * common abbreviations are protected before splitting on sentence-final
    punctuation followed by whitespace;
  * each sentence is attributed to the file line its first character sat on;
  * a sentence is *binding* when it carries a strong modal (must, never,
    always, do not, refuse, shall, mandatory, no longer, cannot, required) or
    opens with an imperative verb from IMPERATIVE_VERBS.

Run:  python3 scan_head.py            # JSONL rows as: file, line, rule, binding
      python3 scan_head.py --counts   # per-file sentence/binding counts
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
SKILLS = REPO / "skills" / "reckon-build"

FILES = (
    "SKILL.md",
    "references/conditional-guidance.md",
    "references/coordinator-reference.md",
    "references/effort-routing.md",
    "references/lane-routing.md",
    "references/outage-recovery.md",
    "references/sprint-orchestration.md",
    "references/worker-backends.md",
    "references/worker-protocol.md",
    "references/worker-verification.md",
    "references/orchestrator-harness/claude-code.md",
    "references/orchestrator-harness/codex-cli.md",
)

KEYWORDS = re.compile(
    r"\b(must|never|always|do not|don't|refus\w*|shall|mandatory|non-negotiable"
    r"|required|no longer|cannot|may not|instead of|is the only|only the)\b",
    re.IGNORECASE,
)

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

ABBREVIATIONS = re.compile(r"\b(e\.g|i\.e|vs|cf|etc|Dr|Mr|Ms|Inc|No)\.")


def is_prose(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    if s.startswith("```") or s.startswith("#"):
        return False
    if s.startswith("|") or s.startswith("<"):
        return False
    return True


def clean(line: str) -> str:
    line = line.strip()
    line = re.sub(r"^>\s?", "", line)
    line = re.sub(r"^\s*[-*+]\s+", "", line)
    line = re.sub(r"^\s*\d+\.\s+", "", line)
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
        if not is_prose(raw):
            if pending.strip():
                out.append((pending_start, pending.replace("<DOT>", ".").strip()))
            pending = ""
            continue
        line = clean(raw)
        if not_empty(line):
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


def not_empty(s: str) -> bool:
    return bool(s.strip())


def is_binding(sentence: str) -> bool:
    if KEYWORDS.search(sentence):
        return True
    first = re.match(r"([A-Za-z][A-Za-z-]*)", sentence)
    return bool(first) and first.group(1).lower() in IMPERATIVE_VERBS


def scan():
    total = 0
    rows = []
    for rel in FILES:
        text = (SKILLS / rel).read_text(encoding="utf-8")
        sents = sentences_with_lines(text)
        total += len(sents)
        for line, sentence in sents:
            if is_binding(sentence):
                rows.append(
                    {
                        "file": f"skills/reckon-build/{rel}",
                        "line": line,
                        "rule": sentence,
                    }
                )
    return total, rows


def main() -> int:
    total, rows = scan()
    if "--counts" in sys.argv:
        per: dict[str, list[int]] = {}
        for rel in FILES:
            text = (SKILLS / rel).read_text(encoding="utf-8")
            sents = sentences_with_lines(text)
            per[f"skills/reckon-build/{rel}"] = [
                len(sents),
                sum(1 for _, s in sents if is_binding(s)),
            ]
        print(f"total_sentences\t{total}")
        print("file\tsentences\tbinding")
        for name, (s, b) in per.items():
            print(f"{name}\t{s}\t{b}")
        print(f"TOTAL\t{total}\t{len(rows)}")
        return 0
    for r in rows:
        print(json.dumps(r))
    print(f"# total_sentences={total} binding_rows={len(rows)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())