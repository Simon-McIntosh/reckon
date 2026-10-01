"""Render-contract verdicts reach the reader and the list as the server sent them.

The server computes each document's verdict; the loader attaches it to the row
it belongs to, the list marks a failing row, and the reader lists the errors.
None of these derives a verdict of its own.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.spa_browser_harness import file_spa, installed_browser_or_skip

REPO_ROOT = Path(__file__).resolve().parents[1]
UI = REPO_ROOT / "docs" / "ui"
PLAN_SOURCE = (UI / "plan.jsx").read_text()
PLANS_SOURCE = (UI / "shell-plans.jsx").read_text()

_FINDINGS = [
    {
        "severity": "error",
        "code": "section-unclosed",
        "message": '<section id="s1"> is never closed',
    },
    {"severity": "warn", "code": "pre-long-line", "message": "a long line"},
    {
        "severity": "error",
        "code": "meta-missing",
        "message": 'missing required <meta name="plan-status">',
    },
]


def _function_source(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated function {name}")


def _node(script: str) -> object:
    result = subprocess.run(
        ["node", "-e", script],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_reader_lists_only_the_error_findings():
    helper = _function_source(PLAN_SOURCE, "readerComplianceErrors")
    check = {"errors": 2, "warnings": 1, "findings": _FINDINGS}
    errors, clean, absent = _node(
        f"{helper}\nconsole.log(JSON.stringify(["
        f"readerComplianceErrors({json.dumps(check)}),"
        f"readerComplianceErrors({{errors: 0, warnings: 3, findings: []}}),"
        f"readerComplianceErrors(null)]));"
    )
    assert [f["code"] for f in errors] == ["section-unclosed", "meta-missing"]
    assert clean == absent == []


def test_row_title_names_each_error_and_no_warning():
    helper = _function_source(PLANS_SOURCE, "artifactCheckTitle")
    title = _node(
        f"{helper}\nconsole.log(JSON.stringify(artifactCheckTitle({json.dumps({'findings': _FINDINGS})})));"
    )
    assert title.splitlines() == [
        'section-unclosed: <section id="s1"> is never closed',
        'meta-missing: missing required <meta name="plan-status">',
    ]


def test_loader_attaches_each_verdict_to_its_own_row_and_clears_the_rest():
    discovery = {
        "inventory": [
            {"slug": "work", "type": "plan", "title": "Work", "status": "active"},
            {"slug": "work", "type": "research", "title": "Same slug", "status": ""},
            {
                "slug": "clean",
                "type": "plan",
                "title": "Clean",
                "status": "active",
                "check": {"errors": 9},
            },
        ]
    }
    checks = {
        "checked": 3,
        "pending": 0,
        "failing": 1,
        "refreshing": False,
        "documents": [
            {
                "type": "research",
                "slug": "work",
                "archived": False,
                "path": "research/work.html",
                "errors": 2,
                "warnings": 1,
                "findings": _FINDINGS,
            }
        ],
    }
    script = f"""
