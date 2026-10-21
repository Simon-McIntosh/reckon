"""A lane that declares image input is named its figures rather than refusing.

The claude-family dialect carries no flag that attaches an image, and it
refused every figure for that reason, so a figure review could only ever route
to the one lane with an image flag. A lane that has been measured to receive a
figure its worker opens itself needs no such flag: the paths go into the prompt
the worker reads, with an instruction to open each one with the Read tool. The
change keys on the lane's own ``input_modalities`` declaration rather than on
the dialect, because clive and the claude lane share one flag grammar and only
clive has been measured -- declared lanes are handed the figure, undeclared
lanes go on refusing by name.

  - a declared claude-family lane given one image carries that path in its
    prompt, carries the instruction to open it, does not raise, and keeps the
    path off argv;
  - the same lane given no image carries the prompt exactly as before;
  - an undeclared claude-family lane still refuses the figure by name;
  - ``_backend_settings`` carries the lane's declaration onto the settings a
    resumed run is rebuilt from, and the launch composed from those settings
    names the image instead of refusing it;
  - a flight whose only image-declaring lane is clive offers clive as a
    figure-review candidate;
  - the two reads of a lane's declaration -- the naming in ``launch_plan`` and
    the review-lane selection in ``reckon.flight`` -- agree on whether it
    admits an image, over the declarations ``[text, image]``, ``[text]``,
    ``[]``, nothing, ``[IMAGE]``, ``[' image ']`` and ``text,image``, the last
    three admitting one, because the naming path reads through the function
    ``reckon.flight`` owns;
  - a prompt that already carries a figure listing composes to exactly one,
    naming the figures the launch carries, and to none on codex.

Every fixture here is built inside the test's own temporary directory, so no
assertion reads a path outside it.

The declared negative control removes the image naming from ``launch_plan``;
the declared lane is then handed no image naming and the prompt assertions
fail, while a lane given no figure is unaffected.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from reckon import _backends, flight
from reckon.crew import recovery

sessions_module = importlib.import_module("reckon.crew.dispatch_sessions")

FIGURE = "run-figures/image-0.png"
OTHER_FIGURE = "run-figures/image-1.png"

# The heading a prompt carries when its lane is named its figures. Spelled here
# rather than imported so the count below reads the text a worker is handed.
FIGURE_LISTING_MARKER = "Figures attached to this node"

DECLARED_LANE = "clive"
UNDECLARED_LANE = "claude"


def _claude_family_lane(command: str, modalities=None) -> dict:
    lane = {"launch": "cli", "command": command, "sandbox": "worktree-full"}
    if modalities is not None:
        lane["input_modalities"] = modalities
    return lane


def _plan(
    backend: dict,
    images=(),
    *,
    backend_name: str = "lane",
    fence: bool = False,
    prompt: str = "review the figure",
):
    return _backends.launch_plan(
        backend_name=backend_name,
        backend=backend,
        prompt=prompt,
        worktree="/wt",
        images=images,
        fence=fence,
    )


def test_a_declared_lane_is_named_the_image_in_its_prompt():
    plan = _plan(
        _claude_family_lane("clive", ["text", "image"]),
        (FIGURE,),
        backend_name=DECLARED_LANE,
    )

    assert FIGURE in plan.stdin_text
    assert "Read" in plan.stdin_text
    assert FIGURE not in plan.argv


def test_a_declared_lane_given_no_image_carries_the_prompt_as_today():
    plan = _plan(
        _claude_family_lane("clive", ["text", "image"]), (), backend_name="clive"
    )

    assert plan.stdin_text == "review the figure"


def test_an_undeclared_lane_still_refuses_the_image_by_name():
    with pytest.raises(_backends.BackendError) as excinfo:
        _plan(_claude_family_lane("claude"), (FIGURE,), backend_name=UNDECLARED_LANE)

    assert UNDECLARED_LANE in str(excinfo.value)


def test_the_codex_dialect_keeps_carrying_images_on_its_own_flag():
    """The declaration is read only where the dialect has no flag: codex hands
    the path over itself, and an undeclared codex lane still emits the flag."""
    plan = _plan(
        {"launch": "cli", "command": "codex", "sandbox": "worktree-full"},
        (FIGURE,),
        backend_name="codex",
    )

    assert plan.argv[plan.argv.index("-i") + 1] == FIGURE
    assert FIGURE not in plan.stdin_text


# ── the declaration's two reads ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("declaration", "admits"),
    [
        (["text", "image"], True),
        (["text"], False),
        ([], False),
        (None, False),
        (["IMAGE"], True),
        ([" image "], True),
        ("text,image", True),
    ],
)
def test_the_two_reads_of_a_declaration_agree_on_whether_it_admits_an_image(
    declaration, admits: bool
) -> None:
    """The naming that hands a lane a figure and the selection that admits it to
    a figure review must not disagree: a lane admitted on a spelling the naming
    path does not recognise is a review answering from the diff. They agree
    because both ask one function, whose read strips each entry and case-folds
    it and reads a string as a comma-separated list."""
    lane = _claude_family_lane("clive", declaration)
    config = {"backends": {DECLARED_LANE: lane}}

    named = _backends.declares_image_input(lane)
    declared = flight.declared_input_modalities(config, DECLARED_LANE)

    assert named is admits, (declaration, declared)
    assert ("image" in declared) is admits, (declaration, declared)
    assert flight.missing_input_modalities(config, DECLARED_LANE, "figure") == (
        () if admits else ("image",)
    )


def test_the_naming_read_goes_through_the_flights_normaliser(monkeypatch) -> None:
    """The agreement above is held by one implementation rather than by two
    implementations happening to match: the naming path reads the declaration
    through ``reckon.flight``, so a change that gives one read a new spelling
    cannot leave the other behind."""
    seen: list[object] = []
    real = flight.normalise_input_modalities

    def recording(declared):
        seen.append(declared)
        return real(declared)

    monkeypatch.setattr(flight, "normalise_input_modalities", recording)

    assert _backends.declares_image_input({"input_modalities": ["IMAGE"]}) is True
    assert seen == [["IMAGE"]]


# ── the figure listing a prompt may already carry ───────────────────────────


def test_a_prompt_already_carrying_a_listing_is_composed_with_exactly_one():
    """A lane change composes from the run's prompt file, so the composer is
    handed a prompt that already carries a listing. It strips that listing
    before naming the figures this launch carries, so the worker is handed one
    listing -- the current figures -- rather than both."""
    lane = _claude_family_lane("clive", ["text", "image"])
    named = _plan(lane, (FIGURE,), backend_name=DECLARED_LANE)

    recomposed = _plan(
        lane, (OTHER_FIGURE,), backend_name=DECLARED_LANE, prompt=named.stdin_text
    )

    assert recomposed.stdin_text.count(FIGURE_LISTING_MARKER) == 1
    assert OTHER_FIGURE in recomposed.stdin_text
    assert FIGURE not in recomposed.stdin_text


def test_a_prompt_already_carrying_a_listing_composes_to_none_on_codex():
    """The strip runs on every lane, not only on a lane that names its figures:
    moving a run whose prompt carries a listing onto codex, which hands the
    figures over itself, must not leave that stale listing in the prompt."""
    named = _plan(
        _claude_family_lane("clive", ["text", "image"]),
        (FIGURE,),
        backend_name=DECLARED_LANE,
    )

    moved = _plan(
        _claude_family_lane("codex"),
        (FIGURE,),
        backend_name="codex",
        prompt=named.stdin_text,
    )

    assert FIGURE_LISTING_MARKER not in moved.stdin_text
    assert moved.argv[moved.argv.index("-i") + 1] == FIGURE


# ── the resumed run ─────────────────────────────────────────────────────────


def _record(run_id: str, argv: list[str], images: list[str]) -> dict:
    """A finished run as a resume finds it: its argv recorded, its node's
    figures still on the record."""
    return {
        "run_id": run_id,
        "backend": DECLARED_LANE,
        "dialect": DECLARED_LANE,
        "argv": argv,
        "launch": "cli",
        "node": {"images": images},
    }


def test_backend_settings_carry_the_lanes_declaration_and_the_launch_names_the_image(
    tmp_path: Path,
) -> None:
    """A resume rebuilds the run's backend from the configured lane, and the
    declaration has to survive that rebuild. Without it the rebuilt lane looks
    undeclared, so the resumed turn refuses the figure the run was dispatched
    with -- and a resumed figure run is exactly the turn that has to keep
    naming it."""
    harness = tmp_path / "bin" / DECLARED_LANE
    harness.parent.mkdir(parents=True)
    harness.write_text("#!/bin/sh\nexec cat\n", encoding="utf-8")
    harness.chmod(0o755)
    config = {
        "backends": {
            DECLARED_LANE: _claude_family_lane(str(harness), ["text", "image"]),
            UNDECLARED_LANE: _claude_family_lane("claude"),
        }
    }
    record = _record("r-named-images", [str(harness)], [FIGURE])

    settings = sessions_module._backend_settings(record, config)

    assert "image" in settings["input_modalities"]

    plan = _plan(settings, (FIGURE,), backend_name=DECLARED_LANE)

    assert FIGURE in plan.stdin_text


def test_backend_settings_declare_nothing_for_a_lane_that_declares_nothing(
    tmp_path: Path,
) -> None:
    """The discriminating pair for the carry: the same record read against a
    lane with no declaration yields no modalities, so the carry is what the
    declaration is, not a value this rebuild invents."""
    harness = tmp_path / "bin" / DECLARED_LANE
    harness.parent.mkdir(parents=True)
    harness.write_text("#!/bin/sh\nexec cat\n", encoding="utf-8")
    harness.chmod(0o755)
    config = {"backends": {DECLARED_LANE: _claude_family_lane(str(harness))}}
    record = _record("r-undeclared-images", [str(harness)], [FIGURE])

    settings = sessions_module._backend_settings(record, config)

    assert not settings.get("input_modalities")


# ── the figure review's lane selection ──────────────────────────────────────


def test_a_figure_review_offers_the_only_lane_that_declares_image_input():
    """The declaration is the whole of the lane's eligibility: a flight whose
    only image-declaring lane is the local one offers that lane, and the
    metered lane that declares nothing is not offered beside it."""
    config = {
        "default_backend": DECLARED_LANE,
        "local_backend": DECLARED_LANE,
        "backends": {
            DECLARED_LANE: _claude_family_lane("clive", ["text", "image"]),
            "metered": _claude_family_lane("codex"),
        },
        "roles": {"implement": {}, "review": {}},
        "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
    }

    candidates = recovery._review_lane_candidates(config, profile="figure")

    assert candidates == [DECLARED_LANE]
    assert recovery._review_lane_candidates(config, profile="text") == [
        DECLARED_LANE,
        "metered",
    ]
