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
