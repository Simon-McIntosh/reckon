"""Every reader of a reclaimed run's review selects it through the classifier's rule.

A run outlives the worktree it worked in. Once that worktree has been
reclaimed, the only tree a reader can still reach is the shared checkout, whose
HEAD is whatever that repository has reached since — a revision the run's review
is not about. The classifier already reads a reclaimed run's head from the run's
own record and refuses a headless reclaimed run rather than borrowing the
checkout's HEAD, but two other readers resolved the head themselves and so
disagreed with it: the crew disposition command, and the sub-floor dimension
duty the obligations derivation builds.

This module drives both readers on two reclaimed runs — one whose manifest
cites its head by a nine-character id, and one whose record names no head — and
asserts that the record each reader selects is the record the classifier
selects for the same run. The repository is left holding a later commit at its
HEAD in every case, so a selection equal to the classifier's is evidence the
run's own record was read and the checkout's HEAD was not.

The fixture stands the runs up inside a temporary configuration home and a
temporary repository, so the operator's review store and their checkouts are
neither read nor written.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon.cli import main as cli_main
from reckon.crew import obligations as obligations_module
from reckon.crew import recovery, runs
from reckon.crew import review as review_module

PROJECT = "review-head-fixture"
SESSION = "coordinator-fixture"
SHORT_RUN = "r-reclaimed-head"
HEADLESS_RUN = "r-reclaimed-headless"
FOLD_NODE = "repair-durability-node"

# The head the runs reached, then a later commit the repository keeps at its
# HEAD: the revision a reader that falls through to the checkout resolves.
LANDING = "landing"
CHECKOUT_LATER = "later"

# A passing total with room to spare while one dimension sits far below its
# declared floor, so the sub-floor duty has exactly one row to build per run.
SCORES = {
    "goal_fidelity": 19,
    "evidence": 19,
    "scope_discipline": 18,
    "durability": 5,
    "fit": 18,
}
FLOORS = {"durability": 10}

REAL_HOME = Path.home() / ".config" / "reckon"

DISPOSITION = (
    "crew",
    "dispose",
    "--project",
    PROJECT,
    "--dimension",
    "durability",
    "--kind",
    "folded",
    "--node",
    FOLD_NODE,
)


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _emit(*, base_sha: str, head_sha: str) -> str:
    """Emitted review text carrying the revision pair and one score per line."""
    lines = [f"reviewed_base_sha: {base_sha}", f"reviewed_head_sha: {head_sha}"]
    lines.extend(f"SCORE {dimension}: {score}" for dimension, score in SCORES.items())
    return "\n".join(lines) + "\n"


def _store_review(reviewed_run_id: str, text: str, *, timestamp: str) -> Path:
    """Store one review through the production writer, as a reviewer would."""
    record = review_module.parse_review(text)
    record.update(
        {
            "project": PROJECT,
            "reviewed_run_id": reviewed_run_id,
            "timestamp": timestamp,
        }
    )
    return review_module.store_review(record)


class _Fleet:
    """One throwaway project: a temporary repository, and two reclaimed runs."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.home = tmp_path / "config"
        self.home.mkdir(parents=True)
        self.repo = tmp_path / "repo"
        (self.repo / "docs" / "state" / PROJECT).mkdir(parents=True)
        (self.repo / "seed.txt").write_text("seed\n", encoding="utf-8")
        for arguments in (
            ("init", "-q", "-b", "main"),
            ("config", "user.email", "worker@example.invalid"),
            ("config", "user.name", "Worker"),
            ("add", "seed.txt"),
            ("commit", "-q", "-m", "test: seed the shared repository"),
        ):
            _git(self.repo, *arguments)
        self.base_sha = self.head()
        self.run_head = self.commit(LANDING)
        self.later_head = self.commit(CHECKOUT_LATER)
        # The crew is pointed at the fixture home only after the repository
        # exists: the git shim resolves this run's own live pointer through
        # RECKON_HOME, and redirecting it first leaves fixture git calls unable
        # to resolve the run they belong to.
        monkeypatch.setenv("RECKON_HOME", str(self.home))
        for name in ("RECKON_FLIGHT_CONFIG", "RECKON_WORKTREES", "RECKON_STATE_ROOT"):
            monkeypatch.delenv(name, raising=False)
        (self.home / "mounts.json").write_text(
            json.dumps({PROJECT: str(self.repo / "docs")}), encoding="utf-8"
        )
        (self.home / "flight.yaml").write_text(
            "gates:\n  dimension_floors:\n"
            + "".join(
                f"    {dimension}: {floor}\n" for dimension, floor in FLOORS.items()
            ),
            encoding="utf-8",
        )
        # Both runs' worktrees have been reclaimed: the path is recorded and
        # nothing is on disk there.
        self.reclaimed_worktree = tmp_path / "worktrees" / "reclaimed"
        self.pointer(SHORT_RUN, commits=[self.run_head[:9]])
        self.pointer(HEADLESS_RUN, commits=[])
        _store_review(
            SHORT_RUN,
            _emit(base_sha=self.base_sha, head_sha=self.run_head),
            timestamp="2026-10-03T01:00:00+00:00",
        )
        _store_review(
            HEADLESS_RUN,
            _emit(base_sha=self.base_sha, head_sha=self.run_head),
            timestamp="2026-10-03T01:00:00+00:00",
        )

    def head(self) -> str:
        return _git(self.repo, "rev-parse", "HEAD")

    def commit(self, name: str) -> str:
        path = f"{name}.txt"
        (self.repo / path).write_text(f"{name}\n", encoding="utf-8")
        _git(self.repo, "add", path)
        _git(self.repo, "commit", "-q", "-m", f"chore: add {name}")
        return self.head()

    def pointer(self, run_id: str, *, commits: list[str]) -> dict:
        """Write the live pointer and the left-behind manifest of one run."""
        manifest = self.home / "manifests" / f"{run_id}.md"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            f"node: {run_id}\nstatus: complete\ncommits: [{', '.join(commits)}]\n",
            encoding="utf-8",
        )
        record = {
            "run_id": run_id,
            "project": PROJECT,
            "session": SESSION,
            "process_alive": False,
            "repo": str(self.repo),
            "worktree": str(self.reclaimed_worktree),
            "manifest_path": str(manifest),
            "node": {
                "id": run_id,
                "plan": "fixture",
                "section": "s1",
                "time_budget": "20m",
                "write_paths": ["seed.txt"],
            },
        }
        runs._write_json(runs.pointer_path(run_id), record)
        return record


