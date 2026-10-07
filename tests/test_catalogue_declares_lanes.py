"""The model catalogue declares lanes and an alias table for old names.

The catalogue regroups its models under ``lanes.<lane>.models`` with a
``default_model`` per lane, keeps the ``backends:`` blocks so the two
groupings can be compared directly, and carries an ``aliases:`` table mapping
each legacy backend name to the lane and model key it names.

The alias table is history, not routing: a stored ledger row, run record or
review is never rewritten, so a name whose backend block has retired still has
to resolve to a pair. ``codex-terra`` and ``codex-spark`` name codex models the
lane no longer offers, and ``clive-glm`` a clive model that retired; these are
kept so old rows normalise, and are asserted here to be exactly that — names
with no declared model behind them.

The committed catalogue is read by its repository path, because the suite
otherwise points ``RECKON_MODEL_CATALOGUE`` at an absent file so a fixture host
cannot inherit this machine's routing. ``RECKON_CATALOGUE_FIXTURE`` lets the
negative control run the same assertions against a copy of the catalogue with
one lane model deleted; with it unset the committed file is read, so the head
arm is the catalogue as shipped. Deleting a model a live alias names then
reddens ``test_every_alias_resolves_to_a_declared_lane_and_model``.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from reckon import flight

# The repository's committed catalogue, resolved from the running package
# rather than through ``model_catalogue_path()`` so the suite's absent-catalogue
# isolation does not hide the file this test exists to assert.
CATALOGUE_PATH = (
    Path(flight.__file__).resolve().parent.parent
    / "docs"
    / "state"
    / "reckon"
    / "model-catalogue.yaml"
)

# Read by the negative control only; unset in a normal run.
FIXTURE_ENV = "RECKON_CATALOGUE_FIXTURE"

# The lanes the catalogue declares, each with the model keys it offers and the
# default a dispatch on the lane resolves to when it names no model.
LANES = {
    "claude": {"default_model": "sonnet", "models": {"sonnet", "opus", "haiku"}},
    "codex": {"default_model": "sol", "models": {"sol", "astra", "luna"}},
    "clive": {"default_model": "flash", "models": {"flash"}},
}

# The alias table, exactly: every legacy name and the pair it resolves to.
ALIASES = {
    "claude-opus": ("claude", "opus"),
    "claude-haiku": ("claude", "haiku"),
    "codex-astra": ("codex", "astra"),
    "codex-luna": ("codex", "luna"),
    "codex-terra": ("codex", "terra"),
    "codex-spark": ("codex", "spark"),
    "clive-glm": ("clive", "glm"),
}

# Aliases that name a model the lane still offers. The rest name a retired
# model and resolve to a lane and a key kept for history only.
LIVE_ALIASES = {"claude-opus", "claude-haiku", "codex-astra", "codex-luna"}
RETIRED_ALIASES = set(ALIASES) - LIVE_ALIASES


def _catalogue_path() -> Path:
    override = os.environ.get(FIXTURE_ENV)
    return Path(override) if override else CATALOGUE_PATH


def _catalogue() -> dict:
    return yaml.safe_load(_catalogue_path().read_text())


def _backend_lane_model(name: str, lanes: dict) -> tuple[str, str]:
    """Return the ``(lane, model key)`` a backend entry's name corresponds to."""
    if name in lanes:
        return name, lanes[name]["default_model"]
    for lane_name, lane in lanes.items():
        if name.startswith(f"{lane_name}-"):
            return lane_name, name[len(lane_name) + 1 :]
    raise AssertionError(f"backend {name!r} corresponds to no lane model")


def test_catalogue_declares_three_lanes_each_with_a_default_model():
    lanes = _catalogue()["lanes"]
    assert set(lanes) == set(LANES)
    for name, expected in LANES.items():
        lane = lanes[name]
        assert lane["default_model"] == expected["default_model"], name
        assert lane["default_model"] in lane["models"], name
        assert set(lane["models"]) == expected["models"], name


def test_each_backend_entry_matches_a_lane_model():
    """The two groupings agree, so the new resolution compares with the old."""
    catalogue = _catalogue()
    lanes = catalogue["lanes"]
    for name, backend in catalogue["backends"].items():
        if name == "native":
            continue
        lane_name, key = _backend_lane_model(name, lanes)
        model = lanes[lane_name]["models"][key]
        for field in ("model", "effort", "alias", "budget_group"):
            if field in model or field in backend:
                assert model.get(field) == backend.get(field), (name, field)


def test_every_alias_resolves_to_a_declared_lane_and_model():
    catalogue = _catalogue()
    lanes = catalogue["lanes"]
    aliases = catalogue["aliases"]

    assert set(aliases) == set(ALIASES)
    for name, (lane, key) in ALIASES.items():
        entry = aliases[name]
        assert entry["lane"] == lane, name
        assert entry["model_key"] == key, name

    for name, (lane, key) in ALIASES.items():
        assert lane in lanes, f"{name} names lane {lane!r}, which is not declared"
        assert key, f"{name} names an empty model key"
        assert name == f"{lane}-{key}", (
            f"{name} is not the derived legacy name of {lane}:{key}"
        )

    # Every alias naming a model its lane still offers resolves to that model.
    for name in LIVE_ALIASES:
        lane, key = ALIASES[name]
        assert key in lanes[lane]["models"], (
            f"{name} names model key {key!r} the {lane} lane does not declare"
        )
    # The retired names resolve to a lane but offer no declared model: they are
    # kept so an old row normalises, not because the lane still serves them.
    for name in RETIRED_ALIASES:
        lane, key = ALIASES[name]
        assert key not in lanes[lane]["models"], name


def test_lanes_and_aliases_move_no_resolved_value(tmp_path):
    """Adding lanes and aliases to the catalogue changes nothing resolved.

    Resolution reads only the catalogue's ``backends`` block, so the declared
    lanes and the alias table are inert to routing. A positive control on the
    instrument: the two catalogues still differ, so an equality this test
    reports is not the equality of two identical inputs.
    """
    catalogue = _catalogue_path()
    stripped = yaml.safe_load(catalogue.read_text())
    lanes = stripped.pop("lanes")
    aliases = stripped.pop("aliases")
    assert lanes and aliases, "the catalogue carries no lanes/aliases to strip"
    stripped_path = tmp_path / "catalogue-without-lanes.yaml"
    stripped_path.write_text(yaml.safe_dump(stripped))

    full = flight.resolve("reckon", catalogue_path=catalogue).config
    without = flight.resolve("reckon", catalogue_path=stripped_path).config
    assert full == without