"""Catalogue readers share parsing while preserving memo hand-out semantics."""

from contextlib import nullcontext
from pathlib import Path

import pytest
import yaml

from reckon import file_memo, flight, ledger


@pytest.fixture
def catalogue(tmp_path, monkeypatch):
    path = tmp_path / "catalogue.yaml"
    path.write_text("""backends:
  sample:
    model: sample-model
lanes:
  sample:
    launch: cli
    command: sample
    default_model: standard
    models:
      standard:
        model: sample-model
        alias: sample-alias
""")
    monkeypatch.setenv("RECKON_MODEL_CATALOGUE", str(path))
    monkeypatch.setenv("RECKON_FLIGHT_CONFIG", str(tmp_path / "absent.yaml"))
    file_memo.clear()
    yield path
    file_memo.clear()


def _resolve():
    return flight.resolve(
        overrides={"backends": {"sample": {"command": "sample"}}}
    ).config


@pytest.mark.parametrize("in_region", [False, True])
def test_fifty_mixed_calls_parse_once(catalogue, monkeypatch, in_region):
    parse = flight._load_yaml_unique_keys
    parsed = []
    handed_out = []
    read = flight.read_layer_file

    def counted(text, source, loader):
        if Path(source) == catalogue:
            parsed.append(source)
        return parse(text, source, loader)

    def observed(path):
        value = read(path)
        if Path(path) == catalogue:
            handed_out.append(value)
        return value

    monkeypatch.setattr(flight, "_load_yaml_unique_keys", counted)
    monkeypatch.setattr(flight, "read_layer_file", observed)
    with file_memo.read_only_region() if in_region else nullcontext():
        for index in range(50):
            caller = index % 3
            start = len(handed_out)
            if caller == 0:
                assert _resolve()["backends"]["sample"]["model"] == "sample-model"
            elif caller == 1:
                assert ledger.resolve_name("sample-alias") == ("sample", "standard")
            else:
                assert ledger.lanes_declaring_model("sample-model") == ["sample"]
            assert len(handed_out) > start
        # All three named callers consume a shared read-only layer view inside
        # a region, and independent mutable layer copies outside it. Their
        # public results remain a resolved config, a pair and a fresh list.
        assert len({id(value) for value in handed_out}) == (
            1 if in_region else len(handed_out)
        )
        if in_region:
            with pytest.raises(TypeError):
                handed_out[0]["lanes"]["sample"]["models"]["standard"]["model"] = (
                    "poison"
                )
        else:
            assert type(handed_out[0]) is dict
    assert len(parsed) == 1


def test_rewrite_invalidates_every_reader(catalogue):
    assert ledger.resolve_name("sample-alias") == ("sample", "standard")
    assert _resolve()["backends"]["sample"]["model"] == "sample-model"
    data = yaml.safe_load(catalogue.read_text())
    data["lanes"]["sample"]["models"]["standard"]["model"] = "replacement-model"
    data["backends"]["sample"]["model"] = "replacement-model"
    catalogue.write_text(yaml.safe_dump(data))
    assert _resolve()["backends"]["sample"]["model"] == "replacement-model"
    assert ledger.resolve_name("replacement-model") == ("sample", "standard")
    assert ledger.lanes_declaring_model("sample-model") == []
    assert ledger.lanes_declaring_model("replacement-model") == ["sample"]


def test_mutating_a_handout_never_changes_the_memo(catalogue):
    for _ in range(2):
        value = flight.read_layer_file(catalogue)
        value["lanes"]["sample"]["models"]["standard"]["model"] = "poison"
        assert (
            flight.read_layer_file(catalogue)["lanes"]["sample"]["models"]["standard"][
                "model"
            ]
            == "sample-model"
        )
        assert ledger.resolve_name("poison") == (None, None)
        assert ledger.lanes_declaring_model("sample-model") == ["sample"]
        assert _resolve()["backends"]["sample"]["model"] == "sample-model"


def test_deleted_catalogue_keeps_missing_file_answers(catalogue):
    assert ledger.resolve_name("sample-alias") == ("sample", "standard")
    catalogue.unlink()
    assert flight.read_layer_file(catalogue) == {}
    assert ledger.resolve_name("sample-alias") == (None, None)
    assert ledger.lanes_declaring_model("sample-model") == []
    assert "model" not in _resolve()["backends"]["sample"]
