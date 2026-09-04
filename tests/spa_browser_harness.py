from __future__ import annotations

import errno
import functools
import html
import http.server
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from unittest import SkipTest
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
COMPOSITION_PROBE = ROOT / "tests" / "spa_render_capture.mjs"
BROWSER_NAMES = ("google-chrome", "chromium", "chromium-browser")
NAVIGATION_FAULT_REPORT = (
    "/home/ITER/mcintos/.config/reckon/crew/reports/browser-navigation-probe.md"
)
_PROBE_STAGE_PREFIX = "[reckon-browser-stage] "
_SPA_STYLESHEETS = (
    "_shared/foundation.css",
    "_shared/dashboard.css",
    "ui/project.css",
    "ui/styles-base.css",
    "ui/styles.css",
    "ui/topbar.css",
    "ui/plans.css",
    "ui/reader.css",
    "ui/sprints.css",
    "ui/crew.css",
    "ui/graph.css",
)


class _IndexScriptParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.sources: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag != "script":
            return
        source = dict(attrs).get("src")
        if source and source.startswith("/_ui/"):
            self.sources.append(source)


def authored_ui_module_paths(root: Path = ROOT) -> tuple[Path, ...]:
    """Resolve UI module sources in the order authored by the SPA index."""

    parser = _IndexScriptParser()
    parser.feed((root / "docs" / "index.html").read_text(encoding="utf-8"))
    ui_root = root / "docs" / "ui"
    paths: list[Path] = []
    for source in parser.sources:
        path = ui_root / source.removeprefix("/_ui/")
        if not path.exists() and path.suffix == ".js":
            path = path.with_suffix(".jsx")
        if not path.is_file():
            raise FileNotFoundError(f"authored UI module has no source: {source}")
        paths.append(path)
    return tuple(paths)


@dataclass(frozen=True)
class AuthoredSource:
    """Path-like reader over an ordered set of authored SPA modules."""

    paths: tuple[Path, ...]

    def read_text(self, encoding: str = "utf-8") -> str:
        return "\n".join(path.read_text(encoding=encoding) for path in self.paths)


def authored_shell_source(root: Path = ROOT) -> AuthoredSource:
    """Return the complete shell contract without inventing another module list."""

    return AuthoredSource(
        tuple(
            path
            for path in authored_ui_module_paths(root)
            if path.stem == "shell" or path.stem.startswith("shell-")
        )
    )


class BrowserProbeError(RuntimeError):
    """A browser gate that could not reach its rendered assertion."""

    def __init__(
        self,
        classification: str,
        *,
        browser_stderr: str,
        inotify_count: int,
        inotify_limit: int,
        detail: str,
    ) -> None:
        self.classification = classification
        self.browser_stderr = browser_stderr
        self.inotify_count = inotify_count
        self.inotify_limit = inotify_limit
        super().__init__(
            f"browser probe {classification}: {detail}; "
            f"inotify instances {inotify_count}/{inotify_limit}; "
            f"browser stderr: {browser_stderr.strip() or '<empty>'}"
        )


