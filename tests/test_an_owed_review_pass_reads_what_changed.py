"""An owed-review pass decodes only the reviews and plans that changed.

The pass runs on the station's tick, and at base each tick listed and read every
stored review and rescanned every resource directory, whether or not anything had
moved since the tick before. This test drives four passes over one project — a
cold pass, a pass with nothing changed, a pass after one stored review is
rewritten, and a pass after one plan file is edited — and counts what each pass
actually read.

The counters wrap the decoders rather than the memoised wrappers above them, and
every file's memo is cleared before each pass, so a count is a real read and not
a cache hit. That is what makes the negative control mean anything: at the base
revision the unchanged second pass reads all four hundred stored reviews.

The parity arm replays the same four passes with the change detection's memory
cleared before each pass, so a pass answering from what it read last time is held
to the answer a pass reading everything from scratch composes.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from reckon import _plan_html, file_memo, resources
from reckon.crew import plan_review, recovery

PROJECT = "example"
PLANS = 40
RECORDS_PER_PLAN = 10
STORED = PLANS * RECORDS_PER_PLAN
MOMENT = datetime(2026, 10, 20, 12, tzinfo=UTC).timestamp()
# When each plan file was last written: past the quiet period, inside the
# authoring burst, so every plan is one the pass has to weigh or file away.
EDITED = 7300.0  # seconds before MOMENT
# The age of the one plan edited during the scenario: still inside the burst and
# outside the quiet period, and not the age it carried before the edit, or the
# pass would rightly see no change at all.
EDITED_LATER = 5000.0


def _stamp(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat()


def _plan_document(slug: str) -> str:
    return (
        "<html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{slug}">'
        '<meta name="plan-status" content="active">'
        '<meta name="plan-version" content="3">'
        "</head><body>"
        '<h2 id="work">Work</h2><p>Deliver the behaviour.</p>'
        "</body></html>"
    )


def _build(root):
    """A project of forty plans, ten stored reviews each, nothing else moving."""
    home = root / "home"
    home.mkdir(parents=True)
    docs = root / "repo" / "docs"
    plans = docs / "plans"
    plans.mkdir(parents=True)
    (home / "mounts.json").write_text(json.dumps({PROJECT: str(docs)}))
    reviews = root / "reviews"
    staging = reviews / PROJECT
    staging.mkdir(parents=True)
    for number in range(PLANS):
        slug = f"topic-{number:02d}"
        path = plans / f"{slug}.html"
        path.write_text(_plan_document(slug))
        os.utime(path, (MOMENT - EDITED, MOMENT - EDITED))
        for version in range(RECORDS_PER_PLAN):
            record = {
                "project": PROJECT,
                "plan_slug": slug,
                "plan_version": version + 1,
                "rubric": "content",
                "findings": [],
                "review_run_id": f"review-{number:02d}-{version:02d}",
                "timestamp": _stamp(MOMENT - 3600 + version),
            }
            (staging / f"plan-{slug}.v{version + 1}.json").write_text(
                json.dumps(record)
            )
    return SimpleNamespace(home=home, docs=docs, plans=plans, reviews=reviews)


def _stand_in(monkeypatch):
    """Replace review dispatch with a recorder that starts nothing."""
    dispatched: list[dict] = []

    def subject(project, slug, session, *, rubric="design", local=False):
        return {
            "subject": "plan",
            "project": project,
            "plan_slug": slug,
            "session": session,
            "rubric": rubric,
            "run_id": f"composed-{slug}",
        }

    def dispatch(brief, *, config=None, launcher=None):
        dispatched.append(dict(brief))
        return {"dispatched": True}

    monkeypatch.setattr(recovery, "plan_review_subject", subject)
    monkeypatch.setattr(recovery, "dispatch_review_for_run", dispatch)
    return dispatched


def _instrument(monkeypatch):
    """Count real record decodes, plan-file reads and resource scans."""
    counts = {"records": 0, "plans": set(), "directories": 0}
    read = plan_review._read_record
    head = _plan_html._parse_meta_head_uncached
    meta = _plan_html._parse_meta_uncached
    scan = resources.iter_resources

    def decoded(path):
        counts["records"] += 1
        return read(path)

    def parsed_head(path, *arguments, **keywords):
        counts["plans"].add(str(path))
        return head(path, *arguments, **keywords)

    def parsed_meta(path, *arguments, **keywords):
        counts["plans"].add(str(path))
        return meta(path, *arguments, **keywords)

    def scanned(*arguments, **keywords):
        counts["directories"] += 1
        return scan(*arguments, **keywords)

    monkeypatch.setattr(plan_review, "_read_record", decoded)
    monkeypatch.setattr(_plan_html, "_parse_meta_head_uncached", parsed_head)
    monkeypatch.setattr(_plan_html, "_parse_meta_uncached", parsed_meta)
    monkeypatch.setattr(resources, "iter_resources", scanned)
    return counts


def _run(population, *, pointers=()):
    """One station tick, as the sweep runs it: a real pass, not a preview."""
    file_memo.clear()
    return plan_review.compose_owed_plan_reviews(
        PROJECT, base_dir=population.reviews, now_seconds=MOMENT, pointers=pointers
    )


def _authored_pointer(slug: str) -> dict:
    return {
        "run_id": "live-author",
        "node": {"plan": slug},
        "session": "live-session",
        "created_at": _stamp(MOMENT),
    }


def _scenario(root, monkeypatch, *, incremental):
    """Four passes over one project, with a changed review and a changed plan."""
    population = _build(root)
    monkeypatch.setenv("RECKON_HOME", str(population.home))
    monkeypatch.setenv("RECKON_RUN_STORE", str(population.home / "runs.db"))
    dispatched = _stand_in(monkeypatch)
    counts = _instrument(monkeypatch)
    file_memo.clear()
    plan_review._PLAN_LISTINGS.clear()
    plan_review._RECORD_SUMMARIES.clear()
    pointers = [_authored_pointer("topic-00")]
    reports = []
    seen = []

    def tick():
        if not incremental:
            # The same pass with no memory of the last one: it re-reads the tree
            # and the store rather than answering from what it decoded before.
            plan_review._PLAN_LISTINGS.clear()
            plan_review._RECORD_SUMMARIES.clear()
        counts["records"] = 0
        counts["plans"] = set()
        counts["directories"] = 0
        report = _run(population, pointers=pointers)
        reports.append(report)
        seen.append(
            {
                "records": counts["records"],
                "plans": len(counts["plans"]),
                "directories": counts["directories"],
            }
        )
        return report

    tick()  # cold
    tick()  # nothing changed
    stored = population.reviews / PROJECT / "plan-topic-05.v1.json"
    record = json.loads(stored.read_text())
    record["timestamp"] = _stamp(MOMENT)
    stored.write_text(json.dumps(record, indent=2))
    tick()  # one stored review rewritten
    edited = population.plans / "topic-11.html"
    edited.write_text(
        _plan_document("topic-11").replace("Deliver the behaviour.", "Deliver more.")
    )
    os.utime(edited, (MOMENT - EDITED_LATER, MOMENT - EDITED_LATER))
    tick()  # one plan file edited
    return SimpleNamespace(reports=reports, counts=seen, dispatched=dispatched)


@pytest.fixture(autouse=True)
def _forget_memos():
    file_memo.clear()
    plan_review._PLAN_LISTINGS.clear()
    plan_review._RECORD_SUMMARIES.clear()
    yield
    file_memo.clear()
    plan_review._PLAN_LISTINGS.clear()
    plan_review._RECORD_SUMMARIES.clear()


def test_an_owed_review_pass_reads_only_what_changed(tmp_path, monkeypatch):
    reading = _scenario(tmp_path / "incremental", monkeypatch, incremental=True)
    counts = reading.counts
    # Cold: the instrument has to see the whole store and the whole tree, or the
    # zeros below would be an instrument that cannot see anything. The pass also
    # re-reads the weighed plan's own ten records for its coverage and burst
    # checks, which is why the figure is a floor rather than the store's size.
    assert counts[0]["records"] >= STORED, counts[0]
    assert counts[0]["plans"] >= PLANS
    assert counts[0]["directories"] == 1
    assert reading.reports[0]["composed"], "the cold pass composed a review"
    # Nothing changed: no stored review, no plan file, no resource directory.
    assert counts[1] == {"records": 0, "plans": 0, "directories": 0}, counts[1]
    # One stored review rewritten: that record and no other.
    assert counts[2]["records"] == 1, counts[2]
    assert counts[2]["directories"] == 0, counts[2]
    assert counts[2]["plans"] == 0, counts[2]
    # One plan file edited: that plan and nothing else, and no stored review.
    assert counts[3]["records"] == 0, counts[3]
    assert counts[3]["plans"] == 1, counts[3]
    assert counts[3]["directories"] == 0, counts[3]

    fresh = _scenario(tmp_path / "reference", monkeypatch, incremental=False)
    for number, (answered, reference) in enumerate(
        zip(reading.reports, fresh.reports, strict=True), start=1
    ):
        assert answered == reference, f"pass {number} composes differently"
    assert reading.reports[3]["composed"] == [], "the edited plan names no author"
    assert fresh.dispatched and reading.dispatched == fresh.dispatched
