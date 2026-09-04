import json
from pathlib import Path

import pytest

from tests.spa_browser_harness import (
    BrowserProbeError,
    installed_browser,
    run_browser_probe,
    served_spa,
)

REPO_ROOT = Path(__file__).parents[1]
PLAN_SOURCE = (REPO_ROOT / "docs" / "ui" / "plan.jsx").read_text()
TITLE_SOURCE = (REPO_ROOT / "docs" / "ui" / "shell-titlebar.jsx").read_text()
BITS_SOURCE = (REPO_ROOT / "docs" / "ui" / "bits.jsx").read_text()


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


def _between(source: str, start: str, end: str) -> str:
    return source.split(start, 1)[1].split(end, 1)[0]


def _function_source(source: str, name: str) -> str:
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


def _evaluate_helpers(source: str, functions: list[str], expression: str):
    import subprocess

    script = "\n".join(_function_source(source, name) for name in functions)
    result = subprocess.run(
        ["node", "-e", f"{script}\nconsole.log(JSON.stringify({expression}));"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _evaluate_title_helpers(functions: list[str], expression: str):
    return _evaluate_helpers(TITLE_SOURCE, functions, expression)


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


def test_inflight_band_fits_reader_at_minimum_width(tmp_path, rendered_browser):
    browser = rendered_browser

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
    metadata = _function_source(TITLE_SOURCE, "readerMetadataRows")

    assert "rows.filter" in metadata
    assert 'String(value).trim() !== ""' in metadata
    assert 'item.sprint || "unscheduled"' not in metadata
    assert 'val.choice || "—"' not in PLAN_SOURCE
    assert ': "—"' not in PLAN_SOURCE


def test_reader_source_trail_has_one_navigation_authority_for_all_kinds():
    state = {
        "inventory": [
            {
                "slug": "work",
                "type": "plan",
                "title": "Work plan",
                "sprint": "current",
            }
        ]
    }
    functions = [
        "canonicalReaderKind",
        "readerReferenceSlug",
        "readerPlanFor",
        "readerSourceTrail",
    ]
    plan = _evaluate_title_helpers(
        functions,
        f"readerSourceTrail({json.dumps(state['inventory'][0])}, {json.dumps(state)})",
    )
    research = _evaluate_title_helpers(
        functions,
        f"readerSourceTrail({json.dumps({'slug': 'study', 'type': 'research', 'title': 'Study', 'informs': ['work']})}, {json.dumps(state)})",
    )
    figure = _evaluate_title_helpers(
        functions,
        f"readerSourceTrail({json.dumps({'slug': 'work/capture.png', 'type': 'figure', 'title': 'Capture', 'for_plan': 'work'})}, {json.dumps(state)})",
    )

    assert [segment["label"] for segment in plan] == ["current", "Work plan"]
    assert [segment["navigates"] for segment in plan] == [True, False]
    assert [segment["label"] for segment in research] == [
        "current",
        "Work plan",
        "Study",
    ]
    assert [segment["navigates"] for segment in research] == [True, True, False]
    assert [segment["label"] for segment in figure] == [
        "current",
        "Work plan",
        "Capture",
    ]
    assert [segment["view"] for segment in figure[:2]] == ["sprint", "plan"]


def test_figure_reader_uses_the_served_image_and_prints_its_source_path():
    functions = ["readerFigureSource", "readerFigurePath"]
    item = {
        "slug": "work/capture.png",
        "type": "figure",
        "href": "/reckon/figures/work/capture.png",
    }
    source, path = _evaluate_helpers(
        PLAN_SOURCE,
        functions,
        f"[readerFigureSource({json.dumps(item)}, 'reckon'), readerFigurePath({json.dumps(item)})]",
    )
    figure = _between(
        PLAN_SOURCE,
        '<figure className="r-reader-figure">',
        "</figure>",
    )

    assert source == "/reckon/figures/work/capture.png"
    assert path == "docs/figures/work/capture.png"
    assert "<img src={readerFigureSource(PG, project)}" in figure
    assert "{readerFigurePath(PG)}" in figure


def test_reader_head_attachment_bars_render_typed_groups_and_route_entries(
    tmp_path, rendered_browser
):
    browser = rendered_browser

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
        document.querySelector('.r-reading')?.dataset.readerKind === 'research'
        && document.querySelector('.r-reading-trail-current')?.textContent.trim() === 'Resource A'
      );
      const activatedRoute = location.hash;
      location.hash = '#plan/empty';
      const emptyOpened = await waitFor(() =>
        document.querySelector('.r-reading-trail-current')?.textContent.trim() === 'Empty plan'
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
        "activatedRoute": "#research/research%3Aresource-a",
        "emptyOpened": True,
        "emptyBars": 0,
    }
