"""The canvas-fidelity check, exercised against the live tree and against copies.

The check itself reads every named value from the canvas import or from the
surface source files; nothing here retypes a canvas value as a literal. The
mutation tests are what make that contract load-bearing: a value that had been
retyped as a literal would not follow a change to its source, so the check that
drifts back to the source is proven by asserting the mutated value arrives and
turns into a verdict, and a renamed source key is proven by requiring a loud
exception rather than a silent skip.

Four tests pin the deferred-deviation semantics. The constant reading measure
is the single documented deviation, declared with its reason; while the surface
still matches the canvas the declaration is dormant, when the surface shows the
declared value the verdict is a declared deviation, and a surface that differs
from the canvas AND from its declaration is a stale declaration that must fail
because it would otherwise outlive its cause.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from tests.spa_canvas_values import (
    DECLARED_DEVIATIONS,
    SURFACES,
    CanvasValueError,
    ImplementationValueError,
    compare,
    extract_canvas_values,
    read_implementation_values,
)

ROOT = Path(__file__).resolve().parents[1]
CANVAS_PATH = ROOT / "design" / "reckon-spa-handoff.dc.html"
UI = ROOT / "docs" / "ui"


def _source() -> str:
    return CANVAS_PATH.read_text(encoding="utf-8")


def _mutated_canvas(replacements: list[tuple[str, str]]) -> str:
    text = _source()
    for old, new in replacements:
        assert old in text, old
        text = text.replace(old, new)
    return text


def _impl_with(**changes: object) -> dict[str, object]:
    values = read_implementation_values()
    values.update(changes)
    return values


def _ui_tree(tmp_path: Path) -> Path:
    for name in ("reader.css", "plans.css", "shell-titlebar.jsx", "graph.jsx"):
        destination = tmp_path / "docs" / "ui" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(UI / name, destination)
    return tmp_path


def test_live_run_reports_a_verdict_for_every_surface_and_states_digits() -> None:
    report = compare(extract_canvas_values(), read_implementation_values())

    assert set(report.verdicts) == set(SURFACES)
    assert report.compared == len(SURFACES) == 14
    assert report.matched + report.mismatched == report.compared
    assert report.failing == (report.mismatched > 0)

    counts = re.match(
        r"fidelity compared=(\d+) matched=(\d+) mismatched=(\d+)", report.summary()
    )
    assert counts is not None
    assert [int(group) for group in counts.groups()] == [
        report.compared,
        report.matched,
        report.mismatched,
    ]
    # The blocked border the canvas declares has no surface rule yet, so the
    # check records it as a verdict rather than repairing or skipping it.
    assert report.verdicts["card_border_blocked"] == "mismatch"
    assert report.mismatch_kinds["card_border_blocked"] == "undeclared"


def test_an_absent_surface_value_compares_as_a_verdict_not_a_skip() -> None:
    report = compare(extract_canvas_values(), _impl_with(card_border_blocked=None))

    assert report.verdicts["card_border_blocked"] == "mismatch"
    assert report.mismatch_kinds["card_border_blocked"] == "undeclared"
    assert "card_border_blocked" in report.mismatches()
    assert report.failing is True


def test_extracted_values_are_read_from_the_canvas_not_retyped() -> None:
    canvas = extract_canvas_values()
    source = _source()

    assert canvas["reader_measure_normal"] in source
    assert canvas["reader_measure_focus"] in source
    assert canvas["card_ground"] in source
    assert canvas["card_border"] in source
    assert canvas["card_border_blocked"] in source
    assert canvas["column_label_none"] in source
    assert 'label: Number(k) === 0 ? "no prerequisites" : `depth ${k}`' in source
    for surface in (
        "metadata_plan",
        "metadata_research",
        "metadata_evidence",
        "metadata_figure",
    ):
        assert all(f'k: "{key}"' in source for key in canvas[surface]), surface


def test_a_mutated_canvas_value_is_read_following_the_mutation() -> None:
    mutated = _mutated_canvas(
        [
            ("font-size:11.5px", "font-size:17px"),
            (
                'readMeasure: S.reading ? "820px" : "760px"',
                'readMeasure: S.reading ? "900px" : "640px"',
            ),
            ('? "var(--bad)" : "var(--line)"', '? "var(--bad)" : "var(--inks)"'),
            ('"no prerequisites"', '"no upstreams"'),
        ]
    )
    values = extract_canvas_values(source=mutated)

    assert values["toggle_font_size"] == 17.0
    assert values["reader_measure_normal"] == "640px"
    assert values["reader_measure_focus"] == "900px"
    assert values["card_border"] == "var(--inks)"
    assert values["column_label_none"] == "no upstreams"


def test_a_mutated_canvas_value_fails_the_check_against_the_live_surface() -> None:
    mutated = _mutated_canvas([("font-size:11.5px", "font-size:14px")])
    report = compare(
        extract_canvas_values(source=mutated), read_implementation_values()
    )

    assert report.verdicts["toggle_font_size"] == "mismatch"
    assert report.mismatch_kinds["toggle_font_size"] == "undeclared"
    assert "toggle_font_size" in report.mismatches()
    assert report.failing is True


def test_a_renamed_canvas_value_fails_loudly_instead_of_being_skipped() -> None:
    renamed_measure = _mutated_canvas([("readMeasure:", "readMeasureX:")])
    with pytest.raises(CanvasValueError) as info:
        extract_canvas_values(source=renamed_measure)
    assert "reader_measure_normal" in str(info.value)

    renamed_toggle = _mutated_canvas([("readGraphBtnStyle:", "readGraphBtnStyleX:")])
    with pytest.raises(CanvasValueError) as info:
        extract_canvas_values(source=renamed_toggle)
    assert "toggle_font_size" in str(info.value)


def test_a_removed_surface_value_fails_loudly_instead_of_being_skipped(
    tmp_path: Path,
) -> None:
    tree = _ui_tree(tmp_path)
    css = tree / "docs" / "ui" / "reader.css"
    css.write_text(
        css.read_text(encoding="utf-8").replace(
            ".r-reading-controls {", ".r-reading-controls-x {"
        ),
        encoding="utf-8",
    )

    with pytest.raises(ImplementationValueError) as info:
        read_implementation_values(root=tree)
    assert "toggle_font_size" in str(info.value)
    assert "reader.css" in str(info.value)


def test_implementation_values_are_read_from_the_live_surface_files() -> None:
    impl = read_implementation_values()

    assert impl["reader_measure_normal"] in (UI / "reader.css").read_text(
        encoding="utf-8"
    )
    assert impl["reader_measure_focus"] in (UI / "reader.css").read_text(
        encoding="utf-8"
    )
    assert impl["card_border"] in (UI / "plans.css").read_text(encoding="utf-8")
    assert impl["card_ground"] in (UI / "plans.css").read_text(encoding="utf-8")
    assert impl["column_label_none"] in (UI / "graph.jsx").read_text(encoding="utf-8")


def test_the_single_deliberate_declaration_is_the_constant_measure() -> None:
    assert set(DECLARED_DEVIATIONS) == {"reader_measure_normal", "reader_measure_focus"}
    wider = extract_canvas_values()["reader_measure_focus"]
    for surface in DECLARED_DEVIATIONS:
        deviation = DECLARED_DEVIATIONS[surface]
        # The declared value is the larger of the canvas's two, derived not literal.
        assert deviation.declared == wider
        assert deviation.reason
        assert "reflow" in deviation.reason


def test_declared_deviation_is_honoured_when_the_surface_matches_it() -> None:
    declared = DECLARED_DEVIATIONS["reader_measure_normal"].declared
    report = compare(
        extract_canvas_values(), _impl_with(reader_measure_normal=declared)
    )

    assert report.verdicts["reader_measure_normal"] == "declared-deviation"
    assert report.deviation_states["reader_measure_normal"] == "active"
    assert report.mismatches() == ["card_border_blocked"]


def test_declaration_is_dormant_while_the_surface_still_matches_the_canvas() -> None:
    """A declared deviation is inert wherever the surface holds the canvas
    value, and live only where the surface is held at the declared value.

    The declaration names one deliberate value that differs from the canvas.
    Which surfaces it is in force on is a fact about the surface, not about the
    declaration, so the verdict and the deviation state are read off the
    comparison for every declared surface rather than asserted from a list.
    """
    canvas = extract_canvas_values()
    implementation = read_implementation_values()
    report = compare(canvas, implementation)

    for surface, deviation in DECLARED_DEVIATIONS.items():
        if implementation[surface] == canvas[surface]:
            assert report.verdicts[surface] == "match"
            assert report.deviation_states[surface] == "dormant"
        else:
            assert implementation[surface] == deviation.declared
            assert report.verdicts[surface] == "declared-deviation"
            assert report.deviation_states[surface] == "active"


def test_undeclared_value_drift_fails_with_the_undeclared_kind() -> None:
    report = compare(extract_canvas_values(), _impl_with(toggle_font_size=99.0))

    assert report.verdicts["toggle_font_size"] == "mismatch"
    assert report.mismatch_kinds["toggle_font_size"] == "undeclared"
    assert report.failing is True


def test_a_stale_declaration_fails_because_it_cannot_outlive_its_cause() -> None:
    report = compare(extract_canvas_values(), _impl_with(reader_measure_normal="912px"))

    assert report.verdicts["reader_measure_normal"] == "mismatch"
    assert report.mismatch_kinds["reader_measure_normal"] == "declaration-stale"
    assert report.deviation_states["reader_measure_normal"] == "contradicted"
    assert report.failing is True
