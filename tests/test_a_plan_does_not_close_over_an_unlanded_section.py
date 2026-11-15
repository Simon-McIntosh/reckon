"""A plan does not close over a done section that has no account of its landing.

The landing line is the only prose account of a node's work. A section declared
``done`` therefore owes one of two things: a landing comment on the section, or
a committed run record naming it. The close refuses while such a section has
neither, and names every unfurnished section it refuses on.

A done section that states no done-when is outside the rule: it gives the plan's
reasons, so no node could have landed it and it owes no account.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import _plan_html, _store

PROJECT = "landing-account"
SLUG = "landing-account-guard"

CLOSE = {
    "op": "close",
    "status": "done",
    "outcome": "every section landed — no followup",
    "by": "reckon-build",
}

DOCUMENT = f"""<!doctype html><html><head>
<meta name="docs-project" content="{PROJECT}">
<title>{SLUG}</title></head><body><main class="plan-doc">
<h2 id="s1">§1 — A landed section</h2>
<p>This section ships one guard.</p>
<p><strong>Done when</strong> the guard refuses the write.</p>
<h2 id="s2">§2 — Reasons</h2>
<p>This section gives the plan's reasons and states no measure.</p>
</main></body></html>
"""


def _plan_state(**overrides: object) -> dict:
    state = {
        "project": PROJECT,
        "type": "plan",
        "slug": SLUG,
        "title": "Landing account fixture",
        "status": "active",
        "version": 1,
        "section_declarations": {"s1": "done", "s2": "done"},
        "comments": {},
        "followups": [],
    }
    state.update(overrides)
    return state


def _landing_comment_op(section: str) -> dict:
    return {
        "op": "append",
        "target": "comments",
        "section": section,
        "item": {
            "id": f"c-close-{section}",
            "who": "reckon-build",
            "when": "2026-11-15T00:00:00Z",
            "body": f"Landed {section}.",
        },
    }


def _write_html(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_plan_html.write_state(DOCUMENT, state), encoding="utf-8")


@pytest.fixture()
def mounted_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Mount the fixture project with a back-linking evidence record.

    The close also owes the terminal-evidence record, so one is present and the
    landing-account guard is left as the only thing that could refuse the write.
    """
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    docs = tmp_path / "repo" / "docs"
    _write_html(docs / "plans" / f"{SLUG}.html", _plan_state())
    _write_html(
        docs / "evidence" / "archive" / f"{SLUG}-landed.html",
        {
            "type": "evidence",
            "slug": SLUG,
            "title": "Landing account evidence",
            "evidence_for": [SLUG],
            "version": 0,
        },
    )
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(docs)}), encoding="utf-8"
    )
    return docs


def _read_state() -> dict:
    state, _version = _store.read_plan(PROJECT, SLUG)
    assert state, "the fixture plan is readable"
    return state


def test_a_close_over_sections_that_each_carry_a_landing_comment_succeeds(
    mounted_plan: Path,
) -> None:
    state = _read_state()

    _store.apply_ops(
        state,
        [_landing_comment_op("s1"), _landing_comment_op("s2"), CLOSE],
        False,
    )

    assert state["status"] == "done"


def test_a_close_is_refused_over_a_done_section_with_no_account(
    mounted_plan: Path,
) -> None:
    state = _read_state()

    with pytest.raises(_store.OpError) as refusal:
        _store.apply_ops(state, [CLOSE], False)

    assert "s1" in str(refusal.value), "the refusal names the unfurnished section"
    assert "s2" not in str(refusal.value), "a section stating no done-when owes nothing"


def test_adding_the_landing_comment_lets_the_same_close_succeed(
    mounted_plan: Path,
) -> None:
    state = _read_state()

    with pytest.raises(_store.OpError):
        _store.apply_ops(state, [CLOSE], False)

    _store.apply_ops(state, [_landing_comment_op("s1"), CLOSE], False)

    assert state["status"] == "done"


def test_a_done_section_that_states_no_done_when_does_not_stop_a_close(
    mounted_plan: Path,
) -> None:
    state = _read_state()

    # s1 answers with a landing comment; s2 states no done-when and has none,
    # so the close owes it nothing.
    _store.apply_ops(state, [_landing_comment_op("s1"), CLOSE], False)

    assert state["status"] == "done"


def test_a_section_done_before_the_rule_does_not_stop_a_close(
    mounted_plan: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The migration record names this section, which is the only thing that
    # makes it pre-rule: nothing about the plan's present text says so, and no
    # clock is consulted, so the exemption cannot change meaning with the day.
    monkeypatch.setattr(
        _store,
        "_LANDING_RULE_EXEMPTIONS",
        {PROJECT: {SLUG: ("s1",)}},
    )
    state = _read_state()

    _store.apply_ops(state, [CLOSE], False)

    assert state["status"] == "done"


def test_a_section_marked_done_after_the_rule_is_still_refused(
    mounted_plan: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The record covers a different section of this plan, so s1 was marked done
    # after the rule landed and owes its landing account like any other.
    monkeypatch.setattr(
        _store,
        "_LANDING_RULE_EXEMPTIONS",
        {PROJECT: {SLUG: ("s9",)}},
    )
    state = _read_state()

    with pytest.raises(_store.OpError) as refusal:
        _store.apply_ops(state, [CLOSE], False)

    assert "s1" in str(refusal.value)


def test_a_run_record_for_the_section_answers_for_it(
    mounted_plan: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The ledger read is substituted so the case needs no promoted run: the
    # record shape is the committed one, and the section spelling is the
    # dotted form a dispatch writes.
    from reckon import ledger

    monkeypatch.setattr(
        ledger,
        "runs",
        lambda *_a, **_k: [{"run_id": "r-x", "plan": SLUG, "section": "§1"}],
    )
    state = _read_state()

    _store.apply_ops(state, [CLOSE], False)

    assert state["status"] == "done"