"""The plan-review rubric prompts mirror their item constants.

A rubric prompt is read by a model and its items are mirrored by a constant the
code reads, so the two can drift: an item added to the constant and not to the
prompt is a check nobody is told to make, and an item added to the prompt and not
to the constant is a check no reader can enumerate. This is the same mirror the
code-review prompt is held to, extended to the two plan rubrics. It fails in both
directions for both prompts.

The two focused cases hold the two rubric clauses the pilot's first run turned
into requirements: the anchor rule resolves an extensionless href the way the
surface does before it reports a finding, and the reasoning check names the
caller the mechanism runs on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon.crew import review as review_module

# (constant name, prompt path attribute, loader name) for each plan rubric.
PLAN_RUBRICS = (
    ("PLAN_REVIEW_ITEMS", "_PLAN_REVIEW_PROMPT_PATH", "load_plan_review_prompt"),
    (
        "PLAN_DESIGN_REVIEW_ITEMS",
        "_PLAN_DESIGN_REVIEW_PROMPT_PATH",
        "load_plan_design_review_prompt",
    ),
)


def _items_from_prompt(text: str) -> list[str]:
    """Read the ``RUBRIC_ITEMS:`` line the prompt carries as its own list."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("RUBRIC_ITEMS:"):
            payload = stripped.split(":", 1)[1]
            return [part.strip() for part in payload.split(",") if part.strip()]
    raise AssertionError(
        "prompt carries no RUBRIC_ITEMS line, so its items cannot be enumerated"
    )


@pytest.mark.parametrize(("constant", "path_attr", "loader"), PLAN_RUBRICS)
def test_prompt_names_every_item_and_every_item_is_in_the_prompt(
    constant: str, path_attr: str, loader: str
) -> None:
    items = tuple(getattr(review_module, constant))
    text = getattr(review_module, loader)()
    # Direction 1: every item the code enumerates is named in the prompt.
    for item in items:
        assert item in text, f"rubric item {item} missing from {path_attr}"
    # Direction 2: every item the prompt names is in the code's constant. This is
    # the half that catches an item added to the prompt alone.
    assert _items_from_prompt(text) == list(items), (
        f"the prompt's RUBRIC_ITEMS list disagrees with {constant}"
    )


@pytest.mark.parametrize(("constant", "path_attr", "loader"), PLAN_RUBRICS)
def test_every_finding_carries_a_would_change_verdict_and_reason(
    constant: str, path_attr: str, loader: str
) -> None:
    text = getattr(review_module, loader)()
    assert "WOULD_CHANGE_THE_PLAN:" in text
    assert "REASON:" in text
    assert "RUBRIC <item>:" in text


def test_anchor_rule_resolves_an_extensionless_href_before_reporting() -> None:
    text = review_module.load_plan_review_prompt()
    assert "extensionless" in text.lower()
    assert "<path>.html" in text
    assert "docs/ui/plan.jsx" in text


def test_reasoning_check_names_the_real_path_caller() -> None:
    text = review_module.load_plan_review_prompt()
    assert "does the mechanism run on the real code path, with the caller named" in text
    assert "does the stated root cause match the cited evidence" in text
    assert "does each section's mechanism produce its done-when" in text


@pytest.mark.parametrize(("constant", "path_attr", "loader"), PLAN_RUBRICS)
def test_loader_reads_from_disk_at_call_time(
    constant: str,
    path_attr: str,
    loader: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    probe = tmp_path / "rubric.md"
    monkeypatch.setattr(review_module, path_attr, probe)
    probe.write_text("first load\n", encoding="utf-8")
    assert getattr(review_module, loader)() == "first load\n"
    probe.write_text("second load sees the edit\n", encoding="utf-8")
    assert getattr(review_module, loader)() == "second load sees the edit\n"
