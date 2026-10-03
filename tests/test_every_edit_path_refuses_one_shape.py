"""Every op path of a plan edit refuses a version conflict in one shape.

A version conflict that reaches a caller as a raised exception reads as a
lost write: the writer cannot distinguish a correct refusal from a crash, and
its work is gone either way. The store signals the conflict by raising, so
each op kind the tool surface accepts must leave the edit as the same
structured refusal — ``ok`` false, ``error`` version_conflict, the expected
and current versions, and a message naming both.

The conflict driven here is real rather than a stale read: a landing write
commits between the edit's read and its write, so the store raises inside the
call and the edit must convert it. A test that only passes a version stale by
construction exercises the read-time pre-check instead, which returns the
same refusal without ever reaching the conversion being measured.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import _plan_html, _store
from reckon.mcp import _edit_plan

PROJECT = "edit-path-fixture"
PLAN = "edit-path-target"
ANCHOR = "s1"

SECTION_RECORD = {
    "id": "s-refused",
    "title": "The authored heading a section append writes",
    "body": "<p>Prose the append authors.</p>",
    "effort_hours": 0.5,
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
        "title": "Edit-path target",
        "status": "active",
        "version": 0,
        "comments": {},
        "section_declarations": {"s1": "implementable"},
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
    assert _store._config_home() == config_home.resolve()
    return root


def _op_kinds() -> dict[str, list[dict]]:
    return {
        "followup-append": [
            {
                "op": "append",
                "target": "followups",
                "item": {
                    "id": "f-refused",
                    "written_by": "w-refused",
                    "written_at": "2026-10-03T00:00:00Z",
                    "title": "A followup appended mid-landing",
                    "body": "<p>Body</p>",
                    "prompt": f"/reckon-build {PLAN} §1",
                },
            }
        ],
        "section-append": [
            {"op": "append", "target": "sections", "item": dict(SECTION_RECORD)}
        ],
        "state-set": [{"op": "set", "path": "owner", "value": "w-refused"}],
        "comment-append": [
            {
                "op": "append",
                "target": "comments",
                "section": ANCHOR,
                "item": {
                    "id": "c-refused",
                    "who": "editor",
                    "when": "2026-10-03T00:00:00Z",
                    "body": "<p>c-refused</p>",
                },
            }
        ],
    }


def _landing_between_read_and_write(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Commit a landing write inside the edit's own store call.

    The wrapper runs after the edit has read the plan and passed its read-time
    version check, so what the writer meets is the store's raise rather than
    the pre-check. The landing changes ``owner`` so a comment append cannot
    merge across it either: the commutative merge only survives a meeting that
    agrees about the rest of the document.
    """
    real_write_state = _store._write_state
    landed = {"done": False}

    def landing_first(*args, **kwargs):
        if not landed["done"]:
            landed["done"] = True
            state, version = _store.read_plan(PROJECT, PLAN, root, artifact_type="plan")
            real_write_state(
                PROJECT,
                PLAN,
                {**state, "owner": "landing"},
                version,
                root,
                artifact_type="plan",
            )
        return real_write_state(*args, **kwargs)

    monkeypatch.setattr(_store, "_write_state", landing_first)


def _edit(root: Path, ops: list[dict], expected_version: int) -> dict:
    return _edit_plan(
        PROJECT,
        PLAN,
        ops,
        expected_version,
        checkout_path=str(root),
        doc_type="plan",
    )


@pytest.mark.parametrize("kind", sorted(_op_kinds()))
def test_every_op_path_refuses_a_stale_version_in_one_shape(
    repository: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    _state, version = _store.read_plan(PROJECT, PLAN, repository, artifact_type="plan")
    assert version == 0

    _landing_between_read_and_write(repository, monkeypatch)
    outcome = _edit(repository, _op_kinds()[kind], version)

    assert isinstance(outcome, dict), (
        f"{kind} raised {outcome!r} instead of returning the structured refusal"
    )
    assert outcome["ok"] is False
    assert outcome["error"] == "version_conflict"
    assert outcome["expected_version"] == 0
    assert outcome["current_version"] == 1
    message = str(outcome["message"])
    assert "0" in message and "1" in message


def test_the_refusal_shape_is_identical_across_op_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shapes: dict[str, dict] = {}
    for kind, ops in sorted(_op_kinds().items()):
        # Each kind is driven on its own seeded plan and home so one refusal
        # cannot stand in for another's.
        home = tmp_path / kind / "config"
        home.mkdir(parents=True)
        monkeypatch.setenv("RECKON_HOME", str(home))
        root = tmp_path / kind / "repo"
        _seed(root)
        (home / "mounts.json").write_text(
            json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
        )
        _state, version = _store.read_plan(PROJECT, PLAN, root, artifact_type="plan")
        assert version == 0

        _landing_between_read_and_write(root, monkeypatch)
        outcome = _edit(root, ops, version)
        assert isinstance(outcome, dict), f"{kind} raised {outcome!r}"
        shapes[kind] = outcome

    reference_kind, reference = next(iter(sorted(shapes.items())))
    for kind, shape in shapes.items():
        assert shape == reference, (
            f"{kind} refused in a different shape than {reference_kind}:\n"
            f"{kind}: {shape}\n{reference_kind}: {reference}"
        )


def test_a_sequential_write_after_the_refusal_still_lands(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control: the refusal is about the version, not a broken path."""
    _state, version = _store.read_plan(PROJECT, PLAN, repository, artifact_type="plan")
    _landing_between_read_and_write(repository, monkeypatch)
    _edit(repository, _op_kinds()["state-set"], version)

    state, current = _store.read_plan(PROJECT, PLAN, repository, artifact_type="plan")
    assert current == 1
    retried = _edit(repository, _op_kinds()["state-set"], current)
    assert retried.get("ok") is True, retried
    assert retried.get("new_version") == 2, retried
    state, current = _store.read_plan(PROJECT, PLAN, repository, artifact_type="plan")
    assert state.get("owner") == "w-refused"
