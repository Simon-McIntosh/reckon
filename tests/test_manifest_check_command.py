"""The manifest check reaches its writer while the run still holds a turn.

The same audit the promotion contract applies hours later, entered through the
command surface a worker can run itself. Each test drives the CLI runner rather
than the audit entry point, because what is under test is the wiring that gives
the audit its first production caller.

Running the command is also held to the repository rule that a write-shaped
test accounts for the state it did not isolate. A case points RECKON_HOME at a
temporary home so its own pointer write stays inside the case, and the crew
home the process resolved *before* that substitution is watched byte for byte:
if home resolution ever stopped honouring the substitution, this fixture's
pointer would land in that other directory, and the witness is what names the
escape rather than leaving it to be discovered later. The production crew home
is not the directory under watch, and cannot be — measured on a live wave it
holds 598,286 entries with 138 of them rewritten in six seconds, so comparing
it whole would be slow and would measure the fleet instead. What the witness
catches is instead made to happen on a substitute root, so this file proves its
own assertion fires without writing anywhere near that directory.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon.cli import main as cli_main
from reckon.crew.runs import _write_json, crew_home, pointer_path

RUN_ID = "r-20260921T000000000000-manifest-check"
ABSOLUTE_SCOPE_RUN_ID = "r-20260921T000000000000-manifest-absolute-scope"

IN_FENCE_MANIFEST = """\
node: manifest-check
status: complete
commits: 1a2b3c4
changed_paths: reckon/cli.py
tests: uv run pytest tests/test_manifest_check_command.py -q -> 3 passed
test_logs: /tmp/manifest-check.log
artifacts: none
evidence_inputs: none
follow_ons: none
blockers: none
"""

ABSOLUTE_SCOPE_MANIFEST = """\
node: manifest-check
status: complete
commits: 1a2b3c4
changed_paths: src/module.py, lib/helper.py
tests: uv run pytest tests/test_manifest_check_command.py -q -> 3 passed
test_logs: /tmp/manifest-check.log
artifacts: none
evidence_inputs: none
follow_ons: none
blockers: none
"""


def _tree_bytes(root: Path) -> dict[str, bytes] | None:
    """Every entry under ``root`` by relative path, with the file's own bytes.

    Directories carry a trailing slash so an empty directory appearing inside
    the tree is a difference rather than an invisible one. ``None`` stands for
    a root that does not exist, which is a state the comparison may see change.
    """
    if not root.exists():
        return None
    entries: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            entries[relative + "/"] = b""
        elif path.is_file():
            entries[relative] = path.read_bytes()
    return entries


def _difference(before: dict[str, bytes] | None, after: dict[str, bytes] | None) -> str:
    """Name the paths that appeared, vanished or changed between two trees."""
    if before is None or after is None:
        return (
            f"the root existed={before is not None} before, {after is not None} after"
        )
    lines = []
    for relative in sorted(set(before) | set(after)):
        was, now = before.get(relative), after.get(relative)
        if was == now:
            continue
        if was is None:
            lines.append(f"  created: {relative}")
        elif now is None:
            lines.append(f"  removed: {relative}")
        else:
            lines.append(f"  changed: {relative} ({len(was)} -> {len(now)} bytes)")
    return "\n".join(lines)


class CrewHomeWatch:
    """A byte-for-byte witness of one crew home across a case's run."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.before = _tree_bytes(root)

    def assert_untouched(self) -> None:
        """Fail naming every path that moved under the watched root."""
        after = _tree_bytes(self.root)
        assert after == self.before, (
            f"the crew home {self.root} was written to by a case that did not "
            f"substitute it:\n{_difference(self.before, after)}"
        )


@pytest.fixture()
def crew_home_watch(isolated_reckon_home: Path) -> CrewHomeWatch:
    """Watch the crew home this process resolved before a case substituted one.

    The suite's own fixture is requested by name so its temporary home is
    already in place: the root under watch is where a case's pointer write
    lands if home resolution stops honouring the substitution the case makes,
    which is the escape the assertion exists to catch.
    """
    root = crew_home()
    assert not root.is_relative_to(Path.home()), (
        f"the witness is watching the production directory {root}: the suite's "
        "temporary home is not in force, so a pass would be measuring the fleet"
    )
    return CrewHomeWatch(root)


