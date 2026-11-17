"""An owed-review pass decodes changed inputs and preserves composition."""

from __future__ import annotations

import json
import os
from collections import Counter
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from reckon import _plan_html, file_memo, ledger, run_store
from reckon.crew import plan_review

PROJECT = "example"
MOMENT = datetime(2026, 10, 20, 12, tzinfo=UTC).timestamp()


def _stamp(seconds):
    return datetime.fromtimestamp(seconds, UTC).isoformat()


def _insert(store, row):
    identity, durable, detail = run_store._split_durable(PROJECT, row)
    store._conn.execute(
        run_store._RUNS_INSERT,
        (
            identity,
            *run_store._query_values(durable, detail),
            None,
            json.dumps(durable),
        ),
    )
    store._conn.execute(
        "INSERT INTO run_details VALUES (?, ?)", (identity, json.dumps(detail))
    )


@pytest.fixture
def population(tmp_path, monkeypatch):
    docs = tmp_path / "repo" / "docs"
    plans = docs / "plans"
    plans.mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_RUN_STORE", str(home / "runs.db"))
    (home / "mounts.json").write_text(json.dumps({PROJECT: str(docs)}))
    reviews = tmp_path / "reviews"
    (reviews / PROJECT).mkdir(parents=True)
    for number in range(50):
        slug = f"topic-{number:02d}"
        path = plans / f"{slug}.html"
        path.write_text(
            '<html><head><meta name="docs-project" content="example">'
            '<meta name="reckon-type" content="plan">'
            f'<meta name="plan-slug" content="{slug}">'
            '<meta name="plan-status" content="active">'
            '<meta name="plan-version" content="3"></head>'
            '<body><h2 id="work">Work</h2><p>Deliver the behaviour.</p></body></html>'
        )
        os.utime(path, (MOMENT - 1000, MOMENT - 1000))
        record = {
            "project": PROJECT,
            "plan_slug": slug,
            "plan_version": 1,
            "rubric": "content",
            "findings": [],
            "review_run_id": f"review-{number:02d}",
        }
        (reviews / PROJECT / f"plan-{slug}.v1.json").write_text(json.dumps(record))
    records = [
        {
            "run_id": f"stored-{number:05d}",
            "project": PROJECT,
            "plan": f"topic-{number % 50:02d}",
            "session": f"author-{number % 50:02d}",
            "completed_at": _stamp(MOMENT - 3600 + number / 100),
        }
        for number in range(20_000)
    ]
    database = run_store.store_path()
    with run_store.RunStore(database) as store, store._conn:
        for row in records:
            _insert(store, row)
        assert store._conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 20_000
    # The composition policy is exercised; only expensive coverage judgement is
    # isolated so the counters measure this pass's input reads, not a reviewer.
    monkeypatch.setattr(
        plan_review, "review_coverage", lambda *a, **k: ([], {"work"}, {})
    )
    monkeypatch.setattr(plan_review, "compose_burst_refusal", lambda *a, **k: None)
    monkeypatch.setattr(plan_review, "PLAN_REVIEW_PREVIEW_SECONDS", 300)
    file_memo.clear()
    yield SimpleNamespace(
        plans=plans, reviews=reviews, database=database, records=records
    )
    file_memo.clear()


def _pass(population, now=MOMENT, pointers=()):
    return plan_review.compose_owed_plan_reviews(
        PROJECT,
        base_dir=population.reviews,
        dry_run=True,
        now_seconds=now,
        pointers=pointers,
    )


def _instrument(monkeypatch):
    counts = Counter()
    loads = json.loads
    parse_head = _plan_html._parse_meta_head_uncached
    parse_meta = _plan_html._parse_meta_uncached

    def decode(value, *args, **kwargs):
        result = loads(value, *args, **kwargs)
        if isinstance(result, dict):
            identity = str(result.get("run_id") or result.get("review_run_id") or "")
            if identity.startswith("stored-"):
                counts["runs"] += 1
            elif identity.startswith("review-"):
                counts["reviews"] += 1
        return result

    def head(path, *args, **kwargs):
        counts["plans"] += 1
        return parse_head(path, *args, **kwargs)

    def meta(path, *args, **kwargs):
        counts["plans"] += 1
        return parse_meta(path, *args, **kwargs)

    monkeypatch.setattr(json, "loads", decode)
    monkeypatch.setattr(_plan_html, "_parse_meta_head_uncached", head)
    monkeypatch.setattr(_plan_html, "_parse_meta_uncached", meta)
    return counts


