"""A main-checkout index that lags HEAD is surfaced with its restore.

On 8 November a landed file was reverted by the next ordinary commit in the
main checkout: the index no longer carried a path HEAD and the working tree
both still held, so ``git add <plan> && git commit`` recorded the fragment as
deleted. The writer could not be recovered after the fact, so the condition is
made visible while it is true.

A temporary repository stands in for the main checkout and a temporary crew
home for the fleet's state. The cases pin the four readings the detector must
give: a lag is a notice naming the path and the path-limited restore, a clean
index raises nothing, an intended deletion is not a lag, and a path deliberately
untracked and ignored is not one either. The first case also pins that the log
holds one line for the lag however many times the view is read, and that reading
the view never repairs the index on the coordinator's behalf. A fifth case
publishes a snapshot and reads it back the way the stop hook does, because the
prompt path sees only the snapshot and never the derivation.

The last case asserts the real crew home is untouched, because a reader that
wrote to the fleet's live state would make the check a monitor of the machine
rather than a test of the code.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import reckon.crew.obligations as obligations_module
from reckon.crew import obligation_snapshot
from reckon.hooks import coordinator_obligations as hook

PROJECT = "demo"
SESSION = "s-demo"
TRACKED = "tracked.txt"
PLAN_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="docs-project" content="demo">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="work">
<meta name="plan-title" content="work">
<meta name="plan-status" content="active">
<title>work</title></head><body><main class="plan-doc"></main></body></html>
"""


def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "tester",
        "GIT_AUTHOR_EMAIL": "tester@example.com",
        "GIT_COMMITTER_NAME": "tester",
        "GIT_COMMITTER_EMAIL": "tester@example.com",
    }
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout


