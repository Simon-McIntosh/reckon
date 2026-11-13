"""Disposition surfaces retain the identity of the review they are answering."""

from __future__ import annotations

import importlib
import json
import shlex
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon import mcp
from reckon.crew import recovery_review_dispatch, review_dispositions, runs
from reckon.crew import review as review_module
from tests.helpers.git import git

obligations = importlib.import_module("reckon.crew.obligations")
PROJECT = "disposition-surfaces-fixture"
SESSION = "disposition-coordinator"
RUN = "r-20261010T090000000000-reviewed-work"
EARLIER_TEXT = "r-20261010T100000000000-earlier-text-review"
TEXT = "r-20261010T110000000000-text-review"
FIGURE = "r-20261010T120000000000-figure-review"
LATER_TEXT = "r-20261010T130000000000-later-text-review"


@dataclass
class World:
    repository: Path
    pointer: dict
    paths: dict[str, Path]
    records: dict[str, dict]
    later_head: str


def _record(review_run: str, base: str, head: str, hour: int, *, figure=False):
    record = {
        "project": PROJECT,
        "reviewed_run_id": RUN,
        "review_run_id": review_run,
        "status": "parsed",
        "reviewed_base_sha": base,
        "reviewed_head_sha": head,
        "timestamp": f"2026-10-10T{hour:02}:00:00+00:00",
        "scores": {
            **dict.fromkeys(review_module.REVIEW_DIMENSIONS, 18),
            "durability": 5,
        },
        "findings": [],
    }
    if figure:
        record["rubric"] = "figure"
        record["findings"] = [
            {
                "file": "docs/figures/example/labels.svg",
                "line": "1",
                "text": "The axis label is clipped.",
                "severity": "blocking",
            }
        ]
    return record


@pytest.fixture()
def world(tmp_path: Path, isolated_reckon_home: Path) -> World:
    repository = tmp_path / "repo"
    (repository / "docs/state" / PROJECT).mkdir(parents=True)
    git(repository, "init", "-q", "-b", "main")
    git(repository, "config", "user.email", "test@example.invalid")
    git(repository, "config", "user.name", "Test")
    revisions = []
    for value in ("base", "reviewed"):
        (repository / "work.txt").write_text(value + "\n")
        git(repository, "add", "work.txt")
        git(
            repository,
            "commit",
            "-q",
            "-m",
            f"test: {value} content",
            "-m",
            "Seed independent review revisions.",
        )
        revisions.append(git(repository, "rev-parse", "HEAD"))
    revisions.append(
        git(
            repository,
            "commit-tree",
            "HEAD^{tree}",
            "-p",
            "HEAD",
            "-m",
            "test: prepare a subsequent reviewed revision",
        )
    )
    (isolated_reckon_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")})
    )
    pointer = {
        "run_id": RUN,
        "project": PROJECT,
        "session": SESSION,
        "repo": str(repository),
        "worktree": str(repository),
        "process_alive": False,
        "node": {"id": "reviewed-work", "plan": "fixture", "write_paths": ["work.txt"]},
    }
    runs._write_json(runs.pointer_path(RUN), pointer)
    records = {
        EARLIER_TEXT: _record(EARLIER_TEXT, revisions[0], revisions[0], 10),
        TEXT: _record(TEXT, revisions[0], revisions[1], 11),
        FIGURE: _record(FIGURE, revisions[0], revisions[1], 12, figure=True),
    }
    paths = {key: review_module.store_review(record) for key, record in records.items()}
    return World(repository, pointer, paths, records, revisions[2])


def test_mcp_dispose_writes_only_the_named_review(world: World) -> None:
    before = {key: path.read_bytes() for key, path in world.paths.items()}
    receipt = mcp._crew(
        action="dispose",
        project=PROJECT,
        run_id=RUN,
        review_run=EARLIER_TEXT,
        dimension="durability",
        kind="exempted",
        reason="Covered by an independent repair.",
    )
    assert receipt["ok"], receipt
    assert receipt["review_run_id"] == EARLIER_TEXT
    assert receipt["path"] == str(world.paths[EARLIER_TEXT])
    answered = json.loads(world.paths[EARLIER_TEXT].read_text())
    assert (
        answered[review_module.DIMENSION_DISPOSITIONS_KEY]["durability"]["kind"]
        == "exempted"
    )
    assert world.paths[TEXT].read_bytes() == before[TEXT]
    assert world.paths[FIGURE].read_bytes() == before[FIGURE]


def test_a_dimension_duty_keeps_the_record_it_was_derived_from(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Delivery can advance the store between deriving the score and rendering
    # its command. The command must still answer the score shown in that row.
    derive = review_dispositions.sub_floor_dimensions

    def deliver_during_derivation(record, floors):
        result = derive(record, floors)
        review_module.store_review(
            _record(LATER_TEXT, world.later_head, world.later_head, 13)
        )
        return result

    monkeypatch.setattr(
        review_dispositions, "sub_floor_dimensions", deliver_during_derivation
    )
    duties = obligations._sub_floor_items_by_session(
        PROJECT,
        {"durability": 10},
        now=datetime(2026, 10, 10, 14, tzinfo=UTC),
    )[SESSION]
    assert len(duties) == 1
    assert duties[0]["score"] == 5
    for key in ("next_command", "exempt_alternative"):
        words = shlex.split(duties[0][key])
        assert words[words.index("--review") + 1] == TEXT


def test_a_blocking_figure_duty_names_the_figure_record(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        obligations, "_refusal_speaks_for_revision", lambda *_args, **_kwargs: True
    )
    pointer = {
        **world.pointer,
        obligations.recovery.REVIEW_DISPATCH_FIELD: {"status": "refused"},
    }
    duty = obligations._review_dispatch_refusal(
        PROJECT,
        pointer,
        row={},
        head=world.later_head,
        now=datetime(2026, 10, 10, 14, tzinfo=UTC),
    )
    assert duty is not None
    assert f"--review {FIGURE} --rubric figure" in duty["next_command"]
    refusal = recovery_review_dispatch._open_review_refusal(world.pointer, dry_run=True)
    assert refusal is not None and refusal["refused"]
    assert f"--review {FIGURE} --rubric figure" in refusal["reason"]
