"""A promotion dry run reports what the real call will refuse.

A dry run that answers a different question from the landing is worse than no
dry run at all: ``would_promote: true`` is what a coordinator plans around, and
a real ``--land`` that then refuses the same run costs the round trip the dry
run was asked to save. Three gaps are closed here, each exercised by making the
guarded thing happen rather than by reading the code that guards it:

* a run whose armed suite pair the landing cannot read is refused by the real
  call and reported by the dry run as the same refusal, naming the fields it is
  missing. The dry run still measures nothing: the pair the landing would take
  is named, never taken.
* a delivered implementation with no stored review of its head reports the
  review it owes, so the answer to "what is missing" is a list a reader can act
  with rather than an empty ``would_file_reviews``.
* a dry run takes no write lock and holds no cross-host claim, so it cannot
  queue behind a promotion of the same run.

The suite case compares the dry run's answer with the refusal the real call
raises over one record, so the two surfaces are held to the same evaluation
rather than to two readings of it.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from reckon import _plan_html, _store, ledger
from reckon.crew import promotion as promotion_module
from reckon.crew import promotion_records
from reckon.crew.runs import _write_json, pointer_path, run_dir
from tests.conftest import EXECUTABLE_GATE_COMMAND
from tests.helpers.git import git as _git

PROJECT = "proj"
PLAN = "plan-a"
NODE_TEST_FILE = "tests/test_alpha.py"
SOURCE_FILE = "reckon/alpha.py"

#: The command the run was armed with. The arms the fixtures record ran a
#: different one — the state a coordinator reaches by re-running a node's tests
#: under a second spelling — so the pair is evidence about another suite.
ARMED_COMMAND = (
    "PYTHONPATH=. .venv/bin/python -m pytest -p no:cacheprovider -q " + NODE_TEST_FILE
)
ARM_RECORDED_COMMAND = (
    "PYTHONPATH=. .venv/bin/python -m pytest -p no:cacheprovider -q tests/test_beta.py"
)


def _write_plan(path: Path) -> None:
    """Write one plan for the promotion's project to resolve against."""
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    path.write_text(
        _plan_html.write_state(
            bare,
            {
                "type": "plan",
                "slug": PLAN,
                "title": "Plan A",
                "status": "active",
                "version": 0,
                "comments": {},
            },
        ),
        encoding="utf-8",
    )


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fixture crew home and a git repository carrying one plan."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    _write_plan(root / "docs" / "plans" / f"{PLAN}.html")
    (root / NODE_TEST_FILE).parent.mkdir(parents=True, exist_ok=True)
    (root / NODE_TEST_FILE).write_text("def test_alpha():\n    assert True\n")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(root, *arguments)
    _git(root, "add", "docs", NODE_TEST_FILE)
    _git(root, "commit", "-q", "-m", "test: seed repository")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _set_plan_impl(repository: Path, value: float) -> None:
    """Move the plan's impl, so the promotion's impl guard is satisfied."""
    state, version = _store.read_plan(PROJECT, PLAN, repository, artifact_type="plan")
    state["impl"] = value
    _store.write_plan(PROJECT, PLAN, state, version, repository, artifact_type="plan")


def _arm(
    revision: str,
    *,
    command: str = ARM_RECORDED_COMMAND,
    failure_ids: Sequence[str] | None = None,
) -> dict:
    """One recorded suite arm, run at ``revision`` under ``command``."""
    ids = list(failure_ids or ())
    return {
        "revision": revision,
        "command": command,
        "exit_status": 0 if not ids else 1,
        "log_path": None,
        "log_digest": f"sha256:{revision}",
        "completed": True,
        "failure_count": 0,
        "failure_ids": ids,
    }


def _manifest(
    directory: Path,
    *,
    commits: Sequence[str] = (),
    changed_paths: Sequence[str] = (),
    baseline: dict | None = None,
    after: dict | None = None,
) -> Path:
    """A delivered manifest citing one gate log that recorded a zero exit."""
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "manifest.md"
    log = directory / "gate.log"
    log.write_text("ran the focused gate\nEXIT=0\n", encoding="utf-8")
    lines = [
        "node: node-a",
        "status: complete",
        "commits: [" + ", ".join(commits) + "]",
        "changed_paths: [" + ", ".join(changed_paths) + "]",
        f"tests: {EXECUTABLE_GATE_COMMAND}",
        f"test_logs: [{log}]",
    ]
    if baseline is not None:
        lines.append("baseline_suite: " + json.dumps(baseline))
    if after is not None:
        lines.append("after_suite: " + json.dumps(after))
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return manifest


