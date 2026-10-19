"""The velocity pane keeps a pending result visible and re-requests it."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from reckon import serve

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "ui" / "velocity.jsx"


def _probe(mode: str) -> dict:
    source = SOURCE.read_text(encoding="utf-8")
    if os.environ.get("RECKON_VELOCITY_202_FINAL"):
        assert "response.status === 202" in source
        source = source.replace("response.status === 202", "false", 1)
    compiled = serve.compile_jsx(source, filename="velocity-probe.jsx").decode()
    script = "\n".join(
        [
            r"""
globalThis.window = globalThis;
const slots = [];
const effects = [];
const listeners = new Map();
const timers = new Map();
const requests = [];
let cursor = 0;
let nextTimer = 1;
globalThis.React = {
  createElement(type, props, ...children) {
    if (typeof type === "function") return type({ ...(props || {}), children });
    return { type, props: props || {}, children };
  },
  Fragment: Symbol("Fragment"),
  useState(initial) {
    const index = cursor++;
    if (!(index in slots)) slots[index] = initial;
    return [slots[index], value => { slots[index] = value; }];
  },
  useEffect(effect, deps) {
    const index = cursor++;
    if (!(index in slots)) {
      slots[index] = deps;
      effects.push(effect);
    }
  },
};
globalThis.addEventListener = (name, listener) => listeners.set(name, listener);
globalThis.removeEventListener = (name, listener) => {
  if (listeners.get(name) === listener) listeners.delete(name);
};
globalThis.setTimeout = callback => {
  const id = nextTimer++;
  timers.set(id, callback);
  return id;
};
globalThis.clearTimeout = id => timers.delete(id);
const key = "velocity:*:default:::plans::";
const replies = [
  { status: 202, body: { status: "computing", key }, retry: "45" },
  { status: 200, body: { ok: true, by_project: [], by_lane: [], by_day: [] } },
];
globalThis.fetch = async url => {
  requests.push(url);
  const answer = replies.shift();
  if (!answer) throw new Error("unexpected request");
  return {
    status: answer.status,
    ok: answer.status < 400,
    json: async () => answer.body,
    headers: { get: name => name === "Retry-After" ? answer.retry : null },
  };
};
function textContent(node) {
  if (node == null || node === false) return "";
  if (Array.isArray(node)) return node.map(textContent).join("");
  if (typeof node !== "object") return String(node);
  return (node.children || []).map(textContent).join("");
}
function render() {
  cursor = 0;
  const tree = VelocityView({ visibleProjects: [], mountedProjectCount: 1 });
  while (effects.length) effects.shift()();
  return tree;
}
async function flush() {
  for (let i = 0; i < 8; i++) await Promise.resolve();
}
""",
            compiled,
            f"const mode = {json.dumps(mode)};",
            r"""
(async () => {
  render();
  await flush();
  const pendingText = textContent(render());
  const initialRequests = requests.length;
  const pendingTimerCount = timers.size;
  if (mode === "timer") {
    const [id, callback] = timers.entries().next().value || [];
    timers.delete(id);
    callback?.();
  } else {
    listeners.get("reckon-fleet")?.({ detail: { key } });
  }
  await flush();
  const finalText = textContent(render());
  process.stdout.write(JSON.stringify({
    pendingText, initialRequests, pendingTimerCount,
    finalRequests: requests.length, finalText, timersRemaining: timers.size,
  }));
})().catch(error => { process.stderr.write(String(error)); process.exitCode = 1; });
""",
        ]
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


def test_pending_velocity_retries_after_the_response_interval():
    result = _probe("timer")
    assert result["initialRequests"] == 1
    assert result["pendingTimerCount"] == 1
    assert "Computing" in result["pendingText"]
    assert "By project" not in result["pendingText"]
    assert result["finalRequests"] == 2
    assert "By project" in result["finalText"]


def test_matching_fleet_event_retries_without_waiting_for_the_timer():
    result = _probe("fleet")
    assert result["initialRequests"] == 1
    assert "Computing" in result["pendingText"]
    assert result["finalRequests"] == 2
    assert result["timersRemaining"] == 0
    assert "By project" in result["finalText"]
