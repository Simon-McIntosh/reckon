"""The figure twin check reads the tree at the run's reviewed head.

The figure rule keeps one SVG per figure and bans a PNG copy beside it: the
reckon page renders the SVG, and a second copy drifts out of step with it. What
the rule can refuse a run for is the tree that run delivers. A worker told to
render its SVG to look at it leaves the copy behind, notices it, and removes it
in a later commit; the head such a run hands over holds one SVG and no twin,
and it breaks no rule.

Judging each cited commit's additions instead charges that run for work its
head no longer carries, and the only remedy left is rewriting history — a run
that can never land. The case below cites only the commit that added the copy,
while the removal sits at the run's head uncited, which is exactly the shape
that passes on the head tree and fails on the additions.

Each case builds its own repository under a temporary directory and reads
nothing outside it. A PNG rendered but never committed is not a finding, and a
PNG already present beside its SVG at the run's base is not the run's to answer
for.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import _plan_html, crew
from reckon.cli import main as cli_main
from reckon.crew.runs import _write_json, pointer_path
from tests.helpers.git import git as _git

PROJECT = "proj"
PLAN = "plan-a"
NODE_ID = "figure-node"
RUN_ID = "r-20261020T000000000000-figure-twin-head"
TEST_FILE = "tests/test_the_figure_twin_check_reads_the_head_tree.py"
FIGURE_DIR = f"docs/figures/{PLAN}/{NODE_ID}"


def _write_plan(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    path.write_text(
        _plan_html.write_state(
            bare,
            {
                "type": "plan",
                "slug": PLAN,
                "title": PLAN,
                "status": "active",
                "version": 0,
                "comments": {},
            },
        ),
        encoding="utf-8",
    )


class Fixture:
    """A repository, its plan, and one dispatched run's pointer and manifest."""

    def __init__(self, repo: Path, config_home: Path) -> None:
        self.repo = repo
        self.config_home = config_home
        self.base = ""
        self.head = ""

    def figure(self, name: str) -> Path:
        directory = self.repo / FIGURE_DIR
        directory.mkdir(parents=True, exist_ok=True)
        return directory / name

    def commit(self, message: str) -> None:
        _git(self.repo, "add", "--", FIGURE_DIR)
        _git(self.repo, "commit", "-q", "-m", message)
        self.head = _git(self.repo, "rev-parse", "HEAD")

    def stage(self, changes: list[str], commits: list[str] | None = None) -> None:
        """Write the run's manifest and pointer over the committed head.

        ``commits`` lets a case cite a commit other than the tree's head, which
        is how a run whose removal commit is its head but not its citation is
        reproduced. A case that passes none cites the head.
        """
        manifest = self.config_home / "manifest.md"
        manifest.write_text(
            f"node: {NODE_ID}\n"
            "status: complete\n"
            f"commits: {', '.join(commits or [self.head])}\n"
            "changed_paths: " + ", ".join(changes) + "\n"
            f"tests: pytest {TEST_FILE}\n"
            f"test_logs: {manifest}.log\n"
            "artifacts: none\n"
            "evidence_inputs: none\n"
            "follow_ons: none\n"
            "messages_received: none\n"
            "blockers: none\n"
            f"orientation_base_sha: {self.base}\n",
            encoding="utf-8",
        )
        Path(f"{manifest}.log").write_text(
            f"# command: pytest {TEST_FILE}\n"
            f"# revision: {self.head}\n"
            "1 passed\n"
            "EXIT=0\n",
            encoding="utf-8",
        )
        _write_json(
            pointer_path(RUN_ID),
            {
                "run_id": RUN_ID,
                "project": PROJECT,
                "repo": str(self.repo),
                "worktree": str(self.repo),
                "base_sha": self.base,
                "launch": "in-harness",
                "role": "implement",
                "manifest_path": str(manifest),
                "node": {
                    "id": NODE_ID,
                    "plan": PLAN,
                    "section": "s1",
                    "write_paths": [FIGURE_DIR],
                },
            },
        )


@pytest.fixture()
def fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fixture:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    repo = tmp_path / "repo"
    (repo / "docs" / "state" / PROJECT).mkdir(parents=True)
    _write_plan(repo / "docs" / "plans" / f"{PLAN}.html")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(repo, *arguments)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "seed.txt", "docs")
    _git(repo, "commit", "-q", "-m", "test: seed repository")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    built = Fixture(repo, config_home)
    built.base = _git(repo, "rev-parse", "HEAD")
    return built


def _check():
    return CliRunner().invoke(
        cli_main, ["crew", "check", "--manifest", "--run", RUN_ID]
    )


def _twin(findings: list[str]) -> list[str]:
    return [finding for finding in findings if "PNG twin" in finding]


def test_a_png_beside_its_svg_is_reported_and_refused(fixture: Fixture) -> None:
    fixture.figure("a.svg").write_text("<svg/>\n", encoding="utf-8")
    fixture.figure("a.png").write_text("png\n", encoding="utf-8")
    fixture.commit("add a figure and its rasterised copy")
    fixture.stage([f"{FIGURE_DIR}/a.svg", f"{FIGURE_DIR}/a.png"])

    checked = _check()
    assert checked.exit_code != 0, checked.output
    payload = json.loads(checked.output)
    twins = _twin(payload["findings"])
    assert len(twins) == 1, payload
    assert f"{FIGURE_DIR}/a.png" in twins[0]
    assert f"{FIGURE_DIR}/a.svg" in twins[0]

    with pytest.raises(crew.CrewError) as refusal:
        crew.complete(RUN_ID, gate="passed", root=fixture.repo)
    assert f"{FIGURE_DIR}/a.png" in str(refusal.value)
    assert f"{FIGURE_DIR}/a.svg" in str(refusal.value)


