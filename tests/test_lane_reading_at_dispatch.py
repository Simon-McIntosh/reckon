"""A dispatch carries the lane's published reading as advisory data, refusing nothing."""

from __future__ import annotations

import copy
import importlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from tests import test_dispatch_names_its_backend as existing_backend_tests

dispatch_module = importlib.import_module("reckon.crew.dispatch")

pytest_plugins = ("tests.test_dispatch_names_its_backend",)

NOW = datetime(2026, 9, 14, 16, 30, 0, tzinfo=UTC)
OBSERVED_STAMP = "2026-09-14T16:29:30Z"


def _invoke(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    node: str,
    lane_document: Path | str | None,
):
    config = copy.deepcopy(existing_backend_tests.CONFIG)
    if lane_document is not None:
        config["backends"]["beta"]["lane_document"] = str(lane_document)
    monkeypatch.setattr(
        cli_module, "_resolved_flight", lambda *_args, **_kwargs: config
    )
    monkeypatch.setattr(
        cli_module, "_model_availability_refusal", lambda *_args, **_kwargs: None
    )
    result = CliRunner().invoke(
        cli_module.main,
        [
            *existing_backend_tests._arguments(repo, node=node),
            "--backend",
            "beta",
        ],
    )
    return existing_backend_tests._payload(result), result


def _field_values(carry: dict) -> dict:
    return {
        key: carry[key]
        for key in ("headroom", "binding_observed", "mean_context", "observed_at")
    }


