"""A claims review record is judged by its own rubric, and passes the check.

A claims review is composed for a read-only run: it re-runs the claims the
report makes and records one verdict per claim. Nothing was landed and no gate
suite ran, so the six landed-node dimensions measure nothing and a claims
reviewer writes none of them. The delivery contract asked every parsed record
for those six scores, so both claims records stored for this session's census
and prototype reviews were refused for missing scores, and each had to be
repaired by hand before it could promote.

The record's rubric now decides the measure: a rubric that records a verdict per
item owes no text scores, so a claims record carrying a verdict for every claim
it re-ran passes, and one that skipped a claim is refused naming the claim. A
scored record without its six dimensions is refused as it was before, and the
figure rubric keeps the exemption it already had.

The two records copied at the end of this file are the stored ones verbatim, so
the check is measured against the shapes live reviewers wrote rather than
against a shape this file invented. Every record, pointer and review store
lives under the suite's temporary home.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon.cli import main
from reckon.crew import review

PROJECT = "claims-review-fixture"
REVIEWED_RUN = "r-read-only-fixture"
REVIEW_RUN = "r-claims-review-fixture"
BASE_SHA = "a" * 40


def _invoke(*args: str):
    return CliRunner().invoke(main, ["crew", *args])


def _check(record: dict, tmp_path: Path):
    """Run ``crew check --record`` on ``record`` as a reviewer would."""
    path = tmp_path / "delivered-review.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return _invoke("check", "--review", "--record", str(path))


def _claims_record(**changes: object) -> dict:
    """A claims record as its reviewer writes one: verdicts, and no scores."""
    body: dict = {
        "project": PROJECT,
        "reviewed_run_id": REVIEWED_RUN,
        "review_run_id": REVIEW_RUN,
        "reviewed_base_sha": BASE_SHA,
        "reviewed_head_sha": BASE_SHA,
        "rubric": review.CLAIMS_RUBRIC,
        "status": "parsed",
        "claims": [
            {
                "rank": str(rank),
                "verdict": "reproduced",
                "detail": f"the re-run showed the figure claim {rank} states",
            }
            for rank in range(1, review.CLAIMS_REQUIRED + 1)
        ],
        "claim_summary": {
            "verdict": "agrees",
            "detail": "the summary's figures are the table's beneath it",
        },
        "added_failure_count": 0,
        "added_failure_ids": [],
        "findings": [],
    }
    body.update(changes)
    return body


def _findings(result) -> list[str]:
    return json.loads(result.output)["findings"]


# ── A claims record passes without the text scores ──────────────────────────


def test_a_complete_claims_record_passes_the_review_check(tmp_path: Path) -> None:
    """The record refused at the base revision: every claim, and no scores."""
    result = _check(_claims_record(), tmp_path)
    assert result.exit_code == 0, result.output
    assert _findings(result) == []


def test_every_claims_record_stored_for_this_session_passes(tmp_path: Path) -> None:
    """The census and prototype records, copied whole, pass as they were stored."""
    for stored in (_CENSUS_RECORD, _PROTOTYPE_RECORD):
        result = _check(stored, tmp_path)
        assert result.exit_code == 0, result.output
        assert _findings(result) == []


def test_a_claims_record_missing_a_claim_is_refused(tmp_path: Path) -> None:
    """A skipped re-run is refused, and the promotion rule refuses it too."""
    record = _claims_record()
    record["claims"] = [claim for claim in record["claims"] if claim["rank"] != "2"]
    result = _check(record, tmp_path)
    assert result.exit_code == 1, result.output
    assert any("too few claim verdicts" in f for f in _findings(result)), _findings(
        result
    )
    assert not any("report's summary" in f for f in _findings(result))
    assert not _review_is_complete(record)


def _review_is_complete(record: dict) -> bool:
    """The promotion reader's verdict on a stored record, imported at call time."""
    from reckon.crew.recovery_review_subject import (
        _review_is_complete as owner,
    )

    return owner(record)


