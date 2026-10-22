"""The domain-check waiver reaches the promotion guard from every surface.

A duty prints the promotion command a coordinator may call, and the crew MCP
tool carries that same command as an action. A printed command stating a
domain-check waiver has to survive both hops: rendered through ``action_form``
into the tool's action form, and carried from the tool's ``complete`` action
into the promotion guard, where the repository's declared invariant check
refuses the promotion until the waiver is stated.

The fixture declares one invariant whose check fails over the paths the node
writes, so the guard is live: the same promotion is refused without a reason
and lands with one, which is what shows the stated reason is what the guard
applied rather than the guard being inert.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import reckon.mcp as mcp_module
from reckon import crew
from reckon.crew import obligations as obligations_module
from reckon.crew import promotion as promotion_module
from reckon.crew.routing import run_directory_of
from reckon.crew.runs import _write_json, pointer_path
from tests.helpers.git import git

PROJECT = "sample"

#: The declared invariant the guard replays: it matches the node's own write
#: paths, and its check command fails, so a promotion of this node is refused
#: until the waiver states a reason.
DOMAIN_FLIGHT = """\
domain:
  invariants:
    - id: convergence
      rule: the section count must match the converged count
      applies_to: ["models/**"]
      check: "exit 3"
"""

WAIVER_REASON = "the record predates the convergence fix"

#: The command a duty prints when its promotion needs the waiver stated, in the
#: CLI's own spelling.
DUTY_COMMAND = (
    "reckon crew complete --run run-waiver --gate passed "
    f'--waive-domain-check "{WAIVER_REASON}"'
)


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthetic config home and repository, isolated from the real fleet."""
    config_home = tmp_path / "config"
    (config_home / "crew" / "live").mkdir(parents=True)
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    # The run identity a crew worker exports is left out of this test's own
    # process: the git shim refuses a mutating verb whose run's pointer it
    # cannot read, and a throwaway fixture repository is exactly the state that
    # guard is not for. Promotion stages the ledger row with git.
    monkeypatch.delenv("RECKON_RUN_ID", raising=False)
    root = tmp_path / "repo"
    state = root / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "flight.yaml").write_text(DOMAIN_FLIGHT, encoding="utf-8")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "chore: seed"),
    ):
        git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _pointer(
    repository: Path,
    run_id: str,
    manifest: Path,
) -> None:
    """A promoted run whose write paths match the declared invariant."""
    base = git(repository, "rev-parse", "HEAD")
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: domain-waiver\nstatus: complete\ntests: not applicable\n",
        encoding="utf-8",
    )
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "base_sha": base,
            "launch": "in-harness",
            "role": "implement",
            "member": "worker-domain-waiver",
            "backend": "native",
            "created_at": "2026-10-22T05:00:00Z",
            "manifest_path": str(manifest),
            "node": {
                "id": "domain-waiver",
                "plan": "fixture",
                "section": "guard",
                "time_budget": "40m",
                "write_paths": ["models/tape.py"],
            },
        },
    )


def _capture_waiver(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Record the waiver reason the promotion guard is called with.

    The real guard still runs, so the fixture's failing check is replayed and
    the promotion is refused or waived on the evidence rather than on a stub's
    answer.
    """
    seen: dict = {}
    real = promotion_module._require_domain_invariant_checks

    def _spy(*args, **kwargs):
        seen["waiver_reason"] = kwargs.get("waiver_reason")
        return real(*args, **kwargs)

    monkeypatch.setattr(
        promotion_module, "_require_domain_invariant_checks", _spy, raising=True
    )
    return seen


def test_a_duty_command_stating_the_waiver_renders_as_a_crew_tool_action() -> None:
    """Every option the printed command carries is carried by the action form.

    An option the form cannot carry leaves the whole command unrenderable, so a
    duty naming this waiver would offer no crew-tool call at all.
    """
    assert obligations_module.action_form(DUTY_COMMAND) == (
        'crew(action="complete", run_id="run-waiver", gate="passed", '
        'waive_domain_check="the record predates the convergence fix")'
    )


def test_a_promotion_through_the_tool_records_the_stated_waiver(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reason the duty states is the reason the promotion guard applies.

    Driven from the duty surface: the command is rendered to the tool's action
    form, and the call that form offers is the one made, so a reader of the
    duty reaches the guard with the waiver rather than composing the shell
    command by hand.
    """
    rendered = obligations_module.action_form(DUTY_COMMAND)
    assert rendered is not None, (
        "the duty command stating the domain waiver rendered no action form"
    )
    _pointer(repository, "run-waiver", tmp_path / "manifests" / "run-waiver.md")
    seen = _capture_waiver(monkeypatch)

    answer = mcp_module._crew(
        action="complete",
        run_id="run-waiver",
        gate="not-run",
        outcome="promotion attempted under a declared domain invariant",
        waive_domain_check=WAIVER_REASON,
    )

    assert seen["waiver_reason"] == WAIVER_REASON, (
        "the stated waiver did not reach the promotion guard"
    )
    assert answer.get("ok") is True, answer
    # The check really ran over the node's paths and its output was kept, so
    # the waiver stood in for a failing check rather than for an inert guard.
    log = run_directory_of({"run_id": "run-waiver"}) / "domain-check-convergence.log"
    assert log.is_file(), "the domain check left no log to waive"


def test_the_cli_records_the_same_waiver_as_the_tool(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both surfaces hand the guard the same reason for the same call.

    The tool action and the CLI verb run one promotion function, so the reason
    the tool carries is compared against the reason the CLI carries rather than
    asserted about either surface alone.
    """
    _pointer(repository, "run-cli", tmp_path / "manifests" / "run-cli.md")
    seen = _capture_waiver(monkeypatch)

    crew.complete(
        "run-cli",
        gate="not-run",
        outcome="promotion attempted under a declared domain invariant",
        root=repository,
        domain_waiver=WAIVER_REASON,
    )

    assert seen["waiver_reason"] == WAIVER_REASON


def test_without_the_waiver_the_same_promotion_is_refused(
    repository: Path, tmp_path: Path
) -> None:
    """The control: the guard bites when no reason is stated."""
    _pointer(repository, "run-refused", tmp_path / "manifests" / "run-refused.md")

    answer = mcp_module._crew(
        action="complete",
        run_id="run-refused",
        gate="not-run",
        outcome="promotion attempted with no waiver",
    )

    assert answer.get("ok") is False, answer
    assert "convergence" in str(answer.get("detail") or ""), answer
