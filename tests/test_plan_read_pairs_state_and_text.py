"""A plan read takes its state and its text from one read of the file.

``_store._read_state`` derives the unparsed-section diagnostics from both the
parsed state and the HTML text. When the two come from separate memoised
lookups, each keyed on its own stat of the file, a write landing between them
pairs one file's state with another file's text — and the diagnostics then
report authored children of a section the stale state shows empty. These tests
drive a write into the middle of a read and assert the two halves agree.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from reckon import _plan_html, _store, file_memo

PROJECT = "read-pairs-sample"
SLUG = "paired-target"


def _plan(title: str, gate_section: str = "") -> str:
    return f"""<!doctype html>
<html><head>
<meta name="docs-project" content="{PROJECT}">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="{SLUG}">
<meta name="plan-title" content="{title}">
<meta name="plan-status" content="active">
<meta name="plan-summary" content="paired read target">
<meta name="plan-version" content="1">
</head><body><h1>{title}</h1>{gate_section}</body></html>
"""


# A gate section holding a child the parser does not recognise: the parsed
# collection stays empty while the text carries an authored element, which is
# exactly the mismatch the unparsed-section diagnostic reports.
_UNPARSED_GATE = (
    '<section data-reckon="gates"><div class="legacy-gate">x</div></section>'
)


@pytest.fixture(autouse=True)
def _clear_file_memo() -> None:
    file_memo.clear()


def _write(root: Path, text: str) -> Path:
    path = root / "docs" / "plans" / f"{SLUG}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _rewriting_text_read(generations: list[str], calls: list[int], real):
    """A text read that rewrites the file to the next generation first.

    Each successive call sees different bytes, modelling a write that lands
    between the two halves of one read.
    """

    def rewriting(path: Path) -> str:
        n = calls[0]
        calls[0] += 1
        path.write_text(generations[min(n, len(generations) - 1)], "utf-8")
        return real(path)

    return rewriting


def _pinned_resolver(path: Path):
    return lambda *args, **kwargs: path


def test_read_state_and_text_agree_across_a_mid_read_write(tmp_path: Path) -> None:
    """The diagnostics must not report a section the paired state also holds."""
    path = _write(tmp_path, _plan("generation one"))
    calls = [0]
    generations = [_plan("generation one"), _plan("generation two", _UNPARSED_GATE)]

    with (
        patch.object(_store, "_resolve_html_file", _pinned_resolver(path)),
        patch.object(
            _plan_html,
            "_read_plan_text",
            _rewriting_text_read(generations, calls, _plan_html._read_plan_text),
        ),
    ):
        state, _version = _store._read_state(PROJECT, SLUG, root=tmp_path)

    warnings = [
        w for w in (state.get("compatibility_warnings") or []) if "authored child" in w
    ]
    assert warnings == []


def test_state_and_text_are_one_read(tmp_path: Path) -> None:
    """A single call returns state and the text that state was parsed from."""
    path = _write(tmp_path, _plan("first"))
    calls = [0]
    generations = [_plan("first"), _plan("second", _UNPARSED_GATE)]

    with patch.object(
        _plan_html,
        "_read_plan_text",
        _rewriting_text_read(generations, calls, _plan_html._read_plan_text),
    ):
        state, text = _plan_html.read_state_and_text_file(path)

    assert calls[0] == 1
    assert state == _plan_html.read_state(text)


def test_warm_read_parses_each_file_once(tmp_path: Path) -> None:
    path = _write(tmp_path, _plan("stable"))
    original = Path.read_text
    reads: list[Path] = []

    def counted(candidate: Path, *args, **kwargs) -> str:
        if candidate == path:
            reads.append(candidate)
        return original(candidate, *args, **kwargs)

    with patch.object(Path, "read_text", counted):
        first, _ = _store._read_state(PROJECT, SLUG, root=tmp_path)
        second, _ = _store._read_state(PROJECT, SLUG, root=tmp_path)

    assert reads == [path]
    assert first["title"] == second["title"] == "stable"
