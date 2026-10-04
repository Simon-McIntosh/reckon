"""A fragment's element ids are checked by the worker that writes them.

The composed record concatenates a plan's landing fragments into one document,
so two fragments that reuse an element id leave every later anchor unreachable
for a reader following a link. The finding existed but nothing on the crew path
ran it, and the worker brief said nothing about ids, so a collision surfaced
only at closure — after the sessions that could have renamed the fragment had
ended. Each case here drives ``check-manifest`` because the wiring under test is
the audit's first fragment-writing caller.

The fixture is a temporary repository holding a plan, its record and one
committed fragment, with a run whose worktree adds a second fragment carrying
the same id. Measured 2026-10-04: three fragments of one plan each carried the
same three ids, so the composed record held nine duplicate element ids.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon.cli import main as cli_main
from reckon.crew.runs import _write_json, pointer_path

RUN_ID = "r-20261004T000000000000-fragment-ids"
PLAN = "fragment-plan"
PROJECT = "sample"
COLLIDING_ID = "landing-what-landed"
CHANGED_FRAGMENT = f"docs/evidence/fragments/{PLAN}/second-node.html"
OTHER_CHANGED_PATH = "notes/fragment-plan.txt"
FRESHLY_PLANNED_FRAGMENT = "docs/evidence/fragments/fresh-plan/fresh-node.html"

PLAN_HTML = """\
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="docs-project" content="sample">
  <meta name="plan-slug" content="fragment-plan">
  <meta name="plan-status" content="active">
  <title>Fragment plan | sample</title>
</head>
<body>
  <main class="plan-doc">
    <h1>Fragment plan</h1>
  </main>
</body>
</html>
"""

RECORD_HTML = """\
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="reckon-type" content="evidence">
  <meta name="docs-project" content="sample">
  <meta name="plan-evidence-for" content="fragment-plan">
  <title>Fragment plan — landed record | sample</title>
</head>
<body>
  <main class="plan-doc">
    <h1>Fragment plan — landed record</h1>
    <section id="record-summary">
      <h2>Summary</h2>
      <p>One committed fragment so far.</p>
    </section>
  </main>
</body>
</html>
"""


def _fragment(node: str, ident: str) -> str:
    """A landing fragment as workers write them, carrying the anchor in question."""
    return f"""\
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="reckon-type" content="evidence">
  <meta name="docs-project" content="sample">
  <meta name="plan-evidence-for" content="fragment-plan">
  <title>Landing: {node} | sample</title>
</head>
<body>
  <main class="plan-doc">
    <section id="{ident}">
      <h2>What landed</h2>
      <p>{node}</p>
    </section>
  </main>
</body>
</html>
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _manifest(head: str, *, changed: str) -> str:
    return f"""\
node: fragment-check
status: complete
commits: {head}
changed_paths: {changed}
tests: pytest tests/test_fragment_ids_are_checked.py -q -> 4 passed
test_logs: /tmp/fragment-ids.log
artifacts: none
evidence_inputs: none
follow_ons: none
blockers: none
"""


class RunFixture:
    """The temporary repository a case drives check-manifest against."""

    def __init__(self, repo: Path, manifest: Path, head: str) -> None:
        self.repo = repo
        self.manifest = manifest
        self.head = head

    def changed_fragment(self) -> Path:
        return self.repo / CHANGED_FRAGMENT

    def declare(self, changed: str) -> None:
        self.manifest.write_text(
            _manifest(self.head, changed=changed), encoding="utf-8"
        )


@pytest.fixture()
def run(tmp_path: Path) -> RunFixture:
    """A plan, its record and one committed fragment; the run adds a second.

    The first fragment is committed and the second exists only as the run's
    working-tree change, which is the state a collision is made in: the worker
    writing a new fragment has no memory of the ones already in the directory.
    """
    repo = tmp_path / "repo"
    fragment_dir = repo / "docs" / "evidence" / "fragments" / PLAN
    fragment_dir.mkdir(parents=True)
    (repo / "docs" / "plans").mkdir(parents=True)
    (repo / "docs" / "plans" / f"{PLAN}.html").write_text(PLAN_HTML, encoding="utf-8")
    record = repo / "docs" / "evidence" / "archive" / f"{PLAN}-landed.html"
    record.parent.mkdir(parents=True)
    record.write_text(RECORD_HTML, encoding="utf-8")
    (fragment_dir / "first-node.html").write_text(
        _fragment("first-node", COLLIDING_ID), encoding="utf-8"
    )
    fresh_dir = repo / "docs" / "evidence" / "fragments" / "fresh-plan"
    fresh_dir.mkdir()
    (fresh_dir / "fresh-node.html").write_text(
        _fragment("fresh-node", "fresh-node-landing"), encoding="utf-8"
    )
    (repo / "notes").mkdir()
    (repo / "notes" / "fragment-plan.txt").write_text("run notes\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "--", "docs", "notes")
    _git(
        repo,
        "-c",
        "user.name=fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-q",
        "-m",
        "seed the plan, its record and one fragment",
    )
    head = _git(repo, "rev-parse", "HEAD")

    (fragment_dir / "second-node.html").write_text(
        _fragment("second-node", COLLIDING_ID), encoding="utf-8"
    )
    manifest = tmp_path / "manifest.md"
    manifest.write_text(_manifest(head, changed=CHANGED_FRAGMENT), encoding="utf-8")
    _write_json(
        pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": PROJECT,
            "repo": str(repo),
            "worktree": str(repo),
            "base_sha": head,
            "launch": "in-harness",
            "role": "implement",
            "node": {
                "id": "fragment-check",
                "plan": PLAN,
                "section": "s8",
                "role": "implement",
                "write_paths": [
                    CHANGED_FRAGMENT,
                    OTHER_CHANGED_PATH,
                    FRESHLY_PLANNED_FRAGMENT,
                ],
            },
            "manifest_path": str(manifest),
        },
    )
    return RunFixture(repo, manifest, head)


def _check():
    return CliRunner().invoke(cli_main, ["crew", "check-manifest", "--run", RUN_ID])


def test_a_new_fragment_repeating_a_committed_id_is_refused_by_name(
    run: RunFixture,
) -> None:
    result = _check()

    assert result.exit_code != 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is False
    duplicates = [f for f in payload["findings"] if "[duplicate-element-id]" in f]
    assert len(duplicates) == 1
    assert COLLIDING_ID in duplicates[0]
    assert "fragment first-node.html" in duplicates[0]
    assert "fragment second-node.html" in duplicates[0]


def test_the_run_passes_once_the_new_fragments_id_is_prefixed(run: RunFixture) -> None:
    run.changed_fragment().write_text(
        _fragment("second-node", f"second-node-{COLLIDING_ID}"), encoding="utf-8"
    )

    result = _check()

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["findings"] == []


def test_a_run_that_changes_no_fragment_is_not_audited(run: RunFixture) -> None:
    """The colliding fragment stays in the tree; the declaration does not name it."""
    run.declare(OTHER_CHANGED_PATH)

    result = _check()

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["findings"] == []


def test_a_plan_with_no_record_yet_has_nothing_to_collide_with(
    run: RunFixture,
) -> None:
    run.declare(FRESHLY_PLANNED_FRAGMENT)

    result = _check()

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["findings"] == []