@pytest.mark.parametrize(
    "change",
    [
        {},
        {"claims": []},
        {"claims": [{"rank": "1", "verdict": "reproduced"}]},
        {"claim_summary": {"detail": "read but not compared"}},
        {"claim_summary": {"verdict": "unknown"}},
    ],
    ids=["complete", "no-claims", "one-claim", "summary-unchecked", "summary-bad"],
)
def test_the_check_refuses_exactly_what_the_promotion_rule_refuses(
    change: dict, tmp_path: Path
) -> None:
    """One rule decides both: a record cannot pass the check and fail promotion."""
    record = _claims_record(**change)
    result = _check(record, tmp_path)
    assert (result.exit_code == 1) is (not _review_is_complete(record)), (
        result.output,
        _review_is_complete(record),
    )


def test_the_hand_written_spelling_is_refused_by_both_readers(tmp_path: Path) -> None:
    """Verdicts under a key the store does not read are unanswered to both."""
    record = _claims_record()
    record["claim_verdicts"] = record.pop("claims")
    result = _check(record, tmp_path)
    assert result.exit_code == 1, result.output
    assert not _review_is_complete(record)


def test_a_claims_record_missing_its_summary_verdict_is_refused(tmp_path: Path) -> None:
    """The report's own head line is a check, so an unchecked one is named."""
    record = _claims_record()
    record["claim_summary"] = {"detail": "read but not compared"}
    result = _check(record, tmp_path)
    assert result.exit_code == 1, result.output
    assert any(
        "no verdict for the report's summary" in finding
        for finding in _findings(result)
    )


# ── The scored contract is unchanged ────────────────────────────────────────


def test_a_scored_record_without_scores_is_still_refused(tmp_path: Path) -> None:
    """The refusal the base revision gives a landed record is not moved."""
    record = _claims_record()
    record.pop("rubric")
    record.pop("claims")
    record.pop("claim_summary")
    result = _check(record, tmp_path)
    assert result.exit_code == 1, result.output
    assert "missing scores" in _findings(result)


def test_a_partly_scored_record_names_each_missing_dimension(tmp_path: Path) -> None:
    record = _claims_record()
    record.pop("rubric")
    record.pop("claims")
    record.pop("claim_summary")
    record["scores"] = dict.fromkeys(review.REVIEW_DIMENSIONS[:-1], 10)
    result = _check(record, tmp_path)
    assert result.exit_code == 1, result.output
    assert _findings(result) == [f"missing scores.{review.REVIEW_DIMENSIONS[-1]}"]


# ── Every verdict-per-item rubric carries the exemption ─────────────────────


def test_the_exempt_set_is_every_verdict_per_item_rubric() -> None:
    """The five rubrics that record verdicts, and no scored rubric among them."""
    assert (
        frozenset(
            {
                review.CLAIMS_RUBRIC,
                review.FIGURE_REVIEW_RUBRIC,
                review.HEALTH_REVIEW_RUBRIC,
                *review.PLAN_REVIEW_RUBRICS,
            }
        )
        == review.ITEM_VERDICT_RUBRICS
    )


@pytest.mark.parametrize(
    "rubric",
    ["claims", "figure", "health", "plan_review", "plan_design_review"],
)
def test_each_verdict_per_item_rubric_owes_no_text_scores(
    rubric: str, tmp_path: Path
) -> None:
    """A record under any of them passes with no ``scores`` mapping at all."""
    record = _claims_record(rubric=rubric)
    result = _check(record, tmp_path)
    assert result.exit_code == 0, result.output
    assert _findings(result) == []


# ── The two claims records stored for this session's reviews, verbatim ────────
# Copied whole, so the check is measured against what live reviewers wrote.


