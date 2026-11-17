"""A hand-merged run is counted in the suite rollup, stamped by one formatter.

A run whose reviewed head the lead merged by hand records a suite delta of
status ``not-run``: the branch already carries the change, so the recording has
no pair of arms to compare. That row used to drop silently out of the ledger
rollup, which tallied only the outcomes a promotion measures. It now counts
under its own key.

The timestamps written beside such a row read the one ISO-Z formatter
``reckon.crew.run_paths._utc_now``, so a crew verb's stamp on a run's pointer is
the same text for the same instant as every other recorded instant, and no
second formatter has to be kept in step.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import ledger
from reckon.cli import main as cli_main
from reckon.crew import promotion_records, run_paths
from tests.helpers.landing_repository import PROJECT
from tests.test_a_hand_merged_run_is_recorded import (  # noqa: F401 - fixture
    merged_run,
)

MERGE_SHA = "0123456789abcdef0123456789abcdef01234567"
FROZEN_INSTANT = datetime(2026, 11, 15, 10, 30, 0, tzinfo=UTC)
FROZEN_TEXT = "2026-11-15T10:30:00Z"


class _FrozenClock(datetime):
    """A ``datetime`` whose ``now`` answers one instant for every caller."""

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return FROZEN_INSTANT.replace(tzinfo=None)
        return FROZEN_INSTANT.astimezone(tz)


@pytest.fixture()
def ledger_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway project state tree and crew home the ledger can use."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    root = tmp_path / "repo"
    state = root / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n",
        encoding="utf-8",
    )
    return root


def _hand_merged_delta(run_id: str) -> dict:
    """The suite delta the hand-merge recording writes for its row."""
    return promotion_records._evaluate_suite_delta(
        run_id, {"recorded_merge": MERGE_SHA}, waiver_reason=""
    )


def test_a_hand_merged_row_is_counted_under_not_run(ledger_repo: Path) -> None:
    """The row measures no delta, and the rollup still reports it."""
    run_id = "r-hand-merged"
    delta = _hand_merged_delta(run_id)
    assert delta["status"] == "not-run", delta
    ledger.append_run(
        PROJECT,
        ledger.build_record(
            run_id=run_id,
            plan="plan-a",
            gate="passed",
            suite_delta=delta,
            completed_at="2026-11-15T10:00:00Z",
        ),
        root=ledger_repo,
        allow_create=True,
    )

    suite_deltas = ledger.summary(PROJECT, root=ledger_repo)["suite_deltas"]

    assert suite_deltas["not-run"] == 1


def test_the_pointer_stamp_comes_from_the_run_paths_formatter(
    request, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crew verb's stamp is the shared formatter's text, not a second one."""
    run = request.getfixturevalue("merged_run")
    monkeypatch.setattr(run_paths, "datetime", _FrozenClock)

    result = CliRunner().invoke(
        cli_main,
        [
            "crew",
            "declare-gate",
            "--run",
            run["run_id"],
            "--command",
            "true",
            "--reason",
            "the arms ran a narrower command than the project declares",
        ],
    )

    assert result.exit_code == 0, result.output
    declared_at = json.loads(result.output)["gate_declaration"]["declared_at"]
    assert declared_at == FROZEN_TEXT
    assert declared_at == run_paths._utc_now()
