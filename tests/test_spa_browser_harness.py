"""Contracts for the browser probe harness itself, without starting a browser."""

from __future__ import annotations

import errno
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from unittest import SkipTest

import pytest

sys.path.insert(0, os.path.dirname(__file__))

import spa_browser_harness as harness
from spa_browser_harness import (
    NAVIGATION_FAULT_REPORT,
    BrowserProbeError,
    _classify_probe_failure,
    _preflight_browser_socket,
    _terminate_process_group,
    file_spa,
    installed_browser_or_skip,
    temporary_browser_profile,
    write_file_spa_document,
)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _leader_with_a_child() -> tuple[subprocess.Popen[str], int]:
    """Start a process that spawns a long-lived child and outlives its own kill.

    This is the shape of the probe driver: it spawns a browser and kills it from
    a `finally` clause, which a hard timeout never reaches.
    """
    leader = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess, sys, time;"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)']);"
            "print(child.pid, flush=True);"
            "time.sleep(600)",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert leader.stdout is not None
    return leader, int(leader.stdout.readline().strip())


def test_timeout_reap_takes_the_spawned_child_with_the_driver() -> None:
    """A timed-out probe must not leave a browser running.

    Signalling only the driver leaves its browser holding the profile directory
    that cleanup then waits on, and consuming the machine that the next probe
    has to meet its own deadline on -- so one timeout makes the following ones
    likelier, and a suite gets slower the longer it runs.
    """
    leader, child = _leader_with_a_child()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            leader.communicate(timeout=1)
        _terminate_process_group(leader)

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and (_alive(leader.pid) or _alive(child)):
            time.sleep(0.05)

        assert not _alive(child), "the driver's child survived the reap"
        assert not _alive(leader.pid)
    finally:
        for pid in (child, leader.pid):
            try:
                os.kill(pid, 9)
            except OSError:
                pass


def test_profile_cleanup_lets_the_body_s_exception_through(tmp_path) -> None:
    """Cleanup runs in a `finally`, so it must not become an exception sink.

    A jump statement leaving that block discards whatever was propagating, which
    turned a probe timeout into an unrelated name error at the call site.
    """
    with pytest.raises(subprocess.TimeoutExpired):
        with temporary_browser_profile(tmp_path) as profile:
            assert profile.is_dir()
            raise subprocess.TimeoutExpired(cmd="probe", timeout=75)


def test_ceiling_exhaustion_names_stderr_and_instance_usage() -> None:
    failure = _classify_probe_failure(
        stage="browser-spawned",
        browser_stderr="inotify_init: Too many open files",
        inotify_count=128,
        inotify_limit=128,
    )

    assert failure.classification == "ceiling-exhausted"
    assert "inotify_init: Too many open files" in str(failure)
    assert "inotify instances 128/128" in str(failure)
    assert NAVIGATION_FAULT_REPORT not in str(failure)
    assert "Network Service above Internet stream-socket creation" not in str(failure)


def test_socket_preflight_names_sandbox_and_worker_role() -> None:
    def denied_socket(_family: int, _kind: int) -> socket.socket:
        raise PermissionError(errno.EPERM, "Operation not permitted")

    with pytest.raises(BrowserProbeError) as captured:
        _preflight_browser_socket(
            socket_factory=denied_socket,
            worker_role="investigate",
            inotify_probe=lambda: (46, 128),
        )

    assert captured.value.classification == "sandbox-denied"
    assert "worker role 'investigate' is the likely cause" in str(captured.value)
    assert "AF_INET" in str(captured.value)
    assert "inotify instances 46/128" in str(captured.value)
    assert NAVIGATION_FAULT_REPORT not in str(captured.value)
    assert "Network Service above Internet stream-socket creation" not in str(
        captured.value
    )


def test_navigation_timeout_skip_reason_names_the_diagnosed_boundary() -> None:
    failure = _classify_probe_failure(
        stage="navigation-started",
        browser_stderr="DevTools connected; navigation pending",
        inotify_count=52,
        inotify_limit=128,
    )

    reason = f"browser unavailable ({failure.classification}): {failure}"

    assert failure.classification == "navigation-never-completed"
    assert "stage 'navigation-started'" in reason
    assert "DevTools connected; navigation pending" in reason
    assert "inotify instances 52/128" in reason
    assert "Network Service above Internet stream-socket creation" in reason
    assert NAVIGATION_FAULT_REPORT in reason
    assert "remains owed against the required host/browser runtime change" in reason


def test_completed_profile_scope_leaves_no_directory(tmp_path: Path) -> None:
    with temporary_browser_profile(tmp_path) as profile:
        assert profile.is_dir()

    assert not profile.exists()


