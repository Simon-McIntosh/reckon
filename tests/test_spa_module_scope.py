from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import serve
from tests.spa_browser_harness import _evaluate_browser_url, installed_browser

ROOT = Path(__file__).parents[1]
UI_ROOT = ROOT / "docs" / "ui"
MODULE_ORDER = (
    "glyphs",
    "_shared",
    "prompts",
    "ui",
    "bits",
    "decision",
    "plan",
    "sprint",
    "graph",
    "crew",
    "shell",
)
WINDOW_EXPORTS = {
    "glyphs": ("GLYPHS", "ACCENTS"),
    "_shared": (
        "ProjectPicker",
        "ProjectVisibilitySheet",
        "SettingsMenu",
        "Sparkline",
        "Chip",
        "ProjectCard",
    ),
    "prompts": ("buildFleetPrompt", "buildFleetPromptAsync"),
    "ui": (
        "Status",
        "Roi",
        "Bar",
        "Stack",
        "Heat",
        "Spark",
        "Tag",
        "Who",
        "Icon",
        "Persist",
        "flashSaved",
    ),
    "bits": (
        "planSave",
        "planLoad",
        "withHandoffProvenance",
        "PromptModal",
        "CommentPopover",
        "CommentReviewPopover",
        "useSelectionToComment",
        "SectionComments",
    ),
    "decision": ("Decision", "DecisionRow"),
    "plan": ("Plan", "GenericBody"),
    "sprint": ("Sprint", "SprintView"),
    "graph": (
        "GraphView",
        "DependencyChainView",
        "CriticalPathView",
        "PathPromptModal",
        "RadialFan",
    ),
    "crew": ("CrewView",),
    "shell": (),
}
MODULE_DEPENDENCIES = {
    "bits": ("Who",),
    "plan": (
        "reckon.planLoad",
        "reckon.planSave",
        "Decision",
        "reckon.CommentPopover",
        "reckon.SectionComments",
    ),
    "shell": ("Plan", "Sprint", "GraphView", "CrewView"),
}
REACT_MEMBERS = (
    "useCallback",
    "useEffect",
    "useLayoutEffect",
    "useMemo",
    "useRef",
    "useState",
)


def _module_path(module: str) -> Path:
    suffix = ".js" if module == "prompts" else ".jsx"
    return UI_ROOT / f"{module}{suffix}"


def _compiled_modules() -> dict[str, str]:
    return {
        module: serve.compile_jsx(
            _module_path(module).read_text(encoding="utf-8"),
            filename=_module_path(module).name,
        ).decode()
        for module in MODULE_ORDER
        if module != "prompts"
    }


def test_react_members_are_bound_by_the_module_that_calls_them():
    compiled = _compiled_modules()
    parser = serve._client_asset("babel.js")
    script = r"""
const fs = require("fs");
const Babel = require(process.argv[1]);
const input = JSON.parse(fs.readFileSync(0, "utf8"));

function walk(node, visit) {
  if (!node || typeof node !== "object") return;
  if (node.type) visit(node);
  for (const [key, child] of Object.entries(node)) {
    if (key === "loc" || key === "start" || key === "end") continue;
    if (Array.isArray(child)) child.forEach(item => walk(item, visit));
    else if (child && typeof child === "object") walk(child, visit);
  }
}

const findings = [];
for (const [moduleName, source] of Object.entries(input.compiled)) {
  const ast = Babel.transform(source, {ast: true, code: false, sourceType: "script"}).ast;
  const bindings = new Set();
  const calls = [];
  walk(ast, node => {
    if (node.type === "VariableDeclarator" && node.id.type === "ObjectPattern"
        && node.init?.type === "Identifier" && node.init.name === "React") {
      for (const property of node.id.properties) {
        if (property.type === "ObjectProperty" && property.value.type === "Identifier") {
          bindings.add(property.value.name);
        }
      }
    } else if (node.type === "CallExpression" && node.callee.type === "Identifier"
               && input.members.includes(node.callee.name)) {
      calls.push({member: node.callee.name, line: node.loc.start.line});
    }
  });
  for (const call of calls) {
    if (!bindings.has(call.member)) findings.push({module: moduleName, ...call});
  }
}
process.stdout.write(JSON.stringify(findings));
"""
    result = subprocess.run(
        ["node", "-e", script, str(parser)],
        input=json.dumps({"compiled": compiled, "members": REACT_MEMBERS}),
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    )

    assert json.loads(result.stdout) == []