def _run(
    repository: Path,
    tmp_path: Path,
    run_id: str,
    *,
    baseline: dict | None = None,
    after: dict | None = None,
    suite_command: str = "",
) -> tuple[dict, str]:
    """A delivered run whose worktree carries one commit beyond its base."""
    base = _git(repository, "rev-parse", "HEAD")
    worktree = tmp_path / f"worktree-{run_id}"
    _git(repository, "worktree", "add", "-q", "--detach", str(worktree), base)
    (worktree / SOURCE_FILE).parent.mkdir(parents=True, exist_ok=True)
    (worktree / SOURCE_FILE).write_text("VALUE = 1\n", encoding="utf-8")
    _git(worktree, "add", SOURCE_FILE)
    _git(worktree, "commit", "-q", "-m", "code")
    head = _git(worktree, "rev-parse", "HEAD")
    manifest = _manifest(
        run_dir(run_id),
        commits=[head],
        changed_paths=[SOURCE_FILE],
        baseline=baseline,
        after=after,
    )
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repository),
        "worktree": str(worktree),
        "base_sha": base,
        "launch": "in-harness",
        "role": "implement",
        "backend": "native",
        "created_at": "2026-11-15T09:00:00Z",
        "manifest_path": str(manifest),
        "node": {
            "id": "node-a",
            "plan": PLAN,
            "section": "s1",
            "time_budget": "25m",
            "write_paths": [SOURCE_FILE, NODE_TEST_FILE],
        },
        "plan_impl_at_dispatch": 0.5,
    }
    if suite_command:
        record["suite_command"] = suite_command
    _write_json(pointer_path(run_id), record)
    return record, head


def _gate_check() -> dict:
    """The gate receipt handed to the promotion, resolved by the fill."""
    return {"command": "", "exit_status": None, "log_path": "", "log_digest": ""}


def _dry_run(run_id: str, repository: Path, **kwargs) -> dict:
    return promotion_module.complete_dry_run(
        run_id,
        root=repository,
        gate="passed",
        gate_check=_gate_check(),
        require_gate_check=True,
        **kwargs,
    )


def test_a_dry_run_reports_the_suite_refusal_the_real_call_raises(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One record, two surfaces: the dry run's answer is the call's refusal.

    The pair here is the one the real call cannot read — arms recorded under a
    second spelling, the after arm left at the base revision with no failure
    evidence — so the landing's own measurement is what decides the run and the
    dry run must not report a run promotable over evidence nobody has produced.
    """
    run_id = "r-20261115T090000000000-node-a"
    _set_plan_impl(repository, 1.0)
    base = _git(repository, "rev-parse", "HEAD")
    # The after arm names no command and no failure evidence, and records the
    # base revision rather than the head: the waiver cannot hold a pair whose
    # after arm is not evidence, so the landing's own measurement decides it.
    after = {"revision": base, "exit_status": 0, "completed": True}
    _run(
        repository,
        tmp_path,
        run_id,
        baseline=_arm(base),
        after=after,
        suite_command=ARMED_COMMAND,
    )
    waiver = "the pair ran under another spelling and cannot be re-measured"

    def _dry_run_took_the_measurement(*_args, **_kwargs):
        raise AssertionError("a dry run submitted the suite measurement")

    monkeypatch.setattr(
        promotion_records, "_measure_missing_arms", _dry_run_took_the_measurement
    )
    report = _dry_run(
        run_id,
        repository,
        suite_delta_waiver=waiver,
        review_waiver="a sibling node already read this change",
    )
    refusals = "\n".join(report["refusals"])

    assert report["dry_run"] is True
    assert report["would_promote"] is False, refusals
    assert (
        "armed promotion requires complete baseline and after suite evidence"
        in refusals
    )
    assert report["suite_delta"]["status"] == "would-measure"
    assert report["would_measure"] is True

    monkeypatch.setattr(
        promotion_records,
        "_measure_missing_arms",
        lambda *_args, **_kwargs: {"unreadable": "the armed command ran no test"},
    )
    with pytest.raises(ledger.SuiteDeltaError) as refused:
        promotion_module.complete(
            run_id,
            root=repository,
            gate="passed",
            gate_check=_gate_check(),
            require_gate_check=True,
            suite_delta_waiver=waiver,
            review_waiver="a sibling node already read this change",
        )
    assert refused.value.missing_fields
    for field in refused.value.missing_fields:
        assert field in refusals, field


def test_a_delivered_unreviewed_run_reports_the_review_it_owes(
    repository: Path, tmp_path: Path
) -> None:
    """The owed review is a fact the dry run states, not one it omits."""
    run_id = "r-20261115T090100000000-node-a"
    _set_plan_impl(repository, 1.0)
    _record, head = _run(repository, tmp_path, run_id)

    report = _dry_run(run_id, repository)
    owed = report["reviews_owed"]

    assert report["would_promote"] is False
    assert [str(entry["run_id"]) for entry in owed] == [run_id]
    assert owed[0]["head"] == head
    assert owed[0]["tier"] not in ("", "none")
    assert "review" in "\n".join(report["refusals"])


def test_a_dry_run_takes_no_write_lock(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dry run writes nothing, so it holds no lock a real call would need."""
    run_id = "r-20261115T090200000000-node-a"
    _set_plan_impl(repository, 1.0)
    _run(repository, tmp_path, run_id)

    def _must_not_lock(*_args, **_kwargs):
        raise AssertionError("a dry run took a write lock")

    for name in ("_promotion_lock", "_pointer_lock", "_promotion_host_lease"):
        monkeypatch.setattr(promotion_module, name, _must_not_lock)

    report = _dry_run(run_id, repository)

    assert report["dry_run"] is True
    assert report["would_promote"] is False
