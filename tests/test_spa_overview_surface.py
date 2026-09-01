import json
import subprocess
from pathlib import Path

import pytest

from tests.spa_browser_harness import (
    BrowserProbeError,
    installed_browser,
    run_browser_probe,
    served_spa,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "ui" / "shell.jsx"


@pytest.fixture(scope="module")
def rendered_browser(tmp_path_factory) -> str:
    browser = installed_browser()
    if browser is None:
        pytest.skip("no supported browser binary is installed")
    try:
        run_browser_probe(
            tmp_path_factory.mktemp("browser-capability"),
            browser,
            "<!doctype html><html><body>ready</body></html>",
            "document.body.textContent",
        )
    except BrowserProbeError as error:
        pytest.skip(f"browser unavailable ({error.classification}): {error}")
    return browser


def _function_source(name: str) -> str:
    source = SOURCE.read_text()
    start = source.index(f"function {name}(")
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated function {name}")


def _evaluate(functions: list[str], expression: str):
    script = "\n".join(_function_source(name) for name in functions)
    result = subprocess.run(
        ["node", "-e", f"{script}\nconsole.log(JSON.stringify({expression}));"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_every_active_sprint_survives_the_overview_projection() -> None:
    state = {
        "active_sprint_id": "middle",
        "active_sprint_conflict": True,
        "sprints": [
            {"id": "first", "status": "active"},
            {"id": "middle", "status": "active"},
            {"id": "last", "status": "active"},
            {"id": "closed", "status": "done"},
        ],
    }
    result = _evaluate(
        ["projectActiveSprints"],
        f"projectActiveSprints({json.dumps(state)})",
    )

    assert [sprint["id"] for sprint in result["active"]] == [
        "first",
        "middle",
        "last",
    ]
    assert result["focus"] == "middle"
    assert result["conflict"] is True


def test_legacy_focus_and_conflict_warning_link_the_sprint_resources() -> None:
    source = SOURCE.read_text()

    assert "sprint.id === row.focus" in source
    assert "legacy focus" in source
    assert 'className="r-overview-conflict" role="alert"' in source
    assert "row.active.map(sprint => sprint.id)" in source
    assert "href={`#sprint/${id}`}" in source


def test_unresolved_blocker_keeps_summary_owner_gated_count_and_next_action() -> None:
    blocker = {
        "id": "capacity",
        "summary": "No execution seat is available",
        "owner": "operations",
        "n": 3,
        "next": "Open one execution seat",
    }
    result = _evaluate(
        ["blockerIsUnresolved"],
        f"blockerIsUnresolved({json.dumps(blocker)})",
    )
    source = SOURCE.read_text()

    assert result is True
    assert "blocker.summary" in source
    assert "blocker.owner" in source
    assert "blocker.n" in source
    assert "blocker.next" in source


def test_resolved_blockers_do_not_enter_project_rows() -> None:
    current = {
        "project": "sample",
        "inventory": [],
        "sprints": [],
        "blockers": [
            {"id": "open", "summary": "Still open", "next": "Act"},
            {
                "id": "closed",
                "summary": "Resolved: service restored",
                "next": "Resolved with no further action",
            },
        ],
    }
    result = _evaluate(
        [
            "blockerIsUnresolved",
            "projectActiveSprints",
            "blockerGatedPlans",
            "overviewProjectRows",
        ],
        f"overviewProjectRows([], {json.dumps(current)}, []).flatMap(row => row.blockers.map(blocker => blocker.id))",
    )

    assert result == ["open"]


def test_blockers_are_projected_by_the_plan_they_gate() -> None:
    state = {
        "project": "sample",
        "inventory": [],
        "sprints": [
            {
                "id": "current",
                "items": [
                    {"slug": "chosen", "blocked_by": ["shared"]},
                    {"slug": "other", "blocked_by": ["unrelated"]},
                ],
            }
        ],
        "blockers": [
            {"id": "shared", "summary": "Chosen blocker", "next": "Act"},
            {"id": "unrelated", "summary": "Other blocker", "next": "Wait"},
        ],
    }
    expression = (
        "(() => { const rows = overviewProjectRows([], "
        f"{json.dumps(state)}, []); return {{ scopes: overviewBlockerScopes(rows), "
        "chosen: blockersForPlanScope(rows, 'sample:chosen').map(blocker => blocker.id), "
        "other: blockersForPlanScope(rows, 'sample:other').map(blocker => blocker.id) }; })()"
    )
    result = _evaluate(
        [
            "blockerIsUnresolved",
            "projectActiveSprints",
            "blockerGatedPlans",
            "overviewProjectRows",
            "overviewBlockerScopes",
            "blockersForPlanScope",
        ],
        expression,
    )

    assert [scope["key"] for scope in result["scopes"]] == [
        "sample:chosen",
        "sample:other",
    ]
    assert result["chosen"] == ["shared"]
    assert result["other"] == ["unrelated"]


def _blocker_scope_expression() -> str:
    return """(async () => {
  const blocker = (id, plan, owner, next) => ({
    id, summary: id + " summary", owner, next, n: 1, gated_plans: [plan]
  });
  const alpha = {
    project: "alpha", inventory: [], sprints: [],
    blockers: [blocker("alpha-blocker", "alpha-plan", "alpha-owner", "alpha-action")]
  };
  const beta = {
    project: "beta", inventory: [], sprints: [],
    blockers: [blocker("beta-blocker", "beta-plan", "beta-owner", "beta-action")]
  };
  const noise = Array.from({ length: 18 }, (_, index) =>
    blocker("noise-" + index, "noise-plan-" + index, "noise-owner", "noise-action")
  );
  alpha.blockers.push(...noise);
  window.STATE = alpha;
  document.body.innerHTML = '<main id="scope-check"></main>';
  ReactDOM.createRoot(document.querySelector("#scope-check")).render(
    React.createElement(OverviewFleet, {
      projects: [{ project: "alpha", state: alpha }, { project: "beta", state: beta }],
      fleetRuns: [], mountedProjectCount: 2
    })
  );
  const waitFor = async predicate => {
    for (let attempt = 0; attempt < 100; attempt++) {
      if (predicate()) return;
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    throw new Error("timed out waiting for scoped blockers");
  };
  await waitFor(() => document.querySelectorAll(".r-overview-blockers article").length === 1);
  const ids = () => [...document.querySelectorAll(".r-overview-blocker-id")].map(node => node.textContent);
  const region = document.querySelector(".r-overview-blockers");
  const viewHeight = 1033;
  const first = {
    ids: ids(), height: region.getBoundingClientRect().height,
    owner: document.querySelector(".r-overview-blocker-owner").textContent,
    gated: document.querySelector(".r-overview-blocker-meta span:nth-child(3)").textContent,
    next: document.querySelector(".r-overview-blocker-next").textContent
  };
  const select = document.querySelector('select[aria-label="Plan scope for unresolved blockers"]');
  select.value = "beta:beta-plan";
  select.dispatchEvent(new Event("change", { bubbles: true }));
  await waitFor(() => ids()[0] === "beta-blocker");
  return { viewHeight, first, secondIds: ids() };
})()"""


def test_rendered_blockers_stay_below_one_view_and_change_with_plan_scope(
    tmp_path: Path,
    rendered_browser: str,
) -> None:
    browser = rendered_browser

    with served_spa(tmp_path, browser) as spa:
        measurement = spa.run_probe(
            _blocker_scope_expression(),
            viewport=(1374, 1100),
            ready_expression=(
                "Boolean(window.React && window.ReactDOM && "
                "typeof OverviewFleet === 'function')"
            ),
        )

    assert measurement["first"]["height"] < measurement["viewHeight"]
    assert measurement["first"]["ids"] == ["alpha-blocker"]
    assert measurement["secondIds"] == ["beta-blocker"]
    assert measurement["first"]["owner"] == "Owner: alpha-owner"
    assert measurement["first"]["gated"] == "1 gated"
    assert measurement["first"]["next"] == "Nextalpha-action"
