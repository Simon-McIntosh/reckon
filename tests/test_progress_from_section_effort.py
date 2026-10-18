"""A plan's progress is its declared sections' done share, weighted by effort.

The denominator is the sections declared done or implementable; deferred
sections sit outside it. Each section weighs the effort its record carries, and
a declared section whose hours no record states takes the mean of the hours
that are present. Only the buildable sections' hours enter that mean: a
deferred section sits outside the denominator, so its recorded hours do not
weigh a buildable section that states none. When no section carries hours every
section weighs the same, so the figure is the count over the declarations. A
plan with no declarations keeps its authored ``plan-impl``.

Six cases are covered: every section recorded with hours, a partial set of
hours, a deferred section whose hours stay out of the imputed mean, no hours at
all, no declarations, and declarations with no records. The last also pins the
head-only reader (the discovery rows) to the parse, so a plan declared but not
yet recorded serves the same figure from both readers.
"""

from __future__ import annotations

import html as html_module
import json
from pathlib import Path

import pytest

from reckon._plan_html import parse_meta, parse_plan, read_state

RECORD = (
    '<section data-reckon="section" data-id="{sid}" data-effort-hours="{hours}"'
    ' data-capability-version="1.0" data-capability-class="general"'
    ' data-capability-reasoning="standard" data-capability-verification="strict"'
    ' data-capability-risk="low" data-attempts="0" data-status="{status}"'
    ' data-links=""></section>'
)


def _plan_text(
    *,
    declarations: dict | None,
    records: list[tuple[str, float, str]],
    impl: str | None,
) -> str:
    body: list[str] = []
    for sid, hours, status in records:
        body.append(f'<h2 id="{sid}">The {sid} work</h2>')
        body.append(RECORD.format(sid=sid, hours=hours, status=status))
    recorded_ids = {sid for sid, _, _ in records}
    body.extend(
        f'<h2 id="{sid}">The {sid} work</h2>'
        for sid in (declarations or {})
        if sid not in recorded_ids
    )
    head = [
        '<!doctype html>\n<html lang="en"><head>\n',
        '<meta charset="utf-8">\n',
        '<meta name="docs-project" content="sample">\n',
        '<meta name="reckon-type" content="plan">\n',
        '<meta name="plan-slug" content="effort">\n',
        '<meta name="plan-title" content="Effort-weighted progress">\n',
        '<meta name="plan-status" content="active">\n',
    ]
    if impl is not None:
        head.append(f'<meta name="plan-impl" content="{impl}">\n')
    if declarations is not None:
        escaped = html_module.escape(json.dumps(declarations), quote=True)
        head.append(f'<meta name="plan-section-declarations" content="{escaped}">\n')
    return (
        "".join(head)
        + "</head><body><main>\n"
        + "\n".join(body)
        + "\n</main></body></html>\n"
    )


def _write(tmp_path: Path, slug: str, text: str) -> Path:
    path = tmp_path / "repo" / "docs" / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_every_section_recorded_with_hours_weights_by_effort(tmp_path: Path) -> None:
    text = _plan_text(
        declarations={"s1": "done", "s2": "implementable"},
        records=[("s1", 4, "done"), ("s2", 6, "implementable")],
        impl="0.9",
    )
    path = _write(tmp_path, "all-hours", text)

    state = read_state(text)
    assert state["impl"] == pytest.approx(0.4)
    assert state["impl_source"] == "computed"
    assert parse_plan(path)["impl"] == pytest.approx(0.4)
    assert parse_meta(path)["impl"] == pytest.approx(0.4)


def test_a_section_without_hours_takes_the_mean_of_the_ones_present(
    tmp_path: Path,
) -> None:
    # s1 done at 2 h and s3 implementable at 4 h are recorded; nothing states
    # s2's hours, so it takes their mean of 3 h and the plan reads 2 / (2+3+4).
    text = _plan_text(
        declarations={"s1": "done", "s2": "implementable", "s3": "implementable"},
        records=[("s1", 2, "done"), ("s3", 4, "implementable")],
        impl="0.1",
    )
    path = _write(tmp_path, "partial-hours", text)

    state = read_state(text)
    assert state["impl"] == pytest.approx(2 / 9)
    assert state["impl_source"] == "declarations"
    assert parse_plan(path)["impl"] == pytest.approx(2 / 9)
    assert parse_meta(path)["impl"] == pytest.approx(2 / 9)


def test_a_deferred_sections_hours_do_not_weigh_the_imputed_mean(
    tmp_path: Path,
) -> None:
    # s4 is deferred at 40 h and sits outside the denominator, so its hours must
    # stay out of the mean s2 is imputed from. The buildable sections state 2 h
    # and 4 h, a mean of 3 h, so s2 weighs 3 h and the plan reads 2 / (2+3+4).
    # Counting s4's 40 h would raise the mean to 46/3 h and move the figure to
    # 2 / 21.33, weighting a buildable section by a deferred section's effort.
    text = _plan_text(
        declarations={
            "s1": "done",
            "s2": "implementable",
            "s3": "implementable",
            "s4": "deferred",
        },
        records=[("s1", 2, "done"), ("s3", 4, "implementable"), ("s4", 40, "deferred")],
        impl="0.1",
    )
    path = _write(tmp_path, "deferred-hours", text)

    state = read_state(text)
    assert state["impl"] == pytest.approx(2 / 9)
    assert state["impl_source"] == "declarations"
    assert parse_plan(path)["impl"] == pytest.approx(2 / 9)
    assert parse_meta(path)["impl"] == pytest.approx(2 / 9)


def test_no_hours_falls_back_to_the_count(tmp_path: Path) -> None:
    text = _plan_text(
        declarations={"s1": "done", "s2": "implementable", "s3": "implementable"},
        records=[],
        impl="0.99",
    )
    path = _write(tmp_path, "no-hours", text)

    state = read_state(text)
    assert state["impl"] == pytest.approx(1 / 3)
    assert state["impl_source"] == "declarations"
    assert parse_plan(path)["impl"] == pytest.approx(1 / 3)
    assert parse_meta(path)["impl"] == pytest.approx(1 / 3)


def test_no_declarations_keeps_the_authored_figure(tmp_path: Path) -> None:
    text = _plan_text(declarations=None, records=[], impl="0.3")
    path = _write(tmp_path, "authored", text)

    state = read_state(text)
    assert state["impl"] == pytest.approx(0.3)
    assert state["impl_source"] == "authored"
    assert parse_plan(path)["impl"] == pytest.approx(0.3)
    assert parse_meta(path)["impl"] == pytest.approx(0.3)


def test_declarations_with_no_records_read_alike_in_both_readers(
    tmp_path: Path,
) -> None:
    # The plan is declared but not yet recorded, and its authored meta is a
    # stale figure. The head-only reader that builds discovery rows must derive
    # the same figure the full parse does.
    text = _plan_text(
        declarations={"s1": "done", "s2": "done", "s3": "implementable"},
        records=[],
        impl="0.08",
    )
    path = _write(tmp_path, "declared-not-recorded", text)

    parsed = read_state(text)
    light = parse_meta(path)
    assert parsed["impl"] == pytest.approx(2 / 3)
    assert parsed["impl_source"] == "declarations"
    assert light["impl"] == pytest.approx(parsed["impl"])
    assert light["impl_source"] == parsed["impl_source"]
    assert parse_plan(path)["impl"] == pytest.approx(parsed["impl"])
    # The authored meta on disk is untouched by a read.
    assert 'name="plan-impl" content="0.08"' in text
