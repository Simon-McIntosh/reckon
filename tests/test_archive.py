from __future__ import annotations

from datetime import date
from pathlib import Path

from click.testing import CliRunner

from reckon import _plan_html
from reckon.archive import ArchiveConfig, find_archive_candidates, run_archive_pass
from reckon.cli import main


TODAY = date(2026, 8, 24)


def _age_days(since: str, today: date | None = None) -> str:
    return str(((today or date.today()) - date.fromisoformat(since)).days)


def _write_document(
    docs_dir: Path,
    slug: str,
    *,
    status: str,
    modified: str,
    archived: str = "",
    artifact_type: str = "plan",
) -> Path:
    bare = (
        '<!doctype html><html lang="en"><head>'
        '<meta charset="utf-8">'
        '<meta name="docs-project" content="proj">'
        f"<title>{slug}</title></head>"
        '<body><main class="plan-doc"><p>Authored content.</p></main></body></html>'
    )
    state = {
        "slug": slug,
        "title": slug,
        "type": artifact_type,
        "status": status,
        "modified": modified,
    }
    if archived:
        state["archived"] = archived
    root = {"plan": "plans", "research": "research", "evidence": "evidence"}[
        artifact_type
    ]
    path = docs_dir / root / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")
    return path


def _archived(path: Path) -> str:
    return str(_plan_html.parse_meta(path).get("archived") or "")


def test_dry_run_lists_every_candidate_without_changing_files(tmp_path) -> None:
    repo = tmp_path / "repo"
    docs = repo / "docs"
    done = _write_document(
        docs, "finished-plan", status="done", modified="2026-04-01"
    )
    superseded = _write_document(
        docs,
        "replaced-research",
        status="superseded",
        modified="2026-03-01",
        artifact_type="research",
    )
    before = {path: path.read_bytes() for path in (done, superseded)}

    result = CliRunner().invoke(
        main,
        [
            "archive",
            "--project",
            "proj",
            "--checkout-path",
            str(repo),
            "--older-than-days",
            "90",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "finished-plan" in result.output
    assert "done" in result.output
    assert f"{_age_days('2026-04-01')}d" in result.output
    assert "replaced-research" in result.output
    assert "superseded" in result.output
    assert f"{_age_days('2026-03-01')}d" in result.output
    assert "Dry run: 2 candidate(s); no files changed." in result.output
    assert {path: path.read_bytes() for path in before} == before


def test_apply_reports_all_candidates_before_setting_only_archive_flags(
    tmp_path,
) -> None:
    docs = tmp_path / "docs"
    done = _write_document(docs, "finished", status="done", modified="2026-01-01")
    superseded = _write_document(
        docs, "replaced", status="superseded", modified="2026-02-01"
    )
    exact_threshold = _write_document(
        docs, "boundary", status="done", modified="2026-05-26"
    )
    recent = _write_document(
        docs, "recent", status="done", modified="2026-07-01"
    )
    originals = {path: path.read_text() for path in (done, superseded)}
    observed: list[str] = []

    def reporter(candidates) -> None:
        observed.extend(candidate.slug for candidate in candidates)
        assert all(_archived(path) == "" for path in originals)

    report = run_archive_pass(
        docs,
        "proj",
        ArchiveConfig(older_than_days=90),
        apply=True,
        reporter=reporter,
        today=TODAY,
    )

    assert observed == ["finished", "replaced"]
    assert report.archived == (done, superseded)
    assert _archived(done) == "1"
    assert _archived(superseded) == "1"
    assert _archived(exact_threshold) == ""
    assert _archived(recent) == ""
    for path, original in originals.items():
        assert path.read_text() == _plan_html.write_state(original, {"archived": "1"})


def test_non_terminal_documents_never_become_candidates(tmp_path) -> None:
    docs = tmp_path / "docs"
    for status in ("draft", "pending", "active", "in-progress", "shipped"):
        _write_document(
            docs,
            f"{status}-document",
            status=status,
            modified="2000-01-01",
        )

    report = run_archive_pass(
        docs,
        "proj",
        ArchiveConfig(older_than_days=0),
        apply=True,
        today=TODAY,
    )

    assert report.candidates == ()
    assert report.archived == ()
    assert all(_archived(path) == "" for path in docs.rglob("*.html"))


def test_already_archived_documents_are_skipped_and_repeat_is_a_no_op(
    tmp_path,
) -> None:
    docs = tmp_path / "docs"
    already = _write_document(
        docs,
        "already-archived",
        status="done",
        modified="2020-01-01",
        archived="1",
    )
    eligible = _write_document(
        docs, "archive-once", status="done", modified="2020-01-01"
    )
    already_before = already.read_bytes()

    first = run_archive_pass(
        docs,
        "proj",
        ArchiveConfig(older_than_days=30),
        apply=True,
        today=TODAY,
    )
    after_first = {path: path.read_bytes() for path in (already, eligible)}
    second = run_archive_pass(
        docs,
        "proj",
        ArchiveConfig(older_than_days=30),
        apply=True,
        today=TODAY,
    )

    assert [candidate.slug for candidate in first.candidates] == ["archive-once"]
    assert first.archived == (eligible,)
    assert already.read_bytes() == already_before
    assert second.candidates == ()
    assert second.archived == ()
    assert {path: path.read_bytes() for path in after_first} == after_first


def test_age_threshold_comes_from_pass_configuration(tmp_path) -> None:
    docs = tmp_path / "docs"
    _write_document(docs, "forty-days-old", status="done", modified="2026-07-15")

    broad = find_archive_candidates(
        docs,
        "proj",
        ArchiveConfig(older_than_days=30),
        today=TODAY,
    )
    narrow = find_archive_candidates(
        docs,
        "proj",
        ArchiveConfig(older_than_days=60),
        today=TODAY,
    )

    assert [candidate.slug for candidate in broad] == ["forty-days-old"]
    assert narrow == ()
