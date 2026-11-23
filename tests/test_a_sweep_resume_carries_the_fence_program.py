"""A resume started with no settings is fenced from its project's flight.

The station's and producers' sweeps resume a run without passing flight
settings. The fence a resume composes reads those settings for the fence
program a job carries to a node with no bubblewrap of its own. A fence built
from none carried no program, so on 23 November a nova worker resumed by the
sweep submitted an H200 job that refused before its task started: "looked at
/usr/bin/bwrap (absent)". A resume handed no settings now resolves the run's
project flight, exactly as a coordinator's resume does.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from reckon.crew import dispatch_sessions as dispatch_sessions_module
from tests.test_a_resumed_launch_keeps_its_lane import RUN, run  # noqa: F401

CARRIED = {
    "path": "/shared/fence/bwrap",
    "checksum": "sha256:" + "0" * 64,
    "source": "a test copy",
    "version": "0.4.1",
}


class _CapturedError(Exception):
    """Raised by the stand-in launch builder once it holds the fence settings."""


def _capture_launch(monkeypatch: pytest.MonkeyPatch) -> dict:
    seen: dict = {}

    def launch_plan(**kwargs):
        seen["fence_config"] = kwargs.get("fence_config")
        raise _CapturedError

    monkeypatch.setattr(dispatch_sessions_module._backends, "launch_plan", launch_plan)
    return seen


def test_a_resume_with_no_settings_is_fenced_from_its_projects_flight(
    run,  # noqa: F811 - the imported fixture
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _worktree = run
    resolved = {**config, "fence_program": CARRIED}
    asked: list[object] = []

    def resolve(project=None, **_kwargs):
        asked.append(project)
        return SimpleNamespace(config=resolved)

    monkeypatch.setattr(dispatch_sessions_module.flight, "resolve", resolve)
    seen = _capture_launch(monkeypatch)

    with pytest.raises(_CapturedError):
        dispatch_sessions_module.resume_plan(RUN, "carry on", config=None)

    assert asked, "a resume with no settings resolved no flight"
    assert seen["fence_config"]["fence_program"] == CARRIED


def test_a_resume_given_settings_uses_them_unchanged(
    run,  # noqa: F811 - the imported fixture
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _worktree = run

    def resolve(project=None, **_kwargs):
        raise AssertionError("settings were passed, so none may be resolved")

    monkeypatch.setattr(dispatch_sessions_module.flight, "resolve", resolve)
    seen = _capture_launch(monkeypatch)

    with pytest.raises(_CapturedError):
        dispatch_sessions_module.resume_plan(RUN, "carry on", config=config)

    assert seen["fence_config"] is config
