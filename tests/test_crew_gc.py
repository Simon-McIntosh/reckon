from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from click.testing import CliRunner

from reckon import cli, ledger


SCRIPT = (
    Path(__file__).parents[1]
    / "skills"
    / "reckon-ship"
    / "scripts"
    / "worktree_fleet.py"
)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Test User")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "seed.txt").write_text("seed\n")
    git(repo, "add", "seed.txt")
    git(repo, "commit", "-q", "-m", "test: seed")
    return repo


def create_worktree(repo: Path, session: str, worker: str) -> Path:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "create",
            "--repo",
            str(repo),
            "--session",
            session,
            "--worker",
            worker,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(json.loads(result.stdout)["path"])


def write_pointer(home: Path, run_id: str, worktree: Path) -> None:
    live = home / "crew" / "live"
    live.mkdir(parents=True, exist_ok=True)
    (live / f"{run_id}.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "worktree": str(worktree),
                "phase": "working",
                "pid": 999999999,
            }
        )
    )


def record_shadow(
    repo: Path,
    home: Path,
    *,
    primary_run_id: str,
    run_id: str,
    node: str,
    retain_patch: bool,
) -> Path:
    artifact = home / "crew" / "runs" / run_id / "shadow.patch"
    if retain_patch:
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("retained evidence\n")
    ledger.append_run(
        "test",
        ledger.build_record(
            run_id=run_id,
            plan="plan-a",
            gate="passed",
            node=node,
            lineage={"kind": "shadow", "primary_run_id": primary_run_id},
            shadow_patch=str(artifact),
        ),
        root=repo,
    )
    return artifact


def test_gc_dry_run_itemizes_worktrees_without_touching_them(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    repo = repository(tmp_path)
    integrated = create_worktree(repo, "audit", "integrated")
    dirty = create_worktree(repo, "audit", "dirty")
    live = create_worktree(repo, "audit", "live")
    (dirty / "untracked.txt").write_text("dirty\n")
    write_pointer(home, "run-live", live)

    result = CliRunner().invoke(cli.main, ["crew", "gc", "--repo", str(repo)])
    payload = json.loads(result.output)

    assert result.exit_code == 0, result.output
    assert payload["dry_run"] is True
    assert payload["counts"] == {
        "dirty": 1,
        "disposable": 0,
        "integrated": 1,
        "live-referenced": 1,
        "unintegrated": 0,
    }
    by_path = {item["path"]: item for item in payload["worktrees"]}
    assert by_path[str(integrated)]["classification"] == "integrated"
    assert by_path[str(dirty)]["classification"] == "dirty"
    assert by_path[str(live)]["claimed_by_live_runs"] == ["run-live"]
    assert integrated.exists() and dirty.exists() and live.exists()


def test_gc_apply_removes_only_integrated_unclaimed_worktrees(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    repo = repository(tmp_path)
    integrated = create_worktree(repo, "apply", "integrated")
    dirty = create_worktree(repo, "apply", "dirty")
    live = create_worktree(repo, "apply", "live")
    (dirty / "untracked.txt").write_text("dirty\n")
    write_pointer(home, "run-live", live)

    result = CliRunner().invoke(
        cli.main, ["crew", "gc", "--repo", str(repo), "--apply"]
    )
    payload = json.loads(result.output)

    assert result.exit_code == 0, result.output
    assert payload["dry_run"] is False
    assert payload["removed_worktrees"] == [str(integrated)]
    assert not integrated.exists()
    assert dirty.exists() and live.exists()


def test_gc_reports_a_completed_shadow_with_a_retained_patch_as_disposable(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    repo = repository(tmp_path)
    shadow = create_worktree(repo, "shadow-primary", "candidate")
    (shadow / "candidate.txt").write_text("uncommitted shadow work\n")
    record_shadow(
        repo,
        home,
        primary_run_id="primary",
        run_id="shadow-run",
        node="candidate",
        retain_patch=True,
    )

    result = CliRunner().invoke(
        cli.main, ["crew", "gc", "--repo", str(repo), "--project", "test"]
    )
    payload = json.loads(result.output)
    item = next(item for item in payload["worktrees"] if item["path"] == str(shadow))

    assert result.exit_code == 0, result.output
    assert item["classification"] == "disposable"
    assert payload["counts"]["disposable"] == 1
    assert payload["counts"]["unintegrated"] == 0


def test_gc_apply_removes_a_disposable_shadow_and_retains_its_patch(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    repo = repository(tmp_path)
    shadow = create_worktree(repo, "shadow-primary", "candidate")
    (shadow / "candidate.txt").write_text("uncommitted shadow work\n")
    artifact = record_shadow(
        repo,
        home,
        primary_run_id="primary",
        run_id="shadow-run",
        node="candidate",
        retain_patch=True,
    )

    result = CliRunner().invoke(
        cli.main,
        ["crew", "gc", "--repo", str(repo), "--project", "test", "--apply"],
    )
    payload = json.loads(result.output)

    assert result.exit_code == 0, result.output
    assert payload["removed_worktrees"] == [str(shadow)]
    assert not shadow.exists()
    assert artifact.read_text() == "retained evidence\n"


def test_gc_withholds_a_shadow_whose_patch_is_absent(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    repo = repository(tmp_path)
    shadow = create_worktree(repo, "shadow-primary", "candidate")
    (shadow / "candidate.txt").write_text("uncommitted shadow work\n")
    record_shadow(
        repo,
        home,
        primary_run_id="primary",
        run_id="shadow-run",
        node="candidate",
        retain_patch=False,
    )

    result = CliRunner().invoke(
        cli.main,
        ["crew", "gc", "--repo", str(repo), "--project", "test", "--apply"],
    )
    payload = json.loads(result.output)
    item = next(item for item in payload["worktrees"] if item["path"] == str(shadow))

    assert result.exit_code == 0, result.output
    assert item["classification"] != "disposable"
    assert str(shadow) not in payload["removed_worktrees"]
    assert shadow.exists()


def test_gc_never_offers_a_live_referenced_shadow(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    repo = repository(tmp_path)
    shadow = create_worktree(repo, "shadow-primary", "candidate")
    record_shadow(
        repo,
        home,
        primary_run_id="primary",
        run_id="shadow-run",
        node="candidate",
        retain_patch=True,
    )
    write_pointer(home, "shadow-run", shadow)

    result = CliRunner().invoke(
        cli.main, ["crew", "gc", "--repo", str(repo), "--project", "test"]
    )
    payload = json.loads(result.output)
    item = next(item for item in payload["worktrees"] if item["path"] == str(shadow))

    assert result.exit_code == 0, result.output
    assert item["classification"] == "live-referenced"
    assert item["claimed_by_live_runs"] == ["shadow-run"]
    assert payload["counts"]["disposable"] == 0


def test_gc_still_withholds_a_non_shadow_unintegrated_worktree(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    repo = repository(tmp_path)
    unintegrated = create_worktree(repo, "ordinary", "candidate")
    (unintegrated / "candidate.txt").write_text("committed only here\n")
    git(unintegrated, "add", "candidate.txt")
    git(unintegrated, "commit", "-q", "-m", "test: divergent work")

    result = CliRunner().invoke(
        cli.main,
        ["crew", "gc", "--repo", str(repo), "--project", "test", "--apply"],
    )
    payload = json.loads(result.output)
    item = next(
        item for item in payload["worktrees"] if item["path"] == str(unintegrated)
    )

    assert result.exit_code == 0, result.output
    assert item["classification"] == "unintegrated"
    assert str(unintegrated) not in payload["removed_worktrees"]
    assert unintegrated.exists()
