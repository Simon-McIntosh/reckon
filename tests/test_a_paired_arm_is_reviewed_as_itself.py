"""A paired arm's review is named by the arm's own run id.

A paired dispatch launches two arms under one node id, so a review node id
composed from that node id is ambiguous: dispatch resolves it by matching the
node id against the live fleet and takes the first match, which is the arm the
listing happens to return first rather than the arm being reviewed. Composing
the node id from the reviewed arm's run id makes each arm's review name itself,
and refusing an ambiguous bare node id at the dispatch grant stops a record
path from being fenced to the wrong arm's directory.

Every case is driven end to end: the crew home is a temporary directory holding
three live pointers — a paired primary, its shadow, and an unpaired run — and
the answer is read from those pointers through the real composer and the real
dispatch grant.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from reckon.crew import runs
from reckon.crew.runs import _write_json, pointer_path, run_dir

review_subject = importlib.import_module("reckon.crew.recovery_review_subject")
review_delivery = importlib.import_module("reckon.crew.recovery_review_delivery")
dispatch_admission = importlib.import_module("reckon.crew.dispatch_admission")

PROJECT = "paired-review-fixture"

PRIMARY = "r-20261018T000000000001-arm-alpha"
SHADOW = "r-20261018T000000000002-arm-beta"
SOLO = "r-20261018T000000000003-solo-run"

PAIR_NODE = "pair-node"
SOLO_NODE = "solo-node"

# The review node-id prefix, spelled here rather than imported so the test pins
# the identity grammar rather than whatever the module happens to export.
REVIEW_PREFIX = "review-of-"


def _record(run_id: str, node_id: str, **extra) -> dict:
    """One live pointer shaped as the composer and the grant read it."""
    return {
        "run_id": run_id,
        "project": PROJECT,
        "role": "implement",
        "session": "coordinator-fixture",
        "node": {
            "id": node_id,
            "plan": "",
            "section": "",
            "time_budget": "20m",
            "write_paths": [],
        },
        **extra,
    }


@pytest.fixture()
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """An isolated crew home holding a paired primary, its shadow and a solo run."""
    real_home = runs.crew_home()
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    primary = _record(PRIMARY, PAIR_NODE, paired_shadow_run_id=SHADOW)
    shadow = _record(
        SHADOW,
        PAIR_NODE,
        lineage={"kind": "shadow", "primary_run_id": PRIMARY},
    )
    solo = _record(SOLO, SOLO_NODE)
    for record in (primary, shadow, solo):
        _write_json(pointer_path(record["run_id"]), record)

    return {
        "real_home": real_home,
        "primary": primary,
        "shadow": shadow,
        "solo": solo,
    }


def _composed(world: dict, key: str) -> dict:
    return review_subject._review_dispatch_fields(world[key])


def test_each_paired_arm_review_names_its_own_run(world: dict) -> None:
    """A paired arm's review node id carries the arm's run id, not the shared node."""
    for key, run_id in (("primary", PRIMARY), ("shadow", SHADOW)):
        fields = _composed(world, key)
        assert fields["node_id"].startswith(f"{REVIEW_PREFIX}{run_id}")
        assert fields["source_node"] == PAIR_NODE
        assert dispatch_admission._reviewed_run_id_for_node(fields["node_id"]) == run_id


def test_an_unpaired_run_review_keeps_its_node_id_source(world: dict) -> None:
    """An unpaired run's review node id and its resolution are unchanged."""
    fields = _composed(world, "solo")
    assert fields["node_id"].startswith(f"{REVIEW_PREFIX}{SOLO_NODE}")
    assert fields["source_node"] == SOLO_NODE
    assert dispatch_admission._reviewed_run_id_for_node(fields["node_id"]) == SOLO


def test_a_bare_paired_node_id_is_refused_by_the_grant(world: dict) -> None:
    """An ambiguous bare node id names no arm, while the unflagged reader keeps one."""
    bare = f"{REVIEW_PREFIX}{PAIR_NODE}"
    assert dispatch_admission._reviewed_run_id_for_node(bare) == PAIR_NODE
    unflagged = review_delivery._resolved_reviewed_run_id(
        {"node": {"id": bare}}, PROJECT
    )
    assert unflagged in {PRIMARY, SHADOW}


def test_the_record_grant_refuses_an_ambiguous_source(world: dict) -> None:
    """A record path is fenced to an arm only when the source names that arm."""
    primary_records = run_dir(PRIMARY) / "review-records"
    shadow_records = run_dir(SHADOW) / "review-records"
    primary_records.mkdir(parents=True, exist_ok=True)
    shadow_records.mkdir(parents=True, exist_ok=True)
    primary_path = str(primary_records / f"{PRIMARY}.json")
    shadow_path = str(shadow_records / f"{SHADOW}.json")

    # The bare source names no run directory, so both arms' record paths refuse.
    assert (
        dispatch_admission._review_record_directory_grant(
            primary_path, reviewed_run_id=PAIR_NODE
        )
        is None
    )
    assert (
        dispatch_admission._review_record_directory_grant(
            shadow_path, reviewed_run_id=PAIR_NODE
        )
        is None
    )
    # Each arm's own run id grants its own directory and refuses the other's, so
    # the refusal above is ambiguity and not a broken helper.
    assert (
        dispatch_admission._review_record_directory_grant(
            primary_path, reviewed_run_id=PRIMARY
        )
        == primary_records.resolve()
    )
    assert (
        dispatch_admission._review_record_directory_grant(
            shadow_path, reviewed_run_id=PRIMARY
        )
        is None
    )


def test_the_real_crew_home_is_untouched(world: dict) -> None:
    """The pointers were written to the temporary home, not the real one."""
    real_home = world["real_home"]
    for run_id in (PRIMARY, SHADOW, SOLO):
        assert not (real_home / "live" / f"{run_id}.json").exists()
        assert not (real_home / "runs" / run_id).exists()
    # Positive control: the writes landed somewhere, and that place is the temp home.
    for run_id in (PRIMARY, SHADOW, SOLO):
        assert pointer_path(run_id).is_file()
