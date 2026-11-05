"""A session without a pane learns of a limit reset on its next prompt.

A session whose follower is down has no stream to read, so a reset announced
while it was stopped has to reach it somewhere else: the coordinator
obligations hook draws one line beside the duty list, the obligations view
carries the same events, and the same reading twice says nothing the second
time. Every case runs under a temporary crew home with the hook driven as the
harness drives it — a subprocess with hook JSON on stdin — so nothing is read
from or written to the workstation's own store.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import budget_reset, lift_watch, runs
from reckon.crew.obligations import obligations as obligations_view
from reckon.hooks import coordinator_obligations
from tests.conftest import real_store_home, real_store_is_not_a_fixture_target
from tests.helpers.git import git as _git

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "reckon" / "hooks" / "coordinator_obligations.py"
PROJECT = "reset-hook-fixture"
SESSION = "reset-hook-session"
OTHER_SESSION = "some-other-session"
DUE_GROUP = "claude-orchestrators"
ANCIENT_GROUP = "an-older-window"
SCOPED_GROUP = "another-sessions-group"
RESET_LINE = "- [limit-reset]"


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary crew home, asserted never to be the real one."""
    real_home = real_store_home().resolve()
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(config / "mounts.json"))
    real_store_is_not_a_fixture_target(config)
    store = coordinator_obligations.budget_reset_store_path()
    assert store.is_relative_to(tmp_path), store
    assert not store.is_relative_to(real_home), store
    return config


@pytest.fixture()
def repository(tmp_path: Path, home: Path) -> Path:
    """A mounted repository, so the hook resolves this session's project."""
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed the reset hook fixture"),
    ):
        _git(root, *arguments)
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _event(
    *,
    event_id: str,
    group: str,
    moment: datetime,
    clock: str = "five_hour",
    session: str | None = None,
) -> dict[str, object]:
    event: dict[str, object] = {
        "kind": "used",
        "id": event_id,
        "detected_at": _iso(moment),
        "clock": clock,
        "reason": "the lead said the limits reset",
        "by": "lead-session",
        "group": group,
    }
    if session:
        event["session"] = session
    return event


def _write_events(events: list[dict[str, object]]) -> Path:
    """Write the store the announcement writes, one group per event."""
    groups: dict[str, dict[str, object]] = {}
    for event in events:
        groups[str(event["group"])] = {
            "available": False,
            "events": [event],
        }
    path = coordinator_obligations.budget_reset_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"groups": groups}), encoding="utf-8")
    return path


def _hook(mode: str, payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
    """Run the hook the way the harness runs it."""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPO_ROOT)
    # A pid no session can own, so the hook does not resolve a live one.
    environment["CLAUDE_PID"] = "2147483647"
    return subprocess.run(
        [sys.executable, str(HOOK), "--hook", mode],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )


def _payload(directory: Path) -> dict[str, object]:
    return {
        "session_id": SESSION,
        "cwd": str(directory),
        "hook_event_name": "UserPromptSubmit",
    }


@contextmanager
def _coordinating() -> Iterator[None]:
    """Hold the follower claim the hook reads a coordinating session from."""
    with runs.follower_claim(PROJECT, SESSION):
        yield


def _context(completed: subprocess.CompletedProcess[str]) -> str:
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == "", completed.stderr
    assert completed.stdout, "the prompt said nothing at all"
    return json.loads(completed.stdout)["hookSpecificOutput"]["additionalContext"]


def _reset_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith(RESET_LINE)]


