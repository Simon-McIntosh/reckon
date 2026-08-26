from __future__ import annotations

import errno
import functools
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
COMPOSITION_PROBE = ROOT / "tests" / "spa_render_capture.mjs"
BROWSER_NAMES = ("google-chrome", "chromium", "chromium-browser")


_PROBE_DRIVER = r"""
import { spawn } from "node:child_process";
import { readFile } from "node:fs/promises";
import path from "node:path";

const [browserPath, profile, pageUrl, widthText, heightText] = process.argv.slice(1);
const width = Number(widthText);
const height = Number(heightText);
const expression = process.env.RECKON_BROWSER_EXPRESSION;
const readyExpression = process.env.RECKON_BROWSER_READY_EXPRESSION;
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

const browser = spawn(browserPath, [
  "--headless=new",
  "--no-sandbox",
  "--disable-gpu",
  "--remote-debugging-port=0",
  `--user-data-dir=${profile}`,
  `--window-size=${width},${height}`,
  "about:blank",
], { stdio: ["ignore", "ignore", "pipe"] });
const browserErrors = [];
browser.stderr.on("data", chunk => browserErrors.push(chunk));

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
    width,
    height,
    deviceScaleFactor: 1,
    mobile: false,
  });
  await devtools.call("Page.navigate", { url: pageUrl });

  const deadline = Date.now() + 45000;
  let ready = false;
  while (Date.now() < deadline) {
    try {
      ready = Boolean(await evaluate(devtools, readyExpression));
    } catch {
      ready = false;
    }
    if (ready) break;
    await delay(100);
  }
  if (!ready) throw new Error(`timed out waiting for browser readiness: ${readyExpression}`);

  const value = await evaluate(devtools, expression);
  process.stdout.write(JSON.stringify(value));
} finally {
  if (devtools) devtools.close();
  browser.kill("SIGTERM");
  await new Promise(resolve => {
    if (browser.exitCode !== null) resolve();
    else browser.once("exit", resolve);
  });
  if (browser.exitCode && browser.exitCode !== 0 && browserErrors.length) {
    console.error(Buffer.concat(browserErrors).toString());
  }
}
"""


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
    ) -> object:
        return _evaluate_browser_url(
            self.tmp_path,
            self.browser,
            self.url,
            expression,
            viewport=viewport,
            ready_expression=ready_expression,
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
) -> object:
    with temporary_browser_profile(tmp_path) as profile:
        environment = dict(os.environ)
        environment["RECKON_BROWSER_EXPRESSION"] = expression
        environment["RECKON_BROWSER_READY_EXPRESSION"] = ready_expression
        result = subprocess.run(
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
            capture_output=True,
            text=True,
            timeout=75,
            check=False,
        )
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
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
) -> object:
    with _served_document(tmp_path, document) as url:
        return _evaluate_browser_url(
            tmp_path,
            browser,
            f"{url}{route}",
            expression,
            viewport=viewport,
            ready_expression=ready_expression,
        )
