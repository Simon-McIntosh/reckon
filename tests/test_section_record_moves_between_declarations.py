"""A declaration write moves the typed section record that must agree with it.

The fixture is one-typesafe-model-picker as it stood at 53a72f8c, the revision
that refused every edit path to its section 5 before the session opened two
further sections as a workaround: a declaration map naming ``s5`` and a record
beside it carrying ``deferred``, which no accepted write could move to
``implementable``. The whole-map form of ``set section_declarations`` replaced
the map without moving the record, so the plan refused the file the write
produced (``sections['s5'].status: 'deferred' disagrees with section
declaration 'implementable'``). The dotted form already moved the record and is
kept here as the positive control.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from reckon import _plan_html
from reckon import mcp as mcp_module

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "section_record_transition"
    / "one-typesafe-model-picker.html"
)
SLUG = "one-typesafe-model-picker"
PROJECT = "reckon"


@pytest.fixture()
def plan(tmp_path: Path) -> tuple[Path, Path]:
    checkout = tmp_path / "checkout"
    path = checkout / "docs" / "plans" / f"{SLUG}.html"
    path.parent.mkdir(parents=True)
    shutil.copy(FIXTURE, path)
    return checkout, path


def _read(path: Path) -> dict:
    return _plan_html.read_state(path.read_text(encoding="utf-8"))


def _edit(checkout: Path, path: Path, ops: list[dict]) -> dict:
    return mcp_module._edit_plan_tool(
        PROJECT,
        SLUG,
        expected_version=_read(path)["version"],
        checkout_path=str(checkout),
        doc_type="plan",
        mode="state",
        ops=ops,
    )


def _statuses(path: Path) -> tuple[str, str]:
    state = _read(path)
    record = next(r for r in state["sections"] if r["id"] == "s5")
    return state["section_declarations"]["s5"], record["status"]


def _whole_map(path: Path, s5: str) -> dict:
    state = _read(path)
    return {**state["section_declarations"], "s5": s5}


def test_fixture_record_reads_deferred_beside_its_declaration(plan) -> None:
    """The fixture is the refusing revision: both places read deferred."""
    _, path = plan
    assert _statuses(path) == ("deferred", "deferred")


def test_whole_map_write_moves_the_record_with_the_declaration(plan) -> None:
    """One versioned write moves the declaration and its record together."""
    checkout, path = plan
    before_version = _read(path)["version"]

    result = _edit(
        checkout,
        path,
        [
            {
                "op": "set",
                "path": "section_declarations",
                "value": _whole_map(path, "implementable"),
            }
        ],
    )

    assert result["ok"] is True, result
    assert _read(path)["version"] == before_version + 1
    assert _statuses(path) == ("implementable", "implementable")


def test_second_write_moves_the_record_back(plan) -> None:
    """The record moves between declared values, in both directions."""
    checkout, path = plan

    up = _edit(
        checkout,
        path,
        [
            {
                "op": "set",
                "path": "section_declarations",
                "value": _whole_map(path, "implementable"),
            }
        ],
    )
    assert up["ok"] is True, up
    assert _statuses(path) == ("implementable", "implementable")

    down = _edit(
        checkout,
        path,
        [
            {
                "op": "set",
                "path": "section_declarations",
                "value": _whole_map(path, "deferred"),
            }
        ],
    )

    assert down["ok"] is True, down
    assert down["new_version"] == up["new_version"] + 1
    assert _statuses(path) == ("deferred", "deferred")


def test_whole_map_write_leaves_the_other_records_alone(plan) -> None:
    """An entry that does not move its declaration does not move its record."""
    checkout, path = plan
    before = _read(path)["sections"]

    result = _edit(
        checkout,
        path,
        [
            {
                "op": "set",
                "path": "section_declarations",
                "value": _whole_map(path, "implementable"),
            }
        ],
    )

    assert result["ok"] is True, result
    after = {r["id"]: r for r in _read(path)["sections"]}
    for record in before:
        if record["id"] != "s5":
            assert after[record["id"]] == record


def test_dotted_write_moves_one_record_and_its_declaration(plan) -> None:
    """The named form of the same write, kept as the positive control."""
    checkout, path = plan

    result = _edit(
        checkout,
        path,
        [{"op": "set", "path": "section_declarations.s5", "value": "implementable"}],
    )

    assert result["ok"] is True, result
    assert _statuses(path) == ("implementable", "implementable")

    other = _read(path)
    assert other["section_declarations"]["s6"] == "implementable"