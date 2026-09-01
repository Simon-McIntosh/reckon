from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

import pytest

from reckon import serve
from reckon.mcp_views import ready_set_view
from reckon.roadmap import build_roadmap


def _plan(
    slug: str, *, dependency: str | None = None, failing_gate: bool = False
) -> str:
    dependency_meta = (
        f'<meta name="plan-depends-on" content="{dependency}">' if dependency else ""
    )
    gate = (
        '<div class="r-gate" data-id="s3-evidence" data-section="s3" '
        f'data-status="{"open" if failing_gate else "closed"}" '
        f'data-verdict="{"" if failing_gate else "passed"}"></div>'
    )
    return (
        "<!doctype html><html><head>"
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{slug}">'
        '<meta name="plan-status" content="active">'
        '<meta name="plan-effort" content="M">'
        f"{dependency_meta}<title>{slug}</title></head><body>"
        '<main class="plan-doc"><section data-reckon="gates">'
        '<div class="r-gate" data-id="s1-evidence" data-section="s1" '
        'data-status="closed" data-verdict="passed"></div>'
        f'{gate}</section><section data-reckon="followups">'
        '<article class="r-fu" data-id="next" data-status="open">'
        '<h4 class="r-fu-title">Continue</h4><div class="r-fu-body"></div>'
        '<pre class="r-fu-prompt">/reckon-ship work</pre>'
        "</article></section></main></body></html>"
    )


def _get(port: int, path: str) -> tuple[int, dict]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def test_discovery_serves_canonical_ready_set_without_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "catalog.html").write_text(
        _plan("catalog", failing_gate=True), encoding="utf-8"
    )
    (plans / "consumer.html").write_text(
        _plan("consumer", dependency="catalog#s3"), encoding="utf-8"
    )
    config_home = tmp_path / "config"
    config_home.mkdir()
    mounts_file = config_home / "mounts.json"
    mounts_file.write_text(json.dumps({"sample": str(repo / "docs")}), encoding="utf-8")
    monkeypatch.setattr(serve, "_MOUNTS_FILE", mounts_file)
    monkeypatch.setattr(serve, "_STATE_ROOT", config_home / "state")
    serve._DISC_CACHE.clear()

    server = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, payload = _get(server.server_port, "/_discover/sample")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        serve._DISC_CACHE.clear()

    assert status == 200
    roadmap = build_roadmap(
        "sample",
        payload["inventory"],
        payload["sprints"],
        active_sprint_id=payload["active_sprint_id"],
        project_manifest=payload,
        review={},
    )
    assert payload["ready_set"] == ready_set_view(roadmap)
    consumer = next(
        row for row in payload["ready_set"]["ready"] if row["slug"] == "consumer"
    )
    assert consumer["reason"]
    assert consumer["ready_sections"] == ["s1"]
    assert consumer["blocked_sections"] == ["s3"]
    assert consumer["section_readiness"][1]["ready"] is False
    assert consumer["dependency_readiness"] == "ready"
    assert payload["ready_set"]["review"] is None
