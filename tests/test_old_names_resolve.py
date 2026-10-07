"""Catalogue identities reach historical record readers without rewriting rows."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from reckon import flight, ledger, mcp_views
from reckon.cli import main
from reckon.crew import query, recovery, reports

ALIASES = {
    "claude-opus": ("claude", "opus"),
    "claude-haiku": ("claude", "haiku"),
    "codex-astra": ("codex", "astra"),
    "codex-luna": ("codex", "luna"),
    "codex-terra": ("codex", "terra"),
    "codex-spark": ("codex", "spark"),
    "clive-glm": ("clive", "glm"),
}
CATALOGUE = (
    Path(flight.__file__).resolve().parent.parent
    / "docs/state/reckon/model-catalogue.yaml"
)


@pytest.fixture(autouse=True)
def catalogue(monkeypatch, tmp_path):
    # A copied catalogue keeps mutation controls isolated from committed data.
    source = Path(os.environ.get("RECKON_NAME_CATALOGUE_FIXTURE", CATALOGUE))
    path = tmp_path / "catalogue.yaml"
    path.write_bytes(source.read_bytes())
    monkeypatch.setenv("RECKON_MODEL_CATALOGUE", str(path))
    return path


def pair(row):
    return row.get("lane"), row.get("model_key")


@pytest.mark.parametrize(("name", "expected"), ALIASES.items())
def test_every_legacy_name_resolves(name, expected):
    assert ledger.resolve_name(name) == expected
    assert pair(ledger.normalize_identity({"backend": name})) == expected
    assert (
        pair(ledger.normalize_identity({"backend": expected[0], "model": expected[1]}))
        == expected
    )


def test_declared_model_ids_aliases_and_lane_defaults(catalogue):
    data = yaml.safe_load(catalogue.read_text())
    for lane, declaration in data["lanes"].items():
        assert ledger.resolve_name(lane) == (lane, declaration["default_model"])
        for key, model in declaration["models"].items():
            for name in (key, model["model"], model["alias"]):
                assert ledger.resolve_name(name) == (lane, key)


def test_unknown_name_is_not_guessed():
    assert ledger.resolve_name("claude-not-declared") == (None, None)
    row = {"backend": "custom", "model": "unlisted"}
    assert ledger.normalize_identity(row) == row


def test_bare_model_ambiguity_names_both_lanes(catalogue):
    data = yaml.safe_load(catalogue.read_text())
    data["lanes"]["secondary"] = {
        "default_model": "opus",
        "models": {"opus": data["lanes"]["claude"]["models"]["opus"]},
    }
    catalogue.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="ambiguous") as caught:
        ledger.resolve_name("opus")
    assert "claude" in str(caught.value) and "secondary" in str(caught.value)
    assert pair(ledger.normalize_identity({"backend": "claude", "model": "opus"})) == (
        "claude",
        "opus",
    )


def test_alias_removal_is_seen_without_restarting(catalogue):
    assert ledger.resolve_name("claude-opus") == ("claude", "opus")
    data = yaml.safe_load(catalogue.read_text())
    del data["aliases"]["claude-opus"]
    catalogue.write_text(yaml.safe_dump(data))
    assert ledger.resolve_name("claude-opus") == (None, None)
    assert pair(ledger.normalize_identity({"backend": "claude-opus"})) == (None, None)
    assert ledger.resolve_name("claude") == ("claude", "sonnet")


def test_recorded_pair_and_nested_agent_are_read_without_mutation():
    for row in (
        {"backend": "claude-opus"},
        {"backend": "claude", "agent": {"model": "opus"}},
        {"agent": {"backend": "claude", "model": "claude-opus-5-5"}},
        {"reviewer": {"backend": "claude", "model": "opus 5.5"}},
        {"lane": "claude", "model_key": "opus"},
    ):
        original = copy.deepcopy(row)
        assert pair(ledger.normalize_identity(row)) == ("claude", "opus")
        assert row == original


def test_unrecognised_explicit_model_does_not_invent_current_default():
    row = ledger.normalize_identity({"backend": "claude", "model": "retired-unknown"})
    assert row.get("lane") == "claude"
    assert row.get("model_key") is None


def write_history(root):
    directory = root / "docs/state/sample"
    directory.mkdir(parents=True)
    rows = [
        {"run_id": "legacy", "backend": "claude-opus"},
        {"run_id": "paired", "backend": "claude", "agent": {"model": "opus"}},
    ]
    path = directory / "crew.json"
    path.write_text(
        json.dumps(
            {
                "project": "sample",
                "doc": "crew",
                "data": {"members": [], "holds": [], "runs": rows, "_version": 1},
            }
        )
    )
    return path, rows


def test_ledger_and_cli_report_pair_and_keep_stored_bytes(tmp_path):
    path, rows = write_history(tmp_path)
    before = path.read_bytes()
    assert ledger.load("sample", tmp_path)[0]["runs"] == rows
    assert [pair(row) for row in ledger.runs("sample", tmp_path)] == [
        ("claude", "opus"),
        ("claude", "opus"),
    ]
    result = CliRunner().invoke(
        main,
        [
            "crew",
            "ledger",
            "--project",
            "sample",
            "--view",
            "records",
            "--checkout-path",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert [pair(row) for row in json.loads(result.output)["runs"]] == [
        ("claude", "opus"),
        ("claude", "opus"),
    ]
    assert path.read_bytes() == before
    assert ledger.load("sample", tmp_path)[0]["runs"] == rows


def test_runs_view_reports_pair_for_stored_rows(tmp_path):
    path, _ = write_history(tmp_path)
    before = path.read_bytes()
    for fields in (None, ["lane", "model_key"]):
        result = query.runs_view(
            "sample", checkout_path=str(tmp_path), source="ledger", fields=fields
        )
        assert result["count"] == 2
        assert all(pair(row) == ("claude", "opus") for row in result["rows"])
    assert path.read_bytes() == before


def test_live_classification_and_compact_view_report_pair(monkeypatch, tmp_path):
    pointer = {
        "run_id": "live-fixture",
        "project": "sample",
        "backend": "claude-opus",
        "node": {"id": "fixture", "plan": "sample"},
        "phase": "working",
        "process_alive": True,
        "manifest_path": str(tmp_path / "absent.md"),
    }
    assert pair(recovery.classify_pointer(pointer)) == ("claude", "opus")
    monkeypatch.setattr(query, "list_live", lambda: [pointer])
    result = query.runs_view("sample", source="live")
    assert pair(result["rows"][0]) == ("claude", "opus")
    assert pair(mcp_views._run_row(pointer)) == ("claude", "opus")


def test_review_read_normalizes_mixed_vocabulary(monkeypatch):
    row = {"reviewer": {"backend": "claude", "model": "opus 5.5"}}
    monkeypatch.setattr(recovery.review_module, "read_review", lambda *a, **k: row)
    result, stale = recovery.select_review_for_head("sample", "fixture", "abc")
    assert stale == ""
    assert pair(result) == ("claude", "opus")
    assert pair(result["reviewer"]) == ("claude", "opus")
    assert row == {"reviewer": {"backend": "claude", "model": "opus 5.5"}}


@pytest.mark.parametrize("exclusion", ["claude-opus", "claude-opus-5-5", "opus 5.5"])
def test_review_exclusions_match_pair_across_names(exclusion):
    config = {
        "backends": {
            "claude": {"model": "opus", "launch": "cli"},
            "other-model": {"lane": "claude", "model_key": "sonnet", "launch": "cli"},
        },
        "review_excluded_backends": [exclusion],
    }
    original = copy.deepcopy(config)
    assert recovery._ordered_review_lanes(config, owning_backend="claude-opus") == [
        "other-model"
    ]
    assert config == original


def test_lane_exclusion_covers_all_models_and_unknown_exclusion_still_works():
    config = {
        "backends": {"claude-opus": {}, "claude-haiku": {}, "custom": {}, "safe": {}},
        "review_excluded_backends": ["claude", "custom"],
    }
    assert recovery._ordered_review_lanes(config) == ["safe"]


def test_report_review_record_uses_shared_identity(monkeypatch, tmp_path):
    from types import SimpleNamespace

    store = tmp_path / "reviews"
    path = store / "sample" / "fixture.json"
    row = {"backend": "claude-opus"}
    node = SimpleNamespace(role="review", id="review-fixture", write_paths=[str(path)])
    monkeypatch.setattr(reports, "_node_is_a_reviewer", lambda node: True)
    monkeypatch.setattr(reports.review_module, "review_store_root", lambda: store)
    monkeypatch.setattr(reports.review_module, "stored_record", lambda *a: (path, row))
    records = reports._reviewer_store_records(node)
    assert len(records) == 1
    assert pair(records[0][2]) == ("claude", "opus")
    assert row == {"backend": "claude-opus"}


def test_lane_receipts_join_equivalent_names_but_keep_models_separate():
    rows = [
        {
            "run_id": "alias-row",
            "backend": "claude-opus",
            "session_id": "alias-session",
            "completed_at": "2030-01-01T00:00:00Z",
        },
        {
            "run_id": "paired-row",
            "backend": "claude",
            "model": "opus",
            "session_id": "paired-session",
            "completed_at": "2030-01-02T00:00:00Z",
        },
        {
            "run_id": "default-row",
            "backend": "claude",
            "session_id": "default-session",
            "completed_at": "2030-01-03T00:00:00Z",
        },
    ]
    latest = mcp_views._latest_backend_runs(rows)
    assert latest[("claude", "opus")]["session_id"] == "paired-session"
    assert latest[("claude", "sonnet")]["session_id"] == "default-session"
    assert len(latest) == 2


def test_lane_view_finds_historical_receipt_by_pair():
    result = mcp_views.crew_lanes_view(
        {"backends": {"claude": {"model": "opus"}}},
        [
            {
                "run_id": "alias-row",
                "backend": "claude-opus",
                "session_id": "alias-session",
                "completed_at": "2030-01-01T00:00:00Z",
            }
        ],
        receipt_reader=lambda session: None,
        composed_at="2030-01-02T00:00:00Z",
    )
    assert pair(result["lanes"][0]) == ("claude", "opus")
    assert result["lanes"][0]["receipt_state"] != "unused"


def test_parent_lane_disambiguates_nested_agent_model(catalogue):
    data = yaml.safe_load(catalogue.read_text())
    data["lanes"]["secondary"] = copy.deepcopy(data["lanes"]["claude"])
    catalogue.write_text(yaml.safe_dump(data))
    row = ledger.normalize_identity({"backend": "claude", "agent": {"model": "opus"}})
    assert pair(row) == pair(row["agent"]) == ("claude", "opus")


def test_explicit_model_wins_over_agent_default():
    row = ledger.normalize_identity(
        {
            "backend": "claude",
            "model": "opus",
            "agent": {"backend": "claude"},
        }
    )
    assert pair(row) == ("claude", "opus")


def test_split_run_file_stays_byte_identical_and_index_reads_current_aliases(
    tmp_path, catalogue
):
    directory = tmp_path / "docs/state/sample/runs"
    directory.mkdir(parents=True)
    path = directory / "fixture.json"
    row = {"run_id": "fixture", "backend": "claude-opus"}
    path.write_text(json.dumps(row))
    before = path.read_bytes()
    assert pair(ledger.runs("sample", tmp_path)[0]) == ("claude", "opus")
    assert ledger.load("sample", tmp_path)[0]["runs"] == [row]
    data = yaml.safe_load(catalogue.read_text())
    del data["aliases"]["claude-opus"]
    catalogue.write_text(yaml.safe_dump(data))
    assert pair(ledger.runs("sample", tmp_path)[0]) == (None, None)
    assert path.read_bytes() == before


def test_mcp_runs_surface_reports_the_resolved_pair(tmp_path):
    from reckon import mcp

    path, _ = write_history(tmp_path)
    before = path.read_bytes()
    result = mcp._crew(
        "sample",
        view="runs",
        checkout_path=str(tmp_path),
        source="ledger",
        fields=["lane", "model_key"],
    )
    assert result["ok"], result
    assert result["count"] == 2
    assert all(pair(row) == ("claude", "opus") for row in result["rows"])
    assert path.read_bytes() == before


@pytest.mark.parametrize(("name", "expected"), ALIASES.items())
def test_legacy_alias_survives_an_uncatalogued_historical_model(name, expected):
    row = {
        "backend": name,
        "agent": {"model": "historical-model-id-not-in-the-catalogue"},
    }
    assert pair(ledger.normalize_identity(row)) == expected
