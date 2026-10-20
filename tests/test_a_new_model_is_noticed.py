"""Model notices from a public list and each lane's served-model document."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from reckon import mcp, mcp_views
from reckon.cli import main
from reckon.crew import model_catalogue


@pytest.fixture()
def catalogue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from reckon.crew.runs import crew_home

    real_snapshot = model_catalogue.snapshot_path()
    real_existed = real_snapshot.exists()
    real_mtime_ns = real_snapshot.stat().st_mtime_ns if real_existed else None
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    assert crew_home().is_relative_to(tmp_path)
    assert model_catalogue.snapshot_path().is_relative_to(tmp_path)
    served = tmp_path / "served.json"
    served.write_text(
        json.dumps({"models": [{"slug": "gpt-6-sol", "visibility": "list"}]})
    )
    listing = tmp_path / "list.json"
    models = [
        {"id": "openai/gpt-6-sol", "created": 100},
        {"id": "openai/gpt-6.1-sol", "created": 200},
        {"id": "openai/gpt-6.1-sol:batch", "created": 200},
        {"id": "anthropic/claude-sonnet", "created": 100},
        {"id": "anthropic/claude-new", "created": 200},
    ]
    listing.write_text(json.dumps({"data": models}))
    config = {
        "model_catalogue": {
            "list_url": "https://example.invalid/models",
            "refresh_age": 86400,
            "watch": {"openai/gpt-": "codex", "anthropic/claude-": "claude"},
        },
        "lanes": {
            "codex": {
                "served_models": str(served),
                "models": {
                    "sol": {"model": "gpt-6-sol", "catalog_id": "openai/gpt-6-sol"}
                },
            },
            "claude": {
                "models": {
                    "sonnet": {
                        "model": "claude-sonnet",
                        "catalog_id": "anthropic/claude-sonnet",
                    }
                }
            },
        },
    }

    def read_list(_url: str):
        return json.loads(listing.read_text())

    monkeypatch.setattr(model_catalogue, "_fetch_list", read_list)
    monkeypatch.setattr(
        model_catalogue.flight,
        "resolve",
        lambda *args, **kwargs: SimpleNamespace(config=config),
    )
    yield config, listing, served, read_list
    assert real_snapshot.exists() == real_existed
    if real_existed:
        assert real_snapshot.stat().st_mtime_ns == real_mtime_ns


def _check(catalogue):
    config, _, _, read_list = catalogue
    return model_catalogue.check_model_catalogue(
        "fixture", config=config, fetch=read_list, force=True
    )


def test_released_records_lane_and_served_state_without_batch(catalogue):
    _, _, served_file, _ = catalogue
    snapshot = _check(catalogue)
    released = [item for item in snapshot["notices"] if item["kind"] == "released"]
    assert {(item["lane"], item["model_id"], item["served"]) for item in released} == {
        ("codex", "openai/gpt-6.1-sol", "unlisted"),
        ("claude", "anthropic/claude-new", "unknown"),
    }
    assert all(":batch" not in item["model_id"] for item in released)
    assert next(item for item in released if item["lane"] == "codex")[
        "served_models_path"
    ] == str(served_file)


def test_visible_undeclared_slug_and_hidden_slug(catalogue):
    _, _, served, _ = catalogue
    served.write_text(
        json.dumps(
            {
                "models": [
                    {"slug": "gpt-6-sol", "visibility": "list"},
                    {"slug": "gpt-extra", "visibility": "list"},
                    {"slug": "gpt-hidden", "visibility": "hide"},
                ]
            }
        )
    )
    snapshot = _check(catalogue)
    served_notices = [
        item["model_id"]
        for item in snapshot["notices"]
        if item["kind"] == "subscription-serves"
    ]
    assert served_notices == ["gpt-extra"]


def test_declared_unserved(catalogue):
    _, _, served, _ = catalogue
    served.write_text(json.dumps({"models": []}))
    snapshot = _check(catalogue)
    assert [
        (item["lane"], item["model_id"])
        for item in snapshot["notices"]
        if item["kind"] == "declared-unserved"
    ] == [("codex", "openai/gpt-6-sol")]


def test_declared_expiration(catalogue):
    _, listing, _, _ = catalogue
    document = json.loads(listing.read_text())
    document["data"][0]["expiration_date"] = "2026-12-01"
    listing.write_text(json.dumps(document))
    snapshot = _check(catalogue)
    expiring = [item for item in snapshot["notices"] if item["kind"] == "expiring"]
    assert [(item["model_id"], item["expiration_date"]) for item in expiring] == [
        ("openai/gpt-6-sol", "2026-12-01")
    ]


def test_acknowledged_notice_is_not_raised_again(catalogue):
    snapshot = _check(catalogue)
    notice = next(item for item in snapshot["notices"] if item["kind"] == "released")
    answer = mcp._crew(
        project="fixture", action="ack", notice_id=notice["id"], reason="reviewed"
    )
    assert answer["notice_id"] == notice["id"]
    assert answer["acknowledgement"]["reason"] == "reviewed"
    _check(catalogue)
    assert notice["id"] not in {
        item["id"]
        for item in mcp_views.serve_crew_view("models", project="fixture")["notices"]
    }


def test_failed_fetch_keeps_previous_snapshot_byte_identical(catalogue):
    config, _, _, _ = catalogue
    _check(catalogue)
    before = model_catalogue.snapshot_path().read_bytes()

    def failed(_url: str):
        raise RuntimeError("fixture fetch failed")

    snapshot = model_catalogue.check_model_catalogue(
        "fixture", config=config, fetch=failed, force=True
    )
    assert model_catalogue.snapshot_path().read_bytes() == before
    assert snapshot == json.loads(before)
    assert (
        json.loads((model_catalogue.snapshot_path().parent / "error.json").read_text())[
            "error"
        ]
        == "fixture fetch failed"
    )


def test_models_view_and_tool_refresh_recompute(catalogue):
    _, listing, _, _ = catalogue
    assert mcp_views.serve_crew_view("models", project="fixture")["notices"] == []
    refreshed = mcp_views.serve_crew_view("models", project="fixture", refresh=True)
    assert any(item["kind"] == "released" for item in refreshed["notices"])
    document = json.loads(listing.read_text())
    document["data"].append({"id": "openai/gpt-6.2-sol", "created": 300})
    listing.write_text(json.dumps(document))
    assert "openai/gpt-6.2-sol" not in {
        item["model_id"]
        for item in mcp._crew(project="fixture", view="models")["notices"]
    }
    recomputed = mcp._crew(project="fixture", view="models", refresh=True)
    assert "openai/gpt-6.2-sol" in {item["model_id"] for item in recomputed["notices"]}


def test_cli_view_refresh_and_notice_ack(catalogue):
    runner = CliRunner()
    result = runner.invoke(
        main, ["crew", "view", "models", "--project", "fixture", "--refresh"]
    )
    assert result.exit_code == 0, result.output
    notice = next(
        item
        for item in json.loads(result.output)["notices"]
        if item["kind"] == "released"
    )
    acknowledged = runner.invoke(
        main, ["crew", "ack", "--notice", notice["id"], "--reason", "reviewed"]
    )
    assert acknowledged.exit_code == 0, acknowledged.output
    assert json.loads(acknowledged.output)["notice_id"] == notice["id"]
    refused = runner.invoke(
        main,
        [
            "crew",
            "ack",
            "--notice",
            notice["id"],
            "--reason",
            "reviewed",
            "--until",
            "tomorrow",
        ],
    )
    assert refused.exit_code != 0
    assert "takes no --until" in refused.output
    for arguments in ([], ["--run", "r-fixture", "--notice", notice["id"]]):
        invalid = runner.invoke(
            main, ["crew", "ack", *arguments, "--reason", "reviewed"]
        )
        assert invalid.exit_code != 0
        assert "exactly one" in invalid.output
