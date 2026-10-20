"""A review that needs an image takes a lane that declares image input.

``review_excluded_backends`` keeps every review off the metered lanes, and no
locally served lane declares image input, so a review that requires a modality
would otherwise find no lane at all. The exemption lives in the lane ordering,
before the candidate list is built: a review that requires a modality draws
only from the lanes that declare it, and such a lane is admitted whatever the
exclusion says. A review that requires nothing keeps the exclusion unchanged.

Both properties are argued with the discriminating negative: remove the
exemption and the image-only case falls back to the eager exclusion and returns
no candidate, while the text case returns the same lane it always did.
"""

from __future__ import annotations

from reckon.crew import recovery

IMAGE_BACKEND = "codex-sol"
SECOND_IMAGE_BACKEND = "codex-luna"
LOCAL_BACKEND = "local-flash"


def _backend(command: str, modalities: list[str] | None = None) -> dict:
    settings = {
        "launch": "cli",
        "command": command,
        "model": "some-model",
        "effort": "high",
        "sandbox": "worktree-full",
        "session_reuse": True,
        "time_budget": "25m",
    }
    if modalities is not None:
        settings["input_modalities"] = modalities
    return settings


# The one lane that reads images is the one the exclusion withholds, which is
# the shape the fleet actually runs: no locally served lane declares image
# input, so a figure review can only run on the metered lane the exclusion names.
CONFIG = {
    "default_backend": LOCAL_BACKEND,
    "local_backend": LOCAL_BACKEND,
    "backends": {
        LOCAL_BACKEND: _backend("local-cli"),
        IMAGE_BACKEND: _backend("codex", ["text", "image"]),
        SECOND_IMAGE_BACKEND: _backend("codex", ["text", "image"]),
    },
    "roles": {"implement": {}, "review": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
    recovery.REVIEW_EXCLUDED_BACKENDS_KEY: [IMAGE_BACKEND, SECOND_IMAGE_BACKEND],
}

# No configured lane declares image input, so a figure review has nowhere to go.
CONFIG_WITHOUT_IMAGE = {
    **CONFIG,
    "backends": {LOCAL_BACKEND: _backend("local-cli")},
    recovery.REVIEW_EXCLUDED_BACKENDS_KEY: [],
}


def test_a_figure_review_takes_the_image_lanes_despite_the_exclusion():
    candidates = recovery._review_lane_candidates(CONFIG, profile="figure")

    assert candidates == [SECOND_IMAGE_BACKEND, IMAGE_BACKEND]


def test_the_image_lanes_lead_when_the_owning_lane_is_one_of_them():
    candidates = recovery._review_lane_candidates(
        CONFIG, owning_backend=SECOND_IMAGE_BACKEND, profile="figure"
    )

    assert candidates == [SECOND_IMAGE_BACKEND, IMAGE_BACKEND]


def test_a_text_review_still_excludes_the_image_lanes():
    for profile in (None, "text"):
        candidates = recovery._review_lane_candidates(CONFIG, profile=profile)

        assert candidates == [LOCAL_BACKEND]


def test_a_figure_review_with_no_image_lane_gets_no_lane_naming_the_modality():
    assert (
        recovery._review_lane_candidates(CONFIG_WITHOUT_IMAGE, profile="figure") == []
    )

    reason = recovery._no_lane_reason(
        "r-test",
        CONFIG_WITHOUT_IMAGE,
        kind="review",
        profile="figure",
    )

    assert "image" in reason


def test_a_text_review_gets_a_lane_where_a_figure_review_gets_none():
    """The discriminating pair: the same config yields a lane for the review
    that needs nothing and no lane for the review that needs an image, so a
    selection that ignored the profile would answer both the same way."""
    assert recovery._review_lane_candidates(CONFIG_WITHOUT_IMAGE, profile="text") == [
        LOCAL_BACKEND
    ]
    assert (
        recovery._review_lane_candidates(CONFIG_WITHOUT_IMAGE, profile="figure") == []
    )
