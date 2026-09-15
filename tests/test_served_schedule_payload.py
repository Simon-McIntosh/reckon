"""The derived schedule reaches window.STATE through the served payload.

The schedule is computed once in Python and transported to the surface by the
discovery payload the shell's state loader reads. This file reads the served
payload end to end — through the HTTP handler for the primary case and through
the state loader for the surface hop — rather than asserting a function's
return value, because the defect this repairs is a value that exists in Python
and never reaches its reader.
"""

from __future__ import annotations

import http.client
import json
import os
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import serve
from reckon.roadmap import schedule_report

ROOT = Path(__file__).resolve().parents[1]
LOADER = ROOT / "docs" / "ui" / "state-loader.js"

REFERENCE = datetime(2026, 9, 4, 4, 0, tzinfo=UTC)


def _plan_html(
    slug: str,
    *,
    status: str,
    wall_clock_hours: float,
    depends_on: str | None = None,
    sprint: str | None = None,
) -> str:
    meta = [
        '<meta name="docs-project" content="sample">',
        '<meta name="reckon-type" content="plan">',
        f'<meta name="plan-slug" content="{slug}">',
        f'<meta name="plan-title" content="{slug}">',
        f'<meta name="plan-status" content="{status}">',
        f'<meta name="plan-effort-hours" content="{wall_clock_hours}">',
        f'<meta name="plan-wall-clock-hours" content="{wall_clock_hours}">',
    ]
    if depends_on:
        meta.append(f'<meta name="plan-depends-on" content="{depends_on}">')
    if sprint:
        meta.append(f'<meta name="plan-sprint" content="{sprint}">')
    return (
        "<!doctype html><html><head>"
        + "".join(meta)
        + f"<title>{slug}</title></head><body>"
        '<main class="plan-doc"></main></body></html>'
    )


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.com",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _write_schedule_fixture(repo: Path) -> None:
    """The five-plan surface fixture, with the recorded plans committed at
    backdated stamps so the served schedule keeps them inside the retention
    window at deterministic offsets. The active and pending plans stay
    untracked: their positions come from status and the chain, not from an
    edited stamp."""
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    now = datetime.now(tz=UTC)
    bodies = {
        "recorded-early": _plan_html(
            "recorded-early", status="shipped", wall_clock_hours=8, sprint="alpha"
        ),
        "recorded-wide": _plan_html(
            "recorded-wide", status="shipped", wall_clock_hours=32, sprint="alpha"
        ),
        "active-work": _plan_html(
            "active-work", status="active", wall_clock_hours=8, sprint="beta"
        ),
        "pending-first": _plan_html(
            "pending-first",
            status="pending",
            wall_clock_hours=14,
            depends_on="active-work",
            sprint="beta",
        ),
        "pending-second": _plan_html(
            "pending-second",
            status="draft",
            wall_clock_hours=12,
            depends_on="pending-first",
            sprint="gamma",
        ),
    }
    for slug, body in bodies.items():
        (plans / f"{slug}.html").write_text(body, encoding="utf-8")

    recorded = {
        "recorded-early": now - timedelta(hours=30),
        "recorded-wide": now - timedelta(hours=10),
    }
    _git(repo, "init", "-q")
    for slug, stamp in recorded.items():
        _git(
            repo,
            "add",
            f"docs/plans/{slug}.html",
        )
        _git(
            repo,
            "commit",
            "-q",
            "--date",
            stamp.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "-m",
            f"record {slug}",
        )
        os.utime(plans / f"{slug}.html", (stamp.timestamp(), stamp.timestamp()))


