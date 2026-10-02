"""impl is derived only when the records cover the plan's declared sections."""

from __future__ import annotations

import html as html_module
import json
from pathlib import Path

import pytest

from reckon._plan_html import parse_meta, parse_plan, read_state

AUTHORED = 0.57
DECLARATIONS = {
    "first": "done",
    "second": "done",
    "third": "done",
    "fourth": "implementable",
}
DONE = [("first", 4), ("second", 3), ("third", 2)]
IMPLEMENTABLE = ("fourth", 6)
RECORD = (
    '<section data-reckon="section" data-id="{sid}" data-effort-hours="{hours}"'
    ' data-capability-version="1.0" data-capability-class="general"'
    ' data-capability-reasoning="standard" data-capability-verification="standard"'
    ' data-capability-risk="low" data-attempts="0" data-status="{status}"'
    ' data-links=""></section>'
)


def _plan(records: list[tuple[str, int]]) -> str:
    status = dict(DECLARATIONS)
    carried = dict(records)
    body = []
    for sid in DECLARATIONS:
        body.append(f'<h2 id="{sid}">The {sid} work</h2>')
        if sid in carried:
            body.append(RECORD.format(sid=sid, hours=carried[sid], status=status[sid]))
    declarations = html_module.escape(json.dumps(DECLARATIONS), quote=True)
    head = (
        '<!doctype html>\n<html lang="en"><head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="docs-project" content="sample">\n'
        '<meta name="reckon-type" content="plan">\n'
        '<meta name="plan-slug" content="partial">\n'
        '<meta name="plan-title" content="Partial coverage">\n'
        f'<meta name="plan-impl" content="{AUTHORED}">\n'
        f'<meta name="plan-section-declarations" content="{declarations}">\n'
        "</head><body><main>\n"
    )
    return head + "\n".join(body) + "\n</main></body></html>\n"


def _write(tmp_path: Path, slug: str, text: str) -> Path:
    path = tmp_path / "repo" / "docs" / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_a_declared_section_without_a_record_keeps_the_authored_impl(tmp_path):
    text = _plan([IMPLEMENTABLE])
    path = _write(tmp_path, "partial", text)

    state = read_state(text)
    assert state["impl"] == pytest.approx(AUTHORED)
    assert state["impl_source"] == "authored"
    assert parse_plan(path)["impl"] == pytest.approx(AUTHORED)
    assert parse_plan(path)["impl_source"] == "authored"
    assert parse_meta(path)["impl"] == pytest.approx(AUTHORED)
    assert parse_meta(path)["impl_source"] == "authored"
    assert path.read_text(encoding="utf-8") == text


def test_records_covering_every_declared_section_derive_the_figure(tmp_path):
    text = _plan([*DONE, IMPLEMENTABLE])
    path = _write(tmp_path, "complete", text)

    expected = 9 / 15
    state = read_state(text)
    assert state["impl"] == pytest.approx(expected)
    assert state["impl_source"] == "computed"
    assert parse_plan(path)["impl"] == pytest.approx(expected)
    assert parse_meta(path)["impl"] == pytest.approx(expected)
    assert parse_meta(path)["impl_source"] == "computed"
    assert path.read_text(encoding="utf-8") == text
