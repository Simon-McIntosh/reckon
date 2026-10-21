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
    figure-review candidate.

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

from reckon import _backends
from reckon.crew import recovery

sessions_module = importlib.import_module("reckon.crew.dispatch_sessions")

FIGURE = "run-figures/image-0.png"

DECLARED_LANE = "clive"
UNDECLARED_LANE = "claude"


def _claude_family_lane(command: str, modalities: list[str] | None = None) -> dict:
    lane = {"launch": "cli", "command": command, "sandbox": "worktree-full"}
    if modalities is not None:
        lane["input_modalities"] = modalities
    return lane


def _plan(backend: dict, images=(), *, backend_name: str = "lane", fence: bool = False):
    return _backends.launch_plan(
        backend_name=backend_name,
        backend=backend,
        prompt="review the figure",
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
