"""gc bounds a promoted run's captured file to the configured size.

A promoted run keeps the streams a later reader opens as evidence. Nothing else
bounds them, so a runaway log grows with the run directory's retention. These
tests build a promoted run directory holding an over-limit log and an
over-limit checkpoint, and assert gc bounds the log to its head and tail around
a marker naming the original size and sha256, reports the checkpoint and leaves
it untouched, and never reaches a run the ledger does not record.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

from click.testing import CliRunner

from reckon import cli, ledger

PROJECT = "gc-proj"
HEAD_LINE = "HEAD-LINE the worker's first line"
TAIL_LINE = "TAIL-LINE the worker's last line"
FILLER = "filler line of some bytes padding the captured stream"
MiB = 1024 * 1024


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
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


def _host_limit(home: Path, mb: int) -> None:
    """Set the run-file bound in the host flight layer.

    The limit is a host or project layer's to set — the shipped defaults layer
    carries no ``gc`` block — so the test sets it where a real host would.
    """
    home.mkdir(parents=True, exist_ok=True)
    (home / "flight.yaml").write_text(f"gc:\n  run_file_max_mb: {mb}\n")


def _write_log(path: Path, size: int) -> None:
    """Write a log of roughly ``size`` bytes with a distinct first and last line."""
    body = (HEAD_LINE + "\n").encode()
    body += (FILLER + "\n").encode() * ((size // (len(FILLER) + 1)) - 2)
    body += (TAIL_LINE + "\n").encode()
    path.write_bytes(body)


def _promoted_run(
    repo: Path,
    home: Path,
    run_id: str,
    *,
    extra_manifest: str = "",
) -> tuple[Path, Path, Path, Path]:
    """Build a promoted run directory with an over-limit log and checkpoint.

    Returns the run directory, the log, the checkpoint and the manifest path.
    """
    run_dir = home / "crew" / "runs" / run_id
    run_dir.mkdir(parents=True)
    log = run_dir / "worker.log"
    checkpoint = run_dir / "checkpoint.pt"
    manifest = run_dir / "manifest.md"
    _write_log(log, 3 * MiB)
    checkpoint.write_bytes(b"\x00" * (3 * MiB))
    manifest.write_text(
        f"status: complete\nnode: node-a\n{extra_manifest}", encoding="utf-8"
    )
    record = ledger.build_record(
        run_id=run_id,
        plan="plan-a",
        gate="passed",
        node="node-a",
        project=PROJECT,
        manifest_path=str(manifest),
    )
    ledger.append_run(PROJECT, record, root=repo)
    return run_dir, log, checkpoint, manifest


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _marker_length(size: int, digest: str) -> int:
    return len(
        f"\n[reckon gc: truncated from {size} bytes; sha256 {digest}]\n".encode()
    )


def test_gc_bounds_a_promoted_runs_log_and_reports_its_checkpoint(
    tmp_path, monkeypatch
):
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    _host_limit(home, 1)
    repo = repository(tmp_path)

    _, log, checkpoint, _ = _promoted_run(repo, home, "run-promoted")
    original_size = log.stat().st_size
    original_digest = _sha256(log)
    checkpoint_bytes = checkpoint.read_bytes()

    result = CliRunner().invoke(
        cli.main, ["crew", "gc", "--repo", str(repo), "--apply"]
    )
    payload = json.loads(result.output)

    assert result.exit_code == 0, result.output
    rows = {row["path"]: row for row in payload["run_files"]}
    assert rows[str(log)]["bounded"] is True
    assert rows[str(log)]["kind"] == "run-file"
    assert rows[str(checkpoint)]["binary"] is True
    assert rows[str(checkpoint)]["action"] == "reported"
    assert sum(1 for row in payload["run_files"] if row["bounded"]) == 1
    assert sum(1 for row in payload["run_files"] if row["binary"]) == 1

    # The log keeps its head and its tail around the marker, so it is the limit
    # plus the marker and never more.
    bounded = log.stat().st_size
    marker_len = _marker_length(original_size, original_digest)
    assert bounded <= 1 * MiB + marker_len, (
        f"the bounded log is {bounded} bytes against a {1 * MiB + marker_len}-byte "
        "ceiling of limit plus marker"
    )
    text = log.read_text(encoding="utf-8")
    assert text.splitlines()[0] == HEAD_LINE, "the log lost its first line"
    assert text.splitlines()[-1] == TAIL_LINE, "the log lost its last line"
    assert original_digest in text, "the marker does not carry the original sha256"

    # The binary artefact is reported, never rewritten.
    assert checkpoint.read_bytes() == checkpoint_bytes
    assert checkpoint.stat().st_size == 3 * MiB


def test_gc_leaves_an_unpromoted_run_directory_untouched(tmp_path, monkeypatch):
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    _host_limit(home, 1)
    repo = repository(tmp_path)

    _promoted_run(repo, home, "run-promoted")
    unpromoted = home / "crew" / "runs" / "run-unpromoted"
    unpromoted.mkdir(parents=True)
    log = unpromoted / "worker.log"
    _write_log(log, 3 * MiB)
    before = log.read_bytes()

    result = CliRunner().invoke(
        cli.main, ["crew", "gc", "--repo", str(repo), "--apply"]
    )

    assert result.exit_code == 0, result.output
    assert log.read_bytes() == before, (
        "an unpromoted run's log was bounded; only a promoted run is held to the limit"
    )


def test_gc_reports_a_declared_gate_log_outside_the_run_directory(
    tmp_path, monkeypatch
):
    """The bound may rewrite only a file the run directory itself owns.

    A gate log a manifest declares but that lives outside the run directory is
    reported, not bounded: the run directory does not own it, so rewriting it in
    place would truncate a file another run or a peer session holds.
    """
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    _host_limit(home, 1)
    repo = repository(tmp_path)

    gate_log = tmp_path / "gate" / "NODE-gate.log"
    gate_log.parent.mkdir(parents=True)
    _write_log(gate_log, 3 * MiB)
    before = gate_log.read_bytes()

    _promoted_run(
        repo,
        home,
        "run-promoted",
        extra_manifest=f"test_logs: {gate_log}\n",
    )

    result = CliRunner().invoke(
        cli.main, ["crew", "gc", "--repo", str(repo), "--apply"]
    )
    payload = json.loads(result.output)

    assert result.exit_code == 0, result.output
    rows = {row["path"]: row for row in payload["run_files"]}
    row = rows[str(gate_log)]
    assert row["kind"] == "gate-log"
    assert row["action"] == "skipped"
    assert row["bounded"] is False
    assert row["reason"], "the skipped row must say why it was not bounded"
    assert gate_log.read_bytes() == before, "an outside gate log was rewritten"


def test_gc_skips_files_the_run_directory_does_not_own(tmp_path, monkeypatch):
    """A symlink, a hard-linked file, a binary blob and a script are reported.

    Each target is larger than the limit, and after ``--apply`` every one is
    byte-identical with every link intact, because the bound rewrites only a
    regular, singly linked file the run directory itself owns.
    """
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    _host_limit(home, 1)
    repo = repository(tmp_path)

    gate = tmp_path / "gate" / "NODE-gate.log"
    gate.parent.mkdir(parents=True)
    _write_log(gate, 3 * MiB)

    run_dir, log, _, _ = _promoted_run(
        repo,
        home,
        "run-promoted",
        extra_manifest=f"test_logs: {gate}\n",
    )

    # A symlink to a text file outside the run directory.
    outside = tmp_path / "outside.txt"
    _write_log(outside, 3 * MiB)
    link_out = run_dir / "outside-link.log"
    link_out.symlink_to(outside)

    # A symlink to a file inside the crew home's stores.
    store = home / "crew" / "run_store.db"
    _write_log(store, 3 * MiB)
    link_store = run_dir / "store-link.db"
    link_store.symlink_to(store)

    # A regular file with a hard link elsewhere.
    hard = run_dir / "hard.log"
    _write_log(hard, 3 * MiB)
    os.link(hard, tmp_path / "hard-peer.log")

    # An extensionless file carrying NUL bytes.
    blob = run_dir / "blob"
    blob.write_bytes(b"\x00" * (3 * MiB))

    # An executable script.
    script = run_dir / "run.sh"
    _write_log(script, 3 * MiB)
    script.chmod(0o755)

    targets = [outside, store, hard, blob, script, gate]
    before = {path: _sha256(path) for path in targets}
    links = {link_out: outside, link_store: store}

    result = CliRunner().invoke(
        cli.main, ["crew", "gc", "--repo", str(repo), "--apply"]
    )
    payload = json.loads(result.output)

    assert result.exit_code == 0, result.output
    rows = {row["path"]: row for row in payload["run_files"]}

    # Every target is byte-identical.
    for path, digest in before.items():
        assert _sha256(path) == digest, f"{path} was rewritten"

    # Every link is still a link to the same target.
    for link, target in links.items():
        assert link.is_symlink(), f"{link} is no longer a symlink"
        assert os.readlink(link) == str(target), f"{link} points elsewhere"

    # Each row says why it was not bounded.
    assert rows[str(link_out)]["action"] == "skipped"
    assert rows[str(link_out)]["reason"]
    assert rows[str(link_store)]["action"] == "skipped"
    assert rows[str(link_store)]["reason"]
    assert rows[str(hard)]["action"] == "skipped"
    assert rows[str(hard)]["reason"]
    assert rows[str(blob)]["action"] == "reported"
    assert rows[str(blob)]["binary"] is True
    assert rows[str(script)]["action"] == "reported"
    assert rows[str(script)]["binary"] is True
    assert rows[str(gate)]["action"] == "skipped"
    assert rows[str(gate)]["reason"]

    # Only the run directory's own over-limit text log is bounded.
    assert rows[str(log)]["bounded"] is True
    assert sum(1 for row in payload["run_files"] if row["bounded"]) == 1


def test_gc_one_file_that_cannot_be_bounded_leaves_the_rest_bounded(
    tmp_path, monkeypatch
):
    """An OSError on one file is reported on its row and the walk continues.

    A failed replace must not abort the whole pass, and it must leave the file
    it failed on whole rather than half written.
    """
    from reckon.crew import fleet_supervisor

    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    _host_limit(home, 1)
    repo = repository(tmp_path)

    run_dir, log, _, _ = _promoted_run(repo, home, "run-promoted")
    bad = run_dir / "bad.log"
    _write_log(bad, 3 * MiB)
    bad_before = _sha256(bad)

    real_replace = os.replace

    def fake_replace(src, dst):
        if Path(dst) == bad:
            raise OSError(26, "Text file busy")
        return real_replace(src, dst)

    monkeypatch.setattr(fleet_supervisor.os, "replace", fake_replace)

    result = CliRunner().invoke(
        cli.main, ["crew", "gc", "--repo", str(repo), "--apply"]
    )
    payload = json.loads(result.output)

    assert result.exit_code == 0, result.output
    rows = {row["path"]: row for row in payload["run_files"]}
    assert rows[str(bad)]["action"] == "skipped"
    assert rows[str(bad)]["error"], "the row must carry the error"
    assert _sha256(bad) == bad_before, "the failed file was left half written"
    assert rows[str(log)]["bounded"] is True, "the walk stopped at the failure"


def test_gc_never_descends_a_nested_source_tree(tmp_path, monkeypatch):
    """A run's own output is bounded; a source tree beside it is not walked.

    The run-file walk prunes a nested tree by the same policy the extraction
    report names it -- a child of ``checkouts``, a ``base-*`` tree, a tree
    carrying a git directory -- so an over-limit file inside one is left alone
    rather than bounded as if it were the run's captured stream.
    """
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    _host_limit(home, 1)
    repo = repository(tmp_path)

    run_dir, _, _, _ = _promoted_run(repo, home, "run-promoted")
    nested = run_dir / "checkouts" / "candidate"
    nested.mkdir(parents=True)
    (nested / "huge.log").write_bytes(b"nested source tree\n" * (3 * MiB // 19))
    base_tree = run_dir / "base-source"
    base_tree.mkdir()
    (base_tree / "huge.log").write_bytes(b"x" * (3 * MiB))
    sized = {
        nested / "huge.log": (nested / "huge.log").stat().st_size,
        base_tree / "huge.log": (base_tree / "huge.log").stat().st_size,
    }

    result = CliRunner().invoke(
        cli.main, ["crew", "gc", "--repo", str(repo), "--apply"]
    )
    payload = json.loads(result.output)
    reported = {row["path"] for row in payload["run_files"]}

    assert result.exit_code == 0, result.output
    for path, size in sized.items():
        assert str(path) not in reported, "the walk descended a nested source tree"
        assert path.stat().st_size == size, "a nested source tree's file was bounded"


def test_the_run_walk_and_the_extraction_report_share_one_policy(tmp_path):
    """Both name the same trees, so the pruner and the report cannot drift."""
    from reckon.crew.fleet_supervisor import run_directory_source_trees
    from reckon.crew.routing import _run_directory_extractions

    home = tmp_path / "config"
    runs_root = home / "crew" / "runs"
    run_dir = runs_root / "run-one"
    extraction = run_dir / "base-source"
    extraction.mkdir(parents=True)
    checkout = run_dir / "checkouts" / "candidate"
    checkout.mkdir(parents=True)
    (run_dir / ".venv").mkdir()
    (run_dir / "node_modules").mkdir()
    # A virtualenv inside checkouts is not a source tree either; a bespoke owner
    # that took every checkouts child would list it and drift from the walk.
    (run_dir / "checkouts" / ".venv").mkdir()

    assert set(run_directory_source_trees(run_dir)) == {extraction, checkout}
    assert set(_run_directory_extractions(runs_root)) == {extraction, checkout}


def test_the_run_file_digest_reuses_the_shared_path_digest():
    """One sha256-over-a-path helper serves both modules, read in chunks."""
    from reckon.crew import fleet_supervisor
    from reckon.project_state import _sha256_path

    assert not hasattr(fleet_supervisor, "_sha256_of"), (
        "a private local sha256 helper duplicates the shared one"
    )
    assert fleet_supervisor._sha256_path is _sha256_path, (
        "the run-file digest must reuse the shared path digest, not duplicate it"
    )
