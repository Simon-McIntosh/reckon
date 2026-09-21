"""A stored session resolves only for the coordinator session that owns it.

The roster keys a member's session by agent configuration alone, so without an
owner any dispatch, from any coordinator session on any day, resumes the same
conversation. Both stored-session branches — the configuration-keyed entry in
the `sessions` map and the legacy entry reached through the bare `session_id`
plus `session_model` fields — must obey the ownership rule, because closing one
alone leaves the other resuming. A session the roster carries no owner for is
foreign by default: nothing proves it belongs to the session dispatching now.

The end-to-end cases drive `crew.dispatch` and read the launched argv back,
since a helper-level assertion is exactly how the resumed-conversation defect
stayed invisible.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from importlib import import_module
from pathlib import Path

import pytest

from reckon import crew, ledger
from reckon.calibration import agent_configuration_key
from reckon.crew import resumption

# `crew` re-exports a `dispatch` function under that name, so the module is
# reached by import rather than by attribute.
dispatch_module = import_module("reckon.crew.dispatch")

CONFIG = {
    "default_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "medium",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "25m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}
MEDIUM_AGENT = {
    "backend": "alpha",
    "launch": "cli",
    "model": "some-model",
    "effort": "medium",
    "sandbox": "worktree-full",
}
DISPATCHING_SESSION = "660d357e-1ede-4027-b678-d89d9391ff8f"
OTHER_SESSION = "1f2c7b40-9a11-4e5d-8f3a-2b6c1d0e9a77"
STORED_SESSION = "266f04b2-75c1-43f0-aa27-0d72a67b340f"
FIXTURE = Path(__file__).parent / "fixtures" / "backends" / "codex-turn.jsonl"


def _configuration_key(agent: Mapping[str, object]) -> str:
    return agent_configuration_key({"agent": agent})


def _member(**overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "id": "worker-a",
        "harness": "alpha",
        "role": "implement",
        "session_id": None,
        "session_model": None,
        "sessions": {},
    }
    entry.update(overrides)
    return entry


# ── the configuration-keyed branch ──────────────────────────────────────────


def test_a_stored_session_owned_by_another_session_is_not_returned() -> None:
    """The defect: a foreign entry resolved for whoever dispatched to it."""
    key = _configuration_key(MEDIUM_AGENT)
    member = _member(
        sessions={key: STORED_SESSION}, session_owners={key: OTHER_SESSION}
    )

    assert (
        dispatch_module._session_for_configuration(
            member,
            MEDIUM_AGENT,
            (),
            dispatching_session=DISPATCHING_SESSION,
        )
        is None
    )


def test_a_stored_session_owned_by_the_dispatching_session_is_returned() -> None:
    """Closing the lookup must not make every entry inert."""
    key = _configuration_key(MEDIUM_AGENT)
    member = _member(
        sessions={key: STORED_SESSION}, session_owners={key: DISPATCHING_SESSION}
    )

    assert (
        dispatch_module._session_for_configuration(
            member,
            MEDIUM_AGENT,
            (),
            dispatching_session=DISPATCHING_SESSION,
        )
        == STORED_SESSION
    )


def test_a_stored_session_with_no_recorded_owner_is_not_returned() -> None:
    """An entry set outside any dispatch proves nothing about its owner."""
    key = _configuration_key(MEDIUM_AGENT)
    member = _member(sessions={key: STORED_SESSION})

    assert (
        dispatch_module._session_for_configuration(
            member,
            MEDIUM_AGENT,
            (),
            dispatching_session=DISPATCHING_SESSION,
        )
        is None
    )


# ── the legacy branch, reached through session_id and session_model ──────────


def _legacy_member(owner: str | None) -> dict[str, object]:
    """A model-keyed entry whose capture evidence already agrees with it.

    The run history matches, so the only thing that can withhold the session is
    the recorded owner — which is what these cases are about.
    """
    entry = _member(session_id=STORED_SESSION, session_model="some-model")
    if owner is not None:
        entry["session_owners"] = {"some-model": owner}
    return entry


LEGACY_CAPTURE_RUN = {
    "member": "worker-a",
    "session_id": STORED_SESSION,
    "agent": MEDIUM_AGENT,
}


def test_a_legacy_session_owned_by_another_session_is_not_returned() -> None:
    """The same defect survives there, so the branches are asserted apart."""

    assert (
        dispatch_module._session_for_configuration(
            _legacy_member(OTHER_SESSION),
            MEDIUM_AGENT,
            (LEGACY_CAPTURE_RUN,),
            dispatching_session=DISPATCHING_SESSION,
        )
        is None
    )


def test_a_legacy_session_owned_by_the_dispatching_session_is_returned() -> None:
    assert (
        dispatch_module._session_for_configuration(
            _legacy_member(DISPATCHING_SESSION),
            MEDIUM_AGENT,
            (LEGACY_CAPTURE_RUN,),
            dispatching_session=DISPATCHING_SESSION,
        )
        == STORED_SESSION
    )


def test_a_legacy_session_with_no_recorded_owner_is_not_returned() -> None:
    assert (
        dispatch_module._session_for_configuration(
            _legacy_member(None),
            MEDIUM_AGENT,
            (LEGACY_CAPTURE_RUN,),
            dispatching_session=DISPATCHING_SESSION,
        )
        is None
    )


def test_the_withholding_names_the_session_and_its_recorded_owner() -> None:
    """A withheld session carries what a reader needs to see why."""
    key = _configuration_key(MEDIUM_AGENT)
    member = _member(
        sessions={key: STORED_SESSION}, session_owners={key: OTHER_SESSION}
    )

    resolution = dispatch_module._member_session_resolution(
        member, MEDIUM_AGENT, (), dispatching_session=DISPATCHING_SESSION
    )

    assert resolution["session_id"] is None
    withheld = resolution["withheld"]
    assert withheld["session_id"] == STORED_SESSION
    assert withheld["owner"] == OTHER_SESSION
    assert OTHER_SESSION in withheld["reason"]


def test_an_unowned_session_is_withheld_with_no_owner_named() -> None:
    key = _configuration_key(MEDIUM_AGENT)
    member = _member(sessions={key: STORED_SESSION})

    resolution = dispatch_module._member_session_resolution(
        member, MEDIUM_AGENT, (), dispatching_session=DISPATCHING_SESSION
    )

    assert resolution["session_id"] is None
    assert resolution["withheld"]["owner"] is None
    assert resolution["withheld"]["session_id"] == STORED_SESSION


# ── the capture path records the owner ──────────────────────────────────────


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "skills" / "reckon-ship" / "scripts").mkdir(parents=True)
    (root / "docs" / "plans").mkdir(parents=True)
    fleet_script = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-ship"
        / "scripts"
        / "worktree_fleet.py"
    )
    (root / "skills" / "reckon-ship" / "scripts" / "worktree_fleet.py").write_text(
        fleet_script.read_text()
    )
    (root / "docs" / "plans" / "plan-a.html").write_text(
        """<!doctype html>
