"""A findings read answers from an index another process left on this node.

A dispatch is a fresh process, so an index held only in memory starts cold every
time: it stats every review directory and parses every record. The committed
tree alone held thousands of records the day this was written, and a review
dispatch took minutes. The reader therefore persists the rows it derived to the
node-local cache the review store already keeps its own index in, and the
writers that change a record — the committed-store writer, the answer verb and
the code-review store — refresh the entry they wrote, so a fresh process answers
without opening a record at all.

Every case here runs against a synthesised crew home, checkout and code-review
store, with the node-local cache redirected into ``tmp_path``; the fixture
asserts afterwards that the operator's own cache directory was not touched.
Each call that is meant to be a fresh process drops the process-wide state the
reader keeps, which is exactly what a new process starts without.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from reckon import file_memo
from reckon.crew import findings as findings_module
from reckon.crew import plan_review, review_records
from reckon.crew import review as review_module
from tests.helpers.git import git as _git

PROJECT = "write-time-index-findings"
FILE = "reckon/a.py"
OTHER_FILE = "reckon/b.py"

RUN_A = "r-reviewed-a"
RUN_B = "r-reviewed-b"
REVIEW_A = "r-20261008T090000000000-review-a"
REVIEW_B = "r-20261008T091000000000-review-b"

PLAN_SLUG = "indexed-plan"
PLAN_ANSWERED = "r-20261008T092000000000-plan-review"
PLAN_WRITTEN = "r-20261008T093000000000-plan-review"

CODE_KEEP = "store-side-keeper"
CODE_NEW = "store-side-newcomer"
CODE_WRITTEN = "store-side-written"


def _no_index_update(*_args: object, **_keywords: object) -> None:
    """Stand in for the writer hook, so a control can withhold it."""


def _snapshot(directory: Path) -> list[str]:
    try:
        return sorted(str(path) for path in directory.iterdir())
    except OSError:
        return []


@pytest.fixture()
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A git checkout whose docs directory is the project's mount.

    The node-local cache is redirected into the test's own tree, and the
    operator's real one is snapshotted before the redirect so the test can
    assert it was never written.
    """
    real_cache = review_records.node_local_cache_dir(PROJECT)
    before = _snapshot(real_cache)
    review_records.forget_cache_code_stamps()

    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "docs" / "state" / PROJECT / "ledger.jsonl").write_text(
        "", encoding="utf-8"
    )
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "docs"),
        ("commit", "-q", "-m", "test: seed the checkout"),
    ):
        _git(root, *arguments)
    (tmp_path / "runtime").mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    (Path(os.environ["RECKON_HOME"]) / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )

    yield root

    assert _snapshot(real_cache) == before, (
        "the read wrote into the operator's own node-local cache directory"
    )


