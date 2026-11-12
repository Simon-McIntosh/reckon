"""An undeclared project shares its source files and keeps the classes git
cannot reconcile exclusive.

The default is exercised on the real dispatch path in a synthesized repository
carrying no ``shared-write-paths.json``: two dispatches naming one source file
both launch, while a plan, a cumulative evidence record, generated state, a
figure topic directory, a dependency lock and a binary file are each refused.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from reckon import _backends, crew
from reckon.crew import runs
from reckon.hooks.install import worker_inbox_group
from tests.helpers.git import git
from tests.test_a_queued_run_is_admitted import (
    CONFIG,
    PROJECT,
    admit_repo,  # noqa: F401 - imported fixture
)

SOURCE = "pkg/source.py"
PLAN_FILE = "docs/plans/x.html"
EVIDENCE_RECORD = "docs/evidence/archive/x-landed.html"
GENERATED_STATE = f"docs/state/{PROJECT}/generated.json"
FIGURE = "docs/figures/topic/plot.svg"
LOCK = "uv.lock"
BINARY = "pkg/blob.dat"

EXCLUSIVE_CLASSES = [
    PLAN_FILE,
    EVIDENCE_RECORD,
    GENERATED_STATE,
    FIGURE,
    LOCK,
    BINARY,
]


@pytest.fixture(autouse=True)
def harness_handshake(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer the harness hook-trust handshake without a writable harness home.

    Composing a launch argv asks the installed harness which inbox hook
    definition it will load, which needs a writable harness home. The subject
    here is the scope claim, so the handshake is answered from the definition
    the argv already carries.
    """
    handshake = {
        "result": {
            "data": [
                {
                    "errors": [],
                    "hooks": [
                        {
                            "key": "/<session-flags>/config.toml:post_tool_use:0:0",
                            "currentHash": "sha256:" + "a" * 64,
                            "source": "sessionFlags",
                            "eventName": "postToolUse",
                            "handlerType": "command",
                            "command": worker_inbox_group()["hooks"][0]["command"],
                        }
                    ],
                }
            ]
        }
    }
    monkeypatch.setattr(_backends, "run_probe", lambda probe: handshake)


@pytest.fixture()
def undeclared(admit_repo: Path) -> Path:  # noqa: F811 - imported fixture
    """A repository with no write-sharing declaration and the fixture files."""
    (admit_repo / "pkg").mkdir(exist_ok=True)
    (admit_repo / SOURCE).write_text("def value():\n    return 1\n", encoding="utf-8")
    (admit_repo / BINARY).write_bytes(b"\x00\x01binary content\x00")
    (admit_repo / PLAN_FILE).write_text("<h2>Another plan</h2>\n", encoding="utf-8")
    (admit_repo / EVIDENCE_RECORD).parent.mkdir(parents=True, exist_ok=True)
    (admit_repo / EVIDENCE_RECORD).write_text("<h2>Landed</h2>\n", encoding="utf-8")
    (admit_repo / GENERATED_STATE).write_text('{"version": 1}\n', encoding="utf-8")
    (admit_repo / FIGURE).parent.mkdir(parents=True, exist_ok=True)
    (admit_repo / FIGURE).write_text("<svg></svg>\n", encoding="utf-8")
    (admit_repo / LOCK).write_text("version = 1\n", encoding="utf-8")
    git(
        admit_repo,
        "add",
        "--",
        "pkg",
        "docs",
        LOCK,
    )
    git(
        admit_repo,
        "commit",
        "-q",
        "-m",
        "test: seed an undeclared project",
        "-m",
        "Provide one file of every exclusive class plus a shared source file.",
    )
    assert not (
        admit_repo / "docs" / "state" / PROJECT / "shared-write-paths.json"
    ).exists()
    return admit_repo


def _node(node_id: str, path: str) -> crew.TaskNode:
    return crew.TaskNode(
        id=node_id,
        plan="plan-a",
        section="dispatch",
        goal="edit one file in a project with no sharing declaration",
        done_when="the shared claim check passes",
        write_paths=[path],
        spec_level="exact",
        time_budget="20m",
    )


def _dispatch(repo: Path, node_id: str, path: str) -> dict:
    return crew.dispatch(
        node=_node(node_id, path),
        project=PROJECT,
        repo=repo,
        config=CONFIG,
        session=f"session-{node_id}",
        check_budget=False,
        launcher=lambda *_args, **_kwargs: os.getpid(),
    )


def test_two_source_file_claims_both_launch(undeclared: Path) -> None:
    """With no declaration, two dispatches naming one source file both succeed."""
    owner = _dispatch(undeclared, "owner", SOURCE)
    joiner = _dispatch(undeclared, "joiner", SOURCE)

    assert owner["run_id"] != joiner["run_id"]
    claimants = {
        claim["node"]
        for claim in crew.scope_claims(PROJECT, undeclared)
        if claim["path"] in {SOURCE, str(undeclared / SOURCE)}
    }
    assert claimants == {"owner", "joiner"}


@pytest.mark.parametrize("path", EXCLUSIVE_CLASSES)
def test_each_exclusive_class_is_still_refused(undeclared: Path, path: str) -> None:
    """Every class a textual merge cannot reconcile stays exclusive."""
    owner = _dispatch(undeclared, "owner", path)

    with pytest.raises(crew.ScopeConflict) as refusal:
        _dispatch(undeclared, "joiner", path)

    assert refusal.value.run_id == owner["run_id"]


def test_an_unreadable_declaration_shares_nothing_and_names_itself(
    undeclared: Path,
) -> None:
    declaration = undeclared / "docs" / "state" / PROJECT / "shared-write-paths.json"
    declaration.write_text("{ this is not json", encoding="utf-8")
    owner = _dispatch(undeclared, "owner", SOURCE)

    with pytest.raises(crew.ScopeConflict) as refusal:
        _dispatch(undeclared, "joiner", SOURCE)

    assert refusal.value.run_id == owner["run_id"]
    assert "shared-write-paths.json" in str(refusal.value)
    assert "unreadable declaration" in str(refusal.value)


def test_an_empty_shared_roots_list_keeps_source_files_exclusive(
    undeclared: Path,
) -> None:
    declaration = undeclared / "docs" / "state" / PROJECT / "shared-write-paths.json"
    declaration.write_text(
        json.dumps({"shared_roots": [], "paths": []}), encoding="utf-8"
    )
    owner = _dispatch(undeclared, "owner", SOURCE)

    with pytest.raises(crew.ScopeConflict) as refusal:
        _dispatch(undeclared, "joiner", SOURCE)

    assert refusal.value.run_id == owner["run_id"]


def test_reckon_declares_every_default_exclusive_class() -> None:
    """The project's own declaration names every class in the one list."""
    declaration = (
        Path(__file__).parents[1] / "docs/state/reckon/shared-write-paths.json"
    )
    patterns = json.loads(declaration.read_text(encoding="utf-8"))["exclusive_patterns"]

    missing = [
        pattern for pattern in runs.DEFAULT_EXCLUSIVE_PATHS if pattern not in patterns
    ]
    assert missing == []
