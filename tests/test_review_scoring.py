"""The reviewer artefact: a prompt, a five-dimension schema, a parser and a
durable store, and the falsifiers that keep each half honest.

An independent review is a second opinion the worker did not author, so its
record must survive the run directory and the worktree that produced it, and
its parse must not quietly reinterpret what the reviewer said: a missing
dimension is named absent rather than summed over fewer, an out-of-range score
is refused rather than clamped, and text that does not parse is stored with
its verbatim text and a status that says so.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon import _store
from reckon.crew import review as review_module

VALID_TEXT = """\
VERDICT goal: the node stores a review durably; reckon/crew/review.py:187 writes it under the config home.
VERDICT done_when: the done_when names tests/test_review_scoring.py and the manifest records that run.
VERDICT write_paths: read the manifest; every path in the diff is inside the declared scope.
VERDICT manifest: read; it names tests/test_review_scoring.py and its result.
VERDICT diff: read commit by commit against the base; four files changed, all declared.
CALL_SITES: reckon/crew/review.py:187
SCORE goal_fidelity: 18
JUSTIFICATION goal_fidelity: reckon/crew/review.py:81 reads the prompt from disk on every call
SCORE evidence: 15
JUSTIFICATION evidence: the gate names tests/test_review_scoring.py and its result is recorded
SCORE scope_discipline: 17
JUSTIFICATION scope_discipline: every path in the diff is inside the declared write paths
SCORE durability: 19
JUSTIFICATION durability: tests/test_review_scoring.py fails if an out-of-range score is clamped
SCORE fit: 16
JUSTIFICATION fit: the module follows the surrounding style of reckon/crew/summary.py
FINDING reckon/crew/query.py:120 an out-of-scope helper was added to a file the node was not fenced to write
"""


def _metadata_record(**overrides: object) -> dict:
    return {
        "project": "reckon",
        "reviewed_run_id": "r-reviewed-run",
        "review_run_id": "r-review-run",
        "backend": "claude-sonnet",
        "model": "claude-sonnet-5",
        "timestamp": "2026-09-06T17:30:00+00:00",
        **overrides,
    }


def _config_file_set(root: Path) -> set[str]:
    if not root.is_dir():
        return set()
    return {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()}


# ── The schema ──────────────────────────────────────────────────────────────


def test_schema_names_five_dimensions_once_with_the_maximum_stated_once() -> None:
    assert len(review_module.REVIEW_DIMENSIONS) == 5
    assert len(set(review_module.REVIEW_DIMENSIONS)) == 5
    assert review_module.REVIEW_MAX_SCORE == 20
    target = review_module.review_path("reckon", "r-any")
    assert "runs" not in target.parts
    assert "crew" in target.parts
    assert "reviews" in target.parts


# ── The prompt ──────────────────────────────────────────────────────────────


def test_prompt_is_read_from_disk_at_call_time_and_names_every_dimension(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    loaded = review_module.load_review_prompt()
    for dimension in review_module.REVIEW_DIMENSIONS:
        assert dimension in loaded, (
            f"schema dimension {dimension} missing from the prompt text"
        )
    # The same falsifier covers the checklist items: each one must be named in
    # the prompt AND named in the emission form, because a target the reviewer
    # is told to read but never told to report on is the skip this schema
    # exists to make visible.
    for item in review_module.REVIEW_ITEMS:
        assert item in loaded, f"checklist item {item} missing from the prompt"
        if item == "call_sites":
            # The sixth item reports on its own CALL_SITES line rather than a
            # VERDICT line; the dedicated emission is asserted below.
            continue
        assert f"VERDICT {item}:" in loaded, (
            f"checklist item {item} has no VERDICT emission in the prompt"
        )
    assert "every checklist item needs a verdict" in loaded.lower()
    assert "call_sites:" in loaded.lower()
    assert "call_sites: none" in loaded.lower()
    assert "not optional" in loaded.lower()
    assert "test files do not count as production call sites" in loaded.lower()
    probe = tmp_path / "prompt.md"
    monkeypatch.setattr(review_module, "_PROMPT_PATH", probe)
    probe.write_text("first load\n", encoding="utf-8")
    assert review_module.load_review_prompt() == "first load\n"
    probe.write_text("second load sees the edit\n", encoding="utf-8")
    assert review_module.load_review_prompt() == "second load sees the edit\n"


# ── The parser ──────────────────────────────────────────────────────────────


def test_full_emission_parses_to_every_dimension_with_an_arithmetic_total() -> None:
    record = review_module.parse_review(VALID_TEXT)
    assert record["status"] == "parsed"
    assert record["absent"] == []
    assert set(record["scores"]) == set(review_module.REVIEW_DIMENSIONS)
    assert record["total"] == 18 + 15 + 17 + 19 + 16
    assert record["total"] == sum(record["scores"].values())
    assert len(record["justifications"]) == 5
    assert "reckon/crew/review.py" in record["justifications"]["goal_fidelity"]
    assert record["findings"] == [
        {
            "file": "reckon/crew/query.py",
            "line": "120",
            "text": "an out-of-scope helper was added to a file the node was not fenced to write",
        }
    ]
    assert record["raw_text"] == VALID_TEXT


def test_every_schema_dimension_is_represented_as_score_or_absent() -> None:
    partial = "SCORE goal_fidelity: 18\nSCORE evidence: 15"
    record = review_module.parse_review(partial)
    assert set(record["scores"]) | set(record["absent"]) == set(
        review_module.REVIEW_DIMENSIONS
    )
    assert len(record["scores"]) + len(record["absent"]) == len(
        review_module.REVIEW_DIMENSIONS
    )


def test_missing_dimension_is_named_absent_not_a_partial_total() -> None:
    text = (
        "SCORE goal_fidelity: 18\n"  # fit omitted
        "SCORE evidence: 15\n"
        "SCORE scope_discipline: 17\n"
        "SCORE durability: 19"
    )
    record = review_module.parse_review(text)
    assert record["status"] == "parsed"
    assert record["absent"] == ["fit"]
    assert record["total"] is None, (
        "a total over four dimensions would read as a worse score"
    )


# ── The checklist item verdicts ─────────────────────────────────────────────


def test_full_emission_records_a_verdict_for_every_checklist_item() -> None:
    record = review_module.parse_review(VALID_TEXT)
    assert set(record["item_verdicts"]) == set(review_module.REVIEW_ITEMS)
    assert record["absent_items"] == []
    assert record["item_aggregate"] == len(review_module.REVIEW_ITEMS)
    assert "tests/test_review_scoring.py" in record["item_verdicts"]["manifest"]


def test_omitted_item_verdict_is_named_absent_with_the_aggregate_withheld() -> None:
    without_diff = "\n".join(
        line for line in VALID_TEXT.splitlines() if not line.startswith("VERDICT diff:")
    )
    record = review_module.parse_review(without_diff)
    assert record["status"] == "parsed"
    assert record["absent_items"] == ["diff"]
    assert "diff" not in record["item_verdicts"]
    assert record["item_aggregate"] is None, (
        "a count over the items present would read as a review that checked fewer"
    )
    assert record["total"] == 85, (
        "an omitted item verdict must not move the dimension scoring"
    )


def test_a_verdict_carrying_no_text_is_not_a_verdict() -> None:
    emptied = "\n".join(
        "VERDICT diff:" if line.startswith("VERDICT diff:") else line
        for line in VALID_TEXT.splitlines()
    )
    record = review_module.parse_review(emptied)
    assert "diff" in record["absent_items"]
    assert record["item_aggregate"] is None


def test_a_verdict_for_an_item_outside_the_schema_is_ignored() -> None:
    record = review_module.parse_review(
        "VERDICT not_an_item: this names nothing the schema declares\n" + VALID_TEXT
    )
    assert set(record["item_verdicts"]) == set(review_module.REVIEW_ITEMS)
    assert record["absent_items"] == []
    assert record["item_aggregate"] == len(review_module.REVIEW_ITEMS)


def test_item_verdicts_alone_do_not_make_a_review_parsed() -> None:
    verdicts_only = "\n".join(
        line
        for line in VALID_TEXT.splitlines()
        if line.startswith(("VERDICT ", "CALL_SITES"))
    )
    record = review_module.parse_review(verdicts_only)
    assert record["status"] == "unparsed"
    assert record["absent_items"] == []
    assert record["item_aggregate"] == len(review_module.REVIEW_ITEMS)
    assert record["total"] is None


def test_out_of_range_score_is_refused_naming_dimension_and_value() -> None:
    for dimension, value in (("evidence", 21), ("fit", -1)):
        with pytest.raises(review_module.ReviewScoreError) as excinfo:
            review_module.parse_review(f"SCORE {dimension}: {value}")
        message = str(excinfo.value)
        assert dimension in message
        assert str(value) in message
        assert str(review_module.REVIEW_MAX_SCORE) in message


def test_unparseable_text_yields_an_unparsed_record() -> None:
    gibberish = "this is not a review at all\nno SCORE lines here"
    record = review_module.parse_review(gibberish)
    assert record["status"] == "unparsed"
    assert record["scores"] == {}
    assert record["absent"] == list(review_module.REVIEW_DIMENSIONS)
    assert record["total"] is None
    assert record["raw_text"] == gibberish


def test_surrounding_prose_is_ignored() -> None:
    mixed = "Some preamble.\n\n" + VALID_TEXT + "\n\nSome closing note."
    record = review_module.parse_review(mixed)
    assert record["status"] == "parsed"
    assert record["total"] == 85


def test_boundary_scores_are_accepted() -> None:
    record = review_module.parse_review(
        "SCORE goal_fidelity: 0\nSCORE evidence: 20\nSCORE scope_discipline: 0\n"
        "SCORE durability: 20\nSCORE fit: 0"
    )
    assert record["scores"] == {
        "goal_fidelity": 0,
        "evidence": 20,
        "scope_discipline": 0,
        "durability": 20,
        "fit": 0,
    }
    assert record["total"] == 40


# ── The durable store ───────────────────────────────────────────────────────


def test_written_record_reads_back_equal_to_what_was_written(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    record = _metadata_record(
        **review_module.parse_review(VALID_TEXT),
        timestamp="2026-09-06T17:30:00+00:00",
    )
    path = review_module.store_review(record)
    assert path == review_module.review_path("reckon", "r-reviewed-run")
    assert path.is_file()
    assert review_module.read_review("reckon", "r-reviewed-run") == record
    assert review_module.read_review("reckon", "r-other-run") is None


def test_unparseable_review_is_stored_verbatim_with_an_unparsed_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    gibberish = "the reviewer returned prose, not a review"
    parsed = review_module.parse_review(gibberish)
    assert parsed["status"] == "unparsed"
    record = _metadata_record(
        **parsed,
        reviewed_run_id="r-gibberish",
        timestamp="2026-09-06T17:31:00+00:00",
    )
    review_module.store_review(record)
    stored = review_module.read_review("reckon", "r-gibberish")
    assert stored is not None
    assert stored["status"] == "unparsed"
    assert stored["raw_text"] == gibberish


def test_missing_timestamp_is_stamped_and_preserved_on_a_later_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    record = _metadata_record(**review_module.parse_review(VALID_TEXT))
    record.pop("timestamp")
    review_module.store_review(record)
    stored = review_module.read_review("reckon", "r-reviewed-run")
    assert stored is not None
    assert stored["timestamp"]
    assert stored["scores"] == record["scores"]
    assert stored["total"] == record["total"]


def test_store_is_keyed_by_project_and_reviewed_run_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    record = _metadata_record(**review_module.parse_review(VALID_TEXT))
    review_module.store_review(record)
    under_config = tmp_path / "config" / "crew" / "reviews"
    assert (under_config / "reckon" / "r-reviewed-run.json").is_file()
    assert not (
        under_config / "reckon" / "other-project" / "r-reviewed-run.json"
    ).is_file()
    assert review_module.read_review("other-project", "r-reviewed-run") is None


def test_store_resolves_under_the_pointed_config_and_the_real_one_gains_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real_home = _store._config_home()
    real_reviews = real_home / "crew" / "reviews"
    before = _config_file_set(real_reviews)
    pointed = tmp_path / "config-home"
    monkeypatch.setenv("RECKON_HOME", str(pointed))
    record = _metadata_record(**review_module.parse_review(VALID_TEXT))
    review_module.store_review(record)
    assert review_module.read_review("reckon", "r-reviewed-run") is not None
    assert (pointed / "crew" / "reviews" / "reckon" / "r-reviewed-run.json").is_file()
    assert _config_file_set(real_reviews) == before


def test_base_dir_override_moves_the_store_and_leaves_config_untouched(
    tmp_path: Path,
) -> None:
    real_home = _store._config_home()
    real_reviews = real_home / "crew" / "reviews"
    before = _config_file_set(real_reviews)
    elsewhere = tmp_path / "elsewhere"
    record = _metadata_record(**review_module.parse_review(VALID_TEXT))
    review_module.store_review(record, base_dir=elsewhere)
    assert (elsewhere / "reckon" / "r-reviewed-run.json").is_file()
    read_back = review_module.read_review(
        "reckon", "r-reviewed-run", base_dir=elsewhere
    )
    assert read_back is not None
    assert read_back["total"] == 85
    assert _config_file_set(real_reviews) == before


# ── The ledger-row reducer ──────────────────────────────────────────────────


def test_a_parsed_review_reduces_to_its_ledger_row_block() -> None:
    block = review_module.ledger_block(
        review_module.parse_review(VALID_TEXT)
    )
    assert block == {
        "status": "parsed",
        "scores": {
            "goal_fidelity": 18,
            "evidence": 15,
            "scope_discipline": 17,
            "durability": 19,
            "fit": 16,
        },
        "absent": [],
        "total": 85,
    }


def test_no_review_at_all_reduces_to_none() -> None:
    assert review_module.ledger_block(None) is None


def test_absent_unparsed_and_scored_zero_stay_three_distinct_blocks() -> None:
    absent = review_module.ledger_block(None)
    unparsed = review_module.ledger_block(
        {"status": "unparsed", "scores": {}, "total": None}
    )
    scored_zero = review_module.ledger_block(
        {
            "status": "parsed",
            "scores": {
                "goal_fidelity": 0,
                "evidence": 0,
                "scope_discipline": 0,
                "durability": 0,
                "fit": 0,
            },
            "absent": [],
            "total": 0,
        }
    )
    # Each pair must read apart, in both directions: an unreviewed run is not
    # one whose review scored zero, and neither is an unparsed review.
    assert absent is None
    assert unparsed is not None
    assert scored_zero is not None
    assert unparsed != scored_zero
    assert scored_zero["status"] == "parsed"
    assert unparsed["status"] == "unparsed"
    assert unparsed["total"] is None
    assert scored_zero["total"] == 0