<html><head>
<meta name="docs-project" content="proj">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="plan-a">
</head><body><h2 id="session-routing">Session routing</h2></body></html>
"""
    )
    (root / "seed.txt").write_text("seed\n")
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills", "docs/plans/plan-a.html"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    (config_home / "mounts.json").write_text(json.dumps({"proj": str(root / "docs")}))
    return root


def _capture(record: Mapping[str, object]) -> Mapping[str, object] | None:
    return dispatch_module._capture_member_session(record)


def test_the_capture_path_records_the_owning_coordinator_session(
    repo: Path,
) -> None:
    ledger.register_member("proj", "worker-a", harness="alpha", root=repo)

    captured = _capture(
        {
            "project": "proj",
            "repo": str(repo),
            "member": "worker-a",
            "session_id": STORED_SESSION,
            "agent": MEDIUM_AGENT,
            "session": DISPATCHING_SESSION,
        }
    )

    assert captured is not None and captured["captured"] is True
    key = _configuration_key(MEDIUM_AGENT)
    stored = ledger.member("proj", "worker-a", repo)
    assert stored is not None
    assert stored["sessions"] == {key: STORED_SESSION}
    assert stored["session_owners"] == {key: DISPATCHING_SESSION}


# ── a dispatch to a member holding only foreign entries ─────────────────────


def _node(home: Path, sequence: int) -> crew.TaskNode:
    return crew.TaskNode(
        id=f"session-scope-node-{sequence}",
        goal="verify a foreign stored session is withheld from a dispatch",
        plan="plan-a",
        section="session-routing",
        spec_level="guided",
        done_when=(
            "pytest tests/test_worker_session_is_session_scoped.py reports every "
            "case passing"
        ),
        write_paths=[f"reckon/session_scope_{sequence}.py"],
        time_budget="20m",
        manifest_path=str(home / f"session-scope-node-{sequence}.md"),
    )


def _dispatch(
    home: Path, repo: Path, sequence: int, *, config: Mapping[str, object] = CONFIG
) -> dict[str, object]:
    return crew.dispatch(
        node=_node(home, sequence),
        project="proj",
        repo=repo,
        config=config,
        session=DISPATCHING_SESSION,
        member="worker-a",
        launcher=lambda *args, **kwargs: 0,
    )


def _register_with_foreign_session(repo: Path) -> str:
    """Register a member whose only stored session belongs to another session."""
    key = _configuration_key(MEDIUM_AGENT)
    ledger.register_member("proj", "worker-a", harness="alpha", root=repo)
    data, version = ledger.load("proj", root=repo)
    for entry in data["members"]:
        if str(entry.get("id")) != "worker-a":
            continue
        entry["sessions"] = {key: STORED_SESSION}
        entry["session_owners"] = {key: OTHER_SESSION}
    ledger.write("proj", data, version, root=repo)
    return key


def test_a_dispatch_withholding_a_foreign_session_carries_no_resume(
    repo: Path,
) -> None:
    """Asserted against the launched argv, not against the stored record."""
    _register_with_foreign_session(repo)

    dispatched = _dispatch(repo.parent / "config", repo, 1)

    assert dispatched["session_id"] is None
    assert "resume" not in dispatched["argv"]


def test_a_dispatch_records_the_withheld_session_its_owner_and_a_reason(
    repo: Path,
) -> None:
    _register_with_foreign_session(repo)

    dispatched = _dispatch(repo.parent / "config", repo, 1)

    withheld = dispatched["session_withheld"]
    assert withheld["session_id"] == STORED_SESSION
    assert withheld["owner"] == OTHER_SESSION
    assert withheld["reason"]


def _register_with_owned_session(repo: Path) -> str:
    """Register a member whose stored session belongs to the dispatching session."""
    key = _configuration_key(MEDIUM_AGENT)
    ledger.register_member("proj", "worker-a", harness="alpha", root=repo)
    data, version = ledger.load("proj", root=repo)
    for entry in data["members"]:
        if str(entry.get("id")) != "worker-a":
            continue
        entry["sessions"] = {key: STORED_SESSION}
        entry["session_owners"] = {key: DISPATCHING_SESSION}
    ledger.write("proj", data, version, root=repo)
    return key


def test_a_dispatch_to_a_member_owning_its_session_still_resumes(
    repo: Path,
) -> None:
    """The ownership rule narrows the lookup; it does not disable reuse."""
    _register_with_owned_session(repo)

    dispatched = _dispatch(repo.parent / "config", repo, 1)

    assert dispatched["session_id"] == STORED_SESSION
    assert dispatched["argv"][dispatched["argv"].index("resume") + 1] == STORED_SESSION
    assert dispatched["session_withheld"] is None


# ── the run record still resolves a session ─────────────────────────────────


def test_a_run_whose_member_holds_no_matching_entry_still_resolves(
    repo: Path,
) -> None:
    """The roster is not the recovery path; the run's own record is."""
    _register_with_foreign_session(repo)
    pointer = {
        "run_id": "run-no-matching-entry",
        "project": "proj",
        "member": "worker-a",
        "session_id": STORED_SESSION,
        "launch": "cli",
        "log_path": str(repo / "absent-stream.jsonl"),
    }

    resolved = resumption.resolve_session("run-no-matching-entry", record=pointer)

    assert resolved["resolved"] is True
    assert resolved["session_id"] == STORED_SESSION
    assert resolved["source"] == "pointer"
