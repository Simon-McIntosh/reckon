from __future__ import annotations

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

from tests.spa_browser_harness import temporary_browser_profile


ROOT = Path(__file__).resolve().parents[1]
BROWSER_NAMES = ("google-chrome", "chromium", "chromium-browser")

PLAN_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="docs-project" content="reckon">
  <meta name="reckon-type" content="plan">
  <meta name="plan-slug" content="rendered-contract">
  <meta name="plan-title" content="Rendered contract">
  <meta name="plan-summary" content="Exercise the composed semantic surface.">
  <meta name="plan-status" content="active">
  <meta name="plan-impl" content="0.42">
  <meta name="plan-version" content="7">
  <meta name="plan-roi" content="high">
  <meta name="plan-effort-hours" content="3.25">
  <meta name="plan-sprint" content="focus">
  <meta name="plan-modified" content="2026-08-25">
  <title>Rendered contract | reckon</title>
</head>
<body>
<main class="plan-doc">
  <h2 id="implementation">Implementation</h2>
  <p>The authored body remains visible when structured state is available.</p>
  <section data-reckon="gates" id="gates" class="r-gates">
    <h2 id="gate-state-heading"><span class="sec">§</span> Evidence gates</h2>
    <div class="r-gate" data-id="rendered-contract" data-section="implementation"
         data-gated-sections="implementation" data-status="open" data-verdict="">
      <h4 class="r-gate-measure">Rendered contract remains visible</h4>
      <p class="r-gate-required-evidence">A composed-page assertion</p>
    </div>
  </section>
  <section data-reckon="followups" id="followups" class="r-followups">
    <h2><span class="sec">§</span> Followups</h2>
    <article class="r-fu" data-id="next" data-status="open"
             data-written-by="tester" data-written-at="2026-08-25"
             data-recommends-skill="/reckon-ship rendered-contract"
             data-resolved-at="" data-resolved-by="">
      <h4 class="r-fu-title">Continue the rendered contract</h4>
      <div class="r-fu-body">Keep the semantic output observable.</div>
      <pre class="r-fu-prompt">/reckon-ship rendered-contract</pre>
    </article>
  </section>
</main>
</body>
</html>
"""

INDEX_STATE = {
    "updated": "2026-08-25T00:00:00",
    "project": "reckon",
    "doc": "index",
    "data": {
        "active_sprint_id": "focus",
        "projects": [
            {
                "project": "reckon",
                "owner": "Test owner",
                "plans_count": 1,
                "active": 1,
                "blocked": 0,
                "pending": 0,
                "shipped": 0,
                "milestones": [],
            }
        ],
        "sprints": [
            {
                "id": "focus",
                "theme": "Stored focus",
                "status": "active",
                "starts": "2026-08-25",
                "ends": "2026-08-26",
                "items": [{"slug": "rendered-contract"}],
            },
            {
                "id": "concurrent",
                "theme": "Concurrent work",
                "status": "active",
                "starts": "2026-08-25",
                "ends": "2026-08-27",
                "items": [],
            },
        ],
        "milestones": [],
        "blockers": [],
        "timeline": [],
        "north_stars": [],
    },
}

NODE_PROBE = r"""
import { spawn } from "node:child_process";
import { access, readFile } from "node:fs/promises";
import path from "node:path";

const input = JSON.parse(process.argv[1]);
const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));

async function waitForFile(file, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      return await readFile(file, "utf8");
    } catch {
      await delay(100);
    }
  }
  throw new Error(`timed out waiting for ${file}`);
}

class DevTools {
  constructor(url) {
    this.nextId = 1;
    this.pending = new Map();
    this.socket = new WebSocket(url);
  }

  async open() {
    await new Promise((resolve, reject) => {
      this.socket.addEventListener("open", resolve, { once: true });
      this.socket.addEventListener("error", reject, { once: true });
    });
    this.socket.addEventListener("message", event => {
      const message = JSON.parse(event.data);
      const pending = this.pending.get(message.id);
      if (!pending) return;
      this.pending.delete(message.id);
      if (message.error) pending.reject(new Error(JSON.stringify(message.error)));
      else pending.resolve(message.result);
    });
  }

  call(method, params = {}) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.socket.send(JSON.stringify({ id, method, params }));
    });
  }

  close() {
    this.socket.close();
  }
}

