"""A cached serving observation that is absent or stale is unknown, not down.

A cached pick issues no request, so it holds no evidence about the lane at
all. Reading that absence as a refusal excludes a lane nothing observed, which
is the opposite of what no observation says.
"""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

from reckon.crew.picker import snapshot
from tests import test_picker as fixtures

config = fixtures.config
live_facts = fixtures.live_facts
request_node = fixtures.request_node


def _no_probe(monkeypatch):
    probe = Mock(side_effect=AssertionError("Cached picks must never probe"))
    monkeypatch.setattr(snapshot.resumption, "probe_lane_availability", probe)
    return probe


def _by_backend(request_node, config, tmp_path):
    options = snapshot.candidates(
        request_node, config, tmp_path, records=[], cached_only=True
    )
    return {candidate.backend: candidate for candidate in options}


def test_absent_cached_observation_is_unknown_and_offered(
    live_facts, monkeypatch, request_node, config, tmp_path
):
    monkeypatch.setattr(snapshot.resumption, "_read_lane_probe_cache", lambda *a: {})
    probe = _no_probe(monkeypatch)
    by = _by_backend(request_node, config, tmp_path)
    assert by["remote"].availability == "unknown"
    assert by["remote"].reasons == []
    probe.assert_not_called()


def test_expired_cached_observation_is_unknown_and_offered(
    live_facts, monkeypatch, request_node, config, tmp_path
):
    stale = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    monkeypatch.setattr(
        snapshot.resumption,
        "_read_lane_probe_cache",
        lambda *a: {"status": "unavailable", "observed_at": stale},
    )
    _no_probe(monkeypatch)
    by = _by_backend(request_node, config, tmp_path)
    assert by["remote"].availability == "unknown"
    assert by["remote"].reasons == []


def test_fresh_refused_cached_observation_still_excludes(
    live_facts, monkeypatch, request_node, config, tmp_path
):
    fresh = datetime.now(UTC).isoformat()
    monkeypatch.setattr(
        snapshot.resumption,
        "_read_lane_probe_cache",
        lambda *a: {"status": "refused", "observed_at": fresh},
    )
    _no_probe(monkeypatch)
    by = _by_backend(request_node, config, tmp_path)
    assert by["remote"].availability == "refused"
    assert by["remote"].reasons == ["availability: refused"]


def test_endpoints_document_backend_reads_its_serving_verdict(
    live_facts, monkeypatch, request_node, config, tmp_path
):
    """A lane publishing its endpoints is served when it lists the model."""
    document = tmp_path / "endpoints.json"
    document.write_text(
        json.dumps({"endpoints": [{"model_id": config["backends"]["local"]["model"]}]})
    )
    config["backends"]["local"]["endpoints_document"] = str(document)
    monkeypatch.setattr(snapshot.resumption, "_read_lane_probe_cache", lambda *a: {})
    _no_probe(monkeypatch)
    by = _by_backend(request_node, config, tmp_path)
    assert by["local"].availability == "served"
    assert by["local"].reasons == []
