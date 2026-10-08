"""The provenance leaf rule has one owner: ``_leaf_paths``.

``_record_provenance`` stamps a layer onto every leaf key path, and the lane
expansion copies lane keys onto ``backends:`` entries through the same leaf
walk. Two copies of the leaf rule stay correct only while they agree on which
paths are leaves, so this pins the two surfaces to a single walk: changing
``_leaf_paths`` alone must move ``origin()`` and ``provenance_sources``
together, which cannot happen unless ``_record_provenance`` walks leaves
through ``_leaf_paths``.

The leaf under test is an empty mapping, which the shipped rule counts as a
leaf. The test replaces the rule in ``_leaf_paths`` alone with one that counts
an empty mapping as a non-leaf. A resolver that kept its own traversal would
leave ``origin()`` still naming the empty-mapping key while only
``provenance_sources`` moved; both surfaces moving is what one owner means.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from reckon import flight

HOST = """
version: 1
lanes:
  claude:
    launch: cli
    command: claude
    environment: {}
    default_model: sonnet
    models:
      sonnet:
        model: claude-sonnet-5-5
"""

CATALOGUE = "version: 1\n"

# A scalar leaf the shipped rule and the mutated rule must both still find, so
# an absent entry for the empty mapping reads as the rule change rather than a
# walk that stopped walking.
SCALAR_LANE_LEAF = "lanes.claude.command"

# The empty mapping the lane declares. ``_record_provenance`` stamps its raw
# layer path, and the lane expansion carries the same key onto the backend
# entry, so it is visible on both provenance surfaces.
LANE_EMPTY_LEAF = "lanes.claude.environment"
BACKEND_EMPTY_LEAF = "backends.claude.environment"


def _empty_mapping_is_not_a_leaf(
    data: Mapping[str, Any], prefix: str = ""
) -> Iterator[str]:
    """The shipped walk, except an empty mapping yields no path.

    A non-empty mapping still recurses and every non-mapping is still a leaf;
    the only difference from ``flight._leaf_paths`` is that an empty mapping is
    a branch with nothing under it rather than a leaf of its own.
    """
    for key, value in data.items():
        path = f"{prefix}{key}"
        if isinstance(value, Mapping):
            yield from _empty_mapping_is_not_a_leaf(value, prefix=f"{path}.")
        else:
            yield path


def _resolve(tmp_path: Path, monkeypatch=None) -> flight.ResolvedFlight:
    host = tmp_path / "host.yaml"
    host.write_text(HOST)
    catalogue = tmp_path / "catalogue.yaml"
    catalogue.write_text(CATALOGUE)
    if monkeypatch is not None:
        monkeypatch.setattr(flight, "_leaf_paths", _empty_mapping_is_not_a_leaf)
    return flight.resolve(host_path=host, catalogue_path=catalogue)


def test_origin_and_sources_follow_a_change_to_the_leaf_rule(tmp_path, monkeypatch):
    baseline = _resolve(tmp_path)

    # Under the shipped rule an empty mapping is a leaf. ``_record_provenance``
    # stamps its raw lane path, and the lane expansion carries the same key
    # onto the backend entry, so both surfaces carry it.
    assert baseline.origin(LANE_EMPTY_LEAF) == "host"
    assert baseline.provenance_sources[BACKEND_EMPTY_LEAF] == LANE_EMPTY_LEAF
    assert baseline.origin(BACKEND_EMPTY_LEAF) == "host"

    changed = _resolve(tmp_path, monkeypatch)

    # The rule changed in ``_leaf_paths`` alone: an empty mapping is no longer
    # a leaf, so the raw-layer stamp and the lane-expansion source both drop it.
    # A resolver whose ``_record_provenance`` kept its own walk would still
    # stamp the raw path here.
    assert changed.origin(LANE_EMPTY_LEAF) is None
    assert BACKEND_EMPTY_LEAF not in changed.provenance_sources
    assert changed.origin(BACKEND_EMPTY_LEAF) is None
    # The weakened rule still walks everything else, so the absent entries above
    # are the empty-mapping rule and not a traversal that found nothing.
    assert changed.origin(SCALAR_LANE_LEAF) == "host"
