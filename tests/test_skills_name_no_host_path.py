"""Skill references must name no path specific to one host or one checkout.

A skill is copied and run from whatever checkout a reader has. A command in one
that opens with an absolute home or work path runs only on the machine the path
was captured from — on any other host it resolves to nothing, and a reader has
no way to tell which checkout the author meant. The harness reference's two
hook install commands carried the author's own checkout prefix, so the copyable
invocation under them worked on exactly one workstation.

The scan covers every file under ``skills/`` rather than the one reference, so a
path captured into a skill later is caught by the same test. It is paired with a
check that the tree it walks is not empty, because a scan over nothing reports
an absence it never looked for.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
SKILLS = ROOT / "skills"

# An absolute path into a user's home or work tree. The leading boundary keeps a
# relative fragment such as ``docs/home`` or a URL path from matching: only a
# token that *starts* with ``/home/`` or ``/work/`` is an absolute host path.
ABSOLUTE_HOST_PATH = re.compile(r"(?<![\w./-])/(?:home|work)/")


def _skill_files() -> list[Path]:
    return sorted(path for path in SKILLS.rglob("*") if path.is_file())


def test_the_skills_tree_is_walked() -> None:
    """A scan over an empty tree would pass without checking anything."""
    files = _skill_files()
    assert files, f"no files found under {SKILLS}; the host-path scan is vacuous"
    assert any(path.name == "SKILL.md" for path in files)


def test_no_skill_file_names_an_absolute_host_path() -> None:
    offenders: list[str] = []
    for path in _skill_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if ABSOLUTE_HOST_PATH.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "a skill file names an absolute host path, which resolves only on the "
        "checkout it was captured from:\n" + "\n".join(offenders)
    )
