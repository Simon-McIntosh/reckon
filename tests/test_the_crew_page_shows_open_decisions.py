"""The crew page renders the open decisions holding work.

The lead reads the crew page to see what the fleet is doing and what waits on
them. The open decisions ride the ``/crew`` payload the page polls, and this
case drives a real browser over a served temporary project holding one open
decision, so the row is asserted as it reaches the reader rather than as the
component would draw it given props.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from reckon._plan_html import write_state
from tests.spa_browser_harness import ServedSpa, installed_browser_or_skip

ROOT = Path(__file__).resolve().parents[1]
#: Starting the server loads the service snapshot, which on this filesystem
#: takes longer than the harness's own readiness deadline; the wait here is
#: generous so a slow start is not read as a broken page.
_SERVER_BOOT_SECONDS = 90.0
PROJECT = "sample"
SLUG = "waiting-work"
DECISION_KEY = "transport"
DECISION_TITLE = "Which transport carries the payload?"
SCOPED_SECTION = "s2"
#: The plan's own last-modified date, fixed in the fixture rather than taken
#: from today, so the case cannot pass on the day it is written and expire.
MODIFIED = "2026-10-01"

_PLAN_DOCUMENT = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    f'<meta name="docs-project" content="{PROJECT}">'
    f"<title>{SLUG}</title></head>"
    '<body><main class="plan-doc">'
    '<h2 id="s1">§1 — The work</h2>'
    f'<h2 id="{SCOPED_SECTION}">§2 — The payload</h2>'
    "</main></body></html>"
)


def _write_project(docs: Path, *, locked: bool) -> Path:
    """Write the temporary project's one plan, with its decision open or locked.

    The locked form is longer than the open one, so a parse memo keyed on the
    file's signature cannot hand the earlier decision state to the later read.
    """

    plans = docs / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    plan = plans / f"{SLUG}.html"
    plan.write_text(
        write_state(
            _PLAN_DOCUMENT,
            {
                "slug": SLUG,
                "title": SLUG,
                "status": "active",
                "modified": MODIFIED,
                "decisions": {
                    DECISION_KEY: {
                        "title": DECISION_TITLE,
                        "choices": ["accept", "reject"],
                        "option_labels": {"accept": "accept", "reject": "reject"},
                        "choice": "accept" if locked else "",
                        "rationale": "",
                        "recommended": "accept",
                        "recommended_by": "surface review",
                        "sections": [SCOPED_SECTION],
                    }
                },
                "gates": [],
                "followups": [],
                "version": 0,
            },
        ),
        encoding="utf-8",
    )
    return plan


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@contextmanager
def _served_project(tmp_path: Path, docs: Path) -> Iterator[ServedSpa]:
    """Serve the temporary project on a real server for the crew page.

    The whole mounted tree is temporary — the plans the rows derive from and the
    directory the server keeps its own state in — so nothing outside ``tmp_path``
    is read or written. The server's output lands in the case's own directory
    rather than in a pipe nobody drains.
    """

    port = _free_port()
    mounts = tmp_path / "mounts.json"
    mounts.write_text(json.dumps({PROJECT: str(docs)}), encoding="utf-8")
    server_home = Path(tempfile.mkdtemp(prefix="reckon-server-", dir=tmp_path))
    log_path = tmp_path / "server.log"
    with log_path.open("w", encoding="utf-8") as log:
        server = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "from reckon.serve import main; "
                    "main(port=int(__import__('sys').argv[1]), host='127.0.0.1', "
                    "mounts_file=__import__('pathlib').Path(__import__('sys').argv[2]))"
                ),
                str(port),
                str(mounts),
            ],
            cwd=ROOT,
            env=dict(os.environ, RECKON_HOME=str(server_home)),
            stdout=subprocess.DEVNULL,
            stderr=log,
        )
        url = f"http://127.0.0.1:{port}/{PROJECT}/#crew"
        deadline = time.monotonic() + _SERVER_BOOT_SECONDS
        try:
            while time.monotonic() < deadline:
                if server.poll() is not None:
                    raise AssertionError(
                        "reckon server exited before readiness: "
                        + log_path.read_text(encoding="utf-8")
                    )
                try:
                    with urlopen(
                        f"http://127.0.0.1:{port}/{PROJECT}/", timeout=2
                    ) as response:
                        if response.status == 200:
                            break
                except (URLError, TimeoutError):
                    time.sleep(0.25)
            else:
                raise AssertionError(
                    "reckon server did not become ready within "
                    f"{_SERVER_BOOT_SECONDS:g} seconds: "
                    + log_path.read_text(encoding="utf-8")[-2000:]
                )
            yield ServedSpa(
                browser=installed_browser_or_skip(),
                url=url,
                tmp_path=tmp_path,
            )
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
            shutil.rmtree(server_home, ignore_errors=True)


def _mounts_preload(docs: Path) -> str:
    """Answer the shell's project list with the temporary project.

    The mount the page is served from is the harness's own file, so the shell's
    own ``/_projects/mounts.json`` read answers nothing here; without a project
    on that list the surface draws no row whatever the payload holds.
    """

    return (
        "(() => {\n"
        "  const nativeFetch = window.fetch.bind(window);\n"
        "  const respond = payload => new Response(JSON.stringify(payload), {\n"
        "    status: 200, headers: { 'Content-Type': 'application/json' },\n"
        "  });\n"
        f"  const mounts = {json.dumps({PROJECT: str(docs)})};\n"
        "  window.fetch = (resource, options) => {\n"
        "    const url = String(resource);\n"
        "    if (url.endsWith('/_projects/mounts.json')) return Promise.resolve(respond(mounts));\n"
        "    return nativeFetch(resource, options);\n"
        "  };\n"
        "})()"
    )


_PROBE = """(async () => {
  const waitFor = async predicate => {
    const deadline = performance.now() + 10000;
    while (performance.now() < deadline) {
      if (predicate()) return true;
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    return false;
  };
  await waitFor(() => Boolean(document.querySelector(".r-crew-decisions")));
  // The row arrives on a poll that answers after the first render, so the
  // assertions wait for the decision itself rather than for the section only.
  await waitFor(() => Boolean(
    document.querySelector(".r-crew-decision")
    || document.querySelector(".r-crew-decisions-empty")
  ));
  const section = document.querySelector(".r-crew-decisions");
  const row = section && section.querySelector(".r-crew-decision");
  const link = row && row.querySelector(".r-crew-decision-plan");
  const holds = row && row.querySelector(".r-crew-decision-holds");
  return {
    drawn: Boolean(section),
    text: section ? section.textContent : "",
    key: row ? row.querySelector(".r-crew-decision-key").textContent : "",
    question: row ? row.querySelector(".r-crew-decision-question").textContent : "",
    recommendation: row
      ? row.querySelector(".r-crew-decision-recommendation").textContent
      : "",
    href: link ? link.getAttribute("href") : "",
    holds: holds ? holds.textContent : "",
    rowCount: section ? section.querySelectorAll(".r-crew-decision").length : 0,
    empty: Boolean(section && section.querySelector(".r-crew-decisions-empty")),
  };
})()"""


def test_the_crew_page_shows_the_open_decision_it_is_served(tmp_path: Path) -> None:
    docs = tmp_path / "repo" / "docs"
    _write_project(docs, locked=False)

    with _served_project(tmp_path, docs) as spa:
        seen = spa.run_probe(
            _PROBE,
            ready_expression="Boolean(window.STATE)",
            preload_expression=_mounts_preload(docs),
        )

    assert isinstance(seen, dict), seen
    assert seen["drawn"] is True, seen
    assert seen["empty"] is False, seen
    assert seen["empty"] is False, seen
    assert seen["key"] == DECISION_KEY, seen
    assert DECISION_TITLE in seen["question"], seen
    assert f"/{PROJECT}/#plan/{SLUG}" == seen["href"], seen
    assert SCOPED_SECTION in seen["holds"], seen
    assert "accept" in seen["recommendation"], seen


def test_the_crew_page_says_no_decision_waits_once_it_is_locked(tmp_path: Path) -> None:
    docs = tmp_path / "repo" / "docs"
    _write_project(docs, locked=False)

    with _served_project(tmp_path, docs) as spa:
        before = spa.run_probe(
            _PROBE,
            ready_expression="Boolean(window.STATE)",
            preload_expression=_mounts_preload(docs),
        )
        # The server reads the plan per request, so locking it is a write to
        # the same tree and the next poll answers with no row.
        _write_project(docs, locked=True)
        after = spa.run_probe(
            _PROBE,
            ready_expression="Boolean(window.STATE)",
            preload_expression=_mounts_preload(docs),
        )

    assert isinstance(before, dict) and isinstance(after, dict)
    assert before["key"] == DECISION_KEY, before
    assert before["empty"] is False, before
    assert after["rowCount"] == 0, after
    assert after["empty"] is True, after
    assert "No decision is waiting." in after["text"], after
    assert DECISION_KEY not in after["text"], after
