"""The nested-launch refusal and the worker's host line name the routes out.

A worker inside the fleet allocation whose launch is refused reads the shim's
refusal. It must name the two legitimate routes — a separate partition job for
heavy work that does not belong inside the allocation, and the explicit opt-in
for a launch that is meant to be nested — because a refusal that names no route
reads as "no route exists", and that is how a worker blocks. The partition
route carries its submitting verb, ``sbatch``, and the text says that an
``srun`` from inside the allocation runs as a step of the held job, so the
opt-in is never taken as the way to reach a separate partition.

The route names and the submitting verb are read from
:mod:`reckon.nested_launch`'s own constants, so the texts are compared against
one declaration rather than against literals.

The declared negative control drops the partition route's verb clause in
memory, leaving :data:`reckon.nested_launch.NESTED_SRUN_SENTENCE` untouched and
still mentioning ``sbatch``. The refusal case then fails on its own assertion
that the declared route phrase appears verbatim, because the composed text no
longer carries it.
"""

from __future__ import annotations

import os

import pytest

from reckon import nested_launch as nested
from reckon.crew import dispatch_launch
from reckon.host import HostFacts

INSIDE = HostFacts(
    in_allocation=True,
    job_id="4242",
    step_id=None,
    node="98dci4-clu-2058",
    tmp_filesystem="xfs",
    home_filesystem="gpfs",
    tmp_is_node_local=True,
    reason="",
    sources={},
)

TOOLS = ("srun", "sbatch", "salloc")

# The declaration this file pins, captured once at import. A route changed
# later in the process cannot move this expectation, so a text that stops
# carrying the declared route fails the case on the text itself rather than on a
# comparison against the changed value.
DECLARED_ROUTE = nested.PARTITION_ROUTE
DECLARED_SENTENCE = nested.NESTED_SRUN_SENTENCE

# The two routes, by the module's own names.
ROUTES = frozenset({DECLARED_ROUTE, nested.OVERRIDE_ENV})

# Environment gate for the declared negative control.
ROUTE_MUTATION_ENV = "RECKON_PARTITION_ROUTE_NEGATIVE_CONTROL"


@pytest.fixture(autouse=True)
def _declared_route_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the partition route's verb clause in memory when the control asks.

    Applied after this module has imported and captured the declaration, so the
    composed texts diverge from ``DECLARED_ROUTE`` and the per-text cases fail
    on their own text assertion.
    """
    if os.environ.get(ROUTE_MUTATION_ENV) == "1":
        monkeypatch.setattr(nested, "PARTITION_ROUTE", "a separate partition job")


def named_routes(text: str) -> set[str]:
    """The declared routes ``text`` names, read from the module's constants.

    The partition route counts as named only when the phrase and one of the
    example partitions appear; the opt-in counts when its variable name
    appears.
    """
    named: set[str] = set()
    if DECLARED_ROUTE in text and any(
        example in text for example in nested.PARTITION_EXAMPLES
    ):
        named.add(DECLARED_ROUTE)
    if nested.OVERRIDE_ENV in text:
        named.add(nested.OVERRIDE_ENV)
    return named


def names_the_partition_route(text: str) -> bool:
    """Whether ``text`` carries the declared partition route verbatim."""
    return DECLARED_ROUTE in text


def says_a_nested_srun_is_a_step(text: str) -> bool:
    """Whether ``text`` says a nested ``srun`` runs as a step of the held job."""
    return DECLARED_SENTENCE in text


def test_the_partition_route_declaration_names_its_submitting_verb() -> None:
    """The one case about the declaration: the route spells out its own verb.

    The per-text cases read the verb from the composed text; this case holds the
    declaration they depend on, so a route that stops naming a verb is caught in
    one place rather than being inferred from the texts.
    """
    assert nested.PARTITION_ROUTE_VERB == "sbatch"
    assert nested.PARTITION_ROUTE_VERB in DECLARED_ROUTE
    assert nested.PARTITION_ROUTE_VERB in nested.PARTITION_ROUTE


@pytest.mark.parametrize("tool", TOOLS)
def test_the_refusal_names_both_routes_and_the_submitting_verb(tool: str) -> None:
    text = nested.refusal_message(tool, INSIDE)

    assert named_routes(text) == set(ROUTES), text
    assert names_the_partition_route(text), text
    assert says_a_nested_srun_is_a_step(text), text


def test_patching_the_route_constant_moves_the_host_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A route changed in memory moves the composed host line with it."""
    run_directory = "/runs/route-constant"
    original = nested.PARTITION_ROUTE
    assert original in dispatch_launch._worker_host_line(INSIDE, run_directory)

    patched = "a distinct partition route named zz9"
    monkeypatch.setattr(nested, "PARTITION_ROUTE", patched)

    host_line = dispatch_launch._worker_host_line(INSIDE, run_directory)
    assert patched in host_line and original not in host_line, host_line
