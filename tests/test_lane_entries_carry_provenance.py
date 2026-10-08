"""Every key ``expand_lanes`` builds names the lane layer and its source key.

A lane declaration expands into ``backends:`` entries so every caller keeps
reading the flat shape. Before this node the expansion copied the values but
recorded nothing about them: the lane-named entry carried no provenance for the
keys the lane supplied, and the derived entry's keys the catalogue had filled
first stayed attributed to the catalogue even after the lane overwrote them.
A reader could not see which ``lanes.*`` key set a value.

These two tests pin the two surfaces:

  - the resolver: ``ResolvedFlight.provenance`` names the layer of the
    ``lanes.*`` key each copied key carries, and ``provenance_sources`` names
    that key; the synthesised keys name the ``expansion`` layer and have no
    source key;
  - ``flight_report``'s payload: the same source keys under the top-level
    ``provenance_sources`` key.

Both fail on the resolver before the expansion records provenance at all.
"""

from __future__ import annotations

from pathlib import Path

from reckon import flight

# The catalogue fills these four keys on ``backends.claude-opus`` first; the
# lane model then overwrites every one of them, so each must end up naming the
# lane layer rather than the catalogue.
CATALOGUE_FILLED = ("alias", "budget_group", "effort", "model")

CATALOGUE = """
version: 1
backends:
  claude-opus:
    model: claude-opus-5-5
    effort: high
    alias: opus 5.5
    budget_group: claude-sub
"""

HOST = """
version: 1
backends:
  claude-opus:
    command: claude
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
        budget_group: claude-sub
        time_budget: 40m
"""

# The keys the lane block itself supplies, shared by every entry it expands to.
LANE_LEVEL = ("launch", "command", "sandbox", "session_reuse", "time_budget")
# The keys the expansion synthesises, which name no ``lanes.*`` source.
SYNTHESISED = ("lane", "model_key", "derived_from")


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    host = _write(tmp_path / "host.yaml", HOST)
    catalogue = _write(tmp_path / "catalogue.yaml", CATALOGUE)
    return host, catalogue


def _resolve(tmp_path: Path) -> flight.ResolvedFlight:
    host, catalogue = _paths(tmp_path)
    return flight.resolve(host_path=host, catalogue_path=catalogue)


def _leaves(node: dict, prefix: str = "") -> list[str]:
    paths: list[str] = []
    for key, value in node.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict) and value:
            paths.extend(_leaves(value, f"{path}."))
        else:
            paths.append(path)
    return paths


def test_lane_named_entry_names_the_lane_layer_and_source(tmp_path):
    resolved = _resolve(tmp_path)
    entry = resolved.config["backends"]["claude"]

    for key in ("launch", "command", "sandbox", "session_reuse", "time_budget"):
        assert resolved.provenance[f"backends.claude.{key}"] == "host"
        assert resolved.provenance_sources[f"backends.claude.{key}"] == (
            f"lanes.claude.{key}"
        )
    # The lane supplies each non-lane key through its default model.
    for key in ("model", "effort", "alias"):
        assert resolved.provenance[f"backends.claude.{key}"] == "host"
        assert resolved.provenance_sources[f"backends.claude.{key}"] == (
            f"lanes.claude.models.sonnet.{key}"
        )
    # Every leaf the entry carries is accounted for.
    for leaf in _leaves(entry):
        assert f"backends.claude.{leaf}" in resolved.provenance
    for key in ("lane", "model_key"):
        assert resolved.provenance[f"backends.claude.{key}"] == "expansion"
        assert f"backends.claude.{key}" not in resolved.provenance_sources


def test_derived_entry_names_the_lane_layer_over_the_catalogue(tmp_path):
    resolved = _resolve(tmp_path)
    entry = resolved.config["backends"]["claude-opus"]

    # The four keys the catalogue filled first are attributed to the lane, and
    # their source names the lane model key that overwrote them.
    for key in CATALOGUE_FILLED:
        assert resolved.provenance[f"backends.claude-opus.{key}"] == "host"
        assert resolved.provenance_sources[f"backends.claude-opus.{key}"] == (
            f"lanes.claude.models.opus.{key}"
        )
    # A model-level key overrides the lane-level default, so its source is the
    # model key rather than the lane block even though both declared it.
    assert resolved.provenance_sources["backends.claude-opus.time_budget"] == (
        "lanes.claude.models.opus.time_budget"
    )
    for key in ("command", "launch", "sandbox", "session_reuse"):
        assert resolved.provenance[f"backends.claude-opus.{key}"] == "host"
        assert resolved.provenance_sources[f"backends.claude-opus.{key}"] == (
            f"lanes.claude.{key}"
        )
    for leaf in _leaves(entry):
        assert f"backends.claude-opus.{leaf}" in resolved.provenance
    for key in SYNTHESISED:
        assert resolved.provenance[f"backends.claude-opus.{key}"] == "expansion"
        assert f"backends.claude-opus.{key}" not in resolved.provenance_sources


def test_flight_report_carries_the_source_keys(tmp_path):
    host, catalogue = _paths(tmp_path)
    payload = flight.flight_report(host_path=host, catalogue_path=catalogue)

    sources = payload["provenance_sources"]
    assert sources["backends.claude.model"] == "lanes.claude.models.sonnet.model"
    assert sources["backends.claude.launch"] == "lanes.claude.launch"
    for key in CATALOGUE_FILLED:
        assert sources[f"backends.claude-opus.{key}"] == (
            f"lanes.claude.models.opus.{key}"
        )
    # The synthesised keys carry no source record on either surface.
    for name in ("claude", "claude-opus"):
        for key in ("lane", "model_key", "derived_from"):
            assert f"backends.{name}.{key}" not in sources
    # The layer mapping stays a key-path to bare-layer map, unchanged in shape.
    assert payload["provenance"]["backends.claude-opus.model"] == "host"
