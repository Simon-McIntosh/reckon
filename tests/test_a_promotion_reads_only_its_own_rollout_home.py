"""A promotion's lane receipt reads only the promoting run's own rollout home.

The receipt lookup confines itself to one crew run's harness home.  A lookup
that names no run walks every run directory under the crew home to find the
session the promoting run already owns, so its cost grows with the fleet's run
population rather than staying constant.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from reckon.crew import rollout
from reckon.crew import runs as crew_runs
from reckon.crew.promotion_release import _harvest_lane_receipt

PROMOTING_RUN = "r-promoting-node"
PROMOTING_SESSION = "1a2b3c4d-0001-4000-8000-000000000001"
OWN_CONTEXT_WINDOW = 137_000
OTHER_CONTEXT_WINDOW = 211_000


def _write_rollout(run_dir: Path, session_id: str, *, context_window: int) -> Path:
    """One run's harness home carrying a single token-count record."""
    root = rollout.run_home_sessions_root(run_dir)
    path = (
        root / "2026" / "11" / "10" / f"rollout-2026-11-10T00-00-00-{session_id}.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": "2026-11-10T18:00:00Z",
        "payload": {
            "type": "token_count",
            "info": {
                "model_context_window": context_window,
                "total_token_usage": {"input_tokens": 100, "output_tokens": 20},
                "last_token_usage": {"input_tokens": 100, "output_tokens": 20},
            },
        },
    }
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return path


def _build_fleet_home(other_runs: int) -> Path:
    """A crew home whose promoting run and ``other_runs`` peers have homes."""
    runs_root = crew_runs.runs_dir()
    if runs_root.is_dir():
        shutil.rmtree(runs_root)
    runs_root.mkdir(parents=True)
    _write_rollout(
        runs_root / PROMOTING_RUN,
        PROMOTING_SESSION,
        context_window=OWN_CONTEXT_WINDOW,
    )
    for index in range(other_runs):
        other = runs_root / f"r-peer-{index:03d}"
        _write_rollout(
            other,
            f"9f9e9d9c-{index:04d}-4000-8000-000000000000",
            context_window=OTHER_CONTEXT_WINDOW,
        )
    return runs_root


@pytest.fixture()
def client_sessions_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary client sessions root, patched where the reader resolves it."""
    root = tmp_path / "client-sessions"
    root.mkdir()
    monkeypatch.setattr(rollout, "CLIENT_SESSIONS_DIR", root)
    return root


def _stat_count(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Record every directory the receipt lookup stats while active."""
    seen: list[Path] = []
    original = Path.is_dir

    def counted_is_dir(self: Path) -> bool:
        seen.append(Path(self))
        return original(self)

    monkeypatch.setattr(Path, "is_dir", counted_is_dir)
    return seen


def _harvest(
    monkeypatch: pytest.MonkeyPatch, *, run_id: str | None
) -> tuple[int, dict]:
    seen = _stat_count(monkeypatch)
    record: dict[str, object] = {"agent": {"model": "deepseek-v4.1-flash"}}
    if run_id is not None:
        record["run_id"] = run_id
    result = _harvest_lane_receipt(
        record,
        session_id=PROMOTING_SESSION,
        observed_at="2026-11-10T19:00:00Z",
    )
    return len(seen), result


def test_a_promotion_stats_only_its_own_run_home(
    client_sessions_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _build_fleet_home(200)
    wide, wide_result = _harvest(monkeypatch, run_id=PROMOTING_RUN)

    _build_fleet_home(20)
    narrow, narrow_result = _harvest(monkeypatch, run_id=PROMOTING_RUN)

    assert wide == narrow, (
        f"the receipt lookup's stat count grew with the run population: "
        f"{narrow} stats with 20 peers, {wide} with 200"
    )
    assert wide_result["effective_context_window"] == OWN_CONTEXT_WINDOW
    assert narrow_result["effective_context_window"] == OWN_CONTEXT_WINDOW
    assert wide_result["effective_context_window"] != OTHER_CONTEXT_WINDOW


def test_the_stat_count_grows_when_the_lookup_names_no_run(
    client_sessions_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The counter sees a population, so a constant count is a real absence."""
    _build_fleet_home(20)
    narrow, _ = _harvest(monkeypatch, run_id=None)

    _build_fleet_home(200)
    wide, _ = _harvest(monkeypatch, run_id=None)

    assert wide > narrow, (
        f"an unconfined lookup statted {wide} directories with 200 peers and "
        f"{narrow} with 20; the instrument cannot see the population"
    )
