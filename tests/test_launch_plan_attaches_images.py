"""A dispatch's figures reach the command line, or the launch refuses.

The plan a launcher runs is the object asserted on here, never a helper
returned by one: a direct call proves the function, not the wiring. So every
assertion reads ``launch_plan(...).argv`` -- the exact argument vector the
launcher would execute -- and the attachment is judged where it would actually
appear.

Three properties, each of which must be able to fail:

* a codex backend handed two figures emits one ``-i <path>`` per file, each
  flag immediately followed by its own path, and emits none when handed none;
* a claude-family lane that declares image input carries each figure's path in
  the prompt its harness reads on stdin -- the only channel such a dialect has
  -- with an instruction to open it, and no path reaches argv;
* a dialect with no way to hand an image to its harness and no declaration --
  the claude lane and an in-harness lane -- raises rather than dropping the
  figure, and the refusal names the lane so the operator learns which
  configured lane refused.

The clive lane's declaration is what separates the second from the third: the
two lanes share one flag grammar, so a change keyed on the dialect would move
both and leave no lane refusing.

Two mutations, each dropping one half of the delivery. Making the codex
dialect's argv ignore its ``images`` argument leaves a figureless launch
unchanged, so the empty-tuple test still passes while the attachment tests
fail -- the discrimination sits on the flag, not on the lane count. Removing
the naming from :func:`launch_plan` leaves a figureless launch unchanged, so a
declared lane serving a figureless node still passes while the prompt-naming
test fails.
"""

from __future__ import annotations

import pytest

from reckon import _backends

CODEX = {"launch": "cli", "command": "codex", "sandbox": "worktree-full"}
CLAUDE = {"launch": "cli", "command": "claude", "sandbox": "worktree-full"}
# clive speaks claude's flags but declares image input, so a figure reaches it
# in the prompt rather than on a flag its harness does not have.
CLIVE = {
    "launch": "cli",
    "command": "clive",
    "sandbox": "worktree-full",
    "input_modalities": ["text", "image"],
}
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
    """The dialect argv itself, so the fence wrapper is not composed here: the
    fence is a wrapper around a launch and is asserted on its own elsewhere."""
    plan = _plan(CODEX, (), fence=False)

    assert "-i" not in plan.argv
    assert _plan(CLAUDE, (), fence=False).argv[0] == "claude"


@pytest.mark.parametrize(
    "backend,backend_name,lane",
    [
        (CLAUDE, "claude", "claude"),
        (NATIVE_HARNESS, "native-harness", "native-harness"),
    ],
)
def test_a_lane_that_cannot_carry_the_figure_refuses_by_name(
    backend, backend_name, lane
):
    """A lane that declares no image input must refuse the figure rather than
    answer from the filename. The refusal names the configured lane: a lane
    that shares the claude flag grammar reports the lane the operator
    configured, not the dialect family behind it."""
    with pytest.raises(_backends.BackendError) as excinfo:
        _plan(backend, ("figures/only.png",), backend_name=backend_name)

    assert lane in str(excinfo.value)


def test_a_declared_claude_family_lane_names_each_figure_in_its_prompt():
    """clive carries no image flag, so the path travels in the prompt its
    harness reads on stdin -- the only channel it has -- and argv stays free of
    it. Both halves matter: a path that never reaches the worker leaves the
    figure unopened, and a path on argv would be handed to a harness that would
    reject it."""
    plan = _plan(CLIVE, ("figures/only.png",), backend_name="clive", fence=False)

    assert "figures/only.png" in plan.stdin_text
    assert "Read" in plan.stdin_text
    assert "figures/only.png" not in plan.argv
    assert plan.stdin_text.startswith("review the figure")


def test_a_declared_lane_given_no_figure_carries_the_prompt_unchanged():
    """The naming is a response to the figure, never to the lane, so a declared
    lane serving a figureless node composes exactly the prompt it always did."""
    plan = _plan(CLIVE, (), backend_name="clive", fence=False)

    assert plan.stdin_text == "review the figure"


def test_a_text_only_lane_with_no_figures_is_unaffected():
    """The refusal is a response to the figure, never to the lane: a text-only
    lane serving a figureless node composes its argv exactly as before."""
    plan = _plan(CLAUDE, (), fence=False)

    assert plan.argv[0] == "claude"
    assert "review the figure" not in plan.argv