def _repository(root: Path) -> Path:
    repo = root / "checkout"
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "work.html").write_text(PLAN_HTML, encoding="utf-8")
    (repo / TRACKED).write_text("kept\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "docs/plans/work.html", TRACKED)
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary main checkout mounted for one project, with its own crew home."""
    repo = _repository(tmp_path)
    mounts = tmp_path / "mounts.json"
    mounts.write_text(json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))
    monkeypatch.setenv("RECKON_HOME", str(home))
    return repo


def _notice_rows(payload: dict) -> list[dict]:
    return [
        row
        for row in payload["obligations"]
        if row.get("kind") == obligations_module.INDEX_LAG_DUTY_KIND
    ]


def _log_lines() -> list[str]:
    path = obligations_module.crew_home() / obligations_module.INDEX_LAG_LOG_NAME
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def _staged_deletions(repo: Path) -> list[str]:
    """What the index stages as deleted against HEAD — the reader's own input."""
    listed = _git(repo, "diff", "--cached", "--name-only", "--diff-filter=D", "HEAD")
    return [name for name in listed.splitlines() if name]


def test_a_lagging_index_is_a_notice_naming_the_restore(checkout: Path) -> None:
    """Case one: the index loses a path the tree still holds, and the view says so."""
    _git(checkout, "rm", "--cached", "-q", "--", TRACKED)
    assert _staged_deletions(checkout) == [TRACKED]

    payload = obligations_module.obligations(PROJECT, SESSION)

    rows = _notice_rows(payload)
    assert len(rows) == 1, payload["obligations"]
    row = rows[0]
    assert row["kind"] in obligations_module.NON_BLOCKING_DUTY_KINDS, row
    assert row["paths"] == [TRACKED], row
    command = row["next_command"]
    assert "reset -q --" in command, row
    assert TRACKED in command, row
    # The reason states what a plain commit would do, which is the whole point
    # of surfacing it before one runs.
    assert "git commit" in row["reason"] and "deleted" in row["reason"], row

    head = _git(checkout, "rev-parse", "HEAD").strip()

    lines = _log_lines()
    assert len(lines) == 1, lines
    record = json.loads(lines[0])
    assert record["paths"] == [TRACKED], record
    # The head is compared against the checkout's own HEAD, read by the test,
    # not against the value the detector's call returned: a detector that read
    # the wrong revision would otherwise satisfy its own assertion.
    assert row["head"] == head, row
    assert record["head"] == head, record
    assert record["at"], record

    # A second read reports the same notice, appends nothing, and never repairs
    # the index on the coordinator's behalf.
    again = obligations_module.obligations(PROJECT, SESSION)
    assert len(_notice_rows(again)) == 1, again["obligations"]
    assert len(_log_lines()) == 1
    assert _staged_deletions(checkout) == [TRACKED]


def test_a_clean_index_raises_nothing(checkout: Path) -> None:
    """Case two: an index at HEAD is silent and writes no log."""
    assert _staged_deletions(checkout) == []

    payload = obligations_module.obligations(PROJECT, SESSION)

    assert _notice_rows(payload) == []
    assert _log_lines() == []


def test_an_intended_deletion_raises_nothing(checkout: Path) -> None:
    """Case three: a deletion staged in the index and on disk is not a lag."""
    _git(checkout, "rm", "-q", "--", TRACKED)
    assert not (checkout / TRACKED).exists()
    assert _staged_deletions(checkout) == [TRACKED]

    payload = obligations_module.obligations(PROJECT, SESSION)

    assert _notice_rows(payload) == []
    assert _log_lines() == []


def test_an_untracked_ignored_path_raises_nothing(checkout: Path) -> None:
    """Case four: an untracked path in .gitignore is a deliberate removal."""
    _git(checkout, "rm", "--cached", "-q", "--", TRACKED)
    (checkout / ".gitignore").write_text(f"{TRACKED}\n", encoding="utf-8")
    assert _staged_deletions(checkout) == [TRACKED]

    payload = obligations_module.obligations(PROJECT, SESSION)

    assert _notice_rows(payload) == []
    assert _log_lines() == []


def test_a_published_snapshot_carries_the_notice_to_the_prompt_path(
    checkout: Path,
) -> None:
    """The stop hook reads a published snapshot, so the notice must be in it.

    The prompt path loads no derivation module: it reads the session's
    published snapshot, formats its rows and asks its own mirrored vocabulary
    which of them hold the stop open. A notice that reached only the
    derivation would be invisible to every session whose row arrived by
    snapshot, which is the path production reads.
    """
    _git(checkout, "rm", "--cached", "-q", "--", TRACKED)

    state = obligation_snapshot.fleet_state(PROJECT)
    payload = obligation_snapshot.payload_for(state, SESSION)
    document = obligation_snapshot.document_for(
        payload,
        computed_at=state.now,
        stream_offset=0,
        producer={"pid": os.getpid()},
    )
    obligation_snapshot.write_snapshot(PROJECT, SESSION, document)

    published = obligation_snapshot.read_snapshot(PROJECT, SESSION)
    assert published is not None
    read_back = obligation_snapshot.live_payload(published)

    rows = _notice_rows(read_back)
    assert len(rows) == 1, read_back["obligations"]
    assert rows[0]["paths"] == [TRACKED], rows[0]
    assert hook._row_is_actionable(rows[0]) is False, rows[0]
    assert len(_log_lines()) == 1


def _home_entries(home: Path) -> tuple[bool, bool]:
    """Whether a crew home carries this reader's own files."""
    return (
        (home / obligations_module.INDEX_LAG_LOG_NAME).exists(),
        (home / obligations_module.INDEX_LAG_SEEN_NAME).exists(),
    )


def test_the_real_crew_home_is_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every write lands in the temporary home the fixture points at."""
    from reckon.crew.run_paths import crew_home

    # Resolved before the override, so the assertion names the home the
    # process would write to without this test's environment.
    real_home = crew_home()
    before = _home_entries(real_home)
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    repo = _repository(tmp_path)
    mounts = tmp_path / "mounts.json"
    mounts.write_text(json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8")
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))

    _git(repo, "rm", "--cached", "-q", "--", TRACKED)
    payload = obligations_module.obligations(PROJECT, SESSION)
    assert len(_notice_rows(payload)) == 1, payload["obligations"]
    assert len(_log_lines()) == 1

    assert _home_entries(real_home) == before
