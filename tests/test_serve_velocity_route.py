"""The velocity view is served by the docs server at GET /crew/velocity.

The route is transport only: ``reckon.velocity.view`` composes the payload, so
the server's JSON must equal what the crew read view answers for the same
window — the parity case below is the whole point of routing through the one
composition. The fixtures are a synthesised git repository and a committed run
ledger built the way ``tests/test_crew_velocity_view.py`` builds them, under
``tmp_path``; the server reads its mounts from the same isolated config home
the read view resolves through, so nothing outside ``tmp_path`` is touched.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import quote

import pytest

from reckon import mcp, serve, velocity

REPO_ROOT = Path(__file__).resolve().parents[1]

DAY = 86400
# Anchored on the run clock rather than a calendar date: the default window the
# server opens when a caller names none is the last fourteen days from now, so
# the fixture's commits must fall inside it to exercise the composed payload.
BASE = int(time.time()) - 12 * DAY
WINDOW_START = velocity.iso(BASE + 1 * DAY)
WINDOW_END = velocity.iso(BASE + 8 * DAY)
BRANCH = "main"

_DISPATCH_IDENTITY = ("RECKON_RUN_ID", "RECKON_MANIFEST", "RECKON_ATTEMPT_STARTED_AT")


def _iso(day: int, seconds: int = 0) -> str:
    return velocity.iso(BASE + day * DAY + seconds)


def _git(repo: Path, *arguments: str, env: dict | None = None) -> str:
    base = {**os.environ, **(env or {})}
    for name in _DISPATCH_IDENTITY:
        base.pop(name, None)
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env=base,
    )
    return result.stdout.strip()


def _commit(repo: Path, message: str, day: int, changes: dict[str, str | None]) -> str:
    when = _iso(day)
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": when,
        "GIT_COMMITTER_DATE": when,
        "GIT_AUTHOR_NAME": "fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    }
    for path, content in changes.items():
        target = repo / path
        if content is None:
            _git(repo, "rm", "-q", path, env=env)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            _git(repo, "add", path, env=env)
    _git(repo, "commit", "-q", "--allow-empty", "-m", message, env=env)
    return _git(repo, "rev-parse", "HEAD", env=env)


def _ledger_row(run_id: str, node: str, role: str, backend: str, day: int) -> dict:
    return {
        "run_id": run_id,
        "node": node,
        "plan": "p",
        "role": role,
        "gate": "passed",
        "backend": backend,
        "dispatched_at": _iso(day),
        "completed_at": _iso(day, 100),
        "worker_seconds": 100,
        "lineage": {},
        "attempt": 1,
    }


def _build_alpha(root: Path) -> Path:
    repo = root / "alpha"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", BRANCH)
    runs = [
        _ledger_row("r-impl", "impl-node", "implement", "claude", 4),
        _ledger_row("r-review", "review-node", "review", "codex", 4),
    ]
    _commit(
        repo,
        "chore: seed the ledger",
        0,
        {"docs/state/alpha/crew.json": json.dumps({"data": {"runs": runs}}, indent=2)},
    )
    _commit(repo, "feat: add source", 1, {"src/a.py": "l1\nl2\nl3\nl4\nl5\n"})
    _commit(repo, "test: add tests", 2, {"tests/test_a.py": "t1\nt2\nt3\n"})
    _commit(
        repo,
        "docs(plan): add a plan",
        3,
        {"docs/plans/p.html": "<p>a</p>\n<p>b</p>\n<p>c</p>\n<x></x>\n<p>d</p>\n"},
    )
    _commit(repo, "promote(r-impl)", 5, {})
    _commit(repo, "promote(r-review)", 6, {})
    return repo


@pytest.fixture()
def served(tmp_path, isolated_reckon_home, monkeypatch):
    alpha = _build_alpha(tmp_path / "code")
    mounts = isolated_reckon_home / "mounts.json"
    mounts.write_text(json.dumps({"alpha": str(alpha / "docs")}), encoding="utf-8")
    # The route resolves through the server's mount table; the read view
    # resolves through the config-home mounts. Pin both at the same file so the
    # parity case compares one repository.
    monkeypatch.setattr(serve, "_MOUNTS_FILE", mounts)
    server = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield alpha, server.server_address
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _get(address, path: str) -> tuple[int, object]:
    connection = HTTPConnection(*address, timeout=60)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read()
        return response.status, json.loads(body)
    finally:
        connection.close()


def test_the_route_equals_the_crew_read_view(served):
    _, address = served
    status, payload = _get(
        address,
        f"/crew/velocity?project=alpha&since={quote(WINDOW_START)}"
        f"&until={quote(WINDOW_END)}",
    )

    assert status == 200
    expected = mcp._crew("alpha", view="velocity", since=WINDOW_START, until=WINDOW_END)
    assert payload == expected
    # The payload is the view's, not a re-derivation: the split is present and
    # both promotion classes are reported.
    raised = payload["by_project"][0]["metrics"]["promoted_nodes"]
    assert raised["implement_class"] == 1
    assert raised["review_investigate"] == 1


def test_a_missing_since_returns_400_carrying_the_refusal(served):
    _, address = served
    status, payload = _get(
        address,
        f"/crew/velocity?project=alpha&until={quote(WINDOW_END)}",
    )

    assert status == 400
    expected = mcp._crew("alpha", view="velocity", since=None, until=WINDOW_END)
    assert expected["ok"] is False
    assert payload["detail"] == expected["detail"]
    assert "since" in payload["detail"]


def test_the_default_window_is_the_last_fourteen_days(served):
    _, address = served
    status, payload = _get(address, "/crew/velocity?project=alpha")

    assert status == 200
    start = velocity.stamp(payload["window"]["start"])
    end = velocity.stamp(payload["window"]["end"])
    assert start is not None and end is not None
    assert end - start == pytest.approx(14 * DAY, abs=5 * 60)


def test_the_spa_loads_velocity_before_shell():
    html = (REPO_ROOT / "docs" / "index.html").read_text(encoding="utf-8")
    modules = [
        reference.lstrip("/")
        for reference in re.findall(r'<script[^>]+src="([^"]+)"', html)
        if reference.lstrip("/").startswith("_ui/")
    ]

    assert "_ui/velocity.js" in modules
    assert modules.index("_ui/velocity.js") < modules.index("_ui/shell.js")


def test_the_work_tabs_carry_the_velocity_key():
    source = (REPO_ROOT / "docs" / "ui" / "shell-route.jsx").read_text(encoding="utf-8")
    block = re.search(r"const WORK_TABS = \[(.*?)\];", source, re.DOTALL)
    assert block is not None
    assert re.search(r'key:\s*"velocity"', block.group(1))
    assert re.search(r'label:\s*"Velocity"', block.group(1))
