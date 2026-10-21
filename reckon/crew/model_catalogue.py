"""Compare declared lane models with public and subscription model lists."""

from __future__ import annotations

import fcntl
import hashlib
import json
import subprocess
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.request import urlopen

from reckon import flight
from reckon._store import write_json_atomically
from reckon._timestamps import parse_utc
from reckon.crew import obligation_snapshot

# The probe is one headless request through the lane's own command. Its prompt
# is one line, and its bound is short because a refused model name fails fast
# and a lane that hangs must not hold the refresh.
PROBE_PROMPT = "Reply with the single word ok."
PROBE_TIMEOUT_SECONDS = 60.0


def _root() -> Path:
    """The catalogue's own directory, beside the snapshot it publishes.

    Derived from the snapshot path rather than recomputed, so the snapshot,
    the error record and the refresh lock cannot land in different homes.
    """
    return snapshot_path().parent


def snapshot_path() -> Path:
    """Where the catalogue snapshot lives, owned by the snapshot reader.

    The coordinator hook reads this snapshot without importing this module, so
    the path is defined once, in :mod:`reckon.crew.obligation_snapshot`, and
    read from there by both.
    """
    return obligation_snapshot.model_catalogue_snapshot_path()


def _read_snapshot() -> dict[str, Any]:
    """The published snapshot as the reader reads it, or an empty mapping."""
    return obligation_snapshot.read_model_catalogue_snapshot()


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
        parsed = parse_utc(value)
        return None if parsed is None else parsed.timestamp()
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
    probe_run: Callable[[list[str]], tuple[int, str]] | None = None,
) -> dict[str, Any]:
    """Refresh the snapshot when due and raise each newly observed notice once.

    One producer fetches however many projects are watched: the snapshot's own
    age is re-read under an exclusive lock, so a second producer sweeping at the
    same instant finds the refresh already done and returns the snapshot it
    reads rather than fetching again. A fetch that raises preserves the previous
    snapshot and records the error beside it. A released notice on a lane that
    opts in and declares no served-model file is probed once, through the lane's
    own command, and the result is recorded on the notice.
    """
    now = now or datetime.now(UTC)
    config = config or flight.resolve(project, checkout_path=checkout_path).config
    settings = config.get("model_catalogue") or {}
    if not settings.get("list_url"):
        return _read_snapshot()
    with _refresh_lock() as acquired:
        if not acquired:
            # Another producer holds the lock and is fetching. Its result lands
            # on disk for the next read; this pass fetches nothing.
            return _read_snapshot()
        previous = _read_snapshot()
        checked = _created(previous.get("list_checked_at"))
        age = settings.get("refresh_age", 86400)
        due = force or checked is None or now.timestamp() - checked >= age
        if not due:
            return previous
        try:
            entries = _list_entries((fetch or _fetch_list)(settings["list_url"]))
            notices = {item["id"]: item for item in previous.get("notices", [])}
            for candidate in _compare(config, entries):
                notices.setdefault(
                    candidate["id"],
                    {
                        **candidate,
                        "first_seen": now.isoformat(),
                        "acknowledgement": None,
                    },
                )
            _probe_notices(config, notices, now=now, probe_run=probe_run)
            snapshot = {
                "checked_at": now.isoformat(),
                "list_checked_at": now.isoformat(),
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


@contextmanager
def _refresh_lock() -> Iterator[bool]:
    """Hold the catalogue refresh lock, yielding whether a fetch may proceed.

    The lock makes the refresh one fetch however many projects are watched: a
    producer that cannot take it is another's fetch already in flight, so it
    reads the snapshot on disk instead of fetching. The lock is released when
    the handle closes, including after a fetch that raised.
    """
    root = _root()
    root.mkdir(parents=True, exist_ok=True)
    handle = (root / "refresh.lock").open("a+")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _probe_model_name(catalog_id: str) -> str:
    """The model spelled the way a lane's own command accepts it.

    The provider segment -- everything up to and including the first ``/`` --
    is dropped, and each ``.`` becomes a ``-``, which is how every declared
    Claude model on this host is spelled.
    """
    _, _, tail = catalog_id.partition("/")
    return (tail or catalog_id).replace(".", "-")


def _default_probe_run(argv: list[str]) -> tuple[int, str]:
    """Run one headless request, returning its exit status and its reply."""
    completed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=PROBE_TIMEOUT_SECONDS,
        check=False,
    )
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def _probe_notices(
    config: Mapping[str, Any],
    notices: dict[str, dict[str, Any]],
    *,
    now: datetime,
    probe_run: Callable[[list[str]], tuple[int, str]] | None,
) -> None:
    """Probe each eligible released notice once and record its result.

    A notice is eligible when it is ``released``, its lane opts in with
    ``probe_new_models`` and declares no served-model file -- such a file
    already answers whether the lane serves the model -- and nothing has probed
    it yet. The result is recorded on the notice as ``served`` or ``unserved``
    with the reply's first line, and a notice carrying a result is never probed
    again.
    """
    lanes = config.get("lanes") or {}
    run = probe_run or _default_probe_run
    for notice in notices.values():
        if notice.get("kind") != "released" or notice.get("probe"):
            continue
        declaration = lanes.get(notice.get("lane")) or {}
        if not declaration.get("probe_new_models") or declaration.get("served_models"):
            continue
        command = declaration.get("command")
        if not command:
            continue
        model_name = _probe_model_name(str(notice.get("model_id") or ""))
        argv = [str(command), "-p", "--model", model_name, PROBE_PROMPT]
        try:
            status, output = run(argv)
        except Exception as exc:  # noqa: BLE001 - a failed probe is recorded as unserved
            status, output = 1, str(exc)
        first_line = next((line for line in output.splitlines() if line.strip()), "")
        notice["probe"] = {
            "result": "served" if status == 0 else "unserved",
            "reply": first_line,
            "model_name": model_name,
            "at": now.isoformat(),
        }


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
        else obligation_snapshot.read_model_catalogue_snapshot()
    )
    notices = obligation_snapshot.unacknowledged_model_notices(snapshot)
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
