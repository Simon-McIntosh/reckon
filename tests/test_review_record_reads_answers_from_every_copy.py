"""A review record is read with the answers every copy of its run holds.

A review worker writes its record by hand, as JSON, to the paths its dispatch
grants: the plain staging file and the head-keyed sibling. ``crew dispose``
writes its disposition back to whichever copy its reader selects, so the two
writes can land on different files and the copy a by-head reader selects can
carry none of what the other holds. These cases dispose on the plain staging
file, then write the head-keyed sibling again directly as a worker finishing a
record does — without the disposition, the response or the response event — and
read them back through ``stored_record`` and through the obligations reader,
both of which select the sibling for that head. Each fails when
``stored_record`` returns the selected copy alone, which is the state before
the answers were merged from every copy.

One case reads through a named staging store and holds the merge to that store:
a copy sitting only in the configured store is not pulled into a read resolved
from another one.

Every store, repository and live pointer resolves inside a temporary
configuration home; nothing touches the operator's own store.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon import flight
from reckon.crew import review as review_module
from reckon.crew import runs

obligations = importlib.import_module("reckon.crew.obligations")

PROJECT = "reads-answers"
SESSION = "coordinator-reads-answers"
PLAN = "reads-answers-plan"

REVIEWED = "r-20261007T100000000000-reviewed-subject"
REVIEW = "r-20261007T110000000000-review-run"
BASE = "a" * 40
HEAD = "543d3a99d6b1c4a1c2d3e4f50617283940a1b2c3"
SUB_FLOOR_SCORE = 5
CLEAN_SCORE = 18
FOLD_NODE = "repair-durability-node"

ANSWERS_KEY = review_module.DIMENSION_DISPOSITIONS_KEY
FINDING = "durability-1"
RESPONSE_WHEN = "2026-10-07T10:30:00+00:00"
# The shape a response writer stores: the latest-per-finding map entry and the
# append-only event that keeps the history of what was answered.
RESPONSE = {
    "action": "acted",
    "reason": "",
    "by": "coordinator",
    "when": RESPONSE_WHEN,
}
RESPONSE_EVENT = {"finding": FINDING, **RESPONSE}

# The derivation is read at a fixed instant after the record's own timestamp, so
# the duty's age is a function of the fixture rather than of the wall clock.
OBSERVED_AT = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
RECORDED_AT = "2026-10-07T10:00:00+00:00"


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
class Copies:
    """The world one assertion reads its record from."""

    home: Path
    repository: Path
    tree: Path
    head_sha: str
    reviewed: str
    plain: Path
    sibling: Path


def _record(*, head: str | None) -> dict:
    """One review record, keyed to a head only when a head is named.

    The plain staging copy carries no revision pair — the shape the file a
    disposition was written to has in the trace this closes — while the
    head-keyed sibling carries the revision it reviewed.
    """
    scores = dict.fromkeys(review_module.REVIEW_DIMENSIONS, CLEAN_SCORE)
    scores["durability"] = SUB_FLOOR_SCORE
    record: dict = {
        "project": PROJECT,
        "reviewed_run_id": REVIEWED,
        "review_run_id": REVIEW,
        "status": "parsed",
        "scores": scores,
        "absent": [],
        "total": sum(scores.values()),
        "timestamp": RECORDED_AT,
    }
    if head is not None:
        record["reviewed_base_sha"] = BASE
        record["reviewed_head_sha"] = head
    return record


def _write_staging(path: Path, payload: dict) -> Path:
    """Write one staging record directly, as a hand-written record may sit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_pointer(reviewed: str, *, worktree: Path) -> None:
    """Write the live pointer naming the tree the reviewed run's work sits in."""
    runs._write_json(
        runs.pointer_path(reviewed),
        {
            "run_id": reviewed,
            "project": PROJECT,
            "session": SESSION,
            "process_alive": False,
            "worktree": str(worktree),
            "node": {
                "id": reviewed,
                "plan": PLAN,
                "section": "fixture-section",
                "time_budget": "20m",
                "write_paths": ["seed.txt"],
            },
        },
    )


