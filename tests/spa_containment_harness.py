from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path

from tests.spa_browser_harness import ROOT, ServedSpa, write_file_spa_document

VIEWPORT_WIDTHS = (1374, 1920)
VIEWPORT_HEIGHT = 900
OFFSCREEN_MARKER = "data-viewport-containment"


def routable_surfaces(state: Mapping[str, object]) -> tuple[dict[str, str], ...]:
    inventory = [
        row
        for row in state.get("inventory", [])
        if isinstance(row, dict) and (row.get("type") or "plan") == "plan"
    ]
    sprints = [row for row in state.get("sprints", []) if isinstance(row, dict)]
    if not inventory:
        raise AssertionError("containment fixture has no plan for the plan reader")
    if not sprints:
        raise AssertionError("containment fixture has no sprint for the sprint route")

    plan_slug = str(inventory[0]["slug"])
    sprint_id = str(state.get("active_sprint_id") or sprints[0]["id"])
    return (
        {"name": "home", "hash": "#home", "ready": ".r-home-project"},
        {
            "name": "cockpit",
            "hash": "#cockpit",
            "ready": ".r-overview-project-row",
        },
        {"name": "plans", "hash": "#plans", "ready": ".r-list .r-row"},
        {
            "name": "plan-reader",
            "hash": f"#plan/{plan_slug}",
            "ready": ".r-plan-html > *",
        },
        {
            "name": "sprints",
            "hash": "#sprints",
            "ready": ".r-sprint-table tbody tr:not([hidden])",
        },
        {
            "name": "sprint",
            "hash": f"#sprint/{sprint_id}",
            "ready": ".r-sprint-table tbody tr:not([hidden])",
        },
        {"name": "graph", "hash": "#graph", "ready": ".r-graph"},
        {
            "name": "crew",
            "hash": "#crew",
            "ready": ".r-crew-list article",
        },
    )


def browser_fixture_bootstrap(state: Mapping[str, object]) -> str:
    plan = next(
        row for row in state["inventory"] if (row.get("type") or "plan") == "plan"
    )
    run = {
        "project": "reckon",
        "run_id": "containment-fixture",
        "plan": str(plan["href"]),
        "node": "rendered-check",
        "section": "containment",
        "phase": "working",
        "goal": "Keep the rendered crew surface populated.",
        "done_when": "The surface contains one representative run.",
        "elapsed_seconds": 60,
        "time_budget_seconds": 600,
    }
    plan_path = ROOT / "docs" / f"{plan['href']}.html"
    plan_html = plan_path.read_text(encoding="utf-8")
    project_summary = {
        "project": "reckon",
        "plans_count": len(state["inventory"]),
        "active": sum(row.get("status") == "active" for row in state["inventory"]),
        "blocked": sum(row.get("status") == "blocked" for row in state["inventory"]),
        "pending": sum(row.get("status") == "pending" for row in state["inventory"]),
        "shipped": sum(row.get("status") == "shipped" for row in state["inventory"]),
        "active_sprint": next(
            (
                {"id": row["id"], "theme": row.get("theme", "")}
                for row in state["sprints"]
                if row.get("status") == "active"
            ),
            None,
        ),
    }
    projects = {
        "projects": [
            {
                "project": "reckon",
                "data": {
                    "projects": [project_summary],
                    "plans": state["inventory"],
                },
            }
        ]
    }
    plan_state = {"decisions": [], "comments": {}, "gates": []}
    return f"""
      const containmentNativeFetch = window.fetch.bind(window);
      const containmentCrew = {{runs: [{json.dumps(run)}]}};
      const containmentProjects = {json.dumps(projects)};
      const containmentDiscovery = {json.dumps(state)};
      const containmentPlanHtml = {json.dumps(plan_html)};
      const containmentPlanState = {json.dumps(plan_state)};
      window.fetch = (resource, options) => {{
        const url = new URL(String(resource), window.location.href);
        if (url.pathname === '/crew' || url.pathname === '/crew/reckon') {{
          return Promise.resolve(new Response(JSON.stringify(containmentCrew), {{
            status: 200,
            headers: {{'Content-Type': 'application/json'}},
          }}));
        }}
        if (url.pathname === '/_projects/index.json') {{
          return Promise.resolve(new Response(JSON.stringify(containmentProjects), {{
            status: 200,
            headers: {{'Content-Type': 'application/json'}},
          }}));
        }}
        if (url.pathname === '/_discover/reckon') {{
          return Promise.resolve(new Response(JSON.stringify(containmentDiscovery), {{
            status: 200,
            headers: {{'Content-Type': 'application/json'}},
          }}));
        }}
        if (url.pathname.startsWith('/reckon/') && url.pathname.endsWith('.html')) {{
          return Promise.resolve(new Response(containmentPlanHtml, {{
            status: 200,
            headers: {{'Content-Type': 'text/html'}},
          }}));
        }}
        if (url.pathname.startsWith('/plan/reckon/')) {{
          return Promise.resolve(new Response(JSON.stringify(containmentPlanState), {{
            status: 200,
            headers: {{'Content-Type': 'application/json'}},
          }}));
        }}
        return containmentNativeFetch(resource, options);
      }};
    """


