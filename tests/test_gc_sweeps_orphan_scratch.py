"""Old unclaimed scratch is reclaimable without touching active or young work."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from click.testing import CliRunner

from reckon import cli
from reckon.crew import routing


def _fixture(tmp_path, monkeypatch):
    root = tmp_path / "scratch"
    root.mkdir()
    home = tmp_path / "state"
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(root))
    monkeypatch.setenv("RECKON_HOME", str(home))
    real_root = Path("/tmp/reckon-crew-scratch")  # noqa: S108 - real root comparison
    assert root != real_root
    return root, home, real_root


def _entry(root, name):
    path = root / name
    path.mkdir()
    (path / "payload").write_bytes(b"fixture")
    return path


def test_sweep_reports_and_removes_only_old_unclaimed_scratch(tmp_path, monkeypatch):
    root, home, real_root = _fixture(tmp_path, monkeypatch)
    live = _entry(root, "live")
    held = _entry(root, "held")
    young = _entry(root, "young")
    old = _entry(root, "old")
    pointers = home / "crew" / "live"
    pointers.mkdir(parents=True)
    (pointers / "live.json").write_text(json.dumps({"run_id": "live"}))
    now = time.time()
    original_ctime = routing._scratch_ctime
    monkeypatch.setattr(
        routing,
        "_scratch_ctime",
        lambda path: (
            now - 7201 if path.name in {"live", "held", "old"} else original_ctime(path)
        ),
    )
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os,sys,time; os.chdir(sys.argv[1]); print('ready',flush=True); time.sleep(30)",
            str(held),
        ],
        stdout=subprocess.PIPE,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline() == b"ready\n"
        dry = routing.garbage_collect_orphan_scratch(now=now)
        by_name = {Path(row["path"]).name: row for row in dry["entries"]}
        assert by_name["live"]["withheld"] == "live pointer"
        assert "held by process" in by_name["held"]["withheld"]
        assert "ctime within" in by_name["young"]["withheld"]
        assert by_name["old"]["withheld"] == ""
        assert dry["would_free_bytes"] == len(b"fixture")
        assert all(path.exists() for path in (live, held, young, old))
        applied = routing.garbage_collect_orphan_scratch(apply=True, now=now)
        assert applied["removed"] == [str(old)]
        assert applied["bytes_freed"] == len(b"fixture")
        assert all(path.exists() for path in (live, held, young))
        assert not old.exists()
        assert all(
            not str(row["path"]).startswith(str(real_root))
            for row in applied["entries"]
        )
    finally:
        holder.terminate()
        holder.wait(timeout=5)


def test_gc_rejects_a_different_root(tmp_path, monkeypatch):
    root, _, real_root = _fixture(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    root.rmdir()
    root.symlink_to(outside, target_is_directory=True)
    import pytest

    with pytest.raises(Exception, match="noncanonical root"):
        routing.garbage_collect_orphan_scratch(apply=True)
    assert outside.is_dir()
    assert root != real_root


def test_ordinary_gc_includes_scratch_without_applying_it(tmp_path, monkeypatch):
    root, _, real_root = _fixture(tmp_path, monkeypatch)
    old = _entry(root, "orphan")
    now = time.time()
    monkeypatch.setattr(routing, "_scratch_ctime", lambda path: now - 7201)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True
    )
    (repo / "seed").write_text("seed")
    subprocess.run(["git", "add", "seed"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, check=True)
    result = CliRunner().invoke(cli.main, ["crew", "gc", "--repo", str(repo)])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["scratch"]["entries"][0]["path"] == str(old)
    assert report["scratch"]["would_free_bytes"] == len(b"fixture")
    assert old.exists()
    assert root != real_root
