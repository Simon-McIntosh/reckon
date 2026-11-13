"""A row replayed when a follower attaches reads as [replay], not as news.

A Monitor reads the follower through a pipe, which has no scrollback, so a row
replayed on attach and a transition written a second ago look identical in the
pane. The follower therefore marks everything it derives from what was already
on disk when it attached — the baseline row of every run it re-announces, and
every transition the stream gained before the attach — with a fixed prefix, and
``crew follow --help`` states the prefix so a line filter can exclude it. A
transition written after the attach keeps the live format, and the live-work
snapshot a pipe resume draws is state rather than news, so it is left unmarked.

The cases below drive the wired ``crew follow`` command against a temporary
crew home. The post-attach transition is appended at the moment the attach
captures its byte boundary, so it is provably written after the attach rather
than after a sleep long enough to look like one.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli, crew, crew_follow_commands
from reckon.crew import runs

PROJECT = "replay-prefix-proj"
SESSION = "s-replay-prefix"

# Two review runs delivered days before the attach, one run whose transition was
# written the previous evening, and one run that moves while the pane is armed.
RUN_REVIEW_A = "r-replay-review-of-nova-a"
RUN_REVIEW_B = "r-replay-review-of-nova-b"
RUN_MOVED = "r-replay-h200-levers"
RUN_LIVE = "r-replay-arrives-live"

ARM_LIFETIME = "1s"
PREFIX = "[replay] "


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every crew directory at a temporary config home."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture()
def follow_lines(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture the follower command's own rendered lines instead of writing them."""
    lines: list[str] = []

    def capture(line, *, stream=None):
        lines.append(line)

    monkeypatch.setattr(crew_follow_commands, "_echo_follow_line", capture)
    return lines


def _write_pointer(
    home: Path, run_id: str, *, phase: str, created_at: str | None = None
) -> None:
    log = home / "logs" / f"{run_id}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('{"type":"turn.started"}\n', encoding="utf-8")
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": SESSION,
            "node": {"id": run_id, "plan": "replay-plan", "time_budget": "20m"},
            "phase": phase,
            "created_at": created_at,
            "manifest_path": str(home / "manifests" / f"{run_id}.md"),
            "log_path": str(log),
            "process_alive": None,
        },
    )


def _deliver(home: Path, run_id: str, status: str, *, at: float | None = None) -> None:
    """Write the worker's manifest, moving the run to the state it words."""
    manifest = home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {run_id}\nstatus: {status}\ncommits: HEAD\nblockers: none\n",
        encoding="utf-8",
    )
    if at is not None:
        os.utime(manifest, (at, at))


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()


def _event(
    run_id: str, *, state: str, observed_at: str, event: str = "transition"
) -> dict:
    return {
        "project": PROJECT,
        "event": event,
        "run_id": run_id,
        "node": run_id,
        "session": SESSION,
        "from_state": "working",
        "to_state": state,
        "working": 1,
        "blocked": 0,
        "unpromoted": 0,
        "observed_at": observed_at,
        "legacy": False,
    }


def _append_stream(stream_path: Path, events: list[dict]) -> None:
    with stream_path.open("a", encoding="utf-8") as handle:
        for event in events:
            handle.write(f"{json.dumps(event)}\n")


def _fixture(home: Path) -> tuple[float, float]:
    """Three runs the store already holds, and the times that date them."""
    now = time.time()
    three_days_ago = now - 3 * 24 * 3600
    last_evening = now - 12 * 3600
    for run_id in (RUN_REVIEW_A, RUN_REVIEW_B):
        _write_pointer(home, run_id, phase="working", created_at=_iso(three_days_ago))
        _deliver(home, run_id, "complete", at=three_days_ago)
    _write_pointer(home, RUN_MOVED, phase="working", created_at=_iso(last_evening))
    _deliver(home, RUN_MOVED, "failed", at=last_evening)
    return three_days_ago, last_evening


