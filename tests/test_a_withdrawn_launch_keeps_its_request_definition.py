"""A launch withdrawn back to the queue keeps the request's own definition.

A queued request's pointer names its node body (brief, done-when, peer scopes,
admission findings) in a file inside the run directory, and the wired peer list
sits beside it. When an admission pass starts a launch and the lane then holds
it, the launch is withdrawn: its claim and the directory the attempt made are
removed and the request waits again. Removing the directory whole took the
request's body with it, so every later pass hydrated a node with no done-when
and no brief and refused the request for good.

The checks use a temporary crew home. A pointer is written through the store
that splits the body into the run directory; the attempt's claim and a launch
file are added; the launch is withdrawn. The node read back from the pointer
still carries the done-when and the brief, the peer list survives, and the
attempt's claim and launch file are gone.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew import dispatch_claims, runs

RUN_ID = "r-20261119T000000000000-a-withdrawn-launch-keeps-its-request"
DONE_WHEN = "tests/test_example.py passes; pytest reports 0 failed"
BRIEF = "/briefs/example-brief.md"
PEERS = [{"run_id": "r-peer", "node": "peer-node"}]


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


def _queued_pointer() -> Path:
    payload = {
        "run_id": RUN_ID,
        "phase": "queued",
        "project": "proj",
        "node": {
            "id": "a-withdrawn-launch-keeps-its-request",
            "plan": "plan-a",
            "section": "s1",
            "role": "implement",
            "write_paths": ["package/definition.py"],
            "done_when": DONE_WHEN,
            "brief": BRIEF,
            "peer_scopes": {"peer-node": ["other/path.py"]},
            "admission_findings": [],
        },
    }
    path = runs.pointer_path(RUN_ID)
    path.parent.mkdir(parents=True, exist_ok=True)
    runs._write_json(path, payload)
    return path


def test_the_store_puts_the_body_in_the_run_directory(crew_home: Path) -> None:
    _queued_pointer()
    stored = json.loads(runs.pointer_path(RUN_ID).read_text())
    assert "done_when" not in stored["node"]
    assert runs.node_body_path(RUN_ID).is_file()


def test_a_withdrawn_launch_keeps_the_node_body_and_peer_list(
    crew_home: Path,
) -> None:
    _queued_pointer()
    directory = runs.run_dir(RUN_ID)
    peers = directory / "peer-channel" / "peers.json"
    peers.parent.mkdir(parents=True, exist_ok=True)
    peers.write_text(json.dumps({"peers": PEERS}), encoding="utf-8")
    (directory / "claim.json").write_text("{}", encoding="utf-8")
    (directory / "review-brief.md").write_text("attempt copy\n", encoding="utf-8")

    dispatch_claims._withdraw_launch_claim(RUN_ID)

    node = runs.node_definition(runs.read_pointer(RUN_ID))
    assert node["done_when"] == DONE_WHEN
    assert node["brief"] == BRIEF
    assert node["peer_scopes"] == {"peer-node": ["other/path.py"]}
    assert json.loads(peers.read_text())["peers"] == PEERS
    assert not (directory / "claim.json").exists()
    assert not (directory / "review-brief.md").exists()
    assert runs.node_body_path(RUN_ID).stat().st_mode & 0o777 == 0o600


def test_a_withdrawn_launch_without_a_body_leaves_no_directory(
    crew_home: Path,
) -> None:
    directory = runs.run_dir(RUN_ID)
    directory.mkdir(parents=True)
    (directory / "claim.json").write_text("{}", encoding="utf-8")

    dispatch_claims._withdraw_launch_claim(RUN_ID)

    assert not directory.exists()
