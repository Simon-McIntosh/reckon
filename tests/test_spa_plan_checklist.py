"""The plan view shows its section checklist above the first section.

The payload carries one entry per authored section; the view renders each
entry as it is and derives nothing else. A closed entry shows its closing
comment, a deferred entry reads as deferred.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from tests import spa_module_eval

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "docs" / "ui" / "plan.jsx"
PLANS_CSS = ROOT / "docs" / "ui" / "plans.css"

# The view's own render of the checklist: the props expression is read back out
# of the source, so the render under test is the one the plan view performs.
CHECKLIST_USAGE = re.compile(r"<PlanChecklist\s+todos=\{([^}]+)\}")

_REACT_STUB = """
const React = {
  createElement: (type, props, ...children) => ({ __element: true, type, props: props || {}, children }),
  Fragment: "Fragment",
  useState: initial => [initial, () => {}],
  useEffect: () => {},
  useLayoutEffect: () => {},
  useRef: () => ({ current: null }),
};
"""


# A document stub that records what a click scrolls into view.
_DOCUMENT_STUB = """
const scrolled = [];
const document = {
  getElementById: id => ({ scrollIntoView: options => scrolled.push([id, options]) }),
};
"""

_CLICK_EXPRESSION = """(() => {
  const anchors = [];
  const walk = node => {
    if (Array.isArray(node)) { node.forEach(walk); return; }
    if (!node || !node.__element) return;
    if (node.type === "a") anchors.push(node);
    (node.children || []).forEach(walk);
  };
  walk(RENDER);
  const last = anchors[anchors.length - 1];
  if (typeof last.props.onClick !== "function") {
    return { handled: false, defaultPrevented: false, scrolled: null, href: last.props.href };
  }
  const event = { defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
  last.props.onClick(event);
  return { handled: true, defaultPrevented: event.defaultPrevented, scrolled, href: last.props.href };
})()"""


def _component_source(source: str) -> str:
    start = source.index("function PlanChecklist(")
    body = source.index(") {", start) + 2
    depth = 0
    for index in range(body, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError("unterminated function PlanChecklist")


def _checklist_expression() -> str:
    usage = CHECKLIST_USAGE.search(PLAN.read_text())
    assert usage, "the plan view does not render <PlanChecklist todos={...} />"
    return f"PlanChecklist({{ todos: ({usage.group(1)}) }})"


def _harness(tmp_path: Path, todos: list[dict], name: str, extra: str = "") -> Path:
    harness = tmp_path / f"{name}.jsx"
    harness.write_text(
        _REACT_STUB
        + extra
        + f"{_component_source(PLAN.read_text())}\n"
        + f"const fullState = {json.dumps({'todos': todos})};\n",
        encoding="utf-8",
    )
    return harness


def _render(tmp_path: Path, todos: list[dict]) -> dict:
    harness = _harness(tmp_path, todos, "plan_checklist_probe")
    return spa_module_eval.evaluate_jsx_module(harness, _checklist_expression())


def _click(tmp_path: Path, todos: list[dict]) -> dict:
    harness = _harness(tmp_path, todos, "plan_checklist_click", _DOCUMENT_STUB)
    expression = _CLICK_EXPRESSION.replace("RENDER", _checklist_expression())
    return spa_module_eval.evaluate_jsx_module(harness, expression)


def _elements(node) -> list[dict]:
    found = []
    if isinstance(node, dict) and node.get("__element"):
        found.append(node)
        for child in node.get("children") or []:
            found.extend(_elements(child))
    elif isinstance(node, list):
        for child in node:
            found.extend(_elements(child))
    return found


def _text(node) -> str:
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_text(child) for child in node)
    if isinstance(node, dict) and node.get("__element"):
        return "".join(_text(child) for child in node.get("children") or [])
    return ""


def _rows(tree: dict) -> dict[str, dict]:
    return {
        element["props"].get("data-section"): element
        for element in _elements(tree)
        if element["type"] == "li"
    }


TODOS = [
    {
        "id": "s1",
        "heading": "§1 — First",
        "link": "#s1",
        "declaration": "done",
        "close": {
            "id": "c-close-s1",
            "who": "lead",
            "when": "2026-10-08",
            "body": "Landed x",
        },
    },
    {
        "id": "s2",
        "heading": "§2 — Second",
        "link": "#s2",
        "declaration": "implementable",
        "close": None,
    },
    {
        "id": "s3",
        "heading": "§3 — Third",
        "link": "#s3",
        "declaration": "deferred",
        "close": None,
    },
]


def test_checklist_links_each_entry_to_its_section_in_document_order(tmp_path):
    anchors = [
        element["props"]["href"]
        for element in _elements(_render(tmp_path, TODOS))
        if element["type"] == "a"
    ]

    assert anchors == ["#s1", "#s2", "#s3"]


def test_done_entry_is_checked_and_shows_its_closing_comment(tmp_path):
    tree = _render(tmp_path, TODOS)
    row = _rows(tree)["s1"]

    assert row["props"]["data-state"] == "done"
    boxes = [e for e in _elements(row) if e["type"] == "input"]
    assert [box["props"]["checked"] for box in boxes] == [True]
    closes = [
        e for e in _elements(row) if e["props"].get("className") == "r-plan-todo-close"
    ]
    assert closes[0]["props"]["dangerouslySetInnerHTML"]["__html"] == "Landed x"
    assert "§1 — First" in _text(row)


def test_deferred_entry_reads_as_deferred_and_open_entry_reads_as_open(tmp_path):
    rows = _rows(_render(tmp_path, TODOS))

    assert rows["s3"]["props"]["data-state"] == "deferred"
    assert "deferred" in _text(rows["s3"])
    assert rows["s2"]["props"]["data-state"] == "open"
    assert "open" in _text(rows["s2"])


def test_checklist_render_sits_above_the_first_section():
    source = PLAN.read_text()

    usage = source.index("<PlanChecklist todos=")
    body = source.index('className="r-plan-html"')
    assert usage < body


def test_row_link_click_scrolls_the_section_without_title_navigation(tmp_path):
    result = _click(tmp_path, TODOS)

    assert result["handled"] is True
    assert result["href"] == "#s3"
    assert result["defaultPrevented"] is True
    assert result["scrolled"] == [["s3", {"block": "start"}]]


def test_checklist_styles_are_registered_for_its_rows():
    styles = PLANS_CSS.read_text()

    for selector in (
        ".r-plan-checklist",
        ".r-plan-todo",
        ".r-plan-todo-box",
        ".r-plan-todo-state",
    ):
        assert selector in styles