def test_completed_probe_run_leaves_no_profile_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class CompletedProbe:
        def __init__(self, args: list[str], **_kwargs: object) -> None:
            self.args = args
            self.returncode = 0

        def communicate(self, timeout: int) -> tuple[str, str]:
            assert timeout == 75
            return '{"loaded": true}', ""

    monkeypatch.setattr(harness, "_preflight_browser_socket", lambda: None)
    monkeypatch.setattr(harness.subprocess, "Popen", CompletedProbe)

    result = harness._evaluate_browser_url(
        tmp_path,
        "/browser",
        "http://127.0.0.1/",
        "true",
        viewport=(800, 600),
        ready_expression="true",
    )

    assert result == {"loaded": True}
    assert list(tmp_path.glob("browser-profile-*")) == []


def _composed_graph_state() -> dict[str, object]:
    foundation = {
        "slug": "offline-foundation",
        "title": "Offline foundation",
        "type": "plan",
        "status": "shipped",
        "effective_status": "shipped",
        "depends_on": [],
        "decisions": [],
        "project": "nova",
        "impl": 1.0,
    }
    endpoint = {
        "slug": "offline-endpoint",
        "title": "Offline endpoint",
        "type": "plan",
        "status": "active",
        "effective_status": "active",
        "depends_on": ["nova:offline-foundation"],
        "decisions": [],
        "project": "reckon",
        "graph_handle": "graph:offline-capture",
        "impl": 0.5,
    }
    inventory = [foundation, endpoint]
    return {
        "today": "2026-09-01",
        "project": "reckon",
        "projects": [{"project": "reckon", "plans_count": 2}],
        "milestones": [],
        "north_stars": [],
        "inventory": inventory,
        "source_format": "distributed",
        "resource_versions": {},
        "loaded_at": "2026-09-01T00:00:00Z",
        "active_sprint_id": None,
        "active_sprints": [],
        "active_sprint_conflict": False,
        "sprints": [],
        "sprint": None,
        "blockers": [],
        "timeline": [],
        "attachment_relations": [],
        "plans": {item["slug"]: item for item in inventory},
    }


def test_file_spa_document_inlines_state_styles_and_scripts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import reckon.serve

    monkeypatch.setattr(
        reckon.serve,
        "client_runtime_assets",
        lambda: {
            "react.js": b"window.React = {};",
            "react-dom.js": b"window.ReactDOM = {};",
        },
    )
    monkeypatch.setattr(
        reckon.serve,
        "compile_jsx",
        lambda source, *, filename: f"/* {filename} */\n{source}".encode(),
    )

    page = write_file_spa_document(
        tmp_path / "offline.html",
        _composed_graph_state(),
        project="reckon",
    )
    source = page.read_text(encoding="utf-8")

    assert page.parent == tmp_path
    assert source.count("<!doctype html>") == 1
    assert 'href="/_shared/' not in source
    assert 'href="/_ui/' not in source
    assert '<script src="/_' not in source
    assert '"graph_handle":"graph:offline-capture"' in source
    assert "window.STATE_READY = Promise.resolve(window.STATE)" in source
    assert "window.revalidateProjectState = async () => window.STATE" in source


def test_file_spa_graph_probe_is_populated_and_repeatable(tmp_path: Path) -> None:
    browser = installed_browser_or_skip()

    expression = """(() => ({
      count: document.querySelectorAll('.r-graph-node-card').length,
      handle: document.querySelector('.r-graph-handle-token')?.textContent || '',
      empty: document.querySelector('.r-graph-empty')?.textContent || ''
    }))()"""
    ready_expression = (
        "document.querySelectorAll('.r-graph-node-card').length > 0 && "
        "Boolean(document.querySelector('.r-graph-handle-token'))"
    )
    with file_spa(
        tmp_path,
        browser,
        _composed_graph_state(),
        route="#graph",
    ) as context:
        assert context.url.startswith("file:")
        first = context.run_probe(
            expression,
            viewport=(900, 420),
            ready_expression=ready_expression,
        )
        second = context.run_probe(
            expression,
            viewport=(900, 420),
            ready_expression=ready_expression,
        )

    assert first == second
    assert first == {
        "count": 2,
        "handle": "graph:offline-capture",
        "empty": "",
    }
    assert list(tmp_path.glob("browser-profile-*")) == []
    assert list(tmp_path.glob("file-spa-*")) == []


def test_file_spa_browser_absence_uses_the_stated_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(harness, "installed_browser", lambda: None)

    with pytest.raises(
        SkipTest,
        match="no supported browser binary is installed; tried google-chrome, chromium, chromium-browser",
    ):
        installed_browser_or_skip()
