"""A batch entry faces the write-scope fence a direct dispatch applies.

``_require_write_paths_in_authority`` confines a node's declared writes to the
work repository, the repositories the dispatch authority registers, and the
durable delivery directories. A direct dispatch runs it while planning, so a
write path outside those roots is refused before anything is launched. A batch
entry is stored by ``queue_requests`` and only judged by the node contract, so
an outside path was queued and could then never launch — the same node was
refused singly and accepted in a batch.

Both arms here name one outside path, and the comparison is between two
refusals of one call: the same path and the same scope rule, with no request
stored. The checkout, plan and crew-home fixtures come from
``tests.test_every_path_meets_the_node_contract``, which drives the same two
paths for the contract's other properties.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from tests.test_every_path_meets_the_node_contract import (
    SESSION,
    _entry,
    _pointers,
    _queue,
)
from tests.test_every_path_meets_the_node_contract import (
    crew_home as crew_home,  # noqa: PLC0414 - the shared fixture
)
from tests.test_every_path_meets_the_node_contract import (
    repository as repository,  # noqa: PLC0414 - the shared fixture
)

#: Each case drives queueing and the CLI through the real crew home, so a
#: fixture that blocks would stall an arm rather than fail it.
pytestmark = pytest.mark.timeout(120)

#: The scope rule both paths must name, as ``mounts.json`` names it.
SCOPE_RULE = "outside the authorised work repository"


def _direct_refusal(entry: dict, repository: Path) -> tuple[int, str]:
    """The single dispatch path's refusal of one entry, as the CLI reports it.

    The preview is the same call a launch makes, so it reaches the same fence
    and answers on its JSON channel; the refusal sentence is read back from
    there for comparison with the batch row.
    """
    arguments = [
        "crew",
        "dispatch",
        "--dry-run",
        "--project",
        str(entry["project"]),
        "--plan",
        str(entry["plan"]),
        "--section",
        str(entry["section"]),
        "--role",
        str(entry["role"]),
        "--spec-level",
        str(entry["spec_level"]),
        "--node",
        str(entry["node"]),
        "--goal",
        str(entry["goal"]),
        "--done-when",
        str(entry["done_when"]),
        "--time-budget",
        str(entry["time_budget"]),
        "--session",
        SESSION,
        "--repo",
        str(repository),
        "--manifest",
        str(repository / "manifest.md"),
    ]
    for path in entry.get("write_paths") or ():
        arguments += ["--write-path", str(path)]
    result = CliRunner().invoke(cli_module.main, arguments)
    if result.exception is not None and not isinstance(result.exception, SystemExit):
        raise result.exception
    first = result.output.strip().splitlines()[0]
    return result.exit_code, str(json.loads(first).get("detail") or "")


def test_the_batch_and_a_direct_dispatch_refuse_the_same_outside_write_path(
    crew_home: Path, repository: Path, tmp_path: Path
) -> None:
    """One outside path, refused by both paths in the same words."""
    outside = tmp_path / "unclaimed" / "artifact.json"
    entry = _entry(node="outside-path", write_paths=[str(outside)])
    before = _pointers()

    result = _queue(repository, [entry])

    assert result["refused"] == 1, result
    assert result["queued"] == 0, result
    batch_reason = str(result["results"][0]["reason"])
    assert str(outside) in batch_reason, batch_reason
    assert SCOPE_RULE in batch_reason, batch_reason
    # A refused entry never becomes a request, so no launch can spend a queue
    # position on it and no obligation reports it as waiting.
    assert _pointers() == before, "a refused batch entry writes no request"

    exit_code, direct_detail = _direct_refusal(entry, repository)

    assert exit_code != 0, direct_detail
    assert str(outside) in direct_detail, direct_detail
    assert SCOPE_RULE in direct_detail, direct_detail
    assert direct_detail == batch_reason, (direct_detail, batch_reason)
    assert _pointers() == before, "the refused direct dispatch stores nothing"


def test_a_batch_entry_inside_the_repository_is_queued(
    crew_home: Path, repository: Path
) -> None:
    """The fence refuses the outside path and not the node's own repository."""
    before = _pointers()

    result = _queue(repository, [_entry(node="inside-path")])

    assert result["refused"] == 0, result
    assert result["queued"] == 1, result
    stored_row = result["results"][0]
    assert f"{stored_row['run_id']}.json" in _pointers()
    assert len(_pointers()) == len(before) + 1