@pytest.fixture()
def record_reads(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Every record path the reader opened, in the order it opened them."""
    opened: list[Path] = []
    original = review_module._committed_record_at

    def counted(path):
        opened.append(Path(path))
        return original(path)

    monkeypatch.setattr(review_module, "_committed_record_at", counted)
    return opened


def _forget_process() -> None:
    """Drop what one process remembers, as a newly started process would."""
    findings_module._INDEXES.clear()
    review_records.forget_cache_code_stamps()
    file_memo.clear()


def _index_files() -> list[Path]:
    directory = review_records.node_local_cache_dir(PROJECT)
    return sorted(directory.glob("findings-index-*.json"))


def _run_record(reviewed_run_id: str, review_run_id: str, text: str) -> dict:
    return {
        "project": PROJECT,
        "reviewed_run_id": reviewed_run_id,
        "review_run_id": review_run_id,
        "reviewed_head_sha": "0" * 40,
        "scores": {"evidence": 17},
        "findings": [
            {"file": FILE, "line": "12", "text": text, "severity": "blocking"}
        ],
    }


def _plan_record(review_run_id: str, *finding_ids: str) -> dict:
    return {
        "project": PROJECT,
        "plan_slug": PLAN_SLUG,
        "plan_version": 3,
        "review_run_id": review_run_id,
        "findings": [
            {
                "id": finding_id,
                "anchor": f"{OTHER_FILE}:40",
                "text": f"{finding_id} raised against the plan",
                "type": "blocking",
            }
            for finding_id in finding_ids
        ],
    }


def _code_record(key: str, text: str) -> dict:
    return {
        "project": PROJECT,
        "subject": "code",
        "neighbourhood_key": key,
        "reviewed_revision": "0" * 40,
        "findings": [
            {
                "id": f"{key}-1",
                "anchor": f"{FILE}:3",
                "text": text,
                "class": "blocking",
            }
        ],
    }


def _store_two(checkout: Path) -> None:
    review_records.store_committed_review(
        _run_record(RUN_A, REVIEW_A, "unguarded index"), root=checkout
    )
    review_records.store_committed_review(
        _run_record(RUN_B, REVIEW_B, "stale docstring"), root=checkout
    )


def _texts(payload: dict) -> list[str]:
    return sorted(str(row["text"]) for row in payload["rows"])


def test_a_fresh_process_opens_no_record_when_the_index_is_current(
    checkout: Path, record_reads: list[Path]
) -> None:
    _store_two(checkout)
    findings_module.findings_by_path(PROJECT, root=checkout)
    record_reads.clear()
    _forget_process()

    payload = findings_module.findings_by_path(PROJECT, root=checkout)

    assert _texts(payload) == ["stale docstring", "unguarded index"]
    assert record_reads == []


def test_deleting_the_index_makes_the_next_read_open_every_record(
    checkout: Path, record_reads: list[Path]
) -> None:
    """The negative control: the reuse above is the index, and nothing else."""
    _store_two(checkout)
    findings_module.findings_by_path(PROJECT, root=checkout)
    for path in _index_files():
        path.unlink()
    record_reads.clear()
    _forget_process()

    payload = findings_module.findings_by_path(PROJECT, root=checkout)

    assert len(record_reads) == 2
    assert _texts(payload) == ["stale docstring", "unguarded index"]


def test_a_record_committed_by_promotion_is_returned_by_the_next_read(
    checkout: Path, record_reads: list[Path]
) -> None:
    _store_two(checkout)
    findings_module.findings_by_path(PROJECT, root=checkout)

    review_records.store_committed_review(
        _run_record("r-reviewed-c", "r-20261008T093000000000-review-c", "a third"),
        root=checkout,
    )
    record_reads.clear()
    _forget_process()

    payload = findings_module.findings_by_path(PROJECT, root=checkout)

    assert "a third" in _texts(payload)


def test_an_answer_is_returned_without_opening_another_record(
    checkout: Path, record_reads: list[Path]
) -> None:
    committed = review_records.store_committed_review(
        _plan_record(PLAN_ANSWERED, "answer-me"), root=checkout
    )
    findings_module.findings_by_path(PROJECT, root=checkout)

    stored = json.loads(committed.read_text(encoding="utf-8"))
    plan_review.record_response(stored, "answer-me", action="noted", by="worker")
    record_reads.clear()
    _forget_process()

    payload = findings_module.findings_by_path(PROJECT, root=checkout)

    answers = {row["id"]: row["response"] for row in payload["rows"]}
    assert answers["answer-me"] is not None
    assert answers["answer-me"]["action"] == "noted"
    assert record_reads == []


def test_a_record_written_behind_the_index_is_returned(
    checkout: Path, record_reads: list[Path]
) -> None:
    _store_two(checkout)
    findings_module.findings_by_path(PROJECT, root=checkout)

    directory = (
        review_module.committed_review_root(PROJECT, root=checkout)
        / review_module.COMMITTED_RUN_DIRNAME
        / "r-reviewed-behind"
    )
    directory.mkdir(parents=True)
    behind = _run_record(
        "r-reviewed-behind", "r-20261008T094000000000-behind", "behind the index"
    )
    (directory / "r-20261008T094000000000-behind.json").write_text(
        json.dumps(behind), encoding="utf-8"
    )
    _forget_process()

    payload = findings_module.findings_by_path(PROJECT, root=checkout)

    assert "behind the index" in _texts(payload)


def test_a_stored_code_review_is_returned_without_opening_another(
    checkout: Path, record_reads: list[Path]
) -> None:
    plan_review.store_code_review(_code_record(CODE_KEEP, "kept neighbourhood"))
    findings_module.findings_by_path(PROJECT)
    record_reads.clear()
    _forget_process()

    plan_review.store_code_review(_code_record(CODE_NEW, "new neighbourhood"))
    record_reads.clear()
    _forget_process()

    payload = findings_module.findings_by_path(PROJECT)

    assert "new neighbourhood" in _texts(payload)
    assert record_reads == []


def test_a_change_to_a_row_source_invalidates_every_entry(
    checkout: Path,
    record_reads: list[Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sources = findings_module._index_sources()
    assert Path(review_records.__file__).resolve() in sources

    stand_in = tmp_path / "row-source.py"
    stand_in.write_text("# one\n", encoding="utf-8")
    monkeypatch.setattr(findings_module, "_index_sources", lambda: (*sources, stand_in))
    review_records.forget_cache_code_stamps()

    _store_two(checkout)
    findings_module.findings_by_path(PROJECT, root=checkout)
    record_reads.clear()

    stand_in.write_text("# two\n", encoding="utf-8")
    _forget_process()

    findings_module.findings_by_path(PROJECT, root=checkout)

    assert len(record_reads) == 2


# ── The writer hooks ────────────────────────────────────────────────────────
# Each writer updates the entry it wrote, so the next process answers without
# opening the record. The control is the same write with the hook switched off:
# the read then opens exactly the record that writer wrote, and no other.


def _write_committed(checkout: Path, monkeypatch: pytest.MonkeyPatch, off: bool) -> str:
    if off:
        monkeypatch.setattr(findings_module, "note_record_written", _no_index_update)
    review_records.store_committed_review(
        _run_record("r-reviewed-w", "r-20261008T095000000000-review-w", "written late"),
        root=checkout,
    )
    return "written late"


def _write_answer(checkout: Path, monkeypatch: pytest.MonkeyPatch, off: bool) -> str:
    committed = review_records.store_committed_review(
        _plan_record(PLAN_WRITTEN, "answer-late"), root=checkout
    )
    findings_module.findings_by_path(PROJECT, root=checkout)
    if off:
        monkeypatch.setattr(findings_module, "note_record_written", _no_index_update)
    stored = json.loads(committed.read_text(encoding="utf-8"))
    plan_review.record_response(stored, "answer-late", action="noted", by="worker")
    return "answer-late raised against the plan"


def _write_code_review(
    checkout: Path, monkeypatch: pytest.MonkeyPatch, off: bool
) -> str:
    plan_review.store_code_review(_code_record(CODE_KEEP, "kept neighbourhood"))
    findings_module.findings_by_path(PROJECT)
    if off:
        monkeypatch.setattr(findings_module, "note_record_written", _no_index_update)
    plan_review.store_code_review(_code_record(CODE_WRITTEN, "written late review"))
    return "written late review"


WRITERS = [
    pytest.param(_write_committed, id="committed-store"),
    pytest.param(_write_answer, id="answer-verb"),
    pytest.param(_write_code_review, id="code-review-store"),
]


@pytest.mark.parametrize("writer", WRITERS)
def test_each_writer_leaves_the_next_read_no_record_to_open(
    writer,
    checkout: Path,
    record_reads: list[Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store_two(checkout)
    findings_module.findings_by_path(PROJECT, root=checkout)

    text = writer(checkout, monkeypatch, False)

    record_reads.clear()
    _forget_process()
    payload = findings_module.findings_by_path(PROJECT, root=checkout)

    assert text in _texts(payload)
    assert record_reads == [], "the writer left a record for the reader to open"


@pytest.mark.parametrize("writer", WRITERS)
def test_without_the_writer_hook_the_same_read_opens_one_record(
    writer,
    checkout: Path,
    record_reads: list[Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store_two(checkout)
    findings_module.findings_by_path(PROJECT, root=checkout)

    text = writer(checkout, monkeypatch, True)

    record_reads.clear()
    _forget_process()
    payload = findings_module.findings_by_path(PROJECT, root=checkout)

    assert text in _texts(payload)
    assert len(record_reads) == 1, (
        "with the hook withheld the read opened "
        f"{len(record_reads)} records, so the hook is not what spares it"
    )