async function evaluate(devtools, expression) {
  const response = await devtools.call("Runtime.evaluate", {
    expression,
    returnByValue: true,
    awaitPromise: true,
  });
  if (response.exceptionDetails) {
    throw new Error(response.exceptionDetails.exception?.description || "browser evaluation failed");
  }
  return response.result.value;
}

async function navigateAndWait(devtools, generation) {
  const [base, fragment = ""] = input.url.split("#", 2);
  const separator = base.includes("?") ? "&" : "?";
  const url = `${base}${separator}semantic_generation=${generation}${fragment ? `#${fragment}` : ""}`;
  await devtools.call("Page.navigate", { url });
  const deadline = Date.now() + 45000;
  while (Date.now() < deadline) {
    const ready = await evaluate(devtools, `Boolean(
      location.search.includes("semantic_generation=${generation}")
      && window.STATE
      && document.querySelector(${JSON.stringify(input.waitSelector)})
    )`);
    if (ready) return;
    await delay(150);
  }
  throw new Error(`timed out waiting for ${input.waitSelector}`);
}

async function main() {
  await access(input.browser);
  const profile = input.profile;
  const browser = spawn(input.browser, [
    "--headless=new",
    "--no-sandbox",
    "--disable-gpu",
    "--remote-debugging-port=0",
    `--user-data-dir=${profile}`,
    "--window-size=1374,900",
    "about:blank",
  ], { stdio: ["ignore", "ignore", "ignore"] });

  let devtools;
  try {
    const activePort = await waitForFile(path.join(profile, "DevToolsActivePort"), 15000);
    const [port] = activePort.trim().split(/\s+/);
    const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    const target = targets.find(candidate => candidate.type === "page");
    if (!target) throw new Error("browser did not expose a page target");

    devtools = new DevTools(target.webSocketDebuggerUrl);
    await devtools.open();
    await devtools.call("Page.enable");
    await devtools.call("Runtime.enable");
    await devtools.call("Emulation.setDeviceMetricsOverride", {
      width: 1374,
      height: 900,
      deviceScaleFactor: 1,
      mobile: false,
    });
    await devtools.call("Page.addScriptToEvaluateOnNewDocument", {
      source: `{
          const fixtureIndex = ${JSON.stringify(input.fixtureIndex)};
          const fixtureSprints = fixtureIndex.data.sprints;
          const nativeFetch = window.fetch.bind(window);
          window.fetch = (resource, options) => {
            const url = String(resource);
            if (url.endsWith("/state/reckon/index.json")) {
              return Promise.resolve(new Response(JSON.stringify(fixtureIndex), {
                status: 200,
                headers: { "Content-Type": "application/json" },
              }));
            }
            if (url.endsWith("/_discover/reckon")) {
              return nativeFetch(resource, options).then(response => response.json()).then(payload =>
                new Response(JSON.stringify({
                  ...payload,
                  sprints: fixtureSprints,
                  active_sprint_id: fixtureIndex.data.active_sprint_id,
                  source_format: "distributed",
                  resource_versions: {
                    "project:reckon": 4,
                    "sprint:focus": 2,
                    "sprint:concurrent": 1,
                  },
                }), {
                  status: 200,
                  headers: { "Content-Type": "application/json" },
                })
              );
            }
            if (${JSON.stringify(input.failPlanHtml)}
                && url.includes("/plans/")
                && url.endsWith(".html")) {
              return Promise.resolve(new Response("", { status: 503 }));
            }
            return nativeFetch(resource, options);
          };
        }`,
    });

    await navigateAndWait(devtools, 1);
    const baseline = await evaluate(devtools, input.probe);

    await navigateAndWait(devtools, 2);
    await evaluate(devtools, input.removeSignal);
    const removed = await evaluate(devtools, input.probe);

    process.stdout.write(`${JSON.stringify({ baseline, removed })}\n`);
  } finally {
    if (devtools) devtools.close();
    browser.kill("SIGTERM");
    await new Promise(resolve => {
      if (browser.exitCode !== null) resolve();
      else browser.once("exit", resolve);
    });
  }
}

