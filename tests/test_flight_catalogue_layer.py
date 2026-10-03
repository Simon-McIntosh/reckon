"""Tests for the reckon-owned model catalogue layer (reckon/flight.py).

Covers the rules the catalogue layer exists to hold:

  - a catalogue entry alone never creates a backend, so a host that does not
    declare codex resolves no codex backend and no other workstation or
    project sees a phantom candidate
  - a host value wins over the catalogue and is reported as a shadow naming the
    key, the backend and the file, so a host layer can be emptied of catalogue
    keys without the resolved values moving
  - with the host catalogue keys removed, every resolved value is unchanged and
    the catalogue is the layer the value resolves later reports
  - every project's resolution applies the catalogue, not only project reckon's
  - the catalogue is located from the running package's own checkout root with
    an environment override, and its absence leaves the host values standing
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


def test_a_second_project_resolves_the_catalogue_not_only_project_reckon(tmp_path):
    """The catalogue is applied by shared resolution, not for one project.

    A synthetic project — a temp project layer under a temp mount root, for a
    name that is not reckon — gets its backends filled from the catalogue just
    as project reckon's do. The default host layer is empty here, so every
    value below can only have come from the project layer or the catalogue.
    """
    project = (
        tmp_path
        / "mounts"
        / "other-project"
        / "docs"
        / "state"
        / "other-project"
        / "flight.yaml"
    )
    write(
        project,
        "default_backend: codex\n"
        "backends:\n"
        "  codex:\n"
        "    launch: cli\n"
        "    command: codex\n",
    )

    resolved = resolve(
        host_path=tmp_path / "host" / "flight.yaml",
        project_path=project,
    )

    codex = resolved.config["backends"]["codex"]
    assert codex["model"] == CATALOGUE["backends"]["codex"]["model"]
    assert codex["alias"] == CATALOGUE["backends"]["codex"]["alias"]
    assert resolved.origin("backends.codex.model") == CATALOGUE_LAYER
    # The project's own choice still wins over the catalogue.
    write(
        project,
        "default_backend: codex\n"
        "backends:\n"
        "  codex:\n"
        "    launch: cli\n"
        "    command: codex\n"
        "    model: project-pinned\n",
    )
    pinned = resolve(
        host_path=tmp_path / "host" / "flight.yaml",
        project_path=project,
    )
    assert pinned.config["backends"]["codex"]["model"] == "project-pinned"
    assert pinned.origin("backends.codex.model") == "project"


def test_a_missing_catalogue_leaves_the_layer_below_standing(tmp_path):
    """No catalogue file means no catalogue layer, exactly as before it existed.

    A wheel install carries no ``docs/`` tree, so the located path does not
    exist; resolution must then hold the values a higher layer supplied, with
    their own provenance, rather than fail or empty them.
    """
    host = write(
        tmp_path / "host" / "flight.yaml",
        "default_backend: codex\n"
        "backends:\n"
        "  codex:\n"
        "    launch: cli\n"
        "    command: codex\n"
        "    model: gpt-host-pinned\n",
    )

    resolved = resolve(
        host_path=host,
        project_path=tmp_path / "project" / "flight.yaml",
        catalogue_path=tmp_path / "absent" / "model-catalogue.yaml",
    )

    codex = resolved.config["backends"]["codex"]
    assert codex["model"] == "gpt-host-pinned"
    assert resolved.origin("backends.codex.model") == "host"
    # Nothing the catalogue would have filled arrived from anywhere.
    assert "alias" not in codex
    assert resolved.shadows == []


def test_environment_override_locates_the_catalogue(tmp_path, monkeypatch):
    """``RECKON_MODEL_CATALOGUE`` relocates the file for tests and odd installs."""
    custom = write(tmp_path / "custom-catalogue.yaml", "version: 1\nbackends: {}\n")
    monkeypatch.setenv("RECKON_MODEL_CATALOGUE", str(custom))

    assert model_catalogue_path() == custom
