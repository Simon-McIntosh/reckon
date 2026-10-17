"""A paired arm is held until its partner finishes.

A paired dispatch starts both arms from one base so neither can find the
other's landed answer. That holds only while neither lands first: an arm
promoted while its partner is still running puts its result in the main
checkout, and the partner can read it from there exactly as a shadow
dispatched after a merge can. The hold is one precondition on promotion,
naming the partner and the phase it is in.

The same module also drives ``crew complete`` on a shadow whose stream reads
its primary's run directory, so the record's contamination verdict — taken in
``_complete_locked`` where both the primary's row and the run's own stream are
in hand — is exercised rather than only reported.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import _plan_html, crew, ledger
from reckon.crew.node import CrewError
from reckon.crew.runs import _write_json, pointer_path, run_dir

PROJECT = "proj"
PLAN = "plan-a"
SECTION = "approved"


def _write_resource(path: Path, state: Path | dict) -> None:
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
        _write_git(root, *arguments)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _write_git(root, "add", "seed.txt", "docs")
    _write_git(root, "commit", "-q", "-m", "test: seed repository")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _write_git(cwd: Path, *arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *arguments], cwd=cwd, check=True, capture_output=True, text=True
    )


def _head(repo: Path) -> str:
    return _write_git(repo, "rev-parse", "HEAD").stdout.strip()


def _pointer(
    repo: Path,
    run_id: str,
    *,
    phase: str = "",
    manifest_path: str = "/durable/manifest.md",
    **extra,
) -> dict:
    """One in-harness live pointer shaped as a promotion otherwise accepts."""
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repo),
        "launch": "in-harness",
        "role": "implement",
        "backend": "native",
        "created_at": "2026-10-17T00:00:00Z",
        "manifest_path": manifest_path,
        "node": {
            "id": "node-a",
            "plan": PLAN,
            "section": SECTION,
            "time_budget": "25m",
            "write_paths": [],
        },
        **extra,
    }
    if phase:
        record["phase"] = phase
    _write_json(pointer_path(run_id), record)
    return record


def _write_partner(repo: Path, run_id: str, phase: str) -> None:
    _pointer(repo, run_id, phase=phase)


def _promote(repo: Path, run_id: str) -> dict:
    return crew.complete(run_id, gate="passed", outcome="landed", root=repo)


def test_a_paired_arm_is_refused_while_its_partner_works(repository: Path) -> None:
    """Promoting an arm whose partner is still working is refused, nothing lands."""
    arm = "r-20261017T000000000001-paired-arm"
    partner = "r-20261017T000000000002-paired-shadow"
    _write_partner(repository, partner, "working")
    _pointer(repository, arm, paired_shadow_run_id=partner)

    with pytest.raises(CrewError) as refusal:
        _promote(repository, arm)

    message = str(refusal.value)
    assert partner in message
    assert "working" in message
    # Nothing merged: the run holds no ledger row and its pointer survives.
    runs = ledger.load(PROJECT, root=repository)[0]["runs"]
    assert not any(str(row.get("run_id") or "") == arm for row in runs)
    assert pointer_path(arm).exists()


def test_the_shadow_arm_is_refused_while_its_primary_works(repository: Path) -> None:
    """The hold reads the shadow's lineage too, so either arm can be held."""
    primary = "r-20261017T000000000003-paired-primary"
    shadow = "r-20261017T000000000004-paired-shadow"
    _write_partner(repository, primary, "starting")
    _pointer(
        repository,
        shadow,
        lineage={"kind": "shadow", "primary_run_id": primary},
    )

    with pytest.raises(CrewError) as refusal:
        _promote(repository, shadow)

    message = str(refusal.value)
    assert primary in message
    assert "starting" in message
    runs = ledger.load(PROJECT, root=repository)[0]["runs"]
    assert not any(str(row.get("run_id") or "") == shadow for row in runs)


def test_a_paired_arm_promotes_once_its_partner_is_terminal(
    repository: Path,
) -> None:
    """A terminal partner phase lifts the hold."""
    arm = "r-20261017T000000000005-paired-arm"
    partner = "r-20261017T000000000006-paired-shadow"
    _write_partner(repository, partner, "complete")
    _pointer(repository, arm, paired_shadow_run_id=partner)

    stored = _promote(repository, arm)

    assert stored["run_id"] == arm
    assert not pointer_path(arm).exists()


def test_a_paired_arm_promotes_once_its_partner_is_gone(repository: Path) -> None:
    """A partner already promoted leaves no pointer, which is terminal too."""
    arm = "r-20261017T000000000007-paired-arm"
    _pointer(
        repository,
        arm,
        paired_shadow_run_id="r-20261017T000000000008-already-promoted",
    )

    stored = _promote(repository, arm)

    assert stored["run_id"] == arm


def test_an_unpaired_run_promotes_unchanged(repository: Path) -> None:
    """A run naming no partner is not held."""
    arm = "r-20261017T000000000009-unpaired"
    _pointer(repository, arm)

    stored = _promote(repository, arm)

    assert stored["run_id"] == arm


def test_a_shadow_of_an_already_promoted_primary_promotes_unchanged(
    repository: Path,
) -> None:
    """A standalone shadow whose primary has landed names a gone partner."""
    primary = "r-20261017T000000000010-promoted-primary"
    _pointer(repository, primary)
    _promote(repository, primary)
    assert not pointer_path(primary).exists()

    shadow = "r-20261017T000000000011-standalone-shadow"
    _pointer(
        repository,
        shadow,
        worktree=str(repository),
        base_sha=_head(repository),
        lineage={"kind": "shadow", "primary_run_id": primary},
    )

    stored = _promote(repository, shadow)

    assert stored["run_id"] == shadow


def _stream_reading(primary_run_id: str, path: Path) -> str:
    """A message-dialect stream whose one tool call cats the primary's manifest."""
    target = run_dir(primary_run_id) / "manifest.md"
    event = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "name": "Bash",
                    "input": {"command": f"cat {target}"},
                }
            ],
        },
    }
    path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    return str(path)


def test_a_shadow_reading_its_primarys_run_directory_is_stored_uncontrolled(
    repository: Path, tmp_path: Path
) -> None:
    """The row a promoting shadow stores carries the scan's own verdict.

    The shadow's stream reads its primary's run directory, and the stored row
    must carry both marks the control predicate and the scan agree on: the
    contamination reason, and a controlled verdict turned false. This guards
    the ``_shadow_control_marks`` call in ``_complete_locked``, which re-takes
    the verdict once the stream has been read.
    """
    primary = "r-20261017T000000000012-contamination-primary"
    _pointer(repository, primary)
    _promote(repository, primary)

    run_dir(primary).mkdir(parents=True, exist_ok=True)
    (run_dir(primary) / "manifest.md").write_text(
        "status: complete\n", encoding="utf-8"
    )

    shadow = "r-20261017T000000000013-contamination-shadow"
    stream = _stream_reading(primary, tmp_path / "stream.jsonl")
    _pointer(
        repository,
        shadow,
        log_path=stream,
        worktree=str(repository),
        base_sha=_head(repository),
        lineage={"kind": "shadow", "primary_run_id": primary},
    )

    stored = _promote(repository, shadow)["record"]

    assert stored["shadow_contaminated"] == "primary_run_directory_read"
    assert stored["shadow_controlled"] is False
