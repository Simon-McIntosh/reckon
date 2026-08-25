import json
from pathlib import Path

import pytest

from tests.spa_browser_harness import (
    installed_browser,
    run_browser_probe,
    served_spa,
)

REPO_ROOT = Path(__file__).parents[1]
PLAN_SOURCE = (REPO_ROOT / "docs" / "ui" / "plan.jsx").read_text()
BITS_SOURCE = (REPO_ROOT / "docs" / "ui" / "bits.jsx").read_text()


def _between(source: str, start: str, end: str) -> str:
    return source.split(start, 1)[1].split(end, 1)[0]


def test_inflight_band_identifies_work_without_rendering_a_run_hash():
    band = _between(
        PLAN_SOURCE, "function PlanInFlightBand", "function ReaderSourceFailure"
    )

    assert "<code>{run.run_id" not in band
    assert "run.run_id ||" not in band
    assert "run.backend || run.harness" in band
    assert "run.model" not in band
    assert "run.effort" not in band
    assert 'run.role || "worker"' in band
    assert "worker-hours" in band
    assert "effortHours={P.effort_hours}" in PLAN_SOURCE
    assert 'run.member || "unassigned"' in band
    assert 'run.section || "whole plan"' in band
    assert "elapsed_seconds" in band
    assert "time_budget" in band
    assert "r-inflight-track" in band
    assert "run.phase" in band
    assert band.count("Copy run command") == 1
    assert "reckon crew observe --run ${run.run_id}" in band


def test_inflight_band_fits_reader_at_minimum_width(tmp_path):
    browser = installed_browser()
    if browser is None:
        pytest.skip("browser-backed geometry check requires an installed browser")

    reader_css = (REPO_ROOT / "docs" / "ui" / "reader.css").read_text()
    document = f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
