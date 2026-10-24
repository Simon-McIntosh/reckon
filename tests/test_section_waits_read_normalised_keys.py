"""A queued section reads its declared waits through one section identity."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew import queue_admission
from reckon.crew.node import TaskNode
from tests import test_a_queued_request_waits_on_its_dependencies as dependency_tests
from tests.test_a_queued_request_waits_on_its_dependencies import (
    CONFIG,
    PLAN,
    PROJECT,
    SESSION,
)


@pytest.fixture()
def waiting_plan_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return dependency_tests.chain_repo.__wrapped__(tmp_path, monkeypatch)


@pytest.mark.parametrize(
    ("section", "authored_key"),
    [("s2", "§2"), ("§2", "s2"), ("section 2", "s2")],
)
def test_queued_section_spelling_keeps_its_upstream_wait(
    waiting_plan_repo: Path, section: str, authored_key: str
) -> None:
    if authored_key != "s2":
        dependency_tests._declare_wait_refs(
            waiting_plan_repo, section=authored_key, refs=[f"{PLAN}#s1"]
        )
    node = TaskNode(
        id="waiting-section",
        goal="build the waiting section",
        plan=PLAN,
        section=section,
        done_when="the upstream section has landed",
        write_paths=["result.txt"],
        time_budget="20m",
        spec_level="exact",
    )

    stored = queue_admission.queue_requests(
        [node],
        project=PROJECT,
        session=SESSION,
        repo_root=waiting_plan_repo,
        config=CONFIG,
        run_ids={node.id: "r-waiting-section"},
    )

    request = stored["r-waiting-section"]
    assert request["waits"] == [f"{PLAN}#s1"]
    assert request["phase"] == queue_admission.AWAITING_UPSTREAM_PHASE
    if authored_key == "s2":
        assert queue_admission.unmet_waits(
            node, repo_root=waiting_plan_repo, project=PROJECT
        ) == [f"{PLAN}#s1"]


@pytest.mark.parametrize("authored_key", ["s2", "§2", "section 2"])
def test_wait_mapping_keys_use_the_same_section_identity(authored_key: str) -> None:
    declaration = json.dumps({authored_key: [f"{PLAN}#s1"]}).replace('"', "&quot;")
    plan_html = f'<meta name="plan-section-depends-on" content="{declaration}">'

    assert queue_admission.section_wait_refs(plan_html, "s2") == [f"{PLAN}#s1"]


def test_a_failed_request_with_an_alternate_section_spelling_blocks_its_ref() -> None:
    dead = queue_admission._dead_sections(
        [{"node": {"plan": PLAN, "section": "§1"}, "phase": "failed"}]
    )

    assert dead == {(PLAN, "§1")}
    assert (
        queue_admission._ref_state(
            f"{PLAN}#s1",
            waiting_section="section 2",
            rows={},
            inventory={},
            dead_sections=dead,
        )
        == queue_admission.ABANDONED
    )
