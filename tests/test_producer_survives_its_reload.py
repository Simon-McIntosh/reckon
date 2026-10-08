"""A producer reload cannot turn its poll signal into its own death."""

from reckon import cli
from reckon.crew import runs
from tests.test_producer_takes_new_code import (
    NODE,
    PROJECT,
    RELOAD_WITHIN_SECONDS,
    _await,
    _await_seat,
    _copy_source,
    _launch_producer,
    _read_until,
    _seat_record,
    _stamp_of,
    _write_live_run,
)


def test_a_producer_keeps_publishing_after_a_slow_reload(tmp_path, monkeypatch) -> None:
    """A poll arriving during the import proof cannot kill the new image."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    root, package = _copy_source(tmp_path)
    follower_path = package / "crew_follow_commands.py"
    source = follower_path.read_text()
    needle = '    "import pathlib, sys\\n"\n'
    assert needle in source
    follower_path.write_text(
        source.replace(
            needle,
            '    "import pathlib, sys, time\\n"\n    "time.sleep(2)\\n"\n',
            1,
        )
    )
    before = _stamp_of(root)
    process, lines = _launch_producer(root, home)
    try:
        _await_seat(PROJECT, before)
        module = package / "crew" / "recovery_watch.py"
        module.write_bytes(module.read_bytes() + b"\n# producer reload probe\n")
        after = _stamp_of(root)

        outcome = _await(
            lambda: (
                f"exited {process.poll()}"
                if process.poll() is not None
                else "reloaded"
                if _seat_record(PROJECT).get("code_stamp") == after
                else ""
            ),
            RELOAD_WITHIN_SECONDS,
            "the producer neither reloaded nor reported an exit",
        )
        assert outcome == "reloaded", outcome
        _read_until(lines, "completed its reload")
        _write_live_run(home, "r-after-reload")
        _read_until(lines, NODE)
        assert process.poll() is None
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)

    monkeypatch.setattr(
        runs,
        "watch_producer_identity",
        lambda project: {
            "reload_started_at": "2026-10-02T12:58:12Z",
            "log_path": str(tmp_path / "watch.log"),
        },
    )
    monkeypatch.setattr(runs, "producer_live", lambda project: False)
    event = next(cli._follow_watch_lines(PROJECT, sleeper=lambda seconds: None))
    assert event["event"] == cli.FOLLOWER_PRODUCER_RELOAD_FAILED_EVENT
    assert "stopped during its reload" in event["line"]