def test_a_prompt_without_a_snapshot_names_the_due_reset_and_no_other(
    repository: Path, home: Path
) -> None:
    """The absent-snapshot prompt carries the 20-minute event and nothing else.

    No obligations snapshot is published, so the reading is the hook's own
    not-fresh line; the reset must ride beside it. The 90-minute event is one
    the horizon excludes and the other-session event is one the scope excludes,
    and both are shown to be visible to the reader before their absence is read
    as a filter: the scoped event is named for its own session, so the absence
    below is scoping and not a reader that found nothing.
    """
    now = datetime.now(tz=UTC)
    _write_events(
        [
            _event(
                event_id="reset-due",
                group=DUE_GROUP,
                moment=now - timedelta(minutes=20),
            ),
            _event(
                event_id="reset-ancient",
                group=ANCIENT_GROUP,
                moment=now - timedelta(minutes=90),
            ),
            _event(
                event_id="reset-scoped",
                group=SCOPED_GROUP,
                moment=now - timedelta(minutes=5),
                session=OTHER_SESSION,
            ),
        ]
    )
    assert (
        not coordinator_obligations.snapshot_module()
        .snapshot_path(PROJECT, SESSION)
        .exists()
    ), "this case is about the absent-session snapshot"

    with _coordinating():
        completed = _hook("prompt", _payload(repository))

    text = _context(completed)
    assert "no producer" in text, text
    lines = _reset_lines(text)
    assert len(lines) == 1, text
    assert DUE_GROUP in lines[0], lines
    assert ANCIENT_GROUP not in text, text
    assert SCOPED_GROUP not in text, text

    # Positive controls: the store really holds the excluded rows, the
    # 20-minute one is the only one that applies here, and the scoped row is
    # named for its own session, so its absence above is the scope rule and
    # not a read that found nothing.
    stored = sorted(
        row["id"]
        for row in json.loads(
            coordinator_obligations.budget_reset_store_path().read_text(
                encoding="utf-8"
            )
        )["groups"][ANCIENT_GROUP]["events"]
    )
    assert stored == ["reset-ancient"], stored
    here = [
        row["id"]
        for row in coordinator_obligations.limit_reset_events(SESSION, now=now)
    ]
    assert here == ["reset-due"], here
    scoped = [
        row["id"]
        for row in coordinator_obligations.limit_reset_events(OTHER_SESSION, now=now)
    ]
    assert scoped == ["reset-scoped", "reset-due"], scoped


def test_a_second_prompt_with_the_same_event_adds_no_context(
    repository: Path, home: Path
) -> None:
    """The same reading twice speaks once: nothing changed, so nothing is said."""
    now = datetime.now(tz=UTC)
    _write_events(
        [
            _event(
                event_id="reset-due",
                group=DUE_GROUP,
                moment=now - timedelta(minutes=20),
            )
        ]
    )

    with _coordinating():
        first = _hook("prompt", _payload(repository))
        second = _hook("prompt", _payload(repository))

    assert _reset_lines(_context(first)), first.stdout
    assert second.returncode == 0
    assert second.stdout == "", second.stdout


def test_the_obligations_view_lists_the_due_reset(repository: Path, home: Path) -> None:
    """A reader of the session's obligations sees what its prompt would see."""
    now = datetime.now(tz=UTC)
    _write_events(
        [
            _event(
                event_id="reset-due",
                group=DUE_GROUP,
                moment=now - timedelta(minutes=20),
            ),
            _event(
                event_id="reset-ancient",
                group=ANCIENT_GROUP,
                moment=now - timedelta(minutes=90),
            ),
            _event(
                event_id="reset-scoped",
                group=SCOPED_GROUP,
                moment=now - timedelta(minutes=5),
                session=OTHER_SESSION,
            ),
        ]
    )

    view = obligations_view(PROJECT, SESSION)

    assert [row["id"] for row in view["limit_resets"]] == ["reset-due"]
    assert view["limit_resets"][0]["group"] == DUE_GROUP


def test_the_hook_reader_matches_the_store_reader(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The prompt's own read is the store's read, horizon and scope included.

    The hook reads the file rather than importing the module that owns it, so
    this pins the two readings together on a store exercising each rule: a due
    global event, one past the horizon, and one scoped elsewhere. The horizon
    override is cleared so both arms read the default.
    """
    monkeypatch.delenv(coordinator_obligations.LIMIT_RESET_HORIZON_ENV, raising=False)
    now = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
    path = coordinator_obligations.budget_reset_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "groups": {
                    DUE_GROUP: {
                        "events": [
                            _event(
                                event_id="reset-due",
                                group=DUE_GROUP,
                                moment=now - timedelta(minutes=20),
                            )
                        ]
                    },
                    ANCIENT_GROUP: {
                        "events": [
                            _event(
                                event_id="reset-ancient",
                                group=ANCIENT_GROUP,
                                moment=now - timedelta(minutes=90),
                            )
                        ]
                    },
                    SCOPED_GROUP: {
                        "events": [
                            _event(
                                event_id="reset-scoped",
                                group=SCOPED_GROUP,
                                moment=now - timedelta(minutes=5),
                                session=OTHER_SESSION,
                            )
                        ]
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    assert (
        lift_watch.RESET_HORIZON.total_seconds()
        == coordinator_obligations.LIMIT_RESET_HORIZON_SECONDS
    )
    assert coordinator_obligations.limit_reset_events(
        SESSION, now=now
    ) == budget_reset.recent_resets(
        session=SESSION, now=now, horizon=lift_watch.RESET_HORIZON
    )
    assert coordinator_obligations.limit_reset_events(
        None, now=now
    ) == budget_reset.recent_resets(now=now, horizon=lift_watch.RESET_HORIZON)
