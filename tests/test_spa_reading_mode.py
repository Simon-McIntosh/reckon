import json
import subprocess
from pathlib import Path

from tests.spa_browser_harness import AuthoredSource, authored_shell_source

ROOT = Path(__file__).resolve().parents[1]
SHELL = authored_shell_source(ROOT)
PLAN = ROOT / "docs" / "ui" / "plan.jsx"


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
