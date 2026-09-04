import json
from pathlib import Path

import pytest

from tests.spa_browser_harness import file_spa, installed_browser_or_skip

REPO_ROOT = Path(__file__).parents[1]
PLAN_SOURCE = (REPO_ROOT / "docs" / "ui" / "plan.jsx").read_text()
TITLE_SOURCE = (REPO_ROOT / "docs" / "ui" / "shell-titlebar.jsx").read_text()
BITS_SOURCE = (REPO_ROOT / "docs" / "ui" / "bits.jsx").read_text()


@pytest.fixture(scope="module")
def rendered_browser() -> str:
    return installed_browser_or_skip()


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


def _reader_state() -> dict[str, object]:
    inventory = [
        {
            "slug": "work",
            "title": "Work plan",
            "type": "plan",
            "status": "active",
            "effective_status": "active",
            "sprint": "current",
            "effort_hours": 3.25,
        },
        {
            "slug": "empty",
            "title": "Empty plan",
            "type": "plan",
            "status": "active",
            "effective_status": "active",
        },
        {
            "nav_key": "research:resource-a",
            "slug": "resource-a",
            "href": "research/resource-a",
            "title": "Resource A",
            "type": "research",
            "status": "done",
            "informs": ["work"],
        },
        {
            "nav_key": "research:resource-b",
            "slug": "resource-b",
            "href": "research/resource-b",
            "title": "Resource B",
            "type": "research",
            "status": "done",
            "informs": ["work"],
        },
        {
            "nav_key": "evidence:outcome",
            "slug": "outcome",
            "href": "evidence/outcome",
            "title": "Outcome evidence",
            "type": "evidence",
            "status": "done",
            "evidence_for": ["work"],
        },
        {
            "nav_key": "figure:work/capture.png",
            "slug": "work/capture.png",
            "href": "/reckon/figures/work/capture.png",
            "title": "Capture",
            "type": "figure",
            "status": "done",
            "for_plan": "work",
            "dims": "640 x 480",
        },
    ]
    return {
        "project": "reckon",
        "projects": [{"project": "reckon", "plans_count": len(inventory)}],
        "inventory": inventory,
        "plans": {item.get("nav_key", item["slug"]): item for item in inventory},
        "sprints": [{"id": "current", "status": "active", "items": ["work"]}],
        "milestones": [],
        "north_stars": [],
        "timeline": [],
        "blockers": [],
        "attachment_relations": [
            {"source": "research:resource-a", "target": "work"},
            {"source": "research:resource-b", "target": "work"},
            {"source": "evidence:outcome", "target": "work"},
        ],
    }


