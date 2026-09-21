"""A reader of the committed run ledger receives a node's wall time and width.

Every test enters through a surface a reader actually reaches — the CLI
``crew ledger --view records`` command and the MCP ``crew(view="records)``
read — rather than through the arithmetic underneath them, because the defect
this covers is that the derivation existed and no surface emitted it. A test
that called the deriver directly would pass against a build whose readers still
show the stored field.

The last test is a static one: it proves the derivation has a production caller
inside :mod:`reckon` at all. It walks ``reckon/`` alone, so test modules cannot
satisfy it, and it checks the instrument against the definition it must find
before it reports on the call sites.

The repository under test is a throwaway directory written by the fixture, so
no test reads or writes the real crew state and a reading here is a statement
about the code.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli, mcp

PROJECT = "proj"

# The fixture ranges the input domain rather than one shape: two runs dispatched
# at the same moment, a row whose stored field disagrees with its own stamps, a
# row missing its completion stamp, and a row missing its dispatch stamp.
#
# The plan assignment is load-bearing for the filter-order case: the row
# ``early`` is dispatched at 10:00, when both it and ``disagreeing`` are in
# flight, but ``disagreeing`` holds another plan. So a read that counted width
# after applying its filter would report one run at that instant while the
# fleet held two, and the two orders therefore answer differently for this
# fixture.
ROWS = [
    {
        "run_id": "early",
        "plan": "alpha",
        "node": "early",
        "dispatched_at": "2026-09-20T10:00:00Z",
        "completed_at": "2026-09-20T10:30:00Z",
        "wall_seconds": 1800,
    },
    {
        "run_id": "disagreeing",
        "plan": "beta",
        "node": "disagreeing",
        "dispatched_at": "2026-09-20T10:00:00Z",
        "completed_at": "2026-09-20T10:02:00Z",
        "wall_seconds": 4242,
    },
    {
        "run_id": "no-completion",
        "plan": "gamma",
        "node": "no-completion",
        "dispatched_at": "2026-09-20T10:10:00Z",
        "wall_seconds": 7,
    },
    {
        "run_id": "alone",
        "plan": "beta",
        "node": "alone",
        "dispatched_at": "2026-09-20T11:00:00Z",
        "completed_at": "2026-09-20T11:01:00Z",
    },
    {
        "run_id": "no-dispatch",
        "plan": "alpha",
        "node": "no-dispatch",
        "completed_at": "2026-09-20T10:20:00Z",
        "wall_seconds": 999,
    },
]

SURFACES = ("cli", "mcp")


def _repository(tmp_path: Path) -> Path:
    """Stage a throwaway repository carrying one committed ledger."""

    repo = tmp_path / "repo"
    state = repo / "docs" / "state" / PROJECT
    state.mkdir(parents=True, exist_ok=True)
    (state / "crew.json").write_text(
        json.dumps(
            {
                "project": PROJECT,
                "data": {
                    "_version": 3,
                    "members": [],
                    "runs": ROWS,
                    "holds": [],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return repo


def _rows(surface: str, repo: Path, **filters) -> dict[str, dict]:
    """Read the committed rows through the surface a caller would use."""

    if surface == "cli":
        result = CliRunner().invoke(
            cli.main,
            [
                "crew",
                "ledger",
                "--project",
                PROJECT,
                "--view",
                "records",
                "--checkout-path",
                str(repo),
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
    else:
        payload = mcp._crew(PROJECT, view="records", checkout_path=str(repo), **filters)
    assert payload["ok"] is True, payload
    return {row["run_id"]: row for row in payload["runs"]}


@pytest.mark.parametrize("surface", SURFACES)
def test_the_records_read_emits_both_figures_for_every_row(tmp_path, surface):
    """Every row a reader receives carries a wall time and a width at start."""

    rows = _rows(surface, _repository(tmp_path))
    assert set(rows) == {
        "early",
        "disagreeing",
        "no-completion",
        "alone",
        "no-dispatch",
    }
    for run_id, row in rows.items():
        assert "wall_seconds" in row, run_id
        assert "width_at_start" in row, run_id

    # The stamped rows report the span between their own stamps, and the width
    # is counted from the measured spans of the same row set.
    assert rows["early"]["wall_seconds"] == 1800
    assert rows["disagreeing"]["wall_seconds"] == 120
    assert rows["alone"]["wall_seconds"] == 60
    assert rows["early"]["width_at_start"] == 2
    assert rows["disagreeing"]["width_at_start"] == 2
    assert rows["no-completion"]["width_at_start"] == 1
    assert rows["alone"]["width_at_start"] == 1


@pytest.mark.parametrize("surface", SURFACES)
def test_a_disagreeing_stored_field_never_reaches_the_read_surface(tmp_path, surface):
    """A stored wall time that disagrees with its runs' stamps loses to the span."""

    rows = _rows(surface, _repository(tmp_path))
    stored = {row["run_id"]: row.get("wall_seconds") for row in ROWS}
    # The fixture's stored field disagrees with its own stamps on three rows, so
    # none of those values may appear at the surface under the span's name.
    assert stored["disagreeing"] == 4242
    assert stored["no-completion"] == 7
    assert stored["no-dispatch"] == 999
    for run_id in ("disagreeing", "no-completion", "no-dispatch"):
        assert rows[run_id]["wall_seconds"] != stored[run_id]
    assert rows["disagreeing"]["wall_seconds"] == 120
    # The one row whose stored field agrees reports that value because it is the
    # derived one: an implementation that passed the stored field through would
    # report the other three as well.
    assert rows["early"]["wall_seconds"] == stored["early"] == 1800


