"""A promoted row names the revision its review read.

A stored review is evidence about one revision, and the row it lands on is read
later as evidence about the run. These cases hold both ends of that path to the
same fact: the composed review brief and the review role prompt ask the
reviewer to record the canonical base/head pair, and the compact block a
promotion stores onto the row carries that pair, resolved through the store's
own reader from whichever of the five spellings the record used.

The row case is built from a record written the way ``store_review`` writes one
and read through the selector the promotion gate uses, so the record the brief
asks for is the one the row is read against.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew import recovery
from reckon.crew import review as review_module

PROJECT = "sample-project"
REVIEWED_RUN = "r-reviewed-run"
REVIEW_RUN = "r-review-run"
BASE = "a" * 40
HEAD = "b" * 40
SCORES = {
    "goal_fidelity": 18,
    "evidence": 15,
    "scope_discipline": 17,
    "durability": 19,
    "fit": 16,
    "reuse": 20,
}
TOTAL = sum(SCORES.values())


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate the durable review store from the operator's crew state."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _source_record() -> dict:
    """A run awaiting review, shaped with the fields its pointer carries."""
    return {
        "run_id": REVIEWED_RUN,
        "project": PROJECT,
        "session": "sample-session",
        "node": {
            "id": "source-node",
            "plan": "delivery-plan",
            "section": "delivery",
        },
    }


def test_the_emitted_review_brief_asks_for_the_reviewed_revisions() -> None:
    fields = recovery._review_dispatch_fields(_source_record())

    assert "reviewed_base_sha" in fields["done_when"]
    assert "reviewed_head_sha" in fields["done_when"]


def test_the_review_prompt_asks_for_the_reviewed_revisions() -> None:
    prompt = review_module.load_review_prompt()

    assert "reviewed_base_sha" in prompt
    assert "reviewed_head_sha" in prompt


def test_a_promoted_row_names_the_reviewed_head(crew_home: Path) -> None:
    record = {
        "project": PROJECT,
        "reviewed_run_id": REVIEWED_RUN,
        "review_run_id": REVIEW_RUN,
        "status": "parsed",
        "scores": SCORES,
        "absent": [],
        "total": TOTAL,
        "reviewed_base_sha": BASE,
        "reviewed_head_sha": HEAD,
    }
    path = review_module.store_review(record)

    assert path.name == f"{REVIEWED_RUN}.at-{HEAD}.json"

    stored, stale = recovery.select_review_for_head(PROJECT, REVIEWED_RUN, HEAD)
    assert stored is not None
    assert stale == ""

    row = ledger.build_record(
        run_id="r-promoted",
        plan="delivery-plan",
        node="source-node",
        gate="passed",
        review=review_module.ledger_block(stored),
    )

    assert row["review"]["reviewed_head_sha"] == HEAD
    assert row["review"]["reviewed_base_sha"] == BASE


def test_a_legacy_spelling_reaches_the_row_under_the_canonical_name() -> None:
    block = review_module.ledger_block(
        {
            "status": "parsed",
            "scores": SCORES,
            "absent": [],
            "total": TOTAL,
            "reviewed_base": BASE,
            "reviewed_commit": HEAD,
        }
    )

    assert block["reviewed_base_sha"] == BASE
    assert block["reviewed_head_sha"] == HEAD


def test_a_record_naming_no_revision_keeps_the_bare_block() -> None:
    block = review_module.ledger_block(
        {"status": "parsed", "scores": SCORES, "absent": [], "total": TOTAL}
    )

    assert set(block) == {"status", "scores", "absent", "total"}
