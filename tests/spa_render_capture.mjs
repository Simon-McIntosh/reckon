import { spawn } from "node:child_process";
import { access, mkdtemp, readFile, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

const argumentsByName = new Map();
for (let index = 2; index < process.argv.length; index += 2) {
  argumentsByName.set(process.argv[index], process.argv[index + 1]);
}

const browserPath = argumentsByName.get("--browser");
const pageUrl = argumentsByName.get("--url");
const expectedWidth = Number(argumentsByName.get("--expected-width"));
const conflictingWidth = Number(argumentsByName.get("--conflicting-width") || 0);
const viewportHeight = 900;

if (!browserPath || !pageUrl || !Number.isFinite(expectedWidth)) {
  console.error("usage: node spa_render_capture.mjs --browser PATH --url URL --expected-width PX [--conflicting-width PX]");
  process.exit(2);
}

try {
  await access(browserPath);
} catch {
  console.error(`SKIP: browser binary is not present: ${browserPath}`);
  process.exit(77);
}

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
    throw new Error(response.exceptionDetails.text || "browser evaluation failed");
  }
  return response.result.value;
}

async function waitForComposition(devtools, timeoutMs = 45000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const ready = await evaluate(devtools, `(() => {
      const app = document.querySelector(".r-app");
      const view = document.querySelector(".r-canvas-view");
      return Boolean(app && view && app.getBoundingClientRect().height > 500);
    })()`);
    if (ready) return;
    await delay(200);
  }
  throw new Error("timed out waiting for the served SPA container");
}

function closeEnough(left, right) {
  return Math.abs(left - right) <= 1;
}

async function main() {
  const profile = await mkdtemp(path.join(os.tmpdir(), "reckon-composition-"));
  const browser = spawn(browserPath, [
    "--headless=new",
    "--no-sandbox",
    "--disable-gpu",
    "--remote-debugging-port=0",
    `--user-data-dir=${profile}`,
    `--window-size=${expectedWidth},${viewportHeight}`,
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
      width: expectedWidth,
      height: viewportHeight,
      deviceScaleFactor: 1,
      mobile: false,
    });
    await devtools.call("Page.navigate", { url: pageUrl });
    await waitForComposition(devtools);

    if (conflictingWidth) {
      await evaluate(devtools, `(() => {
        const style = document.createElement("style");
        style.textContent = ${JSON.stringify(".r-app { min-width: " + conflictingWidth + "px !important; }")};
        document.head.appendChild(style);
      })()`);
    }

    const geometry = await evaluate(devtools, `(() => {
      const app = document.querySelector(".r-app");
      const topbar = document.querySelector(".r-topbar");
      const views = [...document.querySelectorAll(".r-canvas-view")].filter(element => {
        const rect = element.getBoundingClientRect();
        const style = getComputedStyle(element);
        return style.display !== "none" && rect.width > 0 && rect.height > 0;
      });
      const view = views[0];
      const rect = element => {
        const value = element.getBoundingClientRect();
        return { x: value.x, top: value.top, right: value.right, bottom: value.bottom, width: value.width, height: value.height };
      };
      return {
        viewport: { width: innerWidth, height: innerHeight },
        visibleViewCount: views.length,
        app: { ...rect(app), display: getComputedStyle(app).display, flexDirection: getComputedStyle(app).flexDirection },
        topbar: rect(topbar),
        view: rect(view),
      };
    })()`);

    const failures = [];
    if (geometry.visibleViewCount !== 1) failures.push(`visible views: ${geometry.visibleViewCount}`);
    if (geometry.app.display !== "flex") failures.push(`app display: ${geometry.app.display}`);
    if (geometry.app.flexDirection !== "column") failures.push(`app flex direction: ${geometry.app.flexDirection}`);
    if (!closeEnough(geometry.app.width, expectedWidth)) failures.push(`app width: ${geometry.app.width}, expected ${expectedWidth}`);
    if (!closeEnough(geometry.view.width, geometry.app.width)) failures.push(`view width: ${geometry.view.width}, app width ${geometry.app.width}`);
    if (!closeEnough(geometry.view.top, geometry.topbar.bottom)) failures.push(`view top: ${geometry.view.top}, topbar bottom ${geometry.topbar.bottom}`);
    if (!closeEnough(geometry.view.bottom, geometry.app.bottom)) failures.push(`view bottom: ${geometry.view.bottom}, app bottom ${geometry.app.bottom}`);

    process.stdout.write(`${JSON.stringify(geometry)}\n`);
    if (failures.length) {
      throw new Error(`composed container geometry mismatch: ${failures.join("; ")}`);
    }
  } finally {
    if (devtools) devtools.close();
    browser.kill("SIGTERM");
    await new Promise(resolve => {
      if (browser.exitCode !== null) resolve();
      else browser.once("exit", resolve);
    });
    await rm(profile, { recursive: true, force: true });
    if (browserErrors.length && browser.exitCode && browser.exitCode !== 0) {
      console.error(Buffer.concat(browserErrors).toString());
    }
  }
}

main().catch(error => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