_CENSUS_RECORD: dict = {
    "absent": [
        "goal_fidelity",
        "evidence",
        "scope_discipline",
        "durability",
        "fit",
        "reuse",
    ],
    "added_failure_count": 0,
    "added_failure_ids": [],
    "claim_summary": {
        "detail": "The summary bullets match measurements.json on every "
        "worker and coordinator figure the table carries, but two "
        "defects stand: the share-of-run figure caption says 'One "
        "run reaches 0.63 and every other run lies below 0.30' "
        "while the table it cites "
        "(deliverables/per-run-share.json) carries a second run "
        "at 0.3568, so 2 of 154 runs sit at or above 0.30; and "
        "three counts the summary quotes (24 "
        "failure_classification, 11 lineage, 18 outcome records) "
        "are carried nowhere in measurements.json, though "
        "recounting the run records reproduces 24, 11 and 18 "
        "exactly.",
        "verdict": "differs",
    },
    "claims": [
        {
            "detail": "share of the run: re-ran summarise.py from the run directory, "
            "both over the reviewed run's per-run.json and over "
            "per-run.json freshly re-derived by census.py on rigel (job "
            "1316311). Share median 0.07144 (7.1%), p90 0.18287 (18.3%), "
            "population total 0.07872 (7.9%), from 906,969 evidence-write "
            "characters of 11,522,034 tool-output characters; token share "
            "median 0.06065 (6.1%) on the same 4 runs. Identical results "
            "both ways, and identical to the report.",
            "rank": "1",
            "verdict": "reproduced",
        },
        {
            "detail": "turns touching evidence: 790 of 14,781 turns (5.3%, population "
            "total 0.05345), per-run median 0.05933 (5.9%), p90 0.27273 "
            "(27.3%), 149 of 154 runs writing a fragment, 188 file writes "
            "over 186 distinct files. Identical to the report.",
            "rank": "2",
            "verdict": "reproduced",
        },
        {
            "detail": "later reads: the report states 729 fragment read calls over "
            "489 distinct files across both populations, 530 archive calls "
            "over 72 files, 39 research calls over 8 files; the re-run "
            "shows 733 over 493, 538 over 72, and 41 over 8. Worker-only "
            "reads reproduce exactly (246 fragment calls over 187 files, 2 "
            "archive distinct files, 2 research distinct files). The drift "
            "is in the coordinator half: the 16 transcripts under "
            "~/.claude/projects/-home-ITER-mcintos-Code-reckon are live "
            "files that grew between the census (23:42) and the re-run "
            "(~01:25), coordinator turns 48,414 against 47,947 and turns "
            "touching evidence 1,208 against 1,190. Every gain is a read; "
            "no write was added.",
            "rank": "3",
            "verdict": "differs",
        },
    ],
    "claims_rubric_note": "A read-only run scores no landed-node dimension, so scores is "
    "empty and the six dimensions are named under absent; the "
    "claims and claim_summary keys carry the verdicts.",
    "completed_at": "2026-11-15T01:09:51.476Z",
    "dispatched_at": "2026-11-15T00:11:56Z",
    "finding_dispositions": {
        "0": {
            "kind": "exempted",
            "reason": "the caption error is in the worker manifest "
            "only; the research document redraws the "
            "figure from per-run-share.json and makes no "
            "upper-tail claim",
            "recorded_at": "2026-11-15T01:16:20.608572+00:00",
        },
        "1": {
            "kind": "exempted",
            "reason": "the three counts recount correctly from the "
            "committed run records and the research "
            "document does not quote them",
            "recorded_at": "2026-11-15T01:16:28.315080+00:00",
        },
    },
    "findings": [
        {
            "file": "/home/ITER/mcintos/.config/reckon/crew/runs/r-20261114T205946996208-evidence-writing-cost-census/manifest.md",
            "line": "58",
            "severity": "non-blocking",
            "text": "the share-of-run caption's upper-tail claim is false against "
            "its own table: 2 of 154 runs are at or above 0.30 (0.6283 and "
            "0.3568), not one run at 0.63 with every other run below 0.30.",
        },
        {
            "file": "/home/ITER/mcintos/.config/reckon/crew/runs/r-20261114T205946996208-evidence-writing-cost-census/manifest.md",
            "line": "50",
            "severity": "non-blocking",
            "text": "three counts quoted in the rounds finding (24 "
            "failure_classification, 11 lineage, 18 outcome records) are "
            "absent from deliverables/measurements.json, whose rounds block "
            "carries only cause_counts, the retry total, the denominator "
            "and five examples; a reader of the deliverable cannot check "
            "them, though they recount correctly from "
            "docs/state/reckon/runs.",
        },
    ],
    "project": "reckon",
    "raw_text": "CLAIM 1: reproduced: re-ran summarise.py over the reviewed run's "
    "per-run.json and over per-run.json freshly re-derived by census.py on "
    "rigel; share of the run median 0.0714 (7.1%), p90 0.1829 (18.3%), "
    "population total 0.0787 (7.9%), 906,969 of 11,522,034 tool-output "
    "characters, identical to the report.\n"
    "CLAIM 2: reproduced: turns touching evidence 790 of 14,781 (5.3%), "
    "median 5.9%, p90 27.3%, 149 of 154 runs writing a fragment, 188 writes "
    "over 186 distinct files, identical to the report.\n"
    "CLAIM 3: differs: cross-population fragment reads 733 over 493 distinct "
    "files against the report's 729 over 489, archive 538/72 against 530/72, "
    "research 41/8 against 39/8; worker-only reads reproduce exactly (246 "
    "calls over 187 files); the coordinator transcripts are live files that "
    "grew between the census and the re-run, coordinator turns 48,414 against "
    "47,947.\n"
    "CLAIM_SUMMARY: differs: the summary's figures match measurements.json "
    "where the table carries them, but the share-of-run caption's 'every "
    "other run lies below 0.30' is contradicted by per-run-share.json, whose "
    "second-highest run is 0.3568, and three counts (24/11/18) the summary "
    "quotes appear nowhere in measurements.json.\n",
    "review_run_id": "r-20261114T231208943976-review-of-evidence-writing-cost-census-1ba716cf0aeb",
    "reviewed_base_sha": "1ba716cf0aeb500f4cf9bc0192d6927e11f9d6b2",
    "reviewed_head_sha": "1ba716cf0aeb500f4cf9bc0192d6927e11f9d6b2",
    "reviewed_run_id": "r-20261114T205946996208-evidence-writing-cost-census",
    "rubric": "claims",
    "scores": {},
    "status": "parsed",
    "times_source": "run-record",
    "timestamp": "2026-11-15T01:15:04.914246+00:00",
    "total": None,
}


