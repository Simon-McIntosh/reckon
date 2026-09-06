"""Routing evidence is recorded on each promoted row at promotion time.

A measure that reads the worker manifest decays as run directories are
cleaned, so promotion copies three facts onto the ledger row while the
evidence exists: the repository paths the manifest named under ``follow_ons``,
a continuation link naming the run this one continues (or nothing), and a
count of corrections the worker stated against the premises of its brief.
The rows distinguish measured-zero from never-measured, which is what lets a
later touch of already-named paths read differently from a defect.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import _plan_html, crew, ledger
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "proj"
PLAN = "plan-a"


def _write_resource(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'\n<meta name="docs-project" content="{PROJECT}">'
        f"\n<title>{state['slug']}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    _write_resource(
        root / "docs" / "plans" / f"{PLAN}.html",
        {
            "type": "plan",
            "slug": PLAN,
            "title": "Plan A",
            "status": "active",
            "version": 0,
            "comments": {},
        },
    )
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _write_manifest(repository: Path, text: str, run_id: str) -> Path:
    manifests = repository.parent / "manifests"
    manifests.mkdir(exist_ok=True)
    path = manifests / f"{run_id}.md"
    path.write_text(text, encoding="utf-8")
    return path


def _promote_with_manifest(
    repository: Path,
    run_id: str,
    manifest_text: str,
    *,
    base_sha: str = "",
    read_manifest: bool = True,
    node_extra: dict | None = None,
    pointer_extra: dict | None = None,
) -> dict:
    """Write a pointer (with an optional manifest) and promote it."""
    manifest_path = (
        str(_write_manifest(repository, manifest_text, run_id)) if read_manifest else ""
    )
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repository),
        "launch": "in-harness",
        "role": "implement",
        "member": "worker-a",
        "backend": "native",
        "created_at": "2026-09-04T12:00:00Z",
        "manifest_path": manifest_path,
        "node": {
            "id": f"node-{run_id}",
            "plan": PLAN,
            "section": "§1",
            "time_budget": "25m",
            "write_paths": [],
            **(node_extra or {}),
        },
        **(pointer_extra or {}),
    }
    if base_sha:
        pointer["base_sha"] = base_sha
    _write_json(pointer_path(run_id), pointer)
    return crew.complete(
        run_id,
        gate="passed",
        no_commit="test: the report is the deliverable",
        root=repository,
    )


def _row(repository: Path, run_id: str) -> dict:
    runs = ledger.load(PROJECT, repository)[0]["runs"]
    return next(item for item in runs if item["run_id"] == run_id)


# ── follow_on_paths extraction ──────────────────────────────────────────────


def test_empty_follow_ons_extract_to_an_empty_list() -> None:
    assert ledger.follow_on_paths(None) == []
    assert ledger.follow_on_paths([]) == []
    assert ledger.follow_on_paths(["none"]) == []


def test_prose_without_paths_extracts_nothing() -> None:
    entries = [
        "the observe command needs a --wait flag",
        "the fixture README needs a diagram",
    ]
    assert ledger.follow_on_paths(entries) == []


def test_prose_naming_a_path_extracts_that_path() -> None:
    assert ledger.follow_on_paths(["workers.py is outside the fence"]) == ["workers.py"]
    assert ledger.follow_on_paths(["reckon/crew/promotion.py is not in my fence"]) == [
        "reckon/crew/promotion.py"
    ]


def test_bare_paths_are_preserved_and_deduplicated() -> None:
    entries = ["reckon/crew/promotion.py", "tests/test_routing_evidence_fields.py"]
    assert ledger.follow_on_paths(entries) == entries
    assert ledger.follow_on_paths([*entries, "reckon/crew/promotion.py"]) == entries


def test_structured_follow_on_entries_name_their_own_paths() -> None:
    entries = [
        {"path": "reckon/ledger.py"},
        {"paths": "tests/test_routing_evidence_fields.py"},
    ]
    assert ledger.follow_on_paths(entries) == [
        "reckon/ledger.py",
        "tests/test_routing_evidence_fields.py",
    ]
    assert ledger.follow_on_paths(
        [
            {"path": "reckon/ledger.py"},
            {"paths": ["experiments/lab.py", "experiments/lab.py"]},
        ]
    ) == ["reckon/ledger.py", "experiments/lab.py"]


# ── predecessor link derivation ─────────────────────────────────────────────


def test_coordinator_supplied_predecessor_is_authoritative() -> None:
    runs = [
        {"run_id": "r-earlier", "commits": ["1111111111111111111111111111111111111111"]}
    ]
    supplied = "r-the-coordinator-named"
    assert (
        ledger.predecessor_run_id(
            supplied=supplied,
            base_sha="1111111111111111111111111111111111111111",
            runs=runs,
        )
        == supplied
    )


def test_base_matching_a_single_prior_tip_names_that_run() -> None:
    runs = [
        {"run_id": "r-earlier", "commits": ["aaaa", "bbbb"]},
        {"run_id": "r-unrelated", "commits": ["cccc"]},
    ]
    assert (
        ledger.predecessor_run_id(
            supplied="", base_sha="bbbb", runs=runs, exclude_run_id="r-now"
        )
        == "r-earlier"
    )


def test_ambiguous_or_absent_base_matches_name_no_predecessor() -> None:
    runs = [
        {"run_id": "r-one", "commits": ["aaaa"]},
        {"run_id": "r-two", "commits": ["aaaa"]},
    ]
    assert ledger.predecessor_run_id(supplied="", base_sha="aaaa", runs=runs) is None
    assert ledger.predecessor_run_id(supplied="", base_sha="zzzz", runs=runs) is None
    assert ledger.predecessor_run_id(supplied="", base_sha="", runs=runs) is None


# ── stated correction count ─────────────────────────────────────────────────


def test_an_unreadable_report_counts_as_unknown() -> None:
    assert ledger.stated_correction_count(None) == "unknown"
    assert ledger.stated_correction_count("") == "unknown"
    assert ledger.stated_correction_count("   ") == "unknown"
    # A body that declares itself JSON but is not a readable object does not
    # parse either, so it cannot claim a measured zero.
    assert ledger.stated_correction_count('{"status": broken') == "unknown"


def test_a_clean_report_counts_zero_corrections() -> None:
    report = (
        "status: complete\n"
        "commits: none\n"
        "tests: PASS, 3 passed\n"
        "follow_ons: none\n"
        "blockers: none\n"
    )
    assert ledger.stated_correction_count(report) == 0


def test_a_report_stating_a_dispute_counts_it() -> None:
    assert ledger.stated_correction_count("I disputed the premise of the brief") == 1
    assert ledger.stated_correction_count("contrary to the brief") == 1
    assert (
        ledger.stated_correction_count(
            "the stated premise of the estimate is not the case"
        )
        == 1
    )
    assert (
        ledger.stated_correction_count("contrary to the brief.\nI disputed the premise")
        == 2
    )


def test_a_structured_correction_list_counts_its_items() -> None:
    report = (
        "status: complete\n"
        "premise_corrections: the base already carried the wiring, "
        "the estimate assumed a clean base\n"
    )
    assert ledger.stated_correction_count(report) == 2
    json_report = json.dumps(
        {"status": "complete", "premise_corrections": ["one", "two"]}
    )
    assert ledger.stated_correction_count(json_report) == 2
    assert (
        ledger.stated_correction_count(
            json.dumps({"status": "complete", "premise_corrections": []})
        )
        == 0
    )


# ── promotion records the three fields ──────────────────────────────────────


def test_promotion_records_an_empty_follow_on_list_not_a_missing_key(
    repository: Path,
) -> None:
    run_id = "r-20260904T120000000001-empty-follows"
    _promote_with_manifest(
        repository,
        run_id,
        "status: complete\nfollow_ons: none\nblockers: none\n",
    )
    row = _row(repository, run_id)
    assert row["follow_on_paths"] == []
    assert row["dispute_count"] == 0


def test_promotion_records_paths_named_in_follow_ons(
    repository: Path,
) -> None:
    run_id = "r-20260904T120000000002-paths"
    _promote_with_manifest(
        repository,
        run_id,
        "status: complete\n"
        "follow_ons: reckon/crew/promotion.py is outside the fence, "
        "reckon/ledger.py\n",
    )
    row = _row(repository, run_id)
    assert row["follow_on_paths"] == [
        "reckon/crew/promotion.py",
        "reckon/ledger.py",
    ]


def test_promotion_records_a_stated_dispute_count(repository: Path) -> None:
    run_id = "r-20260904T120000000003-disputed"
    _promote_with_manifest(
        repository,
        run_id,
        "status: complete\n"
        "commits: none\n"
        "I disputed the premise of the brief estimate and landed once.\n",
    )
    row = _row(repository, run_id)
    assert row["dispute_count"] == 1


def test_a_promotion_without_a_manifest_is_never_measured(
    repository: Path,
) -> None:
    run_id = "r-20260904T120000000004-no-manifest"
    _promote_with_manifest(
        repository,
        run_id,
        "",
        read_manifest=False,
    )
    row = _row(repository, run_id)
    assert "follow_on_paths" not in row
    assert row["dispute_count"] == "unknown"


def test_a_run_with_no_predecessor_omits_the_continuation_link(
    repository: Path,
) -> None:
    run_id = "r-20260904T120000000005-first"
    _promote_with_manifest(
        repository,
        run_id,
        "status: complete\nfollow_ons: none\n",
        base_sha="f" * 40,
    )
    row = _row(repository, run_id)
    assert "predecessor_run" not in row


def test_a_coordinator_supplied_predecessor_is_recorded(
    repository: Path,
) -> None:
    run_id = "r-20260904T120000000006-linked"
    _promote_with_manifest(
        repository,
        run_id,
        "status: complete\nfollow_ons: none\n",
        base_sha="f" * 40,
        node_extra={"predecessor": "r-earlier-run"},
    )
    row = _row(repository, run_id)
    assert row["predecessor_run"] == "r-earlier-run"


def test_a_predecessor_derived_from_the_base_names_the_earlier_run(
    repository: Path,
) -> None:
    earlier = ledger.build_record(run_id="r-earlier", plan=PLAN, gate="passed")
    earlier["commits"] = ["e" * 40]
    ledger.append_run(PROJECT, earlier, root=repository)
    run_id = "r-20260904T120000000007-continues"
    _promote_with_manifest(
        repository,
        run_id,
        "status: complete\nfollow_ons: none\n",
        base_sha="e" * 40,
    )
    row = _row(repository, run_id)
    assert row["predecessor_run"] == "r-earlier"


def test_a_legacy_promoted_row_keeps_every_existing_field_type() -> None:
    """A row promoted before this change is unchanged by the new fields.

    The routing-evidence keys are additions of records whose evidence exists;
    a row built without them must carry neither the keys nor any alteration of
    the fields it always had.
    """
    legacy = ledger.build_record(
        run_id="r-legacy",
        plan=PLAN,
        gate="passed",
        worker_seconds=320,
        tests_added=4,
        commits=["c" * 40],
        session_id="sess-1",
    )
    for name in ("follow_on_paths", "predecessor_run", "dispute_count"):
        assert name not in legacy
    assert set(ledger.RECORD_FIELDS) <= set(legacy)
    assert legacy["gate"] == "passed"
    assert legacy["worker_seconds"] == 320
    assert legacy["tests_added"] == 4
    assert legacy["commits"] == ["c" * 40]
    assert legacy["session_id"] == "sess-1"


def test_a_promoted_row_carries_all_three_fields_together(
    repository: Path,
) -> None:
    earlier = ledger.build_record(run_id="r-earlier", plan=PLAN, gate="passed")
    earlier["commits"] = ["e" * 40]
    ledger.append_run(PROJECT, earlier, root=repository)
    run_id = "r-20260904T120000000008-all-three"
    _promote_with_manifest(
        repository,
        run_id,
        "status: complete\n"
        "I disputed the premise of the time budget.\n"
        "follow_ons: reckon/ledger.py is outside the fence\n",
        base_sha="e" * 40,
    )
    row = _row(repository, run_id)
    assert row["follow_on_paths"] == ["reckon/ledger.py"]
    assert row["predecessor_run"] == "r-earlier"
    assert row["dispute_count"] == 1
    # The row is still a complete completed-run record.
    assert set(ledger.RECORD_FIELDS) <= set(row)
