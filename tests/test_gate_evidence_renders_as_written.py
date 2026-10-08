"""A gate's evidence renders as a link only when it is a reference.

A gate records its evidence either as a reference the surface can follow or as
prose describing what was recorded. The plan view renders the value through the
server's reference grammar, so prose evidence is shown as text instead of being
wrapped in an href a reader cannot follow.

The view's own render is exercised: the grammar helpers and the gate table are
read back out of ``docs/ui/plan.jsx`` and evaluated, so the code under test is
the code the plan view runs.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests import spa_module_eval

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "docs" / "ui" / "plan.jsx"

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


def _function_source(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    body = source.index(") {", start) + 2
    depth = 0
    for index in range(body, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated function {name}")


def _grammar_source() -> str:
    source = PLAN.read_text()
    grammar_start = source.index("const GATE_REFERENCE_SEGMENT")
    guard = source.index("function gateEvidenceIsReference(")
    guard_end = _function_source(source, "gateEvidenceIsReference")
    return source[grammar_start:guard] + guard_end


def _harness(tmp_path: Path, gates: list[dict], name: str) -> Path:
    harness = tmp_path / f"{name}.jsx"
    harness.write_text(
        _REACT_STUB
        + _grammar_source()
        + "\n"
        + _function_source(PLAN.read_text(), "GateTable")
        + f"\nconst gateRows = {json.dumps(gates)};\n",
        encoding="utf-8",
    )
    return harness


def _render(tmp_path: Path, gates: list[dict]) -> object:
    harness = _harness(tmp_path, gates, "gate_evidence_probe")
    return spa_module_eval.evaluate_jsx_module(harness, "GateTable({ gates: gateRows })")


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


def _anchors(tree) -> list[dict]:
    return [element for element in _elements(tree) if element["type"] == "a"]


PROSE = (
    "jev-prompt-carries-what-it-weighs merged on main at 5b60f4c91 (2026-10-07): "
    "state.jinja renders the one build_state mapping and each candidate carries "
    "its lane and model as separate fields."
)
ANCHOR = "one-typesafe-model-picker#s6-jev-chooses-the-lane"
ROUTE = "/reckon/evidence/archive/budget-aware-dispatch-landed#holds"


def _gate(gate_id: str, evidence: str) -> dict:
    return {
        "id": gate_id,
        "section": "s11",
        "gated_sections": ["s11"],
        "status": "closed",
        "measure": "A measure",
        "required_evidence": "",
        "verdict": "passed",
        "evidence": evidence,
        "passed": True,
    }


def test_prose_evidence_renders_as_text_with_no_link(tmp_path):
    tree = _render(tmp_path, [_gate("prose", PROSE)])

    assert _anchors(tree) == []
    assert PROSE in _text(tree)


def test_prose_evidence_value_is_never_wrapped_in_an_href(tmp_path):
    tree = _render(tmp_path, [_gate("prose", PROSE)])

    assert all(anchor["props"].get("href") != PROSE for anchor in _anchors(tree))


def test_anchor_reference_evidence_links_to_the_anchor(tmp_path):
    tree = _render(tmp_path, [_gate("anchor", ANCHOR)])

    assert [anchor["props"]["href"] for anchor in _anchors(tree)] == [ANCHOR]


def test_route_reference_evidence_links_to_the_route(tmp_path):
    tree = _render(tmp_path, [_gate("route", ROUTE)])

    assert [anchor["props"]["href"] for anchor in _anchors(tree)] == [ROUTE]


def test_prose_and_reference_gates_render_side_by_side(tmp_path):
    tree = _render(
        tmp_path,
        [_gate("prose", PROSE), _gate("anchor", ANCHOR)],
    )

    assert [anchor["props"]["href"] for anchor in _anchors(tree)] == [ANCHOR]


def test_absent_evidence_reads_not_recorded(tmp_path):
    tree = _render(tmp_path, [_gate("open", "")])

    assert _anchors(tree) == []
    assert "Not recorded" in _text(tree)