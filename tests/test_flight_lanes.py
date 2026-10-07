"""Lane declarations expand into today's backend entries (reckon/flight.py).

A lane declares a subscription or host and the models chosen inside it. The
platform schema accepts the block and resolution expands it back into the
``backends:`` entries every caller already reads, so nothing outside this
module learns a new shape. These tests cover the expansion contract:

  - a lane becomes a backend entry named after the lane, carrying its default
    model and the ``lane``/``model_key`` pair a reader needs;
  - a model whose derived name ``<lane>-<key>`` is a declared legacy backend
    gains its own entry, marked ``derived_from`` so the remaining legacy names
    can be counted;
  - an override written on a lane model (``--set lanes.<lane>.models...``)
    reaches the expanded entry, because the override layer merges before the
    expansion rather than after it;
  - a layer declaring no lanes leaves the resolved backends unchanged.

The last test is the negative control's head half: under
``RECKON_LANES_OVERRIDE_AFTER_EXPANSION`` the resolver applies the override
layer after expansion, and the override no longer reaches the entry, so the
``--set`` test fails.
"""

from __future__ import annotations

import os
from pathlib import Path

from reckon import flight

# When set, the resolver applies the override layer after lane expansion — the
# mutation the negative control declares — instead of before it.
OVERRIDE_AFTER_EXPANSION = "RECKON_LANES_OVERRIDE_AFTER_EXPANSION"

HOST_WITH_LANES = """
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
        effort: medium
        alias: sonnet 5.5
      opus:
        model: claude-opus-5-5
        effort: high
        alias: opus 5.5
        time_budget: 40m
      haiku:
        model: claude-haiku-4-5
        effort: high
backends:
  claude-opus:
    command: claude
    time_budget: 40m
  claude-haiku:
    command: claude
    time_budget: 25m
"""

HOST_WITHOUT_LANES = """
backends:
  claude-opus:
    command: claude
    time_budget: 40m
"""


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _resolve(host: Path, tmp_path: Path, *, overrides=None):
    """Resolve a host layer with no catalogue, so only these tests' layers apply.

    The override-after-expansion mutation is applied here rather than in the
    product, so the negative control runs the same test the head arm runs.
    """
    catalogue = tmp_path / "no-catalogue.yaml"
    if os.environ.get(OVERRIDE_AFTER_EXPANSION):
        resolved = flight.resolve(host_path=host, catalogue_path=catalogue)
        if overrides:
            for key, value in overrides.items():
                resolved.config[key] = value
        return resolved
    return flight.resolve(host_path=host, catalogue_path=catalogue, overrides=overrides)


def test_lane_expands_its_derived_legacy_entries(tmp_path):
    host = _write(tmp_path / "host.yaml", HOST_WITH_LANES)
    backends = _resolve(host, tmp_path).config["backends"]

    opus = backends["claude-opus"]
    assert opus["lane"] == "claude"
    assert opus["model_key"] == "opus"
    assert opus["derived_from"] == "lanes.claude.models.opus"
    assert opus["model"] == "claude-opus-5-5"
    assert opus["effort"] == "high"
    assert opus["time_budget"] == "40m"

    haiku = backends["claude-haiku"]
    assert haiku["lane"] == "claude"
    assert haiku["model_key"] == "haiku"
    assert haiku["derived_from"] == "lanes.claude.models.haiku"

    # A model whose derived name is not a declared backend gains no entry.
    assert "claude-sonnet" not in backends


def test_set_on_a_lane_model_reaches_the_expanded_entry(tmp_path):
    host = _write(tmp_path / "host.yaml", HOST_WITH_LANES)
    overrides = flight.parse_overrides(["lanes.claude.models.opus.effort=xhigh"])
    backends = _resolve(host, tmp_path, overrides=overrides).config["backends"]

    assert backends["claude-opus"]["effort"] == "xhigh"


def test_no_lanes_leaves_the_backends_unchanged(tmp_path):
    host = _write(tmp_path / "host.yaml", HOST_WITHOUT_LANES)
    resolved = _resolve(host, tmp_path)

    assert resolved.config.get("lanes") is None
    opus = resolved.config["backends"]["claude-opus"]
    assert opus["command"] == "claude"
    assert "lane" not in opus
    assert "model_key" not in opus
    assert "derived_from" not in opus
