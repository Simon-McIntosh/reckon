"""Compare declared lane models with public and subscription model lists."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.request import urlopen

from reckon import flight
from reckon._store import write_json_atomically
from reckon.crew.runs import crew_home


def _root() -> Path:
    return crew_home() / "model-catalogue"


def snapshot_path() -> Path:
    return _root() / "snapshot.json"


def _read_snapshot() -> dict[str, Any]:
    try:
        value = json.loads(snapshot_path().read_text())
    except FileNotFoundError:
        return {}
    return value if isinstance(value, dict) else {}


def _fetch_list(url: str) -> dict[str, Any]:
    with urlopen(url, timeout=10) as response:  # noqa: S310 - operator URL
        value = json.load(response)
    if not isinstance(value, dict) or not isinstance(value.get("data"), list):
        raise TypeError("public model list has no data array")
    return value


def _created(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            return None
    return None


def _served(path: str | None) -> tuple[set[str], set[str], str]:
    if not path:
        return set(), set(), "unknown"
    try:
        document = json.loads(Path(path).expanduser().read_text())
        entries = document["models"]
    except (OSError, ValueError, KeyError, TypeError):
        return set(), set(), "unknown"
    if not isinstance(entries, list):
        return set(), set(), "unknown"
    all_slugs = {
        entry["slug"]
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("slug"), str)
    }
    visible = {
        entry["slug"]
        for entry in entries
        if isinstance(entry, dict)
        and isinstance(entry.get("slug"), str)
        and entry.get("visibility") != "hide"
    }
    return all_slugs, visible, "known"


def _notice(kind: str, lane: str, model_id: str, **details: Any) -> dict[str, Any]:
    identity = f"{kind}:{lane}:{model_id}"
    return {
        "id": hashlib.sha256(identity.encode()).hexdigest()[:20],
        "kind": kind,
        "lane": lane,
        "model_id": model_id,
        **details,
    }


def _compare(
    config: Mapping[str, Any], entries: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    lanes = config.get("lanes") or {}
    rules = (config.get("model_catalogue") or {}).get("watch") or {}
    indexed = {
        entry["id"]: entry for entry in entries if isinstance(entry.get("id"), str)
    }
    notices: list[dict[str, Any]] = []
    for lane, declaration in lanes.items():
        models = declaration.get("models") or {}
        declared = {
            key: (model.get("catalog_id") or model.get("model") or key)
            for key, model in models.items()
        }
        slugs, visible, served_state = _served(declaration.get("served_models"))
        if served_state == "known":
            notices.extend(
                _notice("subscription-serves", lane, slug, served="listed")
                for slug in sorted(
                    visible - {value.rsplit("/", 1)[-1] for value in declared.values()}
                )
            )
            for key, catalog_id in declared.items():
                slug = catalog_id.rsplit("/", 1)[-1]
                if slug not in slugs:
                    notices.append(
                        _notice(
                            "declared-unserved",
                            lane,
                            catalog_id,
                            model_key=key,
                            served="unlisted",
                        )
                    )
        for key, catalog_id in declared.items():
            entry = indexed.get(catalog_id)
            if entry and entry.get("expiration_date"):
                notices.append(
                    _notice(
                        "expiring",
                        lane,
                        catalog_id,
                        model_key=key,
                        expiration_date=entry["expiration_date"],
                    )
                )

    matched_ids: set[str] = set()
    for prefix, rule in sorted(
        rules.items(), key=lambda item: len(item[0]), reverse=True
    ):
        lane = rule.get("lane") if isinstance(rule, dict) else rule
        declaration = lanes.get(lane) or {}
        models = declaration.get("models") or {}
        declared_ids = {
            model.get("catalog_id") or model.get("model") or key
            for key, model in models.items()
        }
        newest = max(
            (
                _created(indexed[model_id].get("created")) or 0
                for model_id in declared_ids
                if model_id in indexed and model_id.startswith(prefix)
            ),
            default=0,
        )
        slugs, _, served_state = _served(declaration.get("served_models"))
        for model_id, entry in indexed.items():
            if (
                not model_id.startswith(prefix)
                or model_id in declared_ids
                or ":" in model_id
                or model_id in matched_ids
            ):
                continue
            matched_ids.add(model_id)
            created = _created(entry.get("created"))
            if created is None or created <= newest or newest == 0:
                continue
            served = (
                "unknown"
                if served_state == "unknown"
                else ("listed" if model_id.rsplit("/", 1)[-1] in slugs else "unlisted")
            )
            notices.append(
                _notice(
                    "released",
                    lane,
                    model_id,
                    created=entry.get("created"),
                    served=served,
                    served_models_path=declaration.get("served_models"),
                )
            )
    return notices


def check_model_catalogue(
    project: str,
    *,
    checkout_path: str | None = None,
    config: Mapping[str, Any] | None = None,
    fetch: Callable[[str], dict[str, Any]] | None = None,
    force: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Refresh the snapshot when due and raise each newly observed notice once."""
    now = now or datetime.now(UTC)
    config = config or flight.resolve(project, checkout_path=checkout_path).config
    settings = config.get("model_catalogue") or {}
    previous = _read_snapshot()
    if not settings.get("list_url"):
        return previous
    checked = _created(previous.get("list_checked_at"))
    age = settings.get("refresh_age", 86400)
    due = force or checked is None or now.timestamp() - checked >= age
    try:
        document = (
            (fetch or _fetch_list)(settings["list_url"])
            if due
            else {"data": previous["models"]}
        )
        entries = _list_entries(document)
        notices = {item["id"]: item for item in previous.get("notices", [])}
        for candidate in _compare(config, entries):
            notices.setdefault(
                candidate["id"],
                {**candidate, "first_seen": now.isoformat(), "acknowledgement": None},
            )
        snapshot = {
            "checked_at": now.isoformat(),
            "list_checked_at": now.isoformat() if due else previous["list_checked_at"],
            "models": entries,
            "notices": sorted(
                notices.values(), key=lambda item: (item["first_seen"], item["id"])
            ),
        }
        write_json_atomically(snapshot_path(), snapshot, indent=2, sort_keys=True)
        write_json_atomically(
            _root() / "error.json",
            {"at": now.isoformat(), "error": None},
            indent=2,
            sort_keys=True,
        )
        return snapshot
    except Exception as exc:  # noqa: BLE001 - a failed fetch preserves the snapshot
        write_json_atomically(
            _root() / "error.json",
            {"at": now.isoformat(), "error": str(exc)},
            indent=2,
            sort_keys=True,
        )
        return previous


