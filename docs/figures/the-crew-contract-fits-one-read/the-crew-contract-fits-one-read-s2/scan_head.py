"""Walk the twelve build-skill files into prose sentences and binding rules.

The vocabulary is not defined here. The binding keyword set, the imperative
verb table, the abbreviation guard and the line cleaner all come from
``classify-rules-in-the-skill/scan_binding.py``, the module that already owns
them. This module widens that owner's corpus from ``SKILL.md`` to the skill and
its references, and supplies the sentence walk with a corrected locator: a
sentence is attributed to the line its first character sits on, not to the
line where a split happened to land.

Why the walk is here rather than imported: the owner's walk cannot serve this
corpus. It treats every non-fenced line as prose, so table rows and raw HTML
become sentences, while this page counts prose only. Its locator also points a
sentence at the line a split landed on. So the walk is local, and this module
takes the parts that define a rule -- the keyword set, the imperative verb
table, the abbreviation guard and the cleaner -- from the one owner that
defines them.

Run:  python3 scan_head.py            # JSONL rows as: file, line, rule
      python3 scan_head.py --counts   # per-file sentence/binding counts
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

OWNER = Path(__file__).resolve().parents[1] / "classify-rules-in-the-skill"
if str(OWNER) not in sys.path:
    sys.path.insert(0, str(OWNER))

from scan_binding import (  # noqa: E402
    ABBREVIATIONS,
    IMPERATIVE_VERBS,
    KEYWORDS,
    clean,
    is_candidate,
)

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

SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def is_prose(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    if s.startswith("```") or s.startswith("#"):
        return False
    if s.startswith("|") or s.startswith("<"):
        return False
    return True


def _protect_abbreviations(line: str) -> str:
    """Mask an abbreviation's full stop with a one-character non-sentence mark."""
    return ABBREVIATIONS.sub(lambda match: match.group(0)[:-1] + "\x00", line)


def sentences_with_lines(text: str) -> list[tuple[int, str]]:
    """Sentences with the source line their first character sits on."""
    out: list[tuple[int, str]] = []
    block: list[tuple[int, str]] = []

    def flush() -> None:
        nonlocal block
        if not block:
            return
        buf = ""
        owners: list[int] = []
        for lineno, line in block:
            if buf:
                buf += " "
                owners.append(lineno)
            owners.extend([lineno] * len(line))
            buf += line
        protected = _protect_abbreviations(buf)
        spans: list[tuple[int, int]] = []
        last = 0
        for match in SENTENCE_END.finditer(protected):
            spans.append((last, match.start()))
            last = match.end()
        spans.append((last, len(buf)))
        for start, end in spans:
            while start < end and buf[start].isspace():
                start += 1
            while end > start and buf[end - 1].isspace():
                end -= 1
            if start < end:
                sentence = buf[start:end]
                out.append((owners[start], sentence.replace("\x00", ".")))
        block = []

    fenced = False
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if raw.lstrip().startswith("```"):
            flush()
            fenced = not fenced
            continue
        if fenced or not is_prose(raw):
            flush()
            continue
        line = clean(raw)
        if line:
            block.append((lineno, line))
    flush()
    return out


def is_binding(sentence: str) -> bool:
    return is_candidate(sentence)


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