@pytest.fixture()
def manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crew_home_watch: CrewHomeWatch
) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    path = tmp_path / "manifest.md"
    path.write_text(IN_FENCE_MANIFEST, encoding="utf-8")
    _write_json(
        pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": "sample",
            "repo": str(tmp_path / "repo"),
            "worktree": str(tmp_path / "repo"),
            "base_sha": "0" * 40,
            "launch": "in-harness",
            "role": "implement",
            "node": {
                "id": "manifest-check",
                "plan": "fixture",
                "section": "s9",
                "write_paths": ["reckon/cli.py"],
            },
            "manifest_path": str(path),
        },
    )
    return path


def _check(run_id: str = RUN_ID):
    return CliRunner().invoke(cli_main, ["crew", "check-manifest", "--run", run_id])


@pytest.fixture()
def absolute_scope_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crew_home_watch: CrewHomeWatch
) -> dict[str, Path]:
    """A run whose fence is declared absolutely, in trees the caller's cwd is not.

    The worktree and the repository are two distinct directories so a case can
    tell which one a declaration was resolved against, and the declared roots
    lie under each: ``src`` under the worktree, ``lib`` under the repository.
    Declarations are the absolute paths on disk, which is how dispatch grants a
    directory, while the manifest records the repository-relative paths a diff
    produces. Only a scope resolver that maps an absolute declaration through
    the tree that run worked in brings the two into agreement; one that falls
    back to the process working directory leaves both roots unmapped and calls
    every changed path stray.
    """
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    worktree = tmp_path / "run-worktree"
    repository = tmp_path / "main-checkout"
    (worktree / "src").mkdir(parents=True)
    (repository / "lib").mkdir(parents=True)
    manifest_path = tmp_path / "manifest.md"
    manifest_path.write_text(ABSOLUTE_SCOPE_MANIFEST, encoding="utf-8")
    _write_json(
        pointer_path(ABSOLUTE_SCOPE_RUN_ID),
        {
            "run_id": ABSOLUTE_SCOPE_RUN_ID,
            "project": "sample",
            "repo": str(repository),
            "worktree": str(worktree),
            "base_sha": "0" * 40,
            "launch": "in-harness",
            "role": "implement",
            "node": {
                "id": "manifest-check",
                "plan": "fixture",
                "section": "s9",
                "write_paths": [str(worktree / "src"), str(repository / "lib")],
            },
            "manifest_path": str(manifest_path),
        },
    )
    return {
        "worktree": worktree,
        "repository": repository,
        "manifest": manifest_path,
    }


@pytest.fixture()
def outside_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Stand the caller somewhere that is neither the worktree nor the repository."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    return elsewhere


def test_a_manifest_inside_its_fence_exits_zero_with_no_finding(
    manifest: Path, crew_home_watch: CrewHomeWatch
) -> None:
    result = _check()

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["findings"] == []
    crew_home_watch.assert_untouched()


def test_the_command_names_the_run_it_read(
    manifest: Path, crew_home_watch: CrewHomeWatch
) -> None:
    result = _check()

    assert RUN_ID in result.output
    assert json.loads(result.output)["run_id"] == RUN_ID
    crew_home_watch.assert_untouched()


def test_a_path_outside_the_node_fence_is_reported_by_name(
    manifest: Path, crew_home_watch: CrewHomeWatch
) -> None:
    manifest.write_text(
        IN_FENCE_MANIFEST.replace(
            "changed_paths: reckon/cli.py",
            "changed_paths: reckon/cli.py, reckon/other_module.py",
        ),
        encoding="utf-8",
    )

    result = _check()

    assert result.exit_code != 0, result.output
    findings = json.loads(result.output)["findings"]
    assert "reckon/other_module.py" in " ".join(findings)
    crew_home_watch.assert_untouched()


