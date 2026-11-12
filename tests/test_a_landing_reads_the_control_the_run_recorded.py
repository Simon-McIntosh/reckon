"""A landing admits the control the run recorded, not the field's spelling.

A node declares the mutation its check must fail against, and the run's red log
is that mutation's evidence. Three landings were refused on the control line
alone: one log kept only its exit line, one manifest named a lint log, and one
named no log at all while a red log sat beside the manifest. In each case the
run had recorded the fact the declaration rests on; only the field disagreed.

The landing therefore reads the log the manifest names first and keeps it
whenever it admits the control. When it does not, the run's own directory is
searched for a log that does — one whose run failed and which names at least
one failing test id the head arm passes — and the log used is written on the
row. When the directory holds none, the landing runs the declared mutation
itself, over a checkout of the run's base revision, and admits the control on
that run's failing ids; a mutation that fails nothing is refused.

The tests drive the promotion entry point directly with a temporary run
directory, and the measurement cases drive the real runner over a temporary
repository, so the reading is shown to happen rather than described.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from reckon.crew.node import CrewError
from reckon.crew.promotion import _require_declared_negative_control

RUN_ID = "r-20261112T070000000000-recorded-control"
DECLARATION = (
    "read only the manifest-named control log; the exit-line-only case must refuse"
)
TEST_FILE = "tests/test_guard.py"
MUTATION_FAILS = f"{TEST_FILE}::test_the_guard_is_restored"
MUTATION_PASSES = f"{TEST_FILE}::test_an_unrelated_case"


def _observation(failure_ids: list[str], *, revision: str = "head") -> dict:
    """One suite observation in the shape a manifest records it."""
    return {
        "revision": revision,
        "command": "pytest -p no:cacheprovider -q " + TEST_FILE,
        "exit_status": 1 if failure_ids else 0,
        "completed": True,
        "failure_count": len(failure_ids),
        "failure_ids": failure_ids,
    }


def _red_log(failure_ids: list[str], *, exit_line: bool = True) -> str:
    """A pytest log whose run failed, optionally without its capture line."""
    listed = "".join(f"FAILED {test_id}\n" for test_id in failure_ids)
    body = f"{listed}{len(failure_ids)} failed\n"
    return body + ("EXIT=1\n" if exit_line else "")


def _gate(
    tmp_path: Path,
    *,
    named: str | None,
    files: dict[str, str],
    head_ids: list[str] | None = None,
    record: dict | None = None,
) -> dict:
    """Run the control gate over one run directory and the manifest naming it."""
    run_directory = tmp_path / "run"
    run_directory.mkdir(exist_ok=True)
    for name, text in files.items():
        (run_directory / name).write_text(text, encoding="utf-8")
    manifest: dict = {}
    if named is not None:
        manifest["negative_control_log"] = named
    if head_ids is not None:
        manifest["after_suite"] = _observation(head_ids)
    run_record = {
        "run_id": RUN_ID,
        "node": {
            "id": "recorded-control",
            "write_paths": [TEST_FILE],
            "negative_control": DECLARATION,
        },
    }
    if record is not None:
        run_record.update(record)
    return _require_declared_negative_control(
        RUN_ID,
        run_record,
        gate="passed",
        manifest=manifest,
        manifest_path=str(run_directory / "manifest.md"),
    )


def test_an_exit_line_only_log_falls_back_to_the_run_directory(tmp_path: Path) -> None:
    check = _gate(
        tmp_path,
        named="exit-only.log",
        files={
            "exit-only.log": "EXIT=1\n",
            "negative-control.log": _red_log([MUTATION_FAILS]),
        },
        head_ids=[],
    )

    assert check["verdict"] == "matched"
    assert check["control_log_source"] == "run-directory"
    assert Path(check["resolved_log"]).name == "negative-control.log"
    assert check["added_failure_ids"] == [MUTATION_FAILS]


def test_a_manifest_naming_no_log_is_admitted_on_the_recorded_one(
    tmp_path: Path,
) -> None:
    check = _gate(
        tmp_path,
        named="",
        files={"negative-control.log": _red_log([MUTATION_FAILS])},
        head_ids=[],
    )

    assert check["verdict"] == "matched"
    assert check["control_log_source"] == "run-directory"
    assert Path(check["resolved_log"]).name == "negative-control.log"


def test_a_log_of_another_mutation_is_passed_over(tmp_path: Path) -> None:
    # A red log, but its run failed the case the head arm fails as well: it
    # records a different mutation and admits nothing.
    check = _gate(
        tmp_path,
        named="lint.log",
        files={
            "lint.log": _red_log([MUTATION_PASSES]),
            "negative-control.log": _red_log([MUTATION_FAILS]),
        },
        head_ids=[MUTATION_PASSES],
    )

    assert check["verdict"] == "matched"
    assert Path(check["resolved_log"]).name == "negative-control.log"
    assert check["added_failure_ids"] == [MUTATION_FAILS]


def test_a_valid_named_log_is_used_and_no_other_is_read(tmp_path: Path) -> None:
    # The decoy is admissible on its own, and sorts first, so reading any log
    # but the named one would show up here.
    check = _gate(
        tmp_path,
        named="named-control.log",
        files={
            "aaa-control.log": _red_log([MUTATION_FAILS]),
            "named-control.log": _red_log([MUTATION_FAILS]),
        },
        head_ids=[],
    )

    assert check["verdict"] == "matched"
    assert check["control_log_source"] == "manifest"
    assert Path(check["resolved_log"]).name == "named-control.log"


def test_a_run_keeping_no_red_log_is_measured_by_the_landing(
    tmp_path: Path,
) -> None:
    repo, base_sha = _mutation_repository(tmp_path, restored_at_base=False)
    check = _gate(
        tmp_path,
        named="",
        files={},
        head_ids=[],
        record={
            "project": "sample",
            "repo": str(repo),
            "worktree": str(repo),
            "base_sha": base_sha,
            "suite_command": _suite_command(),
        },
    )

    assert check["verdict"] == "matched"
    assert check["control_log_source"] == "landing-measure"
    assert MUTATION_FAILS in check["control_failure_ids"]
    assert Path(check["resolved_log"]).is_file()


def test_a_mutation_that_fails_nothing_is_refused(tmp_path: Path) -> None:
    repo, base_sha = _mutation_repository(tmp_path, restored_at_base=True)
    with pytest.raises(CrewError) as refusal:
        _gate(
            tmp_path,
            named="",
            files={},
            head_ids=[],
            record={
                "project": "sample",
                "repo": str(repo),
                "worktree": str(repo),
                "base_sha": base_sha,
                "suite_command": _suite_command(),
            },
        )

    assert "ran the declared mutation itself" in str(refusal.value)


def _suite_command() -> str:
    return f"{sys.executable} -m pytest -p no:cacheprovider -q {{node_test_files}}"


def _git(repo: Path, *arguments: str) -> str:
    finished = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        check=True,
    )
    return finished.stdout.strip()


def _mutation_repository(tmp_path: Path, *, restored_at_base: bool) -> tuple[Path, str]:
    """A repository whose guard is restored at the head and absent at the base.

    The node's test measures the restored guard, so it fails at the base — the
    mutation in place — and passes at the head. ``restored_at_base`` builds the
    other repository: the guard is restored in both revisions, so the mutation
    fails nothing and a control that reads it proves nothing.
    """
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_guard.py").write_text(
        "import guard\n\n"
        "def test_the_guard_is_restored():\n"
        "    assert guard.GUARD\n\n"
        "def test_an_unrelated_case():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    (repo / "guard.py").write_text(
        f"GUARD = {'True' if restored_at_base else 'False'}\n", encoding="utf-8"
    )
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "control-fixture")
    _git(repo, "config", "user.email", "control-fixture@example.invalid")
    _git(repo, "add", "guard.py", "tests/test_guard.py")
    _git(repo, "commit", "-q", "-m", "the guard before the repair")
    base_sha = _git(repo, "rev-parse", "HEAD")
    (repo / "guard.py").write_text("GUARD = True\n", encoding="utf-8")
    _git(repo, "add", "guard.py")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "restore the guard")
    return repo, base_sha
