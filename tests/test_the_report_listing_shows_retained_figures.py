"""A plan's report items are listed, filtered and persisted from the plan view.

The listing is the server's: the surface shows the retention class, the expiry
and the counts it carries and derives none of them, so the default view is the
retained material and every assertion reads its value back from the listing.
The browser cases serve the SPA against a synthetic project whose reports store
holds one persisted, one cited and two working figures, persist a working figure
through the control, and read the project's persisted-items resource back from
the temporary store.

Every path this module resolves is inside the test's own temporary tree. The
cases assert within it that the operator's real crew home is never the target.

Setting ``RECKON_REPORT_LISTING_SWAP_CLASSES`` swaps each item's class in the
listing the page receives. The default view then shows the working figures, so
the assertions below fail: the negative control makes the guarded thing happen
and shows the test refusing it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli
from reckon.crew import persist
from reckon.project_state import create_project_state
from tests.conftest import real_store_is_not_a_fixture_target
from tests.spa_browser_harness import installed_browser_or_skip, served_spa

PROJECT = "report-listing-project"
PLAN = "a-report-listing-plan"
PERSISTED_ITEM = "persisted-run"
CITED_ITEM = "cited-run"
WORKING_ITEMS = ("working-run-a", "working-run-b")
FIGURE = "figure.svg"
DATA = "trace.nc"

RETAINED_NAMES = sorted([PERSISTED_ITEM, CITED_ITEM])
ALL_NAMES = sorted([PERSISTED_ITEM, CITED_ITEM, *WORKING_ITEMS])

DEFAULT_VIEW_PROBE = """(async () => {
  const rows = () => [...document.querySelectorAll('.r-report-item')].map(r => ({
    name: r.dataset.item,
    retention: r.dataset.retention,
    expires: r.dataset.expires || '',
  }));
  const counts = () => Object.fromEntries(
    [...document.querySelectorAll('.r-report-count')]
      .map(n => [n.dataset.count, Number(n.textContent)])
  );
  const filter = () => document.querySelector('.r-report-items').dataset.filter;
  const retained = { filter: filter(), rows: rows(), counts: counts() };
  document.querySelector('[data-filter="all"]').click();
  const end = Date.now() + 10000;
  while (Date.now() < end && filter() !== 'all') {
    await new Promise(go => setTimeout(go, 50));
  }
  return {
    retained: retained,
    all: { filter: filter(), rows: rows(), counts: counts() },
  };
})()"""

PERSIST_PROBE = """(async () => {
  const rows = () => [...document.querySelectorAll('.r-report-item')].map(r => ({
    name: r.dataset.item,
    retention: r.dataset.retention,
  }));
  const filter = () => document.querySelector('.r-report-items').dataset.filter;
    const pick = () => document.querySelector('.r-report-item[data-item="TARGET"]');
  document.querySelector('[data-filter="all"]').click();
  let end = Date.now() + 10000;
  while (Date.now() < end && filter() !== 'all') {
    await new Promise(go => setTimeout(go, 50));
  }
  const all = rows();
  pick().querySelector('.r-report-persist').click();
  end = Date.now() + 10000;
  while (Date.now() < end && !pick().querySelector('.r-report-persist-panel')) {
    await new Promise(go => setTimeout(go, 50));
  }
  const boxes = [...pick().querySelectorAll('input.r-report-data-file')];
  let ticked = 0;
  if (TICK && boxes.length > 0) {
    boxes[0].click();
    ticked = 1;
  }
  pick().querySelector('.r-report-persist-apply').click();
  end = Date.now() + 15000;
  while (Date.now() < end && pick().dataset.retention !== 'persisted') {
    await new Promise(go => setTimeout(go, 50));
  }
  const moved = pick().dataset.retention === 'persisted';
  document.querySelector('[data-filter="retained"]').click();
  end = Date.now() + 10000;
  while (Date.now() < end && filter() !== 'retained') {
    await new Promise(go => setTimeout(go, 50));
  }
  return {
    all: all,
    ticked: ticked,
    moved: moved,
    retained: { filter: filter(), rows: rows() },
    error: (document.querySelector('.r-report-error') || {}).textContent || '',
  };
})()"""


@pytest.fixture(scope="module")
def rendered_browser() -> str:
    return installed_browser_or_skip()


def _swap_classes_requested() -> bool:
    return os.environ.get("RECKON_REPORT_LISTING_SWAP_CLASSES") == "1"


_SWAP_PRELOAD = """(() => {
  const original = window.fetch.bind(window);
  const swap = value => value === 'persisted' ? 'working'
                    : value === 'working' ? 'persisted' : value;
  window.fetch = async (resource, options = {}) => {
    const response = await original(resource, options);
    const url = String(resource);
    const method = String((options && options.method) || 'GET').toUpperCase();
    if (method !== 'GET' || !url.includes('/reports')) return response;
    const payload = await response.clone().json();
    if (payload && Array.isArray(payload.items)) {
      payload.items = payload.items.map(item => ({...item, class: swap(item.class)}));
    }
    return new Response(JSON.stringify(payload), {
      status: response.status,
      headers: {'Content-Type': 'application/json'},
    });
  };
})()"""


def _plan_doc(docs: Path, citation: Path) -> Path:
    path = docs / "plans" / f"{PLAN}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{PLAN}">'
        '<meta name="plan-title" content="Report listing">'
        '<meta name="plan-status" content="active">'
        "<title>Report listing</title></head><body><main>"
        f'<p>Cited figure: <a href="{citation}">trace</a>.</p>'
        "<p>A plan whose runs left figures in the reports store.</p>"
        "</main></body></html>",
        encoding="utf-8",
    )
    return path


def _create_state(docs: Path) -> None:
    create_project_state(docs, PROJECT)


def _write_item(plan_dir: Path, name: str) -> Path:
    item = plan_dir / name
    item.mkdir(parents=True)
    figure = item / FIGURE
    figure.write_text(
        "<svg xmlns='http://www.w3.org/2000/svg'></svg>\n", encoding="utf-8"
    )
    if name != CITED_ITEM:
        (item / DATA).write_text("data for the figure\n", encoding="utf-8")
    return figure


def _build_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    crew_home = tmp_path / "crew-home"
    crew_home.mkdir()
    real_store_is_not_a_fixture_target(crew_home)
    docs = tmp_path / "repo" / "docs"
    docs.mkdir(parents=True)
    (docs / ".reckon").mkdir()
    (docs / "state").mkdir()
    plan_dir = crew_home / "crew" / "reports" / PROJECT / PLAN
    plan_dir.mkdir(parents=True)
    figures = {}
    for name in (PERSISTED_ITEM, CITED_ITEM, *WORKING_ITEMS):
        figures[name] = _write_item(plan_dir, name)
    _plan_doc(docs, figures[CITED_ITEM])
    _create_state(docs)
    mounts = tmp_path / "mounts.json"
    mounts.write_text(json.dumps({PROJECT: str(docs)}), encoding="utf-8")
    monkeypatch.setenv("RECKON_HOME", str(crew_home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))
    result = CliRunner().invoke(cli.main, ["build", str(docs), "--project", PROJECT])
    assert result.exit_code == 0, result.output
    persist_root = tmp_path / "work" / "persist"
    flight_yaml = docs / "state" / PROJECT / "flight.yaml"
    flight_yaml.parent.mkdir(parents=True, exist_ok=True)
    flight_yaml.write_text(
        f"reports:\n  persist_root: {persist_root}\n", encoding="utf-8"
    )
    seeded = persist.persist_into(
        figures[PERSISTED_ITEM], project=PROJECT, plan=PLAN, docs_dir=docs
    )
    assert len(seeded) == 1
    return {
        "crew_home": crew_home,
        "docs": docs,
        "plan_dir": plan_dir,
        "figures": figures,
        "persist_root": persist_root,
        "mounts": mounts,
    }


def _records(store: dict) -> list[dict]:
    return persist.read_persisted(PROJECT, docs_dir=store["docs"])


def _names(rows: list[dict]) -> list[str]:
    return sorted(row["name"] for row in rows)


def _preload() -> str | None:
    return _SWAP_PRELOAD if _swap_classes_requested() else None


def _serve(tmp_path: Path, browser: str, store: dict):
    return served_spa(
        tmp_path,
        browser,
        docs=store["docs"],
        project=PROJECT,
        route=f"#plan/{PLAN}",
        crew_home=store["crew_home"],
    )


def test_the_default_view_lists_exactly_the_retained_items(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rendered_browser: str,
) -> None:
    store = _build_store(tmp_path, monkeypatch)
    with _serve(tmp_path, rendered_browser, store) as spa:
        result = spa.run_probe(
            DEFAULT_VIEW_PROBE,
            ready_expression=("Boolean(document.querySelector('.r-report-item'))"),
            preload_expression=_preload(),
        )
    assert result["retained"]["filter"] == "retained"
    assert _names(result["retained"]["rows"]) == RETAINED_NAMES
    assert result["all"]["filter"] == "all"
    assert _names(result["all"]["rows"]) == ALL_NAMES
    assert result["retained"]["counts"] == {
        "retained": 2,
        "working": 2,
        "all": 4,
    }
    retained_rows = {row["name"]: row for row in result["retained"]["rows"]}
    for name in RETAINED_NAMES:
        assert retained_rows[name]["retention"] in {"persisted", "cited"}
        assert retained_rows[name]["expires"] == ""
    working_rows = [
        row for row in result["all"]["rows"] if row["name"] in WORKING_ITEMS
    ]
    assert len(working_rows) == 2
    assert all(row["retention"] == "working" for row in working_rows)
    assert all(row["expires"] for row in working_rows)


def test_persisting_a_working_figure_moves_it_into_the_retained_view(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rendered_browser: str,
) -> None:
    store = _build_store(tmp_path, monkeypatch)
    before = _records(store)
    probe = PERSIST_PROBE.replace("TARGET", WORKING_ITEMS[0]).replace("TICK", "false")
    with _serve(tmp_path, rendered_browser, store) as spa:
        result = spa.run_probe(
            probe,
            ready_expression=("Boolean(document.querySelector('.r-report-item'))"),
            preload_expression=_preload(),
        )
    assert result["error"] == ""
    assert result["moved"] is True
    assert result["ticked"] == 0
    assert result["retained"]["filter"] == "retained"
    assert _names(result["retained"]["rows"]) == sorted(
        [*RETAINED_NAMES, WORKING_ITEMS[0]]
    )
    after = _records(store)
    assert len(after) == len(before) + 1
    fresh = [record for record in after if record not in before]
    assert [record["kind"] for record in fresh] == ["figure"]
    assert fresh[0]["source"] == str(
        (store["plan_dir"] / WORKING_ITEMS[0] / FIGURE).resolve()
    )


def test_choosing_figure_with_data_ticking_one_file_leaves_two_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rendered_browser: str,
) -> None:
    store = _build_store(tmp_path, monkeypatch)
    before = _records(store)
    probe = PERSIST_PROBE.replace("TARGET", WORKING_ITEMS[1]).replace("TICK", "true")
    with _serve(tmp_path, rendered_browser, store) as spa:
        result = spa.run_probe(
            probe,
            ready_expression=("Boolean(document.querySelector('.r-report-item'))"),
            preload_expression=_preload(),
        )
    assert result["error"] == ""
    assert result["moved"] is True
    assert result["ticked"] == 1
    assert result["retained"]["filter"] == "retained"
    assert _names(result["retained"]["rows"]) == sorted(
        [*RETAINED_NAMES, WORKING_ITEMS[1]]
    )
    after = _records(store)
    assert len(after) == len(before) + 2
    fresh = [record for record in after if record not in before]
    assert sorted(record["kind"] for record in fresh) == ["data", "figure"]
    data_record = next(record for record in fresh if record["kind"] == "data")
    assert data_record["source"].endswith(DATA)
    assert data_record["figure"] == str(
        (store["plan_dir"] / WORKING_ITEMS[1] / FIGURE).resolve()
    )
