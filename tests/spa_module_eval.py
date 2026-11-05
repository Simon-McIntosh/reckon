from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from reckon.serve import compile_jsx

ROOT = Path(__file__).parents[1]

# A React that records the element tree instead of mounting it, the browser
# globals a UI module reaches for at load, and the tree readers a probe uses to
# find the drawn nodes. ``__stateSeed`` stands in for the one piece of React
# state a test needs to set before a component renders: leave it undefined and
# each hook keeps its initial value, set it and every hook reads the seed.
REACT_TREE_PRELUDE = r"""
const noop = () => {};
globalThis.React = {
  createElement(type, props, ...children) { return { type, props: props || {}, children }; },
  Fragment: Symbol("Fragment"),
  useState(value) {
    const initial = typeof value === "function" ? value() : value;
    return [globalThis.__stateSeed === undefined ? initial : globalThis.__stateSeed, noop];
  },
  useMemo(factory) { return factory(); },
  useEffect() {},
  useLayoutEffect() {},
  useCallback(fn) { return fn; },
  useRef(value) { return { current: value }; },
};
globalThis.navigator = { clipboard: { writeText: noop } };
globalThis.fetch = async () => ({ ok: true, json: async () => ({ runs: [] }) });
window.setInterval = noop;
window.clearInterval = noop;
window.setTimeout = noop;
window.flashSaved = noop;
window.ReckonShell = window.ReckonShell || { route: { ARTIFACT_ROUTES: [] } };

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

function textOf(node) {
  if (node == null || node === false || node === true) return "";
  if (Array.isArray(node)) return node.map(textOf).join("");
  if (typeof node !== "object") return String(node);
  return (node.children || []).map(textOf).join("");
}
"""


def evaluate_jsx_module(
    source_path: Path,
    expression: str,
    *,
    prelude: str = "",
    source_suffix: str = "",
    bindings: Mapping[str, Any] | None = None,
    timeout: float = 60.0,
) -> Any:
    """Compile a JSX module and evaluate an expression inside its module scope.

    ``prelude`` runs before the module, which is where a caller supplies the
    globals its components need. ``source_suffix`` is appended to the module
    source, which is how a caller exports a component the file declares without
    publishing it on ``window``. ``bindings`` are injected as JSON constants, so
    the expression can name the values it renders from.
    """

    source = source_path.read_text(encoding="utf-8") + source_suffix
    instrumented = f"{source}\nconsole.log(JSON.stringify({expression}));\n"
    compiled = compile_jsx(instrumented, filename=source_path.name).decode()
    injected = "".join(
        f"const {name} = {json.dumps(value)};\n"
        for name, value in (bindings or {}).items()
    )
    script = "\n".join(
        part
        for part in ("globalThis.window = globalThis;", prelude, injected, compiled)
        if part
    )
    result = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, result.args, output=result.stdout, stderr=result.stderr
        )
    return json.loads(result.stdout)


def evaluate_jsx_render(
    source_path: Path,
    probe: str,
    *,
    exports: str = "",
    bindings: Mapping[str, Any] | None = None,
) -> Any:
    """Render a component a JSX module defines and return what the probe reads.

    The module loads under ``REACT_TREE_PRELUDE``, so the probe can call the
    component, walk the drawn nodes with ``findAll``, ``hasClass`` and
    ``textOf``, and return plain JSON describing what the surface drew.
    """

    return evaluate_jsx_module(
        source_path,
        f"(() => {{{probe}}})()",
        prelude=REACT_TREE_PRELUDE,
        source_suffix=exports,
        bindings=bindings,
    )