@pytest.fixture()
def fleets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[], _Fleet]:
    return lambda: _Fleet(tmp_path, monkeypatch)


@pytest.fixture(autouse=True)
def real_config_home_untouched():
    """No fixture write reaches the real configuration home."""
    yield
    reviews = REAL_HOME / "crew" / "reviews" / PROJECT
    assert not reviews.exists(), f"fixture review store leaked to {reviews}"
    live = REAL_HOME / "crew" / "live"
    leaked = [
        str(path)
        for path in (live.glob("*.json") if live.is_dir() else ())
        if PROJECT in path.read_text(encoding="utf-8", errors="replace")
    ]
    assert not leaked, f"fixture pointers leaked to the real live directory: {leaked}"


def _assert_reclaimed_premises(fleet: _Fleet) -> None:
    """The premises: the run's head is cited short, and no tree still holds it.

    The repository is left holding a later commit at its HEAD and the run's
    worktree is gone, so a reader that resolves any tree resolves a revision
    that is not this run's — the assertion that the two differ is what shows a
    head equalling the run's came from the run's own record.
    """
    assert len(fleet.run_head) == 40
    assert fleet.run_head != fleet.later_head
    assert fleet.head() == fleet.later_head
    assert not fleet.reclaimed_worktree.is_dir()
    citation = fleet.run_head[:9]
    assert len(citation) == 9
    assert citation != fleet.run_head
    manifest = (fleet.home / "manifests" / f"{SHORT_RUN}.md").read_text(
        encoding="utf-8"
    )
    assert f"commits: [{citation}]" in manifest


class _SelectionAudit:
    """Every stored record each reader selects, observed at the shared seam."""

    def __init__(self) -> None:
        self.records: dict[str, list[dict | None]] = {}

    def note(self, run_id: object, record: dict | None) -> None:
        self.records.setdefault(str(run_id), []).append(record)


