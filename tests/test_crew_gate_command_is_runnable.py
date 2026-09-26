"""A recorded gate command is re-executed, so a description of a check is refused.

The integration re-run executes a run's recorded gate text verbatim through a
shell at the merged head. Measured 2026-09-20: a command whose file set was
written as a description re-ran pytest against a path that does not exist, and
the ledger recorded a merge finding against a node that was clean. These cases
pin the two halves of the repair — the promotion path refuses the description,
and the re-run reports what it could not run distinctly from what ran and
failed.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import _plan_html, crew, ledger
from reckon.cli import main as cli_main
from reckon.crew import promotion
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "proj"
PLAN = "plan-a"

# The gate command recorded on r-20260926T095413623438: the real case the
# refusal exists for, kept verbatim so the class it is refused under is the one
# a coordinator actually wrote.
RECORDED_PROSE_COMMAND = (
    "/repo/.venv/bin/python -m pytest -p no:cacheprovider -q -n 8 "
    "--timeout=300 (the base-passing population of remeasure/measured-files.txt,"
    " listed in the log header)"
)
PROSE_CLASS = "parenthetical prose selection"


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write_resource(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{state['slug']}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A promotable repository under a temporary config home."""
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
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(root, *arguments)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt", "docs")
    _git(root, "commit", "-q", "-m", "test: seed repository")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _write_pointer(repository: Path, run_id: str) -> None:
    """A run the promotion path accepts, so only the gate command is on trial."""
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "launch": "in-harness",
            "role": "implement",
            "backend": "native",
            "created_at": "2026-09-26T09:00:00Z",
            "node": {
                "id": "gate-command",
                "plan": PLAN,
                "section": "gate-command",
                "time_budget": "25m",
                "write_paths": ["seed.txt"],
            },
        },
    )


def _real_config_home_pointer(run_id: str) -> Path:
    """Where this run's pointer would land if the test escaped its config home."""
    return Path.home() / ".config" / "reckon" / "crew" / "live" / f"{run_id}.json"


def _gate_check(command: str) -> dict:
    return {"command": command, "exit_status": 0, "log_digest": "x"}


@pytest.mark.parametrize(
    ("command", "token_class", "token", "run_slug"),
    [
        (
            RECORDED_PROSE_COMMAND,
            PROSE_CLASS,
            "(the base-passing population",
            "prose-recorded",
        ),
        (
            "/repo/.venv/bin/python -m pytest <nine focused files>",
            "angle-bracket placeholder",
            "<nine focused files>",
            "angle-bracket",
        ),
        (
            "/repo/.venv/bin/python -m pytest tests/a.py tests/b.py ...",
            "ellipsis",
            "...",
            "ellipsis",
        ),
    ],
)
def test_a_promotion_refuses_a_gate_command_it_cannot_re_run(
    repository: Path, command: str, token_class: str, token: str, run_slug: str
) -> None:
    """Promoting a run whose recorded gate text describes the check raises, and
    the refusal names the token class and the token."""
    run_id = f"r-20260926T090000000000-gate-command-{run_slug}"
    _write_pointer(repository, run_id)

    with pytest.raises(crew.CrewError) as refused:
        crew.complete(
            run_id,
            gate="passed",
            outcome="the node's own measure",
            root=repository,
            gate_check=_gate_check(command),
            require_gate_check=True,
        )

    message = str(refused.value)
    assert token_class in message
    assert token in message
    assert run_id in message
    # A refused promotion leaves the run live and its ledger unwritten, so the
    # record can be corrected and promoted again rather than lost, and the
    # pointer's survival is the receipt that nothing landed.
    assert pointer_path(run_id).is_file()
    rows = ledger.load(PROJECT, root=repository)[0]["runs"]
    assert all(str(row.get("run_id") or "") != run_id for row in rows)


def test_the_refused_promotion_writes_nothing_to_the_real_config_home(
    repository: Path,
) -> None:
    """The temporary config home is the only one written: the run's own pointer
    never appears under the real one, which is what proves the isolation."""
    run_id = "r-20260926T090500000000-gate-command-untouched"
    escaped = _real_config_home_pointer(run_id)
    assert not escaped.exists(), "a stale pointer of this run's own id is present"
    _write_pointer(repository, run_id)

    with pytest.raises(crew.CrewError):
        crew.complete(
            run_id,
            gate="passed",
            outcome="the node's own measure",
            root=repository,
            gate_check=_gate_check(RECORDED_PROSE_COMMAND),
            require_gate_check=True,
        )

    assert not escaped.exists()
    assert pointer_path(run_id).is_file()


def test_a_runnable_gate_command_still_promotes(repository: Path) -> None:
    """The refusal judges the shapes that cannot run, not the spelling of a
    command that can: an ordinary gate command promotes and is recorded."""
    run_id = "r-20260926T090600000000-gate-command-runnable"
    _write_pointer(repository, run_id)
    command = "/repo/.venv/bin/python -m pytest -q tests/test_crew_promotion.py"

    crew.complete(
        run_id,
        gate="passed",
        outcome="the node's own measure",
        root=repository,
        gate_check=_gate_check(command),
        require_gate_check=True,
    )

    row = ledger.load(PROJECT, root=repository)[0]["runs"][0]
    assert row["gate_check"]["command"] == command