def _list_entries(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    entries = document["data"]
    if not isinstance(entries, list):
        raise TypeError("public model list has no data array")
    return entries


def model_notices(
    project: str, *, refresh: bool = False, checkout_path: str | None = None
) -> dict[str, Any]:
    snapshot = (
        check_model_catalogue(project, checkout_path=checkout_path, force=True)
        if refresh
        else _read_snapshot()
    )
    notices = [
        item for item in snapshot.get("notices", []) if not item.get("acknowledgement")
    ]
    error_path = _root() / "error.json"
    error = json.loads(error_path.read_text()) if error_path.exists() else None
    if error and not error.get("error"):
        error = None
    return {
        "ok": True,
        "view": "models",
        "project": project,
        "notices": notices,
        "checked_at": snapshot.get("checked_at"),
        "error": error,
    }


def acknowledge_notice(notice_id: str, reason: str) -> dict[str, Any]:
    if not reason.strip():
        raise ValueError("notice acknowledgement requires a reason")
    snapshot = _read_snapshot()
    for notice in snapshot.get("notices", []):
        if notice["id"] == notice_id:
            notice["acknowledgement"] = {
                "reason": reason,
                "at": datetime.now(UTC).isoformat(),
            }
            write_json_atomically(snapshot_path(), snapshot, indent=2, sort_keys=True)
            return notice
    raise ValueError(f"unknown model notice {notice_id}")