* {{ box-sizing: border-box; }}
:root {{ --line: #ddd; --line-2: #ccc; --border: #ccc; --accent: #2867c7; --muted: #666; }}
body {{ margin: 0; width: 1374px; font: 12px Arial, sans-serif; }}
.workspace {{ display: flex; width: 1374px; }}
.rail {{ flex: 0 0 178px; }}
.list {{ flex: 0 0 390px; }}
.r-reading {{ flex: 1; min-width: 0; padding: 22px 26px 34px; }}
.attachments {{ flex: 0 0 300px; }}
.r-inflight-band {{ margin-bottom: 18px; padding: 12px 14px; border: 1px solid var(--border); font-size: 12px; }}
.r-inflight-heading {{ margin-bottom: 8px; }}
.r-inflight-runs {{ display: grid; gap: 9px; margin: 0; padding: 0; list-style: none; }}
.r-inflight-identity {{ display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 10px; }}
.r-inflight-progress {{ display: grid; gap: 4px; font-size: 10px; }}
.r-inflight-action {{ white-space: nowrap; }}
{reader_css}
</style></head><body>
<div class="workspace"><div class="rail"></div><div class="list"></div><main class="r-reading">
  <aside class="r-inflight-band"><div class="r-inflight-heading">In flight</div>
    <ul class="r-inflight-runs"><li class="r-inflight-run">
      <div class="r-inflight-identity"><strong>codex</strong><span>worker · 3.25 worker-hours</span><span>@runner</span><span>reader</span></div>
      <div class="r-inflight-progress"><span>4m / 25m</span><span class="r-inflight-track"></span></div>
      <span class="r-inflight-phase">working</span><button class="r-inflight-action">Copy run command</button>
    </li></ul>
  </aside>
</main><div class="attachments"></div></div>
 </body></html>"""
    measurement = run_browser_probe(
        tmp_path,
        browser,
        document,
        """(() => {
          const band = document.querySelector('.r-inflight-band');
          const text = band.textContent;
          return {
            clientWidth: band.clientWidth,
            scrollWidth: band.scrollWidth,
            fits: band.scrollWidth <= band.clientWidth,
            hasWorkerHours: text.includes('3.25 worker-hours'),
            hasRuntimeModel: text.includes('gpt-5.6-sol'),
          };
        })()""",
        ready_expression="Boolean(document.querySelector('.r-inflight-band'))",
    )
    assert measurement["scrollWidth"] <= measurement["clientWidth"]
    assert measurement["fits"] is True
    assert measurement["hasWorkerHours"] is True
    assert measurement["hasRuntimeModel"] is False


def test_authored_html_failure_renders_status_missing_sections_and_retry():
    assert 'source="authored plan HTML"' in PLAN_SOURCE
    assert "status={htmlFailure.status}" in PLAN_SOURCE
    assert (
        'missing={["authored body", "authored followups", "authored comments"]}'
        in PLAN_SOURCE
    )
    assert "if (!r.ok) throw { responseStatus: `HTTP ${r.status}` };" in PLAN_SOURCE
    assert (
        'setHtmlFailure({ status: error?.responseStatus || "network error" })'
        in PLAN_SOURCE
    )
    assert "setHtmlRetry(value => value + 1)" in PLAN_SOURCE


def test_structured_state_failure_renders_status_missing_sections_and_retry():
    assert 'source="structured plan state"' in PLAN_SOURCE
    assert "status={stateFailure.status}" in PLAN_SOURCE
    assert (
        'missing={["interactive decisions", "evidence gates", "structured followups", "loaded plan version"]}'
        in PLAN_SOURCE
    )
    assert (
        'setStateFailure({ status: error?.responseStatus || "network error" })'
        in PLAN_SOURCE
    )
    assert "setStateRetry(value => value + 1)" in PLAN_SOURCE
    assert "Status: {status}. Missing: {missing.join" in PLAN_SOURCE
    assert ">Retry</button>" in PLAN_SOURCE


def test_copied_handoff_carries_live_source_provenance_and_plan_version():
    provenance = _between(
        BITS_SOURCE, "function withHandoffProvenance", "function PromptModal"
    )

    assert "Built from live plan HTML and project discovery." in provenance
    assert "Loaded plan version: ${version}" in provenance
    assert "withHandoffProvenance(" in PLAN_SOURCE
    assert "fullState?.version ?? P.version" in PLAN_SOURCE
    assert "planVersion={loadedPlanVersion}" in PLAN_SOURCE
    assert "built from live plan HTML + project discovery" in BITS_SOURCE


def test_absent_reader_values_are_omitted_instead_of_rendered_as_placeholders():
    controls = _between(
        PLAN_SOURCE,
        '<nav className="r-reading-controls"',
        '<div className="r-reading-viewport">',
    )
    focus_title = _between(PLAN_SOURCE, "{focusMode && (", "<h1>")

    assert '{project && <span className="r-reading-project">' in controls
    assert "{P.sprint && <span>" in controls
    assert "{implementationLabel && <span>" in controls
    assert 'P.impl !== ""' in PLAN_SOURCE
    assert '.filter(Boolean).join(" · ")' in focus_title
    assert 'P.sprint || "unscheduled"' not in focus_title
    assert 'val.choice || "—"' not in PLAN_SOURCE
    assert ': "—"' not in PLAN_SOURCE


def test_reader_head_attachment_bars_render_typed_groups_and_route_entries(tmp_path):
    browser = installed_browser()
    if browser is None:
        pytest.skip("browser-backed reader check requires an installed browser")

    docs = tmp_path / "docs"
    plans = docs / "plans"
    research = docs / "research"
    evidence = docs / "evidence"
    state = docs / "state" / "reckon"
    for directory in (plans, research, evidence, state):
        directory.mkdir(parents=True, exist_ok=True)
    (docs / "index.html").symlink_to(REPO_ROOT / "docs" / "index.html")
    (state / "index.json").write_text(
        json.dumps(
            {
                "project": "reckon",
                "data": {
                    "active_sprint_id": None,
                    "sprints": [],
                    "milestones": [],
                    "blockers": [],
                    "timeline": [],
                },
            }
        ),
        encoding="utf-8",
    )

    def write_resource(
        root: Path,
        slug: str,
        title: str,
        resource_type: str,
        relation: tuple[str, str] | None = None,
    ) -> None:
        relation_meta = (
            f'<meta name="plan-{relation[0]}" content="{relation[1]}">'
            if relation
            else ""
        )
        (root / f"{slug}.html").write_text(
            f"""<!doctype html><html><head>
<meta name="docs-project" content="reckon">
<meta name="reckon-type" content="{resource_type}">
<meta name="plan-slug" content="{slug}">
<meta name="plan-title" content="{title}">
<meta name="plan-summary" content="{title} summary">
<meta name="plan-status" content="active">
<meta name="plan-version" content="1">
{relation_meta}
</head><body><main class="plan-doc"><h2>{title}</h2>
<p class="fixture-body fixture-{slug}">{title} body</p></main></body></html>""",
            encoding="utf-8",
        )

    write_resource(plans, "work", "Work plan", "plan")
    write_resource(plans, "empty", "Empty plan", "plan")
    write_resource(
        research,
        "resource-a",
        "Resource A",
        "research",
        ("informs", "work"),
    )
    write_resource(
        research,
        "resource-b",
        "Resource B",
        "research",
        ("informs", "work"),
    )
    write_resource(
        evidence,
        "outcome",
        "Outcome evidence",
        "evidence",
        ("evidence-for", "work"),
    )

    probe = """(async () => {
      const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
      const waitFor = async predicate => {
        const deadline = Date.now() + 5000;
        while (Date.now() < deadline) {
          if (predicate()) return true;
          await delay(50);
        }
        return false;
      };
      const bars = [...document.querySelectorAll('.r-reader-attachment-bar')];
      const labels = bars.map(bar => bar.querySelector('summary span')?.textContent.trim());
      const counts = bars.map(bar => Number(bar.querySelector('.r-reader-attachment-count')?.textContent));
      const body = document.querySelector('.r-plan-html');
      const barsBeforeBody = bars.every(bar => Boolean(
        bar.compareDocumentPosition(body) & Node.DOCUMENT_POSITION_FOLLOWING
      ));
      bars[0].open = true;
      bars[0].querySelector('button')?.click();
      const attachmentOpened = await waitFor(() =>
        document.querySelector('.r-reading-path')?.textContent.trim() === '/research:resource-a'
      );
      const activatedRoute = document.querySelector('.r-reading-path')?.textContent.trim() || '';
      location.hash = '#plan/empty';
      const emptyOpened = await waitFor(() =>
        document.querySelector('.r-reading-path')?.textContent.trim() === '/empty'
        && document.querySelectorAll('.r-reader-attachment-bar').length === 0
      );
      return {
        populatedBars: bars.length,
        labels,
        counts,
        railCount: document.querySelectorAll('.r-attachment-rail').length,
        barsBeforeBody,
        attachmentOpened,
        activatedRoute,
        emptyOpened,
        emptyBars: document.querySelectorAll('.r-reader-attachment-bar').length,
      };
    })()"""
    with served_spa(
        tmp_path,
        browser,
        docs=docs,
        route="#plan/work",
    ) as spa:
        measurement = spa.run_probe(
            probe,
            ready_expression=(
                "document.querySelectorAll('.r-reader-attachment-bar').length === 2 "
                "&& Boolean(document.querySelector('.r-plan-html'))"
            ),
        )

    assert measurement == {
        "populatedBars": 2,
        "labels": ["Resources", "Evidence"],
        "counts": [2, 1],
        "railCount": 0,
        "barsBeforeBody": True,
        "attachmentOpened": True,
        "activatedRoute": "/research:resource-a",
        "emptyOpened": True,
        "emptyBars": 0,
    }
