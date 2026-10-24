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
from reckon import mcp_edit_plan as edit_module
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


FOLLOWUP_RECORD = {
    "id": "f-open",
    "written_by": "w-seed",
    "written_at": "2026-10-01T00:00:00Z",
    "title": "A followup the resolve addresses",
    "body": "<p>Body</p>",
    "prompt": f"/reckon-build {PLAN} §1",
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
        "owner": "seeded",
        "comments": {
            ANCHOR: [
                {
                    "id": "c-seeded",
                    "who": "editor",
                    "when": "2026-10-01T00:00:00Z",
                    "body": "<p>c-seeded</p>",
                }
            ]
        },
        "followups": [
            dict(FOLLOWUP_RECORD),
            # A second open followup keeps the batch's continuation open when
            # the resolve path is the one under test.
            {**FOLLOWUP_RECORD, "id": "f-next", "title": "The continuation"},
        ],
        "decisions": {"d-seeded": {"title": "A decision the lock addresses"}},
        "gates": [{"id": "g-seeded", "measure": "what the gate measures"}],
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
    """One op per addressing family, each naming a target the document holds.

    Every kind here addresses something the seeded document already carries, so
    the refusal's ``current`` value can be checked against the document for each
    path without an exception. A kind that inserts a new record has nothing to
    read back and is driven by its own case below.
    """
    return {
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
        "decision-lock": [
            {
                "op": "lock",
                "key": "d-seeded",
                "choice": "the first option",
                "rationale": "The rationale the lock records.",
                "by": "w-refused",
            }
        ],
        "followup-resolve": [
            {
                "op": "resolve",
                "target": "followups",
                "id": "f-open",
                "outcome": "Landed in the refused batch.",
                "by": "w-refused",
            }
        ],
        "gate-verdict": [
            {"op": "pass", "id": "g-seeded", "evidence": "The gate's log."}
        ],
        "state-set": [{"op": "set", "path": "owner", "value": "w-refused"}],
    }


def _value_in(state: dict, path: str):
    """The document's value at ``path``, resolved independently of the module."""
    current = state
    for part in path.split("."):
        assert isinstance(current, (dict, list)), (
            f"{path!r}: {part!r} addresses no mapping or list"
        )
        if isinstance(current, dict):
            assert part in current, f"{path!r}: {part!r} absent from the document"
            current = current[part]
        else:
            hits = [
                row
                for row in current
                if isinstance(row, dict) and str(row.get("id")) == part
            ]
            assert hits, f"{path!r}: the document holds no record {part!r}"
            current = hits[0]
    return current


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


def _seed_index(root: Path) -> Path:
    """A project index holding one item in the first of two sprints."""
    path = root / "docs" / "state" / PROJECT / "index.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    envelope = {
        "updated": "2026-10-01T00:00:00",
        "project": PROJECT,
        "doc": "index",
        "data": {
            "_version": 0,
            "active_sprint_id": "S1",
            "sprints": [
                {"id": "S1", "status": "active", "items": [{"slug": "i-moved"}]},
                {"id": "S2", "status": "planned", "items": []},
            ],
        },
    }
    path.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
    return path


