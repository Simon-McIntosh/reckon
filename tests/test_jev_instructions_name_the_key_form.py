"""The routing instructions name the option-key form option_key returns.

Jev picks an option by a key, so the instructions that tell it to "choose only an
offered option" must describe the key in the form the answer is keyed by. An
option names a lane, a model and a backend joined by colons: the backend keeps
two configured backends that share a lane and a model distinct, so a key that
named only the lane and model would leave Jev unable to select the pair it was
shown. Each test here fixes one part of that agreement, and the instructions are
read back through the same ``questions.jinja`` render a pick sends.
"""

import json
import re

from reckon.crew.picker import prompts
from reckon.crew.picker.types import Candidate


def candidate(backend="remote", family="codex-family", model="gpt-codex"):
    return Candidate(
        backend=backend,
        family=family,
        model=model,
        effort="high",
        local=False,
        availability="served",
        utilisation_pct=None,
        burn_multiple=None,
        pace_allowance=None,
        resets_at=None,
        worker_slots=None,
        congestion=None,
        outcomes={"passed": 0, "failed": 0, "not-run": 0, "unknown": 0},
    )


def instructions(cands=None):
    rendered = prompts.render("questions.jinja", candidates=cands or [candidate()])
    return json.loads(rendered)["route"]["instructions"]


def test_instructions_name_a_key_form_with_the_parts_option_key_joins():
    """The named form has as many parts as option_key returns, in its order."""

    key = prompts.option_key(candidate())
    arity = key.count(":") + 1
    assert arity == 3
    named = re.findall(r"<([a-z_]+)>", instructions())
    assert named == ["lane", "model", "backend"][:arity]
    assert "<lane>:<model>:<backend>" in instructions()


def test_the_named_parts_match_the_fields_message_carries():
    """Each named part is a field the state keys a candidate by.

    The form is not decoration: ``lane``, ``model`` and ``backend`` are exactly
    the candidate fields the option entry names, so a form that named another
    word would describe a field no candidate carries.
    """

    key = prompts.option_key(candidate())
    assert key == "codex-family:gpt-codex:remote"
    entry = json.loads(
        prompts.render(
            "questions.jinja", candidates=[candidate()]
        )
    )["route"]["criteria"][key]
    for part in ("lane", "model", "backend"):
        assert part in entry, part


def test_instructions_name_the_serving_lane_facts():
    """The hold guidance names serving_lanes and hold_waits_for."""

    text = instructions()
    assert "serving_lanes" in text
    assert "hold_waits_for" in text
    assert "exactly one lane can serve" in text


def test_a_familyless_candidate_key_is_still_named_by_the_form():
    """A key with no model part is a shorter spelling of the same colon form.

    A candidate that declares no model leaves the model part empty, so its key
    has one fewer part; the instructions describe the three-part form, and the
    shorter key is that form with its middle part absent rather than a different
    grammar.
    """

    key = prompts.option_key(candidate(backend="solo", family="local", model=None))
    assert key == "local:solo"
    assert "<lane>:<model>:<backend>" in instructions()