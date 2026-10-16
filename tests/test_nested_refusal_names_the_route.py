"""The nested-launch refusal and the worker digest name both routes out.

A worker inside the fleet allocation meets one of two texts when a launch is
refused: the shim's refusal, or the compute section its role digest carries.
Both must name the same two legitimate routes — a separate partition job for
heavy work that does not belong inside the allocation, and the explicit opt-in
for a launch that is meant to be nested — because a refusal that names no route
reads as "no route exists", and that is how a worker blocks. The partition
route carries its submitting verb, ``sbatch``, and both texts say that an
``srun`` from inside the allocation runs as a step of the held job, so the
opt-in is never taken as the way to reach a separate partition.

The route names and the submitting verb are read from
:mod:`reckon.nested_launch`'s own constants, so the two texts are compared
against one declaration rather than against literals. The policy the digest is
composed over states the fleet rule and names neither route, which is what
makes the digest's own sentence visible: the canonical file lives outside this
repository and stays untouched.

The declared negative control reverts :data:`reckon.nested_launch.PARTITION_ROUTE`
to its bare "a separate partition job" in memory, dropping the ``sbatch`` verb;
every case that asserts the verb is carried must fail there.
"""

from __future__ import annotations

import re

import pytest

from reckon import nested_launch as nested
from reckon.crew import dispatch_launch
from reckon.crew import worker_digest as digest
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

# The policy body the digest is composed over. It states the fleet rule the way
# the canonical file does and names neither route, so a route that appears in
# the composed digest is one the digest added.
FLEET_BODY = (
    "A session whose shell has SLURM_JOB_ID set is already on compute. It runs\n"
    "its heavy gates directly and never submits them to a debug partition.\n"
)

# The two routes, by the module's own names.
ROUTES = frozenset({nested.PARTITION_ROUTE, nested.OVERRIDE_ENV})


def named_routes(text: str) -> set[str]:
    """The declared routes ``text`` names, read from the module's constants.

    The partition route counts as named only when the phrase and one of the
    example partitions appear; the opt-in counts when its variable name
    appears.
    """
    named: set[str] = set()
    if nested.PARTITION_ROUTE in text and any(
        example in text for example in nested.PARTITION_EXAMPLES
    ):
        named.add(nested.PARTITION_ROUTE)
    if nested.OVERRIDE_ENV in text:
        named.add(nested.OVERRIDE_ENV)
    return named


def names_sbatch_as_the_submission(text: str) -> bool:
    """Whether ``text`` names the partition route together with its verb.

    The route counts only when the declared phrase appears *and* the
    declaration itself carries the submitting verb, so reverting the route to a
    bare "a separate partition job" fails here however else the text may
    mention ``sbatch``.
    """
    return (
        nested.PARTITION_ROUTE in text
        and nested.PARTITION_ROUTE_VERB in nested.PARTITION_ROUTE
    )


def says_a_nested_srun_is_a_step(text: str) -> bool:
    """Whether ``text`` says a nested ``srun`` runs as a step of the held job."""
    return nested.NESTED_SRUN_SENTENCE in text


def block(text: str, header: str) -> str:
    """One retained block of a digest, from its header line to the next."""
    match = re.search(rf"(?m)^{re.escape(header)}", text)
    assert match is not None, header
    rest = text[match.end() :]
    following = re.search(r"(?m)^#{2,4} ", rest)
    return rest if following is None else rest[: following.start()]


def test_the_partition_route_carries_its_submitting_verb() -> None:
    """The declaration names the verb, so no text can restate the route wrongly."""
    assert nested.PARTITION_ROUTE_VERB == "sbatch"
    assert nested.PARTITION_ROUTE_VERB in nested.PARTITION_ROUTE


@pytest.mark.parametrize("tool", TOOLS)
def test_the_refusal_names_both_routes_and_the_submitting_verb(tool: str) -> None:
    text = nested.refusal_message(tool, INSIDE)

    assert named_routes(text) == set(ROUTES), text
    assert names_sbatch_as_the_submission(text), text
    assert says_a_nested_srun_is_a_step(text), text


def test_the_digested_compute_section_names_both_routes_and_the_verb() -> None:
    """The policy names neither route; the composed digest names both."""
    assert named_routes(FLEET_BODY) == set()
    policy = f"{digest.FLEET_COMPUTE_HEADER}\n\n{FLEET_BODY}"
    text = digest.generate(
        "implement",
        policy,
        section_map={"implement": (digest.FLEET_COMPUTE_HEADER,)},
    )

    section = block(text, digest.FLEET_COMPUTE_HEADER)
    assert named_routes(section) == set(ROUTES), section
    assert names_sbatch_as_the_submission(section), section
    assert says_a_nested_srun_is_a_step(section), section


def test_patching_the_route_constant_moves_the_host_line_and_the_sentence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A route declared changed in memory moves both composed texts with it."""
    run_directory = "/runs/route-constant"
    original = nested.PARTITION_ROUTE
    assert names_sbatch_as_the_submission(
        dispatch_launch._worker_host_line(INSIDE, run_directory)
    )
    assert names_sbatch_as_the_submission(digest.compute_routes_sentence())

    patched = "a distinct partition route named zz9"
    monkeypatch.setattr(nested, "PARTITION_ROUTE", patched)

    host_line = dispatch_launch._worker_host_line(INSIDE, run_directory)
    sentence = digest.compute_routes_sentence()
    assert patched in host_line and original not in host_line, host_line
    assert patched in sentence and original not in sentence, sentence


CANONICAL = digest.canonical_path()
requires_canonical = pytest.mark.skipif(
    not CANONICAL.exists(),
    reason=f"{digest.CANONICAL_SOURCE} is not present on this host",
)


@requires_canonical
def test_the_role_digests_name_both_routes_in_their_compute_section() -> None:
    canonical = CANONICAL.read_text(encoding="utf-8")

    for role in ("implement", "test"):
        text = digest.generate(role, canonical)
        section = block(text, digest.FLEET_COMPUTE_HEADER)
        assert named_routes(section) == set(ROUTES), role
        assert names_sbatch_as_the_submission(section), role
        assert says_a_nested_srun_is_a_step(section), role


@requires_canonical
def test_a_role_without_the_compute_section_carries_neither_route() -> None:
    """The report role retains no fleet block, so it gets no route sentence."""
    canonical = CANONICAL.read_text(encoding="utf-8")
    text = digest.generate("report", canonical)

    assert digest.FLEET_COMPUTE_HEADER not in text
    assert named_routes(text) == set()
