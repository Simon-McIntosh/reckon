from __future__ import annotations

import json
import subprocess
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import pytest

from reckon.serve import compile_jsx, discover_plans
from tests.spa_browser_harness import (
    BROWSER_NAMES,
    BrowserProbeError,
    ServedSpa,
    file_spa,
    installed_browser,
)

VIEWPORT_WIDTHS = (1280, 1440, 1920)


@contextmanager
def _skip_when_browser_is_unavailable():
    try:
        yield
    except BrowserProbeError as error:
        pytest.skip(f"browser unavailable ({error.classification}): {error}")


def _composed_state() -> dict[str, object]:
    root = Path(__file__).parents[1]
    state = discover_plans(root / "docs", "reckon", root / "docs/state")
    active = [
        sprint
        for sprint in state.get("sprints", [])
        if sprint.get("status") == "active"
    ]
    inventory = state.get("inventory", [])
    now = datetime.now(UTC)
    return {
        **state,
        "today": now.date().isoformat(),
        "project": "reckon",
        "projects": [{"project": "reckon", "plans_count": len(inventory)}],
        "loaded_at": now.isoformat(),
        "active_sprints": active,
        "active_sprint_conflict": len(active) > 1,
        "plans": {item["slug"]: item for item in inventory},
    }


def _javascript_function(source: str, name: str) -> str:
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
    raise AssertionError(f"unterminated JavaScript function: {name}")