def _get(port: int, path: str) -> tuple[int, dict]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def test_discovery_serves_the_derived_schedule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _write_schedule_fixture(repo)
    config_home = tmp_path / "config"
    config_home.mkdir()
    mounts_file = config_home / "mounts.json"
    mounts_file.write_text(json.dumps({"sample": str(repo / "docs")}), encoding="utf-8")
    monkeypatch.setattr(serve, "_MOUNTS_FILE", mounts_file)
    monkeypatch.setattr(serve, "_STATE_ROOT", config_home / "state")
    monkeypatch.setattr(serve.crew, "list_live", lambda **_kwargs: [])
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
    served = payload["schedule"]
    assert served is not None

    # The served schedule is exactly the pure derivation at the reference the
    # payload declares — nothing recomputed by a second surface.
    reference = datetime.fromisoformat(served["reference"])
    assert served == schedule_report(
        "sample", payload["inventory"], [], reference=reference
    )

    # The bar list the surface's flow figure reads: bars, low and high, with
    # the chained column packing to two best-fit lanes. The recorded bars are
    # anchored by their backdated git author stamps, but the server emits
    # ``edited`` in host-local time, so both recorded bars shift by the same
    # host offset. Their widths, their separation, and their past-ness are
    # timezone-independent, so those are asserted — never an absolute offset
    # that would encode the test host's clock. The active and pending bars
    # are exact because their positions come from status and the chain, not
    # from an edited stamp.
    assert served["lane_count"] == 2
    assert [bar["slug"] for bar in served["bars"]] == [
        "recorded-wide",
        "recorded-early",
        "active-work",
        "pending-first",
        "pending-second",
    ]
    by_slug = {bar["slug"]: bar for bar in served["bars"]}
    early, wide = by_slug["recorded-early"], by_slug["recorded-wide"]
    assert early["end"] - early["start"] == pytest.approx(8, abs=0.01)
    assert wide["end"] - wide["start"] == pytest.approx(32, abs=0.01)
    assert wide["start"] < early["start"] < 0 and early["end"] < wide["end"] < 0
    assert early["end"] == pytest.approx(wide["end"] - 20, abs=0.02)
    active = by_slug["active-work"]
    assert active["start"] == pytest.approx(0, abs=0.02)
    assert active["end"] == pytest.approx(8, abs=0.02)
    assert by_slug["pending-first"]["start"] == pytest.approx(active["end"], abs=0.001)
    assert by_slug["pending-first"]["end"] == pytest.approx(22, abs=0.02)
    assert by_slug["pending-second"]["start"] == pytest.approx(
        by_slug["pending-first"]["end"], abs=0.001
    )
    assert by_slug["pending-second"]["end"] == pytest.approx(34, abs=0.02)
    for bar in served["bars"]:
        assert {"slug", "plan", "start", "end", "wall_hours"} <= set(bar)
        assert bar["plan"]["slug"]
        assert bar["plan"]["title"]
    assert served["low"] <= 0 <= served["high"]
    assert served["high"] == max(24.0, served["latest_end_hours"])


def test_attach_schedule_uses_the_live_runs_and_the_given_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = {
        "inventory": [
            {
                "slug": "active-work",
                "title": "Active work",
                "type": "plan",
                "status": "active",
                "wall_clock_hours": 8,
                "sprint": "beta",
            }
        ],
        "sprints": [],
        "milestones": [],
    }

    def _one_run_row(_mounts, _project):
        return [
            {
                "run_id": "run-live",
                "project": "sample",
                "plan": "active-work",
                "dispatched_at": "2026-09-04T00:00:00Z",
            }
        ]

    monkeypatch.setattr(serve, "_crew_rows", _one_run_row)
    unused_mount = tmp_path / "unused"

    payload = serve._attach_schedule(
        result, "sample", {"sample": unused_mount}, reference=REFERENCE
    )
    assert payload["schedule"]["reference"] == "2026-09-04T04:00:00+00:00"

    # The live run's dispatch reaches the derivation: the active bar starts at
    # the dispatch, 4 hours before the reference, rather than at the reference.
    active = next(
        bar for bar in payload["schedule"]["bars"] if bar["slug"] == "active-work"
    )
    assert active["start"] == -4
    assert active["end"] == 4

    # A different explicit reference re-anchors the bar; the schedule is a
    # function of the supplied instant, never of whatever the clock reads.
    raised = serve._attach_schedule(
        result,
        "sample",
        {"sample": unused_mount},
        reference=REFERENCE + timedelta(hours=6),
    )
    moved = next(
        bar for bar in raised["schedule"]["bars"] if bar["slug"] == "active-work"
    )
    assert moved["start"] == -10
    assert moved["end"] == -2


def _load_discovery_state(payload: dict) -> dict:
    script = f"""
const fs = require("fs");
global.window = {{ location: {{ pathname: "/sample/" }} }};
global.document = {{ querySelector: () => ({{ content: "sample" }}) }};
const discovery = {json.dumps(payload)};
global.fetch = async (url) => {{
  if (url === "state/sample/projection.json") {{
    return {{ ok: false, status: 404, json: async () => ({{}}) }};
  }}
  if (url === "state/sample/index.json") {{
    return {{ ok: true, status: 200, json: async () => ({{ data: {{}} }}) }};
  }}
  if (url === "/_discover/sample") {{
    return {{ ok: true, status: 200, json: async () => discovery }};
  }}
  throw new Error("unexpected fetch " + url);
}};
eval(fs.readFileSync({json.dumps(str(LOADER))}, "utf8"));
window.STATE_READY.then(() => console.log(JSON.stringify(window.STATE)));
"""
    result = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True
    )
    return json.loads(result.stdout)


def test_state_loader_carries_the_served_schedule_to_window_state() -> None:
    served = {
        "reference": "2026-09-04T04:00:00+00:00",
        "far_end_hours": 30.0,
        "latest_end_hours": 30.0,
        "lane_count": 2,
        "item_count": 5,
        "low": -42.0,
        "high": 30.0,
        "bars": [
            {
                "slug": "pending-second",
                "plan": {"slug": "pending-second", "title": "Pending second"},
                "start": 18.0,
                "end": 30.0,
                "wall_hours": 12.0,
            }
        ],
    }
    state = _load_discovery_state(
        {
            "inventory": [],
            "sprints": [],
            "milestones": [],
            "active_sprint_id": None,
            "source_format": "distributed",
            "schedule": served,
        }
    )
    assert state["schedule"] == served
    assert state["schedule"]["bars"][0]["slug"] == "pending-second"
