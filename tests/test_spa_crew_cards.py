from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from reckon import serve

ROOT = Path(__file__).resolve().parents[1]
CREW = ROOT / "docs" / "ui" / "crew.jsx"

NODE_PRELUDE = r"""
globalThis.window = globalThis;
const noop = () => {};
globalThis.React = {
  createElement(type, props, ...children) { return { type, props: props || {}, children }; },
  Fragment: Symbol("Fragment"),
  useState(value) { return [typeof value === "function" ? value() : value, noop]; },
  useEffect(effect) { effect(); },
};
Object.defineProperty(globalThis, "navigator", {
  configurable: true,
  value: { clipboard: { writeText(value) { globalThis.__copiedText = value; } } },
});
globalThis.fetch = async () => ({ ok: true, json: async () => ({ runs: [] }) });
globalThis.__pollMs = null;
window.setInterval = (_callback, milliseconds) => { globalThis.__pollMs = milliseconds; return 1; };
window.clearInterval = noop;
window.flashSaved = noop;

function walk(node, visit) {
  if (node == null || node === false || node === true) return;
  if (Array.isArray(node)) { node.forEach(child => walk(child, visit)); return; }
  if (typeof node !== "object") return;
  visit(node);
  for (const child of node.children || []) walk(child, visit);
}

function hasClass(node, name) {
  return String(node?.props?.className || "").split(/\s+/).includes(name);
}

function findAll(node, predicate) {
  const matches = [];
  walk(node, candidate => { if (predicate(candidate)) matches.push(candidate); });
  return matches;
}

function textContent(node) {
  if (node == null || node === false || node === true) return "";
  if (Array.isArray(node)) return node.map(textContent).join("");
  if (typeof node === "string" || typeof node === "number") return String(node);
  return (node.children || []).map(textContent).join("");
}

function textOutside(node, excluded) {
  if (node === excluded) return "";
  if (node == null || node === false || node === true) return "";
  if (Array.isArray(node)) return node.map(child => textOutside(child, excluded)).join("");
  if (typeof node === "string" || typeof node === "number") return String(node);
  return (node.children || []).map(child => textOutside(child, excluded)).join("");
}
"""

TEST_EXPORTS = """
window.__crewTest = {
  CrewRunCard,
  CrewView,
  crewCardProjection,
  crewScopedProjects,
  styles: CREW_CARD_STYLES,
};
"""