def test_a_png_with_no_svg_beside_it_passes_both(fixture: Fixture) -> None:
    fixture.figure("raster.png").write_text("png\n", encoding="utf-8")
    fixture.commit("add a raster-only figure")
    fixture.stage([f"{FIGURE_DIR}/raster.png"])

    checked = _check()
    assert checked.exit_code == 0, checked.output
    assert _twin(json.loads(checked.output)["findings"]) == []

    promoted = crew.complete(RUN_ID, gate="passed", root=fixture.repo)
    assert promoted["record"]["run_id"] == RUN_ID


def test_an_svg_alone_passes_both(fixture: Fixture) -> None:
    fixture.figure("only.svg").write_text("<svg/>\n", encoding="utf-8")
    fixture.commit("add an SVG figure")
    fixture.stage([f"{FIGURE_DIR}/only.svg"])

    checked = _check()
    assert checked.exit_code == 0, checked.output
    assert _twin(json.loads(checked.output)["findings"]) == []

    promoted = crew.complete(RUN_ID, gate="passed", root=fixture.repo)
    assert promoted["record"]["run_id"] == RUN_ID


def test_a_png_present_at_base_is_not_the_run_s_twin(fixture: Fixture) -> None:
    fixture.figure("pair.svg").write_text("<svg/>\n", encoding="utf-8")
    fixture.figure("pair.png").write_text("old\n", encoding="utf-8")
    fixture.commit("keep a figure in both formats")
    fixture.base = fixture.head
    fixture.figure("pair.png").write_text("regenerated\n", encoding="utf-8")
    fixture.commit("regenerate the PNG copy")
    fixture.stage([f"{FIGURE_DIR}/pair.png"])

    checked = _check()
    assert checked.exit_code == 0, checked.output
    assert _twin(json.loads(checked.output)["findings"]) == []

    promoted = crew.complete(RUN_ID, gate="passed", root=fixture.repo)
    assert promoted["record"]["run_id"] == RUN_ID


def test_an_untracked_png_is_not_a_finding(fixture: Fixture) -> None:
    fixture.figure("b.svg").write_text("<svg/>\n", encoding="utf-8")
    fixture.commit("add an SVG figure")
    fixture.figure("b.png").write_text("rendered\n", encoding="utf-8")
    fixture.stage([f"{FIGURE_DIR}/b.svg"])

    checked = _check()
    assert checked.exit_code == 0, checked.output
    assert _twin(json.loads(checked.output)["findings"]) == []

    promoted = crew.complete(RUN_ID, gate="passed", root=fixture.repo)
    assert promoted["record"]["run_id"] == RUN_ID


def test_a_twin_the_run_removed_in_a_later_commit_is_not_charged(
    fixture: Fixture,
) -> None:
    """The run added a copy, then removed it: its head holds one SVG."""
    fixture.figure("c.svg").write_text("<svg/>\n", encoding="utf-8")
    fixture.figure("c.png").write_text("png\n", encoding="utf-8")
    fixture.commit("add a figure and its rasterised copy")
    added = fixture.head
    fixture.figure("c.png").unlink()
    fixture.commit("remove the rasterised copy")
    fixture.stage([f"{FIGURE_DIR}/c.svg"])
    manifest = fixture.config_home / "manifest.md"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            f"commits: {fixture.head}\n", f"commits: {added}, {fixture.head}\n"
        ),
        encoding="utf-8",
    )

    checked = _check()
    assert checked.exit_code == 0, checked.output
    assert _twin(json.loads(checked.output)["findings"]) == []

    promoted = crew.complete(RUN_ID, gate="passed", root=fixture.repo)
    assert promoted["record"]["run_id"] == RUN_ID


def test_a_twin_removed_before_the_head_passes_with_only_the_add_cited(
    fixture: Fixture,
) -> None:
    """The head holds one SVG; the removal commit is not among the manifest's
    citations, so a check that reads each commit's additions refuses it."""
    fixture.figure("f.svg").write_text("<svg/>\n", encoding="utf-8")
    fixture.figure("f.png").write_text("png\n", encoding="utf-8")
    fixture.commit("add a figure and its rasterised copy")
    added = fixture.head
    fixture.figure("f.png").unlink()
    fixture.commit("remove the rasterised copy")
    fixture.stage([f"{FIGURE_DIR}/f.svg"], commits=[added])

    checked = _check()
    assert checked.exit_code == 0, checked.output
    assert _twin(json.loads(checked.output)["findings"]) == []

    promoted = crew.complete(RUN_ID, gate="passed", root=fixture.repo)
    assert promoted["record"]["run_id"] == RUN_ID


def test_a_twin_still_at_the_head_is_charged_whatever_else_is_cited(
    fixture: Fixture,
) -> None:
    fixture.figure("d.svg").write_text("<svg/>\n", encoding="utf-8")
    fixture.figure("d.png").write_text("png\n", encoding="utf-8")
    fixture.commit("add a figure and its rasterised copy")
    added = fixture.head
    fixture.figure("e.svg").write_text("<svg/>\n", encoding="utf-8")
    fixture.commit("add a second figure")
    fixture.stage([f"{FIGURE_DIR}/d.svg", f"{FIGURE_DIR}/d.png", f"{FIGURE_DIR}/e.svg"])
    manifest = fixture.config_home / "manifest.md"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            f"commits: {fixture.head}\n", f"commits: {added}, {fixture.head}\n"
        ),
        encoding="utf-8",
    )

    with pytest.raises(crew.CrewError) as refusal:
        crew.complete(RUN_ID, gate="passed", root=fixture.repo)
    assert f"{FIGURE_DIR}/d.png" in str(refusal.value)
