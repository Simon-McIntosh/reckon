from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.spa_browser_harness import file_spa, installed_browser_or_skip

ROOT = Path(__file__).resolve().parents[1]
LOADER = ROOT / "docs" / "ui" / "state-loader.js"


@pytest.fixture(scope="module")
def rendered_browser() -> str:
    return installed_browser_or_skip()


# ─── Node-level arrival diff (real state-loader, mocked discovery) ────────


def _load_arrival_state(
    payloads: list[dict],
    *,
    reveal_kind: str | None = None,
    revalidate_after_reveal: bool = True,
) -> dict:
    script = f"""
const fs = require("fs");
global.window = {{location: {{pathname: "/sample/"}}}};
global.document = {{querySelector: () => ({{content: "sample"}})}};
const payloads = {json.dumps(payloads)};
let discoveryCalls = 0;
global.fetch = async (url) => {{
  if (url === "state/sample/projection.json") {{
    return {{ok: false, status: 404, json: async () => ({{}})}};
  }}
  if (url === "state/sample/index.json") {{
    return {{ok: true, status: 200, json: async () => ({{data: {{}}}})}};
  }}
  if (url === "/_discover/sample") {{
    const payload = payloads[Math.min(discoveryCalls, payloads.length - 1)];
    discoveryCalls += 1;
    return {{ok: true, status: 200, json: async () => payload}};
  }}
  throw new Error("unexpected fetch " + url);
}};
eval(fs.readFileSync({json.dumps(str(LOADER))}, "utf8"));
const summarize = state => ({{
  receipt: state.arrival.receipt,
  total: state.arrival.total,
  byKind: state.arrival.byKind,
  pending: state.arrival.pending.map(row => row.nav_key),
  updates: state.arrival.updates.map(row => row.nav_key),
  inventory: state.inventory.map(row => row.nav_key),
}});
window.STATE_READY.then(async () => {{
  const snapshots = [summarize(window.STATE)];
  for (let index = 1; index < payloads.length; index++) {{
    await window.revalidateProjectState();
    snapshots.push(summarize(window.STATE));
  }}
  let revealed = null;
  if ({json.dumps(reveal_kind)}) {{
    revealed = window.revealArrivals({json.dumps(reveal_kind)}).map(row => row.nav_key);
    if ({json.dumps(revalidate_after_reveal)}) {{
      await window.revalidateProjectState();
      snapshots.push(summarize(window.STATE));
    }}
  }}
  console.log(JSON.stringify({{snapshots, revealed}}));
}});
"""
    result = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _discovery(*inventory: dict) -> dict:
    return {"inventory": list(inventory)}


def _load(row_type: str, slug: str, **extra: object) -> dict:
    return {"slug": slug, "type": row_type, "status": "active", **extra}


def test_arrival_diff_holds_new_evidence_and_marks_a_changed_plan() -> None:
    first = _discovery(
        _load("plan", "sit", edited="2026-01-01T00:00:00"),
        _load("evidence", "marker-1", gate="running", edited="2026-01-02T00:00:00"),
    )
    second = _discovery(
        _load("plan", "sit", edited="2026-01-05T00:00:00"),
        _load("evidence", "marker-1", gate="running", edited="2026-01-02T00:00:00"),
        _load("evidence", "arrival-1", gate="running", edited="2026-01-06T00:00:00"),
        _load("evidence", "arrival-2", gate="passed", edited="2026-01-06T00:00:00"),
    )
    observed = _load_arrival_state(
        [first, second], reveal_kind="evidence", revalidate_after_reveal=True
    )
    snapshots, revealed = observed["snapshots"], observed["revealed"]

    assert snapshots[0]["receipt"] == "live"
    assert snapshots[0]["pending"] == []
    assert snapshots[0]["updates"] == []

    assert snapshots[1]["receipt"] == "2 new"
    assert snapshots[1]["total"] == 2
    assert snapshots[1]["byKind"] == {"evidence": 2}
    assert snapshots[1]["pending"] == [
        "evidence:arrival-1",
        "evidence:arrival-2",
    ]
    assert snapshots[1]["updates"] == ["sit"]
    # The hold is a rendering concern: the inventory stays complete so an open
    # page's state never loses the arriving rows.
    assert snapshots[1]["inventory"] == [
        "sit",
        "evidence:marker-1",
        "evidence:arrival-1",
        "evidence:arrival-2",
    ]

    assert revealed == ["evidence:arrival-1", "evidence:arrival-2"]
    # After the reader reveals them, a later change event must not re-flag
    # the same rows as new again.
    assert snapshots[2]["receipt"] == "live"
    assert snapshots[2]["pending"] == []


