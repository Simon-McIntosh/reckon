import json
import subprocess
from pathlib import Path

from tests.spa_browser_harness import (
    AuthoredSource,
    authored_shell_source,
    file_spa,
    installed_browser_or_skip,
)

ROOT = Path(__file__).resolve().parents[1]
SHELL = authored_shell_source(ROOT)
PLAN = ROOT / "docs" / "ui" / "plan.jsx"
VIEWPORTS = ((1374, 900), (1920, 900))


def _function_source(name: str, path: Path | AuthoredSource = SHELL) -> str:
    source = path.read_text()
    start = source.index(f"function {name}(")
    brace = source.index(") {", start) + 2
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated function {name}")


def _evaluate(
    functions: list[str],
    expression: str,
    path: Path | AuthoredSource = SHELL,
):
    script = "\n".join(_function_source(name, path) for name in functions)
    result = subprocess.run(
        ["node", "-e", f"{script}\nconsole.log(JSON.stringify({expression}));"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_focus_key_toggles_both_ways_and_escape_keeps_selection() -> None:
    result = _evaluate(
        ["nextReadingMode"],
        "(() => { const selected = 'plan-a'; "
        "const entered = nextReadingMode(false, 'f', true); "
        "const leftByF = nextReadingMode(entered, 'f', true); "
        "const leftByEscape = nextReadingMode(entered, 'Escape', true); "
        "return { entered, leftByF, leftByEscape, selected }; })()",
    )

    assert result == {
        "entered": True,
        "leftByF": False,
        "leftByEscape": False,
        "selected": "plan-a",
    }


def test_reading_queue_follows_filtered_plan_and_attachment_bar_order() -> None:
    state = {
        "plans": {
            "plan-a": {"slug": "plan-a", "type": "plan"},
            "plan-b": {"slug": "plan-b", "type": "plan"},
            "research:note": {
                "nav_key": "research:note",
                "slug": "note",
                "type": "research",
            },
            "evidence:receipt": {
                "nav_key": "evidence:receipt",
                "slug": "receipt",
                "type": "evidence",
            },
        },
        "attachment_relations": [
            {"source": "research:note", "target": "plan-a"},
            {"source": "evidence:receipt", "target": "plan-a#gate"},
        ],
    }
    filtered = [
        {"slug": "plan-b", "type": "plan", "last": "2026-08-24"},
        {"slug": "plan-a", "type": "plan", "last": "2026-08-25"},
    ]
    queue = _evaluate(
        ["sortItems", "attachmentGroups", "readingQueue"],
        f"readingQueue({json.dumps(state)}, {json.dumps(filtered)}, 'edited', 'desc')",
    )
    next_after_plan = _evaluate(
        ["readingQueueStep"],
        f"readingQueueStep({json.dumps(queue)}, 'plan-a', 1)",
    )
    next_after_evidence = _evaluate(
        ["readingQueueStep"],
        f"readingQueueStep({json.dumps(queue)}, 'evidence:receipt', 1)",
    )

    assert queue == [
        "plan-a",
        "research:note",
        "evidence:receipt",
        "plan-b",
    ]
    assert next_after_plan == "research:note"
    assert next_after_evidence == "plan-b"


def test_palette_projects_typed_results_across_repositories() -> None:
    current = {
        "project": "alpha",
        "inventory": [
            {"slug": "work", "type": "plan", "title": "Work", "status": "active"},
        ],
    }
    projects = [
        {
            "project": "beta",
            "state": {
                "inventory": [
                    {
                        "nav_key": "research:study",
                        "slug": "study",
                        "type": "research",
                        "title": "Study",
                        "status": "done",
                    }
                ]
            },
        }
    ]
    result = _evaluate(
        ["paletteItems"],
        f"paletteItems({json.dumps(current)}, {json.dumps(projects)})",
    )

    assert [
        (row["kind"], row["label"], row["repository"], row["status"]) for row in result
    ] == [
        ("plan", "Work", "alpha", "active"),
        ("research", "Study", "beta", "done"),
    ]


def test_focus_mode_reuses_reader_with_provenance_banners() -> None:
    expression = "readerProvenanceSignals(FOCUS, { status: 404 }, { status: 503 })"
    reading = _evaluate(
        ["readerProvenanceSignals"],
        expression.replace("FOCUS", "false"),
        PLAN,
    )
    focused = _evaluate(
        ["readerProvenanceSignals"],
        expression.replace("FOCUS", "true"),
        PLAN,
    )

    assert reading == {
        "focusMode": False,
        "htmlFailure": True,
        "stateFailure": True,
    }
    assert focused == {**reading, "focusMode": True}
    assert {key: value for key, value in focused.items() if key != "focusMode"} == {
        key: value for key, value in reading.items() if key != "focusMode"
    }

    empty = _evaluate(
        ["readerAttachmentRows"],
        "readerAttachmentRows({ research: [], evidence: [] })",
        PLAN,
    )
    populated = _evaluate(
        ["readerAttachmentRows"],
        "readerAttachmentRows({ "
        "research: [{ slug: 'resource-a' }, { slug: 'resource-b' }], "
        "evidence: [{ slug: 'outcome' }] })",
        PLAN,
    )
    component = _function_source("ReaderAttachmentBars", PLAN)
    reader = _function_source("Plan", PLAN)

    assert empty == []
    assert [(label, len(items)) for _, label, items in populated] == [
        ("Resources", 2),
        ("Evidence", 1),
    ]
    assert component.count("if (rows.length === 0) return null;") == 1
    assert reader.count("<ReaderAttachmentBars") == 1
    assert "provenanceSignals.attachments" not in reader


def test_escape_path_exits_focus_without_routing_or_clearing_selection() -> None:
    app = (
        SHELL.read_text()
        .split("function App()", 1)[1]
        .split("function CmdKPalette", 1)[0]
    )
    escape = app.split('if (e.key === "Escape" && readingMode)', 1)[1].split(
        "return;", 1
    )[0]

    assert "setReadingMode" in escape
    assert "nav(" not in escape
    assert "route.slug" not in escape


def test_reader_steps_the_published_rendered_order_instead_of_rederiving_it() -> None:
    list_rows = [
        {"key": "first", "slug": "first", "type": "plan"},
        {"key": "second", "slug": "second", "type": "plan"},
        {"key": "third", "slug": "third", "type": "plan"},
    ]
    position = _evaluate(
        ["readerListPosition"],
        f"readerListPosition({json.dumps(list_rows)}, 'second', 'second')",
        PLAN,
    )
    target = _evaluate(
        ["readerListPosition", "readerStepTarget"],
        f"readerStepTarget({json.dumps(list_rows)}, 'second', 'second', 1)",
        PLAN,
    )

    assert position == {"current": 2, "total": 3}
    assert target == {"key": "third", "slug": "third", "type": "plan"}
    reader = _function_source("Plan", PLAN)
    assert 'document.addEventListener("keydown", handleReaderKey, true)' in reader
    assert "matches?.(\"input, textarea, select, [contenteditable='true']\")" in reader
    assert "document.activeElement?.matches?." in reader
    assert "focusPosition" not in reader
    assert "onPage" not in reader
    assert "setReaderSelectionKey(target.key)" in reader


def _measure_state() -> dict[str, object]:
    inventory = [
        {
            "slug": "measured",
            "nav_key": "measured",
            "title": "Measured plan",
            "type": "plan",
            "status": "active",
            "effective_status": "active",
            "depends_on": [],
            "impl": 0.5,
            "effort_hours": 1,
        }
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


def _measure_preload() -> str:
    return r"""
      const nativeFetch = window.fetch.bind(window);
      window.fetch = (resource, options) => {
        const url = new URL(String(resource), window.location.href);
        if (url.pathname.startsWith('/plan/reckon/')) {
          return Promise.resolve(new Response(JSON.stringify({
            version: 1, decisions: [], comments: {}, gates: [], followups: [],
          }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
        }
        if (url.pathname.startsWith('/reckon/') && url.pathname.endsWith('.html')) {
          return Promise.resolve(new Response(
            '<main class="plan-doc"><p>Rendered body</p></main>',
            { status: 200, headers: { 'Content-Type': 'text/html' } },
          ));
        }
        return nativeFetch(resource, options);
      };
    """


def _measure_probe() -> str:
    return r"""window.__measureReadingWidths = async () => {
      const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
      const settle = () => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      const waitFor = async (predicate, description) => {
        const deadline = performance.now() + 8000;
        while (performance.now() < deadline) {
          if (predicate()) { await settle(); return; }
          await delay(25);
        }
        throw new Error(`timed out waiting for ${description}`);
      };
      const measure = () => {
        const element = document.querySelector('.r-reading-content');
        if (!element) throw new Error('no .r-reading-content rendered');
        return element.getBoundingClientRect().width;
      };

      await waitFor(() => document.querySelector('.r-reading-content'), 'entering base view');
      const base = measure();
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'f', bubbles: true }));
      await waitFor(() => document.querySelector('.r-reading.is-focus-mode'), 'focus mode after f');
      const focus = measure();
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
      await waitFor(() => !document.querySelector('.r-reading.is-focus-mode'), 'exit focus with Escape');
      const restored = measure();
      return { base, focus, restored };
    }"""


def test_reading_measure_does_not_move_in_full_screen(tmp_path: Path) -> None:
    browser = installed_browser_or_skip()
    for viewport in VIEWPORTS:
        with file_spa(
            tmp_path, browser, _measure_state(), route="#plan/measured"
        ) as spa:
            result = spa.run_probe(
                "window.__measureReadingWidths()",
                viewport=viewport,
                ready_expression="Boolean(document.querySelector('.r-reading-content'))",
                preload_expression=_measure_preload() + _measure_probe(),
            )
        assert result["base"] == result["focus"] == result["restored"] == 820, (
            viewport,
            result,
        )