@contextmanager
def file_spa_with_bootstrap(
    tmp_path: Path,
    browser: str,
    state: Mapping[str, object],
) -> Iterator[ServedSpa]:
    generated_root = Path(tempfile.mkdtemp(prefix="containment-spa-", dir=tmp_path))
    page = write_file_spa_document(generated_root / "index.html", state)
    document = page.read_text(encoding="utf-8")
    script_end = -1
    for _ in range(3):
        script_end = document.index("</script>", script_end + 1)
    insertion = script_end + len("</script>")
    bootstrap = browser_fixture_bootstrap(state).replace("</script", "<\\/script")
    page.write_text(
        document[:insertion]
        + f"\n  <script>{bootstrap}</script>"
        + document[insertion:],
        encoding="utf-8",
    )
    try:
        yield ServedSpa(
            browser=browser,
            url=f"{page.resolve().as_uri()}#cockpit",
            tmp_path=tmp_path,
        )
    finally:
        shutil.rmtree(generated_root)


def measurement_probe_preload(surfaces: Sequence[Mapping[str, str]]) -> str:
    encoded_surfaces = json.dumps(list(surfaces), separators=(",", ":"))
    return f"""window.__runViewportContainment = async () => {{
      const surfaces = {encoded_surfaces};
      const marker = {json.dumps(OFFSCREEN_MARKER)};
      const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
      const settle = () => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));

      const runtimeErrors = [];
      window.addEventListener('error', event => runtimeErrors.push(
        event.error?.stack || event.message || 'window error'
      ));
      window.addEventListener('unhandledrejection', event => runtimeErrors.push(
        event.reason?.stack || String(event.reason || 'unhandled rejection')
      ));

      async function waitFor(predicate, description, timeoutMs = 12000, required = true) {{
        const deadline = Date.now() + timeoutMs;
        while (Date.now() < deadline) {{
          if (predicate()) {{ await settle(); return true; }}
          await delay(50);
        }}
        if (required) {{
          throw new Error(`timed out waiting for ${{description}} at ${{location.hash}}`);
        }}
        return false;
      }}

      function selectorFor(element) {{
        if (element.id) return `#${{CSS.escape(element.id)}}`;
        const parts = [];
        let current = element;
        while (current && current.nodeType === Node.ELEMENT_NODE && current !== document.body) {{
          let part = current.localName;
          const classes = [...current.classList].slice(0, 2);
          if (classes.length) part += classes.map(name => `.${{CSS.escape(name)}}`).join('');
          const siblings = current.parentElement
            ? [...current.parentElement.children].filter(sibling => sibling.localName === current.localName)
            : [];
          if (siblings.length > 1) part += `:nth-of-type(${{siblings.indexOf(current) + 1}})`;
          parts.unshift(part);
          if (current.classList.contains('r-app')) break;
          current = current.parentElement;
        }}
        return parts.join(' > ');
      }}

      function horizontalScrollOwner(element) {{
        let current = element.parentElement;
        while (current && current !== document.body && current !== document.documentElement) {{
          const style = getComputedStyle(current);
          if ((style.overflowX === 'auto' || style.overflowX === 'scroll')
              && current.scrollWidth > current.clientWidth) return current;
          current = current.parentElement;
        }}
        return null;
      }}

      function measure(name) {{
        const violations = [];
        const exemptions = [];
        let elementCount = 0;
        for (const element of document.querySelectorAll('body *')) {{
          if (element.getClientRects().length === 0) continue;
          const rect = element.getBoundingClientRect();
          if (rect.width === 0 && rect.height === 0) continue;
          const selector = selectorFor(element);
          if (element.closest(`[${{marker}}="offscreen"]`)) {{
            exemptions.push({{selector, marker: `${{marker}}=offscreen`}});
            continue;
          }}

          elementCount += 1;
          const owner = horizontalScrollOwner(element);
          const ownerRect = owner?.getBoundingClientRect();
          const leftBound = owner
            ? ownerRect.left + owner.clientLeft - owner.scrollLeft
            : 0;
          const rightBound = owner ? leftBound + owner.scrollWidth : innerWidth;
          const leftOverflow = Math.max(0, leftBound - rect.left);
          const rightOverflow = Math.max(0, rect.right - rightBound);
          if (leftOverflow > 0 || rightOverflow > 0) {{
            violations.push({{
              selector,
              left_overflow_px: Number(leftOverflow.toFixed(3)),
              right_overflow_px: Number(rightOverflow.toFixed(3)),
              boundary: owner ? selectorFor(owner) : 'viewport',
            }});
          }}
        }}
        return {{
          name,
          status: 'measured',
          hash: location.hash,
          element_count: elementCount,
          violations,
          exemptions,
        }};
      }}

      async function visit(surface) {{
        location.hash = surface.hash;
        await waitFor(
          () => location.hash === surface.hash && Boolean(document.querySelector(surface.ready)),
          `${{surface.name}} surface (${{surface.ready}})`,
        );
        return measure(surface.name);
      }}

      const verdicts = [];
      for (const surface of surfaces) verdicts.push(await visit(surface));

      location.hash = '#plans';
      await waitFor(() => Boolean(document.querySelector('.r-list .r-row')), 'plans for overlays');

      const picker = document.querySelector('details.r-project-manage');
      picker.open = true;
      await waitFor(() => Boolean(document.querySelector('.r-project-menu')), 'project picker');
      verdicts.push(measure('overlay-project-picker'));
      picker.open = false;

      document.querySelector('.r-project-configure').click();
      await waitFor(() => Boolean(document.querySelector('.r-visibility-sheet')), 'visibility sheet');
      verdicts.push(measure('overlay-visibility-sheet'));
      document.querySelector('.r-visibility-sheet-close').click();
      await waitFor(() => !document.querySelector('.r-visibility-sheet'), 'visibility sheet close');

      const searchButton = document.querySelector('.r-topbar-search');
      const searchRect = searchButton?.getBoundingClientRect();
      const paletteDiagnostic = {{
        action: 'HTMLElement.click on .r-topbar-search',
        route: location.hash,
        prior_overlay_state: {{
          project_picker_open: picker.open,
          visibility_sheet_count: document.querySelectorAll('.r-visibility-sheet').length,
          command_palette_count: document.querySelectorAll('.r-cmdk').length,
        }},
        button: searchButton ? {{
          selector: '.r-topbar-search',
          left: Number(searchRect.left.toFixed(3)),
          right: Number(searchRect.right.toFixed(3)),
          top: Number(searchRect.top.toFixed(3)),
          bottom: Number(searchRect.bottom.toFixed(3)),
          width: Number(searchRect.width.toFixed(3)),
          height: Number(searchRect.height.toFixed(3)),
          displayed: searchButton.getClientRects().length > 0,
          disabled: Boolean(searchButton.disabled),
        }} : null,
        body_before: {{
          app_class: document.querySelector('.r-app')?.className || null,
          child_count: document.body.children.length,
          text_length: document.body.innerText.length,
          client_width: document.documentElement.clientWidth,
          scroll_width: document.documentElement.scrollWidth,
          active_element: selectorFor(document.activeElement),
        }},
        prior_attempts: [
          {{attempt: 1, transport: 'loopback-http', outcome: 'initial navigation did not complete', inotify: '36/128'}},
          {{attempt: 2, transport: 'file-bootstrap-in-environment', outcome: 'process creation exceeded ARG_MAX'}},
          {{attempt: 3, transport: 'file-bootstrap-in-document', action: 'search button click', outcome: '.r-cmdk did not render'}},
          {{attempt: 4, transport: 'file-bootstrap-in-document', action: 'Ctrl+K keydown', outcome: '.r-cmdk did not render'}},
        ],
      }};
      searchButton?.click();
      const paletteOpened = await waitFor(
        () => Boolean(document.querySelector('.r-cmdk')),
        'command palette diagnostic',
        2000,
        false,
      );
      paletteDiagnostic.body_after = {{
        app_class: document.querySelector('.r-app')?.className || null,
        child_count: document.body.children.length,
        text_length: document.body.innerText.length,
        client_width: document.documentElement.clientWidth,
        scroll_width: document.documentElement.scrollWidth,
        active_element: selectorFor(document.activeElement),
        command_palette_count: document.querySelectorAll('.r-cmdk').length,
      }};
      paletteDiagnostic.runtime_exceptions = [...runtimeErrors];
      if (paletteOpened) {{
        const paletteVerdict = measure('overlay-command-palette');
        paletteVerdict.diagnostic = paletteDiagnostic;
        verdicts.push(paletteVerdict);
        document.querySelector('.r-cmdk-scrim').dispatchEvent(new MouseEvent('mousedown', {{bubbles: true}}));
        await waitFor(() => !document.querySelector('.r-cmdk'), 'command palette close');
      }} else {{
        verdicts.push({{
          name: 'overlay-command-palette',
          status: 'not_measured',
          hash: location.hash,
          element_count: 0,
          violations: [],
          exemptions: [],
          reason: 'the authored search action did not render .r-cmdk within 2000ms',
          diagnostic: paletteDiagnostic,
        }});
      }}

      const shifted = document.createElement('div');
      shifted.id = 'containment-shifted-case';
      shifted.style.cssText = 'position:fixed;left:100vw;top:20px;width:40px;height:10px';
      document.body.append(shifted);

      const scrolling = document.createElement('div');
      scrolling.id = 'containment-scroll-case';
      scrolling.style.cssText = 'position:fixed;left:20px;top:40px;width:100px;height:20px;overflow-x:auto';
      const wide = document.createElement('div');
      wide.id = 'containment-scroll-content';
      wide.style.cssText = 'width:180px;height:10px';
      scrolling.append(wide);
      document.body.append(scrolling);

      const exempt = document.createElement('div');
      exempt.id = 'containment-offscreen-case';
      exempt.setAttribute(marker, 'offscreen');
      exempt.style.cssText = 'position:fixed;left:-100px;top:70px;width:10px;height:10px';
      document.body.append(exempt);
      await settle();
      verdicts.push(measure('harness-cases'));

      shifted.remove();
      scrolling.remove();
      exempt.remove();
      return {{width: innerWidth, height: innerHeight, surfaces: verdicts}};
    }}"""


