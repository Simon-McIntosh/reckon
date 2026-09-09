"""Declared executable-section remainder contract."""

from reckon._plan_html import read_state, write_state
from reckon._schema import (
    EXECUTABLE_REMAINDER_UNKNOWN,
    PlanState,
    plan_executable_remainder,
    plan_section_anchors,
)


def _html(meta: str = "") -> str:
    return (
        "<!doctype html><html><head>"
        '<meta name="reckon-type" content="plan">'
        f"{meta}</head><body><main>"
        '<h2 id="s1">First</h2><h2 id="s2">Second</h2>'
        "</main></body></html>"
    )


def test_declared_implementable_sections_establish_the_denominator() -> None:
    state = {
        "section_declarations": {
            "s1": "implementable",
            "s2": "implementable",
            "s3": "implementable",
        },
        "comments": {"s1": [{"id": "landing"}]},
    }

    assert plan_executable_remainder(state) == 3


def test_landing_evidence_does_not_decrement_the_remainder() -> None:
    state = {
        "section_declarations": {"s1": "implementable", "s2": "implementable"},
        "comments": {
            "s1": [{"id": "first"}, {"id": "second"}],
            "s2": [{"id": "third"}],
        },
    }

    assert plan_executable_remainder(state) == 2


def test_five_declared_sections_with_three_landings_all_remain() -> None:
    state = {
        "section_declarations": {
            "s1": "implementable",
            "s2": "implementable",
            "s3": "implementable",
            "s4": "implementable",
            "s5": "implementable",
        },
        "comments": {
            "s1": [{"id": "landing-one"}],
            "s3": [{"id": "landing-two"}],
            "s5": [{"id": "landing-three"}],
        },
    }

    assert plan_executable_remainder(state) == 5


def test_done_reclassification_lowers_the_remainder_by_exactly_one() -> None:
    state = {
        "section_declarations": {
            "s1": "implementable",
            "s2": "implementable",
            "s3": "implementable",
            "s4": "implementable",
            "s5": "implementable",
        }
    }
    before = plan_executable_remainder(state)
    state["section_declarations"]["s3"] = "done"

    assert before == 5
    assert plan_executable_remainder(state) == before - 1


def test_later_landing_evidence_does_not_reopen_a_done_section() -> None:
    state = {
        "section_declarations": {"s1": "done", "s2": "implementable"},
        "comments": {"s1": [{"id": "later-landing"}]},
    }

    assert plan_executable_remainder(state) == 1


def test_malformed_declaration_stays_unknown_even_with_landing_evidence() -> None:
    state = {
        "section_declarations": {"s1": "queued"},
        "comments": {"s1": [{"id": "landing"}]},
    }

    assert plan_executable_remainder(state) is EXECUTABLE_REMAINDER_UNKNOWN


def test_empty_comment_collection_is_not_landing_evidence() -> None:
    state = {
        "section_declarations": {"s1": "implementable"},
        "comments": {"s1": []},
    }

    assert plan_executable_remainder(state) == 1


def test_missing_declaration_is_unknown_and_never_zero() -> None:
    remainder = plan_executable_remainder({"comments": {"s1": [{"id": "landing"}]}})

    assert remainder is EXECUTABLE_REMAINDER_UNKNOWN
    assert remainder != 0


def test_explicit_empty_declaration_is_known_zero() -> None:
    assert plan_executable_remainder({"section_declarations": {}}) == 0


def test_deferred_and_done_entries_are_excluded_from_the_denominator() -> None:
    state = {
        "section_declarations": {
            "s1": "implementable",
            "s2": "deferred",
            "s3": "done",
        }
    }

    assert plan_executable_remainder(state) == 1


def test_malformed_declaration_is_unknown_instead_of_false_zero() -> None:
    state = {"section_declarations": {"s1": "queued"}}

    assert plan_executable_remainder(state) is EXECUTABLE_REMAINDER_UNKNOWN


def test_typed_state_preserves_the_declaration_mapping() -> None:
    declarations = {
        "s1": "done",
        "s2": "implementable",
        "s3": "deferred",
    }

    assert (
        PlanState.model_validate(
            {"section_declarations": declarations}
        ).canonical_dump()["section_declarations"]
        == declarations
    )


def test_html_write_then_read_preserves_declarations_byte_identically() -> None:
    declarations = {
        "s2": "implementable",
        "s1": "done",
        "s3": "deferred",
    }
    rendered = write_state(_html(), {"section_declarations": declarations})

    assert read_state(rendered)["section_declarations"] == declarations


def test_html_without_declaration_reads_as_unknown() -> None:
    state = read_state(_html())

    assert "section_declarations" not in state
    assert plan_executable_remainder(state) is EXECUTABLE_REMAINDER_UNKNOWN


def test_non_plan_round_trip_does_not_persist_declarations() -> None:
    source = _html().replace('content="plan"', 'content="research"')
    state = PlanState.model_validate(
        {"type": "research", "section_declarations": {"s1": "implementable"}}
    )

    assert "section_declarations" not in state.canonical_dump()
    assert "plan-section-declarations" not in write_state(
        source, state.canonical_dump()
    )


def test_existing_section_anchor_derivation_is_unchanged() -> None:
    state = {
        "section_declarations": {"declared-only": "implementable"},
        "gates": [{"section": "gated", "gated_sections": ["downstream"]}],
        "comments": {"commented": [{"id": "evidence"}]},
    }

    assert plan_section_anchors(state) == frozenset(
        {"gated", "downstream", "commented"}
    )
