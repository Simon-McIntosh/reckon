"""A quota reading served from the on-disk account cache carries its fetch age.

The client keeps a copy of the account block on disk. That copy is a
last-known position rather than a heartbeat, so a figure read from it and
shown alone reads as a freshly measured one. Every case here asserts one half
of that requirement:

* the age travels as a field beside the figure, so a caller reaches it without
  parsing prose, and it agrees with the copy's own fetch stamp;
* an unresolvable fetch stamp leaves the reading unknown, with no figure
  rendered anywhere in the block;
* a live reading is untouched, so the age marks provenance of a fallback
  rather than decorating every reading.

The cache fixture is written at a path the environment resolves, and the real
path is asserted untouched afterwards, because an isolated read does not prove
an isolated write.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import _backends, _store

CLAUDE = {"launch": "cli", "command": "claude"}

FIVE_HOURS = 5 * 3600


def _account_payload(utilisation: float, *, stamp: object) -> dict:
    """The account block's shape plus the one key that dates the copy."""
    return {
        "windows": {
            "weekly": {
                "utilization": utilisation,
                "resetsAt": 1789416000,
                "windowMinutes": 10080,
            },
        },
        "fetch_stamp": stamp,
    }


def _figures(block: object) -> list[dict]:
    """Every structure in an emitted block that holds a utilisation figure."""
    found: list[dict] = []
    stack: list[object] = [block]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if node.get("utilisation_pct") is not None:
                found.append(node)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return found


def _cache_figures_without_age(block: object) -> list[dict]:
    """A cache-sourced figure that does not carry its age in the same structure.

    The stamp is the copy's own marker, so only a structure carrying it is a
    cache-sourced reading. A live reading legitimately shows a figure with no
    cache age and is not flagged.
    """
    return [
        node
        for node in _figures(block)
        if _backends.ACCOUNT_CACHE_STAMP in node and "fetch_age_seconds" not in node
    ]


def _env_resolved_cache(tmp_path: Path, monkeypatch, payload: dict) -> Path:
    """Write a cache fixture at a path the environment resolves to a temp dir.

    Redirecting the environment-resolved config home keeps the read inside
    tmp_path; the caller asserts the real path is untouched.
    """
    isolated = tmp_path / "isolated-config"
    isolated.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(isolated))
    cache = _store._config_home() / "crew" / "lane-probes" / "proj" / "metered.json"
    assert cache.is_relative_to(tmp_path.resolve())
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps(payload))
    return cache


def test_the_cache_reader_carries_the_fetch_age_beside_the_figure_it_returns(
    tmp_path, monkeypatch
):
    """The age is a field of the reading, not a sentence about it.

    A caller reading ``utilisation_pct`` reaches the age in the same block,
    without parsing the detail string, and the field agrees with the copy's own
    stamp — so a renderer cannot show the figure while losing the age.
    """
    now = datetime(2030, 1, 1, tzinfo=UTC)
    stamp = now - timedelta(seconds=FIVE_HOURS)
    cache = _env_resolved_cache(
        tmp_path, monkeypatch, _account_payload(0.53, stamp=int(stamp.timestamp()))
    )

    block = _backends.cached_account_budget(path=cache, now=now)

    assert block["headroom"] == "known"
    assert block["utilisation_pct"] == 53.0
    # Reachable without parsing prose, and consistent with the stamp it came from.
    assert block["fetch_age_seconds"] == FIVE_HOURS
    recorded = datetime.fromisoformat(block[_backends.ACCOUNT_CACHE_STAMP])
    assert recorded + timedelta(seconds=block["fetch_age_seconds"]) == now
    assert "5h00m" in block["detail"]
    assert not _cache_figures_without_age(block)


def test_the_probe_entry_point_serves_the_cached_age_and_not_only_the_figure(
    tmp_path, monkeypatch
):
    """The entry point a pre-flight reads carries the same age as the reader.

    When the live surface yields nothing, the figure a caller acts on is the
    cached one, so the age must be present there too rather than only at the
    reader the entry point delegates to.
    """
    now = datetime(2030, 1, 1, tzinfo=UTC)
    stamp = now - timedelta(seconds=FIVE_HOURS)
    cache = _env_resolved_cache(
        tmp_path, monkeypatch, _account_payload(0.53, stamp=int(stamp.timestamp()))
    )

    def _unavailable(oauth):
        raise OSError("account surface unreachable")

    block = _backends.probe_budget(
        backend_name="metered",
        backend=CLAUDE,
        fetch=_unavailable,
        now=now,
        cache_path=cache,
    )

    assert block["headroom"] == "known"
    assert block["utilisation_pct"] == 53.0
    assert block["fetch_age_seconds"] == FIVE_HOURS
    assert _backends.ACCOUNT_CACHE_STAMP in block
    assert not _cache_figures_without_age(block)


