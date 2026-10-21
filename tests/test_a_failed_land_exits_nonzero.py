"""A land step that fails after promotion exits non-zero and names the failure.

``crew complete --land`` promotes and releases a run before it merges the
reviewed head, so a merge that raises anything other than the merge module's
own refusal used to escape the land step: no ``steps.land`` entry was reported,
the command's exit status said nothing about the failure, and the promoted run
reached no reader. The land step now reports any exception the merge raises as
a failed land step, naming the exception's declared type and message and
offering the command that resumes the landing. ``crew merge`` maps the same
failure to a refusal rather than a traceback.

The declared mutation narrows the handler back to ``MergeError``: the patched
``UnicodeDecodeError`` then escapes ``crew complete --land`` and the first case
fails, which is the red log the manifest names.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon.crew import merge
from reckon.crew.runs import _write_json, pointer_path
from tests.helpers.git import git as _git
from tests.helpers.landing_repository import (
    PLAN,
    PROJECT,
    _complete_land,
    _write_origin_and_checkout,
)

MARKER = "the reviewed change survives the merge\n"


def _decode_error() -> UnicodeDecodeError:
    return UnicodeDecodeError("utf-8", b"\x87", 10, 11, "invalid start byte")


def _raise_on_land(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    def _land_merge_raises(*_args, **_kwargs):
        raise exc

    monkeypatch.setattr(merge, "land_merge", _land_merge_raises)


def _merge_argv(run: dict) -> list[str]:
    return [
        "crew",
        "merge",
        "--project",
        PROJECT,
        "--head",
        run["head"],
        "--message",
        "land the reviewed head",
        "--body",
        "the reviewed head reaches every reader",
    ]


@pytest.fixture()
def landed_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A finished run whose reviewed change is committed in its own worktree."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    origin, repository = _write_origin_and_checkout(tmp_path)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    run_id = "r-20261019T140000000000-a-failed-land-exits-nonzero"
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _git(repository, "worktree", "add", "--detach", str(worktree), "HEAD")
    (worktree / "reviewed.txt").write_text(MARKER, encoding="utf-8")
    _git(worktree, "add", "reviewed.txt")
    _git(worktree, "commit", "-q", "-m", "add the reviewed change")
    head = _git(worktree, "rev-parse", "HEAD")
    manifest = tmp_path / "manifests" / (run_id + ".md")
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("node: node-a\nstatus: complete\n", encoding="utf-8")
    gate_log = tmp_path / "gate.log"
    gate_log.write_text("# command: true\nEXIT=0\n", encoding="utf-8")
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(worktree),
            "launch": "in-harness",
            "role": "implement",
            "member": "worker-a",
            "backend": "native",
            "created_at": "2026-10-19T14:00:00Z",
            "base_sha": _git(repository, "rev-parse", "HEAD"),
            "manifest_path": str(manifest),
            "node": {
                "id": "node-a",
                "plan": PLAN,
                "section": "§2",
                "time_budget": "35m",
                "write_paths": ["reviewed.txt"],
            },
        },
    )
    return {
        "origin": origin,
        "repository": repository,
        "worktree": worktree,
        "run_id": run_id,
        "head": head,
        "gate_log": gate_log,
    }


def test_a_land_that_raises_after_promotion_exits_nonzero(
    landed_run: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = landed_run
    _raise_on_land(monkeypatch, _decode_error())
    result = _complete_land(run)

    assert result.exit_code != 0, result.output
    report = json.loads(result.output)
    assert report["ok"] is False, report
    assert report["steps"]["promote"]["ok"] is True, report
    land = report["steps"]["land"]
    assert land["ok"] is False, land
    assert "UnicodeDecodeError" in land["error"], land
    assert land["resume"].startswith("reckon crew merge"), land


def test_crew_merge_reports_a_non_merge_error_as_a_refusal(
    landed_run: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = landed_run
    _raise_on_land(monkeypatch, _decode_error())
    result = CliRunner().invoke(cli_module.main, _merge_argv(run))

    assert result.exit_code != 0, result.output
    assert "UnicodeDecodeError" in result.output, result.output
    assert "Traceback" not in result.output, result.output


def test_a_merge_error_refusal_reports_as_at_base(
    landed_run: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = landed_run
    _raise_on_land(monkeypatch, merge.MergeError("the merge dropped the change"))
    result = _complete_land(run)

    assert result.exit_code != 0, result.output
    report = json.loads(result.output)
    land = report["steps"]["land"]
    assert land["ok"] is False, land
    assert land["error"] == "the merge dropped the change", land
    assert land["resume"].startswith("reckon crew merge"), land