def _inotify_usage(
    *,
    proc_root: Path = Path("/proc"),
    limit_path: Path = Path("/proc/sys/fs/inotify/max_user_instances"),
    uid: int | None = None,
) -> tuple[int, int]:
    """Count this user's live inotify instances against the kernel ceiling."""

    owner = os.getuid() if uid is None else uid
    try:
        limit = int(limit_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        limit = 0
    count = 0
    try:
        processes = tuple(proc_root.iterdir())
    except OSError:
        processes = ()
    for process in processes:
        if not process.name.isdigit():
            continue
        try:
            if process.stat().st_uid != owner:
                continue
            descriptors = tuple((process / "fd").iterdir())
        except OSError:
            continue
        for descriptor in descriptors:
            try:
                if os.readlink(descriptor) == "anon_inode:inotify":
                    count += 1
            except OSError:
                continue
    return count, limit


def _probe_stage(stderr: str) -> str:
    stages = [
        line.removeprefix(_PROBE_STAGE_PREFIX).strip()
        for line in stderr.splitlines()
        if line.startswith(_PROBE_STAGE_PREFIX)
    ]
    return stages[-1] if stages else "browser-not-observed"


def _classify_probe_failure(
    *,
    stage: str,
    browser_stderr: str,
    inotify_count: int,
    inotify_limit: int,
) -> BrowserProbeError:
    """Turn observable launch state into one actionable failure class."""

    if inotify_limit > 0 and inotify_count >= inotify_limit:
        classification = "ceiling-exhausted"
        detail = "the per-user inotify ceiling is full; wait for leaked browsers to be reaped"
    elif stage == "sandbox-preflight-denied":
        classification = "sandbox-denied"
        detail = "the worker sandbox denied AF_INET socket creation"
    else:
        classification = "navigation-never-completed"
        detail = (
            f"the browser reached stage {stage!r} but the page did not become ready; "
            "the diagnosed boundary is Chrome's Network Service above Internet "
            f"stream-socket creation; evidence: {NAVIGATION_FAULT_REPORT}; "
            "this rendered case remains owed against the required host/browser "
            "runtime change"
        )
    return BrowserProbeError(
        classification,
        browser_stderr=browser_stderr,
        inotify_count=inotify_count,
        inotify_limit=inotify_limit,
        detail=detail,
    )


def _preflight_browser_socket(
    *,
    socket_factory: Callable[[int, int], socket.socket] = socket.socket,
    worker_role: str | None = None,
    inotify_probe: Callable[[], tuple[int, int]] = _inotify_usage,
) -> None:
    """Refuse a browser launch when the current sandbox cannot open AF_INET."""

    role = worker_role or os.environ.get("RECKON_WORKER_ROLE") or "unknown"
    try:
        candidate = socket_factory(socket.AF_INET, socket.SOCK_STREAM)
        candidate.close()
    except OSError as error:
        if error.errno not in (errno.EPERM, errno.EACCES):
            raise
        count, limit = inotify_probe()
        raise BrowserProbeError(
            "sandbox-denied",
            browser_stderr="browser not started: AF_INET preflight was denied",
            inotify_count=count,
            inotify_limit=limit,
            detail=(
                f"worker role {role!r} is the likely cause; use an execution-capable "
                "network-enabled role"
            ),
        ) from error


_PROBE_DRIVER = r"""
import { spawn } from "node:child_process";
import { readFile } from "node:fs/promises";
import path from "node:path";

const argumentsAfterScript = process.argv.slice(1);
const legacyInput = argumentsAfterScript.length === 1 && argumentsAfterScript[0].startsWith("{")
  ? JSON.parse(argumentsAfterScript[0])
  : null;
const [browserPath, profile, pageUrl, widthText, heightText] = legacyInput
  ? [legacyInput.browser, legacyInput.profile, legacyInput.url, "1374", "900"]
  : argumentsAfterScript;
const width = Number(widthText);
const height = Number(heightText);
const expression = legacyInput?.probe || process.env.RECKON_BROWSER_EXPRESSION;
const readyExpression = legacyInput
  ? `Boolean(window.STATE && document.querySelector(${JSON.stringify(legacyInput.waitSelector)}))`
  : process.env.RECKON_BROWSER_READY_EXPRESSION;
const preloadExpression = legacyInput
  ? `{
          const fixtureIndex = ${JSON.stringify(legacyInput.fixtureIndex)};
          const fixtureSprints = fixtureIndex.data.sprints;
          const fixtureReview = ${JSON.stringify(legacyInput.fixtureReview)};
          const fixtureNewPlan = ${JSON.stringify(legacyInput.fixtureNewPlan)};
          const nativeFetch = window.fetch.bind(window);
          window.__discoveryRequestCount = 0;
          window.fetch = (resource, options) => {
            const url = String(resource);
            if (url.endsWith("/state/reckon/index.json")) {
              return Promise.resolve(new Response(JSON.stringify(fixtureIndex), {
                status: 200,
                headers: { "Content-Type": "application/json" },
              }));
            }
            if (url.endsWith("/_discover/reckon")) {
              window.__discoveryRequestCount += 1;
              return nativeFetch(resource, options).then(response => response.json()).then(payload => {
                const inventory = fixtureNewPlan && window.__discoveryRequestCount > 1
                  ? [...(payload.inventory || []), fixtureNewPlan]
                  : payload.inventory;
                return new Response(JSON.stringify({
                  ...payload,
                  inventory,
                  sprints: fixtureSprints,
                  active_sprint_id: fixtureIndex.data.active_sprint_id,
                  review: fixtureReview,
                  source_format: "distributed",
                  resource_versions: {
                    "project:reckon": 4,
                    "sprint:focus": 2,
                    "sprint:concurrent": 1,
                  },
                }), {
                  status: 200,
                  headers: { "Content-Type": "application/json" },
                });
              });
            }
            if (${JSON.stringify(legacyInput.failPlanHtml)}
                && url.includes("/plans/")
                && url.endsWith(".html")) {
              return Promise.resolve(new Response("", { status: 503 }));
            }
            return nativeFetch(resource, options);
          };
        }`
  : process.env.RECKON_BROWSER_PRELOAD_EXPRESSION;
const prepareExpression = legacyInput?.prepareSignal
  || process.env.RECKON_BROWSER_PREPARE_EXPRESSION;
const removeExpression = legacyInput?.removeSignal
  || process.env.RECKON_BROWSER_REMOVE_EXPRESSION;
const refreshExpression = legacyInput?.refreshProbe
  || process.env.RECKON_BROWSER_REFRESH_EXPRESSION;
const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
const stage = value => console.error(`[reckon-browser-stage] ${value}`);
const browserEvents = [];

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
      if (message.method === "Runtime.exceptionThrown") {
        browserEvents.push(message.params.exceptionDetails?.exception?.description
          || message.params.exceptionDetails?.text || "runtime exception");
      } else if (message.method === "Runtime.consoleAPICalled") {
        browserEvents.push((message.params.args || [])
          .map(argument => argument.value || argument.description || "").join(" "));
      }
      const pending = this.pending.get(message.id);
      if (!pending) return;
      this.pending.delete(message.id);
      message.error ? pending.reject(message.error) : pending.resolve(message.result);
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

async function evaluate(devtools, source) {
  const response = await devtools.call("Runtime.evaluate", {
    expression: source,
    awaitPromise: true,
    returnByValue: true,
  });
  if (response.exceptionDetails) {
    const detail = response.exceptionDetails.exception?.description || response.exceptionDetails.text;
    throw new Error(detail || "browser evaluation failed");
  }
  return response.result.value;
}

function navigationUrl(generation) {
  const [base, fragment = ""] = pageUrl.split("#", 2);
  const separator = base.includes("?") ? "&" : "?";
  return `${base}${separator}browser_generation=${generation}${fragment ? `#${fragment}` : ""}`;
}

async function navigateAndWait(devtools, generation) {
  await devtools.call("Page.navigate", { url: navigationUrl(generation) });
  stage(`navigation-${generation}-started`);
  const deadline = Date.now() + 45000;
  let ready = false;
  while (Date.now() < deadline) {
    try {
      ready = Boolean(await evaluate(devtools, `Boolean(
        location.search.includes("browser_generation=${generation}")
        && (${readyExpression})
      )`));
    } catch {
      ready = false;
    }
    if (ready) return;
    await delay(100);
  }
  throw new Error(
    `timed out waiting for browser readiness: ${readyExpression}; `
    + `browser events: ${browserEvents.slice(-10).join(" | ") || "none"}`
  );
}

const browser = spawn(browserPath, [
  "--headless=new",
  "--no-sandbox",
  "--disable-gpu",
  "--remote-debugging-port=0",
  `--user-data-dir=${profile}`,
  `--window-size=${width},${height}`,
  "about:blank",
], { stdio: ["ignore", "ignore", "inherit"] });
stage("browser-spawned");

let devtools;
try {
  const activePort = await waitForFile(path.join(profile, "DevToolsActivePort"), 15000);
  stage("browser-started");
  const [port] = activePort.trim().split(/\s+/);
  const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  const target = targets.find(candidate => candidate.type === "page");
  if (!target) throw new Error("browser did not expose a page target");

  devtools = new DevTools(target.webSocketDebuggerUrl);
  await devtools.open();
  await devtools.call("Page.enable");
  await devtools.call("Runtime.enable");
  await devtools.call("Emulation.setDeviceMetricsOverride", {
    width,
    height,
    deviceScaleFactor: 1,
    mobile: false,
  });
  if (preloadExpression) {
    await devtools.call("Page.addScriptToEvaluateOnNewDocument", {
      source: preloadExpression,
    });
  }
  await navigateAndWait(devtools, 1);
  if (refreshExpression) {
    const refreshed = await evaluate(devtools, refreshExpression);
    process.stdout.write(JSON.stringify({ refreshed }));
  } else {
    await evaluate(devtools, prepareExpression || "undefined");
    const baseline = await evaluate(devtools, expression);
    if (removeExpression) {
      await navigateAndWait(devtools, 2);
      await evaluate(devtools, prepareExpression || "undefined");
      await evaluate(devtools, removeExpression);
      const removed = await evaluate(devtools, expression);
      process.stdout.write(JSON.stringify({ baseline, removed }));
    } else {
      process.stdout.write(JSON.stringify(baseline));
    }
  }
} finally {
  if (devtools) devtools.close();
  browser.kill("SIGTERM");
  await new Promise(resolve => {
    if (browser.exitCode !== null) resolve();
    else browser.once("exit", resolve);
  });
}
"""

# Compatibility for rendered checks that configure the shared driver source
# directly. New probes use ServedSpa.run_probe; these callers still execute the
# same browser-launch owner while their migration remains independently scoped.
NODE_PROBE = _PROBE_DRIVER.replace(
    "`--window-size=${width},${height}`",
    '"--window-size=1374,900"',
).replace(
    "    width,\n    height,",
    "    width: 1374,\n    height: 900,",
)


def _terminate_process_group(process: subprocess.Popen[str]) -> tuple[str, str]:
    """Reap a timed-out probe and every process it started.

    The group is signalled rather than the leader, because the browser is a
    child the driver spawned and outlives it otherwise. SIGKILL rather than
    SIGTERM: the driver is already past its deadline, and a browser asked to
    shut down politely can take longer than the timeout it just missed.
    """
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        process.kill()
    try:
        return process.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        return "", ""


@contextmanager
def temporary_browser_profile(tmp_path: Path) -> Iterator[Path]:
    profile = Path(tempfile.mkdtemp(prefix="browser-profile-", dir=tmp_path))
    try:
        yield profile
    finally:
        # No jump statement may leave this block: returning from a `finally`
        # discards whatever exception was propagating, and because this runs as a
        # contextmanager generator the suppression reaches the caller -- the probe
        # timeout below it vanished and surfaced as an UnboundLocalError on the
        # `result` the swallowed call never assigned. Record the outcome, leave by
        # falling off the end.
        deadline = time.monotonic() + 15
        absent_since: float | None = None
        stayed_removed = False
        while time.monotonic() < deadline:
            if profile.exists():
                absent_since = None
                try:
                    shutil.rmtree(profile)
                except FileNotFoundError:
                    pass
                except OSError as error:
                    if error.errno not in (errno.EBUSY, errno.ENOTEMPTY):
                        raise
            elif absent_since is None:
                absent_since = time.monotonic()
            elif time.monotonic() - absent_since >= 0.5:
                stayed_removed = True
                break
            time.sleep(0.05)
        if not stayed_removed:
            raise TimeoutError(f"browser profile did not remain removed: {profile}")


def installed_browser() -> str | None:
    return next(
        (path for name in BROWSER_NAMES if (path := shutil.which(name))),
        None,
    )


def installed_browser_or_skip() -> str:
    """Return a supported browser or report the harness capability as skipped."""

    browser = installed_browser()
    if browser is None:
        raise SkipTest(
            "no supported browser binary is installed; tried "
            + ", ".join(BROWSER_NAMES)
        )
    return browser


def _script_body(source: str) -> str:
    """Keep an inlined script from terminating its own HTML element."""

    return source.replace("</script", "<\\/script")


def write_file_spa_document(
    destination: Path,
    composed_state: Mapping[str, object],
    *,
    project: str = "reckon",
) -> Path:
    """Write one browser-ready SPA document with no external dependencies."""

    from reckon.serve import client_runtime_assets, compile_jsx

    runtime = client_runtime_assets()
    styles = [
        (ROOT / "docs" / relative_path).read_text(encoding="utf-8")
        for relative_path in _SPA_STYLESHEETS
    ]

    scripts = [
        runtime["react.js"].decode("utf-8"),
        runtime["react-dom.js"].decode("utf-8"),
        (
            f"window.STATE = {json.dumps(composed_state, separators=(',', ':'))};\n"
            "window.STATE_ERROR = null;\n"
            "window.STATE_READY = Promise.resolve(window.STATE);\n"
            "window.revalidateProjectState = async () => window.STATE;"
        ),
    ]
    for path in authored_ui_module_paths():
        if path.name == "state-loader.js":
            continue
        source = path.read_text(encoding="utf-8")
        if path.suffix == ".jsx":
            source = compile_jsx(source, filename=path.name).decode("utf-8")
        scripts.append(source)

    document = "\n".join(
        [
            "<!doctype html>",
            '<html lang="en">',
            "<head>",
            '  <meta charset="utf-8">',
            f'  <meta name="docs-project" content="{html.escape(project, quote=True)}">',
            '  <meta name="viewport" content="width=device-width,initial-scale=1">',
            f"  <title>reckon · {html.escape(project)}</title>",
            f"  <style>{'</style><style>'.join(styles)}</style>",
            "</head>",
            "<body>",
            '  <div id="root"></div>',
            *(f"  <script>{_script_body(script)}</script>" for script in scripts),
            "</body>",
            "</html>",
        ]
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(document, encoding="utf-8")
    return destination


@contextmanager
def file_spa(
    tmp_path: Path,
    browser: str,
    composed_state: Mapping[str, object],
    *,
    project: str = "reckon",
    route: str = "#cockpit",
) -> Iterator[ServedSpa]:
    """Open a composed SPA from one temporary file instead of an HTTP server."""

    generated_root = Path(tempfile.mkdtemp(prefix="file-spa-", dir=tmp_path))
    page = write_file_spa_document(
        generated_root / "index.html",
        composed_state,
        project=project,
    )
    try:
        yield ServedSpa(
            browser=browser,
            url=f"{page.resolve().as_uri()}{route}",
            tmp_path=tmp_path,
        )
    finally:
        shutil.rmtree(generated_root)


def _unused_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@dataclass(frozen=True)
class ServedSpa:
    browser: str
    url: str
    tmp_path: Path

    def run_probe(
        self,
        expression: str,
        *,
        viewport: tuple[int, int] = (1374, 900),
        ready_expression: str = "document.readyState === 'complete'",
        preload_expression: str | None = None,
        prepare_expression: str | None = None,
        remove_expression: str | None = None,
        refresh_expression: str | None = None,
    ) -> object:
        return _evaluate_browser_url(
            self.tmp_path,
            self.browser,
            self.url,
            expression,
            viewport=viewport,
            ready_expression=ready_expression,
            preload_expression=preload_expression,
            prepare_expression=prepare_expression,
            remove_expression=remove_expression,
            refresh_expression=refresh_expression,
        )

    def run_composition_probe(
        self,
        *extra: str,
        expected_width: int = 1374,
        screenshot: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        with temporary_browser_profile(self.tmp_path) as profile:
            command = [
                "node",
                str(COMPOSITION_PROBE),
                "--browser",
                self.browser,
                "--profile",
                str(profile),
                "--url",
                self.url,
                "--expected-width",
                str(expected_width),
                *extra,
            ]
            if screenshot is not None:
                command.extend(["--screenshot", str(screenshot)])
            return subprocess.run(
                command,
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=75,
                check=False,
            )


@contextmanager
def served_spa(
    tmp_path: Path,
    browser: str,
    *,
    docs: Path = ROOT / "docs",
    project: str = "reckon",
    route: str = "#cockpit",
) -> Iterator[ServedSpa]:
    port = _unused_port()
    mounts = tmp_path / "mounts.json"
    mounts.write_text(json.dumps({project: str(docs)}), encoding="utf-8")
    server_home = Path(tempfile.mkdtemp(prefix="reckon-server-", dir=tmp_path))
    fixture_state = docs / "state" / project
    if fixture_state.is_dir():
        shutil.copytree(fixture_state, server_home / "state" / project)
    environment = dict(os.environ)
    environment["RECKON_HOME"] = str(server_home)
    server = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "from reckon.serve import main; main(port=int(__import__('sys').argv[1]), "
                "host='127.0.0.1', mounts_file=__import__('pathlib').Path(__import__('sys').argv[2]))"
            ),
            str(port),
            str(mounts),
        ],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    url = f"http://127.0.0.1:{port}/{project}/{route}"
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
        yield ServedSpa(browser=browser, url=url, tmp_path=tmp_path)
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)
        shutil.rmtree(server_home)


