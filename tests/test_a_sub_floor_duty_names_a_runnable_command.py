"""A sub-floor duty hands the coordinator a command, not a sentence.

A duty row carries the command that answers it in ``next_command``, and every
other duty writes a runnable ``reckon crew …`` invocation there. The sub-floor
duty used to write prose, which the action-form builder cannot split and which
once raised ``No closing quotation`` inside the obligations read. The row now
carries a runnable ``crew dispose`` command naming the duty's project, run and
dimension, split by ``shlex`` and renderable as an action form.

The fixture stands the run up inside a temporary configuration home, so the
operator's review store is neither read nor written.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Callable
from pathlib import Path

import pytest

from reckon.crew import obligations as obligations_module
from reckon.crew import review as review_module
from reckon.crew import runs

PROJECT = "sub-floor-command-fixture"
SESSION = "coordinator-fixture"
RUN = "r-sub-floor-command"
DIMENSION = "durability"

# A passing total with one dimension far below its declared floor, so the
# sub-floor duty has exactly one row to build.
SCORES = {
    "goal_fidelity": 19,
    "evidence": 19,
    "scope_discipline": 18,
    DIMENSION: 4,
    "fit": 18,
    "reuse": 18,
}
FLOORS = {DIMENSION: 10}

REAL_HOME = Path.home() / ".config" / "reckon"


def _emit(*, base_sha: str, head_sha: str) -> str:
    """Emitted review text carrying the revision pair and one score per line."""
    lines = [f"reviewed_base_sha: {base_sha}", f"reviewed_head_sha: {head_sha}"]
    lines.extend(f"SCORE {key}: {value}" for key, value in SCORES.items())
    return "\n".join(lines) + "\n"


class _Fleet:
    """One throwaway project holding a single run with a sub-floor review."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.home = tmp_path / "config"
        self.home.mkdir(parents=True)
        monkeypatch.setenv("RECKON_HOME", str(self.home))
        for name in ("RECKON_FLIGHT_CONFIG", "RECKON_WORKTREES", "RECKON_STATE_ROOT"):
            monkeypatch.delenv(name, raising=False)
        (self.home / "mounts.json").write_text(
            json.dumps({PROJECT: str(tmp_path / "docs")}), encoding="utf-8"
        )
        (self.home / "flight.yaml").write_text(
            "gates:\n  dimension_floors:\n"
            + "".join(f"    {key}: {floor}\n" for key, floor in FLOORS.items()),
            encoding="utf-8",
        )
        self._write_pointer()

    def _write_pointer(self) -> None:
        manifest = self.home / "manifests" / f"{RUN}.md"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            f"node: {RUN}\nstatus: complete\ncommits: []\n", encoding="utf-8"
        )
        record = {
            "run_id": RUN,
            "project": PROJECT,
            "session": SESSION,
            "process_alive": False,
            "manifest_path": str(manifest),
            "node": {
                "id": RUN,
                "plan": "fixture",
                "section": "s1",
                "time_budget": "20m",
                "write_paths": [],
            },
        }
        runs._write_json(runs.pointer_path(RUN), record)

    def store_review(self, base_sha: str, head_sha: str) -> Path:
        record = review_module.parse_review(_emit(base_sha=base_sha, head_sha=head_sha))
        record.update(
            {
                "project": PROJECT,
                "reviewed_run_id": RUN,
                "timestamp": "2026-10-03T01:00:00+00:00",
            }
        )
        return review_module.store_review(record)


@pytest.fixture()
def fleets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[], _Fleet]:
    return lambda: _Fleet(tmp_path, monkeypatch)


@pytest.fixture(autouse=True)
def real_config_home_untouched():
    """No fixture write reaches the real configuration home."""
    yield
    reviews = REAL_HOME / "crew" / "reviews" / PROJECT
    assert not reviews.exists(), f"fixture review store leaked to {reviews}"


def _sub_floor_row(fleet: _Fleet) -> dict:
    """The single sub-floor duty row for the fixture run."""
    fleet.store_review("a" * 40, "b" * 40)
    report = obligations_module.obligations(PROJECT, SESSION)
    rows = [
        row
        for row in report["obligations"]
        if row["kind"] == obligations_module.SUB_FLOOR_DUTY_KIND
    ]
    assert len(rows) == 1, report["obligations"]
    return rows[0]


def test_the_command_splits_and_names_a_crew_dispose(
    fleets: Callable[[], _Fleet],
) -> None:
    """The tokeniser splits the command and it names the dispose verb."""
    row = _sub_floor_row(fleets())

    tokens = shlex.split(row["next_command"])
    assert tokens[:3] == ["reckon", "crew", "dispose"]


def test_the_command_carries_the_findings_project_run_and_dimension(
    fleets: Callable[[], _Fleet],
) -> None:
    """Every value the disposition needs is present with the finding's own."""
    row = _sub_floor_row(fleets())

    tokens = shlex.split(row["next_command"])

    def value_of(option: str) -> str:
        return tokens[tokens.index(option) + 1]

    assert value_of("--project") == PROJECT
    assert value_of("--run") == RUN
    assert value_of("--dimension") == DIMENSION
    assert value_of("--kind") == "folded"


def test_the_command_renders_an_action_form(
    fleets: Callable[[], _Fleet],
) -> None:
    """The command is one the coordinator can call on the tool surface."""
    row = _sub_floor_row(fleets())

    rendered = obligations_module.action_form(str(row["next_command"]))

    assert rendered is not None
    assert rendered.startswith('crew(action="dispose"')
    assert f"project={json.dumps(PROJECT)}" in rendered
    assert f"run_id={json.dumps(RUN)}" in rendered
    assert f"dimension={json.dumps(DIMENSION)}" in rendered
    assert "next_action_form" in row
    assert row["next_action_form"] == rendered


def test_the_exempt_alternative_is_prose_beside_the_command(
    fleets: Callable[[], _Fleet],
) -> None:
    """The exemption shape rides beside the command, not inside it."""
    row = _sub_floor_row(fleets())

    alternative = str(row.get("exempt_alternative") or "")
    assert "--kind exempted" in alternative
    assert "--reason" in alternative
    assert "exempted" not in str(row["next_command"])
