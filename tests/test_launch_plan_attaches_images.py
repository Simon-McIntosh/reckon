"""A dispatch's figures reach the command line, or the launch refuses.

The plan a launcher runs is the object asserted on here, never a helper
returned by one: a direct call proves the function, not the wiring. So every
assertion reads ``launch_plan(...).argv`` -- the exact argument vector the
launcher would execute -- and the attachment is judged where it would actually
appear.

Two properties, and one of them must be able to fail:

* a codex backend handed two figures emits one ``-i <path>`` per file, each
  flag immediately followed by its own path, and emits none when handed none;
* a dialect with no way to hand an image to its harness -- claude, clive, and
  an in-harness lane -- raises rather than dropping the figure, and the refusal
  names the lane so the operator learns which configured lane refused.

The declared negative control makes the codex dialect's argv ignore its
``images`` argument. That mutation leaves the argv of a figureless launch
unchanged, so the empty-tuple test still passes while the attachment tests
fail -- the discrimination sits on the flag, not on the lane count.
"""

from __future__ import annotations

import pytest

from reckon import _backends

CODEX = {"launch": "cli", "command": "codex", "sandbox": "worktree-full"}
CLAUDE = {"launch": "cli", "command": "claude", "sandbox": "worktree-full"}
CLIVE = {"launch": "cli", "command": "clive", "sandbox": "worktree-full"}
NATIVE_HARNESS = {"launch": "in-harness"}


def _plan(backend, images, *, backend_name="b", **kwargs):
    return _backends.launch_plan(
        backend_name=backend_name,
        backend=backend,
        prompt="review the figure",
        worktree="/wt",
        images=images,
        **kwargs,
    )


def _image_pairs(argv):
    return [
        (flag, argv[index + 1])
        for index, flag in enumerate(argv)
        if flag == "-i" and index + 1 < len(argv)
    ]


def test_two_figures_emit_two_flags_each_carrying_its_path():
    plan = _plan(CODEX, ("figures/first.png", "figures/second.png"))

    assert plan.argv.count("-i") == 2
    assert _image_pairs(plan.argv) == [
        ("-i", "figures/first.png"),
        ("-i", "figures/second.png"),
    ]


def test_a_figure_flag_precedes_the_prompt_terminator():
    """The flags are exec options; the trailing ``-`` is how the prompt travels
    on stdin, so an image after it would be read as part of the prompt."""
    plan = _plan(CODEX, ("figures/first.png",))

    stdin_marker = plan.argv.index("-")
    for index, flag in enumerate(plan.argv):
        if flag == "-i":
            assert index < stdin_marker


def test_a_figure_precedes_the_resume_subcommand():
    """resume takes only a codex session id and the prompt, so an option after
    it is rejected outright rather than carried into the resumed turn."""
    plan = _plan(
        CODEX,
        ("figures/first.png",),
        resume_session="session-1",
        final_message_path="/wt/final.txt",
    )

    assert plan.argv.index("-i") < plan.argv.index("resume")


def test_no_figures_emit_no_flag():
    plan = _plan(CODEX, ())

    assert "-i" not in plan.argv
    assert _plan(CLAUDE, ()).argv[0] == "claude"


@pytest.mark.parametrize(
    "backend,backend_name,lane",
    [
        (CLAUDE, "claude", "claude"),
        (CLIVE, "clive", "clive"),
        (NATIVE_HARNESS, "native-harness", "native-harness"),
    ],
)
def test_a_lane_that_cannot_carry_the_figure_refuses_by_name(
    backend, backend_name, lane
):
    """A lane with no image flag must refuse the figure rather than answer
    from the filename. The refusal names the configured lane: the local
    harness that shares the claude flag grammar reports the lane the operator
    configured, not the dialect family behind it."""
    with pytest.raises(_backends.BackendError) as excinfo:
        _plan(backend, ("figures/only.png",), backend_name=backend_name)

    assert lane in str(excinfo.value)


def test_a_text_only_lane_with_no_figures_is_unaffected():
    """The refusal is a response to the figure, never to the lane: a text-only
    lane serving a figureless node composes its argv exactly as before."""
    plan = _plan(CLAUDE, ())

    assert plan.argv[0] == "claude"
    assert "review the figure" not in plan.argv
