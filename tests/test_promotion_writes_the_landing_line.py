"""A run's ``landing:`` manifest line lands through the plan's versioned write.

A run under the fragment default records its landing by writing a ``landing:``
line in its manifest rather than editing the plan. Promotion reads that line
and writes it as the node section's comment through the same versioned plan
write every other landing uses, so the record reaches the plan as an ordinary
versioned edit and never depends on a git merge of the plan HTML.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import _plan_html, _store, crew
from reckon.crew import promotion
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "landing-line-fixture"
PLAN = "landing-target"
NODE = "promotion-writes-the-landing-line"
RUN_IDS = (
    "r-20260928T101000000001-landing-line",
    "r-20260928T101000000002-no-landing-line",
    "r-20260928T101000000003-concurrent-edit",
)
# A synthetic promotion whose subject is the plan write itself carries no
# stored review; the landing comment is what these tests measure.
REVIEW_WAIVER = "the landing write is the subject; this synthetic run stores no review"
LANDING_LINE = (
    "the run's landing record lands as a versioned plan comment; "
    "docs/evidence/fragments/landing-target/promotion-writes-the-landing-line.html "
    "carries the anchor"
)


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write_plan(root: Path) -> Path:
    path = root / "docs" / "plans" / f"{PLAN}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    state = {
        "type": "plan",
        "slug": PLAN,
        "title": "Landing target",
        "status": "active",
        "version": 0,
        "comments": {},
    }
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def real_stores_are_not_fixture_targets() -> None:
    repository = Path(__file__).resolve().parents[1]
    real_plan = repository / "docs" / "plans" / f"{PLAN}.html"
    real_state = repository / "docs" / "state" / PROJECT
    real_home = Path.home() / ".config" / "reckon" / "crew"
    real_run_paths = [
        path
        for run_id in RUN_IDS
        for path in (real_home / "live" / f"{run_id}.json", real_home / "runs" / run_id)
    ]
    assert not real_plan.exists()
    assert not real_state.exists()
    assert not any(path.exists() for path in real_run_paths)
    yield
    assert not real_plan.exists()
    assert not real_state.exists()
    assert not any(path.exists() for path in real_run_paths)


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    _write_plan(root)
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "docs"),
        ("commit", "-q", "-m", "test: seed repository"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _dispatch(
    repository: Path,
    run_id: str,
    tmp_path: Path,
    *,
    manifest_body: str,
) -> Path:
    manifest = tmp_path / f"{run_id}-manifest.md"
    manifest.write_text(manifest_body, encoding="utf-8")
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "launch": "in-harness",
            "role": "implement",
            "member": "worker-a",
            "backend": "native",
            "created_at": "2026-09-28T10:00:00Z",
            "manifest_path": str(manifest),
            "node": {
                "id": NODE,
                "plan": PLAN,
                "section": "§3",
                "time_budget": "25m",
                "write_paths": [],
            },
        },
    )
    return manifest


MANIFEST_WITH_LANDING = f"""node: {NODE}
status: complete
landing: {LANDING_LINE}
"""

MANIFEST_WITHOUT_LANDING = f"""node: {NODE}
status: complete
"""


def test_manifest_landing_line_lands_as_a_comment_and_bumps_the_version(
    repository: Path, tmp_path: Path
) -> None:
    run_id = RUN_IDS[0]
    _dispatch(repository, run_id, tmp_path, manifest_body=MANIFEST_WITH_LANDING)
    _before, before_version = _store.read_plan(
        PROJECT, PLAN, repository, artifact_type="plan"
    )

    # The landing line replaces the coordinator narrative, so a run carrying
    # one is promoted with no narrative at all: the only record is the landing
    # line, and if promotion stops writing it the section is left with no
    # comment.
    crew.complete(
        run_id,
        gate="passed",
        outcome="",
        root=repository,
        review_waiver=REVIEW_WAIVER,
    )

    plan, after_version = _store.read_plan(
        PROJECT, PLAN, repository, artifact_type="plan"
    )
    comment = plan["comments"]["s3"][0]
    assert comment["id"] == f"c-run-{run_id}"
    assert comment["body"] == f"<p>{LANDING_LINE}</p>"
    assert after_version > before_version


def test_run_without_a_landing_line_keeps_the_coordinator_narrative(
    repository: Path, tmp_path: Path
) -> None:
    run_id = RUN_IDS[1]
    manifest = _dispatch(
        repository, run_id, tmp_path, manifest_body=MANIFEST_WITHOUT_LANDING
    )
    assert promotion._declared_manifest_landing({"manifest_path": str(manifest)}) == ""

    crew.complete(
        run_id,
        gate="passed",
        outcome="the coordinator records the landing",
        root=repository,
        review_waiver=REVIEW_WAIVER,
    )

    plan, _version = _store.read_plan(PROJECT, PLAN, repository, artifact_type="plan")
    comment = plan["comments"]["s3"][0]
    assert comment["body"] == "<p>the coordinator records the landing</p>"


def test_concurrent_plan_edit_between_read_and_write_is_retried_not_lost(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = RUN_IDS[2]
    _dispatch(repository, run_id, tmp_path, manifest_body=MANIFEST_WITH_LANDING)
    real_write = promotion._store.write_plan
    calls = 0

    def concurrent_then_conflict(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            # A peer lands an unrelated comment on the same plan between this
            # promotion's read and its write, so its write is stale and refused.
            project, plan_slug, _state, _version, root = args[:5]
            peer_state, peer_version = _store.read_plan(
                project, plan_slug, root, artifact_type="plan"
            )
            comments = {
                key: list(items)
                for key, items in (peer_state.get("comments") or {}).items()
            }
            comments.setdefault("s3", []).append(
                {
                    "id": "c-run-peer-node",
                    "who": "peer",
                    "when": "2026-09-28T10:01:00Z",
                    "body": "<p>a concurrent landing</p>",
                }
            )
            real_write(
                project,
                plan_slug,
                {**peer_state, "comments": comments},
                peer_version,
                root,
                artifact_type="plan",
            )
            raise _store.VersionConflict(_version, peer_version, {})
        return real_write(*args, **kwargs)

    monkeypatch.setattr(promotion._store, "write_plan", concurrent_then_conflict)

    crew.complete(
        run_id,
        gate="passed",
        outcome="unused narrative",
        root=repository,
        review_waiver=REVIEW_WAIVER,
    )

    plan, _version = _store.read_plan(PROJECT, PLAN, repository, artifact_type="plan")
    ids = [entry["id"] for entry in plan["comments"]["s3"]]
    assert ids == ["c-run-peer-node", f"c-run-{run_id}"]
    assert plan["comments"]["s3"][1]["body"] == f"<p>{LANDING_LINE}</p>"
    assert calls == 2
