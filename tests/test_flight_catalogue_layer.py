"""Tests for the reckon-owned model catalogue layer (reckon/flight.py).

Covers the three rules the catalogue layer exists to hold:

  - a catalogue entry alone never creates a backend, so a host that does not
    declare codex resolves no codex backend and no other workstation or
    project sees a phantom candidate
  - a host value wins over the catalogue and is reported as a shadow naming the
    key, the backend and the file, so a host layer can be emptied of catalogue
    keys without the resolved values moving
  - with the host catalogue keys removed, every resolved value is unchanged and
    the catalogue is the layer the value resolves later reports
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon.flight import (
    CATALOGUE_LAYER,
    flight_report,
    model_catalogue_path,
    read_layer_file,
    resolve,
)

CATALOGUE = read_layer_file(model_catalogue_path())


def write(path: Path, text: str) -> Path:
    """Write a config layer and return its path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def layers(tmp_path):
    """Two absent layer paths a test writes only what it needs into."""
    return {
        "host": tmp_path / "host" / "flight.yaml",
        "project": tmp_path / "project" / "flight.yaml",
    }


def resolve_files(layers, *, overrides=None):
    return resolve(
        overrides=overrides,
        host_path=layers["host"],
        project_path=layers["project"],
    )


def test_a_catalogue_entry_alone_creates_no_backend(layers):
    """A host that never defines codex resolves no codex backend.

    The catalogue carries codex, codex-luna and the rest, but a backend exists
    only because some layer declared it. A host declaring a single backend of
    its own must not sprout every model the catalogue knows.
    """
    write(
        layers["host"],
        "default_backend: alpha\n"
        "backends:\n"
        "  alpha:\n"
        "    launch: cli\n"
        "    command: alpha-cli\n",
    )

    resolved = resolve_files(layers)

    assert "codex" in CATALOGUE["backends"]
    assert "codex" not in resolved.config["backends"]
    # Nothing from the catalogue reached the defined backend either.
    assert "model" not in resolved.config["backends"]["alpha"]
    assert resolved.origin("backends.codex.model") is None


def test_host_value_wins_and_is_reported_as_a_shadow(layers):
    """A host model overrides the catalogue and is named as the shadower."""
    write(
        layers["host"],
        "default_backend: codex\n"
        "backends:\n"
        "  codex:\n"
        "    launch: cli\n"
        "    command: codex\n"
        "    model: gpt-host-pinned\n",
    )

    resolved = resolve_files(layers)

    # The host value wins, and its origin is the host.
    assert resolved.config["backends"]["codex"]["model"] == "gpt-host-pinned"
    assert resolved.origin("backends.codex.model") == "host"
    # Keys the host did not name are filled from the catalogue.
    assert (
        resolved.config["backends"]["codex"]["alias"]
        == CATALOGUE["backends"]["codex"]["alias"]
    )
    assert resolved.origin("backends.codex.alias") == CATALOGUE_LAYER

    # The shadow report names the key, the backend and the file.
    shadows = [s for s in resolved.shadows if s["backend"] == "codex"]
    assert shadows == [
        {
            "backend": "codex",
            "key": "model",
            "layer": "host",
            "file": str(layers["host"]),
        }
    ]


def test_emptying_host_catalogue_keys_leaves_every_value_unchanged(layers):
    """Removing the host's catalogue keys moves provenance, not values.

    This is the property that lets the host layer be emptied in a later commit:
    the same resolved config, now sourced from the catalogue, with no host
    shadow left to report.
    """
    codex_catalogue = CATALOGUE["backends"]["codex"]
    host_body = (
        f"    model: {codex_catalogue['model']}\n"
        f"    alias: {codex_catalogue['alias']}\n"
        f"    effort: {codex_catalogue['effort']}\n"
        f"    budget_group: {codex_catalogue['budget_group']}\n"
    )
    host_src = (
        "default_backend: codex\n"
        "backends:\n"
        "  codex:\n"
        "    launch: cli\n"
        "    command: codex\n"
    )
    # Before: the host declares the catalogue keys itself, with the values the
    # catalogue also carries.
    write(layers["host"], host_src + host_body)
    before = resolve_files(layers)

    # After: the host keeps only the machine facts.
    write(layers["host"], host_src)
    after = resolve_files(layers)

    assert before.config["backends"]["codex"] == after.config["backends"]["codex"]
    for key in ("model", "alias", "effort", "budget_group"):
        # The value is unchanged and now resolves from the catalogue.
        assert after.config["backends"]["codex"][key] == codex_catalogue[key]
        assert before.origin(f"backends.codex.{key}") == "host"
        assert after.origin(f"backends.codex.{key}") == CATALOGUE_LAYER

    # Nothing shadows the catalogue once the host stops declaring its keys.
    assert [s for s in after.shadows if s["backend"] == "codex"] == []
    assert len(before.shadows) == 4


def test_flight_report_carries_the_shadow_list(layers):
    """The resolved flight reports shadows, so `reckon flight` and the crew
    flight view show a host value over a catalogue value.

    The shadows ride the layer inventory rather than a new top-level key, so
    the report's existing shape is unchanged: the host layer's own row names
    the catalogue keys its values shadow.
    """
    write(
        layers["host"],
        "default_backend: clive\n"
        "backends:\n"
        "  clive:\n"
        "    launch: cli\n"
        "    command: clive\n"
        "    model: local-model\n",
    )

    report = flight_report(
        None, host_path=layers["host"], project_path=layers["project"]
    )

    host_row = next(layer for layer in report["layers"] if layer["name"] == "host")
    assert host_row["shadows"] == [
        {
            "backend": "clive",
            "key": "model",
            "layer": "host",
            "file": str(layers["host"]),
        }
    ]
    # The layer inventory keeps its four rows.
    assert [layer["name"] for layer in report["layers"]] == [
        "shipped",
        "host",
        "project",
        "override",
    ]