def _reference_authors(now, pointers=()):
    rows, _ = ledger.read_records(
        PROJECT,
        with_figures=False,
        activity_since=now - plan_review.PLAN_REVIEW_BURST_SECONDS,
    )
    newest = {}
    for row in [*pointers, *rows]:
        plan = plan_review._run_plan(row)
        at = plan_review._run_moment(row)
        if not plan or at is None or now - at > plan_review.PLAN_REVIEW_BURST_SECONDS:
            continue
        if not row.get("session") or not row.get("run_id"):
            continue
        if plan not in newest or at > newest[plan][0]:
            newest[plan] = (at, row["run_id"], row["session"])
    return {
        plan: {"run_id": run, "session": session}
        for plan, (_, run, session) in newest.items()
    }


def test_an_unchanged_pass_parses_nothing(population, monkeypatch):
    counts = _instrument(monkeypatch)
    first = _pass(population)
    assert not first["failed"]
    assert first["composed"] == [{"plan": "topic-00", "dispatched": False}]
    assert counts["reviews"] == 50 and counts["runs"] == 20_000
    assert counts["plans"] >= 50, "the instrument must see the cold plan reads"
    counts.clear()
    second = _pass(population)
    print(f"unchanged_parse_counts={dict(counts)}")
    assert second == first
    assert counts == {}, "an unchanged pass must parse no review, plan or stored run"
    _assert_composition_matches_an_uncached_pass(population, monkeypatch)
    review_path = population.reviews / PROJECT / "plan-topic-00.v1.json"
    record = json.loads(review_path.read_text())
    record["timestamp"] = _stamp(MOMENT)
    review_path.write_text(json.dumps(record))
    counts.clear()
    assert _pass(population) == first
    assert counts == {"reviews": 1}


def test_a_changed_pass_parses_only_changed_inputs(population, monkeypatch):
    counts = _instrument(monkeypatch)
    first = _pass(population)
    path = population.plans / "topic-00.html"
    path.write_text(
        path.read_text().replace("Deliver the behaviour.", "Deliver changed behaviour.")
    )
    os.utime(path, (MOMENT - 900, MOMENT - 900))
    row = dict(population.records[-1], session="changed-author")
    with run_store.RunStore(population.database) as store:
        store.sync_run(PROJECT, row)
    counts.clear()
    changed = _pass(population)
    print(f"changed_parse_counts={dict(counts)}")
    assert changed == first
    assert counts["runs"] == 1
    assert counts["reviews"] == 0
    assert 1 <= counts["plans"] <= 2
    _assert_authoring_reuse_tracks_time_live_runs_rewrites_and_deletions(
        population, monkeypatch
    )


def _assert_composition_matches_an_uncached_pass(population, monkeypatch):
    first = _pass(population)
    counts = _instrument(monkeypatch)
    warm = _pass(population)
    assert counts == {}
    file_memo.clear()
    assert _pass(population) == warm == first
    with monkeypatch.context() as reference:
        reference.setattr(plan_review, "_stored_authoring_runs", lambda *a: None)
        assert _pass(population) == first
    assert plan_review.plan_authoring_runs(
        PROJECT, now_seconds=MOMENT, pointers=[]
    ) == _reference_authors(MOMENT)


def _assert_authoring_reuse_tracks_time_live_runs_rewrites_and_deletions(
    population, monkeypatch
):
    for now in (MOMENT, MOMENT + 90_000, MOMENT - 5000, MOMENT):
        assert plan_review.plan_authoring_runs(
            PROJECT, now_seconds=now, pointers=[]
        ) == _reference_authors(now)
    pointer = {
        "run_id": "live-author",
        "node": {"plan": "topic-00"},
        "session": "live-session",
        "created_at": _stamp(MOMENT),
    }
    assert plan_review.plan_authoring_runs(
        PROJECT, now_seconds=MOMENT, pointers=[pointer]
    ) == _reference_authors(MOMENT, [pointer])
    row = dict(population.records[-1], plan="renamed", session="rewritten")
    with run_store.RunStore(population.database) as store:
        store.sync_run(PROJECT, row)
    assert plan_review.plan_authoring_runs(
        PROJECT, now_seconds=MOMENT, pointers=[]
    ) == _reference_authors(MOMENT)
    with run_store.RunStore(population.database) as store, store._conn:
        store._conn.execute("DELETE FROM runs WHERE run_id = ?", (row["run_id"],))
        store._conn.execute(
            "DELETE FROM run_details WHERE run_id = ?", (row["run_id"],)
        )
    assert plan_review.plan_authoring_runs(
        PROJECT, now_seconds=MOMENT, pointers=[]
    ) == _reference_authors(MOMENT)
    counts = _instrument(monkeypatch)
    expected = _reference_authors(MOMENT)
    counts.clear()
    assert (
        plan_review.plan_authoring_runs(PROJECT, now_seconds=MOMENT, pointers=[])
        == expected
    )
    assert counts["runs"] == 0
