"""A live crew follower adopts code changes without losing its stream."""

from __future__ import annotations

import io
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from reckon import cli as cli_module
from reckon.crew import runs


@pytest.fixture()
def isolated_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


def _event(number: int) -> dict:
    return {
        "project": "proj",
        "event": "transition",
        "observed_at": "2026-09-04T04:00:00+00:00",
        "run_id": f"run-{number}",
        "node": f"node-{number}",
        "session": "session-a",
        "role": "implement",
        "backend": "local",
        "model": "model",
        "alias": "model",
        "effort": "medium",
        "from_state": "starting",
        "to_state": "working",
        "working": 1,
        "blocked": 0,
        "unpromoted": 0,
        "detail": "",
        "needs_help_complete": False,
    }


def _wait_for_registration(project: str, session: str) -> dict:
    deadline = time.monotonic() + 8
    state = runs.follower_state(project, session)
    while time.monotonic() < deadline and not state["registered"]:
        time.sleep(0.02)
        state = runs.follower_state(project, session)
    assert state["registered"], "the running follower never registered"
    return state


def test_running_follower_reloads_without_stream_or_registration_gap(
    isolated_home, tmp_path
) -> None:
    source_package = Path(cli_module.__file__).resolve().parent
    copied_root = tmp_path / "source"
    copied_package = copied_root / "reckon"
    shutil.copytree(source_package, copied_package)

    executable = Path(sys.executable).with_name("reckon")
    environment = {
        **os.environ,
        "PYTHONPATH": str(copied_root),
        "RECKON_HOME": str(isolated_home),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
    }
    process = subprocess.Popen(
        [
            str(executable),
            "crew",
            "follow",
            "--project",
            "proj",
            "--session",
            "session-a",
            "--no-color",
            "--width",
            "240",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    assert process.stdout is not None
    lines: queue.Queue[str] = queue.Queue()
    reader = threading.Thread(
        target=lambda: [lines.put(line.rstrip("\n")) for line in process.stdout],
        daemon=True,
    )
    reader.start()

    try:
        with runs._project_watch_claim("proj", "1h") as (acquired, registration):
            assert acquired
            first_registration = _wait_for_registration("proj", "session-a")
            original_pid = first_registration["follower"]["pid"]
            stream_path = Path(registration["stream_path"])

            runs._append_watch_lines(stream_path, [_event(0)])
            first_line = lines.get(timeout=5)
            assert "node-0" in first_line
            assert not first_line.startswith("fresh:")

            renderer = copied_package / "crew" / "recovery.py"
            original = renderer.read_text()
            return_line = (
                "return (ticker or _PLAIN).render(event, with_session=with_session)"
            )
            assert original.count(return_line) == 1
            renderer.write_text(
                original.replace(
                    return_line,
                    "return 'fresh:' + (ticker or _PLAIN).render("
                    "event, with_session=with_session)",
                )
            )

            expected_nodes = {f"node-{number}" for number in range(1, 19)}
            observed_registration = []
            for number in range(1, 19):
                runs._append_watch_lines(stream_path, [_event(number)])
                observed_registration.append(
                    runs.follower_state("proj", "session-a")["registered"]
                )
                time.sleep(0.12)

            subsequent = [lines.get(timeout=5) for _ in expected_nodes]

        assert any(line.startswith("fresh:") for line in subsequent), (
            "the replacement stayed alive but did not perform the changed behaviour"
        )
        rendered_nodes = {
            node
            for node in expected_nodes
            for line in subsequent
            if node in line.split()
        }
        assert rendered_nodes == expected_nodes
        assert (
            sum(any(node in line for node in expected_nodes) for line in subsequent)
            == 18
        )
        assert all(observed_registration)
        final_registration = runs.follower_state("proj", "session-a")
        assert final_registration["follower"]["pid"] == original_pid
        assert process.pid == original_pid
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_failed_reexec_reports_once_and_keeps_the_registration(
    monkeypatch,
) -> None:
    stamps = iter(["imported", "changed"])
    monkeypatch.setattr(runs, "follower_code_stamp", lambda: next(stamps, "changed"))
    monkeypatch.setattr(
        cli_module.os,
        "execv",
        lambda *_args: (_ for _ in ()).throw(OSError("execution refused")),
    )
    output = io.StringIO()

    class Registration:
        prepared = False
        cancelled = False

        def prepare_reexec(self) -> None:
            self.prepared = True

        def cancel_reexec(self) -> None:
            self.cancelled = True

    registration = Registration()
    reloader = cli_module._FollowerReloader("proj", registration, stream=output)
    reloader.poll({"reported": {"run": "working"}, "offset": 12})
    reloader.poll({"reported": {"run": "working"}, "offset": 12})

    assert registration.prepared is True
    assert registration.cancelled is True
    assert output.getvalue().count("could not reload itself") == 1
    assert "cycle it with:" in output.getvalue()
    assert cli_module._FOLLOWER_CHECKPOINT_ENV not in os.environ
