"""Browser-free checks that a mounted shell always renders a visible root."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from reckon.serve import compile_jsx

ROOT = Path(__file__).parents[1]
SHELL = ROOT / "docs" / "ui" / "shell.jsx"


def _project_payload(project: str, *, unrelated_title: str) -> dict[str, object]:
    return {
        "project": project,
        "projects": [
            {"project": project, "title": "Selected project"},
            {"project": "other", "title": unrelated_title},
        ],
        "inventory": [],
        "sprints": [],
    }


def _render_root(
    source: str, *, payload: dict[str, object], available_modules: set[str]
) -> dict[str, Any]:
    compiled = compile_jsx(source, filename="shell.jsx").decode()
    script = (
        r"""
const window = Object.create(null);
const noop = () => {};
let rendered = null;
const input = JSON.parse(process.argv[1]);
window.STATE = input.payload;
window.ReckonShell = input.modules.includes("shell-ready")
  ? {ready: {ReadyGate() { return null; }}}
  : {};
const document = { getElementById() { return {}; }, querySelector() { return null; } };
const React = {
  createElement(type, props, ...children) { return { type, props: props || {}, children }; },
  useCallback: value => value,
  useEffect: noop,
  useMemo: value => value(),
  useRef: value => ({ current: value }),
  useState: value => [value, noop],
};
const ReactDOM = { createRoot() { return { render(value) { rendered = value; } }; } };
"""
        + compiled
        + r"""
process.stdout.write(JSON.stringify({
  childCount: rendered?.children?.length || 0,
  moduleState: rendered?.props?.["data-shell-modules"] || null,
  role: rendered?.props?.role || null,
}));
"""
    )
    result = subprocess.run(
        [
            "node",
            "-e",
            script,
            json.dumps({"payload": payload, "modules": sorted(available_modules)}),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(result.stdout)


def _assert_nonempty_surface(
    source: str, *, payload: dict[str, object], available_modules: set[str]
) -> dict[str, Any]:
    receipt = _render_root(
        source,
        payload=payload,
        available_modules=available_modules,
    )
    assert receipt["childCount"] > 0, "mounted project surface rendered no children"
    return receipt


def _silent_fallback_source(source: str) -> str:
    """Return the original empty root shape for the recorded module skew."""
    start = source.index("  : React.createElement(\n", source.index("const shellRoot"))
    end = source.index("\n\nReactDOM.createRoot", start)
    return (
        source[:start]
        + '  : React.createElement("main", {"data-shell-modules": "unavailable"});'
        + source[end:]
    )


def test_blank_surface_guard_rejects_the_recorded_module_skew() -> None:
    source = SHELL.read_text(encoding="utf-8")
    payload = _project_payload("nova", unrelated_title="Unchanged neighbour")
    modules = {"shell"}

    repaired = _assert_nonempty_surface(
        source,
        payload=payload,
        available_modules=modules,
    )
    reverted = _render_root(
        _silent_fallback_source(source),
        payload=payload,
        available_modules=modules,
    )

    assert repaired == {
        "childCount": 4,
        "moduleState": "unavailable",
        "role": "alert",
    }
    assert reverted["childCount"] == 0
    with pytest.raises(AssertionError, match="rendered no children"):
        _assert_nonempty_surface(
            _silent_fallback_source(source),
            payload=payload,
            available_modules=modules,
        )


def test_blank_surface_guard_ignores_unrelated_project_and_module_changes() -> None:
    source = SHELL.read_text(encoding="utf-8")
    payload = _project_payload("nova", unrelated_title="Original neighbour")
    changed_payload = _project_payload("nova", unrelated_title="Changed neighbour")
    modules = {"shell"}

    receipts = [
        _assert_nonempty_surface(
            source,
            payload=payload,
            available_modules=modules,
        ),
        _assert_nonempty_surface(
            source,
            payload=changed_payload,
            available_modules=modules,
        ),
        _assert_nonempty_surface(
            source,
            payload=payload,
            available_modules={*modules, "unrelated-module"},
        ),
    ]

    assert [receipt["childCount"] for receipt in receipts] == [4, 4, 4]
