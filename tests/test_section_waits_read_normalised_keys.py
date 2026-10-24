"""A queued section reads its declared waits through one section identity."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew import queue_admission, runs
from reckon.crew.dispatch_admission import LaneHeld
from reckon.crew.node import TaskNode
from tests import test_a_queued_request_waits_on_its_dependencies as dependency_tests
from tests import test_dispatch_names_its_backend as backend_tests
from tests.test_a_queued_request_waits_on_its_dependencies import (
    CONFIG,
    PLAN,
    PROJECT,
    SESSION,
)


@pytest.fixture()
def waiting_plan_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return dependency_tests.chain_repo.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture()
def dispatch_plan_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return backend_tests.dispatch_repo.__wrapped__(tmp_path, monkeypatch)


@pytest.mark.parametrize("section", ["s2", "§2"])
def test_dispatch_stores_the_section_wait_before_launch(
    dispatch_plan_repo: Path, section: str
) -> None:
    dependency_tests._declare_section_wait(
        dispatch_plan_repo,
        section="s2",
        refs=[f"{PLAN}#upstream"],
        sections=("s2", "upstream"),
    )
    node = TaskNode(
        id="waiting-section",
        goal="build the waiting section",
        plan=PLAN,
        section=section,
        done_when="pytest -q tests/test_section_waits_read_normalised_keys.py passes",
        write_paths=["result.txt"],
        time_budget="20m",
        spec_level="exact",
    )
    with pytest.raises(LaneHeld) as caught:
        backend_tests.dispatch_module.dispatch(
            node=node,
            project=PROJECT,
            repo=dispatch_plan_repo,
            config={
                **backend_tests.CONFIG,
                "default_backend": "clive",
                "local_backend": "clive",
            },
            session=SESSION,
            check_budget=False,
            local=True,
            backend_override="clive",
        )

    pointers = runs.list_live(project=PROJECT)
    assert len(pointers) == 1
    pointer = pointers[0]
    assert dict(pointer.get("node") or {}).get("section") == "§2"
    assert pointer["waits"] == [f"{PLAN}#upstream"]
    assert pointer["phase"] == queue_admission.AWAITING_UPSTREAM_PHASE
    assert caught.value.queued["phase"] == queue_admission.AWAITING_UPSTREAM_PHASE
    assert not pointer.get("pid")


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
