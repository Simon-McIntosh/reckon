"""A stored command the tokeniser cannot split does not take down the duty read.

A duty row carries the command that answers it as printed text, and the row
also carries an action form — the same command spelled as a tool call — when
that text can be turned into one. The sub-floor duty's printed command names
the disposition in prose ("folded with the dispatched node's id, or exempted
with the recorded reason"), and the apostrophe in that prose is a quotation
mark to ``shlex``: splitting the line raises ``ValueError`` on an unbalanced
quote. The form builder must treat that command as one it cannot carry and
leave the row without a form, so the whole obligations read still completes and
the row keeps its printed command — rather than the parse error propagating out
of the reader.

The fixture stands the run up inside a temporary configuration home, so the
operator's review store is neither read nor written.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from reckon.crew import obligations as obligations_module
from reckon.crew import review as review_module
from reckon.crew import runs

PROJECT = "unsplittable-fixture"
SESSION = "coordinator-fixture"
RUN = "r-unsplittable"

# A passing total with one dimension far below its declared floor, so the
# sub-floor duty has exactly one row to build and its command is the composer's
# own text.
SCORES = {
    "goal_fidelity": 19,
    "evidence": 19,
    "scope_discipline": 18,
    "durability": 4,
    "fit": 18,
    "reuse": 18,
}
FLOORS = {"durability": 10}

REAL_HOME = Path.home() / ".config" / "reckon"


def _emit(*, base_sha: str, head_sha: str) -> str:
    """Emitted review text carrying the revision pair and one score per line."""
    lines = [f"reviewed_base_sha: {base_sha}", f"reviewed_head_sha: {head_sha}"]
    lines.extend(f"SCORE {dimension}: {score}" for dimension, score in SCORES.items())
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
            + "".join(
                f"    {dimension}: {floor}\n" for dimension, floor in FLOORS.items()
            ),
            encoding="utf-8",
        )
        self.pointer()

    def pointer(self) -> dict:
        """Write the live pointer and the left-behind manifest of the two runs."""
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
        return record

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


def _stored_command(run_id: str) -> str:
    """The composer's own printed command for one sub-floor dimension."""
    return (
        f"dispose durability scored {SCORES['durability']} against a floor of "
        f"{FLOORS['durability']} on run {run_id}: folded with the dispatched "
        "node's id, or exempted with the recorded reason"
    )


def test_an_unsplittable_command_has_no_action_form() -> None:
    """The builder returns no form, and the tokeniser is shown to refuse it."""
    command = _stored_command(RUN)
    with pytest.raises(ValueError, match="No closing quotation"):
        obligations_module.shlex.split(command)
    assert obligations_module.action_form(command) is None


def test_a_splittable_command_still_has_an_action_form() -> None:
    """The guard narrows nothing: a well-formed command still renders a form."""
    rendered = obligations_module.action_form(
        "reckon crew complete --run r-x --gate pass"
    )
    assert rendered == 'crew(action="complete", run_id="r-x", gate="pass")'


def test_the_obligations_read_completes_over_the_stored_command(
    fleets: Callable[[], _Fleet],
) -> None:
    """The row survives with its printed command and no form."""
    fleet = fleets()
    fleet.store_review("a" * 40, "b" * 40)

    report = obligations_module.obligations(PROJECT, SESSION)

    rows = [
        row
        for row in report["obligations"]
        if row["kind"] == obligations_module.SUB_FLOOR_DUTY_KIND
    ]
    assert len(rows) == 1, report["obligations"]
    row = rows[0]
    assert row["run_id"] == RUN
    assert row["dimension"] == "durability"
    assert row["next_command"] == _stored_command(RUN)
    assert "next_action_form" not in row
