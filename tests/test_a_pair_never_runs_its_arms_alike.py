"""A paired dispatch refuses two arms that resolve to one backend and model.

``--pair-backend`` names the lane the shadow arm runs on. When the picker
routes the primary arm to that same lane, both arms run one backend with one
model: the pair compares nothing and spends that lane twice. The comparison
``_shadow_dispatch_config`` already builds carries both arms' values — the
backend is always substituted, and the model only when the arms differ — so
``dispatch_pair`` refuses on it before the dry-run return and before any run
directory or worktree exists. A pair whose arms share a backend but run
different models still compares the two models and is not refused.

Every case writes nothing outside the test's temporary tree: the isolated home
and repository fixtures are the ones ``test_a_paired_dispatch_hides_each_arm``
binds. The declared negative control removes the check and the first case must
then fail; applied alone with the check comparing backends only, the third case
must fail.
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import os
import subprocess
import sys

import pytest

from reckon.crew.node import CrewError
from reckon.crew.runs import runs_dir
from tests import test_crew as _test_crew

# ``reckon.crew`` re-exports the dispatcher's ``dispatch`` under that name, so
# the module is reached by import rather than by the facade attribute.
dispatch_module = importlib.import_module("reckon.crew.dispatch")

CONFIG = _test_crew.CONFIG
_node = _test_crew._node
home = _test_crew.home
repo = _test_crew.repo

PAIR_BACKEND = "candidate"
PROJECT = "proj"
CANDIDATE_MODEL = "candidate-model"
OTHER_MODEL = "other-model"

# The environment variable the declared mutation is applied through: ``removed``
# drops the check, ``backends-only`` compares backends without the model branch.
MUTATION_ENV = "RECKON_TEST_PAIR_ALIKE_MUTATION"

NEGATIVE_CONTROL_MUTATION = (
    "remove the check from dispatch_pair and the first case fails; make the "
    "check compare backends only and the third case fails; each mutation "
    "applied alone under pytest, with pytest's default short summary so the log "
    "lists the FAILED test id"
)


def _named_backend_config() -> dict:
    """Default backend ``alpha``, implement role unset, candidate configured.

    The implement role names no backend, so the node's role resolves to the
    flight's ``default_backend`` unless a layer overrides it. A second backend,
    ``candidate``, carries its own model.
    """
    config = json.loads(json.dumps(CONFIG))
    config["backends"]["candidate"] = {
        **config["backends"]["alpha"],
        "model": CANDIDATE_MODEL,
    }
    return config


def _primary_on_candidate_config() -> dict:
    """The implement role routes to ``candidate``, so the primary lands there.

    The primary resolves to ``candidate`` with its one model — the same backend
    and model the shadow arm names — which is the alike case a pair refuses.
    """
    config = _named_backend_config()
    config["roles"]["implement"] = {"backend": "candidate"}
    return config


def _backends_only_check(comparison: dict) -> None:
    """The check with its model branch dropped: backends decide alone."""
    substituted = comparison.get("substituted") or {}
    alike_backend = substituted.get("backend") or {}
    if alike_backend.get("primary") is not None and alike_backend.get(
        "primary"
    ) == alike_backend.get("shadow"):
        from reckon.crew.refusals import format_refusal

        raise CrewError(
            format_refusal(
                "D21",
                "both arms of the pair resolve to backend "
                f"{alike_backend.get('primary')!r}; the pair backend must "
                "differ from the lane the primary runs on",
            )
        )


@pytest.fixture(autouse=True)
def _apply_declared_mutation(monkeypatch):
    """Apply a declared mutation in-process when its env var asks for it."""
    mutation = os.environ.get(MUTATION_ENV)
    if mutation == "removed":
        monkeypatch.setattr(
            dispatch_module, "_refuse_arms_that_resolve_alike", lambda comparison: None
        )
    elif mutation == "backends-only":
        monkeypatch.setattr(
            dispatch_module, "_refuse_arms_that_resolve_alike", _backends_only_check
        )


def _run_dir_names() -> set[str]:
    return (
        {path.name for path in runs_dir().iterdir()} if runs_dir().is_dir() else set()
    )


def test_arms_that_resolve_alike_are_refused(home, repo) -> None:
    """A primary routed to the pair backend is refused, dry run and real alike.

    The refusal carries D21's registered remedy sentence and names the backend
    and model both arms resolved to. Nothing is spent: no worker is launched
    and no run directory for either arm is created.
    """
    config = _primary_on_candidate_config()
    launched: list[str] = []

    def launcher(plan, *, log_path, stderr_path, prompt_path):
        launched.append(str(log_path))
        return 0

    before = _run_dir_names()

    with pytest.raises(CrewError) as dry:
        dispatch_module.dispatch_pair(
            node=_node(spec_level="guided"),
            project=PROJECT,
            repo=repo,
            config=config,
            session="sess",
            pair_backend=PAIR_BACKEND,
            dry_run=True,
        )
    dry_message = str(dry.value)
    assert "Resolve with" in dry_message
    assert PAIR_BACKEND in dry_message
    assert CANDIDATE_MODEL in dry_message

    with pytest.raises(CrewError) as real:
        dispatch_module.dispatch_pair(
            node=_node(spec_level="guided"),
            project=PROJECT,
            repo=repo,
            config=config,
            session="sess",
            pair_backend=PAIR_BACKEND,
            launcher=launcher,
        )
    assert "Resolve with" in str(real.value)

    assert launched == []
    assert _run_dir_names() == before


def test_a_primary_on_the_default_backend_pairs(home, repo) -> None:
    """A primary on the flight default leaves the shadow free to run the candidate.

    The two arms resolve to different backends, so the pair compares something
    and the dry run reports both agents on their own lanes.
    """
    result = dispatch_module.dispatch_pair(
        node=_node(spec_level="guided"),
        project=PROJECT,
        repo=repo,
        config=_named_backend_config(),
        session="sess",
        pair_backend=PAIR_BACKEND,
        dry_run=True,
    )
    assert result["paired"] is True
    assert result["primary"]["agent"]["backend"] == "alpha"
    assert result["shadow"]["agent"]["backend"] == PAIR_BACKEND
    assert result["shadow"]["agent"]["model"] == CANDIDATE_MODEL


def test_a_shared_backend_with_different_models_pairs(home, repo, monkeypatch) -> None:
    """Two arms on one backend but different models still compare the models.

    The primary is planned on the candidate like the shadow, but with a
    different model, so the pair compares the two models and must not be
    refused. The primary is built by wrapping ``plan_dispatch`` as the
    dispatcher reads it — the first call dispatch_pair makes — and replacing
    only that primary's ``backend_settings["model"]``.
    """
    config = _primary_on_candidate_config()
    real_plan_dispatch = dispatch_module.plan_dispatch
    primary_run_id: dict[str, str | None] = {"value": None}

    def wrapped_plan_dispatch(**kwargs):
        plan = real_plan_dispatch(**kwargs)
        if primary_run_id["value"] is None:
            primary_run_id["value"] = kwargs.get("run_id")
        if kwargs.get("run_id") == primary_run_id["value"]:
            return dataclasses.replace(
                plan,
                backend_settings={**plan.backend_settings, "model": OTHER_MODEL},
            )
        return plan

    monkeypatch.setattr(dispatch_module, "plan_dispatch", wrapped_plan_dispatch)

    result = dispatch_module.dispatch_pair(
        node=_node(spec_level="guided"),
        project=PROJECT,
        repo=repo,
        config=config,
        session="sess",
        pair_backend=PAIR_BACKEND,
        dry_run=True,
    )
    assert result["paired"] is True
    assert result["primary"]["agent"]["backend"] == PAIR_BACKEND
    assert result["primary"]["agent"]["model"] == OTHER_MODEL
    assert result["shadow"]["agent"]["backend"] == PAIR_BACKEND
    assert result["shadow"]["agent"]["model"] == CANDIDATE_MODEL


def _negative_control_report() -> int:
    """Run this file with a declared mutation applied; 1 when nothing failed."""
    print(NEGATIVE_CONTROL_MUTATION)
    env = {**os.environ, MUTATION_ENV: "removed"}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", __file__],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    print(proc.stdout)
    print(proc.stderr)
    print(f"EXIT={proc.returncode}")
    return 0 if proc.returncode != 0 else 1


if __name__ == "__main__":
    raise SystemExit(_negative_control_report())
