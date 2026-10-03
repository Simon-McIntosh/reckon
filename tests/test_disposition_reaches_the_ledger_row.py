"""A run's disposition verb reaches the committed ledger row at promotion.

``record_run_disposition`` records why a live pointer may outlive its session,
and the pointer is the only copy of that decision. Promotion deletes the
pointer, so unless the row reads the verb while the pointer still exists, the
committed record cannot tell a run that was deliberately handed off from one
that simply disappeared. The row carries the verb as its own field, null on a
run that carried none, so the two never read alike.
"""

from __future__ import annotations

import json
import subprocess
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
    _seed_git_repository(root)
    return root


def _seed_git_repository(root: Path) -> None:
    """Make the fixture a git worktree with one commit.

    Promotion refuses a checkout that cannot host the landing commit, and the
    ledger reads an absent crew.json as a deletion to recover unless git can
    report the path was never tracked — a question it answers only from a
    checkout that carries at least one commit.
    """
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "docs"),
        ("commit", "-q", "-m", "seed repository"),
    ):
        subprocess.run(
            ["git", *arguments],
            cwd=root,
            check=True,
            capture_output=True,
        )


def _write_manifest(repository: Path, text: str, run_id: str) -> Path:
    manifests = repository.parent / "manifests"
    manifests.mkdir(exist_ok=True)
    path = manifests / f"{run_id}.md"
    path.write_text(text, encoding="utf-8")
    return path


def _write_pointer(
    repository: Path,
    run_id: str,
    *,
    disposition: dict | None = None,
) -> None:
    manifest_path = str(
        _write_manifest(repository, "status: complete\nfollow_ons: none\n", run_id)
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
        "closure_disposition": disposition,
        "node": {
            "id": f"node-{run_id}",
            "plan": PLAN,
            "section": "§1",
            "time_budget": "25m",
            "write_paths": [],
        },
    }
    _write_json(pointer_path(run_id), pointer)


def _promote(repository: Path, run_id: str) -> dict:
    # The review gate is not this node's subject, and its verdict depends on
    # the tree the promotion resolves the run's revision in — a fixture whose
    # pointer names no worktree resolves that tree from the working directory,
    # so a review record would make this test pass or fail by where pytest was
    # started rather than by what the ledger row carries. The waiver states
    # that directly instead.
    return crew.complete(
        run_id,
        gate="passed",
        no_commit="test: the report is the deliverable",
        review_waiver="test: the row's disposition field is the subject, not the review gate",
        root=repository,
    )


def _row(repository: Path, run_id: str) -> dict:
    runs = ledger.load(PROJECT, repository)[0]["runs"]
    return next(item for item in runs if item["run_id"] == run_id)


# ── the verb reaches the committed row ──────────────────────────────────────


def test_a_pointer_disposition_lands_on_the_promoted_row(repository: Path) -> None:
    run_id = "r-20261003T090000000001-handed-off"
    _write_pointer(
        repository,
        run_id,
        disposition={"kind": "handed-off", "recorded_at": "2026-10-03T09:00:00Z"},
    )

    _promote(repository, run_id)

    row = _row(repository, run_id)
    assert "disposition" in ledger.RECORD_FIELDS
    assert set(ledger.RECORD_FIELDS) <= set(row)
    assert row["disposition"] == "handed-off"
    assert row["disposition"] in ("handed-off", "still-working")


def test_a_run_with_no_disposition_reads_null_and_not_an_inferred_verb(
    repository: Path,
) -> None:
    run_id = "r-20261003T090000000002-undisposed"
    _write_pointer(repository, run_id, disposition=None)

    _promote(repository, run_id)

    row = _row(repository, run_id)
    assert "disposition" in row
    assert row["disposition"] is None


def test_two_rows_of_one_run_shape_are_distinguishable(
    repository: Path,
) -> None:
    disposed = "r-20261003T090000000003-disposed"
    plain = "r-20261003T090000000004-plain"
    _write_pointer(
        repository,
        disposed,
        disposition={"kind": "still-working", "recorded_at": "2026-10-03T09:00:00Z"},
    )
    _write_pointer(repository, plain, disposition=None)

    _promote(repository, disposed)
    _promote(repository, plain)

    assert _row(repository, disposed)["disposition"] == "still-working"
    assert _row(repository, plain)["disposition"] is None


def test_the_pointer_writer_records_the_verb_the_row_carries(
    repository: Path,
) -> None:
    """The row's verb is the one the writer recorded, not a transcription.

    ``record_run_disposition`` is the only producer of the verb, so the test
    drives it rather than hand-writing the pointer block: a promotion that read
    a different key, or normalised the verb, would miss here while a
    hand-written pointer could still pass.
    """
    run_id = "r-20261003T090000000005-recorded"
    _write_pointer(repository, run_id, disposition=None)

    crew.record_run_disposition(run_id, "handed-off", project=PROJECT)

    _promote(repository, run_id)
    assert _row(repository, run_id)["disposition"] == "handed-off"


def test_a_verb_outside_the_closed_set_is_refused_before_it_can_be_recorded(
    repository: Path,
) -> None:
    """The guarded thing happens: a verb outside the set is refused.

    The row renders whatever the pointer carries, so the closed set is what
    keeps prose off the ledger. A suite that never writes a bad verb cannot
    show the guard fires.
    """
    run_id = "r-20261003T090000000006-candidate"
    _write_pointer(repository, run_id, disposition=None)

    with pytest.raises(crew.CrewError) as refusal:
        crew.record_run_disposition(run_id, "landed", project=PROJECT)

    assert "landed" in str(refusal.value)
    # The refusal left the pointer without a disposition rather than with a
    # malformed one, so the row can only ever carry a verb from the set.
    pointer = json.loads(pointer_path(run_id).read_text(encoding="utf-8"))
    assert pointer.get("closure_disposition") is None
