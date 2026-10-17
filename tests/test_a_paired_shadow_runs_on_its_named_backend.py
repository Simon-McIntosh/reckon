"""A paired dispatch resolves its shadow arm against the backend it names.

``dispatch_pair`` passed the dispatch's own config, unmodified, into
``_shadow_dispatch_config``, which resolved the node's role and then refused
whenever that resolution named a backend other than the candidate. A role that
does not already route to the candidate — the ordinary case, and the whole
point of naming one — was therefore rejected, so ``--pair-backend`` refused
every backend except the one the role already resolved to.

The shadow arm now resolves through ``resolve_role_override``, the same
function that re-resolves a role onto a budget fallback: the role's overlays
still apply, only the concrete backend changes. A candidate no layer defines is
the one refusal left, and it carries D21 with the resolver's own text.

Every case writes nothing outside the test's temporary tree: the isolated home
and repository fixtures are the ones ``test_a_paired_dispatch_hides_each_arm``
binds. The declared negative control restores the resolve_role call and the
equality check; the first case must then fail with D21.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys

import pytest

from reckon.crew.node import CrewError
from reckon.crew.refusals import DISPATCH_REFUSAL_REMEDIES
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

# The environment variable the declared mutation is applied through: with it set
# the shadow arm resolves through ``resolve_role`` and the pairing is refused.
RESTORE_ROLE_RESOLUTION_ENV = "RECKON_TEST_PAIR_SHADOW_RESOLVE_ROLE"

# The environment variable the second declared mutation is applied through:
# with it set D21's remedy reverts to the text the fix replaced.
OLD_REMEDY_ENV = "RECKON_TEST_PAIR_OLD_REMEDY"

NEGATIVE_CONTROL_MUTATION = (
    "restore the resolve_role call and the equality check in "
    "_shadow_dispatch_config and the first case fails with D21 because alpha is "
    "not candidate; restore the old D21 remedy text in reckon/crew/refusals.py "
    "and the third case fails on its flight override assertion; each mutation "
    "applied alone under pytest"
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


def _guided_to_alpha_config() -> dict:
    """The named-backend config with the node's spec level routed to ``alpha``.

    A ``by_spec_level`` overlay on the implement role names ``alpha``, so a
    resolver that honoured the overlay's backend would route the shadow there.
    """
    config = _named_backend_config()
    config["roles"]["implement"] = {"by_spec_level": {"guided": {"backend": "alpha"}}}
    return config


def _mutated_shadow_config(**kwargs):
    """Resolve the node's role and refuse when it differs from the candidate.

    This restores the resolve_role call and the equality check the fix replaced,
    so the declared mutation runs the very shape the fix removed.
    """
    from reckon.crew.routing import resolve_role

    config = kwargs["config"]
    node = kwargs["node"]
    candidate_backend = kwargs["candidate_backend"]
    resolved_backend, candidate = resolve_role(config, node.role, node.spec_level)
    if resolved_backend != candidate_backend:
        from reckon.crew.refusals import format_refusal

        raise CrewError(
            format_refusal(
                "D21",
                f"candidate backend {candidate_backend!r} resolved to "
                f"{resolved_backend!r}; route the node explicitly to the candidate",
            )
        )
    return config, candidate


@pytest.fixture(autouse=True)
def _apply_declared_mutation(monkeypatch):
    """Apply a declared mutation in-process when its env var asks for it."""
    if os.environ.get(RESTORE_ROLE_RESOLUTION_ENV) == "1":
        monkeypatch.setattr(
            dispatch_module, "_shadow_dispatch_config", _mutated_shadow_config
        )
    if os.environ.get(OLD_REMEDY_ENV) == "1":
        monkeypatch.setitem(
            DISPATCH_REFUSAL_REMEDIES,
            "D21",
            "`reckon crew shadow --backend <candidate>` after correcting the "
            "flight override",
        )


def test_a_paired_shadow_arm_runs_on_the_candidate_backend(home, repo) -> None:
    """The shadow arm lands on the named backend; the primary keeps its own.

    The primary arm resolves the node's role, which names no backend, so it
    lands on the flight default. The shadow arm must land on the candidate the
    caller named, carrying the candidate's model.
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
    assert result["primary_run_id"] != result["shadow_run_id"]
    assert result["primary"]["agent"]["backend"] == "alpha"
    assert result["shadow"]["agent"]["backend"] == PAIR_BACKEND
    assert result["shadow"]["agent"]["model"] == CANDIDATE_MODEL


