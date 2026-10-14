"""This repository's project flight layer names lanes, never a lane-and-model.

Lane and model are two dimensions. A name like ``codex-astra`` or
``claude-opus`` is neither: it spells a lane and a model as one token, so a
reader cannot tell which of the two a setting applies to, and every model of a
lane needs its own budget row and its own exclusion entry. The lane expansion
in ``reckon.flight`` keeps a combined name alive whenever any layer still
declares it, so this repository's own layer must declare none.

Two facts are pinned here, both read from the resolved configuration rather
than from the layer's text:

  - the resolve of this project layer over a host that declares the ``claude``
    and ``codex`` lanes and no combined name yields no entry carrying
    ``derived_from`` (the marker the lane expansion stamps on a combined name
    it kept alive);
  - with the exclusions naming lanes, a review routed with no lane named cannot
    reach any model of an excluded lane.

The host stands in for the workstation's real host layer, which still declares
the four combined names until a coordinator edit that follows this node; the
fixture is what lets the node's own gate go green on its own change.

The negative control restores ``codex-astra`` to this repository's layer: the
derived entry comes back and ``test_project_layer_declares_no_combined_name``
fails.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from reckon import flight
from reckon.crew import recovery

REPO_ROOT = Path(__file__).resolve().parents[1]
PROJECT_LAYER = REPO_ROOT / "docs" / "state" / "reckon" / "flight.yaml"

# The repository's own catalogue, which declares the claude and codex lanes.
# The suite's isolation points every test at an absent catalogue, so a fixture
# host declares its own; these tests route by lane, so they resolve names the
# way a real dispatch does.
_REAL_CATALOGUE = (
    Path(flight.__file__).resolve().parent.parent
    / "docs"
    / "state"
    / "reckon"
    / "model-catalogue.yaml"
)

# Lanes the project exclusions cover; a review routed with no lane named must
# reach no model of either.
EXCLUDED_LANES = {"claude", "codex"}

# A host layer that declares the claude and codex lanes and no combined name.
# Its model keys match the real host's, so a combined name the project layer
# re-declares would be derived here exactly as it is in production.
FIXTURE_HOST = """\
version: 1
lanes:
  claude:
    launch: cli
    command: claude
    sandbox: worktree-full
    session_reuse: false
    time_budget: 25m
    default_model: sonnet
    models:
      sonnet:
        model: claude-sonnet-5-5
      haiku:
        model: claude-haiku-5-5
      opus:
        model: claude-opus-5-5
  codex:
    launch: cli
    command: codex
    sandbox: worktree-full
    session_reuse: false
    time_budget: 55m
    default_model: sol
    models:
      sol:
        model: gpt-6-sol
      astra:
        model: gpt-6-astra
      luna:
        model: gpt-6-luna
"""


@pytest.fixture
def catalogue(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve lane names against the repository's catalogue.

    The suite isolates every test from the checkout's catalogue, which leaves
    every lane name unknown to the resolver; these tests route a review by
    lane, so they point it back the way a real dispatch sees it.
    """

    assert _REAL_CATALOGUE.is_file()
    monkeypatch.setenv("RECKON_MODEL_CATALOGUE", str(_REAL_CATALOGUE))


def _resolve(project_layer_text: str, tmp_path: Path) -> dict[str, Any]:
    host = tmp_path / "host.yaml"
    host.write_text(FIXTURE_HOST, encoding="utf-8")
    project = tmp_path / "project.yaml"
    project.write_text(project_layer_text, encoding="utf-8")
    resolved = flight.resolve(
        project="reckon",
        host_path=host,
        project_path=project,
    )
    return resolved.config


def _derived_names(config: dict[str, Any]) -> list[str]:
    return [
        str(name)
        for name, entry in (config.get("backends") or {}).items()
        if isinstance(entry, dict) and "derived_from" in entry
    ]


def test_project_layer_declares_no_combined_name(
    catalogue: None, tmp_path: Path
) -> None:
    config = _resolve(PROJECT_LAYER.read_text(encoding="utf-8"), tmp_path)
    assert _derived_names(config) == []


def test_nameless_review_cannot_reach_an_excluded_lane(
    catalogue: None, tmp_path: Path
) -> None:
    config = _resolve(PROJECT_LAYER.read_text(encoding="utf-8"), tmp_path)
    backends = config.get("backends") or {}

    # The lanes the exclusions cover must actually declare a model here, or the
    # assertion below would pass on a configuration that names nothing.
    models_of_excluded = {
        str(name)
        for name, entry in backends.items()
        if isinstance(entry, dict) and entry.get("lane") in EXCLUDED_LANES
    }
    assert models_of_excluded, "fixture declares no model of an excluded lane"

    reachable = set(recovery._ordered_review_lanes(config))
    assert reachable, "fixture leaves no review lane reachable"
    assert not (reachable & models_of_excluded)
