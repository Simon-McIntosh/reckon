from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from urllib.error import URLError
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
COMPOSITION_PROBE = ROOT / "tests" / "spa_render_capture.mjs"
BROWSER_NAMES = ("google-chrome", "chromium", "chromium-browser")


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

    def run_composition_probe(
        self,
        *extra: str,
        expected_width: int = 1374,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "node",
                str(COMPOSITION_PROBE),
                "--browser",
                self.browser,
                "--url",
                self.url,
                "--expected-width",
                str(expected_width),
                *extra,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=75,
        )


@contextmanager
def served_spa(
    tmp_path: Path,
    browser: str,
    *,
    docs: Path = ROOT / "docs",
    route: str = "#cockpit",
) -> Iterator[ServedSpa]:
    port = _unused_port()
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
                with urlopen(url, timeout=0.5) as response:
                    if response.status == 200:
                        break
            except URLError:
                time.sleep(0.1)
        else:
            raise AssertionError("reckon server did not become ready within 15 seconds")
        yield ServedSpa(browser=browser, url=url)
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)
