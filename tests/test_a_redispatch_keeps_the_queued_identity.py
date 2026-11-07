"""A re-dispatch of a queued node merges into that request rather than replacing it.

Re-dispatching a node whose request is still waiting is how a coordinator
steers queued work: a new goal, a new measure, a changed brief. The request is
the same one, so the run id and everything about it that a launch or an
observation has already recorded must survive the steer.

Replacing the whole record did not. A held-and-released request carries the
``argv`` and ``base_sha`` a launch wrote and the observed ``session_id``, and a
re-dispatch wiped all three, so ``crew resume`` had no session to continue and
the landing pass had no argv to check. It also repointed the run's manifest,
which put the worker's manifest in one directory and its stream in another.
"""

from __future__ import annotations

import importlib
import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from reckon import cli
from reckon.crew import runs
from tests import test_dispatch_names_its_backend as backend_tests

dispatch_module = importlib.import_module("reckon.crew.dispatch")

LOCAL = "clive"
PROJECT = "proj"
SESSION = "session"
NODE = "held-node"
PLAN = "plan-a"
SECTION = "dispatch"

LAUNCHED_ARGV = ["claude", "--print", "do the work"]
LAUNCHED_BASE_SHA = "0123456789abcdef0123456789abcdef01234567"
OBSERVED_SESSION_ID = "11111111-2222-3333-4444-555555555555"
EXISTING_MANIFEST = "/runs/r-queued/manifest.md"
CREATED_AT = "2026-10-13T00:00:00+00:00"


@pytest.fixture()
def dispatch_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return backend_tests.dispatch_repo.__wrapped__(tmp_path, monkeypatch)


