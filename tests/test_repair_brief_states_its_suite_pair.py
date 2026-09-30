"""A repair brief states its suite pair only when the run it repairs was armed.

The repair's own review reconciles its added-failure count by comparing the two
suite observations its manifest records — a ``baseline_suite`` at the repair's
base and an ``after_suite`` at its head — against the suite the reviewed run was
measured with. Both arms are only a measurement when the reviewed run was itself
armed: a repair of an unarmed run has no observed pair to reconcile against, so
its brief must name neither arm rather than ask for a baseline it cannot compare
with anything.

The defect this file guards is a brief that states the pair unconditionally. The
composed node's done-when carries the command when one was inherited and must
name neither arm when none was; naming the arms without the command leaves the
repair measuring a different suite from the run it answers.
"""

from __future__ import annotations

from reckon.crew import repair

RUN_ID = "r-work"
COMMAND = "uv run pytest -q tests/test_one.py"


def _review() -> dict:
    return {
        "project": "sample",
        "reviewed_run_id": RUN_ID,
        "reviewed_head_sha": "a" * 40,
        "status": "parsed",
        "findings": [
            {"file": "reckon/crew/recovery.py", "line": "1748", "text": "a defect"}
        ],
    }


def test_an_armed_repair_brief_states_both_suite_arms() -> None:
    """An inherited command makes the brief name both arms with that command.

    The pair the reviewed run's suite commands reconciliation from is only
    meaningful when it is the reviewed run's own suite, so the brief names the
    exact command — quoted literally — beside the two arms it is run for.
    """
    node = repair.compose_repair_node(_review(), suite_command=COMMAND)
    assert node is not None
    done_when = str(node["done_when"])
    assert "baseline_suite" in done_when
    assert "after_suite" in done_when
    assert COMMAND in done_when


def test_an_unarmed_repair_brief_names_neither_suite_arm() -> None:
    """No inherited command leaves the brief naming neither arm.

    A repair of an unarmed run has no observed pair to reconcile, so asking for
    a baseline it cannot compare against would leave the repair measuring a
    different suite from the run it answers.
    """
    node = repair.compose_repair_node(_review())
    assert node is not None
    done_when = str(node["done_when"])
    assert "baseline_suite" not in done_when
    assert "after_suite" not in done_when