def _run_probe(
    source: str,
    run: dict,
    *,
    state: dict | None = None,
    probe: str,
) -> dict:
    compiled = serve.compile_jsx(
        source + TEST_EXPORTS,
        filename="crew-contract-probe.jsx",
    ).decode()
    script = "\n".join(
        (
            NODE_PRELUDE,
            f"window.STATE = {json.dumps(state)};",
            compiled,
            f"const run = {json.dumps(run)};",
            "const result = (() => {" + probe + "})();",
            "process.stdout.write(JSON.stringify(result));",
        )
    )
    result = subprocess.run(
        ["node"],
        cwd=ROOT,
        input=script,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _mutate_once(source: str, pattern: str, replacement: str) -> str:
    mutated, count = re.subn(pattern, replacement, source, count=1, flags=re.MULTILINE)
    assert count == 1
    return mutated


def _assert_mutation_is_rejected(
    source: str,
    mutation: Callable[[str], str],
    contract: Callable[[str], None],
) -> None:
    contract(source)
    with pytest.raises(AssertionError):
        contract(mutation(source))


def test_collapsed_card_shows_plan_worker_hours_without_runtime_model_strings() -> None:
    source = CREW.read_text(encoding="utf-8")
    runtime_backend = "runtime-backend-sentinel"
    runtime_model = "runtime-model-sentinel"
    run = {
        "run_id": "run-visible",
        "project": "reckon",
        "plan": "work",
        "role": "implement",
        "backend": runtime_backend,
        "model": runtime_model,
        "effort": "high",
    }
    state = {
        "project": "reckon",
        "inventory": [{"slug": "work", "type": "plan", "effort_hours": 3.25}],
    }

    def contract(candidate: str) -> None:
        result = _run_probe(
            candidate,
            run,
            state=state,
            probe="""
const projected = window.__crewTest.crewCardProjection(run, window.STATE);
const rendered = window.__crewTest.CrewRunCard({ run });
const connection = findAll(rendered, node => hasClass(node, "r-crew-connect"))[0];
return {
  projected,
  collapsedText: textOutside(rendered, connection),
  connectionText: textContent(connection),
};
""",
        )
        assert result["projected"]["planEffort"] == "3.25 worker-hours"
        assert "3.25 worker-hours" in result["collapsedText"]
        assert runtime_backend not in result["collapsedText"]
        assert runtime_model not in result["collapsedText"]
        assert runtime_backend in result["connectionText"]
        assert runtime_model in result["connectionText"]

    _assert_mutation_is_rejected(
        source,
        lambda candidate: _mutate_once(
            candidate,
            r'^\s*<span className="r-crew-plan-effort">\{card\.planEffort\}</span>\s*$',
            "",
        ),
        contract,
    )


def test_long_done_when_is_one_line_without_hiding_phase_or_activity() -> None:
    source = CREW.read_text(encoding="utf-8")
    done_when = "measured evidence remains visible; " * 39
    assert len(done_when) >= 1_300
    run = {
        "run_id": "run-visible",
        "phase": "stalled",
        "gate": done_when,
        "last_activity": "2026-08-24T13:27:35Z",
    }

    def contract(candidate: str) -> None:
        result = _run_probe(
            candidate,
            run,
            probe="""
const rendered = window.__crewTest.CrewRunCard({ run });
const details = findAll(rendered, node => hasClass(node, "r-crew-done-when"));
const phase = findAll(rendered, node => hasClass(node, "r-crew-phase"))[0];
const activity = findAll(rendered, node => hasClass(node, "r-crew-activity"))[0];
return {
  detailsCount: details.length,
  doneWhenText: details.map(textContent).join(""),
  phase: textContent(phase),
  activity: textContent(activity),
  styles: window.__crewTest.styles,
};
""",
        )
        assert result["detailsCount"] == 1
        assert done_when in result["doneWhenText"]
        assert result["phase"] == "stalled"
        assert result["activity"].removeprefix("active ") not in {"", "—"}
        assert re.search(
            r"\.r-crew-contract\s*\{[^}]*-webkit-line-clamp\s*:\s*1(?:\s*[;}])",
            result["styles"],
        )

    _assert_mutation_is_rejected(
        source,
        lambda candidate: _mutate_once(
            candidate,
            r"-webkit-line-clamp\s*:\s*1",
            "",
        ),
        contract,
    )


def test_card_exposes_copyable_attach_command_only_in_connection_details() -> None:
    source = CREW.read_text(encoding="utf-8")
    command = "ssh worker@compute -t 'zellij -s fleet attach'"
    run = {
        "run_id": "run-attach",
        "session": "fleet",
        "host": "compute",
        "attach_command": command,
    }

    def contract(candidate: str) -> None:
        result = _run_probe(
            candidate,
            run,
            probe="""
const rendered = window.__crewTest.CrewRunCard({ run });
const connection = findAll(rendered, node => hasClass(node, "r-crew-connect"))[0];
const copyButton = findAll(connection, node => node.type === "button")[0];
copyButton.props.onClick();
return {
  connectionCount: findAll(rendered, node => hasClass(node, "r-crew-connect")).length,
  connectionText: textContent(connection),
  outsideText: textOutside(rendered, connection),
  copyDisabled: Boolean(copyButton.props.disabled),
  copiedText: globalThis.__copiedText || "",
};
""",
        )
        assert result["connectionCount"] == 1
        assert command in result["connectionText"]
        assert command not in result["outsideText"]
        assert result["copyDisabled"] is False
        assert result["copiedText"] == command

    _assert_mutation_is_rejected(
        source,
        lambda candidate: _mutate_once(
            candidate,
            r"navigator\.clipboard\?\.writeText\(card\.attachCommand\);",
            "",
        ),
        contract,
    )


def test_crew_scoped_projects_defaults_to_selected_and_widens_when_all_visible() -> (
    None
):
    source = CREW.read_text(encoding="utf-8")

    def contract(candidate: str) -> None:
        result = _run_probe(
            candidate,
            {},
            probe="""
return {
  defaultScope: window.__crewTest.crewScopedProjects("nova", ["nova", "reckon", "ambix"], false),
  widenedScope: window.__crewTest.crewScopedProjects("nova", ["nova", "reckon", "ambix"], true),
  fallbackScope: window.__crewTest.crewScopedProjects(null, ["nova", "reckon"], false),
};
""",
        )
        assert result["defaultScope"] == ["nova"]
        assert result["widenedScope"] == ["nova", "reckon", "ambix"]
        assert result["fallbackScope"] == ["nova", "reckon"]

    _assert_mutation_is_rejected(
        source,
        lambda candidate: _mutate_once(
            candidate,
            r"return \[selectedProject\];",
            "return Array.isArray(visibleProjects) ? visibleProjects : [];",
        ),
        contract,
    )


def test_crew_view_scopes_cards_to_selected_project_by_default() -> None:
    source = CREW.read_text(encoding="utf-8")
    runs = [
        {"run_id": "a", "project": "nova"},
        {"run_id": "b", "project": "reckon"},
        {"run_id": "c", "project": "nova"},
    ]

    def contract(candidate: str) -> None:
        result = _run_probe(
            candidate,
            {},
            probe=f"""
const runs = {json.dumps(runs)};
const scoped = window.__crewTest.crewScopedProjects("nova", ["nova", "reckon"], false);
const widened = window.__crewTest.crewScopedProjects("nova", ["nova", "reckon"], true);
return {{
  scopedProjects: runs.filter(run => scoped.includes(run.project)).map(run => run.project),
  widenedProjects: runs.filter(run => widened.includes(run.project)).map(run => run.project),
}};
""",
        )
        assert result["scopedProjects"] == ["nova", "nova"]
        assert sorted(result["widenedProjects"]) == ["nova", "nova", "reckon"]

    _assert_mutation_is_rejected(
        source,
        lambda candidate: _mutate_once(
            candidate,
            r"return \[selectedProject\];",
            "return Array.isArray(visibleProjects) ? visibleProjects : [];",
        ),
        contract,
    )


def test_crew_view_header_and_toggle_reflect_selected_project() -> None:
    source = CREW.read_text(encoding="utf-8")

    def contract(candidate: str) -> None:
        result = _run_probe(
            candidate,
            {},
            state={},
            probe="""
const rendered = window.__crewTest.CrewView({ visibleProjects: ["nova", "reckon"], mountedProjectCount: 2, selectedProject: "nova" });
const heading = findAll(rendered, node => hasClass(node, "r-crew-heading"))[0];
const title = findAll(heading, node => node.type === "h1")[0];
const toggle = findAll(heading, node => hasClass(node, "r-crew-scope-toggle"))[0];
return {
  titleText: textContent(title),
  toggleText: textContent(toggle),
  togglePressed: toggle.props["aria-pressed"],
};
""",
        )
        assert result["titleText"] == "nova · 0 runs"
        assert result["toggleText"] == "All visible"
        assert result["togglePressed"] is False

    _assert_mutation_is_rejected(
        source,
        lambda candidate: _mutate_once(
            candidate,
            r'className="r-crew-scope-toggle"',
            'className="r-crew-scope-toggle-removed"',
        ),
        contract,
    )


def test_cards_keep_poll_timing_and_use_structured_budget_and_gate_marks() -> None:
    source = CREW.read_text(encoding="utf-8")
    run = {
        "elapsed_seconds": 900,
        "time_budget": "30m",
        "gates_total": 4,
        "gates_done": 2,
        "current_gate": "focused tests",
        "gate_verdict": "passed",
    }

    def contract(candidate: str) -> None:
        result = _run_probe(
            candidate,
            run,
            state={},
            probe="""
window.__crewTest.CrewView({ visibleProjects: [], mountedProjectCount: 0 });
const projected = window.__crewTest.crewCardProjection(run, window.STATE);
const rendered = window.__crewTest.CrewRunCard({ run });
const meter = findAll(rendered, node => hasClass(node, "r-crew-meter"))[0];
const marks = findAll(rendered, node => hasClass(node, "r-crew-gate-marks"))[0];
const markNodes = findAll(marks, node => node.type === "i");
const currentGate = findAll(rendered, node => hasClass(node, "r-crew-current-gate"))[0];
return {
  projected,
  pollMs: globalThis.__pollMs,
  meterWidth: findAll(meter, node => node.type === "i")[0].props.style.width,
  markCount: markNodes.length,
  measuredMarkCount: markNodes.filter(node => hasClass(node, "measured")).length,
  currentGate: textContent(currentGate),
};
""",
        )
        assert result["pollMs"] == 3_000
        assert result["projected"]["budget"] == "30m 00s"
        assert result["projected"]["budgetPercent"] == 50
        assert result["projected"]["gates"] == {
            "total": 4,
            "completed": 2,
            "current": "focused tests",
            "verdict": "passed",
        }
        assert result["meterWidth"] == "50%"
        assert result["markCount"] == 4
        assert result["measuredMarkCount"] == 2
        assert "focused tests" in result["currentGate"]

    _assert_mutation_is_rejected(
        source,
        lambda candidate: _mutate_once(
            candidate,
            r"Math\.min\(1, Math\.max\(0, elapsed / budgetSeconds\)\)",
            "0",
        ),
        contract,
    )
