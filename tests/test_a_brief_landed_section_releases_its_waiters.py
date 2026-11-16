"""A section's reachable brief-carried promotion releases its queued waiters."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew import runs
from reckon.crew.admission import readiness, release
from reckon.crew.dispatch_sections import project_mount_repository
from tests.test_a_wait_releases_when_its_upstream_lands import _git


def _promotion(tmp_path, home, monkeypatch, *, carrier, reachable=True):
    """Write a promotion through the store, registering only a temporary checkout."""
    project = tmp_path.name
    slug, section = "delivery", "upstream"
    repository = tmp_path / "checkout"
    docs = repository / "docs"
    (docs / "plans").mkdir(parents=True)
    declarations = json.dumps({section: "done", "waiter": "implementable"})
    (docs / "plans" / f"{slug}.html").write_text(
        "<!doctype html>\n"
        f'<meta name="docs-project" content="{project}">\n'
        '<meta name="reckon-type" content="plan">\n'
        f'<meta name="plan-slug" content="{slug}">\n'
        '<meta name="plan-status" content="active">\n'
        f'<meta name="plan-section-declarations" '
        f"content='{declarations}'>\n"
        f'<h2 id="{section}">Upstream</h2>\n'
        '<h2 id="waiter">Waiter</h2>\n',
        encoding="utf-8",
    )
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "Test")
    _git(repository, "config", "user.email", "test@example.invalid")
    _git(repository, "add", "docs/plans/delivery.html")
    _git(repository, "commit", "-q", "-m", "test: seed checkout\n\nDeclare work.")
    head = _git(repository, "rev-parse", "HEAD")
    revision = head
    if not reachable:
        revision = _git(
            repository,
            "commit-tree",
            "HEAD^{tree}",
            "-p",
            head,
            "-m",
            "test: pending delivery\n\nKeep this revision outside HEAD.",
        )
    ancestry = subprocess.run(
        ["git", "-C", str(repository), "merge-base", "--is-ancestor", revision, head],
        check=False,
        capture_output=True,
    )
    assert ancestry.returncode == (0 if reachable else 1)
    mounts = home / "mounts.json"
    mounts.write_text(json.dumps({project: str(docs)}), encoding="utf-8")
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(docs / "state"))
    assert Path(project_mount_repository(project)).resolve() == repository.resolve()
    row = ledger.build_record(
        run_id="r-promoted",
        project=project,
        plan=slug if carrier == "plan" else "",
        section=section,
        node="delivery",
        gate="passed",
        brief=None if carrier == "plan" else {"path": "brief.md", "sha256": "0" * 64},
        node_definition={"id": "delivery", "section": section},
    )
    if carrier != "plan":
        row[carrier] = {"plan": slug, "section": section}
    row["promoted_revision"] = revision
    ledger.run_path(project, row["run_id"]).parent.mkdir(parents=True)
    ledger.write_run_file(project, row, fsync=False)
    stored = ledger.load(project)[0]["runs"]
    assert len(stored) == 1
    assert stored[0]["promoted_revision"] == revision
    assert ledger.row_plan(stored[0]) == slug
    assert stored[0]["section"] == section
    assert runs._list_live_records(project=project) == []
    return project, slug, section, docs


@pytest.mark.parametrize("carrier", ["impl_move", "node_definition"])
def test_a_reachable_brief_promotion_is_not_unlanded(
    tmp_path, home, monkeypatch, carrier
):
    project, slug, section, _docs = _promotion(
        tmp_path, home, monkeypatch, carrier=carrier
    )

    assert readiness._section_run_unlanded(project, slug, section, live=()) is False


@pytest.mark.parametrize("carrier", ["impl_move", "node_definition"])
def test_an_unreachable_brief_promotion_still_holds_its_section(
    tmp_path, home, monkeypatch, carrier
):
    project, slug, section, _docs = _promotion(
        tmp_path, home, monkeypatch, carrier=carrier, reachable=False
    )

    assert readiness._section_run_unlanded(project, slug, section, live=()) is True


def test_a_reachable_plan_promotion_is_not_unlanded(tmp_path, home, monkeypatch):
    project, slug, section, _docs = _promotion(
        tmp_path, home, monkeypatch, carrier="plan"
    )

    assert readiness._section_run_unlanded(project, slug, section, live=()) is False


@pytest.mark.parametrize("carrier", ["impl_move", "node_definition"])
@pytest.mark.parametrize("reachable", [True, False])
def test_the_queue_releases_a_waiter_only_after_its_brief_promotion_lands(
    tmp_path, home, monkeypatch, carrier, reachable
):
    project, slug, section, docs = _promotion(
        tmp_path, home, monkeypatch, carrier=carrier, reachable=reachable
    )
    waiter = {
        "run_id": "r-waiter",
        "project": project,
        "phase": "awaiting-upstream",
        "node": {"id": "waiter", "plan": slug, "section": "waiter"},
        "waits": [f"{slug}#{section}"],
    }

    phase = release.resolve_waits(waiter, live=[waiter], docs_dir=docs, cache={})

    assert phase == ("queued" if reachable else "awaiting-upstream")


def test_git_commands_share_the_checked_helper():
    from tests import test_a_wait_releases_when_its_upstream_lands as owner

    assert _git is owner._git