@pytest.fixture()
def copies(isolated_reckon_home: Path, tmp_path: Path, monkeypatch) -> Copies:
    """Stand up a store whose disposition and head-keyed sibling disagree."""
    home = isolated_reckon_home
    monkeypatch.delenv("RECKON_FLIGHT_CONFIG", raising=False)
    monkeypatch.delenv("RECKON_WORKTREES", raising=False)
    monkeypatch.delenv("RECKON_STATE_ROOT", raising=False)
    monkeypatch.setattr(obligations, "_utc_now", lambda: OBSERVED_AT)

    repository = tmp_path / "repo"
    (repository / "docs" / "state" / PROJECT).mkdir(parents=True)
    (repository / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed answers fixture"),
    ):
        _git(repository, *arguments)
    (home / "mounts.json").write_text(
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
    (tree / "work.txt").write_text("head\n", encoding="utf-8")
    _git(tree, "add", "work.txt")
    _git(tree, "commit", "-q", "-m", "test: head revision")
    head_sha = _git(tree, "rev-parse", "HEAD")

    _write_pointer(REVIEWED, worktree=tree)

    # The plain staging copy is the only copy when the disposition is recorded,
    # so the writer's own newest-copy selection lands the disposition on it.
    plain = _write_staging(
        review_module.review_path(PROJECT, REVIEWED), _record(head=None)
    )
    _dispose_run()
    stored_plain = json.loads(plain.read_text(encoding="utf-8"))
    assert review_module.DIMENSION_DISPOSITIONS_KEY in stored_plain, (
        "premise: the disposition landed on the plain staging copy"
    )

    # A coordinator then records a response and its event on the plain copy the
    # reader selected — the answer the delivered body carries none of.
    stored_plain["responses"] = {FINDING: dict(RESPONSE)}
    stored_plain["response_events"] = [dict(RESPONSE_EVENT)]
    plain.write_text(json.dumps(stored_plain), encoding="utf-8")

    # The review worker then finishes and stores its head-keyed sibling built
    # from the delivered body, which carries no disposition and no answer.
    sibling = _write_staging(
        review_module.review_path(PROJECT, REVIEWED, reviewed_head_sha=head_sha),
        _record(head=head_sha),
    )

    return Copies(
        home=home,
        repository=repository,
        tree=tree,
        head_sha=head_sha,
        reviewed=REVIEWED,
        plain=plain,
        sibling=sibling,
    )


def _dispose_run() -> None:
    """Record the sub-floor disposition the way the command does."""
    review_module.record_dimension_disposition(
        PROJECT, REVIEWED, "durability", kind="folded", node=FOLD_NODE
    )


def _sub_floor_rows() -> list[dict]:
    """The sub-floor duty rows the obligations reader derives."""
    report = obligations.obligations(PROJECT, SESSION)
    return [
        row
        for row in report["obligations"]
        if row["kind"] == obligations.SUB_FLOOR_DUTY_KIND
    ]


def test_a_disposition_on_the_plain_copy_is_read_through_the_head_keyed_sibling(
    copies: Copies,
) -> None:
    """The sibling the reader selects carries the disposition its sibling holds.

    The head-keyed reader selects the sibling, which was written without the
    disposition, the response or the response event; only the merge of every
    copy's answers puts each back. All three are confirmed on the plain copy and
    absent from the sibling, so the values the reader returns can only have come
    from the merge.
    """
    sibling_raw = json.loads(copies.sibling.read_text(encoding="utf-8"))
    assert review_module.DIMENSION_DISPOSITIONS_KEY not in sibling_raw, (
        "the selected copy carries no disposition of its own"
    )
    assert not sibling_raw.get("responses") and not sibling_raw.get(
        "response_events"
    ), "the selected copy carries no response or response event of its own"

    path, record = review_module.stored_record(
        PROJECT, copies.reviewed, reviewed_head_sha=copies.head_sha
    )

    assert path == copies.sibling, "selection still chooses the head-keyed copy"
    assert record is not None
    disposition = record[review_module.DIMENSION_DISPOSITIONS_KEY]["durability"]
    assert disposition["kind"] == "folded"
    assert disposition["node"] == FOLD_NODE
    # The response and the response event written only to the plain copy are
    # read back through the sibling too.
    assert record["responses"][FINDING]["action"] == "acted"
    assert [event["finding"] for event in record["response_events"]] == [FINDING]


def test_a_copy_only_in_the_configured_store_is_not_merged_into_a_named_store(
    copies: Copies, tmp_path: Path
) -> None:
    """A read resolved from one store merges only that store's copies.

    ``stored_record`` can be pointed at a named staging store through
    ``base_dir``, and the merge must read the same store it selected from: a
    copy that exists only in the configured store would otherwise be pulled
    into a record selected from another one. The configured store's copy is
    confirmed present and merged by a read that resolves there, so the absence
    through the named store is a reading rather than an empty fixture.
    """
    named = tmp_path / "named-store"
    selected = _write_staging(
        review_module.review_path(
            PROJECT, copies.reviewed, base_dir=named, reviewed_head_sha=copies.head_sha
        ),
        _record(head=copies.head_sha),
    )

    path, record = review_module.stored_record(
        PROJECT,
        copies.reviewed,
        base_dir=named,
        reviewed_head_sha=copies.head_sha,
    )

    assert path == selected, "selection was resolved from the named store"
    assert record is not None
    assert review_module.DIMENSION_DISPOSITIONS_KEY not in record
    assert "responses" not in record and "response_events" not in record

    # The configured store's copy is genuinely there: a read that resolves there
    # merges its answers, so the named store's absence above is a real isolation.
    _default_path, default_record = review_module.stored_record(
        PROJECT, copies.reviewed, reviewed_head_sha=copies.head_sha
    )
    assert default_record is not None
    assert review_module.DIMENSION_DISPOSITIONS_KEY in default_record
    assert default_record["responses"][FINDING]["action"] == "acted"


def test_the_obligations_reader_retires_the_duty_through_the_sibling(
    copies: Copies,
) -> None:
    """The duty reader sees the disposition the selected copy does not hold.

    The sibling on its own still measures a sub-floor duty for durability, so
    the scenario is one that produces a row; the obligations reader, which
    selects that same sibling through ``stored_record``, must see the
    disposition the plain copy holds and raise no row.
    """
    floors = review_module.declared_dimension_floors(flight.resolve(PROJECT).config)
    standing = review_module.sub_floor_dimensions(
        json.loads(copies.sibling.read_text(encoding="utf-8")), floors
    )
    assert [row["dimension"] for row in standing] == ["durability"], (
        "premise: the selected copy alone produces a sub-floor duty"
    )

    assert _sub_floor_rows() == []
