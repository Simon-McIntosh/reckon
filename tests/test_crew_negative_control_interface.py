"""The negative control is expressible from the surfaces a caller actually uses.

A node whose write paths include a test file is refused at dispatch unless its
record declares the mutation that check must fail against, and a passing gate on
such a node is refused at promotion unless its manifest names a red log whose
content names that mutation. Both refusals are only satisfiable if the surfaces a
coordinator and a worker reach can express the declaration: the dispatch command
must be able to set it on the node record, and the manifest template a worker is
handed must name the log field it has to fill. A refusal whose own discharge is
unreachable from the command a caller runs is a guard that reports a requirement
it cannot be met from, which is the failure this section exists to prevent.

Each property is pinned by its own case and reddened by its own mutation of the
change it measures, and the red log each mutation produced is kept beside the
green one under the node's report directory: ``dispatch_option_removed.red.log``
for the dispatch interface and ``manifest_field_removed.red.log`` for the
emitted template.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon.crew.node import TaskNode
from reckon.crew.prompts import compose_prompt
from tests import test_dispatch_names_its_backend as dispatch_tests

pytest_plugins = ("tests.test_dispatch_names_its_backend",)

# The declared write path that makes the node a check writer: the dispatch
# refusal triggers on a test-naming path, so this is the input that separates an
# interface able to discharge the refusal from one that cannot.
TEST_PATH = "tests/test_guard.py"


# ── Property one: the dispatch command sets the declaration on the node ─────


def _invoke(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    node: str,
    extra: list[str] | None = None,
):
    """Run the dispatch command for a test-writing node and read its payload."""
    config = deepcopy(dispatch_tests.CONFIG)
    monkeypatch.setattr(cli_module, "_resolved_flight", lambda *_a, **_k: config)
    monkeypatch.setattr(
        cli_module, "_model_availability_refusal", lambda *_a, **_k: None
    )
    result = CliRunner().invoke(
        cli_module.main,
        [
            *dispatch_tests._arguments(repo, node=node),
            "--write-path",
            TEST_PATH,
            # An unmetered lane, so the only property this node can fail is
            # the one under test rather than the metered-lane declaration.
            "--backend",
            "clive",
            *(extra or []),
        ],
    )
    return dispatch_tests._payload(result), result


def _diagnosis(payload: dict, result) -> str:
    """The validation findings and any traceback, for a failing assertion."""
    return json.dumps(payload.get("validation", payload), indent=2) + (
        f"\n--- command output ---\n{result.output}"
        + (f"\n--- exception ---\n{result.exception!r}" if result.exception else "")
    )


def _control_finding(payload: dict) -> dict[str, str]:
    """Return the node's negative-control finding, failing loudly if absent."""
    findings = payload["validation"]["findings"]
    for finding in findings:
        if finding["property"] == "negative_control":
            return finding
    raise AssertionError(f"no negative_control finding among {findings}")


def test_the_command_refuses_a_test_path_with_no_declaration(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guarded thing happens: a test path is dispatched with nothing declared."""
    payload, result = _invoke(dispatch_repo, monkeypatch, node="nc-interface-refused")

    assert result.exit_code == 2
    assert payload["validation"]["ok"] is False
    finding = _control_finding(payload)
    # The declaration is the only property this node fails, so the refusal is
    # the one under test rather than a second finding arriving alongside it.
    assert payload["validation"]["findings"] == [finding]
    assert "negative_control" in finding["detail"]
    assert TEST_PATH in finding["detail"]


def test_the_command_carries_a_declaration_onto_the_node_record(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same node resolves once the command is given the mutation."""
    mutation = "removing the test's input guard turns its only case red"
    payload, result = _invoke(
        dispatch_repo,
        monkeypatch,
        node="nc-interface-declared",
        extra=["--negative-control", mutation],
    )

    assert result.exit_code == 0, _diagnosis(payload, result)
    assert payload["validation"]["ok"] is True, _diagnosis(payload, result)
    assert payload["node"]["negative_control"] == mutation


def test_the_command_admits_the_none_escape_with_its_reason(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A check no mutation can redden declares none, and the escape reaches
    the command rather than only a library caller."""
    declaration = "none: the assertion is an identity that must hold"
    payload, result = _invoke(
        dispatch_repo,
        monkeypatch,
        node="nc-interface-none",
        extra=["--negative-control", declaration],
    )

    assert result.exit_code == 0, _diagnosis(payload, result)
    assert payload["validation"]["ok"] is True, _diagnosis(payload, result)
    assert payload["node"]["negative_control"] == declaration


# ── Property two: the emitted contract names the log field ─────────────────


def _prompt() -> str:
    node = TaskNode(
        id="nc-interface-template",
        goal="the emitted contract names the field a worker must fill",
        plan="plan-a",
        section="guard",
        role="implement",
        done_when="the emitted manifest template names negative_control_log",
        write_paths=[TEST_PATH],
        time_budget="20m",
        manifest_path="/state/runs/nc-interface-template/manifest.md",
        negative_control="removing the guard turns its case red",
    )
    return compose_prompt(
        node=node,
        project="proj",
        worktree="/repo/worktrees/nc-interface-template",
        working_directory="/repo/worktrees/nc-interface-template",
        manifest_path="/state/runs/nc-interface-template/manifest.md",
        time_budget="20m",
        needs_help_after_failures=2,
    )


def test_the_emitted_manifest_template_names_the_negative_control_log() -> None:
    """The promotion refusal is legible from the contract a worker is handed."""
    prompt = _prompt()

    assert "negative_control_log:" in prompt
    gloss = next(
        line
        for line in prompt.splitlines()
        if line.strip().startswith("negative_control_log:")
    ).strip()
    # The gloss must make the discrimination promotion applies: this log is the
    # one the declared mutation produced, and a log that failed for anything
    # else is refused. Pinning the wording of that discrimination rather than a
    # nickname for the artifact is what keeps it legible from the contract.
    assert "declared mutation" in gloss
    assert "failed for any other reason" in gloss
    assert "refusal" in gloss or "refused" in gloss


def test_the_template_places_the_log_field_beside_the_other_test_fields() -> None:
    """It sits with tests and test_logs, where a worker fills them in."""
    lines = [line.strip() for line in _prompt().splitlines()]
    order = [line.split(":", 1)[0] for line in lines if ":" in line]
    assert (
        order.index("tests")
        < order.index("test_logs")
        < order.index("negative_control_log")
    )
