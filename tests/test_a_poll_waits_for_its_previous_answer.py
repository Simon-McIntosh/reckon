"""A SPA poll waits for its previous answer before it asks again.

The shell, the plan view and the crew view each polled a route on a fixed
``setInterval``, which fires whether or not the previous request has returned.
Under load a ``/crew`` answer took longer than the interval, so polls stacked
and each overlap added server work that slowed the next answer. The three
pollers now go through ``window.startPoll`` in ``_shared.jsx``, which schedules
the next request only once the previous one has settled, and the shell and the
crew view share one ``/crew`` poll.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from reckon import serve

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "docs" / "ui"
SHARED = UI / "_shared.jsx"
SHELL = UI / "shell.jsx"
PLAN = UI / "plan.jsx"
CREW = UI / "crew.jsx"

TIMER_PRELUDE = r"""
globalThis.window = globalThis;
globalThis.React = {
  createElement: () => ({}),
  Fragment: Symbol("Fragment"),
  useState: value => [typeof value === "function" ? value() : value, () => {}],
  useEffect: () => {},
  useRef: () => ({ current: null }),
  useMemo: fn => fn(),
  useCallback: fn => fn,
};

globalThis.__seq = 0;
globalThis.__now = 0;
globalThis.__tasks = new Map();
globalThis.__errors = [];
window.setTimeout = (fn, ms) => {
  const id = ++globalThis.__seq;
  globalThis.__tasks.set(id, { kind: "timeout", at: globalThis.__now + (Number(ms) || 0), fn });
  return id;
};
window.clearTimeout = id => { globalThis.__tasks.delete(id); };
window.setInterval = (fn, ms) => {
  const id = ++globalThis.__seq;
  const every = Number(ms) || 0;
  globalThis.__tasks.set(id, { kind: "interval", at: globalThis.__now + every, every, fn });
  return id;
};
window.clearInterval = id => { globalThis.__tasks.delete(id); };
globalThis.__flush = async () => { for (let i = 0; i < 50; i += 1) await Promise.resolve(); };
globalThis.__advance = async ms => {
  const target = globalThis.__now + ms;
  for (;;) {
    let dueId = null;
    let dueAt = Infinity;
    for (const [id, task] of globalThis.__tasks) {
      if (task.at <= target && task.at < dueAt) { dueAt = task.at; dueId = id; }
    }
    if (dueId === null) break;
    globalThis.__now = dueAt;
    const picked = globalThis.__tasks.get(dueId);
    if (picked.kind === "timeout") globalThis.__tasks.delete(dueId);
    else picked.at = picked.at + picked.every;
    // Start the callback without awaiting it: it may await a timer that fires
    // on a later tick of this same loop, which awaiting would deadlock.
    try { picked.fn(); } catch (error) { globalThis.__errors.push(String(error)); }
    await globalThis.__flush();
  }
  globalThis.__now = target;
};
"""

HELPER_PROBE = r"""
(async () => {
  const INTERVAL = 100;
  globalThis.__outstanding = 0;
  globalThis.__peak = 0;
  globalThis.__requests = 0;
  globalThis.__handled = 0;
  globalThis.fetch = () => {
    globalThis.__requests += 1;
    globalThis.__outstanding += 1;
    if (globalThis.__outstanding > globalThis.__peak) globalThis.__peak = globalThis.__outstanding;
    return new Promise(resolve => {
      window.setTimeout(() => {
        globalThis.__outstanding -= 1;
        resolve({ ok: true, json: async () => ({ runs: [] }) });
      }, 5 * INTERVAL);
    });
  };
  const stop = window.startPoll("/crew", () => { globalThis.__handled += 1; }, INTERVAL);
  await globalThis.__advance(10 * INTERVAL);
  stop();
  return {
    peak: globalThis.__peak,
    requests: globalThis.__requests,
    handled: globalThis.__handled,
    hasHelper: typeof window.startPoll === "function",
  };
})().then(out => process.stdout.write(JSON.stringify(out)));
"""


def _run_helper_probe(source: str | None = None) -> dict:
    shared = source if source is not None else SHARED.read_text(encoding="utf-8")
    compiled = serve.compile_jsx(shared, filename="poll-contract-shared.jsx").decode()
    script = f"{TIMER_PRELUDE}\n{compiled}\n{HELPER_PROBE}"
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


def _balanced(text: str, open_index: int) -> str:
    """Return the text inside the parentheses that open at ``open_index``."""
    depth = 0
    quote = None
    index = open_index
    while index < len(text):
        char = text[index]
        if quote is not None:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in "\"'`":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[open_index + 1 : index]
        index += 1
    return ""


def _resolve_callable(source: str, expression: str) -> str:
    """Return the callable text a first argument names, if it is an identifier."""
    expression = expression.strip()
    if not re.fullmatch(r"[A-Za-z_$][\w$]*", expression):
        return expression
    declaration = re.search(
        rf"(?:const|let|var)\s+{re.escape(expression)}\s*=\s*", source
    )
    if declaration:
        rest = source[declaration.end() :]
        cut = rest.find(";")
        return rest if cut < 0 else rest[:cut]
    named = re.search(rf"function\s+{re.escape(expression)}\s*\(", source)
    if named:
        return source[named.start() : named.start() + 800]
    return ""


def _setinterval_fetchers(source: str) -> list[str]:
    """Callbacks passed to ``setInterval`` that reach a ``fetch``."""
    found = []
    for match in re.finditer(r"\bsetInterval\s*\(", source):
        arguments = _balanced(source, match.end() - 1)
        first = arguments.split(",", 1)[0].strip()
        if re.search(r"\bfetch\s*\(", _resolve_callable(source, first)):
            found.append(first)
    return found


def _crew_route_pollers(source: str) -> int:
    """Pollers that request the ``/crew`` route itself, not ``/crew/<project>``."""
    pollers = len(re.findall(r"startPoll\(\s*[\"']/crew[\"']\s*,", source))
    for match in re.finditer(r"\bsetInterval\s*\(", source):
        arguments = _balanced(source, match.end() - 1)
        first = arguments.split(",", 1)[0].strip()
        if re.search(r"fetch\(\s*[\"']/crew[\"']", _resolve_callable(source, first)):
            pollers += 1
    return pollers


def test_helper_keeps_one_request_outstanding_under_a_slow_answer() -> None:
    result = _run_helper_probe()

    assert result["hasHelper"] is True
    # The helper actually polled, so a no-op cannot pass the bound below.
    assert result["handled"] >= 1
    assert result["requests"] >= 1
    # With each answer taking five intervals to arrive, ten intervals never put
    # more than one request in flight.
    assert result["peak"] <= 1


def test_no_surface_starts_a_setinterval_that_fetches() -> None:
    for path in (SHELL, PLAN, CREW):
        source = path.read_text(encoding="utf-8")
        assert _setinterval_fetchers(source) == [], path.name


def test_shell_and_crew_share_one_crew_poll() -> None:
    shell_pollers = _crew_route_pollers(SHELL.read_text(encoding="utf-8"))
    crew_pollers = _crew_route_pollers(CREW.read_text(encoding="utf-8"))

    assert shell_pollers + crew_pollers == 1
    assert shell_pollers == 1
    assert crew_pollers == 0
