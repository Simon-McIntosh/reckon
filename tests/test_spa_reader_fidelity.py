from __future__ import annotations

import json
import re
from pathlib import Path

from tests.spa_browser_harness import file_spa, installed_browser_or_skip

ROOT = Path(__file__).resolve().parents[1]
CANVAS = (ROOT / "design" / "reckon-spa-handoff.dc.html").read_text()
VIEWPORT = (1374, 900)


def _canvas_contract() -> dict[str, object]:
    toggle = re.search(
        r"readGraphBtnStyle:.*?: `(?P<style>display:flex[^`]+)`",
        CANVAS,
        re.DOTALL,
    )
    cards = re.search(
        r"const coneCard = p => \(\{(?P<body>.*?)\n\s*\}\);", CANVAS, re.DOTALL
    )
    assert toggle and cards
    toggle_style = toggle.group("style")
    card_style = cards.group("body")
    padding = re.search(r"padding:([\d.]+)px ([\d.]+)px", toggle_style)
    font_size = re.search(r"font-size:([\d.]+)px", toggle_style)
    opacity = re.search(r"opacity:([\d.]+)", toggle_style)
    background = re.search(r"background:(var\(--[^)]+\))", card_style)
    border = re.search(r'\? "var\(--bad\)" : "(var\(--[^)]+\))"', card_style)
    assert padding and font_size and opacity and background and border
    return {
        "padding": [float(padding.group(1)), float(padding.group(2))],
        "font_size": float(font_size.group(1)),
        "standalone_opacity": float(opacity.group(1)),
        "card_background": background.group(1),
        "card_border": border.group(1),
    }


def _reader_state() -> dict[str, object]:
    inventory = [
        {
            "slug": "connected",
            "nav_key": "connected",
            "title": "Connected plan",
            "type": "plan",
            "status": "active",
            "effective_status": "active",
            "depends_on": ["upstream-a", "upstream-b"],
            "impl": 0.4,
            "effort_hours": 3,
        },
        {
            "slug": "upstream-a",
            "nav_key": "upstream-a",
            "title": "Upstream A",
            "type": "plan",
            "status": "shipped",
            "effective_status": "shipped",
            "depends_on": [],
            "impl": 1,
            "effort_hours": 1,
        },
        {
            "slug": "upstream-b",
            "nav_key": "upstream-b",
            "title": "Upstream B",
            "type": "plan",
            "status": "active",
            "effective_status": "active",
            "depends_on": [],
            "impl": 0.25,
            "effort_hours": 2,
        },
        {
            "slug": "downstream",
            "nav_key": "downstream",
            "title": "Downstream plan",
            "type": "plan",
            "status": "pending",
            "effective_status": "pending",
            "depends_on": ["connected"],
            "impl": 0,
            "effort_hours": 2,
        },
        {
            "slug": "isolated",
            "nav_key": "isolated",
            "title": "Isolated plan",
            "type": "plan",
            "status": "active",
            "effective_status": "active",
            "depends_on": [],
            "impl": 0,
            "effort_hours": 1,
        },
    ]
    return {
        "project": "reckon",
        "projects": [{"project": "reckon", "plans_count": len(inventory)}],
        "inventory": inventory,
        "plans": {item["slug"]: item for item in inventory},
        "sprints": [],
        "milestones": [],
        "north_stars": [],
        "timeline": [],
        "blockers": [],
        "active_sprints": [],
        "active_sprint_conflict": False,
        "attachment_relations": [],
    }


def _reader_preload() -> str:
    crew = json.dumps(
        {
            "runs": [
                {
                    "run_id": "reader-run",
                    "plan": "connected",
                    "backend": "local",
                    "role": "implement",
                    "member": "reader",
                    "section": "chrome",
                    "elapsed_seconds": 120,
                    "time_budget": "20m",
                    "phase": "working",
                }
            ]
        },
        separators=(",", ":"),
    )
    return f"""
      const nativeReaderFetch = window.fetch.bind(window);
      const readerCrew = {crew};
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
            '<main class="plan-doc"><p>Rendered body</p></main>',
            {{status: 200, headers: {{'Content-Type': 'text/html'}}}},
          ));
        }}
        return nativeReaderFetch(resource, options);
      }};
    """


