"""The sprint view carries the same wiring finding the document audit emits."""

from __future__ import annotations

import json
from pathlib import Path

from reckon.doccheck import unwired_plan_finding
from reckon.roadmap import build_roadmap

PROJECT = "unmounted-project-for-wiring-findings"
MOUNTED = "mounted-project-for-wiring-findings"
ENFORCED_FROM = "2026-09-19"


def _write_plan(
    docs_dir: Path,
    *,
    project: str,
    slug: str,
    standalone: str | None = None,
    modified: str = ENFORCED_FROM,
    status: str = "active",
) -> Path:
    """Write one plan HTML into ``docs_dir/plans`` in the store's own layout."""

    path = docs_dir / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    metas = [
        ("docs-project", project),
        ("reckon-type", "plan"),
        ("plan-slug", slug),
        ("plan-status", status),
        ("plan-modified", modified),
    ]
    if standalone is not None:
        metas.append(("plan-standalone", standalone))
    head = "".join(f'<meta name="{name}" content="{value}">' for name, value in metas)
    path.write_text(
        "<!doctype html><html><head>"
        f"{head}<title>{slug}</title></head>"
        '<body><main class="plan-doc"></main></body></html>',
        encoding="utf-8",
    )
    return path


def _mount(tmp_path: Path, monkeypatch, project: str, docs_dir: Path) -> None:
    """Register ``docs_dir`` as the project's mount for this test."""

    mounts_file = tmp_path / "mounts.json"
    mounts_file.write_text(json.dumps({project: str(docs_dir)}), encoding="utf-8")
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))


def _plan(**overrides) -> dict:
    row = {
        "slug": "plan",
        "type": "plan",
        "status": "active",
        "modified": ENFORCED_FROM,
        "depends_on": [],
        "blocks": [],
        "informs": [],
        "gates": [],
    }
    row.update(overrides)
    return row


def _wiring(report):
    return [f for f in report["wiring_findings"] if f["code"] == "unwired-plan"]


def test_roadmap_reports_an_unwired_plan_as_an_error():
    report = build_roadmap(PROJECT, [_plan(slug="unwired")], [])

    (finding,) = _wiring(report)

    assert finding["severity"] == "error"
    assert finding["slug"] == "unwired"
    assert (
        finding["message"]
        == unwired_plan_finding(
            doc_type="plan",
            status="active",
            modified=ENFORCED_FROM,
            links=[],
            gate_count=0,
            standalone=None,
            slug="unwired",
        ).message
    )


def test_roadmap_warns_on_a_plan_that_predates_the_rule():
    report = build_roadmap(PROJECT, [_plan(slug="legacy", modified="2026-08-01")], [])

    (finding,) = _wiring(report)

    assert finding["severity"] == "warn"


def test_roadmap_is_silent_for_a_wired_plan():
    report = build_roadmap(PROJECT, [_plan(slug="wired", depends_on=["other"])], [])

    assert _wiring(report) == []


def test_roadmap_is_silent_for_a_gated_plan():
    report = build_roadmap(
        PROJECT,
        [_plan(slug="gated", gates=[{"id": "g", "verdict": "pending"}])],
        [],
    )

    assert _wiring(report) == []


def test_roadmap_is_silent_for_a_terminal_plan():
    report = build_roadmap(PROJECT, [_plan(slug="done", status="shipped")], [])

    assert _wiring(report) == []


def test_roadmap_is_silent_for_a_plan_that_declares_standalone(tmp_path):
    docs_dir = tmp_path / "docs"
    _write_plan(
        docs_dir,
        project=MOUNTED,
        slug="alone",
        standalone="Feeds nothing and waits on nothing; it is a one-file fix.",
    )

    report = build_roadmap(MOUNTED, [_plan(slug="alone")], [], docs_dir=docs_dir)

    assert _wiring(report) == []


def test_roadmap_finds_the_declaration_in_the_inventoried_checkout(
    tmp_path, monkeypatch
):
    """A worktree-scoped roadmap reads declarations from the rows' own tree.

    The rows are inventoried from a worktree while the project is mounted at
    another checkout; reading the registered mount would judge one tree's plans
    against another tree's declarations and flip the verdict.
    """

    worktree = tmp_path / "worktree"
    registered = tmp_path / "registered"
    _write_plan(
        worktree,
        project=MOUNTED,
        slug="alone",
        standalone="Single-file fix; it feeds nothing and waits on nothing.",
    )
    _write_plan(registered, project=MOUNTED, slug="alone")
    _mount(tmp_path, monkeypatch, MOUNTED, registered)

    scoped = build_roadmap(MOUNTED, [_plan(slug="alone")], [], docs_dir=worktree)
    unmounted = build_roadmap(MOUNTED, [_plan(slug="alone")], [])

    assert _wiring(scoped) == []
    # Positive control: the mount is consulted only when no checkout is named,
    # and it does not carry the declaration.
    assert len(_wiring(unmounted)) == 1


def test_worktree_roadmap_flags_a_plan_the_mount_silences(tmp_path, monkeypatch):
    """The other direction: the worktree, not the mount, is the authority."""

    worktree = tmp_path / "worktree"
    registered = tmp_path / "registered"
    _write_plan(worktree, project=MOUNTED, slug="not-declared-here")
    _write_plan(
        registered,
        project=MOUNTED,
        slug="not-declared-here",
        standalone="Declared in the registered mount only.",
    )
    _mount(tmp_path, monkeypatch, MOUNTED, registered)

    scoped = build_roadmap(
        MOUNTED, [_plan(slug="not-declared-here")], [], docs_dir=worktree
    )
    unmounted = build_roadmap(MOUNTED, [_plan(slug="not-declared-here")], [])

    assert len(_wiring(scoped)) == 1
    assert _wiring(unmounted) == []