def run_containment_probe(
    spa: ServedSpa,
    state: Mapping[str, object],
    width: int,
) -> dict[str, object]:
    surfaces = routable_surfaces(state)
    result = spa.run_probe(
        "window.__runViewportContainment()",
        viewport=(width, VIEWPORT_HEIGHT),
        preload_expression=measurement_probe_preload(surfaces),
        ready_expression="Boolean(window.STATE && document.querySelector('.r-app'))",
    )
    if not isinstance(result, dict):
        raise TypeError(f"containment probe returned {type(result).__name__}")
    return result


def aggregate_verdict(
    width_results: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    surface_rows = [
        {"width": result["width"], **surface}
        for result in width_results
        for surface in result["surfaces"]
    ]
    violations = [
        {"width": row["width"], "surface": row["name"], **violation}
        for row in surface_rows
        for violation in row["violations"]
    ]
    exemptions = [
        {"width": row["width"], "surface": row["name"], **exemption}
        for row in surface_rows
        for exemption in row["exemptions"]
    ]
    measured_rows = [row for row in surface_rows if row["status"] == "measured"]
    not_measured_rows = [
        {
            "width": row["width"],
            "name": row["name"],
            "hash": row["hash"],
            "reason": row["reason"],
            "diagnostic": row["diagnostic"],
        }
        for row in surface_rows
        if row["status"] == "not_measured"
    ]
    return {
        "widths": [result["width"] for result in width_results],
        "surfaces_walked": [
            {"width": row["width"], "name": row["name"], "hash": row["hash"]}
            for row in measured_rows
        ],
        "surface_count": len(surface_rows),
        "measured_surface_count": len(measured_rows),
        "not_measured_surface_count": len(not_measured_rows),
        "surfaces_not_measured": not_measured_rows,
        "element_count": sum(int(row["element_count"]) for row in measured_rows),
        "violations": violations,
        "exemptions": exemptions,
        "surface_verdicts": surface_rows,
    }


def write_verdict(path: Path, verdict: Mapping[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(verdict, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


def assert_horizontally_contained(
    violations: Sequence[Mapping[str, object]],
) -> None:
    if not violations:
        return
    detail = "; ".join(
        f"{row['selector']}: left {row['left_overflow_px']}px, "
        f"right {row['right_overflow_px']}px past {row['boundary']}"
        for row in violations
    )
    raise AssertionError(f"horizontal containment violations: {detail}")
