"""A review dimension below its configured floor stays a visible finding.

The duty this module asserts is derived from a stored review rather than
projected from a live run's state, so the fixture stands up everything the
derivation reads — a temporary config home, live pointers, a reviewed run's
tree, and the reviews themselves — inside a temporary directory. Two reviews
carry a passing total: one with a durability at the score the measured pair
from this plan's own landings took, and one with every dimension at or above
its floor. A third names a superseded revision, so a record that is not about
the run's current work is seen to produce nothing.

Floors are declared in flight configuration, so the fixture writes the host
layer of the temporary config home and moves a floor between assertions rather
than editing the review: the verdict has to follow the configuration, not the
record.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import review as review_module
from reckon.crew import runs

obligations_module = importlib.import_module("reckon.crew.obligations")

PROJECT = "sub-floor-fixture"
SESSION = "coordinator-fixture"
FIXTURE_PLAN = "fixture-plan"

# The shape this plan's own landings measured: a total that passes with room to
# spare while one dimension sits far below the rest. Averaging is what hid it.
PASSING_TOTAL = 79
SUB_FLOOR_SCORE = 5
CLEAN_TOTAL = 80
CLEAN_SCORE = 16
DECLARED_FLOOR = 10

OBSERVED_AT = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


def _git(tree: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=tree,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@dataclass(frozen=True)
class Fixture:
    """The temporary world one assertion reads its report from."""

    config_home: Path
    repository: Path
    tree: Path
    base_sha: str
    head_sha: str
    below: str
    clean: str
    stale: str


def _emit(scores: dict[str, int], *, base_sha: str, head_sha: str) -> str:
    """Emitted review text carrying the revision pair and one score per line."""
    lines = [f"reviewed_base_sha: {base_sha}", f"reviewed_head_sha: {head_sha}"]
    lines.extend(f"SCORE {dimension}: {score}" for dimension, score in scores.items())
    return "\n".join(lines) + "\n"


def _store(
    run_id: str,
    text: str,
    *,
    timestamp: str,
) -> None:
    """Store one review through the production writer, as a reviewer would."""
    record = review_module.parse_review(text)
    record.update(
        {
            "project": PROJECT,
            "reviewed_run_id": run_id,
            "timestamp": timestamp,
        }
    )
    review_module.store_review(record)


def _write_pointer(run_id: str, *, worktree: Path) -> None:
    """Write one live pointer naming the tree the run's work sits in."""
    runs._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": SESSION,
            "process_alive": False,
            "worktree": str(worktree),
            "node": {
                "id": run_id,
                "plan": FIXTURE_PLAN,
                "section": "fixture-section",
                "time_budget": "20m",
                "write_paths": ["seed.txt"],
            },
        },
    )


@pytest.fixture()
def fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fixture:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.delenv("RECKON_FLIGHT_CONFIG", raising=False)

    repository = tmp_path / "repo"
    (repository / "docs" / "state" / PROJECT).mkdir(parents=True)
    (repository / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed sub-floor fixture"),
    ):
        _git(repository, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )

    tree = tmp_path / "run-tree"
    tree.mkdir()
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(tree, *arguments)
    (tree / "work.txt").write_text("base\n", encoding="utf-8")
    _git(tree, "add", "work.txt")
    _git(tree, "commit", "-q", "-m", "test: base revision")
    base_sha = _git(tree, "rev-parse", "HEAD")
    (tree / "work.txt").write_text("head\n", encoding="utf-8")
    _git(tree, "add", "work.txt")
    _git(tree, "commit", "-q", "-m", "test: head revision")
    head_sha = _git(tree, "rev-parse", "HEAD")

    below, clean, stale = "run-sub-floor", "run-clean", "run-superseded"
    for run_id in (below, clean, stale):
        _write_pointer(run_id, worktree=tree)

    _store(
        below,
        _emit(
            {
                "goal_fidelity": 19,
                "evidence": 19,
                "scope_discipline": 18,
                "durability": SUB_FLOOR_SCORE,
                "fit": 18,
            },
            base_sha=base_sha,
            head_sha=head_sha,
        ),
        timestamp="2026-09-28T11:00:00+00:00",
    )
    _store(
        clean,
        _emit(
            dict.fromkeys(review_module.REVIEW_DIMENSIONS, CLEAN_SCORE),
            base_sha=base_sha,
            head_sha=head_sha,
        ),
        timestamp="2026-09-28T11:30:00+00:00",
    )
    # A review of the revision the run carried before its current head: it
    # describes work that is no longer what the run holds, so it is not
    # evidence about the run's current revision and owes nothing.
    _store(
        stale,
        _emit(
            {
                "goal_fidelity": 19,
                "evidence": 19,
                "scope_discipline": 18,
                "durability": SUB_FLOOR_SCORE,
                "fit": 18,
            },
            base_sha=base_sha,
            head_sha=base_sha,
        ),
        timestamp="2026-09-28T10:30:00+00:00",
    )

    monkeypatch.setattr(obligations_module, "_utc_now", lambda: OBSERVED_AT)
    return Fixture(
        config_home=config_home,
        repository=repository,
        tree=tree,
        base_sha=base_sha,
        head_sha=head_sha,
        below=below,
        clean=clean,
        stale=stale,
    )


