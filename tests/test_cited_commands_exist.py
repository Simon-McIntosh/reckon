"""Every ``reckon`` command a skill or format document cites must exist.

The instruction corpus a coordinator or worker follows carries ``reckon``
invocations in backtick spans and fenced code blocks. Nothing checks that the
verbs and flags those invocations name still resolve, so a rename or a removal
leaves the prose pointing at a command that exits non-zero and the drift is
found only by reading. :func:`citations` extracts every such invocation, and
:func:`test_every_cited_command_and_flag_resolves` resolves each verb chain and
matches each flag against the command's own ``--help`` as a whole option token,
so a flag that is only a prefix of a longer option does not resolve.

Two kinds of text are exempt because they are not drift:

* a citation the surrounding sentence marks as planned and binds to a plan
  (``planned in `<slug>```), which is a forward reference rather than a claim;
* text where ``reckon`` is not invoked at all — ``a reckon SPA``, a path, or an
  identifier. A standalone ``reckon`` word is required, and a citation must sit
  at the head of its span or behind a shell launcher (``uv run``), never behind
  a sentence preposition such as ``from``.
"""

from __future__ import annotations

import re
from pathlib import Path

from click.testing import CliRunner

from reckon.cli import main

ROOT = Path(__file__).resolve().parents[1]

#: A standalone ``reckon`` word: not ``reckon-build``, ``reckon.py``,
#: ``reckon/crew`` or any longer identifier.
_WORD = re.compile(r"(?<![\w./-])reckon(?![\w./-])")

#: A shell launcher that may precede a citation at the head of a span.
_LAUNCHER = re.compile(
    r"^\s*(?:\$\s*|>\s*)?(?:uv|sudo|python3?|command|env|time|nohup)\b"
)

#: One command word: lowercase, hyphenated, no placeholder punctuation.
_CMD = re.compile(r"^[a-z][a-z0-9-]*$")

_FLAG = re.compile(r"(?<![\w-])(--[a-z][a-z0-9-]*)")

#: A sentence that marks a citation as planned and binds it to a plan slug.
_PLANNED = re.compile(r"planned in `[a-z0-9][a-z0-9-]*`")

_CORPUS_GLOBS = ("skills/**/*.md", "reckon/crew/prompts/*.md")


def corpus(root: Path) -> list[Path]:
    """The instruction files whose ``reckon`` citations are checked.

    Recursive by design: a reference nested under ``references/`` is as binding
    as the skill body that links it, so the walk reaches both and a citation is
    checked wherever it is filed.
    """
    found: list[Path] = []
    for pattern in _CORPUS_GLOBS:
        found += root.glob(pattern)
    found.append(root / "PLAN-FORMAT.md")
    found += [p for p in root.rglob("AGENTS.md") if ".venv" not in p.parts]
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in sorted(found):
        if path.is_file() and path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def _spans(text: str):
    """Yield ``(line_no, span)`` candidates from backticks and fenced lines.

    Inline backtick runs are matched across line breaks (a citation may wrap);
    fenced code lines are read whole. ``reckon`` occurrences inside fenced
    regions are not re-read as inline spans, because that text is already seen
    line by line.
    """
    lines = text.splitlines()
    fenced = [False] * len(lines)
    infence = False
    for i, line in enumerate(lines):
        if line.strip().startswith("```"):
            fenced[i] = True
            infence = not infence
            continue
        fenced[i] = infence

    masked = "\n".join("" if fenced[i] else line for i, line in enumerate(lines))
    for match in re.finditer(r"`([^`]*)`", masked, re.DOTALL):
        span = match.group(1)
        if _WORD.search(span):
            yield masked[: match.start()].count("\n") + 1, span

    for i, line in enumerate(lines, 1):
        if fenced[i - 1] and _WORD.search(line):
            yield i, line


def _candidates(text: str):
    """Yield ``(line_no, chain, flags, prefix, is_head)`` for one file's text.

    A span is split at every standalone ``reckon`` so two commands on one line
    are two candidates. ``prefix`` is the text before the word within its span,
    and ``is_head`` marks the head candidate — the only position a prose
    preposition can occupy.
    """
    for line_no, span in _spans(text):
        hits = list(_WORD.finditer(span))
        for index, hit in enumerate(hits):
            end = hits[index + 1].start() if index + 1 < len(hits) else len(span)
            segment = span[hit.end() : end]
            chain = _chain(segment)
            if not chain:
                continue
            yield (
                line_no,
                chain,
                _flags(segment),
                span[: hit.start()],
                index == 0,
                segment,
            )


