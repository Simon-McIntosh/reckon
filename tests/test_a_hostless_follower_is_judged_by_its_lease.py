"""A legacy follower registration takes its host from a fresh lease."""

import json
import socket
import time
from pathlib import Path

import pytest

from reckon.crew import follower_registration as followers


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    real_home = Path.home() / ".config" / "reckon"
    before = sorted(real_home.iterdir()) if real_home.exists() else []
    monkeypatch.setenv("RECKON_HOME", str(tmp_path))
    yield tmp_path
    after = sorted(real_home.iterdir()) if real_home.exists() else []
    assert after == before


@pytest.mark.parametrize(
    ("host", "age", "live"),
    [
        ("foreign", 0, True),
        ("local", 0, False),
        ("foreign", 601, False),
        (None, 0, False),
    ],
)
def test_hostless_registration_uses_only_a_fresh_named_foreign_lease(
    home: Path, monkeypatch: pytest.MonkeyPatch, host: str | None, age: int, live: bool
) -> None:
    monkeypatch.setenv("RECKON_PRODUCER_LEASE_SECONDS", "600")
    project, session = "hostless-fixture", "session"
    path = followers.follower_lock_path(project, session)
    path.parent.mkdir(parents=True)
    with path.open("a+b") as handle:
        followers._write_watch_record(
            handle,
            {
                "project": project,
                "session": session,
                "pid": 2_147_483_647,
                "delivery": "stream",
                "started_at": "2026-10-15T00:00:00Z",
            },
        )
    assert path.is_relative_to(home)
    lease = {"renewed_at": time.time() - age}
    if host is not None:
        lease["host"] = socket.gethostname() if host == "local" else "another-host"
    path.with_suffix(".lease").write_text(json.dumps(lease), encoding="utf-8")
    state = followers.follower_state(project, session)
    assert state["registered"] is live, state
    assert state["live"] is live, state
    assert state["released"] is not live, state


def test_renewal_records_the_host(home: Path) -> None:
    followers.renew_follower_lease("hostless-fixture", "session")
    lease = followers.follower_lease_path("hostless-fixture", "session")
    assert lease.is_relative_to(home)
    assert json.loads(lease.read_text(encoding="utf-8"))["host"] == socket.gethostname()