def test_cross_module_references_are_qualified():
    compiled = _compiled_modules()
    owners = {
        export: module
        for module, exports in WINDOW_EXPORTS.items()
        for export in exports
    }
    parser = serve._client_asset("babel.js")
    script = r"""
const fs = require("fs");
const Babel = require(process.argv[1]);
const input = JSON.parse(fs.readFileSync(0, "utf8"));

function isReference(parent, key) {
  if (!parent) return true;
  if ((parent.type === "MemberExpression" || parent.type === "OptionalMemberExpression")
      && key === "property" && !parent.computed) return false;
  if ((parent.type === "ObjectProperty" || parent.type === "ObjectMethod")
      && key === "key" && !parent.computed) return false;
  if ((parent.type === "FunctionDeclaration" || parent.type === "FunctionExpression")
      && (key === "id" || key === "params")) return false;
  if (parent.type === "ArrowFunctionExpression" && key === "params") return false;
  if (parent.type === "VariableDeclarator" && key === "id") return false;
  if ((parent.type === "ClassDeclaration" || parent.type === "ClassExpression")
      && key === "id") return false;
  if (parent.type === "CatchClause" && key === "param") return false;
  if ((parent.type === "LabeledStatement" || parent.type === "BreakStatement"
      || parent.type === "ContinueStatement") && key === "label") return false;
  return true;
}

function walk(node, parent, key, visit) {
  if (!node || typeof node !== "object") return;
  if (node.type) visit(node, parent, key);
  for (const [childKey, child] of Object.entries(node)) {
    if (childKey === "loc" || childKey === "start" || childKey === "end") continue;
    if (Array.isArray(child)) {
      child.forEach(item => walk(item, node, childKey, visit));
    } else if (child && typeof child === "object") {
      walk(child, node, childKey, visit);
    }
  }
}

const findings = [];
for (const [moduleName, source] of Object.entries(input.compiled)) {
  const ast = Babel.transform(source, {ast: true, code: false, sourceType: "script"}).ast;
  walk(ast, null, null, (node, parent, key) => {
    if (node.type !== "Identifier" || !isReference(parent, key)) return;
    const owner = input.owners[node.name];
    if (owner && owner !== moduleName) {
      findings.push({module: moduleName, owner, identifier: node.name, line: node.loc.start.line});
    }
  });
}
process.stdout.write(JSON.stringify(findings));
"""
    result = subprocess.run(
        ["node", "-e", script, str(parser)],
        input=json.dumps({"compiled": compiled, "owners": owners}),
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    )

    assert json.loads(result.stdout) == []


