"""The figure list renders only the rows on screen.

A figure index of a thousand-plus rows, each carrying a thumbnail, used to put
every row in the page at once: seven long tasks totalling 1.05 s on a headless
load. The artifact feed now mounts only the rows within and near the viewport,
keeping the scroll height of the full list, so scrolling still reaches the last
row while the page holds a small window of rows.

The negative control renders every row. Armed with
``RECKON_TEST_DISABLE_ROW_WINDOWING=1`` the surface is composed from a copy of
``shell-plans.jsx`` whose windowing returns the whole list, so the at-most-150
rows assertion below fails.
"""

from __future__ import annotations

import html
import json
import os
from collections.abc import Mapping
from pathlib import Path

from tests.spa_browser_harness import (
    _SPA_STYLESHEETS,
    ROOT,
    _script_body,
    authored_ui_module_paths,
    installed_browser_or_skip,
    run_browser_probe,
)

VIEWPORT = (1374, 900)
FIGURE_COUNT = 1000
MAX_MOUNTED_ROWS = 150
LONG_TASK_MS = 200
NEGATIVE_CONTROL_ENV = "RECKON_TEST_DISABLE_ROW_WINDOWING"
TRANSPARENT_GIF = (
    "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
)

# The declared mutation: render every row of the figure list, so the window the
# feed would mount becomes the whole list.
_MUTATION_OLD = (
    "  if (rows.length === 0) return { start: 0, end: -1, top: 0, bottom: 0 };\n"
)
_MUTATION_NEW = (
    _MUTATION_OLD + "  return { start: 0, end: rows.length - 1, top: 0, bottom: 0 };\n"
)


def _figure_state(count: int = FIGURE_COUNT) -> dict[str, object]:
    inventory = [
        {
            "slug": f"figures/{index:04d}.png",
            "nav_key": f"figures/{index:04d}.png",
            "title": f"Figure {index:04d}",
            "type": "figure",
            "status": "active",
            "dims": "640 \u00d7 480",
            "created": 100 + index,
            "edited": "2020-01-01T00:00:00",
            "href": TRANSPARENT_GIF,
        }
        for index in range(count)
    ]
    return {
        "project": "gallery",
        "projects": [{"project": "gallery", "plans_count": count}],
        "inventory": inventory,
        "plans": {item["nav_key"]: item for item in inventory},
        "sprints": [],
        "milestones": [],
        "north_stars": [],
        "timeline": [],
        "blockers": [],
        "attachment_relations": [],
        "active_sprints": [],
        "active_sprint_conflict": False,
    }


def _mutated_module_source(name: str, source: str) -> str:
    if name != "shell-plans.jsx" or os.environ.get(NEGATIVE_CONTROL_ENV) != "1":
        return source
    assert _MUTATION_OLD in source, (
        "windowing source changed; mutation no longer applies"
    )
    return source.replace(_MUTATION_OLD, _MUTATION_NEW)