def test_complete_command_refuses_a_gate_command_that_cannot_run(
    repository: Path,
) -> None:
    """The promotion entry point refuses it too, and says which class it read."""
    run_id = "r-20260926T090700000000-gate-command-cli"
    _write_pointer(repository, run_id)

    result = CliRunner().invoke(
        cli_main,
        [
            "crew",
            "complete",
            "--run",
            run_id,
            "--gate",
            "passed",
            "--gate-command",
            "/repo/.venv/bin/python -m pytest <nine focused files>",
            "--gate-exit-status",
            "0",
            "--gate-log-digest",
            "x",
            "--outcome",
            "the node's own measure",
            "--checkout-path",
            str(repository),
        ],
    )

    assert result.exit_code == 1
    assert "angle-bracket placeholder" in result.output
    assert pointer_path(run_id).is_file()


def test_the_re_run_reports_a_prose_command_as_not_run(repository: Path) -> None:
    """A stored description cannot run, so the re-run reports not-run with the
    class it read rather than a failure the integrated revision did not cause."""
    revision = _git(repository, "rev-parse", "HEAD")

    report = promotion.rerun_gate_at_integrated_revision(
        repository=repository,
        gate_check=_gate_check(RECORDED_PROSE_COMMAND),
        integrated_revision=revision,
    )

    assert report["integrated_verdict"] == "not-run"
    assert report["ran"] is False
    assert report["exit_status"] is None
    assert PROSE_CLASS in str(report["reason"])
    # The push is still held — the merged tree was not verified — but the row
    # the coordinator reads carries the class rather than an exit status, which
    # is the difference between an unmeasured re-run and a failure.
    finding = report["finding"]
    assert finding["integrated_verdict"] == "not-run"
    assert finding["exit_status"] is None
    assert PROSE_CLASS in str(finding["reason"])


def test_the_re_run_reports_a_command_that_ran_and_failed_as_failed(
    repository: Path,
) -> None:
    """The distinction the report exists to make: a gate that ran and exited
    non-zero is a failure, not an unmeasured re-run."""
    revision = _git(repository, "rev-parse", "HEAD")

    report = promotion.rerun_gate_at_integrated_revision(
        repository=repository,
        gate_check=_gate_check("exit 3"),
        integrated_revision=revision,
    )

    assert report["integrated_verdict"] == "failed"
    assert report["ran"] is True
    assert report["exit_status"] == 3
    assert report["reason"] is None
    assert report["finding"]["integrated_verdict"] == "failed"
    assert report["finding"]["exit_status"] == 3


def test_a_refused_re_run_never_executes_the_description(repository: Path) -> None:
    """The marker is the receipt: a description carrying a side effect leaves no
    trace when the shape check refuses it, and leaves one when it does not."""
    revision = _git(repository, "rev-parse", "HEAD")
    marker = repository / "prose.marker"
    command = (
        f"/repo/.venv/bin/python -m pytest <the focused files> ; "
        f"echo attempted > {marker.name}"
    )

    report = promotion.rerun_gate_at_integrated_revision(
        repository=repository,
        gate_check=_gate_check(command),
        integrated_revision=revision,
    )

    assert report["integrated_verdict"] == "not-run"
    assert "angle-bracket placeholder" in str(report["reason"])
    assert not marker.exists()


def test_a_supplied_command_is_judged_by_the_same_shape(repository: Path) -> None:
    """A caller measuring a wider suite with --command gets the same refusal:
    the option replaces the stored text, it does not exempt it."""
    revision = _git(repository, "rev-parse", "HEAD")

    report = promotion.rerun_gate_at_integrated_revision(
        repository=repository,
        gate_check=_gate_check("echo stored"),
        integrated_revision=revision,
        command="/repo/.venv/bin/python -m pytest <the focused files>",
    )

    assert report["integrated_verdict"] == "not-run"
    assert report["gate_command_source"] == "option"
    assert "angle-bracket placeholder" in str(report["reason"])


def test_verify_gate_command_reports_a_stored_prose_command_as_not_run(
    repository: Path,
) -> None:
    """The verify-gate entry point reads a promoted row and reports the stored
    description as not-run on the payload and on the row it commits."""
    run_id = "r-20260926T090800000000-gate-command-verify"
    ledger.append_run(
        PROJECT,
        {
            "run_id": run_id,
            "gate": "passed",
            "gate_check": _gate_check(RECORDED_PROSE_COMMAND),
        },
        root=repository,
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "crew",
            "verify-gate",
            "--project",
            PROJECT,
            "--run",
            run_id,
            "--checkout-path",
            str(repository),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    report = payload["report"]
    assert report["integrated_verdict"] == "not-run"
    assert report["ran"] is False
    assert PROSE_CLASS in str(report["reason"])
    recorded = ledger.load(PROJECT, root=repository)[0]["runs"][0]
    assert recorded["integrated_gate_check"]["integrated_verdict"] == "not-run"
