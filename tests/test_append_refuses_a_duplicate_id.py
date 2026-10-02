"""An append refuses an id its collection already holds.

Two sessions that read one plan and each choose the next sequential id collide
in content rather than in sequence: both writes are correctly version-paired
and neither raises, so the collection can hold two items that address the same
way and resolve-by-id is ambiguous by construction. The refusal names the
collection, the id and the item that already holds it, and leaves the plan file
and its version untouched, so the writer retries with a distinct id.
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

import pytest

from reckon import _plan_html, _store, mcp

PROJECT = "append-dup-fixture"
PLAN = "append-dup-target"
ANCHOR = "s1"


def _seed(root: Path) -> Path:
    path = root / "docs" / "plans" / f"{PLAN}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title></head>"
        '<body><main class="plan-doc"></main></body></html>\n'
    )
    state = {
        "type": "plan",
        "slug": PLAN,
        "title": "Append duplicate target",
        "status": "active",
        "version": 0,
        "comments": {},
    }
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")
    return path


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    _seed(root)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _version(root: Path) -> int:
    return _store.read_plan(PROJECT, PLAN, root, artifact_type="plan")[1]


def _plan_file(root: Path) -> Path:
    return root / "docs" / "plans" / f"{PLAN}.html"


def _edit(root: Path, ops: list[dict], version: int | None = None) -> dict:
    return mcp._edit_plan(
        PROJECT,
        PLAN,
        ops,
        _version(root) if version is None else version,
        checkout_path=str(root),
    )


def _followup(ident: str, title: str = "First followup") -> dict:
    return {
        "id": ident,
        "written_by": "reckon-build",
        "written_at": "2026-10-02T00:00:00Z",
        "title": title,
        "body": "<p>Body.</p>",
        "prompt": "/reckon-build append-refuses-a-duplicate-id",
    }


def _section(ident: str, title: str = "First section") -> dict:
    return {
        "id": ident,
        "title": title,
        "body": "<p>Authored prose.</p>",
        "effort_hours": 1.0,
        "capability": {
            "version": "1.0",
            "class": "general",
            "requirements": {
                "reasoning": "standard",
                "verification": "strict",
                "risk": "low",
            },
        },
        "links": [],
    }


def _comment(ident: str) -> dict:
    return {
        "id": ident,
        "who": "reckon-build",
        "when": "2026-10-02T00:00:00Z",
        "body": "<p>a duplicate comment</p>",
    }


def _append(root: Path, target: str, item: dict, **extra) -> dict:
    return _edit(root, [{"op": "append", "target": target, "item": item, **extra}])


def _assert_refused(detail: str, collection: str, ident: str, existing: str) -> None:
    assert collection in detail, detail
    assert ident in detail, detail
    assert existing in detail, detail


def test_append_same_followup_id_twice_is_refused(repository: Path) -> None:
    first = _append(repository, "followups", _followup("f-dup", "First followup"))
    assert first["ok"], first
    version_after_first = _version(repository)
    digest_after_first = hashlib.sha256(_plan_file(repository).read_bytes()).hexdigest()

    second = _append(repository, "followups", _followup("f-dup", "Second followup"))

    assert second["ok"] is False, second
    assert second["error"] == "op_error", second
    _assert_refused(second["detail"], "followup", "f-dup", "First followup")
    assert _version(repository) == version_after_first
    assert hashlib.sha256(_plan_file(repository).read_bytes()).hexdigest() == (
        digest_after_first
    )
    state, _ = _store.read_plan(PROJECT, PLAN, repository, artifact_type="plan")
    held = [f for f in state.get("followups", []) if f.get("id") == "f-dup"]
    assert len(held) == 1


def test_append_without_an_id_still_mints_one(repository: Path) -> None:
    item = {k: v for k, v in _followup("f-ignored").items() if k != "id"}
    outcome = _append(repository, "followups", item)
    assert outcome["ok"], outcome
    state, _ = _store.read_plan(PROJECT, PLAN, repository, artifact_type="plan")
    minted = [f.get("id") for f in state.get("followups", [])]
    assert len(minted) == 1
    assert minted[0].startswith("f-")


@pytest.mark.parametrize(
    ("label", "ops", "ident", "existing"),
    [
        (
            "followup",
            [{"op": "append", "target": "followups", "item": _followup("f-dup")}],
            "f-dup",
            "First followup",
        ),
        (
            "section",
            [{"op": "append", "target": "sections", "item": _section("s-dup")}],
            "s-dup",
            "effort_hours=1.0",
        ),
        (
            "comment",
            [
                {
                    "op": "append",
                    "target": "comments",
                    "section": ANCHOR,
                    "item": _comment("c-dup"),
                }
            ],
            "c-dup",
            "a duplicate comment",
        ),
        (
            "gate",
            [
                {
                    "op": "gate",
                    "id": "g-dup",
                    "measure": "the measure",
                    "section": ANCHOR,
                    "gated_sections": [ANCHOR],
                }
            ],
            "g-dup",
            "the measure",
        ),
        (
            "decision",
            [
                {
                    "op": "append",
                    "target": "decisions",
                    "key": "d-dup",
                    "item": {
                        "question": "<p>Which way through?</p>",
                        "options": ["left", "right"],
                    },
                }
            ],
            "d-dup",
            "Which way through?",
        ),
    ],
)
def test_every_collection_refuses_a_duplicate_id(
    repository: Path,
    label: str,
    ops: list[dict],
    ident: str,
    existing: str,
) -> None:
    first = _edit(repository, [dict(op) for op in ops])
    assert first["ok"], first
    version_after_first = _version(repository)
    digest_after_first = hashlib.sha256(_plan_file(repository).read_bytes()).hexdigest()

    second = _edit(repository, [dict(op) for op in ops])

    assert second["ok"] is False, second
    assert second["error"] == "op_error", second
    _assert_refused(second["detail"], label, ident, existing)
    assert _version(repository) == version_after_first
    assert (
        hashlib.sha256(_plan_file(repository).read_bytes()).hexdigest()
        == digest_after_first
    )


def test_one_id_appended_from_two_writers_lands_exactly_once(
    repository: Path,
) -> None:
    base_version = _version(repository)
    barrier = threading.Barrier(2)
    results: list[dict | None] = [None, None]

    def writer(index: int) -> None:
        version = base_version
        barrier.wait()
        for _attempt in range(10):
            outcome = _edit(
                repository,
                [
                    {
                        "op": "append",
                        "target": "followups",
                        "item": _followup("f-race", "Raced followup"),
                    }
                ],
                version,
            )
            if outcome.get("ok"):
                results[index] = outcome
                return
            if outcome.get("error") == "version_conflict":
                version = _version(repository)
                continue
            results[index] = outcome
            return
        results[index] = {"ok": False, "error": "retries-exhausted"}

    threads = [
        threading.Thread(target=writer, args=(index,), daemon=True)
        for index in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    landed = [outcome for outcome in results if outcome and outcome.get("ok")]
    refused = [outcome for outcome in results if outcome and not outcome.get("ok")]
    assert len(landed) == 1, results
    assert len(refused) == 1, results
    assert refused[0]["error"] == "op_error", refused[0]
    assert "f-race" in refused[0]["detail"], refused[0]

    state, version = _store.read_plan(PROJECT, PLAN, repository, artifact_type="plan")
    raced = [f for f in state.get("followups", []) if f.get("id") == "f-race"]
    assert len(raced) == 1, state.get("followups")
    assert version == base_version + 1