def _write_document(
    destination: Path,
    state: Mapping[str, object],
    *,
    project: str = "gallery",
) -> Path:
    """Compose one browser-ready SPA document from the authored modules."""

    from reckon.serve import client_runtime_assets, compile_jsx

    runtime = client_runtime_assets()
    styles = [
        (ROOT / "docs" / relative).read_text(encoding="utf-8")
        for relative in _SPA_STYLESHEETS
    ]
    scripts = [
        runtime["react.js"].decode("utf-8"),
        runtime["react-dom.js"].decode("utf-8"),
        (
            f"window.STATE = {json.dumps(state, separators=(',', ':'))};\n"
            "window.STATE_ERROR = null;\n"
            "window.STATE_READY = Promise.resolve(window.STATE);\n"
            "window.revalidateProjectState = async () => window.STATE;"
        ),
    ]
    for path in authored_ui_module_paths():
        if path.name == "state-loader.js":
            continue
        source = _mutated_module_source(path.name, path.read_text(encoding="utf-8"))
        if path.suffix == ".jsx":
            source = compile_jsx(source, filename=path.name).decode("utf-8")
        scripts.append(source)

    document = "\n".join(
        [
            "<!doctype html>",
            '<html lang="en">',
            "<head>",
            '  <meta charset="utf-8">',
            f'  <meta name="docs-project" content="{html.escape(project, quote=True)}">',
            '  <meta name="viewport" content="width=device-width,initial-scale=1">',
            f"  <title>reckon · {html.escape(project)}</title>",
            f"  <style>{'</style><style>'.join(styles)}</style>",
            "</head>",
            "<body>",
            '  <div id="root"></div>',
            *(f"  <script>{_script_body(script)}</script>" for script in scripts),
            "</body>",
            "</html>",
        ]
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(document, encoding="utf-8")
    return destination


# Install the long-task observer before any page script runs, so a main-thread
# task during load or scroll is recorded rather than missed.
_PRELOAD = r"""(() => {
  window.__longTasks = [];
  try {
    const observer = new PerformanceObserver(list => {
      for (const entry of list.getEntries()) {
        window.__longTasks.push({ start: entry.startTime, duration: entry.duration });
      }
    });
    observer.observe({ type: "longtask", buffered: true });
    window.__longTaskObserver = "live";
  } catch (error) {
    window.__longTaskObserver = String(error);
  }
})()"""


_PROBE = r"""(async () => {
  const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
  const waitFor = async (predicate, description, timeoutMs = 20000) => {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      if (predicate()) return true;
      await delay(25);
    }
    throw new Error(`timed out waiting for ${description}`);
  };
  const longTasks = () => window.__longTasks || [];

  const feed = await (async () => {
    await waitFor(
      () => document.querySelector(".r-canvas-view[data-artifact-kind='figure'] .r-artifact-feed"),
      "the figure feed",
    );
    return document.querySelector(".r-artifact-feed");
  })();
  const rows = () => [...feed.querySelectorAll(":scope > .r-artifact-row")];
  await waitFor(() => rows().length > 0, "the figure rows");

  const figures = window.STATE.inventory.filter(item => (item.type || "plan") === "figure");
  const slugOf = item => item.nav_key || item.slug;
  const firstSlug = slugOf(figures[0]);
  const lastSlug = slugOf(figures[figures.length - 1]);

  // Let the height model settle: the first frame mounts an estimate-only window.
  let previousHeight = -1;
  let stable = 0;
  while (stable < 3) {
    await delay(60);
    if (feed.scrollHeight === previousHeight) stable += 1;
    else { previousHeight = feed.scrollHeight; stable = 0; }
  }

  const mountedAtTop = rows().length;
  const firstRendered = rows()[0]?.dataset.artifactSlug || "";
  const loadTaskCount = longTasks().length;

  feed.scrollTop = feed.scrollHeight;
  await waitFor(
    () => Boolean(feed.querySelector(`[data-artifact-slug="${lastSlug}"]`)),
    "the last figure row",
  );
  await delay(100);

  const mountedAtEnd = rows().length;
  const lastRendered = rows()[rows().length - 1]?.dataset.artifactSlug || "";
  const scrollTaskCount = longTasks().length;

  // Prove the instrument sees a long task before an absence is reported: a
  // deliberate 260 ms busy loop must be recorded over the 200 ms threshold.
  const spinUntil = performance.now() + 260;
  while (performance.now() < spinUntil) { /* deliberate long task */ }
  await delay(60);
  const detectorDuration = Math.max(
    0,
    ...longTasks().slice(scrollTaskCount).map(entry => entry.duration),
  );

  return {
    observer: window.__longTaskObserver,
    mountedAtTop,
    mountedAtEnd,
    firstRendered,
    lastRendered,
    firstSlug,
    lastSlug,
    loadLongTasks: longTasks().slice(0, loadTaskCount).map(entry => entry.duration),
    scrollLongTasks: longTasks().slice(loadTaskCount, scrollTaskCount).map(entry => entry.duration),
    detectorDuration,
  };
})()"""


def test_the_figure_list_mounts_only_the_rows_near_the_viewport(
    tmp_path: Path,
) -> None:
    browser = installed_browser_or_skip()
    document = _write_document(tmp_path / "gallery" / "index.html", _figure_state())

    result = run_browser_probe(
        tmp_path,
        browser,
        document,
        _PROBE,
        viewport=VIEWPORT,
        route="#figures",
        ready_expression=(
            'Boolean(document.querySelector(".r-artifact-feed") '
            '&& document.querySelector(".r-artifact-row"))'
        ),
        preload_expression=_PRELOAD,
    )

    assert result["observer"] == "live", result
    assert result["detectorDuration"] >= LONG_TASK_MS, result

    assert result["mountedAtTop"] <= MAX_MOUNTED_ROWS, result
    assert result["mountedAtEnd"] <= MAX_MOUNTED_ROWS, result
    assert result["firstRendered"] == result["firstSlug"], result
    assert result["lastRendered"] == result["lastSlug"], result

    missed = [
        duration
        for duration in result["loadLongTasks"] + result["scrollLongTasks"]
        if duration > LONG_TASK_MS
    ]
    assert missed == [], result
