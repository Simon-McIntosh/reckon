import json
import shutil
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import pytest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "ui" / "shell.jsx"


@contextmanager
def _served_spa(tmp_path: Path):
    port = _unused_port()
    mounts = tmp_path / "mounts.json"
    mounts.write_text(json.dumps({"reckon": str(ROOT / "docs")}), encoding="utf-8")
    server = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from reckon.serve import main; main(port=int(__import__('sys').argv[1]), "
            "host='127.0.0.1', mounts_file=__import__('pathlib').Path(__import__('sys').argv[2]))",
            str(port),
            str(mounts),
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    url = f"http://127.0.0.1:{port}/reckon/#cockpit"
    deadline = time.monotonic() + 15
    try:
        while time.monotonic() < deadline:
            if server.poll() is not None:
                stderr = server.stderr.read() if server.stderr else ""
                raise AssertionError(f"reckon server exited before readiness: {stderr}")
            try:
                with urlopen(url, timeout=0.5) as response:
                    if response.status == 200:
                        break
            except URLError:
                time.sleep(0.1)
        else:
            raise AssertionError("reckon server did not become ready within 15 seconds")
        yield url
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)


def _function_source(name: str) -> str:
    source = SOURCE.read_text()
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


def _evaluate(functions: list[str], expression: str):
    script = "\n".join(_function_source(name) for name in functions)
    result = subprocess.run(
        ["node", "-e", f"{script}\nconsole.log(JSON.stringify({expression}));"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_every_active_sprint_survives_the_overview_projection() -> None:
    state = {
        "active_sprint_id": "middle",
        "active_sprint_conflict": True,
        "sprints": [
            {"id": "first", "status": "active"},
            {"id": "middle", "status": "active"},
            {"id": "last", "status": "active"},
            {"id": "closed", "status": "done"},
        ],
    }
    result = _evaluate(
        ["projectActiveSprints"],
        f"projectActiveSprints({json.dumps(state)})",
    )

    assert [sprint["id"] for sprint in result["active"]] == [
        "first",
        "middle",
        "last",
    ]
    assert result["focus"] == "middle"
    assert result["conflict"] is True


def test_legacy_focus_and_conflict_warning_link_the_sprint_resources() -> None:
    source = SOURCE.read_text()

    assert "sprint.id === row.focus" in source
    assert "legacy focus" in source
    assert 'className="r-overview-conflict" role="alert"' in source
    assert "row.active.map(sprint => sprint.id)" in source
    assert 'href={`#sprint/${id}`}' in source


def test_unresolved_blocker_keeps_summary_owner_gated_count_and_next_action() -> None:
    blocker = {
        "id": "capacity",
        "summary": "No execution seat is available",
        "owner": "operations",
        "n": 3,
        "next": "Open one execution seat",
    }
    result = _evaluate(
        ["blockerIsUnresolved"],
        f"blockerIsUnresolved({json.dumps(blocker)})",
    )
    source = SOURCE.read_text()

    assert result is True
    assert "blocker.summary" in source
    assert "blocker.owner" in source
    assert "blocker.n" in source
    assert "blocker.next" in source


def test_resolved_blockers_do_not_enter_project_rows() -> None:
    current = {
        "project": "sample",
        "inventory": [],
        "sprints": [],
        "blockers": [
            {"id": "open", "summary": "Still open", "next": "Act"},
            {
                "id": "closed",
                "summary": "Resolved: service restored",
                "next": "Resolved with no further action",
            },
        ],
    }
    result = _evaluate(
        [
            "blockerIsUnresolved",
            "projectActiveSprints",
            "blockerGatedPlans",
            "overviewProjectRows",
        ],
        f"overviewProjectRows([], {json.dumps(current)}, []).flatMap(row => row.blockers.map(blocker => blocker.id))",
    )

    assert result == ["open"]


def test_blockers_are_projected_by_the_plan_they_gate() -> None:
    state = {
        "project": "sample",
        "inventory": [],
        "sprints": [
            {
                "id": "current",
                "items": [
                    {"slug": "chosen", "blocked_by": ["shared"]},
                    {"slug": "other", "blocked_by": ["unrelated"]},
                ],
            }
        ],
        "blockers": [
            {"id": "shared", "summary": "Chosen blocker", "next": "Act"},
            {"id": "unrelated", "summary": "Other blocker", "next": "Wait"},
        ],
    }
    expression = (
        "(() => { const rows = overviewProjectRows([], "
        f"{json.dumps(state)}, []); return {{ scopes: overviewBlockerScopes(rows), "
        "chosen: blockersForPlanScope(rows, 'sample:chosen').map(blocker => blocker.id), "
        "other: blockersForPlanScope(rows, 'sample:other').map(blocker => blocker.id) }; })()"
    )
    result = _evaluate(
        [
            "blockerIsUnresolved",
            "projectActiveSprints",
            "blockerGatedPlans",
            "overviewProjectRows",
            "overviewBlockerScopes",
            "blockersForPlanScope",
        ],
        expression,
    )

    assert [scope["key"] for scope in result["scopes"]] == [
        "sample:chosen",
        "sample:other",
    ]
    assert result["chosen"] == ["shared"]
    assert result["other"] == ["unrelated"]


def _browser_scope_measurement(browser: str, url: str) -> dict:
    port = _unused_port()
    process = subprocess.Popen(
        [
            browser,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            f"--remote-debugging-port={port}",
            "--window-size=1374,1100",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    protocol = r"""
const [port, pageUrl] = process.argv.slice(1);
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
const target = targets.find(candidate => candidate.type === "page");
const socket = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((resolve, reject) => {
  socket.addEventListener("open", resolve, { once: true });
  socket.addEventListener("error", reject, { once: true });
});
let nextId = 1;
const pending = new Map();
socket.addEventListener("message", event => {
  const message = JSON.parse(event.data);
  const resolver = pending.get(message.id);
  if (!resolver) return;
  pending.delete(message.id);
  message.error ? resolver.reject(message.error) : resolver.resolve(message.result);
});
const call = (method, params = {}) => new Promise((resolve, reject) => {
  const id = nextId++;
  pending.set(id, { resolve, reject });
  socket.send(JSON.stringify({ id, method, params }));
});
const evaluate = async expression => {
  const result = await call("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text);
  return result.result.value;
};
await call("Page.enable");
await call("Runtime.enable");
await call("Emulation.setDeviceMetricsOverride", { width: 1374, height: 1100, deviceScaleFactor: 1, mobile: false });
await call("Page.navigate", { url: pageUrl });
for (let attempt = 0; attempt < 200; attempt++) {
  if (await evaluate("Boolean(window.React && window.ReactDOM && typeof OverviewFleet === 'function')")) break;
  await delay(100);
}
const result = await evaluate(`(async () => {
  const blocker = (id, plan, owner, next) => ({
    id, summary: id + " summary", owner, next, n: 1, gated_plans: [plan]
  });
  const alpha = {
    project: "alpha", inventory: [], sprints: [],
    blockers: [blocker("alpha-blocker", "alpha-plan", "alpha-owner", "alpha-action")]
  };
  const beta = {
    project: "beta", inventory: [], sprints: [],
    blockers: [blocker("beta-blocker", "beta-plan", "beta-owner", "beta-action")]
  };
  const noise = Array.from({ length: 18 }, (_, index) =>
    blocker("noise-" + index, "noise-plan-" + index, "noise-owner", "noise-action")
  );
  alpha.blockers.push(...noise);
  window.STATE = alpha;
  document.body.innerHTML = '<main id="scope-check"></main>';
  ReactDOM.createRoot(document.querySelector("#scope-check")).render(
    React.createElement(OverviewFleet, {
      projects: [{ project: "alpha", state: alpha }, { project: "beta", state: beta }],
      fleetRuns: [], mountedProjectCount: 2
    })
  );
  const waitFor = async predicate => {
    for (let attempt = 0; attempt < 100; attempt++) {
      if (predicate()) return;
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    throw new Error("timed out waiting for scoped blockers");
  };
  await waitFor(() => document.querySelectorAll(".r-overview-blockers article").length === 1);
  const ids = () => [...document.querySelectorAll(".r-overview-blocker-id")].map(node => node.textContent);
  const region = document.querySelector(".r-overview-blockers");
  const viewHeight = 1033;
  const first = {
    ids: ids(), height: region.getBoundingClientRect().height,
    owner: document.querySelector(".r-overview-blocker-owner").textContent,
    gated: document.querySelector(".r-overview-blocker-meta span:nth-child(3)").textContent,
    next: document.querySelector(".r-overview-blocker-next").textContent
  };
  const select = document.querySelector('select[aria-label="Plan scope for unresolved blockers"]');
  select.value = "beta:beta-plan";
  select.dispatchEvent(new Event("change", { bubbles: true }));
  await waitFor(() => ids()[0] === "beta-blocker");
  return { viewHeight, first, secondIds: ids() };
})()`);
process.stdout.write(JSON.stringify(result));
socket.close();
"""
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                with urlopen(f"http://127.0.0.1:{port}/json/list", timeout=0.5):
                    break
            except URLError:
                time.sleep(0.1)
        else:
            raise AssertionError("browser debugging endpoint did not become ready")
        result = subprocess.run(
            ["node", "--input-type=module", "-e", protocol, str(port), url],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=75,
        )
        return json.loads(result.stdout)
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _unused_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def test_rendered_blockers_stay_below_one_view_and_change_with_plan_scope(
    tmp_path: Path,
) -> None:
    browser = next(
        (
            path
            for name in ("google-chrome", "chromium", "chromium-browser")
            if (path := shutil.which(name))
        ),
        None,
    )
    if browser is None:
        pytest.skip("rendered blocker check requires an installed browser")

    with _served_spa(tmp_path) as url:
        measurement = _browser_scope_measurement(browser, url)

    assert measurement["first"]["height"] < measurement["viewHeight"]
    assert measurement["first"]["ids"] == ["alpha-blocker"]
    assert measurement["secondIds"] == ["beta-blocker"]
    assert measurement["first"]["owner"] == "Owner: alpha-owner"
    assert measurement["first"]["gated"] == "1 gated"
    assert measurement["first"]["next"] == "Nextalpha-action"
