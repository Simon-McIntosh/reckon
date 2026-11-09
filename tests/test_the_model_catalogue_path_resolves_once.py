"""The model catalogue's package-relative default resolves once per process."""

from __future__ import annotations

import pathlib

from reckon import flight, ledger

_READS = 1_000

_CATALOGUE_ONE = """\
version: 1
lanes:
  temp:
    default_model: first
    models:
      first:
        model: temp-model
"""

_CATALOGUE_TWO = """\
version: 1
lanes:
  temp:
    default_model: second
    models:
      second:
        model: temp-model
"""


def test_a_thousand_reads_resolve_no_path(monkeypatch):
    """After one call the default path is fixed, so repeated reads resolve none."""
    # The default path is the subject, so no override may stand in for it.
    monkeypatch.delenv("RECKON_MODEL_CATALOGUE", raising=False)
    flight.model_catalogue_path()

    resolves = 0
    original = pathlib.Path.resolve

    def counting_resolve(self, *args, **kwargs):
        nonlocal resolves
        resolves += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "resolve", counting_resolve)
    for _ in range(_READS):
        assert ledger.resolve_name("no-such-model-anywhere") == (None, None)

    assert resolves == 0, f"expected no path resolution, counted {resolves}"


def test_an_override_set_later_takes_effect(monkeypatch, tmp_path):
    """The environment override is read on every call, not cached with the default."""
    monkeypatch.delenv("RECKON_MODEL_CATALOGUE", raising=False)
    default = flight.model_catalogue_path()

    override = tmp_path / "catalogue.yaml"
    override.write_text(_CATALOGUE_ONE)
    monkeypatch.setenv("RECKON_MODEL_CATALOGUE", str(override))

    assert flight.model_catalogue_path() == override
    assert flight.model_catalogue_path() != default


def test_a_rewritten_catalogue_is_read_again(monkeypatch, tmp_path):
    """A catalogue whose file identity moved between two reads is read again."""
    catalogue = tmp_path / "catalogue.yaml"
    catalogue.write_text(_CATALOGUE_ONE)
    monkeypatch.setenv("RECKON_MODEL_CATALOGUE", str(catalogue))

    assert ledger.resolve_name("temp-model") == ("temp", "first")

    catalogue.write_text(_CATALOGUE_TWO)

    assert ledger.resolve_name("temp-model") == ("temp", "second")