def _arm(*, extra_args: list[str] | None = None) -> None:
    result = CliRunner().invoke(
        cli.crew,
        [
            "follow",
            "--project",
            PROJECT,
            "--session",
            SESSION,
            "--lifetime",
            ARM_LIFETIME,
            "--no-color",
            "--width",
            "200",
            *(extra_args or []),
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output


def _fleet_lines(lines: list[str]) -> list[str]:
    """The drawn fleet rows, without the pane's framing or the follower's end."""
    return [
        line
        for line in lines
        if line.strip() and "follower end" not in line and "── " not in line
    ]


def _naming(lines: list[str], run_id: str) -> list[str]:
    return [line for line in _fleet_lines(lines) if run_id in line]


def _append_after_the_attach(
    monkeypatch: pytest.MonkeyPatch, events: list[dict]
) -> None:
    """Append transitions at the instant the attach captures its boundary.

    The boundary is read after the fleet snapshot and before any replay, so a
    row written there is provably written after the attach rather than after a
    sleep the test hopes was long enough.
    """
    real_boundary = crew_follow_commands._follow_boundary
    written = {"done": False}

    def boundary(path):
        value = real_boundary(path)
        if not written["done"]:
            written["done"] = True
            _append_stream(Path(path), events)
        return value

    monkeypatch.setattr(crew_follow_commands, "_follow_boundary", boundary)


# ── Case 1: the attach marks what it replays ────────────────────────────────


def test_a_row_replayed_on_attach_is_prefixed_and_a_live_row_is_not(
    home: Path, follow_lines: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    three_days_ago, last_evening = _fixture(home)
    live = _event(RUN_LIVE, state="failed", observed_at=_iso(time.time()))

    with runs._project_watch_claim(PROJECT, "1h") as (acquired, seat):
        assert acquired
        stream_path = Path(seat["stream_path"])
        _append_stream(
            stream_path,
            [
                _event(
                    RUN_REVIEW_A,
                    state="complete",
                    observed_at=_iso(three_days_ago),
                    event="baseline",
                ),
                _event(
                    RUN_REVIEW_B,
                    state="complete",
                    observed_at=_iso(three_days_ago),
                    event="baseline",
                ),
                _event(RUN_MOVED, state="failed", observed_at=_iso(last_evening)),
            ],
        )
        crew.list_live(project=PROJECT)
        _append_after_the_attach(monkeypatch, [live])
        _arm()

    for run_id in (RUN_REVIEW_A, RUN_REVIEW_B, RUN_MOVED):
        drawn = _naming(follow_lines, run_id)
        assert drawn, (
            f"the attach draws the row it replayed for {run_id}; got {follow_lines!r}"
        )
        for line in drawn:
            assert line.startswith(PREFIX), (
                f"a row replayed on attach begins with {PREFIX!r}; "
                f"{run_id} got {line!r}"
            )

    drawn_live = _naming(follow_lines, RUN_LIVE)
    assert drawn_live, (
        f"a transition written after the attach is drawn; got {follow_lines!r}"
    )
    for line in drawn_live:
        assert not line.startswith(PREFIX), (
            f"a transition written after the attach keeps the live format; got {line!r}"
        )


# ── Case 2: the help states the prefix ──────────────────────────────────────


def test_the_help_states_the_replay_prefix() -> None:
    result = CliRunner().invoke(cli.crew, ["follow", "--help"])
    assert result.exit_code == 0, result.output
    assert PREFIX.strip() in result.output, (
        f"crew follow --help states the {PREFIX.strip()!r} prefix so a line "
        f"filter can exclude it; got {result.output!r}"
    )


# ── Case 3: the pipe-resume snapshot is state, not replay ───────────────────


def test_the_pipe_resume_snapshot_is_not_prefixed(
    home: Path, follow_lines: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """§124's live-work snapshot is state, so the replay prefix never marks it."""
    _write_pointer(home, RUN_MOVED, phase="working")
    runs.write_delivered(PROJECT, SESSION, {RUN_MOVED: "working"})
    _write_pointer(home, RUN_MOVED, phase="queued")
    crew.list_live(project=PROJECT)

    def one_resume(*_args, **_kwargs):
        yield {
            "event": crew_follow_commands.FOLLOWER_RESUME_EVENT,
            "project": PROJECT,
            "session": SESSION,
            "reported": {},
        }

    monkeypatch.setattr(crew_follow_commands, "_follow_watch_lines", one_resume)
    result = CliRunner().invoke(
        cli.crew,
        ["follow", "--project", PROJECT, "--session", SESSION, "--no-color"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output

    snapshot = [line for line in follow_lines if RUN_MOVED in line and "  " in line]
    assert snapshot, (
        f"the pipe resume draws the live-work snapshot; got {follow_lines!r}"
    )
    for line in snapshot:
        assert not line.startswith(PREFIX), (
            f"the snapshot is state rather than news, so it carries no prefix; "
            f"got {line!r}"
        )