def _audit_selections(monkeypatch: pytest.MonkeyPatch) -> _SelectionAudit:
    """Record what each reader selects, at the functions the readers share.

    Both readers converge on :func:`reckon.crew.recovery.select_review_for_head`
    or, for a headless record, on the classifier's own headless helper, so the
    record those functions return is the record the reader went on to use —
    observed rather than inferred from the reader's final effect.
    """
    audit = _SelectionAudit()
    original_select = recovery.select_review_for_head
    original_headless = recovery.newest_review_for_headless_run

    def select(project, run_id, head, *, tree=None):
        record, described = original_select(project, run_id, head, tree=tree)
        audit.note(run_id, record)
        return record, described

    def headless(project, run_id, *, reclaimed):
        record = original_headless(project, run_id, reclaimed=reclaimed)
        audit.note(run_id, record)
        return record

    monkeypatch.setattr(recovery, "select_review_for_head", select)
    monkeypatch.setattr(recovery, "newest_review_for_headless_run", headless)
    return audit


def _classifier_record(fleet: _Fleet, run_id: str) -> dict | None:
    """The record ``classify_pointer`` itself reads for this run."""
    pointer = runs.read_pointer(run_id)
    record, error = recovery._stored_review(pointer)
    assert error == "", error
    return record


def _drive_duty(monkeypatch: pytest.MonkeyPatch) -> tuple[_SelectionAudit, list[dict]]:
    audit = _audit_selections(monkeypatch)
    report = obligations_module.obligations(PROJECT, SESSION)
    rows = [
        row
        for row in report["obligations"]
        if row["kind"] == obligations_module.SUB_FLOOR_DUTY_KIND
    ]
    return audit, rows


def _drive_disposition(run_id: str):
    return CliRunner().invoke(cli_main, [*DISPOSITION, "--run", run_id])


def test_the_short_head_run_is_selected_as_the_classifier_selects_it(
    fleets: Callable[[], _Fleet], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both readers select the record keyed to the run's own full id.

    The nine-character citation is what makes the readers' head the resolved
    full id rather than a copy of what the manifest wrote down, and the
    repository's later HEAD is what a reader resolving any tree would select
    against: a review keyed to the run's own head is on file, so a reader on
    the checkout's HEAD selects nothing where this one selects the record.
    """
    fleet = fleets()
    _assert_reclaimed_premises(fleet)
    classifier_record = _classifier_record(fleet, SHORT_RUN)
    assert classifier_record is not None
    assert classifier_record.get("reviewed_head_sha") == fleet.run_head

    audit, rows = _drive_duty(monkeypatch)
    assert [row["dimension"] for row in rows if row["run_id"] == SHORT_RUN] == [
        "durability"
    ]

    result = _drive_disposition(SHORT_RUN)
    assert result.exit_code == 0, result.output
    keyed = review_module.review_path(
        PROJECT, SHORT_RUN, reviewed_head_sha=fleet.run_head
    )
    assert json.loads(result.output)["path"] == str(keyed)

    selected = audit.records.get(SHORT_RUN, [])
    assert selected, "neither reader reached the shared selection"
    assert all(record is not None for record in selected), selected
    assert all(record == classifier_record for record in selected), selected


def test_the_headless_run_is_selected_as_the_classifier_selects_it(
    fleets: Callable[[], _Fleet], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both readers select nothing where the classifier selects nothing.

    A reclaimed run whose record names no head has no revision to select a
    review by, and the classifier reads no record for it. Neither reader may
    read the store's newest record in its place: that is evidence about a
    revision the run cannot name, and a duty built on it would have the
    coordinator retire a finding the rest of the runtime does not accept.
    """
    fleet = fleets()
    _assert_reclaimed_premises(fleet)
    assert _classifier_record(fleet, HEADLESS_RUN) is None

    audit, rows = _drive_duty(monkeypatch)
    assert [row for row in rows if row["run_id"] == HEADLESS_RUN] == []

    result = _drive_disposition(HEADLESS_RUN)
    assert result.exit_code != 0, result.output
    assert "no stored review" in result.output

    selected = audit.records.get(HEADLESS_RUN, [])
    assert selected, "neither reader reached the shared selection"
    assert all(record is None for record in selected), selected
