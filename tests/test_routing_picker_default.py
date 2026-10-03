"""The shipped flight default routes dispatch through the picker.

A project declaring nothing resolves ``routing.picker`` to ``route`` from the
shipped layer, so a fresh install is routed rather than only recording a
selection. A project layer may set ``shadow`` to opt out, and ``--route
deterministic`` opts out a single dispatch.
"""

from __future__ import annotations

import pytest

from reckon import flight


@pytest.fixture(autouse=True)
def isolated_host_config(monkeypatch, tmp_path):
    """Keep the machine's real host layer out of the resolved value.

    Without this the assertion would depend on whatever the workstation's host
    layer happens to declare, which is not what this test measures.
    """
    monkeypatch.setenv("RECKON_FLIGHT_CONFIG", str(tmp_path / "absent" / "flight.yaml"))


def test_a_project_declaring_nothing_routes_through_the_picker(tmp_path):
    resolved = flight.resolve(host_path=tmp_path / "absent.yaml")

    assert resolved.config["routing"]["picker"] == "route"
    assert resolved.provenance["routing.picker"] == "shipped"


def test_a_project_layer_declaring_shadow_opts_out(tmp_path):
    project_layer = tmp_path / "flight.yaml"
    project_layer.write_text("routing:\n  picker: shadow\n")

    resolved = flight.resolve(
        host_path=tmp_path / "absent.yaml",
        project_path=project_layer,
    )

    assert resolved.config["routing"]["picker"] == "shadow"
    assert resolved.provenance["routing.picker"] == "project"