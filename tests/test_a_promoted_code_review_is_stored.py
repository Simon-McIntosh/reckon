"""A delivered code review is stored when its run promotes.

A code review is keyed by the neighbourhood it read rather than by a run id: the
composed run id and the promoting run id differ, so the store index lookup by the
promoting run's id finds nothing, and a ``review-code-<key>`` node is neither a
plan review nor a run review. Before this, promotion stored and committed
nothing for such a run, so the delivered review survived only as a report on
disk: the findings view held none of its rows and ``crew dispose`` refused with
no stored review. Here promotion finds the delivery by the neighbourhood key
through its ``code-review.json`` sidecar, stores it in the staging store, where
the findings view and ``crew dispose`` read it, and commits no payload, because
a code review names neither a plan nor a reviewed run and would roll the landing
back.

Every crew directory is environment-resolved under ``tmp_path``; nothing touches
the operator's own store.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import crew, crew_run_commands
from reckon.crew import findings as findings_reader
from reckon.crew import plan_review
from reckon.crew import review as review_module
from reckon.crew.runs import _write_json, pointer_path
from tests.conftest import EXECUTABLE_GATE_COMMAND

PROJECT = "sample"
PLAN_SLUG = "fixture"
PLAN_VERSION = 3
# The composed run id and the promoting run id carry the same neighbourhood key
# but differ, which is the whole reason the index lookup by the promoting run's
# id finds nothing.
KEY = "a8eb488cc578"
COMPOSED_RUN_ID = f"r-20261015T053316274650-review-code-{KEY}"
PROMOTING_RUN_ID = f"r-20261015T053318062329-review-code-{KEY}"
NAMED_PATHS = ["reckon/a.py"]
MODULES = ["reckon/a.py", "reckon/b.py"]
REVIEWED_REVISION = "b" * 40
DISPATCHED_AT = "2026-10-15T05:33:18+00:00"
COMPLETED_AT = "2026-10-15T05:50:00+00:00"

# A delivered health review in the grammar the rubric's reader takes: one RUBRIC
# line per item, a FINDING line naming a path, and the single TREATMENT line the
# store refuses a report without.
REPORT = (
    "RUBRIC known_findings: pass — the brief names no open finding.\n"
    "RUBRIC duplicate_capability: a finding — b.py owns what a re-reads.\n"
    "RUBRIC unreached: pass — every module is reached by an entry point.\n"
    "RUBRIC interface_growth: pass — no public surface was added.\n"
    "RUBRIC width: pass — one module changed.\n"
    "RUBRIC test_debt: pass — no test asserts source text.\n"
    "FINDING duplicate_capability reckon/a.py:3 — CLASS: duplicated_capability "
    "— the constant is owned twice — CITES: new\n"
    "TREATMENT: none — the neighbourhood is healthy\n"
)
UNDELIVERED_REPORT = "report drafted, no reviewer output yet\n"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_VELOCITY_CACHE", str(home / "cache" / "velocity"))
    repo = tmp_path / "repo"
    package = repo / "reckon"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "a.py").write_text("import reckon.b\n\n\nVALUE = 1\n", encoding="utf-8")
    (package / "b.py").write_text("VALUE = 1\n", encoding="utf-8")
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / f"{PLAN_SLUG}.html").write_text(
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{PLAN_SLUG}">'
        '<meta name="plan-title" content="Fixture">'
        '<meta name="plan-status" content="active">'
        f'<meta name="plan-version" content="{PLAN_VERSION}">'
        '</head><body><h2 id="delivery">Delivery</h2>'
        "<p>Extend the existing mechanism.</p></body></html>",
        encoding="utf-8",
    )
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "test@example.invalid"],
        ["config", "user.name", "Test"],
        ["add", "reckon", "docs"],
        ["commit", "-q", "-m", "chore: seed fixture", "-m", "Supply a plan and code."],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (home / "mounts.json").write_text(json.dumps({PROJECT: str(repo / "docs")}))
    return home, repo


def _code_review_directory() -> Path:
    return plan_review.code_review_report_directory(project=PROJECT, key=KEY)


def _compose(report_text: str) -> Path:
    """Write a delivered (or undelivered) code review report and its sidecar."""
    directory = _code_review_directory()
    directory.mkdir(parents=True, exist_ok=True)
    report_path = directory / "report.md"
    report_path.write_text(report_text, encoding="utf-8")
    plan_review.write_review_sidecar(
        directory,
        payload={
            "subject": "code",
            "project": PROJECT,
            "around": "",
            "neighbourhood_key": KEY,
            "reviewed_revision": REVIEWED_REVISION,
            "named_paths": NAMED_PATHS,
            "modules": MODULES,
            "report_path": str(report_path),
            "node_id": f"review-code-{KEY}",
            "session": "coordinator",
            "review_run_id": COMPOSED_RUN_ID,
        },
        name=plan_review.CODE_REVIEW_SIDECAR_NAME,
    )
    return directory


def _promoting_pointer(repo: Path, directory: Path) -> None:
    """Write the promoting run's pointer and manifest, one run's whole record."""
    manifest = repo.parent / f"{PROMOTING_RUN_ID}.manifest.md"
    manifest.write_text(
        f"node: review-code-{KEY}\n"
        "status: complete\n"
        "commits: none\n"
        "changed_paths: []\n"
        f"tests: {EXECUTABLE_GATE_COMMAND}\n",
        encoding="utf-8",
    )
    head = (
        subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True
        )
        .stdout.decode()
        .strip()
    )
    _write_json(
        pointer_path(PROMOTING_RUN_ID),
        {
            "run_id": PROMOTING_RUN_ID,
            "project": PROJECT,
            "repo": str(repo),
            "worktree": str(repo),
            "base_sha": head,
            "launch": "in-harness",
            "backend": "native",
            "created_at": DISPATCHED_AT,
            "completed_at": COMPLETED_AT,
            "role": "review",
            "manifest_path": str(manifest),
            "node": {
                "id": f"review-code-{KEY}",
                "plan": "",
                "section": "",
                "role": "review",
                "write_paths": [str(directory)],
            },
        },
    )


def _stored() -> list[dict]:
    return plan_review.list_code_reviews(project=PROJECT)


def _stored_sidecar(directory: Path) -> dict:
    path = directory / plan_review.CODE_REVIEW_SIDECAR_NAME
    return json.loads(path.read_text(encoding="utf-8"))


# -- (1) A code review is stored on promotion, under a differing run id --------


def test_a_code_review_composed_under_one_run_id_is_stored_on_promotion(
    project: tuple[Path, Path],
) -> None:
    _home, repo = project
    directory = _compose(REPORT)
    assert _stored() == [], "nothing is stored before the run promotes"

    _promoting_pointer(repo, directory)
    crew.complete(PROMOTING_RUN_ID, gate="passed", root=repo)

    stored = _stored()
    assert len(stored) == 1
    record = stored[0]
    assert record["neighbourhood_key"] == KEY
    assert record["review_run_id"] == COMPOSED_RUN_ID
    assert record["subject"] == "code"


# -- (2) The promotion lands and commits no code review ----------------------


def test_the_promotion_lands_and_commits_no_code_review(
    project: tuple[Path, Path],
) -> None:
    _home, repo = project
    directory = _compose(REPORT)
    _promoting_pointer(repo, directory)
    result = crew.complete(PROMOTING_RUN_ID, gate="passed", root=repo)

    # The landing succeeded: a rollback raises rather than returning a row.
    assert result, "promotion returned no ledger row"

    committed = review_module.committed_review_root(PROJECT, root=repo)
    if committed is not None:
        code_files = sorted(str(p) for p in committed.rglob("code-*.json"))
        assert code_files == [], (
            f"a code review reached the committed tree: {code_files}"
        )


# -- (3) The findings view returns the stored review's findings ---------------


def test_the_findings_view_returns_the_stored_code_review(
    project: tuple[Path, Path],
) -> None:
    _home, repo = project
    directory = _compose(REPORT)
    _promoting_pointer(repo, directory)
    crew.complete(PROMOTING_RUN_ID, gate="passed", root=repo)

    grouped = findings_reader.findings_by_path(PROJECT, path="reckon/a.py", root=repo)
    paths = {group["path"] for group in grouped["paths"]}
    assert "reckon/a.py" in paths
    rows = [row for group in grouped["paths"] for row in group["rows"]]
    assert any("owned twice" in row["text"] for row in rows)


# -- (4) crew dispose records a disposition by the promoting run id ----------


def test_crew_dispose_records_a_disposition_by_the_promoting_run_id(
    project: tuple[Path, Path],
) -> None:
    _home, repo = project
    directory = _compose(REPORT)
    _promoting_pointer(repo, directory)
    crew.complete(PROMOTING_RUN_ID, gate="passed", root=repo)

    crew_run_commands.crew_dispose.callback(
        project=PROJECT,
        reviewed_run_id=PROMOTING_RUN_ID,
        dimension=None,
        finding_index=0,
        kind="exempted",
        node_id=None,
        reason="the duplicate is deliberate",
        pretty=False,
    )

    from reckon.crew.findings import finding_disposition

    assert (
        finding_disposition(_stored()[0], 0) == "exempted:the duplicate is deliberate"
    )


# -- (5) An undelivered report records store_error and promotion proceeds -----


def test_a_report_with_no_review_material_records_store_error_and_proceeds(
    project: tuple[Path, Path],
) -> None:
    _home, repo = project
    directory = _compose(UNDELIVERED_REPORT)
    _promoting_pointer(repo, directory)
    result = crew.complete(PROMOTING_RUN_ID, gate="passed", root=repo)

    assert result, "the promotion did not proceed"
    assert _stored() == [], "an undelivered report was stored as a review"
    sidecar = _stored_sidecar(directory)
    assert sidecar.get("store_error"), "the refusal was not recorded on the sidecar"


# -- (6) An already-stored code review is not stored a second time -----------


def test_an_already_stored_code_review_is_not_stored_again(
    project: tuple[Path, Path],
) -> None:
    _home, repo = project
    directory = _compose(REPORT)
    _promoting_pointer(repo, directory)
    crew.complete(PROMOTING_RUN_ID, gate="passed", root=repo)

    stored = _stored()
    assert len(stored) == 1
    before = Path(stored[0]["review_path"]).read_bytes()

    from reckon.crew import promotion_checks

    promotion_checks._staging_review_record_by_run(
        PROJECT,
        PROMOTING_RUN_ID,
        {"project": PROJECT, "node": {"id": f"review-code-{KEY}"}},
    )
    after = Path(_stored()[0]["review_path"]).read_bytes()
    assert before == after
    assert len(_stored()) == 1
