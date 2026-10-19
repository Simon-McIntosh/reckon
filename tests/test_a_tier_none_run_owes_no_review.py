"""A run the tier rule owes no review dispatches no review, so it owes no duty.

The review sweep reads a run's tier through promotion's resolver and skips the
review of a run that changes no runtime source, recording the skip on the run's
pointer. The coordinator's obligations view must agree with that decision: a
duty left ``review-missing`` names a dispatch the sweep has already decided
against, and the coordinator can satisfy it only by running a review the tier
rule exempted, or by acknowledging the duty by hand.

The tier is read through the same resolver the sweep uses, so the test drives
the whole path — a temporary crew home, a live pointer classified as a scoring
run, and ``obligations`` — with no patched tier. The two runs are read together,
because a guard that only ever quietens is indistinguishable from one that has
been deleted: the runtime-source run must still read ``review-missing`` in the
same pass that reads the documentation run as owed no review.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import _plan_html, crew
from reckon.crew import obligations, recovery, runs

PROJECT = "sample"
PLAN = "fixture"
SESSION = "session-orchestrating"
DOCS_RUN = "r-docs-only"
SOURCE_RUN = "r-source"
BASE_RUN = "r-base-only"

# More changed source lines than the shipped light ceiling, so the runtime-source
# run resolves to ``full`` rather than ``light`` and still owes a review.
_LARGE_SOURCE = "".join(f"VALUE_{index} = {index}\n" for index in range(80))


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments], cwd=root, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _write_plan(root: Path) -> None:
    plan = root / "docs" / "plans" / f"{PLAN}.html"
    plan.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title>"
        '</head><body><main class="plan-doc">'
        '<h2 id="s2">Section two</h2>'
        "</main></body></html>\n"
    )
    state: dict = {
        "type": "plan",
        "slug": PLAN,
        "title": "Fixture",
        "status": "active",
        "version": 0,
        "comments": {},
    }
    plan.write_text(_plan_html.write_state(bare, state), encoding="utf-8")


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary crew home holding one repo with a documentation run and a
    runtime-source run, both live and both classified as scoring."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    _write_plan(root)
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


def _commit_change(root: Path, relative_path: str, content: str) -> str:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _git(root, "add", relative_path)
    _git(root, "commit", "-q", "-m", "feature: change a path")
    return _git(root, "rev-parse", "HEAD")


def _write_pointer(
    root: Path,
    run_id: str,
    *,
    commit: str,
    changed_paths: tuple[str, ...],
    base: str = "",
) -> None:
    manifest = Path(runs.run_dir(run_id)) / "manifest.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: node-a\n"
        "status: complete\n"
        f"commits: {commit}\n"
        "changed_paths: " + ", ".join(changed_paths) + "\n"
        "tests: focused check passed\n",
        encoding="utf-8",
    )
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "base_sha": base,
            "repo": str(root),
            "worktree": str(root),
            "launch": "cli",
            "role": "implement",
            "backend": "alpha",
            "argv": ["codex"],
            "phase": "starting",
            "process_alive": False,
            "session": SESSION,
            "manifest_path": str(manifest),
            "node": {
                "id": "node-a",
                "plan": PLAN,
                "section": "s2",
                "spec_level": "exact",
                "write_paths": list(changed_paths),
            },
        },
    )


def _items(root: Path) -> dict[str, dict]:
    result = obligations.obligations(PROJECT, SESSION)
    return {item["run_id"]: item for item in result["obligations"]}


def test_a_tier_none_run_reads_a_promotable_kind_and_a_source_run_stays_missing(
    project: Path,
) -> None:
    """The tier-none run owes no review; the source run still owes its own."""
    root = project
    docs_commit = _commit_change(
        root, "docs/evidence/fixture-note.html", "<p>a note</p>\n"
    )
    source_commit = _commit_change(root, "reckon/thing.py", _LARGE_SOURCE)
    _write_pointer(
        root,
        DOCS_RUN,
        commit=docs_commit,
        changed_paths=("docs/evidence/fixture-note.html",),
    )
    _write_pointer(
        root,
        SOURCE_RUN,
        commit=source_commit,
        changed_paths=("reckon/thing.py",),
    )

    items = _items(root)

    # Both runs are classified as scoring, whose unqualified reading is
    # review-missing: the docs run's promotable reading below is therefore the
    # tier read and not the classifier's own promotable kind, which would make
    # the assertion pass whatever the tier said.
    for run_id in (DOCS_RUN, SOURCE_RUN):
        assert (
            recovery.classify_pointer(runs.read_pointer(run_id))["classification"]
            == "scoring"
        ), run_id

    # The tier-none run stays in the list and reads the kind a reviewed run
    # with a passing review would get, not review-missing and not absent.
    assert DOCS_RUN in items, items
    docs_item = items[DOCS_RUN]
    assert docs_item["kind"] == "review-ready", docs_item
    assert docs_item["review_tier"] == "none", docs_item

    # The command is the promotion the run is ready for, not the review
    # dispatch its own row carries: a duty that reads ready to promote while
    # naming that dispatch offers work the tier rule has decided against.
    assert docs_item["next_command"].startswith(
        f"reckon crew complete --run {DOCS_RUN}"
    ), docs_item

    # The runtime-source run owes a review still and reads it missing, so the
    # quietening above is keyed on the tier and not on something the read did
    # to every run.
    assert SOURCE_RUN in items, items
    assert items[SOURCE_RUN]["kind"] == "review-missing", items[SOURCE_RUN]
    assert "crew complete" not in items[SOURCE_RUN]["next_command"], items[SOURCE_RUN]


def test_a_tier_none_run_citing_only_its_base_offers_the_no_commit_form(
    project: Path,
) -> None:
    """A citation of the dispatch base is refused, so the duty offers none.

    The base predates the run, so promotion refuses a citation of the base and
    the offer takes the declaration a commitless run promotes under. The duty
    reads that case from the same helper the classifier does, so the two agree
    about it without a second reading of the rule here.
    """
    root = project
    base = _git(root, "rev-parse", "HEAD")
    _write_pointer(
        root,
        BASE_RUN,
        commit=base,
        changed_paths=("seed.txt",),
        base=base,
    )

    items = _items(root)

    assert BASE_RUN in items, items
    base_item = items[BASE_RUN]
    assert base_item["review_tier"] == "none", base_item
    assert base_item["next_command"].startswith(
        f"reckon crew complete --run {BASE_RUN} --gate not-run --no-commit"
    ), base_item
