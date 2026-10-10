"""Every option Jev is offered carries a key that is distinct and honest.

Two properties hold the offered options together. Each key identifies exactly
one configured backend, so every candidate in a batch is offered and each is
resolvable from Jev's answer; and a key names only what the candidate carries,
so a candidate with no model is never described as having one. Each test here
fixes one of them by building the candidate that would break it.
"""

import pytest

from reckon.crew.picker import prompts
from reckon.crew.picker.types import Candidate


def candidate(
    backend: str, *, family: str, model: str | None, **overrides: object
) -> Candidate:
    values: dict = {
        "backend": backend,
        "family": family,
        "model": model,
        "effort": "high",
        "local": False,
        "availability": "served",
        "utilisation_pct": None,
        "burn_multiple": None,
        "pace_allowance": None,
        "resets_at": None,
        "worker_slots": None,
        "congestion": None,
        "outcomes": {"passed": 0, "failed": 0, "not-run": 0, "unknown": 0},
    }
    values.update(overrides)
    return Candidate(**values)


def resolve(offered, choice):
    """The single candidate a chosen key resolves to, as the picker resolves it."""
    matches = [c for c in offered if prompts.option_key(c) == choice]
    assert len(matches) == 1, f"key {choice!r} resolved to {len(matches)} candidates"
    return matches[0]


def test_two_backends_sharing_a_lane_and_a_model_are_both_offered():
    """Two backends on one lane running one model each get their own option.

    The pair alone names neither, so keying by the pair would collapse them and
    drop the second; each must be offered and each must resolve to its backend.
    """
    offered = [
        candidate("claude-primary", family="claude", model="opus"),
        candidate("claude-secondary", family="claude", model="opus"),
    ]

    keys = [prompts.option_key(item) for item in offered]
    assert len(set(keys)) == len(offered), keys

    entries = prompts.build_questions(offered)["criteria_entries"]
    assert set(entries) == set(keys)
    for item, key in zip(offered, keys, strict=True):
        assert entries[key]["backend"] == item.backend
        assert resolve(offered, key) is item


def test_a_modelless_candidate_does_not_name_its_backend_a_model():
    """A candidate with no model offers a key that carries no model part."""
    modelless = candidate("amine", family="claude", model=None)
    entry = prompts.build_questions([modelless])["criteria_entries"]
    key = prompts.option_key(modelless)

    assert entry[key]["model"] is None
    lane, _, model = key.partition(":")
    assert lane == "claude"
    assert model != "amine", "the key offers the backend where a model belongs"


def test_two_candidates_under_one_key_are_refused_not_collapsed():
    """Two offerings the key cannot tell apart are refused, not silently merged."""
    twin = candidate("claude-primary", family="claude", model="opus")
    with pytest.raises(ValueError, match="one option key"):
        prompts.build_questions(
            [twin, candidate("claude-primary", family="claude", model="opus")]
        )