@pytest.mark.parametrize(
    "stamp",
    [None, "monday-ish", [0.53], True, "2030-01-01T00:00:00"],
)
def test_an_unresolvable_fetch_stamp_leaves_the_reading_unknown_with_no_figure(
    tmp_path, monkeypatch, stamp
):
    """No trusted fetch moment means no figure is rendered at all.

    Each shape that cannot be dated — absent, unparseable, wrongly typed, or
    carrying no zone — resolves to unknown. The block is asserted to hold no
    utilisation figure anywhere and no prose percentage, so an ungated copy
    cannot be shown bare by any caller.
    """
    now = datetime(2030, 1, 1, 0, 0, 30, tzinfo=UTC)
    cache = _env_resolved_cache(
        tmp_path, monkeypatch, _account_payload(0.53, stamp=stamp)
    )

    block = _backends.cached_account_budget(path=cache, now=now)

    assert block["headroom"] == "unknown"
    assert block["utilisation_pct"] is None
    assert block["resets_at"] is None
    assert "fetch_age_seconds" not in block
    assert _backends.ACCOUNT_CACHE_STAMP not in block
    assert _figures(block) == []
    assert "%" not in block["detail"]
    assert _backends.ACCOUNT_CACHE_STAMP in block["detail"]


def test_a_live_reading_is_untouched_by_the_cache_fallback(tmp_path, monkeypatch):
    """The change is not a blanket suppression of figures.

    A live account-surface answer wins outright even with a cache supplied, and
    carries no cache age: the age marks provenance of a fallback rather than
    decorating every reading.
    """
    now = datetime(2030, 1, 1, tzinfo=UTC)
    stamp = now - timedelta(seconds=FIVE_HOURS)
    cache = _env_resolved_cache(
        tmp_path, monkeypatch, _account_payload(0.53, stamp=int(stamp.timestamp()))
    )
    live = {
        "windows": {
            "weekly": {
                "utilization": 0.89,
                "resetsAt": 1789416000,
                "windowMinutes": 10080,
            },
        },
    }

    block = _backends.probe_budget(
        backend_name="metered",
        backend=CLAUDE,
        fetch=lambda oauth: live,
        now=now,
        cache_path=cache,
    )

    assert block["headroom"] == "known"
    assert block["utilisation_pct"] == 89.0
    assert "fetch_age_seconds" not in block
    assert _backends.ACCOUNT_CACHE_STAMP not in block
    assert not _cache_figures_without_age(block)


def test_the_cache_read_is_confined_to_the_temp_path_leaving_the_real_one_untouched(
    tmp_path, monkeypatch
):
    """An isolated read does not prove an isolated write.

    The real config home is given its own cache copy before the read; the read
    is served entirely from the environment-resolved temp path, and the real
    copy is byte-identical afterwards.
    """
    fallback_home = tmp_path / "fallback-home"
    monkeypatch.setenv("HOME", str(fallback_home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("RECKON_HOME", raising=False)
    real_cache = (
        fallback_home
        / ".config"
        / "reckon"
        / "crew"
        / "lane-probes"
        / "proj"
        / "metered.json"
    )
    real_cache.parent.mkdir(parents=True)
    sentinel = _account_payload(0.97, stamp="2029-12-31T19:00:00+00:00")
    real_cache.write_text(json.dumps(sentinel))
    before = real_cache.read_bytes()

    now = datetime(2030, 1, 1, tzinfo=UTC)
    cache = _env_resolved_cache(
        tmp_path, monkeypatch, _account_payload(0.53, stamp=int((now - timedelta(hours=5)).timestamp()))
    )
    assert cache != real_cache

    block = _backends.cached_account_budget(path=cache, now=now)

    assert block["utilisation_pct"] == 53.0
    assert block["fetch_age_seconds"] == FIVE_HOURS
    assert real_cache.read_bytes() == before