def test_an_unknown_run_is_refused_rather_than_reported_clean(
    manifest: Path, crew_home_watch: CrewHomeWatch
) -> None:
    result = _check(run_id="r-20260921T000000000000-absent")

    assert result.exit_code != 0
    assert result.output.strip()
    crew_home_watch.assert_untouched()


def test_an_absolute_declaration_resolves_from_outside_the_run_worktree(
    absolute_scope_run: dict[str, Path],
    outside_cwd: Path,
    crew_home_watch: CrewHomeWatch,
) -> None:
    """A declaration granted absolutely maps through the tree the run worked in.

    The caller stands in neither the worktree nor the repository, so a scope
    resolved against the working directory would leave the declared roots
    unmapped and report both changed paths stray. A clean reading is only
    possible when the worktree and repository travel with the pointer.
    """
    result = _check(ABSOLUTE_SCOPE_RUN_ID)

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["findings"] == []
    crew_home_watch.assert_untouched()


def test_a_path_under_no_declaration_is_reported_from_outside(
    absolute_scope_run: dict[str, Path],
    outside_cwd: Path,
    crew_home_watch: CrewHomeWatch,
) -> None:
    """From the same outside caller, an undeclared path alone is refused.

    The finding names exactly the undeclared path and none of the declared
    ones: the scope resolves from the pointer yet the guard it feeds still
    fires, so the earlier case is a mapping that works rather than a check that
    stopped judging.
    """
    absolute_scope_run["manifest"].write_text(
        ABSOLUTE_SCOPE_MANIFEST.replace(
            "changed_paths: src/module.py, lib/helper.py",
            "changed_paths: src/module.py, docs/plan.html",
        ),
        encoding="utf-8",
    )

    result = _check(ABSOLUTE_SCOPE_RUN_ID)

    assert result.exit_code != 0, result.output
    findings = json.loads(result.output)["findings"]
    assert findings == ["changed paths outside the write scope: docs/plan.html"]
    crew_home_watch.assert_untouched()


def test_the_scope_comes_from_the_run_pointer_not_the_working_directory(
    absolute_scope_run: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crew_home_watch: CrewHomeWatch,
) -> None:
    """Two callers in different directories judge one manifest identically.

    The reading is the same from each because the tree it is resolved against
    is named by the pointer rather than read from the process. A resolver that
    read the working directory would judge the same manifest two ways.
    """
    findings_by_cwd: dict[str, list[str]] = {}
    for relative in ("first-caller", "second-caller/deeper"):
        cwd = tmp_path / relative
        cwd.mkdir(parents=True)
        monkeypatch.chdir(cwd)
        result = _check(ABSOLUTE_SCOPE_RUN_ID)
        assert result.exit_code == 0, result.output
        findings_by_cwd[relative] = json.loads(result.output)["findings"]

    assert list(findings_by_cwd.values()) == [[], []]
    crew_home_watch.assert_untouched()


def test_the_isolation_assertion_fires_on_a_substituted_root(tmp_path: Path) -> None:
    """The witness is shown to fire, on a root the production home never sees.

    A guard that never fires is indistinguishable from no guard, so the failure
    the witness exists to catch is made to happen here and the assertion is
    shown to name it. The write goes into a substitute root, and the production
    crew home is never a party to this case at all.
    """
    root = tmp_path / "substituted-crew-home"
    (root / "live").mkdir(parents=True)
    watch = CrewHomeWatch(root)

    watch.assert_untouched()  # nothing written yet: the witness stays quiet

    _write_json(
        root / "live" / f"{RUN_ID}.json",
        {"run_id": RUN_ID, "node": {"id": "manifest-check"}},
    )

    with pytest.raises(AssertionError, match=f"live/{RUN_ID}.json"):
        watch.assert_untouched()


def test_the_cli_is_the_only_production_caller() -> None:
    root = Path(cli_module.__file__).resolve().parent
    referencing = {
        path.relative_to(root).as_posix()
        for path in sorted(root.rglob("*.py"))
        if "audit_manifest" in path.read_text(encoding="utf-8")
    }

    assert referencing == {"cli.py", "crew.py", "crew/reports.py"}