def test_payload_carries_the_four_reading_fields_from_the_lane_document(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stamp = datetime.now(UTC).isoformat()
    lane_path = dispatch_repo.parent / "lane-reading.json"
    lane_path.write_text(
        json.dumps(
            {
                "headroom": 16.0,
                "binding_observed": "weekly scoped",
                "mean_context": 73121.0,
                "observed_at": stamp,
                "suggested_shelf_life_seconds": 60.0,
            }
        ),
        encoding="utf-8",
    )
    payload, result = _invoke(
        dispatch_repo, monkeypatch, node="carries-the-reading", lane_document=lane_path
    )

    assert result.exit_code == 0
    reading = payload["lane_reading"]
    assert reading["state"] == "fresh"
    assert _field_values(reading) == {
        "headroom": 16.0,
        "binding_observed": "weekly scoped",
        "mean_context": 73121.0,
        "observed_at": stamp,
    }


def test_absent_document_yields_unknown_rather_than_zero_or_an_error() -> None:
    carry = dispatch_module._lane_reading_carry(None, now=NOW)
    assert carry["state"] == "unknown"
    assert _field_values(carry) == {
        "headroom": "unknown",
        "binding_observed": "unknown",
        "mean_context": "unknown",
        "observed_at": None,
    }
    assert carry["headroom"] not in (0, 0.0)
    assert "no lane document" in carry["detail"]


def test_absent_declaration_dispatches_with_an_unknown_reading(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload, result = _invoke(
        dispatch_repo, monkeypatch, node="absent-declaration", lane_document=None
    )

    assert result.exit_code == 0
    reading = payload["lane_reading"]
    assert reading["state"] == "unknown"
    assert reading["headroom"] == "unknown"


def test_unreadable_document_yields_unknown_naming_the_reason(
    tmp_path: Path,
) -> None:
    missing = dispatch_module._dispatch_lane_reading(
        {"lane_document": str(tmp_path / "absent.json")}
    )
    assert missing["state"] == "unknown"
    assert missing["headroom"] == "unknown"
    assert "cannot be read" in missing["detail"]


def test_malformed_document_yields_unknown_naming_the_reason(
    tmp_path: Path,
) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    carry = dispatch_module._dispatch_lane_reading({"lane_document": str(broken)})
    assert carry["state"] == "unknown"
    assert "not valid JSON" in carry["detail"]
    assert carry["headroom"] not in (0, 0.0)


def test_a_missing_or_non_numeric_field_never_resolves_to_zero() -> None:
    base = {
        "binding_observed": "weekly scoped",
        "mean_context": 73121.0,
        "observed_at": OBSERVED_STAMP,
    }
    missing = dispatch_module._lane_reading_carry({**base}, now=NOW)
    assert missing["state"] == "unknown"
    assert "headroom" in missing["detail"]
    assert missing["headroom"] not in (0, 0.0)

    non_numeric = dispatch_module._lane_reading_carry(
        {**base, "headroom": "ample"}, now=NOW
    )
    assert non_numeric["state"] == "unknown"
    assert "headroom" in non_numeric["detail"]
    assert non_numeric["headroom"] not in (0, 0.0)

    no_mean_context = {
        "headroom": 16.0,
        "binding_observed": "weekly scoped",
        "observed_at": OBSERVED_STAMP,
    }
    mean_missing = dispatch_module._lane_reading_carry(no_mean_context, now=NOW)
    assert mean_missing["state"] == "unknown"
    assert "mean_context" in mean_missing["detail"]
    assert mean_missing["mean_context"] not in (0, 0.0)


def test_stale_reading_yields_unknown_with_its_age_stated() -> None:
    doc = {
        "headroom": 122.0,
        "binding_observed": "five hour",
        "mean_context": 15586.0,
        "observed_at": "2026-09-14T16:26:40Z",
        "suggested_shelf_life_seconds": 60.0,
    }
    carry = dispatch_module._lane_reading_carry(doc, now=NOW)

    assert carry["state"] == "unknown"
    assert carry["headroom"] == "unknown"
    assert carry["headroom"] != 122.0
    assert carry["age_seconds"] == 200
    assert "200s old" in carry["detail"]
    assert "60" in carry["detail"]


def test_binding_observed_is_consumed_as_the_documents_own_field() -> None:
    base = {
        "headroom": 16.0,
        "mean_context": 73121.0,
        "observed_at": OBSERVED_STAMP,
        "suggested_shelf_life_seconds": 60.0,
    }
    scoped = dispatch_module._lane_reading_carry(
        {**base, "binding_observed": "weekly scoped"}, now=NOW
    )
    five_hour = dispatch_module._lane_reading_carry(
        {**base, "binding_observed": "five hour"}, now=NOW
    )

    assert scoped["state"] == five_hour["state"] == "fresh"
    assert scoped["binding_observed"] == "weekly scoped"
    assert five_hour["binding_observed"] == "five hour"


def test_a_lane_naming_no_binding_constraint_still_reports_its_figures() -> None:
    """A null binding constraint withholds one field, not the whole reading.

    The field names WHICH constraint binds. A locally served lane has no window
    to name and publishes null permanently, so treating that as an unreadable
    document discards a headroom and a mean context the lane did measure. Read
    live on 2026-09-23: every dispatch carried 'unknown' while the document
    beside it reported a headroom of 0 against 32 running.
    """
    base = {
        "headroom": 0.0,
        "mean_context": 98750.0,
        "observed_at": OBSERVED_STAMP,
        "suggested_shelf_life_seconds": 60.0,
    }

    for absent in (None, "", "   "):
        carry = dispatch_module._lane_reading_carry(
            {**base, "binding_observed": absent}, now=NOW
        )
        assert carry["state"] == "fresh", absent
        assert carry["headroom"] == 0.0, absent
        assert carry["mean_context"] == 98750.0, absent
        assert carry["binding_observed"] == "unknown", absent
        assert carry["detail"] == "", absent

    missing = dispatch_module._lane_reading_carry(base, now=NOW)
    assert missing["state"] == "fresh"
    assert missing["headroom"] == 0.0
    assert missing["binding_observed"] == "unknown"


def test_no_dispatch_is_refused_held_or_rerouted_by_any_value_in_the_document(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stamp = datetime.now(UTC).isoformat()
    ample = dispatch_repo.parent / "lane-ample.json"
    ample.write_text(
        json.dumps(
            {
                "headroom": 122.0,
                "binding_observed": "five hour",
                "mean_context": 15586.0,
                "observed_at": stamp,
                "suggested_shelf_life_seconds": 60.0,
            }
        ),
        encoding="utf-8",
    )
    zero = dispatch_repo.parent / "lane-zero.json"
    zero.write_text(
        json.dumps(
            {
                "headroom": 0.0,
                "binding_observed": "weekly scoped",
                "mean_context": 73121.0,
                "observed_at": stamp,
                "suggested_shelf_life_seconds": 60.0,
            }
        ),
        encoding="utf-8",
    )

    ample_payload, ample_result = _invoke(
        dispatch_repo, monkeypatch, node="reading-ample", lane_document=ample
    )
    zero_payload, zero_result = _invoke(
        dispatch_repo, monkeypatch, node="reading-zero", lane_document=zero
    )
    absent_payload, absent_result = _invoke(
        dispatch_repo, monkeypatch, node="reading-absent", lane_document=None
    )

    for result in (ample_result, zero_result, absent_result):
        assert result.exit_code == 0
    for payload in (ample_payload, zero_payload, absent_payload):
        assert payload["backend"] == "beta"
        assert payload["requested_backend"] == "beta"
        assert payload["validation"]["ok"] is True
    assert ample_payload["lane_reading"]["headroom"] == 122.0
    assert zero_payload["lane_reading"]["headroom"] == 0.0
    assert absent_payload["lane_reading"]["state"] == "unknown"