def test_a_spec_level_overlay_does_not_move_the_shadow_arm(home, repo) -> None:
    """A by_spec_level overlay routing the node to alpha leaves the shadow on candidate.

    The overlay still applies to the role's other settings, but the shadow arm
    was asked for the candidate and must run there: a named backend wins over
    the overlay that would otherwise select the primary's lane.
    """
    result = dispatch_module.dispatch_pair(
        node=_node(spec_level="guided"),
        project=PROJECT,
        repo=repo,
        config=_guided_to_alpha_config(),
        session="sess",
        pair_backend=PAIR_BACKEND,
        dry_run=True,
    )
    assert result["paired"] is True
    assert result["shadow"]["agent"]["backend"] == PAIR_BACKEND
    assert result["shadow"]["agent"]["model"] == CANDIDATE_MODEL


def test_an_undefined_candidate_is_refused_with_d21(home, repo) -> None:
    """A candidate no layer defines is refused, and the remedy names the real fix.

    The detail names the undefined backend, the message ends with the sentence
    ``format_refusal`` builds from the registered remedy read here rather than
    from a literal, and the message never sends the reader looking for a flight
    override that does not exist. The raw error from ``_effective_backend``,
    without the D21 wrap, fails this case.
    """
    with pytest.raises(CrewError) as excinfo:
        dispatch_module.dispatch_pair(
            node=_node(spec_level="guided"),
            project=PROJECT,
            repo=repo,
            config=_named_backend_config(),
            session="sess",
            pair_backend="nonesuch",
            dry_run=True,
        )
    message = str(excinfo.value)
    assert "D21" not in message
    remedy = DISPATCH_REFUSAL_REMEDIES["D21"]
    assert message.endswith(f"Resolve with {remedy}.")
    assert "nonesuch" in message
    assert "not defined" in message
    assert "flight override" not in message


def test_the_cli_pair_flag_drives_the_shadow_arm(home, repo, monkeypatch) -> None:
    """The paired dry run through the command line lands the shadow on candidate.

    The paired branch is entered before the ordinary dispatch path, so a wiring
    error there is invisible to every other test in this file.
    """
    monkeypatch.setattr(
        _test_crew.crew_dispatch_commands,
        "_resolved_flight",
        lambda *args, **kwargs: _named_backend_config(),
    )
    node = _node()
    result = _test_crew.CliRunner().invoke(
        _test_crew.cli_module.main,
        [
            "crew",
            "dispatch",
            "--project",
            PROJECT,
            "--plan",
            node.plan,
            "--section",
            node.section,
            "--spec-level",
            "guided",
            "--node",
            node.id,
            "--goal",
            node.goal,
            "--done-when",
            node.done_when,
            "--write-path",
            node.write_paths[0],
            "--session",
            "sess",
            "--repo",
            str(repo),
            "--negative-control",
            "none: the dry run prepares no worktree and executes no check",
            "--pair-backend",
            PAIR_BACKEND,
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["paired"] is True
    assert payload["shadow"]["agent"]["backend"] == PAIR_BACKEND
    assert payload["shadow"]["agent"]["model"] == CANDIDATE_MODEL


def _negative_control_report() -> int:
    """Run this file with the declared mutation applied; 1 when nothing failed."""
    print(NEGATIVE_CONTROL_MUTATION)
    env = {**os.environ, RESTORE_ROLE_RESOLUTION_ENV: "1"}
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