def _landing_index_between_read_and_write(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Commit an index write inside the edit's own store call.

    The same shape as the plan landing above, aimed at the project index: the
    write lands after the edit has read the index and passed its read-time
    version check, so the store raises the conflict being converted.
    """
    real_write_plan = edit_module.write_plan
    landed = {"done": False}

    def landing_first(*args, **kwargs):
        if not landed["done"] and len(args) > 1 and args[1] == "index":
            landed["done"] = True
            state, version = _store.read_plan(
                PROJECT, "index", root, artifact_type="index"
            )
            real_write_plan(
                PROJECT,
                "index",
                {**state, "active_sprint_id": "S1", "sprints": state["sprints"]},
                version,
                root,
            )
        return real_write_plan(*args, **kwargs)

    monkeypatch.setattr(edit_module, "write_plan", landing_first)


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
        # Every op kind names the targets its own ops address, each carrying
        # the value the document holds at that address — read back off disk,
        # not one this test supplied.
        targets = outcome.get("changed_targets") or []
        assert targets, f"{kind} refused without naming its changed targets: {outcome}"
        state_after, _ = _store.read_plan(PROJECT, PLAN, root, artifact_type="plan")
        for entry in targets:
            assert "current" in entry, (
                f"{kind} named {entry['path']!r} without the value it holds"
            )
            assert entry["current"] == _value_in(state_after, entry["path"]), entry
        # The targets themselves differ between kinds by construction, so they
        # are compared apart from the refusal every path must return alike.
        shapes[kind] = {
            key: value for key, value in outcome.items() if key != "changed_targets"
        }

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


def test_an_append_of_a_new_record_names_its_path_alone(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record the document does not hold yet has no value to read back."""
    _state, version = _store.read_plan(PROJECT, PLAN, repository, artifact_type="plan")
    _landing_between_read_and_write(repository, monkeypatch)
    outcome = _edit(
        repository,
        [{"op": "append", "target": "sections", "item": dict(SECTION_RECORD)}],
        version,
    )

    assert isinstance(outcome, dict), outcome
    assert outcome["error"] == "version_conflict", outcome
    assert outcome["changed_targets"] == [{"index": 0, "path": "sections.s-refused"}], (
        outcome["changed_targets"]
    )


def test_a_refused_move_names_both_sprints(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A move leaves one sprint and joins another, so both are named.

    The refused write touches the source sprint's items and the destination's,
    so an answer naming only the destination sends the caller back to reconcile
    against a source sprint that also moved.
    """
    _seed_index(repository)
    _state, version = _store.read_plan(
        PROJECT, "index", repository, artifact_type="index"
    )
    assert version == 0

    _landing_index_between_read_and_write(repository, monkeypatch)
    outcome = _edit_plan(
        PROJECT,
        "index",
        [
            {
                "op": "move",
                "target": "sprint_item",
                "slug": "i-moved",
                "from": "S1",
                "to": "S2",
            }
        ],
        version,
        checkout_path=str(repository),
    )

    assert isinstance(outcome, dict), outcome
    assert outcome["error"] == "version_conflict", outcome
    # The landing really committed, so the refusal came from the store's own
    # version check rather than a stale read.
    assert outcome["current_version"] == 1, outcome
    targets = outcome.get("changed_targets") or []
    assert [entry["path"] for entry in targets] == ["sprints.S1", "sprints.S2"], (
        outcome["changed_targets"]
    )
    state_after, _ = _store.read_plan(
        PROJECT, "index", repository, artifact_type="index"
    )
    for entry in targets:
        assert entry["current"] == _value_in(state_after, entry["path"]), entry


def test_every_store_op_verb_names_a_target() -> None:
    """Every store verb composes a target, so no verb falls through to none.

    The op below carries every key any address branch reads, so it is a usable
    representative for every verb: a branch that exists finds its key and
    returns an address, while a verb with no branch — a verb just added to the
    store's dispatch, or whose branch was removed — returns ``None`` here.
    """
    from reckon import _store as store_module
    from reckon.mcp_edit_plan import _op_target_path

    representative = {
        "op": None,
        "path": "owner",
        "target": "followups",
        "id": "x-1",
        "key": "d-1",
        "item": {"id": "i-1"},
        "plan": PLAN,
        "anchor": "a-1",
        "slug": "i-1",
        "from": "s-1",
        "to": "s-2",
        "preimage": "<p>x</p>",
    }
    verbs = sorted(store_module._OP_DISPATCH)
    assert verbs, "the store's dispatch is empty — the check would pass vacuously"
    untargeted = [
        verb
        for verb in verbs
        if _op_target_path({**representative, "op": verb}) is None
    ]
    assert not untargeted, (
        f"{untargeted} are store op verbs with no composed target address — "
        "give each a branch in _op_target_path"
    )
