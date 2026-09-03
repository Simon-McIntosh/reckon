from __future__ import annotations

import inspect
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
    repo.mkdir(parents=True)
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


def register_mount(home: Path, project: str, repo: Path) -> None:
    mounts_file = home / "mounts.json"
    mounts_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {}
    if mounts_file.is_file():
        payload = json.loads(mounts_file.read_text())
    payload.setdefault("mounts", {})[project] = str(repo / "docs")
    mounts_file.write_text(json.dumps(payload))


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
        # What `--apply` would actually reclaim. `disposable` alone read as the
        # headline said 0 while dozens of integrated worktrees were removable,
        # so a caller concluded nothing was reclaimable and the accumulation
        # grew — measured at 46 worktrees in one project, 40 of them integrated.
        "reclaimable": 1,
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


def test_gc_never_offers_a_live_referenced_shadow(tmp_path: Path, monkeypatch) -> None:
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


def test_a_withheld_worktree_says_which_condition_holds_it(tmp_path, monkeypatch):
    """A caller cannot tell "withheld on purpose" from "the classifier missed it".

    Both look like an integrated worktree that was not offered, and the remedies
    are opposite: one is a condition to clear, the other a defect to report. So
    every row that `--apply` would not reclaim names the condition.
    """
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
    rows = {Path(item["path"]).name: item for item in payload["worktrees"]}

    assert rows[integrated.name]["reclaimable"] is True
    assert "withheld" not in rows[integrated.name]

    assert rows[dirty.name]["reclaimable"] is False
    assert "uncommitted changes" in rows[dirty.name]["withheld"]
    assert "exists nowhere else" in rows[dirty.name]["withheld"]

    assert rows[live.name]["reclaimable"] is False
    assert "live run pointer" in rows[live.name]["withheld"]


def test_the_reclaimable_set_is_exactly_what_apply_removes(tmp_path, monkeypatch):
    """The report and the removal branch must not drift apart.

    The report is what a caller decides from, so a classification the report
    calls reclaimable has to be one `--apply` acts on, and the reverse. Asserted
    against the vocabulary rather than by running a destructive pass.
    """
    from reckon.crew import routing

    assert set(routing.RECLAIMABLE_CLASSES) == {"integrated", "disposable"}
    assert set(routing.WITHHELD_REASONS) == {
        "dirty",
        "unintegrated",
        "live-referenced",
    }
    assert not set(routing.RECLAIMABLE_CLASSES) & set(routing.WITHHELD_REASONS)

    source = inspect.getsource(routing.garbage_collect)
    branch = source.split('if item["classification"] not in (', 1)[1].split(")", 1)[0]
    for name in routing.RECLAIMABLE_CLASSES:
        assert f'"{name}"' in branch, f"{name} is reported reclaimable but not removed"


def test_gc_with_no_repo_resolves_the_named_projects_registered_checkout(
    tmp_path: Path, monkeypatch
) -> None:
    """A named project must resolve its own checkout, never the caller's cwd.

    Measured defect: `--repo` defaulted to the enclosing repository, so three
    different projects run from the reckon checkout returned byte-identical
    counts scanned from reckon's own worktrees rather than each project's.
    Asserted from a third directory belonging to neither the registered
    checkout nor any other registered project.
    """
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    nova = repository(tmp_path / "nova")
    register_mount(home, "nova", nova)
    reclaimable = create_worktree(nova, "audit", "reclaimable")
    git(nova, "worktree", "prune")  # no-op; keeps intent explicit
    third_directory = tmp_path / "elsewhere"
    third_directory.mkdir()
    monkeypatch.chdir(third_directory)

    result = CliRunner().invoke(cli.main, ["crew", "gc", "--project", "nova"])
    payload = json.loads(result.output)

    assert result.exit_code == 0, result.output
    assert payload["repo"] == str(nova.resolve())
    by_path = {item["path"]: item for item in payload["worktrees"]}
    assert str(reclaimable) in by_path


def test_gc_refuses_an_explicit_repo_disagreeing_with_the_registered_checkout(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    nova = repository(tmp_path / "nova")
    register_mount(home, "nova", nova)
    other = repository(tmp_path / "other")

    result = CliRunner().invoke(
        cli.main, ["crew", "gc", "--project", "nova", "--repo", str(other)]
    )

    assert result.exit_code != 0
    assert str(nova.resolve()) in result.output
    assert str(other.resolve()) in result.output
    assert "--confirm-cross-repo" in result.output


def test_gc_confirm_cross_repo_scans_the_stated_repository_deliberately(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    nova = repository(tmp_path / "nova")
    register_mount(home, "nova", nova)
    other = repository(tmp_path / "other")
    reclaimable = create_worktree(other, "audit", "reclaimable")

    result = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "gc",
            "--project",
            "nova",
            "--repo",
            str(other),
            "--confirm-cross-repo",
        ],
    )
    payload = json.loads(result.output)

    assert result.exit_code == 0, result.output
    assert payload["repo"] == str(other.resolve())
    by_path = {item["path"]: item for item in payload["worktrees"]}
    assert str(reclaimable) in by_path


def test_gc_explicit_repo_matching_the_registered_checkout_is_not_refused(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    nova = repository(tmp_path / "nova")
    register_mount(home, "nova", nova)

    result = CliRunner().invoke(
        cli.main, ["crew", "gc", "--project", "nova", "--repo", str(nova)]
    )
    payload = json.loads(result.output)

    assert result.exit_code == 0, result.output
    assert payload["repo"] == str(nova.resolve())


def test_gc_result_names_the_repository_and_the_ledger_it_read(
    tmp_path: Path, monkeypatch
) -> None:
    """A result cannot be attributed to the wrong project by a reader."""
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    repo = repository(tmp_path)
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

    assert result.exit_code == 0, result.output
    assert payload["repo"] == str(repo.resolve())
    assert payload["ledger"] == [
        str((repo / "docs" / "state" / "test" / "crew.json").resolve())
    ]