@pytest.mark.parametrize("surface", SURFACES)
def test_a_missing_stamp_is_absent_at_the_read_surface(tmp_path, surface):
    """A figure the stamps cannot support is absent, and never zero."""

    rows = _rows(surface, _repository(tmp_path))
    assert rows["no-completion"]["wall_seconds"] is None
    assert rows["no-completion"]["wall_seconds"] != 0
    assert rows["no-dispatch"]["wall_seconds"] is None
    assert rows["no-dispatch"]["wall_seconds"] != 0
    assert rows["no-dispatch"]["width_at_start"] is None
    assert rows["no-dispatch"]["width_at_start"] != 0


def test_the_two_read_surfaces_agree_on_every_figure(tmp_path):
    """The CLI and the MCP read report one set of figures, not two."""

    repo = _repository(tmp_path)
    from_cli = _rows("cli", repo)
    from_mcp = _rows("mcp", repo)
    assert set(from_cli) == set(from_mcp)
    for run_id, row in from_cli.items():
        assert row["wall_seconds"] == from_mcp[run_id]["wall_seconds"], run_id
        assert row["width_at_start"] == from_mcp[run_id]["width_at_start"], run_id


def _width_within_plan(plan: str, run_id: str) -> int:
    """Count the fixture's runs in ``plan`` in flight at ``run_id``'s dispatch.

    This is the figure a read would report if it counted the width after
    applying its own filter rather than before. It is derived from the same
    rows the fixture stages, so the case can state the two orders' answers
    instead of asserting that one of them looks plausible.
    """

    selected = [row for row in ROWS if row["plan"] == plan]
    target = next(row for row in selected if row["run_id"] == run_id)
    start = datetime.fromisoformat(target["dispatched_at"])
    return sum(
        1
        for row in selected
        if row.get("dispatched_at")
        and row.get("completed_at")
        and datetime.fromisoformat(row["dispatched_at"])
        <= start
        <= datetime.fromisoformat(row["completed_at"])
    )


def test_a_filtered_read_reports_the_width_of_the_fleet_not_of_the_selection(tmp_path):
    """Selection narrows the rows, never the width they started in.

    The selected row was dispatched while a run holding a different plan was in
    flight, so the two orders answer differently for this fixture: counting the
    width over every committed row reports the fleet, and counting it after the
    filter reports the selection. The case asserts the fleet's figure and, from
    the same fixture, that the selection's own count is strictly smaller — so
    it fails when the read reports the selection's figure and equally when a
    later fixture edit removes the difference it rests on.
    """

    repo = _repository(tmp_path)
    selected = _rows("mcp", repo, plan="alpha")
    assert set(selected) == {"early", "no-dispatch"}
    unfiltered = _rows("cli", repo)

    assert (
        selected["early"]["width_at_start"]
        == unfiltered["early"]["width_at_start"]
        == 2
    )
    assert _width_within_plan("alpha", "early") == 1
    # A selected row whose dispatch stamp is missing still reports absence,
    # which the filter cannot manufacture a width for.
    assert selected["no-dispatch"]["width_at_start"] is None


def _call_sites(root: Path) -> dict[str, list[int]]:
    """Lines under ``reckon/`` that call ``run_rows`` outside its definition."""

    call = re.compile(r"\brun_rows\s*\(")
    definition = re.compile(r"^\s*def\s+run_rows\s*\(")
    sites: dict[str, list[int]] = {}
    for path in sorted((root / "reckon").rglob("*.py")):
        lines = path.read_text(encoding="utf-8").splitlines()
        hits = [
            number
            for number, line in enumerate(lines, 1)
            if call.search(line) and not definition.match(line)
        ]
        if hits:
            sites[str(path.relative_to(root))] = hits
    return sites


def _definition_sites(root: Path) -> dict[str, list[int]]:
    """The control for :func:`_call_sites`: the definitions it must also see."""

    definition = re.compile(r"^\s*def\s+run_rows\s*\(")
    found: dict[str, list[int]] = {}
    for path in sorted((root / "reckon").rglob("*.py")):
        lines = path.read_text(encoding="utf-8").splitlines()
        hits = [
            number for number, line in enumerate(lines, 1) if definition.match(line)
        ]
        if hits:
            found[str(path.relative_to(root))] = hits
    return found


def test_the_derivation_has_a_production_caller_inside_reckon():
    """A read no production code calls reaches no reader.

    The search ranges over ``reckon/`` alone, so a test module cannot satisfy
    it, and it confirms the walk sees the definition before it reports on the
    call sites: an empty result from a blind instrument and a genuine absence
    read the same otherwise.
    """

    root = Path(__file__).resolve().parent.parent

    definitions = _definition_sites(root)
    assert definitions, "the search instrument found no run_rows definition at all"
    assert any("summary.py" in path for path in definitions), definitions

    sites = _call_sites(root)
    assert sites, "run_rows has no caller outside its own definition"
    assert "reckon/crew/summary.py" not in sites, sites
