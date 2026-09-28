"""A sub-floor review dimension is retired by the command that writes its disposition.

A stored review dimension below the floor flight configuration declares for it
is an obligation row until an entry in the closed set answers it. This module
runs that entry's writer through the CLI entry point — once as a fold naming
the node the finding went into, once as an exemption naming why it is not
being acted on — and reads the row back from the derivation a coordinator's
surface uses. The command is exercised the way an operator calls it, so the
fold case exits as an unknown subcommand at the revision before the command
existed — which is what makes this module fail there.

The refusals are asserted through the same entry point, because a refusal that
still wrote would be indistinguishable from an accepted call at every later
read. The fixture stands the world up inside a temporary config home — mounts,
a live pointer, the reviewed run's tree and the review itself — and the last
case resolves every root the derivation uses, so the real configuration home
is neither read nor written.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from reckon import flight
from reckon.cli import main as cli_main
from reckon.crew import review as review_module
from reckon.crew import runs

obligations_module = importlib.import_module("reckon.crew.obligations")

PROJECT = "disposition-fixture"
SESSION = "coordinator-fixture"
FIXTURE_PLAN = "fixture-plan"

# The shape this plan's own landings measured: a total that passes with room to
# spare while one dimension sits far below the rest.
PASSING_TOTAL = 79
SUB_FLOOR_SCORE = 5
CLEAN_SCORE = 16
FOLD_NODE = "repair-durability-node"

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
    tree: Path
    base_sha: str
    head_sha: str
    reviewed: str


def _emit(scores: dict[str, int], *, base_sha: str, head_sha: str) -> str:
    """Emitted review text carrying the revision pair and one score per line."""
    lines = [f"reviewed_base_sha: {base_sha}", f"reviewed_head_sha: {head_sha}"]
    lines.extend(f"SCORE {dimension}: {score}" for dimension, score in scores.items())
    return "\n".join(lines) + "\n"


def _store_review(reviewed_run_id: str, text: str, *, timestamp: str) -> Path:
    """Store one review through the production writer, as a reviewer would."""
    record = review_module.parse_review(text)
    record.update(
        {
            "project": PROJECT,
            "reviewed_run_id": reviewed_run_id,
            "timestamp": timestamp,
        }
    )
    return review_module.store_review(record)


def _write_pointer(run_id: str, *, worktree: Path) -> None:
    """Write the live pointer naming the tree the reviewed run's work sits in."""
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
    monkeypatch.delenv("RECKON_WORKTREES", raising=False)

    repository = tmp_path / "repo"
    (repository / "docs" / "state" / PROJECT).mkdir(parents=True)
    (repository / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed disposition fixture"),
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

    reviewed = "run-sub-floor"
    _write_pointer(reviewed, worktree=tree)
    _store_review(
        reviewed,
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

    monkeypatch.setattr(obligations_module, "_utc_now", lambda: OBSERVED_AT)
    return Fixture(
        config_home=config_home,
        tree=tree,
        base_sha=base_sha,
        head_sha=head_sha,
        reviewed=reviewed,
    )


def _dispose(fixture: Fixture, *arguments: str):
    """Invoke the disposition command through the CLI entry point."""
    return CliRunner().invoke(
        cli_main,
        [
            "crew",
            "dispose",
            "--project",
            PROJECT,
            "--run",
            fixture.reviewed,
            *arguments,
        ],
    )


def _sub_floor_rows() -> list[dict[str, Any]]:
    report = obligations_module.obligations(PROJECT, SESSION)
    return [
        row
        for row in report["obligations"]
        if row["kind"] == obligations_module.SUB_FLOOR_DUTY_KIND
    ]


def _stored_dispositions(reviewed_run_id: str) -> dict[str, Any]:
    stored = review_module.read_review(PROJECT, reviewed_run_id)
    assert stored is not None
    recorded = stored.get(review_module.DIMENSION_DISPOSITIONS_KEY)
    return dict(recorded) if isinstance(recorded, dict) else {}


def test_a_fold_naming_its_node_retires_the_row(fixture: Fixture) -> None:
    """The command's fold clears the finding without touching the reviewer's score."""
    assert [row["dimension"] for row in _sub_floor_rows()] == ["durability"]

    result = _dispose(
        fixture, "--dimension", "durability", "--kind", "folded", "--node", FOLD_NODE
    )
    assert result.exit_code == 0, result.output
    receipt = json.loads(result.output)
    assert receipt["disposition"]["kind"] == "folded"
    assert receipt["disposition"]["node"] == FOLD_NODE

    assert _sub_floor_rows() == []
    stored = review_module.read_review(PROJECT, fixture.reviewed)
    assert stored is not None
    assert stored["total"] == PASSING_TOTAL
    assert stored["scores"]["durability"] == SUB_FLOOR_SCORE
    assert _stored_dispositions(fixture.reviewed)["durability"]["node"] == FOLD_NODE


def test_an_exemption_recording_its_reason_retires_the_row(fixture: Fixture) -> None:
    """The other kind in the closed set clears a row and keeps the reason."""
    reason = "the finding is answered by the merged head reviewed beside it"
    result = _dispose(
        fixture,
        "--dimension",
        "durability",
        "--kind",
        "exempted",
        "--reason",
        reason,
    )
    assert result.exit_code == 0, result.output
    assert _sub_floor_rows() == []
    assert _stored_dispositions(fixture.reviewed)["durability"]["reason"] == reason


def test_a_legacy_copy_beside_the_keyed_record_takes_the_disposition(
    fixture: Fixture,
) -> None:
    """The command writes the file the obligation row is read from.

    A store can hold both files for one revision: the review at the
    revision-keyed path, and a legacy record carrying the same head beside it —
    the shape this plan's own review store holds. The head-first reader takes
    the first candidate carrying the head, so a command that selected the
    newest record, or wrote wherever the record's own fields pointed, could
    answer ok while the copy the row is read from kept its sub-floor finding.
    The premise is asserted first, the row standing; after the call the
    disposition is in exactly the file the reader reads and the row is gone.
    """
    head = fixture.head_sha
    keyed = review_module.review_path(PROJECT, fixture.reviewed, reviewed_head_sha=head)
    legacy = review_module.review_path(PROJECT, fixture.reviewed)
    assert keyed.is_file()
    legacy.write_text(keyed.read_text(encoding="utf-8"), encoding="utf-8")

    read_path, _record = review_module.stored_record(
        PROJECT, fixture.reviewed, reviewed_head_sha=head
    )
    assert read_path == legacy, "the row's record is the legacy copy"
    assert [row["dimension"] for row in _sub_floor_rows()] == ["durability"]

    result = _dispose(
        fixture, "--dimension", "durability", "--kind", "folded", "--node", FOLD_NODE
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["path"] == str(read_path)

    assert _sub_floor_rows() == []
    carrying = [
        path
        for path in (legacy, keyed)
        if review_module.DIMENSION_DISPOSITIONS_KEY
        in json.loads(path.read_text(encoding="utf-8"))
    ]
    assert carrying == [read_path]


REFUSALS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "unknown-dimension",
        ("--dimension", "charisma", "--kind", "folded", "--node", FOLD_NODE),
        "unknown review dimension",
    ),
    (
        "unknown-kind-with-a-reason",
        ("--dimension", "durability", "--kind", "noted", "--reason", "shorthand"),
        "unknown disposition",
    ),
    (
        "unknown-kind-alone",
        ("--dimension", "durability", "--kind", "noted"),
        "unknown disposition",
    ),
    (
        "fold-without-a-node",
        ("--dimension", "durability", "--kind", "folded"),
        "folded disposition must name",
    ),
    (
        "exemption-without-a-reason",
        ("--dimension", "durability", "--kind", "exempted"),
        "exempted disposition must record",
    ),
    (
        "fold-carrying-a-reason",
        (
            "--dimension",
            "durability",
            "--kind",
            "folded",
            "--node",
            FOLD_NODE,
            "--reason",
            "carried over from an exemption that was never recorded",
        ),
        "folded disposition carries no reason",
    ),
    (
        "exemption-carrying-a-node",
        (
            "--dimension",
            "durability",
            "--kind",
            "exempted",
            "--reason",
            "answered by the revision the merged head carries",
            "--node",
            FOLD_NODE,
        ),
        "exempted disposition names no node",
    ),
)


@pytest.mark.parametrize(
    "arguments, refusal",
    [case[1:] for case in REFUSALS],
    ids=[case[0] for case in REFUSALS],
)
def test_a_refused_disposition_leaves_the_row_standing(
    fixture: Fixture, arguments: tuple[str, ...], refusal: str
) -> None:
    """A refusal exits non-zero, names itself, and writes no disposition."""
    result = _dispose(fixture, *arguments)

    assert result.exit_code != 0, result.output
    assert refusal in result.output
    assert [row["dimension"] for row in _sub_floor_rows()] == ["durability"]
    assert _stored_dispositions(fixture.reviewed) == {}


def test_a_kind_outside_the_closed_set_is_not_honoured_when_stored(
    fixture: Fixture,
) -> None:
    """A hand-written entry of an unknown kind leaves the row standing.

    The store is reachable by hand, so the read-back is asserted on its own: an
    entry naming a kind the closed set does not define is not a disposition,
    however complete its other fields look.
    """
    path = review_module.review_path(
        PROJECT, fixture.reviewed, reviewed_head_sha=fixture.head_sha
    )
    record = json.loads(path.read_text(encoding="utf-8"))
    record[review_module.DIMENSION_DISPOSITIONS_KEY] = {
        "durability": {"kind": "noted", "reason": "shorthand only this reader knows"}
    }
    path.write_text(json.dumps(record), encoding="utf-8")

    assert [row["dimension"] for row in _sub_floor_rows()] == ["durability"]


def test_the_shipped_floors_stand_behind_a_review(fixture: Fixture) -> None:
    """The shipped defaults declare a floor for every dimension and catch half scale.

    Nothing in the fixture's configuration home declares a floor, so the only
    standard in play is the one the install ships. Removing it from the shipped
    defaults leaves every dimension unfloored, and the review's score at half
    the scale stops producing a row.
    """
    resolved = flight.resolve(PROJECT)
    floors = review_module.declared_dimension_floors(resolved.config)
    assert set(floors) == set(review_module.REVIEW_DIMENSIONS)

    rows = _sub_floor_rows()
    assert [row["dimension"] for row in rows] == ["durability"]
    assert rows[0]["score"] == SUB_FLOOR_SCORE
    assert rows[0]["floor"] > SUB_FLOOR_SCORE


def test_the_command_writes_only_inside_the_temporary_config_home(
    fixture: Fixture,
) -> None:
    """Every store the command resolves sits under the fixture's own config home.

    The real config home is not stat'd to assert this: an absence check on it
    would itself be a read of it. Resolving each root the command uses, and
    requiring the path it reports inside the temporary home, proves the same
    thing without touching the operator's review store.
    """
    assert review_module.review_store_root().is_relative_to(fixture.config_home)
    assert runs.pointer_path("probe").is_relative_to(fixture.config_home)

    result = _dispose(
        fixture, "--dimension", "durability", "--kind", "folded", "--node", FOLD_NODE
    )
    assert result.exit_code == 0, result.output
    written = Path(json.loads(result.output)["path"])
    assert written.is_relative_to(fixture.config_home)
    assert written.is_file()