const events = [];
global.window = {{
  location: {{ pathname: "/proj/", href: "http://x/proj/" }},
  dispatchEvent: event => events.push(event.type),
}};
global.CustomEvent = class {{ constructor(type, init) {{ this.type = type; this.detail = init?.detail; }} }};
global.document = {{ querySelector: () => null }};
const respond = (status, body) => Promise.resolve({{
  ok: status === 200, status, json: async () => body,
}});
const requested = [];
global.fetch = url => {{
  requested.push(url);
  if (url.startsWith("/_index/")) return respond(404, null);
  if (url.startsWith("/_discover/")) return respond(200, {json.dumps(discovery)});
  if (url.startsWith("/_checks/")) return respond(200, {json.dumps(checks)});
  return respond(404, null);
}};
eval(require("fs").readFileSync({json.dumps(str(UI / "state-loader.js"))}, "utf8"));
window.STATE_READY.then(async () => {{
  const loadRequests = requested.slice();
  await window.loadDocumentChecks("proj");
  const rows = window.STATE.inventory.map(row => [row.type, row.slug, row.check ? row.check.errors : null]);
  console.log(JSON.stringify({{ rows, checks: window.STATE.checks, events, loadRequests }}));
}});
"""
    outcome = _node(script)
    assert sorted(map(tuple, outcome["rows"])) == [
        ("plan", "clean", None),
        ("plan", "work", None),
        ("research", "work", 2),
    ]
    assert outcome["checks"] == {"checked": 3, "pending": 0, "failing": 1}
    assert "reckon:checks" in outcome["events"]
    # The load sequence itself never asks for verdicts; the shell does, later.
    assert not any(url.startswith("/_checks/") for url in outcome["loadRequests"])


def test_shell_asks_for_verdicts_only_after_derived_state_settles():
    shell = (UI / "shell.jsx").read_text()
    trigger = shell[shell.index("const loadChecksAfterDerived") :]
    trigger = trigger[: trigger.index("), []);")]
    assert "Promise.resolve(window.STATE_DERIVED_READY)" in trigger
    assert trigger.index("STATE_DERIVED_READY") < trigger.index("loadDocumentChecks")


@pytest.fixture(scope="module")
def browser() -> str:
    return installed_browser_or_skip()


def _state(check: dict | None = None) -> dict:
    row = {
        "slug": "work",
        "title": "Work plan",
        "type": "plan",
        "status": "active",
        "effective_status": "active",
        "sprint": "current",
    }
    if check is not None:
        row["check"] = check
    return {"project": "reckon", "inventory": [row], "sprints": [], "milestones": []}


def _reader_preload(check: dict) -> str:
    return f"""
      const nativeFetch = window.fetch.bind(window);
      const json = body => Promise.resolve(new Response(JSON.stringify(body), {{
        status: 200, headers: {{'Content-Type': 'application/json'}},
      }}));
      window.fetch = (resource, options) => {{
        const url = new URL(String(resource), window.location.href);
        if (url.pathname === '/_checks/reckon/plans/work') return json({json.dumps(check)});
        if (url.pathname.startsWith('/crew')) return json({{runs: []}});
        if (url.pathname.startsWith('/plan/reckon/')) return json({{
          version: 1, decisions: [], comments: {{}}, gates: [], followups: [],
        }});
        if (url.pathname.endsWith('.html')) return Promise.resolve(new Response(
          '<main class="plan-doc"><p>Body</p></main>',
          {{status: 200, headers: {{'Content-Type': 'text/html'}}}},
        ));
        return nativeFetch(resource, options);
      }};
    """


def test_reader_shows_the_render_contract_errors(tmp_path: Path, browser: str):
    check = {
        "project": "reckon",
        "type": "plan",
        "slug": "work",
        "path": "docs/plans/work.html",
        "errors": 2,
        "warnings": 1,
        "findings": _FINDINGS,
    }
    probe = """(() => {
      const banner = document.querySelector('.r-reader-compliance');
      return {
        text: banner ? banner.innerText : '',
        items: banner ? banner.querySelectorAll('li').length : 0,
      };
    })()"""
    with file_spa(tmp_path, browser, _state(), route="#plan/work") as spa:
        measured = spa.run_probe(
            probe,
            ready_expression="Boolean(document.querySelector('.r-reader-compliance'))",
            preload_expression=_reader_preload(check),
        )
    assert "fails the render contract — 2 errors" in measured["text"]
    assert "reckon audit-doc docs/plans/work.html" in measured["text"]
    assert measured["items"] == 2


def test_list_row_shows_its_contract_errors(tmp_path: Path, browser: str):
    check = {"errors": 2, "warnings": 1, "findings": _FINDINGS}
    probe = """(() => {
      const badge = document.querySelector('.r-artifact-row .r-artifact-check');
      return { text: badge ? badge.innerText : '', title: badge ? badge.title : '' };
    })()"""
    with file_spa(tmp_path, browser, _state(check), route="#plans") as spa:
        measured = spa.run_probe(
            probe,
            ready_expression="Boolean(document.querySelector('.r-artifact-row'))",
        )
    assert measured["text"] == "2 contract errors"
    assert measured["title"].startswith("section-unclosed:")
