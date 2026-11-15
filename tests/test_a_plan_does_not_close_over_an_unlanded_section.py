"""A plan does not close over a done section that has no account of its landing.

The landing line is the only prose account of a node's work. A section declared
``done`` therefore owes one of two things: a landing comment on the section, or
a committed run record naming it. The close refuses while such a section has
neither, and names every unfurnished section it refuses on.

A done section that states no done-when is outside the rule: it gives the plan's
reasons, so no node could have landed it and it owes no account.

The rule applies to a section marked done after it landed. The store records no
per-section tick time, so the mark is read from the section's own last recorded
activity — a comment on it, or the completion time of a run naming it — held
against one fixed instant. A section recorded before that instant, or recorded
nowhere at all, was already done when the rule landed and owes nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import _plan_html, _store

PROJECT = "landing-account"
SLUG = "landing-account-guard"

#: Stamps on either side of the rule's fixed instant, so a fixture states which
#: side of the rule its section was marked done on without reading the clock.
DONE_BEFORE_THE_RULE = "2026-11-14T09:00:00Z"
DONE_AFTER_THE_RULE = "2026-11-15T12:00:00Z"
#: The same instant as an epoch, the shape some writers store.
DONE_AFTER_THE_RULE_EPOCH = 1794744000

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


def _activity(when: str) -> dict:
    """A comment on the section that is not a landing comment.

    It records that the store wrote something about the section at ``when``,
    which is what the rule reads to place the section either side of its fixed
    instant. Its id carries no close prefix, so it is never mistaken for the
    landing account.
    """
    return {
        "id": "c-run-r-fixture-review-of-the-section",
        "who": "reckon-build",
        "when": when,
        "body": "A review of the section.",
    }


def _plan_state(**overrides: object) -> dict:
    state = {
        "project": PROJECT,
        "type": "plan",
        "slug": SLUG,
        "title": "Landing account fixture",
        "status": "active",
        "version": 1,
        "section_declarations": {"s1": "done", "s2": "done"},
        # Recorded after the rule landed, so s1 is inside the rule and owes an
        # account. A fixture about the pre-rule side replaces this stamp.
        "comments": {"s1": [_activity(DONE_AFTER_THE_RULE)]},
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
    # s2 is worked on after the rule landed, so it is inside the rule's reach
    # and only the absence of a done-when keeps it clear. Without this stamp
    # s2 would also be exempt for having no recorded activity, and the case
    # would pass even against a check that ignored the done-when entirely.
    state["comments"]["s2"] = [_activity(DONE_AFTER_THE_RULE)]

    # s1 answers with a landing comment; s2 states no done-when and has none,
    # so the close owes it nothing.
    _store.apply_ops(state, [_landing_comment_op("s1"), CLOSE], False)

    assert state["status"] == "done"


def test_a_section_done_before_the_rule_does_not_stop_a_close(
    mounted_plan: Path,
) -> None:
    # The only difference from the refused case is the stamp: this section's
    # last recorded activity predates the rule's fixed instant, so it was
    # already done when the rule landed and owes no account. No clock is read,
    # so the expectation holds on any later day.
    state = _read_state()
    state["comments"] = {"s1": [_activity(DONE_BEFORE_THE_RULE)]}

    _store.apply_ops(state, [CLOSE], False)

    assert state["status"] == "done"


def test_a_section_marked_done_after_the_rule_is_still_refused(
    mounted_plan: Path,
) -> None:
    # This section's last recorded activity is after the rule's fixed instant,
    # so it was marked done under the rule and owes its landing account.
    state = _read_state()
    assert state["comments"], "the fixture records activity on s1"

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
        lambda *_a, **_k: [
            {
                "run_id": "r-x",
                "plan": SLUG,
                "section": "\u00a71",
                "completed_at": DONE_AFTER_THE_RULE,
            }
        ],
    )
    state = _read_state()

    _store.apply_ops(state, [CLOSE], False)

    assert state["status"] == "done"


def test_a_section_already_done_stays_exempt_when_a_later_comment_arrives(
    mounted_plan: Path,
) -> None:
    # A section that states a done-when and was recorded before the rule landed
    # stays exempt however its plan is edited, so a comment appended since then
    # must not take the exemption away. Keying the rule on the newest activity
    # instead of the earliest refuses this close.
    state = _read_state()
    state["comments"]["s1"] = [
        _activity(DONE_BEFORE_THE_RULE),
        {**_activity(DONE_AFTER_THE_RULE), "id": "c-run-later"},
    ]

    _store.apply_ops(state, [CLOSE], False)

    assert state["status"] == "done"


def test_a_stamp_only_the_shared_parser_reads_places_the_section(
    mounted_plan: Path,
) -> None:
    # The store's comment stamps are ISO strings, but the ledger and other
    # writers also produce numeric epochs, and one parser already owns every
    # shape. An epoch after the rule's instant puts the section inside the rule,
    # so the close is refused; a reader that understands only ISO leaves the
    # stamp unread and the section exempt, and this close would succeed.
    state = _read_state()
    state["comments"]["s1"] = [
        {"id": "c-run-epoch", "who": "reckon-build", "when": DONE_AFTER_THE_RULE_EPOCH}
    ]

    with pytest.raises(_store.OpError) as refusal:
        _store.apply_ops(state, [CLOSE], False)

    assert "s1" in str(refusal.value)