def _write_host_flight(config_home: Path, floors: dict[str, int]) -> None:
    """Declare dimension floors in the temporary config home's host layer."""
    (config_home / "flight.yaml").write_text(
        "gates:\n  dimension_floors:\n"
        + "".join(f"    {dimension}: {floor}\n" for dimension, floor in floors.items()),
        encoding="utf-8",
    )


def _sub_floor_rows() -> list[dict[str, object]]:
    report = obligations_module.obligations(PROJECT, SESSION)
    return [
        row
        for row in report["obligations"]
        if row["kind"] == obligations_module.SUB_FLOOR_DUTY_KIND
    ]


def test_a_dimension_below_its_floor_surfaces_beside_a_passing_total(
    fixture: Fixture,
) -> None:
    """The durability-5 review produces one row naming its run, score and floor."""
    stored = review_module.read_review(PROJECT, fixture.below)
    assert stored is not None
    assert stored["total"] == PASSING_TOTAL
    clean = review_module.read_review(PROJECT, fixture.clean)
    assert clean is not None
    assert clean["total"] == CLEAN_TOTAL

    rows = _sub_floor_rows()
    assert [row["run_id"] for row in rows] == [fixture.below]
    row = rows[0]
    assert row["dimension"] == "durability"
    assert row["score"] == SUB_FLOOR_SCORE
    assert row["floor"] == DECLARED_FLOOR
    assert row["plan"] == FIXTURE_PLAN
    assert "folded" in str(row["next_command"])
    assert "exempted" in str(row["next_command"])


def test_a_review_of_a_superseded_revision_owes_nothing(fixture: Fixture) -> None:
    """A record describing another revision neither produces nor suppresses a row."""
    assert fixture.stale not in {row["run_id"] for row in _sub_floor_rows()}
    assert review_module.read_review(PROJECT, fixture.stale) is not None


def test_a_recorded_disposition_retires_the_row(fixture: Fixture) -> None:
    """A fold naming its node clears the finding without touching the total."""
    review_module.record_dimension_disposition(
        PROJECT,
        fixture.below,
        "durability",
        kind="folded",
        node="repair-durability-node",
    )
    assert _sub_floor_rows() == []
    stored = review_module.read_review(PROJECT, fixture.below)
    assert stored is not None
    assert stored["total"] == PASSING_TOTAL
    assert (
        stored["dimension_dispositions"]["durability"]["node"]
        == "repair-durability-node"
    )


def test_the_closed_disposition_set_is_enforced(fixture: Fixture) -> None:
    """A kind outside the set, and a fold naming no node, are both refused."""
    with pytest.raises(ValueError, match="unknown disposition"):
        review_module.record_dimension_disposition(
            PROJECT, fixture.below, "durability", kind="noted"
        )
    with pytest.raises(ValueError, match="folded disposition must name"):
        review_module.record_dimension_disposition(
            PROJECT, fixture.below, "durability", kind="folded"
        )
    with pytest.raises(ValueError, match="exempted disposition must record"):
        review_module.record_dimension_disposition(
            PROJECT, fixture.below, "durability", kind="exempted"
        )
    assert [row["run_id"] for row in _sub_floor_rows()] == [fixture.below]


def test_the_floor_is_read_from_flight_config(fixture: Fixture) -> None:
    """Moving a declared floor moves the verdict, in both directions."""
    _write_host_flight(fixture.config_home, {"durability": 4})
    assert _sub_floor_rows() == []

    _write_host_flight(fixture.config_home, {"durability": DECLARED_FLOOR, "fit": 19})
    rows = _sub_floor_rows()
    assert [
        (row["run_id"], row["dimension"], row["score"], row["floor"]) for row in rows
    ] == [
        (fixture.below, "durability", SUB_FLOOR_SCORE, DECLARED_FLOOR),
        (fixture.below, "fit", 18, 19),
        (fixture.clean, "fit", CLEAN_SCORE, 19),
    ]


def test_an_exempted_disposition_records_its_reason(fixture: Fixture) -> None:
    """The other half of the closed set clears a row a raised floor produced."""
    _write_host_flight(fixture.config_home, {"fit": 19})
    assert {row["run_id"] for row in _sub_floor_rows()} == {
        fixture.below,
        fixture.clean,
    }

    review_module.record_dimension_disposition(
        PROJECT,
        fixture.clean,
        "fit",
        kind="exempted",
        reason="the fixture's fit finding is answered at the merged head",
    )
    assert {row["run_id"] for row in _sub_floor_rows()} == {fixture.below}


def test_the_report_reads_and_writes_only_the_temporary_config_home(
    fixture: Fixture,
) -> None:
    """Every store the report resolves sits under the fixture's own config home.

    The real config home is not stat'd to assert this: an absence check on it
    would itself be a read of it. Resolving each root the derivation uses, and
    requiring it inside the temporary home, proves the same thing without
    touching the operator's flight configuration or review store.
    """
    assert review_module.review_store_root().is_relative_to(fixture.config_home)
    assert runs.pointer_path("probe").is_relative_to(fixture.config_home)

    _sub_floor_rows()
    review_module.record_dimension_disposition(
        PROJECT, fixture.below, "durability", kind="folded", node="probe-node"
    )
    store = review_module.review_store_root() / PROJECT
    written = list(store.glob(f"{fixture.below}*.json"))
    assert written
    assert all(path.is_relative_to(fixture.config_home) for path in written)
