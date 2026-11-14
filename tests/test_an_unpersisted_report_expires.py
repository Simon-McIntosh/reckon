"""An unpersisted report item expires at its plan's close or after 30 days.

Every case runs against a synthetic crew home and a mounted synthetic project.
The operator's real crew home is read before and after each pass so an item
that leaked out of the temporary home fails the matrix rather than colliding
with a live fleet.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew.routing import _run_report_trees
from reckon.crew.runs import reports_dir
from tests.conftest import real_store_home, real_store_is_not_a_fixture_target

PROJECT = "report-probe-project"
OPEN_PLAN = "an-open-probe-plan"
CLOSED_PLAN = "a-closed-probe-plan"
RESEARCH = "probe-research"
EVIDENCE = "probe-evidence"
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


def _doc(docs: Path, kind: str, slug: str, body: str = "") -> Path:
    path = (
        (docs / kind / f"{slug}.html") if kind == "plans" else (docs / f"{slug}.html")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    kind_type = {"plans": "plan", "research": "research", "evidence": "evidence"}[kind]
    status = "active"
    if slug == CLOSED_PLAN:
        status = "done"
    path.write_text(
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f'<meta name="reckon-type" content="{kind_type}">'
        f'<meta name="plan-slug" content="{slug}">'
        f'<meta name="plan-status" content="{status}">'
        f"<title>{slug}</title></head><body>{body}</body></html>"
    )
    return path


def _age(path: Path, days: int) -> None:
    stamp = (NOW - timedelta(days=days)).timestamp()
    if path.is_dir():
        for entry in sorted(path.rglob("*"), reverse=True):
            os.utime(entry, (stamp, stamp), follow_symlinks=False)
    if path.exists() or path.is_symlink():
        os.utime(path, (stamp, stamp), follow_symlinks=False)


@pytest.fixture()
def store(home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    docs = tmp_path / "sample-repo" / "docs"
    docs.mkdir(parents=True)
    (docs / ".reckon").mkdir()
    (docs / ".reckon" / "project-state-migration.json").write_text(
        json.dumps({"status": "complete", "format": "distributed"})
    )
    _doc(docs, "plans", OPEN_PLAN)
    _doc(docs, "plans", CLOSED_PLAN)

    staging_root = tmp_path / "work" / "staging"
    persist_root = tmp_path / "work" / "persist"
    flight_yaml = docs / "state" / PROJECT / "flight.yaml"
    flight_yaml.parent.mkdir(parents=True)
    flight_yaml.write_text(
        f"reports:\n  persist_root: {persist_root}\n  staging_root: {staging_root}\n"
    )

    mounts = tmp_path / "mounts.json"
    mounts.write_text(json.dumps({PROJECT: str(docs)}))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))

    monkeypatch.setattr("reckon.ledger.runs", lambda *a, **k: [])

    reports = reports_dir() / PROJECT
    reports.mkdir(parents=True)
    return {
        "home": home,
        "docs": docs,
        "reports": reports,
        "persist_root": persist_root,
        "staging_root": staging_root,
    }


def _item(reports: Path, name: str, days: int, filename: str = "trace.svg") -> Path:
    item = reports / name
    item.mkdir(parents=True)
    (item / filename).write_text("<svg>trace</svg>\n")
    _age(item, days)
    return item


def _sweep(*, apply: bool) -> list[dict]:
    return _run_report_trees(
        project=PROJECT,
        apply=apply,
        now=NOW,
        run_id=None,
        live_ids=set(),
        ledgered=set(),
        retention_days=30,
    )


def test_an_unreferenced_item_thirty_one_days_old_is_removed(store):
    old = _item(store["reports"], "old-item", 31)
    young = _item(store["reports"], "young-item", 29)

    rows = _sweep(apply=True)

    assert not old.exists()
    assert young.exists()
    assert [row["run_id"] for row in rows] == ["old-item"]


def test_a_folder_named_for_a_plan_keeps_until_that_plan_closes(store):
    open_item = _item(store["reports"], OPEN_PLAN, 29)
    closed_item = _item(store["reports"], CLOSED_PLAN, 29)

    _sweep(apply=True)

    assert open_item.exists(), "an open plan's item is kept at 29 days"
    assert not closed_item.exists(), "a closed plan's item expires at its close"


def test_a_thirty_one_day_item_of_an_open_plan_is_removed(store):
    item = _item(store["reports"], OPEN_PLAN, 31)

    _sweep(apply=True)

    assert not item.exists(), (
        "the 30-day ceiling applies whichever way it is attributed"
    )


def test_a_ledger_run_attributes_an_item_to_its_plan(store, monkeypatch):
    item = _item(store["reports"], "hand-filed-topic", 29)
    run_id = "r-20260101T000000000000-a-run"
    (item / run_id).mkdir()

    monkeypatch.setattr(
        "reckon.ledger.runs",
        lambda *a, **k: [{"plan": CLOSED_PLAN, "run_id": run_id}],
    )

    _sweep(apply=True)

    assert not item.exists()


def test_an_unattributable_item_takes_only_the_ceiling(store):
    item = _item(store["reports"], "nobody-names-me", 29)

    _sweep(apply=True)

    assert item.exists(), "no plan's status may end an item no plan is named for"


def test_every_keeper_survives_ninety_days(store, monkeypatch):
    docs = store["docs"]
    reports = store["reports"]

    plain_cited = _item(reports, "cited-directly", 90)
    nested_cited = _item(reports, "cited-through-a-file", 90)
    research_cited = _item(reports, "cited-by-research", 90)
    evidence_cited = _item(reports, "cited-by-evidence", 90)
    live_named = _item(reports, "named-by-a-live-run", 90)

    _doc(docs, "plans", "a-citing-plan", f'<a href="{plain_cited}">x</a>')
    _doc(
        docs,
        "plans",
        "a-nested-citing-plan",
        f'<img src="{nested_cited / "trace.svg"}">',
    )
    _doc(docs, "research", RESEARCH, f'<a href="{research_cited}">x</a>')
    _doc(docs, "evidence", EVIDENCE, f'<a href="{evidence_cited}">x</a>')

    import importlib

    runs_module = importlib.import_module("reckon.crew.runs")
    monkeypatch.setattr(
        runs_module,
        "list_live",
        lambda **kwargs: [{"run_id": live_named.name}],
    )

    _sweep(apply=True)

    for item in (plain_cited, nested_cited, research_cited, evidence_cited, live_named):
        assert item.exists(), item.name


def test_a_persisted_item_is_kept_at_ninety_days(store):
    from reckon.crew import persist

    figure = _item(store["reports"], "persisted-item", 90) / "trace.svg"
    persist.persist_into(
        figure,
        project=PROJECT,
        docs_dir=store["docs"],
        plan=OPEN_PLAN,
        persisted_by="probe",
    )

    _sweep(apply=True)

    assert figure.exists()


def test_no_reckon_written_folder_is_removed_whole_and_plan_review_is_untouched(store):
    reports = store["reports"]
    code_review = reports / "code-review"
    stale_child = code_review / "stale-round"
    stale_child.mkdir(parents=True)
    (stale_child / "report.md").write_text("old\n")
    _age(stale_child, 31)
    _age(code_review, 31)

    plan_review = reports / "plan-review" / "some-plan" / "r-1"
    plan_review.mkdir(parents=True)
    (plan_review / "report.md").write_text("raw\n")
    _age(plan_review, 400)

    _sweep(apply=True)

    assert code_review.is_dir(), "the container folder is never removed whole"
    assert not stale_child.exists(), "its stale child is an item in its own right"
    assert plan_review.is_dir(), "plan-review keeps its own rule"


def test_a_symlinked_item_is_expired_with_its_target(store):
    target = store["staging_root"] / "moved-item"
    target.mkdir(parents=True)
    (target / "trace.svg").write_text("<svg>trace</svg>\n")
    _age(target, 40)
    link = store["reports"] / "moved-item"
    link.symlink_to(target)

    _sweep(apply=True)

    assert not link.exists()
    assert not target.exists()


def test_a_symlink_outside_the_permitted_roots_is_kept_and_reported(store, tmp_path):
    outside = tmp_path / "elsewhere" / "outside-item"
    outside.mkdir(parents=True)
    (outside / "trace.svg").write_text("<svg>trace</svg>\n")
    _age(outside, 40)
    link = store["reports"] / "outside-item"
    link.symlink_to(outside)

    rows = _sweep(apply=True)

    assert link.exists() and outside.exists()
    row = next(row for row in rows if row["run_id"] and "outside" in row["path"])
    assert row["action"] == "keep"
    assert "outside the permitted roots" in row["reason"]


def test_the_dry_run_names_exactly_what_apply_removes(store):
    _item(store["reports"], "candidate-one", 31)
    _item(store["reports"], "candidate-two", 40)
    _item(store["reports"], "kept-one", 5)

    candidates = {row["path"] for row in _sweep(apply=False)}
    assert Path(store["reports"] / "candidate-one") in {Path(p) for p in candidates}

    before = {Path(p) for p in candidates}
    rows = _sweep(apply=True)
    removed = {Path(row["path"]) for row in rows}

    assert removed == before


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """Every entry under ``root`` with its mtime and size, for a before/after."""
    snapshot: dict[str, tuple[int, int]] = {}
    if not root.is_dir():
        return snapshot
    for path in sorted(root.rglob("*")):
        try:
            flat = path.lstat()
        except OSError:
            continue
        snapshot[str(path)] = (flat.st_mtime_ns, flat.st_size)
    return snapshot


def _real_reports_root() -> Path:
    return real_store_home() / "crew" / "reports"


def test_the_real_crew_home_is_untouched(store):
    """A sweep in the synthetic home leaves the operator's own store identical."""
    real_store_is_not_a_fixture_target(store["home"])
    root = _real_reports_root()
    before = _snapshot(root)

    _item(store["reports"], "aged-away", 40)
    rows = _sweep(apply=True)

    assert any(row["removed"] for row in rows), "the sweep must have removed something"
    assert _snapshot(root) == before
