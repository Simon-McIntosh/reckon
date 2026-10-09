"""A tool-surface edit that meets a landing write is refused, never raised.

An edit reads the plan's version, composes its new state, and the store checks
the version again under the plan's write lock. A landing write that commits
between the edit's read and its write makes the store raise its version
conflict, and the tool surface must convert that into the one-shape refusal
naming both versions rather than let it propagate to the caller.

The interleaving here is forced rather than raced: a monkeypatched read commits
the landing write at the moment the edit has taken its version.

Reproducing it also needs what the suite reaches under load — a reload of
``reckon._store``. Fixtures that re-read its environment-resolved paths reload
that module, and a reload normally rebinds ``_store.VersionConflict`` to a new
class object while a module that imported it by name keeps the old one, so a
handler's ``except VersionConflict`` stops matching what the store raises. The
store keeps the class object identical across a reload, which is what these
cases exercise: the reload makes a propagated conflict visible.

Three writers must come back refused with both versions named, their change
absent and the landing record intact: a followup append, a section append and a
state set. A comment append is the one commutative shape — the store merges it
onto the file's comments — so its assertion is that it succeeds with both
records present. In every case, an exception reaching the caller is a failure.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from reckon import _plan_html, _store, mcp, mcp_edit_plan
from tests.mcp_family_reload import reload_mcp_family

PROJECT = "race-fixture"
PLAN = "race-target"
ANCHOR = "s5"


def _section_record() -> dict:
    return {
        "id": "s-race",
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


def _comment(ident: str, who: str) -> dict:
    return {
        "id": ident,
        "who": who,
        "when": "2026-10-02T00:00:00Z",
        "body": f"<p>{ident}</p>",
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
        "title": "Race target",
        "status": "active",
        "version": 0,
        "comments": {},
        # An implementable section gives an appended followup a dispatchable
        # pointer, so the op is valid for reasons unrelated to the interleaving
        # being measured.
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
    # Every environment-resolved path — mounts, lock files, state — must
    # resolve inside the temp home before any writer runs.
    assert _store._config_home() == config_home.resolve()
    return root


def _landing_write(root: Path, *, retries: int = 3) -> dict:
    """Append one landing record the way a promotion appends it.

    Re-reads and re-appends after a version conflict instead of discarding the
    record, and catches the store's conflict through the module attribute so
    the handler keeps matching across a reload of the store.
    """
    for _attempt in range(retries + 1):
        state, version = _store.read_plan(PROJECT, PLAN, root, artifact_type="plan")
        comments = {
            key: list(items) for key, items in (state.get("comments") or {}).items()
        }
        comments.setdefault(ANCHOR, []).append(_comment("c-landing", "promote"))
        try:
            _store.write_plan(
                PROJECT,
                PLAN,
                {**state, "comments": comments},
                version,
                root,
                artifact_type="plan",
            )
        except _store.VersionConflict:
            continue
        return {"ok": True, "recorded": True, "comment_id": "c-landing"}
    raise AssertionError("the landing record never landed")


def _forced_interleaving(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_edit: Callable[[], object],
) -> tuple[object, dict]:
    """Run ``run_edit`` with a landing write committed between its read and write.

    ``reckon._store`` is reloaded first, the condition the suite reaches, so a
    conflict that escapes a by-name handler propagates here instead of hiding.
    The monkeypatched read commits the landing write on the edit's first read —
    after the edit has taken its version, before it writes.
    """
    importlib.reload(_store)

    fired = False
    landing: dict = {}
    real_read = mcp_edit_plan.read_plan

    def hooked_read(*args, **kwargs):
        nonlocal fired
        data = real_read(*args, **kwargs)
        if not fired:
            fired = True
            landing["outcome"] = _landing_write(root)
        return data

    monkeypatch.setattr(mcp_edit_plan, "read_plan", hooked_read)
    try:
        outcome = run_edit()
    finally:
        monkeypatch.setattr(mcp_edit_plan, "read_plan", real_read)
        # Leave the process with the store and the tool surface agreeing again,
        # whichever class objects they started with.
        reload_mcp_family()
    if not fired:
        raise AssertionError(
            "the edit never read the plan, so no landing write was forced in"
        )
    return outcome, landing["outcome"]


def _initial_version(root: Path) -> int:
    _state_dict, version = _store.read_plan(PROJECT, PLAN, root, artifact_type="plan")
    return version


def _state(root: Path) -> dict:
    state, _version = _store.read_plan(PROJECT, PLAN, root, artifact_type="plan")
    return state


def _comment_ids(state: dict) -> set[str]:
    return {
        str(item.get("id"))
        for items in (state.get("comments") or {}).values()
        for item in items
    }


def _refused_naming_both_versions(outcome: object) -> bool:
    """Whether an outcome is a refusal that names both versions."""
    if not isinstance(outcome, dict):
        return False
    if outcome.get("ok") is not False or outcome.get("error") != "version_conflict":
        return False
    expected = outcome.get("expected_version")
    current = outcome.get("current_version")
    message = str(outcome.get("message", ""))
    return (
        isinstance(expected, int)
        and isinstance(current, int)
        and expected != current
        and str(expected) in message
        and str(current) in message
    )


def _wrote_followup(state: dict) -> bool:
    return "f-race" in {str(item.get("id")) for item in (state.get("followups") or [])}


def _wrote_section(state: dict) -> bool:
    return "s-race" in {str(item.get("id")) for item in (state.get("sections") or [])}


def _wrote_state_set(state: dict) -> bool:
    return state.get("owner") == "w-race"


def _wrote_comment(state: dict) -> bool:
    return "c-edit" in _comment_ids(state)


@pytest.mark.parametrize(
    ("build_ops", "change_present", "expectation"),
    [
        pytest.param(
            lambda: [
                {
                    "op": "append",
                    "target": "followups",
                    "item": {
                        "id": "f-race",
                        "written_by": "w-race",
                        "written_at": "2026-10-02T00:00:00Z",
                        "title": "A followup appended mid-landing",
                        "body": "<p>Body</p>",
                        "prompt": "/reckon-build race-target §1",
                    },
                }
            ],
            _wrote_followup,
            "refused",
            id="followup-append",
        ),
        pytest.param(
            lambda: [{"op": "append", "target": "sections", "item": _section_record()}],
            _wrote_section,
            "refused",
            id="section-append",
        ),
        pytest.param(
            lambda: [{"op": "set", "path": "owner", "value": "w-race"}],
            _wrote_state_set,
            "refused",
            id="state-set",
        ),
        pytest.param(
            lambda: [
                {
                    "op": "append",
                    "target": "comments",
                    "section": ANCHOR,
                    "item": _comment("c-edit", "editor"),
                }
            ],
            _wrote_comment,
            "commuted",
            id="comment-append",
        ),
    ],
)
def test_edit_forced_to_lose_a_landing_race_is_refused_not_raised(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_ops: Callable[[], list[dict]],
    change_present: Callable[[dict], bool],
    expectation: str,
) -> None:
    version = _initial_version(repository)
    outcome, landing = _forced_interleaving(
        repository,
        monkeypatch,
        lambda: mcp._edit_plan(
            PROJECT,
            PLAN,
            build_ops(),
            version,
            checkout_path=str(repository),
            doc_type="plan",
        ),
    )
    assert landing.get("ok") is True, landing

    state = _state(repository)
    assert "c-landing" in _comment_ids(state), sorted(_comment_ids(state))
    assert change_present(state) is (expectation == "commuted"), state

    if expectation == "refused":
        assert _refused_naming_both_versions(outcome), outcome
        assert isinstance(outcome, dict)
        assert outcome["expected_version"] == version, outcome
        assert outcome["current_version"] == version + 1, outcome
        assert outcome.get("resource", {}).get("id") == PLAN, outcome
    else:
        assert isinstance(outcome, dict) and outcome.get("ok") is True, outcome
        assert {"c-landing", "c-edit"} <= _comment_ids(state), sorted(
            _comment_ids(state)
        )

    # Positive control: the plan is not fenced against every write — a
    # sequential edit after the interleaving still lands and advances the
    # version.
    before = _initial_version(repository)
    sequential = mcp._edit_plan(
        PROJECT,
        PLAN,
        [{"op": "set", "path": "owner", "value": "after-race"}],
        before,
        checkout_path=str(repository),
        doc_type="plan",
    )
    assert sequential.get("ok") is True, sequential
    assert sequential.get("new_version") == before + 1, sequential
    assert _state(repository).get("owner") == "after-race"