def _reader_preload(*, live_runs: list[dict[str, object]] | None = None) -> str:
    runs = json.dumps({"runs": live_runs or []}, separators=(",", ":"))
    return f"""
      const readerNativeFetch = window.fetch.bind(window);
      const readerCrew = {runs};
      window.fetch = (resource, options) => {{
        const url = new URL(String(resource), window.location.href);
        if (url.pathname === '/crew' || url.pathname === '/crew/reckon') {{
          return Promise.resolve(new Response(JSON.stringify(readerCrew), {{
            status: 200,
            headers: {{'Content-Type': 'application/json'}},
          }}));
        }}
        if (url.pathname.startsWith('/plan/reckon/')) {{
          return Promise.resolve(new Response(JSON.stringify({{
            version: 1, decisions: [], comments: {{}}, gates: [], followups: [],
          }}), {{
            status: 200,
            headers: {{'Content-Type': 'application/json'}},
          }}));
        }}
        if (url.pathname.startsWith('/reckon/') && url.pathname.endsWith('.html')) {{
          return Promise.resolve(new Response(
            '<main class="plan-doc"><p class="fixture-body">Rendered body</p></main>',
            {{status: 200, headers: {{'Content-Type': 'text/html'}}}},
          ));
        }}
        return readerNativeFetch(resource, options);
      }};
    """


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
    run = {
        "plan": "work",
        "backend": "codex",
        "role": "worker",
        "member": "runner",
        "section": "reader",
        "elapsed_seconds": 240,
        "time_budget": "25m",
        "phase": "working",
    }
    probe = """(() => {
          const band = document.querySelector('.r-inflight-band');
          const text = band.textContent;
          return {
            clientWidth: band.clientWidth,
            scrollWidth: band.scrollWidth,
            fits: band.scrollWidth <= band.clientWidth,
            hasWorkerHours: text.includes('3.25 worker-hours'),
            hasRuntimeModel: text.includes('gpt-5.6-sol'),
          };
        })()"""
    with file_spa(
        tmp_path,
        rendered_browser,
        _reader_state(),
        route="#plan/work",
    ) as spa:
        measurement = spa.run_probe(
            probe,
            viewport=(1374, 900),
            ready_expression="Boolean(document.querySelector('.r-inflight-band'))",
            preload_expression=_reader_preload(live_runs=[run]),
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


def test_figure_reader_uses_the_served_image_and_prints_its_source_path(
    tmp_path, rendered_browser
):
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

    with file_spa(
        tmp_path,
        rendered_browser,
        _reader_state(),
        route="#figure/work%2Fcapture.png",
    ) as spa:
        measurement = spa.run_probe(
            """(() => {
              const trail = document.querySelector('.r-reading-trail');
              const segments = [...trail.querySelectorAll('.r-reading-trail-link, .r-reading-trail-current')];
              const image = document.querySelector('.r-reader-figure img');
              return {
                trail: segments.map(segment => segment.textContent.trim()),
                navigatingSegments: trail.querySelectorAll('button.r-reading-trail-link').length,
                imageSource: image.getAttribute('src'),
                sourcePath: document.querySelector('.r-reader-figure figcaption')?.textContent.trim(),
              };
            })()""",
            ready_expression="Boolean(document.querySelector('.r-reader-figure img'))",
        )

    assert measurement == {
        "trail": ["current", "Work plan", "Capture"],
        "navigatingSegments": 2,
        "imageSource": "/reckon/figures/work/capture.png",
        "sourcePath": "docs/figures/work/capture.png",
    }


def test_reader_head_attachment_bars_render_typed_groups_and_route_entries(
    tmp_path, rendered_browser
):
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
      const planTrail = [...document.querySelectorAll('.r-reading-trail-link, .r-reading-trail-current')]
        .map(segment => segment.textContent.trim());
      const planNavigatingSegments = document.querySelectorAll('button.r-reading-trail-link').length;
      bars[0].open = true;
      bars[0].querySelector('button')?.click();
      const attachmentOpened = await waitFor(() =>
        document.querySelector('.r-reading')?.dataset.readerKind === 'research'
        && document.querySelector('.r-reading-trail-current')?.textContent.trim() === 'Resource A'
      );
      const activatedRoute = location.hash;
      const researchTrail = [...document.querySelectorAll('.r-reading-trail-link, .r-reading-trail-current')]
        .map(segment => segment.textContent.trim());
      const researchNavigatingSegments = document.querySelectorAll('button.r-reading-trail-link').length;
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
        planTrail,
        planNavigatingSegments,
        attachmentOpened,
        activatedRoute,
        researchTrail,
        researchNavigatingSegments,
        emptyOpened,
        emptyBars: document.querySelectorAll('.r-reader-attachment-bar').length,
      };
    })()"""
    with file_spa(
        tmp_path,
        rendered_browser,
        _reader_state(),
        route="#plan/work",
    ) as spa:
        measurement = spa.run_probe(
            probe,
            ready_expression=(
                "document.querySelectorAll('.r-reader-attachment-bar').length === 2 "
                "&& Boolean(document.querySelector('.r-plan-html'))"
            ),
            preload_expression=_reader_preload(),
        )

    assert measurement == {
        "populatedBars": 2,
        "labels": ["Resources", "Evidence"],
        "counts": [2, 1],
        "railCount": 0,
        "barsBeforeBody": True,
        "planTrail": ["current", "Work plan"],
        "planNavigatingSegments": 1,
        "attachmentOpened": True,
        "activatedRoute": "#research/resource-a",
        "researchTrail": ["current", "Work plan", "Resource A"],
        "researchNavigatingSegments": 2,
        "emptyOpened": True,
        "emptyBars": 0,
    }