_PROTOTYPE_RECORD: dict = {
    "added_failure_count": 0,
    "added_failure_ids": [],
    "claim_summary": {
        "detail": "the summary line's figures are the table's: 296,595 and "
        "116,089 bytes (39 percent) are the byte totals of the "
        "three size rows, 13 of 60 is the sum of the per-plan "
        "lost counts (9 + 2 + 2), the kinds 4 / 4 / 5 are the "
        "per-plan by_kind sums, and the per-plan pairs 254,691 -> "
        "49,946, 15,799 -> 12,346 and 26,105 -> 53,797 are the "
        "three size rows verbatim; every byte figure was "
        "confirmed against os.path.getsize on disk. The only "
        "tension is the 13 itself, which the re-run put at 12, "
        "and that is a claim finding rather than a mismatch "
        "between the summary and its table.",
        "verdict": "agrees",
    },
    "claims": [
        {
            "detail": "size pair re-measured against the reviewed worktree at "
            "70272d91 (git rev-parse HEAD matches, status clean) and the "
            "prototypes under the run directory: overviews 12,346 + 53,797 "
            "+ 49,946 = 116,089 bytes against records 15,799 + 26,105 + "
            "254,691 = 296,595 bytes, 39.1 percent. Per-plan pairs match "
            "the report exactly: 254,691 -> 49,946, 15,799 -> 12,346, "
            "26,105 -> 53,797 (the third overview is larger than its "
            "record). Caveat: the cited command `measure_comparison.py` "
            "does not run as delivered because closure_overview.RUNS_CACHE "
            "resolves to "
            "/tmp/reckon-crew-scratch/r-20261114T211102171695-closure-overview-prototype/work/runs_compact.json, "
            "which no longer exists; it aborts FileNotFoundError before any "
            "measure. The sizes do not read that cache, and the same "
            "plan_measure functions with the cache rebuilt from the 8,798 "
            "committed run records returned the figures above.",
            "rank": "1",
            "verdict": "reproduced",
        },
        {
            "detail": "the report states 13 of 60 sampled claims are recoverable from "
            "no allowed source (4 routine counts, 4 measurement narratives, "
            "5 negative results); the re-run produced 12 of 60 (4 routine "
            "counts, 3 measurement narratives, 5 negative results). The "
            "single differing claim is the a-review-is-sized-to-its-risk "
            "arm table ('base ... 7595 passed ... head ... 7690 passed'), "
            "whose tokens 7595 and 7690 are absent from the plan, comments, "
            "reviews and figures and match only inside commit SHAs in the "
            "run records' manifest_commits field, so the original 13 rests "
            "on the subset of run-record fields its discarded cache "
            "carried.",
            "rank": "2",
            "verdict": "differs",
        },
        {
            "detail": "the unsourced-claim counter recomputes to 0 unsourced blocks "
            "and 0 unsourced sentences on all three overviews, over 19, 33 "
            "and 67 factual sentences respectively, and the routine tables "
            "carry a source in every row (8/8, 114/114, 61/61). The cited "
            "falsifier that made this zero credible "
            "(/tmp/reckon-crew-scratch/r-20261114T211102171695-closure-overview-prototype/work/falsify_unsourced.py) "
            "went with the /tmp scratch, so its 0 -> 1 demonstration cannot "
            "be re-run; the record of it in negative-control.log was read "
            "and matches the page sha256.",
            "rank": "3",
            "verdict": "reproduced",
        },
    ],
    "completed_at": "2026-11-15T02:49:06.790Z",
    "dispatched_at": "2026-11-15T02:09:59Z",
    "finding_dispositions": {
        "0": {
            "kind": "exempted",
            "reason": "the prototype is a throwaway measurement; "
            "its sizes and sourcing counts were "
            "reproduced by the review, and the product "
            "version is specified by plan "
            "a-landing-is-recorded-once section 3, whose "
            "test builds its own ledger",
            "recorded_at": "2026-11-15T02:50:57.069236+00:00",
        },
        "1": {
            "kind": "exempted",
            "reason": "the worker's count of 13 lost stands; the "
            "12 arose in the re-run from a substring "
            "match inside commit hashes, and the "
            "research document reports 13",
            "recorded_at": "2026-11-15T02:51:09.809874+00:00",
        },
        "2": {
            "kind": "exempted",
            "reason": "a confirmation, not a defect",
            "recorded_at": "2026-11-15T02:51:23.656805+00:00",
        },
    },
    "findings": [
        {
            "file": "measure_comparison.py",
            "line": "58",
            "severity": "blocking",
            "text": "the measurement is not reproducible as delivered: "
            "closure_overview.py:28-31 points RUNS_CACHE at "
            "/tmp/reckon-crew-scratch/r-20261114T211102171695-closure-overview-prototype/work/runs_compact.json, "
            "a node-local file no script in the run directory produces, so "
            "the cited re-run aborts FileNotFoundError and a later reader "
            "cannot recover the corpus the loss figure was measured "
            "against. The cache should be rebuilt from "
            "docs/state/reckon/runs/ by a recorded command, or written into "
            "the run directory.",
        },
        {
            "file": "deliverables/comparison.json",
            "line": "158",
            "severity": "follow-on",
            "text": "the loss count is sensitive to which fields the missing cache "
            "carried: with the full committed run records it re-measures 12 "
            "of 60 rather than 13, because the tokens 7595 and 7690 of one "
            "arm table match substrings of manifest_commits SHAs. A token "
            "match is a substring test, so incidental hex can mark a claim "
            "recoverable that is not; the repository-wide corpus should "
            "exclude identifier-shaped hex or require a token boundary "
            "before a claim counts as recovered.",
        },
        {
            "file": "deliverables/comparison.json",
            "line": "1",
            "severity": "follow-on",
            "text": "verified and consistent: the three size pairs reproduce "
            "byte-exact from the reviewed tree, the routine tables carry a "
            "source in all 183 rows, the unsourced counter recomputes to "
            "zero, and the three overviews' figure references resolve "
            "inside the run directory. Only the falsifier script and the "
            "runs cache lived in /tmp and are now gone.",
        },
    ],
    "project": "reckon",
    "raw_text": "CLAIM 1: reproduced: ...\n"
    "CLAIM 2: differs: ...\n"
    "CLAIM 3: reproduced: ...\n"
    "CLAIM_SUMMARY: agrees: ...",
    "review_run_id": "r-20261115T020506097715-review-of-closure-overview-prototype-70272d915026",
    "reviewed_base_sha": "70272d915026239598b7ce5b77128c50c54b58cb",
    "reviewed_head_sha": "70272d915026239598b7ce5b77128c50c54b58cb",
    "reviewed_run_id": "r-20261114T211102171695-closure-overview-prototype",
    "rubric": "claims",
    "scores": {
        "durability": None,
        "evidence": None,
        "fit": None,
        "goal_fidelity": None,
        "reuse": None,
        "scope_discipline": None,
    },
    "scores_note": "the six landed-node dimensions are not what a claims review measures: "
    "nothing was landed and no gate suite ran, so each is null rather than "
    "scored; the verdicts above are the review.",
    "status": "parsed",
    "times_source": "run-record",
    "timestamp": "2026-11-15T02:49:47.358430+00:00",
}