main().catch(error => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
"""


def _installed_browser() -> str:
    browser = next(
        (path for name in BROWSER_NAMES if (path := shutil.which(name))),
        None,
    )
    if browser is None:
        pytest.skip(
            "rendered semantic checks require an installed browser; tried "
            + ", ".join(BROWSER_NAMES)
        )
    return browser


def _unused_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _write_fixture_docs(tmp_path: Path) -> Path:
    docs = tmp_path / "docs"
    plans = docs / "plans"
    state = docs / "state" / "reckon"
    plans.mkdir(parents=True)
    state.mkdir(parents=True)
    (docs / "index.html").symlink_to(ROOT / "docs" / "index.html")
    (plans / "rendered-contract.html").write_text(PLAN_HTML, encoding="utf-8")
    (state / "index.json").write_text(
        json.dumps(INDEX_STATE),
        encoding="utf-8",
    )
    return docs


@contextmanager
def _served_fixture(tmp_path: Path, route: str):
    port = _unused_port()
    docs = _write_fixture_docs(tmp_path)
    mounts = tmp_path / "mounts.json"
    mounts.write_text(json.dumps({"reckon": str(docs)}), encoding="utf-8")
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
    url = f"http://127.0.0.1:{port}/reckon/{route}"
    deadline = time.monotonic() + 15
    try:
        while time.monotonic() < deadline:
            if server.poll() is not None:
                stderr = server.stderr.read() if server.stderr else ""
                raise AssertionError(f"reckon server exited before readiness: {stderr}")
            try:
                with urlopen(f"http://127.0.0.1:{port}/reckon/", timeout=0.5) as response:
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


def _rendered_probe(
    tmp_path: Path,
    *,
    route: str,
    wait_selector: str,
    probe: str,
    remove_signal: str,
    fail_plan_html: bool = False,
) -> dict[str, dict[str, object]]:
    with temporary_browser_profile(tmp_path) as profile:
        with _served_fixture(tmp_path, route) as url:
            result = subprocess.run(
                [
                    "node",
                    "--input-type=module",
                    "-e",
                    NODE_PROBE,
                    json.dumps(
                        {
                            "browser": _installed_browser(),
                            "profile": str(profile),
                            "url": url,
                            "waitSelector": wait_selector,
                            "probe": probe,
                            "removeSignal": remove_signal,
                            "failPlanHtml": fail_plan_html,
                            "fixtureIndex": INDEX_STATE,
                        }
                    ),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=90,
            )
    assert not profile.exists()
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _assert_rendered_signal(observation: dict[str, object], name: str) -> None:
    assert observation["ok"] is True, f"rendered signal missing: {name}: {observation}"


def _assert_removal_is_detected(
    observation: dict[str, object],
    name: str,
) -> None:
    with pytest.raises(AssertionError, match=f"rendered signal missing: {name}"):
        _assert_rendered_signal(observation, name)


def test_overview_renders_every_active_sprint_and_focus_conflict(tmp_path: Path) -> None:
    result = _rendered_probe(
        tmp_path,
        route="#cockpit",
        wait_selector=".r-overview-project-row",
        probe="""(() => {
          const row = document.querySelector(".r-overview-project-row");
          const links = [...row.querySelectorAll(".r-overview-sprints > a")];
          const conflict = row.querySelector(".r-overview-conflict[role='alert']");
          const sprintNames = links.map(link => link.textContent.trim());
          return {
            ok: sprintNames.some(name => name.includes("focus"))
              && sprintNames.some(name => name.includes("concurrent"))
              && Boolean(conflict)
              && conflict.textContent.includes("focus")
              && conflict.textContent.includes("concurrent"),
            sprintNames,
            conflict: conflict?.textContent.trim() || null,
          };
        })()""",
        remove_signal="document.querySelector('.r-overview-conflict')?.remove()",
    )

    _assert_rendered_signal(result["baseline"], "active sprint conflict")
    _assert_removal_is_detected(result["removed"], "active sprint conflict")


def test_plan_row_renders_authored_to_effective_transition_and_open_gate_count(
    tmp_path: Path,
) -> None:
    result = _rendered_probe(
        tmp_path,
        route="#plans",
        wait_selector=".r-status-transition",
        probe="""(() => {
          const transition = document.querySelector(".r-status-transition");
          const text = transition?.textContent.replace(/\\s+/g, " ").trim() || "";
          return {
            ok: text.includes("active")
              && text.includes("blocked")
              && text.includes("1 open gate"),
            text,
          };
        })()""",
        remove_signal="document.querySelector('.r-status-transition')?.remove()",
    )

    _assert_rendered_signal(result["baseline"], "status transition")
    _assert_removal_is_detected(result["removed"], "status transition")


def test_reader_renders_partial_source_status_missing_sections_and_retry(
    tmp_path: Path,
) -> None:
    result = _rendered_probe(
        tmp_path,
        route="#plan/rendered-contract",
        wait_selector=".r-reader-source-failure",
        fail_plan_html=True,
        probe="""(() => {
          const banner = document.querySelector(".r-reader-source-failure[role='alert']");
          const text = banner?.textContent.replace(/\\s+/g, " ").trim() || "";
          return {
            ok: text.includes("authored plan HTML failed")
              && text.includes("HTTP 503")
              && text.includes("authored body")
              && text.includes("authored followups")
              && text.includes("authored comments")
              && [...(banner?.querySelectorAll("button") || [])]
                .some(button => button.textContent.trim() === "Retry"),
            text,
          };
        })()""",
        remove_signal="document.querySelector('.r-reader-source-failure')?.remove()",
    )

    _assert_rendered_signal(result["baseline"], "partial source failure")
    _assert_removal_is_detected(result["removed"], "partial source failure")


def test_shell_renders_snapshot_source_resource_count_load_time_and_refresh(
    tmp_path: Path,
) -> None:
    result = _rendered_probe(
        tmp_path,
        route="#cockpit",
        wait_selector=".r-snapshot-receipt",
        probe="""(() => {
          const receipt = document.querySelector(".r-snapshot-receipt[role='status']");
          const text = receipt?.textContent.replace(/\\s+/g, " ").trim() || "";
          const count = Number(text.match(/(\\d+) resources/)?.[1] || 0);
          return {
            ok: !text.includes("unknown source")
              && count > 0
              && /loaded (?!unknown)/.test(text)
              && [...(receipt?.querySelectorAll("button") || [])]
                .some(button => button.textContent.trim() === "Refresh"),
            text,
            count,
          };
        })()""",
        remove_signal="document.querySelector('.r-snapshot-receipt')?.remove()",
    )

    _assert_rendered_signal(result["baseline"], "snapshot receipt")
    _assert_removal_is_detected(result["removed"], "snapshot receipt")


def test_handoff_renders_live_source_and_loaded_plan_version(tmp_path: Path) -> None:
    result = _rendered_probe(
        tmp_path,
        route="#plan/rendered-contract",
        wait_selector=".r-titlebar .gen-prompt",
        probe="""(async () => {
          document.querySelector(".r-titlebar .gen-prompt")?.click();
          const deadline = Date.now() + 5000;
          while (Date.now() < deadline && !document.querySelector(".r-modal textarea")) {
            await new Promise(resolve => setTimeout(resolve, 50));
          }
          const modal = document.querySelector(".r-modal");
          const text = modal?.querySelector("textarea")?.value || "";
          const footer = modal?.querySelector(".foot")?.textContent.replace(/\\s+/g, " ").trim() || "";
          return {
            ok: text.includes("Built from live plan HTML and project discovery.")
              && text.includes("Loaded plan version: 7")
              && footer.includes("built from live plan HTML + project discovery")
              && footer.includes("plan version 7"),
            footer,
            provenance: text.split("Handoff provenance").pop()?.trim() || "",
          };
        })()""",
        remove_signal="document.querySelector('.r-titlebar .gen-prompt')?.remove()",
    )

    _assert_rendered_signal(result["baseline"], "handoff provenance")
    _assert_removal_is_detected(result["removed"], "handoff provenance")


def test_compact_progress_renders_label_tooltip_and_navigation_target(
    tmp_path: Path,
) -> None:
    result = _rendered_probe(
        tmp_path,
        route="#plans",
        wait_selector=".r-compact-signal.pct",
        probe="""(async () => {
          const signal = document.querySelector(".r-compact-signal.pct");
          const label = signal?.getAttribute("aria-label") || "";
          const tooltip = signal?.getAttribute("title") || "";
          signal?.click();
          await new Promise(resolve => setTimeout(resolve, 100));
          return {
            ok: label.includes("42 percent complete")
              && tooltip.includes("42 percent complete")
              && location.hash.includes("#plan/rendered-contract"),
            label,
            tooltip,
            target: location.hash,
          };
        })()""",
        remove_signal="document.querySelector('.r-compact-signal.pct')?.remove()",
    )

    _assert_rendered_signal(result["baseline"], "compact progress navigation")
    _assert_removal_is_detected(
        result["removed"],
        "compact progress navigation",
    )