def test_no_new_change_leaves_the_receipt_live() -> None:
    payload = _discovery(_load("plan", "sit", edited="2026-01-01T00:00:00"))
    observed = _load_arrival_state([payload, payload])

    snapshots = observed["snapshots"]
    assert snapshots[0]["receipt"] == "live"
    assert snapshots[1]["receipt"] == "live"
    assert snapshots[1]["pending"] == []
    assert snapshots[1]["updates"] == []


# ─── Rendered hold (composed state via the browser harness) ───────────────


def _row(slug: str, **extra: object) -> dict:
    kind = extra.pop("type", "evidence")
    return {
        "slug": slug,
        "nav_key": slug if kind == "plan" else f"{kind}:{slug}",
        "title": slug,
        "type": kind,
        "status": "active",
        "effective_status": "active",
        "gate": "running",
        "created": 100,
        "edited": "2026-01-01T00:00:00",
        **extra,
    }


def _artifact_state(*rows: dict, arrival: dict | None = None) -> dict:
    inventory = list(rows)
    state = {
        "project": "reckon",
        "projects": [{"project": "reckon", "plans_count": len(inventory)}],
        "inventory": inventory,
        "plans": {row["nav_key"]: row for row in inventory},
        "sprints": [],
        "milestones": [],
        "north_stars": [],
        "timeline": [],
        "blockers": [],
        "attachment_relations": [],
        "ready_set": {},
        "endpoints": [],
        "active_sprints": [],
        "active_sprint_conflict": False,
    }
    if arrival is not None:
        state["arrival"] = arrival
    return state


_SORT_PRELOAD = (
    "localStorage.setItem('reckon:reckon:groupBy','created');"
    "localStorage.setItem('reckon:reckon:sortDirs', JSON.stringify({created:'desc'}));"
)


def _baseline_rows() -> list[dict]:
    return [
        _row("alpha", created=100, edited="2026-01-01T00:00:00"),
        _row("beta", created=200, edited="2026-01-02T00:00:00"),
        _row("gamma", created=300, edited="2026-01-03T00:00:00"),
        _row("foundation", type="plan", created=1, edited="2026-01-01T00:00:00"),
    ]


def _arrival_rows() -> list[dict]:
    base = _baseline_rows()
    delta = _row("delta", created=250, edited="2026-01-07T00:00:00")
    epsilon = _row("epsilon", created=150, edited="2026-01-07T00:00:00")
    beta_v2 = _row("beta", created=200, edited="2026-01-07T00:00:00", title="beta v2")
    return [base[0], beta_v2, base[2], base[3], delta, epsilon]


