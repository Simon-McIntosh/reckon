"""A duty raised from a run's stored review is one the disposition command can retire.

A run outlives the worktree it worked in. The sub-floor dimension duty is
derived from the run's stored review whether or not that worktree survives, and
the disposition command is the writer that retires the duty its row names — so
the two have to select the same record, or the coordinator is shown a row that
no command can dispose. These cases drive both readers over the same run: a
reclaimed run whose manifest cites its head by a nine-character id, a reclaimed
run whose record names no head, and a run with no readable tree. Each case
asserts the duty row exists, that the disposition command accepts, and that the
record it selected is the record the duty reader selected.

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
from reckon.crew import review as review_module
from reckon.crew import runs

PROJECT = "review-head-fixture"
SESSION = "coordinator-fixture"
SHORT_RUN = "r-reclaimed-head"
HEADLESS_RUN = "r-reclaimed-headless"
TREELESS_RUN = "r-no-readable-tree"
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
    """One throwaway project: a temporary repository, and three runs.

    Every run's stored review is keyed to the revision the run reached, and
    the shared repository is left holding a later commit at its HEAD, so a
    selection equal to the run-head record is evidence the run's own record
    was read rather than whatever the checkout carries now.
    """

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.home = tmp_path / "config"
        self.home.mkdir(parents=True)
        self.repo = tmp_path / "repo"
        (self.repo / "docs" / "state" / PROJECT).mkdir(parents=True)
        (self.repo / "seed.txt").write_text("seed\n", encoding="utf-8")
        for arguments in (
            ("init", "-q", "-b", "main"),
            ("config", "user.email", "worker@example.invalid"),
            ("config", "user.name", "reclaimed"),
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
        # The reclaimed runs' worktrees are recorded and gone; the treeless
        # run's repository is recorded and gone too, so neither pointer names
        # a tree a reader could resolve a head from.
        self.reclaimed_worktree = tmp_path / "worktrees" / "reclaimed"
        self.missing_repo = tmp_path / "checkouts" / "gone"
        self.pointer(
            SHORT_RUN,
            commits=[self.run_head[:9]],
            worktree=str(self.reclaimed_worktree),
            repo=str(self.repo),
        )
        self.pointer(
            HEADLESS_RUN,
            commits=[],
            worktree=str(self.reclaimed_worktree),
            repo=str(self.repo),
        )
        self.pointer(TREELESS_RUN, commits=[], repo=str(self.missing_repo))
        for run_id in (SHORT_RUN, HEADLESS_RUN, TREELESS_RUN):
            _store_review(
                run_id,
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

    def pointer(
        self,
        run_id: str,
        *,
        commits: list[str],
        worktree: str = "",
        repo: str = "",
    ) -> dict:
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
            "manifest_path": str(manifest),
            "node": {
                "id": run_id,
                "plan": "fixture",
                "section": "s1",
                "time_budget": "20m",
                "write_paths": ["seed.txt"],
            },
        }
        if worktree:
            record["worktree"] = worktree
        if repo:
            record["repo"] = repo
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
    that is not the run's — the assertion that the two differ is what shows a
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
    """Every stored record the two readers selected, per run."""

    def __init__(self) -> None:
        self.records: dict[str, list[dict | None]] = {}

    def note(self, run_id: object, record: dict | None) -> None:
        self.records.setdefault(str(run_id), []).append(record)


def _audit_selections(monkeypatch: pytest.MonkeyPatch) -> _SelectionAudit:
    """Record what each reader selects, at the reading the readers share.

    Both the duty reader and the disposition command select their record
    through ``stored_review_for_run`` in ``reckon/crew/obligations.py``, so the
    record that function returns is the record the reader went on to use, and
    the audit observes each reader's choice rather than inferring it from the
    reader's final effect. Other obligation readers select against other heads
    and do not pass through this seam, so a recorded selection is the duty
    reader's or the disposition command's.
    """
    audit = _SelectionAudit()
    original = obligations_module.stored_review_for_run

    def shared(project: str, run_id: str, pointer):
        record, described = original(project, run_id, pointer)
        audit.note(run_id, record)
        return record, described

    monkeypatch.setattr(obligations_module, "stored_review_for_run", shared)
    return audit


def _drive_duty(
    monkeypatch: pytest.MonkeyPatch, run_id: str
) -> tuple[_SelectionAudit, list[dict]]:
    """Run the obligations derivation and return the duty rows for one run."""
    audit = _audit_selections(monkeypatch)
    report = obligations_module.obligations(PROJECT, SESSION)
    rows = [
        row
        for row in report["obligations"]
        if row["kind"] == obligations_module.SUB_FLOOR_DUTY_KIND
        and row["run_id"] == run_id
    ]
    return audit, rows


def _drive_disposition(run_id: str):
    return CliRunner().invoke(cli_main, [*DISPOSITION, "--run", run_id])


def _assert_readers_agree(
    fleet: _Fleet, monkeypatch: pytest.MonkeyPatch, run_id: str
) -> dict:
    """The duty reader and the disposition command select the same record.

    The duty row is asserted first, because a run whose review this fixture
    stored owes a row whatever else happens; the disposition then has to
    accept, and the record observed behind both readers has to be the one
    record the store holds for the run, keyed to the revision the run reached.
    """
    audit, rows = _drive_duty(monkeypatch, run_id)
    assert [row["dimension"] for row in rows] == ["durability"], rows

    duty_seen = list(audit.records.get(run_id, []))
    assert duty_seen, "the duty reader reached no selection"
    duty_record = duty_seen[0]
    assert duty_record is not None
    assert all(record == duty_record for record in duty_seen), duty_seen
    assert duty_record.get("reviewed_head_sha") == fleet.run_head, duty_record

    result = _drive_disposition(run_id)
    assert result.exit_code == 0, result.output
    selected = audit.records.get(run_id, [])
    assert len(selected) > len(duty_seen), (
        "the disposition command reached no selection"
    )
    assert all(record == duty_record for record in selected), selected

    keyed = review_module.review_path(
        PROJECT, run_id, reviewed_head_sha=duty_record["reviewed_head_sha"]
    )
    assert json.loads(result.output)["path"] == str(keyed)
    return duty_record


def test_the_reclaimed_run_with_a_short_head_is_disposed_where_its_duty_reads(
    fleets: Callable[[], _Fleet], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The nine-character citation resolves to the run's own full id, and both
    readers select the record keyed to it — not the checkout's later HEAD."""
    fleet = fleets()
    _assert_reclaimed_premises(fleet)
    _assert_readers_agree(fleet, monkeypatch, SHORT_RUN)


def test_the_reclaimed_headless_run_is_disposed_where_its_duty_reads(
    fleets: Callable[[], _Fleet], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stored review outlives the worktree, and its duty stays disposable.

    The record names no head, so the duty reader reads the store's newest
    record for the run; the disposition command takes the same reading and
    retires the row rather than refusing the run it was raised for.
    """
    fleet = fleets()
    _assert_reclaimed_premises(fleet)
    _assert_readers_agree(fleet, monkeypatch, HEADLESS_RUN)


def test_the_run_with_no_readable_tree_is_disposed_where_its_duty_reads(
    fleets: Callable[[], _Fleet], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No worktree and no repository still leave the stored review readable."""
    fleet = fleets()
    _assert_readers_agree(fleet, monkeypatch, TREELESS_RUN)
