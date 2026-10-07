"""One context figure reaches Jev from the production dispatch pick.

The picker threads a dispatch authority through the request estimate and each
candidate's context-fit verdict so a dispatcher-granted landing fragment is
exempt in one figure and charged in neither. That threading is inert unless the
production caller supplies the authority: this test drives the real
``dispatch_picker_selection`` a node with granted paths takes and asserts the
request's estimate and every offered candidate's block carry the same figure.
The picker-internal regression asserts the two measurements agree when both are
handed the authority; this test asserts the production call hands it over.
"""

from __future__ import annotations

import importlib

import pytest

from reckon.crew import picker
from reckon.crew.dispatch import _grant_landing_write_paths
from reckon.crew.node import TaskNode

dispatch = importlib.import_module("reckon.crew.dispatch")


def build_node(**overrides):
    values = {
        "id": "n",
        "goal": "g",
        "plan": "p",
        "role": "implement",
        "spec_level": "configuration",
        "done_when": "PASS",
        "write_paths": ["src/target.py"],
        "negative_control": "none: nothing to refuse here",
    }
    values.update(overrides)
    return TaskNode(**values)


def build_config(window=200000):
    return {
        "default_backend": "remote",
        "local_backend": "local",
        "roles": {"implement": {}},
        "backends": {
            "local": {
                "launch": "cli",
                "command": "claude",
                "model": "local-model",
                "usable_input_window": window,
            },
            "remote": {
                "launch": "cli",
                "command": "claude",
                "model": "remote-model",
                "usable_input_window": window,
            },
        },
    }


def _granted_authority(repo):
    return {
        "plan": {
            "project": "proj",
            "repository": str(repo),
            "docs": str(repo / "docs"),
        }
    }


@pytest.fixture
def repo_with_granted_fragment(tmp_path):
    """A repository holding the node's declared write path and a granted fragment."""

    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "docs").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "target.py").write_text("x" * 400)
    return repo


@pytest.fixture
def candidate_scan(monkeypatch):
    """Stub the pick's non-estimate inputs, leaving the estimate real.

    Only the pieces that would read the live machine are replaced: the
    availability probe, the lane gate and the competence verdict. The context
    estimate and the context-fit verdict run for real, because the whole point
    of the test is the figure they compute.
    """

    monkeypatch.setattr(
        picker.snapshot.resumption,
        "probe_lane_availability",
        lambda project, name, backend, **k: {"status": "served"},
    )
    monkeypatch.setattr(
        picker.snapshot, "_dispatch_lane_gate", lambda backend: {"state": "open"}
    )
    monkeypatch.setattr(
        picker.snapshot, "_lane", lambda *a, **k: (3, {"waiting": 0}, {"held": False})
    )
    monkeypatch.setattr(
        picker.snapshot.routing,
        "_competence_verdict",
        lambda **k: {"allowed": True, "reason": "", "context": None},
    )


def test_request_estimate_and_candidate_blocks_are_one_figure(
    repo_with_granted_fragment, candidate_scan, monkeypatch
):
    repo = repo_with_granted_fragment
    node = build_node()
    authority = _granted_authority(repo)
    # The dispatcher grants the node its landing fragment, which the census must
    # see to exempt: the fragment is large enough that charging it would move
    # the figure a reader compares.
    _grant_landing_write_paths(node, project="proj", authority=authority, warnings=[])
    assert len(node.write_paths) > 1
    for granted in node.write_paths:
        resolved = repo / granted
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text("g" * 4000)

    seen: dict[str, object] = {}

    def stub_ask(state, questions, *, env_path):
        seen["request_estimate"] = state["node"]["estimated_context"]
        seen["candidate_blocks"] = {
            name: entry["context"]["estimated_tokens"]
            for name, entry in state["candidates"].items()
        }
        offered = list(state["candidates"])
        split = round(1.0 / (len(offered) + 1), 4)
        return {
            "answers": {
                "route": {
                    "choice": offered[0],
                    "confidence": 0.9,
                    "probabilities": {
                        **dict.fromkeys(offered, split),
                        "hold": round(1.0 - split * len(offered), 4),
                    },
                }
            }
        }

    monkeypatch.setitem(picker.pick.__kwdefaults__, "caller", stub_ask)

    dispatch.dispatch_picker_selection(
        node=node,
        config=build_config(),
        project="proj",
        repo=repo,
        authority=authority,
    )

    assert seen["candidate_blocks"], (
        "the scan offered no candidate with a context block"
    )
    request_estimate = seen["request_estimate"]
    assert request_estimate and request_estimate > 0
    for name, block_estimate in seen["candidate_blocks"].items():
        assert block_estimate == request_estimate, name

    # Equality alone is a weak check: measured without the authority both the
    # request figure and every candidate block charge the granted fragment and
    # so still agree, only larger. Pin the exemption, so a production call that
    # stops passing the authority moves the figure Jev weighs and fails here.
    with_authority = picker.snapshot.estimated_context_tokens(
        node, repo, authority=authority
    )
    without_authority = picker.snapshot.estimated_context_tokens(node, repo)
    assert with_authority < without_authority, "the granted fragment was not exempted"
    assert request_estimate == with_authority