class _QuietStaticHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return


@contextmanager
def _served_document(tmp_path: Path, document: str | Path) -> Iterator[str]:
    generated_root: Path | None = None
    if isinstance(document, Path):
        page = document.resolve()
        root = page.parent
    else:
        generated_root = Path(
            tempfile.mkdtemp(prefix="browser-document-", dir=tmp_path)
        )
        root = generated_root
        page = root / "index.html"
        page.write_text(document, encoding="utf-8")

    handler = functools.partial(_QuietStaticHandler, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = int(server.server_address[1])
        yield f"http://127.0.0.1:{port}/{page.name}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if generated_root is not None:
            shutil.rmtree(generated_root)


def _evaluate_browser_url(
    tmp_path: Path,
    browser: str,
    url: str,
    expression: str,
    *,
    viewport: tuple[int, int],
    ready_expression: str,
    preload_expression: str | None = None,
    prepare_expression: str | None = None,
    remove_expression: str | None = None,
    refresh_expression: str | None = None,
) -> object:
    _preflight_browser_socket()
    with temporary_browser_profile(tmp_path) as profile:
        environment = dict(os.environ)
        environment["RECKON_BROWSER_EXPRESSION"] = expression
        environment["RECKON_BROWSER_READY_EXPRESSION"] = ready_expression
        optional_expressions = {
            "RECKON_BROWSER_PRELOAD_EXPRESSION": preload_expression,
            "RECKON_BROWSER_PREPARE_EXPRESSION": prepare_expression,
            "RECKON_BROWSER_REMOVE_EXPRESSION": remove_expression,
            "RECKON_BROWSER_REFRESH_EXPRESSION": refresh_expression,
        }
        environment.update(
            {
                key: value
                for key, value in optional_expressions.items()
                if value is not None
            }
        )
        # Run the driver in its own process group so a timeout can reap the
        # browser with it. `subprocess.run(timeout=...)` kills only the process
        # it started, and the driver kills the browser from a JavaScript
        # `finally` that a killed process never reaches -- so every timed-out
        # probe used to leave a full browser tree alive. Those survivors then
        # hold the profile directory that cleanup is waiting to remove, and load
        # the machine enough to time out the next probe: 30 timeouts in one run
        # left 410 browser processes behind and a load average above 10, which
        # is a suite that makes itself slower the longer it runs.
        probe = subprocess.Popen(
            [
                "node",
                "--input-type=module",
                "-e",
                _PROBE_DRIVER,
                browser,
                str(profile),
                url,
                str(viewport[0]),
                str(viewport[1]),
            ],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = probe.communicate(timeout=75)
        except subprocess.TimeoutExpired:
            stdout, stderr = _terminate_process_group(probe)
            count, limit = _inotify_usage()
            raise _classify_probe_failure(
                stage=_probe_stage(stderr),
                browser_stderr=stderr,
                inotify_count=count,
                inotify_limit=limit,
            ) from None
        result = subprocess.CompletedProcess(
            probe.args, probe.returncode, stdout=stdout, stderr=stderr
        )
    if result.returncode != 0:
        count, limit = _inotify_usage()
        raise _classify_probe_failure(
            stage=_probe_stage(result.stderr),
            browser_stderr=result.stderr or result.stdout,
            inotify_count=count,
            inotify_limit=limit,
        )
    return json.loads(result.stdout)


def run_browser_probe(
    tmp_path: Path,
    browser: str,
    document: str | Path,
    expression: str,
    *,
    viewport: tuple[int, int] = (1374, 900),
    ready_expression: str = "document.readyState === 'complete'",
    route: str = "",
    preload_expression: str | None = None,
    prepare_expression: str | None = None,
    remove_expression: str | None = None,
    refresh_expression: str | None = None,
) -> object:
    with _served_document(tmp_path, document) as url:
        return _evaluate_browser_url(
            tmp_path,
            browser,
            f"{url}{route}",
            expression,
            viewport=viewport,
            ready_expression=ready_expression,
            preload_expression=preload_expression,
            prepare_expression=prepare_expression,
            remove_expression=remove_expression,
            refresh_expression=refresh_expression,
        )