def test_rendered_order_survives_arrival_until_the_show_control(
    tmp_path: Path, rendered_browser: str
) -> None:
    probe_baseline = r"""(() => ({
      order: [...document.querySelectorAll(".r-artifact-row")].map(row => row.dataset.artifactSlug),
      banner: Boolean(document.querySelector(".r-arrival-banner")),
    }))()"""
    probe_arrival = r"""(async () => {
      const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
      const waitFor = async (predicate, ms = 3000) => {
        const deadline = performance.now() + ms;
        while (performance.now() < deadline) {
          const value = predicate();
          if (value) return value;
          await delay(25);
        }
        return null;
      };
      const order = () => [...document.querySelectorAll(".r-artifact-row")]
        .map(row => row.dataset.artifactSlug);
      const before = order();
      const titlesBefore = [...document.querySelectorAll(".r-artifact-row")]
        .map(row => row.querySelector(".r-artifact-row-title")?.textContent.trim());
      const bannerText = document.querySelector(".r-arrival-banner-text")?.textContent.trim() || null;
      const showLabel = document.querySelector(".r-arrival-show")?.textContent.trim() || null;
      document.querySelector(".r-arrival-show")?.click();
      const after = await waitFor(() => {
        if (document.querySelector(".r-arrival-banner")) return null;
        return order();
      });
      const arriving = [...document.querySelectorAll(".r-artifact-row.is-arriving")]
        .map(row => row.dataset.artifactSlug);
      const titlesAfter = [...document.querySelectorAll(".r-artifact-row")]
        .map(row => row.querySelector(".r-artifact-row-title")?.textContent.trim());
      return { before, titlesBefore, bannerText, showLabel, after, arriving, titlesAfter };
    })()"""

    baseline = _baseline_rows()
    arrival_rows = _arrival_rows()
    delta, epsilon = arrival_rows[4], arrival_rows[5]
    beta_v2, foundation = arrival_rows[1], arrival_rows[3]
    arrival_state = _artifact_state(
        *arrival_rows,
        arrival={
            "pending": [delta, epsilon],
            "updates": [beta_v2, foundation],
            "byKind": {"evidence": 2},
            "total": 2,
            "receipt": "2 new",
        },
    )

    with file_spa(
        tmp_path, rendered_browser, _artifact_state(*baseline), route="#evidence"
    ) as spa:
        base_result = spa.run_probe(
            probe_baseline,
            viewport=(1100, 800),
            ready_expression="Boolean(document.querySelector('.r-artifact-row'))",
            preload_expression=_SORT_PRELOAD,
        )

    with file_spa(
        tmp_path, rendered_browser, arrival_state, route="#evidence"
    ) as spa:
        arrival_result = spa.run_probe(
            probe_arrival,
            viewport=(1100, 800),
            ready_expression="Boolean(document.querySelector('.r-artifact-row'))",
            preload_expression=_SORT_PRELOAD,
        )

    order0 = base_result["order"]
    assert not base_result["banner"]
    assert arrival_result["before"] == order0, (
        f"arrival re-sorted the list under the reader: baseline {order0} "
        f"!= post-payload {arrival_result['before']}"
    )
    assert arrival_result["bannerText"] == "2 new evidence since you opened this list"
    assert arrival_result["showLabel"] == "show"
    # A document that changed in place updates its row without moving it.
    assert arrival_result["titlesBefore"][1] == "beta v2"
    expected_full = [
        "evidence:gamma",
        "evidence:delta",
        "evidence:beta",
        "evidence:epsilon",
        "evidence:alpha",
    ]
    assert arrival_result["after"] == expected_full
    assert arrival_result["after"] != arrival_result["before"]
    assert sorted(arrival_result["arriving"]) == ["evidence:delta", "evidence:epsilon"]
    assert arrival_result["titlesAfter"][2] == "beta v2"


def test_other_kinds_pending_do_not_touch_the_open_index(
    tmp_path: Path, rendered_browser: str
) -> None:
    probe = r"""(() => ({
      order: [...document.querySelectorAll(".r-artifact-row")].map(row => row.dataset.artifactSlug),
      banner: Boolean(document.querySelector(".r-arrival-banner")),
    }))()"""
    pending_plan = _row("fresh-plan", type="plan", created=50)
    state = _artifact_state(
        *_baseline_rows(),
        arrival={
            "pending": [pending_plan],
            "updates": [],
            "byKind": {"plan": 1},
            "total": 1,
            "receipt": "1 new",
        },
    )
    with file_spa(
        tmp_path, rendered_browser, state, route="#evidence"
    ) as spa:
        result = spa.run_probe(
            probe,
            viewport=(1100, 800),
            ready_expression="Boolean(document.querySelector('.r-artifact-row'))",
            preload_expression=_SORT_PRELOAD,
        )

    assert not result["banner"]
    assert result["order"] == [
        "evidence:gamma",
        "evidence:beta",
        "evidence:alpha",
    ]
