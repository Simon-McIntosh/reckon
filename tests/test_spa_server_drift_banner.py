"""The shell shows the server's own drift verdict and nothing it derived itself."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TOPBAR_SOURCE = (REPO_ROOT / "docs" / "ui" / "shell-topbar.jsx").read_text()
SHELL_SOURCE = (REPO_ROOT / "docs" / "ui" / "shell.jsx").read_text()


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


def _notice(report: object) -> object:
    script = _function_source(TOPBAR_SOURCE, "serverDriftNotice")
    result = subprocess.run(
        [
            "node",
            "-e",
            f"{script}\nconsole.log(JSON.stringify(serverDriftNotice({json.dumps(report)}) ?? null));",
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_a_stale_report_renders_the_servers_own_sentence_and_command():
    summary = "The server is running older code than is on disk: 3 files changed."
    notice = _notice(
        {
            "code": {
                "stale": True,
                "summary": summary,
                "restart_command": "reckon service restart",
            }
        }
    )
    assert notice == {"summary": summary, "command": "reckon service restart"}


def test_a_current_or_absent_report_renders_nothing():
    assert _notice({"code": {"stale": False, "summary": None}}) is None
    assert _notice({"code": None}) is None
    assert _notice(None) is None


def test_the_shell_mounts_the_banner_outside_the_reading_mode_guard():
    mount = "{window.ReckonShell.topbar.ServerDriftBanner && <window.ReckonShell.topbar.ServerDriftBanner />}"
    assert mount in SHELL_SOURCE
    topbar_guard = SHELL_SOURCE.index(
        "{!readingMode && <window.ReckonShell.topbar.TopBar"
    )
    guard_end = SHELL_SOURCE.index("/>}", topbar_guard)
    assert SHELL_SOURCE.index(mount) > guard_end
    assert '"ServerDriftBanner"' not in TOPBAR_SOURCE
    assert "ServerDriftBanner, TopBar };" in TOPBAR_SOURCE
