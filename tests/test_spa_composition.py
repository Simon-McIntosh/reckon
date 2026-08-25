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


ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests" / "spa_render_capture.mjs"
BROWSER_NAMES = ("google-chrome", "chromium", "chromium-browser")


def _installed_browser() -> str | None:
    return next((path for name in BROWSER_NAMES if (path := shutil.which(name))), None)


def _unused_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


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


def _run_harness(browser: str, url: str, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "node",
            str(HARNESS),
            "--browser",
            browser,
            "--url",
            url,
            "--expected-width",
            "1374",
            *extra,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=75,
    )


def test_browser_harness_reports_an_absent_binary_as_a_clean_skip(tmp_path: Path) -> None:
    absent = tmp_path / "browser-is-not-installed"
    result = _run_harness(str(absent), "http://127.0.0.1:1/")

    assert result.returncode == 77
    assert result.stderr.strip() == f"SKIP: browser binary is not present: {absent}"


def test_served_spa_composes_the_active_view_inside_its_container(tmp_path: Path) -> None:
    browser = _installed_browser()
    if browser is None:
        pytest.skip(
            "SPA composition check requires an installed browser; tried "
            + ", ".join(BROWSER_NAMES)
        )

    with _served_spa(tmp_path) as url:
        passing = _run_harness(browser, url)
        conflicting = _run_harness(browser, url, "--conflicting-width", "1600")

    assert passing.returncode == 0, passing.stderr
    geometry = json.loads(passing.stdout)
    assert geometry["visibleViewCount"] == 1
    assert geometry["app"]["width"] == pytest.approx(1374, abs=1)
    assert geometry["view"]["width"] == pytest.approx(geometry["app"]["width"], abs=1)
    assert geometry["view"]["top"] == pytest.approx(geometry["topbar"]["bottom"], abs=1)
    assert geometry["view"]["bottom"] == pytest.approx(geometry["app"]["bottom"], abs=1)

    assert conflicting.returncode == 1
    assert "composed container geometry mismatch" in conflicting.stderr
    assert "app width: 1600, expected 1374" in conflicting.stderr
