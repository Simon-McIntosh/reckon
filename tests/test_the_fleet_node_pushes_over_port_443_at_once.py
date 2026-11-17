"""On the fleet node every remote git call takes the port-443 route first.

The fleet node reaches the remote only on port 443, and its port 22 fails by
timing out. A push that tried port 22 before falling back paid that timeout on
every landing, so there the 443 route is chosen before the first attempt. A
host that is not the fleet node keeps Git's own route for a push.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from reckon.crew import merge


@pytest.fixture
def calls(monkeypatch):
    """Record the ssh route each remote git call was given."""
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.delenv("GIT_SSH", raising=False)
    seen: list[tuple[str, str]] = []

    def run(command, **kwargs):
        verb = command[3]
        if verb == "config":
            return subprocess.CompletedProcess(command, 1, "", "")
        env = kwargs.get("env") or {}
        seen.append((verb, env.get("GIT_SSH_COMMAND", "")))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(merge.subprocess, "run", run)
    return seen


@pytest.mark.parametrize("verb", ["push", "ls-remote", "fetch"])
def test_the_fleet_node_takes_the_443_route_first(calls, monkeypatch, verb) -> None:
    monkeypatch.setattr(merge, "_on_fleet_node", lambda: True)

    merge._run(Path("/repo"), verb, "origin")

    assert len(calls) == 1
    assert calls[0][0] == verb
    assert calls[0][1].startswith(merge.FALLBACK_SSH_COMMAND)
    assert "ConnectTimeout" in calls[0][1]


def test_another_host_keeps_gits_own_route_for_a_push(calls, monkeypatch) -> None:
    monkeypatch.setattr(merge, "_on_fleet_node", lambda: False)

    merge._run(Path("/repo"), "push", "origin")
    merge._run(Path("/repo"), "ls-remote", "origin")

    assert calls[0] == ("push", "")
    assert calls[1][0] == "ls-remote"
    assert "ssh.github.com" not in calls[1][1]


def test_a_stated_route_is_kept_on_the_fleet_node(calls, monkeypatch) -> None:
    monkeypatch.setattr(merge, "_on_fleet_node", lambda: True)
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i /keys/deploy")

    merge._run(Path("/repo"), "push", "origin")

    assert calls[0][1].startswith("ssh -i /keys/deploy")
    assert "ssh.github.com" not in calls[0][1]
