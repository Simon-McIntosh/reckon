"""The browser harness resolves Playwright's installed headless shell.

A host can carry a browser the harness never looks for. Playwright installs its
Chromium headless shell under ``PLAYWRIGHT_BROWSERS_PATH`` or
``~/.cache/ms-playwright``, and when nothing is on PATH the harness used to skip
every browser-driven test rather than use it. These checks hold the resolution
the node depends on: the shell is found when it is the only browser present, the
newest installed revision wins, and when nothing resolves the skip message names
every location that was checked so the skip stays legible.
"""

from __future__ import annotations

from pathlib import Path
from unittest import SkipTest

import pytest

from tests.spa_browser_harness import (
    BROWSER_NAMES,
    PLAYWRIGHT_HEADLESS_SHELL_RELATIVE,
    installed_browser,
    installed_browser_or_skip,
    playwright_browsers_root,
)


def write_headless_shell(root: Path, revision: int) -> Path:
    """One executable stub at the path Playwright installs a shell to."""

    shell = (
        root
        / f"chromium_headless_shell-{revision}"
        / PLAYWRIGHT_HEADLESS_SHELL_RELATIVE
    )
    shell.parent.mkdir(parents=True)
    shell.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    shell.chmod(0o755)
    return shell


def without_path_browsers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make PATH carry no browser and drop the explicit override."""

    monkeypatch.delenv("RECKON_BROWSER", raising=False)
    monkeypatch.setenv("PATH", "")


def test_the_playwright_headless_shell_resolves_when_path_has_no_browser(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    without_path_browsers(monkeypatch)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    shell = write_headless_shell(tmp_path, 1243)

    assert installed_browser() == str(shell)
    assert installed_browser_or_skip() == str(shell)


def test_the_newest_installed_playwright_revision_wins(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    without_path_browsers(monkeypatch)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    write_headless_shell(tmp_path, 1200)
    newest = write_headless_shell(tmp_path, 1243)

    assert installed_browser() == str(newest)


def test_the_skip_message_names_every_location_checked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    without_path_browsers(monkeypatch)
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(empty))
    monkeypatch.setenv("HOME", str(empty))

    with pytest.raises(SkipTest) as denial:
        installed_browser_or_skip()

    reason = str(denial.value)
    for name in BROWSER_NAMES:
        assert name in reason
    assert "RECKON_BROWSER" in reason
    assert str(playwright_browsers_root()) in reason
    assert PLAYWRIGHT_HEADLESS_SHELL_RELATIVE in reason
