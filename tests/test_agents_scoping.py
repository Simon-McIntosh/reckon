"""Guard the root AGENTS.md as a thin index.

The root document is charged into every worker's context at session start, so
its length is a standing tax on all nodes. Reference material that only some
workers need has moved to the narrowest scope that owns it, loaded
automatically when work happens there; the root keeps cross-cutting
invariants plus a one-line pointer to each scope file. This module pins the
two consequences that would silently rot: the root may not regrow past its
token bound, and every pointer it publishes must name a file that actually
exists — a pointer to nothing is worse than no pointer.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# An AGENTS.md is loaded into every worker's context at session start, so its
# length is a per-node tax that scales with nothing. The 6,000-estimated-token
# ceiling keeps that standing instruction from eating a small context window
# before any file is opened: a lean dispatch prompt plus this page fits beside
# a focused test, leaving room for the work itself. Characters divided by 3.5
# is the same estimate the plan used to size this document.
ROOT_TOKEN_BOUND = 6_000

# The root's scope pointers: each names a sub-tree file that must exist. Add a
# line here if another scope file is ever split out.
ROOT_SCOPE_FILES = ("AGENTS.md", "reckon/crew/AGENTS.md", "docs/AGENTS.md")

# Every level 2/3 heading the root carried before the split. Each must appear
# in exactly one of the three AGENTS.md files afterwards — the split exists to
# relocate reference material, not to lose it.
CANONICAL_HEADINGS = {
    "Project",
    "Python",
    "Ruff compliance is the target state",
    "Tests",
    "A test must not encode the current date",
    "A test must not read or write state outside the repository under test",
    "Frontend",
    "Repo-agnostic principle",
    "HTML-first plans",
    "Skills you must use — never freelance",
    'Plan-state integrity (mandatory — fix for the "silent bypass" failure mode)',
    "Plan Lifecycle Invariants",
    "Where docs live and how the server works",
    "Server operations",
    "reckon MCP tools",
    "Forwarding the port (from a laptop)",
    "Decision capture model",
    "Mandatory one-line invocation (§05) on every followup",
    "Dissent flow (§07) — disagreeing with a locked decision",
    "Plan semantic data model",
    "CSS architecture",
    "Plan vocabulary (canonical, cross-repo)",
    "Typed archives and cumulative evidence",
    "Repo layout — one canonical format",
    "`index.json` schema (project config only)",
    "Runtime worker routing",
    "Crew command surface",
    "Fleet sizing",
    "What goes wrong without these skills",
}


def _estimated_tokens(text: str) -> float:
    return len(text) / 3.5


def _headings(text: str) -> set[str]:
    headings: set[str] = set()
    for line in text.splitlines():
        match = re.match(r"^(#{2,3}) (.*)$", line)
        if match:
            headings.add(match.group(2))
    return headings


def _scope_texts() -> dict[str, str]:
    return {f: (REPO_ROOT / f).read_text() for f in ROOT_SCOPE_FILES}


def test_root_stays_under_token_bound() -> None:
    root = (REPO_ROOT / "AGENTS.md").read_text()
    assert _estimated_tokens(root) < ROOT_TOKEN_BOUND


def test_root_pointers_name_files_that_exist() -> None:
    root = (REPO_ROOT / "AGENTS.md").read_text()
    # A pointer is a backticked path ending in .md, relative to the repo root.
    # Home-absolute (~/...) and rooted (/...) references are user-space paths,
    # and globs matching no single file (docs/*.md) are patterns, none of which
    # are scope pointers this document owns.
    for token in re.findall(r"`([^`]+)`", root):
        if not token.endswith(".md"):
            continue
        if token.startswith(("~", "/")) or "*" in token:
            continue
        assert (REPO_ROOT / token).exists(), (
            f"root pointer names a missing file: {token}"
        )


def test_every_canonical_heading_in_exactly_one_file() -> None:
    texts = _scope_texts()
    seats: dict[str, list[str]] = {}
    for filename, text in texts.items():
        for heading in _headings(text):
            seats.setdefault(heading, []).append(filename)
    for heading in CANONICAL_HEADINGS:
        holders = seats.get(heading, [])
        assert len(holders) == 1, (
            f"heading {heading!r} must appear in exactly one AGENTS.md, "
            f"found in {holders}"
        )


def test_scope_files_open_with_their_governed_tree() -> None:
    """Each new scope file names the sub-tree it governs in its opening lines."""
    expectations = {
        "reckon/crew/AGENTS.md": "reckon/crew",
        "docs/AGENTS.md": "docs",
    }
    for filename, subtree in expectations.items():
        head = (REPO_ROOT / filename).read_text().splitlines()[:6]
        assert any(subtree in line for line in head), (
            f"{filename} must open by naming the {subtree} sub-tree it governs"
        )