def _reader_probe(contract: dict[str, object]) -> str:
    expected = json.dumps(contract, separators=(",", ":"))
    return rf"""window.__measureReaderFidelity = async () => {{
      const expected = {expected};
      const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
      const settle = () => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      const waitFor = async (predicate, description) => {{
        const deadline = performance.now() + 5000;
        while (performance.now() < deadline) {{
          if (predicate()) {{ await settle(); return; }}
          await delay(25);
        }}
        throw new Error(`timed out waiting for ${{description}}`);
      }};
      const pixels = color => {{
        const canvas = document.createElement('canvas');
        canvas.width = 1;
        canvas.height = 1;
        const context = canvas.getContext('2d', {{willReadFrequently: true}});
        context.clearRect(0, 0, 1, 1);
        context.fillStyle = color;
        context.fillRect(0, 0, 1, 1);
        return [...context.getImageData(0, 0, 1, 1).data];
      }};
      const saturation = color => {{
        const [red, green, blue, alpha] = pixels(color);
        if (alpha === 0) return 0;
        const channels = [red, green, blue].map(value => value / 255);
        const high = Math.max(...channels);
        const low = Math.min(...channels);
        if (high === low) return 0;
        const lightness = (high + low) / 2;
        return (high - low) / (1 - Math.abs(2 * lightness - 1));
      }};
      const groundProbe = document.createElement('span');
      groundProbe.style.cssText = `position:fixed;pointer-events:none;background:${{expected.card_background}}`;
      document.body.append(groundProbe);
      const groundColor = getComputedStyle(groundProbe).backgroundColor;
      const groundSaturation = saturation(groundColor);
      groundProbe.remove();
      const borderProbe = document.createElement('span');
      borderProbe.style.cssText = `position:fixed;pointer-events:none;border:1px solid ${{expected.card_border}}`;
      document.body.append(borderProbe);
      const cardBorderColor = getComputedStyle(borderProbe).borderTopColor;
      borderProbe.remove();

      await waitFor(() => document.querySelector('.r-reading-dependencies'), 'connected dependency control');
      const toggle = document.querySelector('.r-reading-dependencies');
      const header = document.querySelector('.r-reading-controls');
      const copy = document.querySelector('.r-reading-copy');
      const content = document.querySelector('.r-reading-content');
      toggle.click();
      await waitFor(() => document.querySelector('.r-reader-dependency-panel'), 'open dependency panel');
      const toggleRect = toggle.getBoundingClientRect();
      const headerRect = header.getBoundingClientRect();
      const copyRect = copy.getBoundingClientRect();
      const style = getComputedStyle(toggle);
      const cards = [...document.querySelectorAll('.r-reader-dependency-panel .r-dependency-cone-card')];
      const edgeCards = cards.filter(card => !card.classList.contains('is-focal'));
      const connected = {{
        label: toggle.textContent.trim().replace(/\s+/g, ' '),
        hasHeading: Boolean(toggle.querySelector('h1,h2,h3,h4,h5,h6')),
        widthRatio: toggleRect.width / content.getBoundingClientRect().width,
        insideHeader: toggleRect.top >= headerRect.top && toggleRect.bottom <= headerRect.bottom,
        besideControl: toggleRect.right <= copyRect.left,
        fontSize: parseFloat(style.fontSize),
        padding: [parseFloat(style.paddingTop), parseFloat(style.paddingRight)],
        panelVisible: Boolean(document.querySelector('.r-reader-dependency-panel')),
        allCardsOnGround: cards.every(card => getComputedStyle(card).backgroundColor === groundColor),
        edgeBordersUseLine: edgeCards.every(card => {{
          const cardStyle = getComputedStyle(card);
          return cardStyle.borderTopColor === cardBorderColor && cardStyle.borderTopWidth === '1px';
        }}),
      }};

      const visibleElements = [...document.querySelectorAll('.r-reader-with-attachments *')]
        .filter(element => {{
          const rect = element.getBoundingClientRect();
          const elementStyle = getComputedStyle(element);
          return rect.width > 0 && rect.height > 0 && elementStyle.visibility !== 'hidden';
        }});
      const saturatedPanels = visibleElements.flatMap(element => {{
        const rect = element.getBoundingClientRect();
        const color = getComputedStyle(element).backgroundColor;
        const value = saturation(color);
        if (rect.height < 24 || value <= groundSaturation + 0.01) return [];
        return [{{
          className: String(element.className),
          color,
          height: rect.height,
          saturation: value,
        }}];
      }});

      location.hash = '#plan/isolated';
      await waitFor(
        () => document.querySelector('.r-reading-dependencies')?.textContent.trim() === 'standalone',
        'isolated dependency control',
      );
      const standalone = document.querySelector('.r-reading-dependencies');
      return {{
        connected,
        standalone: {{
          label: standalone.textContent.trim(),
          opacity: parseFloat(getComputedStyle(standalone).opacity),
        }},
        saturatedPanels,
        groundColor,
        groundSaturation,
      }};
    }}"""


def test_reader_chrome_and_panels_follow_the_canvas(tmp_path: Path) -> None:
    contract = _canvas_contract()
    with file_spa(
        tmp_path,
        installed_browser_or_skip(),
        _reader_state(),
        route="#plan/connected",
    ) as spa:
        result = spa.run_probe(
            "window.__measureReaderFidelity()",
            viewport=VIEWPORT,
            ready_expression="Boolean(document.querySelector('.r-reading-controls'))",
            preload_expression=_reader_preload() + _reader_probe(contract),
        )

    connected = result["connected"]
    assert connected["label"] == "2 ↑ 1 ↓"
    assert connected["hasHeading"] is False
    assert connected["widthRatio"] < 0.25
    assert connected["insideHeader"] is True
    assert connected["besideControl"] is True
    assert connected["fontSize"] == contract["font_size"]
    assert connected["padding"] == contract["padding"]
    assert connected["panelVisible"] is True
    assert connected["allCardsOnGround"] is True
    assert connected["edgeBordersUseLine"] is True
    assert result["standalone"] == {
        "label": "standalone",
        "opacity": contract["standalone_opacity"],
    }
    assert result["saturatedPanels"] == [], result
