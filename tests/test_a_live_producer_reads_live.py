"""The hook and the producer expire the same temporary lease together."""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon.crew import runs
from reckon.hooks import coordinator_obligations as hook


@pytest.mark.parametrize("override", [None, "17"])
@pytest.mark.parametrize(
    "age_fraction, live", [(0.75, True), (0.99, True), (1.0, False), (1.01, False)]
)
def test_hook_and_producer_share_the_full_lease_interval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    override: str | None,
    age_fraction: float,
    live: bool,
) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("RECKON_PRODUCER_LEASE_SECONDS", raising=False)
    if override is not None:
        monkeypatch.setenv("RECKON_PRODUCER_LEASE_SECONDS", override)
    project = "lease-reader-fixture"
    renewed = 1000.0
    interval = runs.producer_lease_seconds()
    now = renewed + age_fraction * interval
    monkeypatch.setattr(runs, "_utc_epoch_seconds", lambda: now)
    monkeypatch.setattr(runs, "watch_work_is_live", lambda _project: False)
    runs.update_watch_registration(project, lease_renewed_at=renewed)

    assert hook.producer_lease_path(project) == runs.watch_registration_path(project)
    assert hook.producer_lease_path(project).is_relative_to(tmp_path)
    assert hook.producer_lease_seconds() == interval
    assert hook.producer_lease_is_live(project, now=now) is live
    assert runs.watch_lease_lapsed(project) is not live
    assert runs.end_lapsed_watch_seat(project) is not live