def _configure(repo: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A lane document with no slot, so a dispatch is held rather than launched."""
    lane = repo.parent / "lane.json"
    lane.write_text(
        json.dumps(
            {
                "headroom": 0,
                "admission": {
                    "worker_slots": 0,
                    "observed_seconds": 900,
                    "sessions": {SESSION: {"worker_slots": 0}},
                },
            }
        ),
        encoding="utf-8",
    )
    config = deepcopy(backend_tests.CONFIG)
    config["local_backend"] = LOCAL
    config["backends"][LOCAL]["lane_document"] = str(lane)
    monkeypatch.setattr(
        backend_tests.crew_dispatch_commands,
        "_resolved_flight",
        lambda *_a, **_k: config,
    )
    monkeypatch.setattr(
        backend_tests.crew_dispatch_commands,
        "_model_availability_refusal",
        lambda *_a, **_k: None,
    )
    return config


def _dispatch(repo: Path, *extra: str):
    arguments = backend_tests._arguments(repo, node=NODE, dry_run=False)
    arguments.extend(["--local", "--no-watch", *extra])
    return CliRunner().invoke(cli.main, arguments)


def _payload(result) -> dict[str, Any]:
    return json.loads(result.output.splitlines()[0])


def _queued_pointer(run_id: str = "r-queued") -> dict[str, Any]:
    """A queued request as the held path stores one, with nothing launched yet."""
    return {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "plan": PLAN,
        "section": SECTION,
        "backend": LOCAL,
        "phase": "queued",
        "queued_at": CREATED_AT,
        "created_at": CREATED_AT,
        "node": {
            "id": NODE,
            "plan": PLAN,
            "section": SECTION,
            "goal": "the queued goal",
            "role": "implement",
            "spec_level": "exact",
            "done_when": "the queued measure passes",
            "write_paths": ["result.json"],
            "manifest_path": EXISTING_MANIFEST,
            "brief": "",
            "brief_path": "",
            "brief_sha256": "",
        },
    }


def _seed(pointer: dict[str, Any]) -> None:
    runs._write_json(runs.pointer_path(str(pointer["run_id"])), pointer)


def _read(run_id: str) -> dict[str, Any]:
    return runs.read_pointer(run_id)


def _with_launched_fields(pointer: dict[str, Any]) -> dict[str, Any]:
    """What a launch and an observation leave on a request that is still waiting.

    A request that was admitted, attempted, put back and is waiting again has
    these on it: ``argv`` and ``base_sha`` from the launch attempt,
    ``session_id`` from the observation of the stream, and the manifest path
    its worker was told to write.
    """
    pointer["argv"] = list(LAUNCHED_ARGV)
    pointer["base_sha"] = LAUNCHED_BASE_SHA
    pointer["session_id"] = OBSERVED_SESSION_ID
    return pointer


# --- Case 1: the launched fields survive a steer -----------------------------


def test_a_steer_keeps_the_requests_launched_fields(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(dispatch_repo, monkeypatch)
    _seed(_with_launched_fields(_queued_pointer()))

    result = _dispatch(
        dispatch_repo, "--done-when", "pytest reports the steered measure passed"
    )
    payload = _payload(result)

    assert result.exit_code == 0, result.output
    assert payload["queued"] is True
    assert payload["run_id"] == "r-queued"
    pointer = _read("r-queued")
    assert runs.dispatch_field(pointer, "argv") == LAUNCHED_ARGV
    assert pointer["base_sha"] == LAUNCHED_BASE_SHA
    assert pointer["session_id"] == OBSERVED_SESSION_ID
    assert pointer["node"]["manifest_path"] == EXISTING_MANIFEST
    assert pointer["node"]["done_when"] == ("pytest reports the steered measure passed")


# --- Case 2: a changed brief is carried onto the same request ----------------


def test_a_steer_carries_the_changed_brief(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(dispatch_repo, monkeypatch)
    stale = _queued_pointer()
    stale["node"]["brief"] = "the stale brief"
    stale["node"]["brief_path"] = "/briefs/stale.md"
    stale["node"]["brief_sha256"] = "a" * 64
    _seed(stale)

    brief = dispatch_repo.parent / "brief.md"
    brief.write_text("the changed brief\n", encoding="utf-8")

    result = _dispatch(
        dispatch_repo,
        "--brief",
        str(brief),
        "--done-when",
        "pytest reports the steered brief passed",
    )
    payload = _payload(result)

    assert result.exit_code == 0, result.output
    assert payload["queued"] is True
    node = _read("r-queued")["node"]
    # A queued request names its brief's source; the run's own stored copy is
    # taken at launch, so the request carries no path to one yet.
    assert node["brief"] == str(brief)
    assert node["brief_path"] == ""
    assert node["brief_sha256"] not in ("", "a" * 64)
    assert len(node["brief_sha256"]) == 64


# --- Case 3: a plain re-dispatch keeps the request's identity ----------------


def test_a_steer_keeps_the_run_id_session_and_created_time(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(dispatch_repo, monkeypatch)
    _seed(_queued_pointer())

    result = _dispatch(dispatch_repo)
    payload = _payload(result)

    assert result.exit_code == 0, result.output
    assert payload["queued"] is True
    assert payload["run_id"] == "r-queued"
    pointer = _read("r-queued")
    assert pointer["run_id"] == "r-queued"
    assert pointer["session"] == SESSION
    assert pointer["created_at"] == CREATED_AT
    assert pointer["queued_at"] == CREATED_AT


# --- Case 4: a working run is answered and left alone ------------------------


def test_a_steer_of_a_working_run_answers_with_it_and_changes_nothing(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(dispatch_repo, monkeypatch)
    working = _queued_pointer("r-working")
    working["phase"] = "working"
    working["pid"] = os.getpid()
    _seed(working)
    before = _read("r-working")

    result = _dispatch(
        dispatch_repo, "--done-when", "pytest reports the steered working run passed"
    )
    payload = _payload(result)

    assert result.exit_code == 0, result.output
    assert payload["queued"] is False
    assert payload["run_id"] == "r-working"
    assert payload["phase"] == "working"
    assert payload["position"] is None
    assert _read("r-working") == before