def test_compiled_modules_load_with_isolated_cross_module_references():
    compiled = _compiled_modules()
    functional_exports = {
        export
        for module, exports in WINDOW_EXPORTS.items()
        if module != "prompts"
        for export in exports
    }
    assert len(functional_exports) == 39

    prelude = r"""
const window = Object.create(null);
const noop = () => {};
window.location = {
  pathname: "/fixture/",
  href: "http://127.0.0.1/fixture/#plans",
  hash: "#plans",
  search: "",
  reload: noop,
  assign: noop,
};
const document = {
  querySelector() { return null; },
  getElementById() { return {}; },
  createElement() { return { dataset: {}, style: {}, appendChild() {} }; },
  body: { appendChild() {} },
  addEventListener() {},
  removeEventListener() {},
};
const localStorage = { getItem() { return null; }, setItem() {} };
const navigator = { clipboard: null };
const alert = noop;
const fetch = async () => ({ ok: false, json: async () => ({}) });
const React = {
  createElement(type, props, ...children) { return { type, props, children }; },
  Fragment: Symbol("Fragment"),
  useState(value) { return [typeof value === "function" ? value() : value, noop]; },
  useEffect: noop,
  useLayoutEffect: noop,
  useMemo(fn) { return fn(); },
  useRef(value) { return { current: value }; },
  useCallback(fn) { return fn; },
};
const ReactDOM = { createRoot() { return { render: noop }; } };

function resolveDependency(path) {
  return path.split(".").reduce((value, part) => value && value[part], window);
}
function assertDependencies(moduleName, paths) {
  const missing = paths.filter(path => resolveDependency(path) == null);
  if (missing.length) {
    throw new ReferenceError(`${moduleName} missing cross-module components: ${missing.join(", ")}`);
  }
}
function assertExports(moduleName, names) {
  const missing = names.filter(name => !(name in window));
  if (missing.length) throw new Error(`${moduleName} missing exports: ${missing.join(", ")}`);
}
"""
    bundle = [prelude]
    for module in MODULE_ORDER:
        dependencies = MODULE_DEPENDENCIES.get(module, ())
        bundle.append(
            f"assertDependencies({json.dumps(module)}, {json.dumps(dependencies)});"
        )
        bundle.append(
            _module_path(module).read_text(encoding="utf-8")
            if module == "prompts"
            else compiled[module]
        )
        bundle.append(
            f"assertExports({json.dumps(module)}, {json.dumps(WINDOW_EXPORTS[module])});"
        )
    bundle.append(
        "process.stdout.write(JSON.stringify({"
        "functionalExports: "
        f"{len(functional_exports)}, modules: {len(MODULE_ORDER)}"
        "}));"
    )

    result = subprocess.run(
        ["node"],
        cwd=ROOT,
        input="\n".join(bundle),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "functionalExports": 39,
        "modules": len(MODULE_ORDER),
    }


def test_inlined_bundle_renders_from_a_file_url_without_reference_errors(tmp_path):
    browser = installed_browser()
    if browser is None:
        pytest.fail(
            "a supported headless browser is required for the module-scope gate"
        )

    compiled = _compiled_modules()
    scripts = [
        serve._client_asset("react.js").read_text(encoding="utf-8"),
        serve._client_asset("react-dom.js").read_text(encoding="utf-8"),
        """
window.__referenceErrors = [];
window.addEventListener("error", event => {
  if (event.error instanceof ReferenceError) window.__referenceErrors.push(event.error.message);
});
window.addEventListener("unhandledrejection", event => {
  if (event.reason instanceof ReferenceError) window.__referenceErrors.push(event.reason.message);
});
window.STATE = {
  project: "fixture",
  projects: [{project: "fixture", milestones: []}],
  inventory: [],
  sprints: [],
  milestones: [],
  north_stars: [],
  timeline: [],
};
window.STATE_READY = null;
window.STATE_ERROR = null;
""",
    ]
    scripts.extend(
        (
            _module_path(module).read_text(encoding="utf-8")
            if module == "prompts"
            else compiled[module]
        )
        for module in MODULE_ORDER
    )
    scripts.append(
        "window.setTimeout(() => { document.documentElement.dataset.probeReady = '1'; }, 250);"
    )
    script_tags = "\n".join(
        f"<script>{source.replace('</script', '<\\/script')}</script>"
        for source in scripts
    )
    page = tmp_path / "inlined-spa.html"
    page.write_text(
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='docs-project' content='fixture'></head>"
        f"<body><div id='root'></div>{script_tags}</body></html>",
        encoding="utf-8",
    )

    result = _evaluate_browser_url(
        tmp_path,
        browser,
        page.resolve().as_uri(),
        "({referenceErrors: window.__referenceErrors, rootChildren: document.getElementById('root').childElementCount})",
        viewport=(1374, 900),
        ready_expression="document.documentElement.dataset.probeReady === '1'",
    )

    assert result == {"referenceErrors": [], "rootChildren": 1}