def _chain(segment: str) -> list[str]:
    chain: list[str] = []
    for raw in segment.split():
        token = raw.strip("`,;|&()[]")
        if _CMD.match(token):
            chain.append(token)
        else:
            break
    return chain


def _flags(segment: str) -> list[str]:
    return sorted(set(_FLAG.findall(segment)))


class Citation:
    """One ``reckon`` invocation named in the corpus, with where it was seen."""

    __slots__ = ("chain", "flags", "line", "path", "planned")

    def __init__(self, path, line, chain, flags, planned):
        self.path = path
        self.line = line
        self.chain = chain
        self.flags = flags
        self.planned = planned

    @property
    def where(self) -> str:
        return f"{self.path}:{self.line}"

    def __repr__(self) -> str:
        return f"<Citation {self.where} {' '.join(self.chain)} {self.flags}>"


def citations(root: Path = ROOT) -> list[Citation]:
    """Every ``reckon`` command citation in the corpus, planned ones included."""
    out: list[Citation] = []
    for path in corpus(root):
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        rel = path.relative_to(root)
        for line_no, chain, flags, prefix, is_head, _segment in _candidates(text):
            if is_head and prefix.strip() and not _LAUNCHER.match(prefix):
                continue
            planned = bool(_PLANNED.search(lines[line_no - 1]))
            out.append(Citation(str(rel), line_no, chain, flags, planned))
    return out


def _tree():
    return main


def _resolve(chain: list[str]):
    """Walk ``chain`` down the click command tree.

    Returns ``(command, bad_token)``. ``bad_token`` is set when a token lands in
    the subcommand position of a group but names no subcommand — the defect this
    guard exists to catch. A token read after a leaf is an argument, never a
    verb, so a chain longer than the command path is not itself an error.
    """
    node = _tree()
    for token in chain:
        subs = getattr(node, "commands", None)
        if not subs:
            break
        if token not in subs:
            return None, token
        node = subs[token]
    return node, None


def _help(path: list[str]) -> str:
    result = CliRunner().invoke(main, [*path, "--help"])
    return result.output


def _help_has_flag(help_text: str, flag: str) -> bool:
    """True when ``flag`` appears in ``help_text`` as a whole option token.

    A cited flag resolves only when the option name stands alone in the help
    body: ``--time`` is not satisfied by ``--timeout``, because the option name
    is bounded by anything that could extend it on either side.
    """
    return re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", help_text) is not None


def _flag_problems(cite: Citation, help_text: str) -> list[str]:
    """The flags of ``cite`` that ``help_text`` does not offer as whole tokens."""
    return [
        f"{cite.where}: {' '.join(cite.chain)} has no {flag} in --help"
        for flag in cite.flags
        if not _help_has_flag(help_text, flag)
    ]


def test_every_cited_command_and_flag_resolves():
    found = citations()
    checked = [c for c in found if not c.planned]
    problems: list[str] = []

    for cite in checked:
        command, bad = _resolve(cite.chain)
        if bad is not None:
            problems.append(
                f"{cite.where}: unknown command {' '.join(cite.chain)} ({bad!r})"
            )
            continue
        if cite.flags and command is not None:
            help_text = _help(cite.chain[: _path_len(cite.chain)])
            problems.extend(_flag_problems(cite, help_text))

    print(
        f"citations={len(found)} checked={len(checked)} "
        f"planned={len(found) - len(checked)} problems={len(problems)}"
    )
    for problem in problems:
        print("  " + problem)
    assert not problems, f"{len(problems)} cited command(s) do not resolve"


def _path_len(chain: list[str]) -> int:
    node = _tree()
    used = 0
    for token in chain:
        subs = getattr(node, "commands", None)
        if subs and token in subs:
            node = subs[token]
            used += 1
        else:
            break
    return used


def test_a_cited_flag_is_matched_as_a_whole_option_token():
    """A cited flag that is only a substring of a real flag is reported.

    ``crew dispatch`` offers ``--project`` but no ``--proj``, so a citation of
    ``--proj`` must be named as a problem even though the option name occurs
    inside a longer one. The flag arm resolves whole tokens, not substrings.
    """
    help_text = _help(["crew", "dispatch"])
    real = max(sorted(_FLAG.findall(help_text)), key=len)
    substring = real[:-1]
    cite = Citation("skills/example/SKILL.md", 1, ["crew", "dispatch"], [substring], False)

    assert _help_has_flag(help_text, real)
    assert not _help_has_flag(help_text, substring)
    problems = _flag_problems(cite, help_text)
    assert problems, f"{substring} is inside {real} yet was reported as resolving"
    assert substring in problems[0]