def _evaluate_sprint_helpers(expression: str):
    source = (Path(__file__).parents[1] / "docs/ui/sprint.jsx").read_text()
    constants = "\n".join(
        line for line in source.splitlines() if line.startswith("const CLOSED_")
    )
    helpers = "\n".join(
        _javascript_function(source, name)
        for name in (
            "naturalSprintKey",
            "compareNaturalSprintIds",
            "orderedSprints",
            "sprintMemberSlugs",
            "sprintMembers",
            "sprintHoursSummary",
            "sprintStateRows",
            "readyLaneRows",
            "readyLaneState",
            "activeSprintConflict",
        )
    )
    result = subprocess.run(
        [
            "node",
            "-e",
            f"{constants}\n{helpers}\nconsole.log(JSON.stringify({expression}));",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _render_shell_without_ready_module() -> dict[str, object]:
    root = Path(__file__).parents[1]
    source = (root / "docs/ui/shell.jsx").read_text(encoding="utf-8")
    compiled = compile_jsx(
        source,
        filename="shell.jsx",
    ).decode()
    script = (
        r"""
const window = Object.create(null);
const noop = () => {};
let rendered = null;
const document = { getElementById() { return {}; }, querySelector() { return null; } };
const React = {
  createElement(type, props, ...children) { return { type, props: props || {}, children }; },
  useCallback: value => value,
  useEffect: noop,
  useMemo: value => value(),
  useRef: value => ({ current: value }),
  useState: value => [value, noop],
};
const ReactDOM = { createRoot() { return { render(value) { rendered = value; } }; } };
"""
        + compiled
        + r"""
function renderedText(node) {
  if (node == null || node === false) return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  return node.children.map(renderedText).join(" ");
}
process.stdout.write(JSON.stringify({
  childCount: rendered.children.length,
  text: renderedText(rendered),
  role: rendered.props.role || null,
}));
"""
    )
    result = subprocess.run(
        ["node", "-e", script],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(result.stdout)


def test_shell_module_skew_renders_a_legible_failure() -> None:
    rendered = _render_shell_without_ready_module()

    assert rendered["childCount"] == 4
    assert rendered["role"] == "alert"
    assert "Shell modules unavailable" in rendered["text"]


def test_sprint_surface_consumes_composed_review_without_a_review_fetch() -> None:
    source = (Path(__file__).parents[1] / "docs/ui/sprint.jsx").read_text()

    assert "const review = M.review || null" in source
    assert "review.sprint_order" in source
    # Composed sprint metrics now reach the surface through the state-row
    # projection over the served inventory, not a distinct review metrics dump.
    assert "sprintStateRows(allSprints, M.inventory)" in source
    assert "review.priority || []" in source
    assert "review?.findings || []" in source
    assert "fetch(`/review" not in source
    assert "DOMParser" not in source


def test_sprint_surface_keeps_natural_fallback_and_landed_priority_tail() -> None:
    source = (Path(__file__).parents[1] / "docs/ui/sprint.jsx").read_text()

    assert "sort(compareNaturalSprintIds)" in source
    assert "Number(left.landed) - Number(right.landed)" in source
    assert 'className={row.landed ? "landed" : ""}' in source
    for reason in ("critical-path", "unlock", "deadline", "roi", "decision-first"):
        assert reason not in source


def test_sprint_state_rows_keep_composed_metrics_dates_and_independent_flags() -> None:
    sprints = [
        {
            "id": "S2",
            "theme": "No calendar window",
            "status": "active",
            "starts": "",
            "ends": "",
            "derived_state": "active",
            "state_drift": {"stored": "planned", "derived": "active"},
            "implementation_pct": 37.5,
            "items": ["alpha"],
            "metrics": {"item_count": 2, "mean_impl": 0.375},
        },
        {
            "id": "S10",
            "status": "planned",
            "ends": "2026-08-31",
            "derived_state": "planned",
            "blocked": 2,
            "metrics": {"item_count": 1, "mean_impl": 0.0},
        },
        {
            "id": "S1",
            "status": "done",
            "starts": "",
            "ends": "",
            "derived_state": "shipped",
            "metrics": {"item_count": 1, "mean_impl": 1.0},
        },
    ]
    inventory = [{"slug": "alpha", "title": "Alpha", "effort_hours": 3, "impl": 1}]
    rows = _evaluate_sprint_helpers(
        f"sprintStateRows(orderedSprints({json.dumps(sprints)}, "
        f'{{sprint_order: ["S2", "S10", "S1"]}}), {json.dumps(inventory)})'
    )

    assert [row["sprint"]["id"] for row in rows] == ["S2", "S10", "S1"]
    # The first row keeps its authored window and its composed metrics; the
    # row adds derived state, drift flag, held count, impl fraction, member
    # hours and the closure verdict.
    assert rows[0]["sprint"]["starts"] == ""
    assert rows[0]["sprint"]["ends"] == ""
    assert rows[0]["sprint"]["metrics"]["mean_impl"] == 0.375
    assert rows[0]["state"] == "active"
    assert rows[0]["flag"] == "was planned"
    assert rows[0]["heldCount"] == 0
    assert rows[0]["meanImpl"] == 0.375
    assert [member["slug"] for member in rows[0]["members"]] == ["alpha"]
    assert rows[0]["hours"] == {"total": 3, "left": 0}
    assert rows[0]["closed"] is False
    # The second row keeps its end date and reads two-held, with closure still
    # driven by a shipped derived state or an authored done status.
    assert rows[1]["sprint"]["ends"] == "2026-08-31"
    assert rows[1]["flag"] == "2 held"
    assert rows[1]["heldCount"] == 2
    assert rows[1]["closed"] is False
    assert rows[2]["closed"] is True


def test_sprint_order_falls_back_naturally_and_active_disagreement_is_preserved() -> (
    None
):
    ordered = _evaluate_sprint_helpers(
        'orderedSprints([{id: "S10"}, {id: "S2"}, {id: "S1"}], null).map(row => row.id)'
    )
    conflicts = _evaluate_sprint_helpers(
        '[activeSprintConflict([{id: "S9"}, {id: "S12"}], "S6"), '
        'activeSprintConflict([{id: "S9"}], "S9")]'
    )

    assert ordered == ["S1", "S2", "S10"]
    assert conflicts == [True, False]


def test_sprint_state_table_is_primary_and_uses_composed_rows_without_refetch() -> None:
    source = (Path(__file__).parents[1] / "docs/ui/sprint.jsx").read_text()
    table = source.index('className="r-sprint-state"')
    priority = source.index('className="r-priority-panel"')
    row_derivation = _javascript_function(source, "sprintStateRows")

    assert table < priority
    assert '<table className="r-sprint-table">' in source
    assert "stateRows.map(row =>" in source
    assert "hidden={foldClosed && row.closed}" in source
    assert 'className="r-sprint-conflict"' in source
    # The composed status board is gone: the primary table maps state rows that
    # project derived state, impl fraction and current-work members in place of
    # the retired review metrics block, all without a review fetch.
    assert 'const state = sprint.derived_state || "unknown";' in row_derivation
    assert "Number(sprint.implementation_pct || 0) / 100" in source
    assert "sprintMembers(sprint, inventory)" in source
    assert "sprintInventoryItems" not in row_derivation
    assert "fetch(`/review" not in source


def test_ready_lanes_replace_the_status_board_and_consume_served_readiness() -> None:
    root = Path(__file__).parents[1]
    source = (root / "docs/ui/sprint.jsx").read_text()
    styles = (root / "docs/ui/sprints.css").read_text()
    row_derivation = _javascript_function(source, "readyLaneRows")

    for retired in ("r-sprint-board", "r-kanban", "r-kcard", "renderCard"):
        assert retired not in source
        assert retired not in styles
    assert "M.ready_set" in source
    assert "readySet?.ready || []" in row_derivation
    assert "sectionRow.ready !== false" in row_derivation
    assert "depends_on" not in row_derivation
    assert "decision_blockers" not in row_derivation
    assert "explicit_blockers" not in row_derivation
    assert "rank" not in row_derivation
    assert "r-ready-lane-list" in source
    assert "r-ready-lane-invocation" in source
    assert "r-ready-lane.blocked" in styles
    assert "r-ready-lane-state.in-progress" in styles


def test_ready_lane_rows_keep_section_handles_causes_and_landed_tail() -> None:
    ready_set = {
        "ready": [
            {
                "slug": "partly-open",
                "sprint": "S2",
                "progress_pct": 40,
                "reason": "critical path",
                "unlocks": ["consumer"],
                "section_readiness": [
                    {"section": "s1", "ready": True, "blockers": []},
                    {
                        "section": "s3",
                        "ready": False,
                        "blockers": [{"ref": "foundation#s3"}],
                    },
                ],
            },
            {
                "slug": "finished-measure",
                "sprint": "S1",
                "progress_pct": 100,
                "landed": True,
                "reason": "ready with all hard prerequisites satisfied",
            },
        ]
    }
    sprints = [
        {
            "id": "S2",
            "items": [{"slug": "partly-open", "why_now": "It unlocks the consumer."}],
        },
        {
            "id": "S1",
            "items": [{"slug": "finished-measure", "why_now": "Keep closure visible."}],
        },
    ]
    inventory = [
        {"slug": "partly-open", "title": "Partly open", "status": "active"},
        {
            "slug": "finished-measure",
            "title": "Finished measure",
            "status": "active",
        },
    ]

    rows = _evaluate_sprint_helpers(
        f"readyLaneRows({json.dumps(ready_set)}, {json.dumps(sprints)}, "
        f"{json.dumps(inventory)})"
    )

    assert len(rows) == 3
    assert [row["section"] for row in rows[:2]] == ["s1", "s3"]
    assert rows[0]["whyNow"] == "It unlocks the consumer."
    assert rows[0]["invocation"] == "/reckon-build partly-open §1"
    assert rows[0]["ready"] is True
    assert rows[1]["invocation"] == "/reckon-build partly-open §3"
    assert rows[1]["ready"] is False
    assert rows[1]["causeClasses"] == ["dependency"]
    assert rows[2]["landed"] is True
    assert rows[2]["whyNow"] == "Keep closure visible."
    assert rows[2]["invocation"] == "/reckon-build finished-measure"


def test_browser_harness_reports_an_absent_binary_as_a_clean_skip(
    tmp_path: Path,
) -> None:
    absent = tmp_path / "browser-is-not-installed"
    context = ServedSpa(
        browser=str(absent),
        url="http://127.0.0.1:1/",
        tmp_path=tmp_path,
    )
    result = context.run_composition_probe()

    assert result.returncode == 77
    assert result.stderr.strip() == f"SKIP: browser binary is not present: {absent}"


@pytest.mark.parametrize("viewport_width", VIEWPORT_WIDTHS)
def test_plans_surface_never_exceeds_its_window(
    tmp_path: Path,
    viewport_width: int,
) -> None:
    browser = installed_browser()
    if browser is None:
        pytest.skip(
            "SPA composition check requires an installed browser; tried "
            + ", ".join(BROWSER_NAMES)
        )

    screenshot = tmp_path / f"plans-{viewport_width}.png"
    with (
        file_spa(tmp_path, browser, _composed_state(), route="#plans") as context,
        _skip_when_browser_is_unavailable(),
    ):
        passing = context.run_composition_probe(
            expected_width=viewport_width, screenshot=screenshot
        )

    assert passing.returncode == 0, passing.stderr
    geometry = json.loads(passing.stdout)
    assert geometry["visibleViewCount"] == 1
    assert geometry["document"] == {
        "clientWidth": viewport_width,
        "scrollWidth": viewport_width,
    }
    assert geometry["exceedingElementCount"] == 0
    assert geometry["app"]["width"] == pytest.approx(viewport_width, abs=1)
    assert geometry["view"]["width"] == pytest.approx(geometry["app"]["width"], abs=1)
    assert geometry["view"]["top"] == pytest.approx(geometry["topbar"]["bottom"], abs=1)
    assert geometry["view"]["bottom"] == pytest.approx(geometry["app"]["bottom"], abs=1)
    assert screenshot.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_window_containment_fails_when_a_width_floor_returns(tmp_path: Path) -> None:
    browser = installed_browser()
    if browser is None:
        pytest.skip(
            "SPA composition check requires an installed browser; tried "
            + ", ".join(BROWSER_NAMES)
        )

    with (
        file_spa(tmp_path, browser, _composed_state(), route="#plans") as context,
        _skip_when_browser_is_unavailable(),
    ):
        conflicting = context.run_composition_probe(
            "--conflicting-width",
            "1374",
            expected_width=1280,
            screenshot=tmp_path / "plans-width-floor.png",
        )

    assert conflicting.returncode == 1
    assert "composed container geometry mismatch" in conflicting.stderr
    assert "document scroll width: 1374, client width" in conflicting.stderr
    assert "elements past viewport:" in conflicting.stderr


def test_artifact_tab_returns_an_open_reader_to_the_selected_index(
    tmp_path: Path,
) -> None:
    browser = installed_browser()
    if browser is None:
        pytest.skip(
            "SPA composition check requires an installed browser; tried "
            + ", ".join(BROWSER_NAMES)
        )

    state = _composed_state()
    research = next(
        item for item in state["inventory"] if item.get("type") == "research"
    )
    slug = research.get("nav_key") or research["slug"]
    selected = json.dumps(slug)
    expression = """
(async () => {
  const before = document.querySelector('[data-artifact-kind="research"]')?.dataset.artifactSelection;
  const evidenceTab = [...document.querySelectorAll('.r-tabs-artifact .r-tab')]
    .find(button => button.textContent.trim() === 'Evidence');
  evidenceTab.click();
  await new Promise(resolve => setTimeout(resolve, 100));
  const surface = document.querySelector('[data-artifact-kind="evidence"]');
  return {
    before,
    hash: window.location.hash,
    kind: surface?.dataset.artifactKind,
    selected: surface?.dataset.artifactSelection,
    readingMode: document.querySelector('.r-app')?.classList.contains('r-focus-mode'),
  };
})()
"""
    with (
        file_spa(
            tmp_path,
            browser,
            state,
            route=f"#research/{quote(str(slug), safe='')}",
        ) as context,
        _skip_when_browser_is_unavailable(),
    ):
        result = context.run_probe(
            expression,
            ready_expression=(
                "Boolean(document.querySelector("
                f"'[data-artifact-kind=\"research\"][data-artifact-selection={selected}]'"
                "))"
            ),
        )

    assert result == {
        "before": slug,
        "hash": "#evidence",
        "kind": "evidence",
        "selected": "",
        "readingMode": False,
    }
