"""A stored review records the revision it read, under one key
(``reviewed_revision``).

The store spells that one fact five ways and no reader knows any of them, so a
promotion compared a review against the code being promoted while the review
actually described a revision a repair had already replaced. These are the
falsifiers for the normalisation: every spelling resolves, none of them is
mistaken for a value when the record merely carries the key empty, and a record
carrying none of them leaves the canonical key **absent** — a recorded absence
and an unread field are different claims and must stay distinguishable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon.crew import review as review_module

REVISION_KEY = "reviewed_revision"

SHA = "9d208d1e1a5181ceabbcab6eebd2d029ec08e7fa"
OTHER = "aa2675650000000000000000000000000000aaaa"

# Every spelling the store already uses for this one fact.
SPELLINGS = (
    "reviewed_commit",
    "reviewed_base",
    "reviewed_head_sha",
    "reviewed_base_sha",
    "commits_read",
)


def _record(**overrides: object) -> dict:
    return {
        "project": "reckon",
        "reviewed_run_id": "r-reviewed-run",
        "review_run_id": "r-review-run",
        "timestamp": "2026-09-22T10:00:00+00:00",
        **overrides,
    }


# ── Every spelling resolves ────────────────────────────────────────────────


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_each_store_spelling_resolves_to_the_canonical_key(spelling: str) -> None:
    parsed = review_module.parse_review("", record={spelling: SHA})
    assert parsed[REVISION_KEY] == SHA


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_each_store_spelling_resolves_through_the_store(
    spelling: str, tmp_path: Path
) -> None:
    stored = review_module.store_review(_record(**{spelling: SHA}), base_dir=tmp_path)
    written = review_module.read_review("reckon", "r-reviewed-run", base_dir=tmp_path)
    assert written is not None
    assert written[REVISION_KEY] == SHA
    # The legacy spelling survives beside the canonical key; the store is not
    # rewritten, it is annotated from it.
    assert written[spelling] == SHA
    assert Path(stored) == review_module.review_path(
        "reckon", "r-reviewed-run", tmp_path
    )


def test_a_commit_list_resolves_to_the_head_it_read() -> None:
    written = review_module.parse_review("", record={"commits_read": [OTHER, SHA]})
    assert written[REVISION_KEY] == SHA


def test_a_head_spelling_outranks_a_base_spelling() -> None:
    written = review_module.parse_review(
        "", record={"reviewed_base_sha": OTHER, "reviewed_head_sha": SHA}
    )
    assert written[REVISION_KEY] == SHA


# ── The absent case, and why presence is not truthiness ────────────────────


def test_a_record_carrying_no_spelling_leaves_the_key_absent() -> None:
    parsed = review_module.parse_review("", record=_record())
    assert REVISION_KEY not in parsed


def test_no_record_leaves_the_key_absent() -> None:
    parsed = review_module.parse_review("SCORE fit: 12")
    assert REVISION_KEY not in parsed


def test_the_store_invents_no_key_when_no_spelling_is_carried(
    tmp_path: Path,
) -> None:
    review_module.store_review(_record(), base_dir=tmp_path)
    written = review_module.read_review("reckon", "r-reviewed-run", base_dir=tmp_path)
    assert written is not None
    assert REVISION_KEY not in written


@pytest.mark.parametrize("empty", [None, "", [], ["", "  "]])
def test_a_spelling_carried_empty_is_a_recorded_absence(empty: object) -> None:
    parsed = review_module.parse_review("", record={"reviewed_commit": empty})
    assert REVISION_KEY in parsed
    assert parsed[REVISION_KEY] is None


def test_presence_is_the_authority_and_does_not_fall_through() -> None:
    """A carried-but-empty spelling is a recorded absence, not a reason to
    read a lower-precedence spelling: collapsing the two is the defect."""
    parsed = review_module.parse_review(
        "", record={"reviewed_commit": None, "reviewed_base": SHA}
    )
    assert REVISION_KEY in parsed
    assert parsed[REVISION_KEY] is None


def test_the_store_writes_a_recorded_absence_as_a_present_key(tmp_path: Path) -> None:
    review_module.store_review(_record(reviewed_commit=None), base_dir=tmp_path)
    written = review_module.read_review("reckon", "r-reviewed-run", base_dir=tmp_path)
    assert written is not None
    assert REVISION_KEY in written
    assert written[REVISION_KEY] is None


def test_an_existing_canonical_key_is_left_as_stated(tmp_path: Path) -> None:
    review_module.store_review(
        _record(**{REVISION_KEY: SHA, "reviewed_base": OTHER}), base_dir=tmp_path
    )
    written = review_module.read_review("reckon", "r-reviewed-run", base_dir=tmp_path)
    assert written is not None
    assert written[REVISION_KEY] == SHA


# ── The emitted slot ───────────────────────────────────────────────────────


def test_an_emitted_revision_line_is_recorded() -> None:
    parsed = review_module.parse_review(f"SCORE fit: 12\nREVISION: {SHA}")
    assert parsed[REVISION_KEY] == SHA


@pytest.mark.parametrize("label", [*SPELLINGS, REVISION_KEY])
def test_an_emitted_line_under_a_legacy_label_is_recorded(label: str) -> None:
    parsed = review_module.parse_review(f"SCORE fit: 12\n{label}: {SHA}")
    assert parsed[REVISION_KEY] == SHA


def test_an_emitted_revision_line_outranks_the_supplied_record() -> None:
    parsed = review_module.parse_review(
        f"REVISION: {SHA}", record={"reviewed_head_sha": OTHER}
    )
    assert parsed[REVISION_KEY] == SHA


def test_an_emitted_label_with_no_value_is_a_recorded_absence() -> None:
    parsed = review_module.parse_review("SCORE fit: 12\nREVISION:")
    assert REVISION_KEY in parsed
    assert parsed[REVISION_KEY] is None


def test_a_complete_review_without_a_revision_line_omits_the_key() -> None:
    parsed = review_module.parse_review("VERDICT goal: read; found.\nSCORE fit: 12")
    assert REVISION_KEY not in parsed
