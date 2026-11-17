"""Preview scope checks spend git probes only on claims a candidate can meet."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import dispatch_claims, dispatch_plan

CONFIG = {
    "default_backend": "worker",
    "backends": {
        "worker": {
            "launch": "cli",
            "command": "codex",
            "sandbox": "worktree-full",
            "time_budget": "20m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}
FIGURE_PATH = "docs/figures/topic/held"


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture()
def fleet(tmp_path: Path, home: Path) -> tuple[list[Path], list[dict]]:
    repositories = []
    pointers = []
    for name in ("alpha", "beta", "gamma"):
        repo = tmp_path / name
        (repo / "docs" / "plans").mkdir(parents=True)
        (repo / "docs" / "plans" / "fixture.html").write_text(
            f'<meta name="docs-project" content="{name}">'
            '<meta name="reckon-type" content="plan">'
            '<meta name="plan-slug" content="fixture">'
            '<h2 id="dispatch">Dispatch</h2>',
            encoding="utf-8",
        )
        (repo / FIGURE_PATH).mkdir(parents=True)
        (repo / FIGURE_PATH / "trace.svg").write_text("<svg/>", encoding="utf-8")
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "add", "docs/plans/fixture.html", f"{FIGURE_PATH}/trace.svg")
        _git(
            repo,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-q",
            "-m",
            "chore: seed repository",
            "-m",
            "Provide independent repository identities for claim checks.",
        )
        repositories.append(repo)
        for index in range(80):
            # Distinct checkout spellings force independent identity probes;
            # sharing one spelling across pointers would measure a cache hit.
            checkout = repo / "checkouts" / str(index)
            checkout.mkdir(parents=True)
            path = FIGURE_PATH if index == 0 else f"package/{name}/item-{index}.py"
            pointer = {
                "run_id": f"r-{name}-{index:03d}",
                "project": name,
                "repo": str(repo),
                "worktree": str(checkout),
                "pid": os.getpid(),
                "phase": "working",
                "node": {"id": f"owner-{name}-{index}", "write_paths": [path]},
            }
            crew._write_json(crew.pointer_path(pointer["run_id"]), pointer)
            pointers.append(pointer)
    (home / "mounts.json").write_text(
        json.dumps({repo.name: str(repo / "docs") for repo in repositories}),
        encoding="utf-8",
    )
    assert len(crew.list_live()) == 240
    return repositories, pointers


def _preview(repo: Path, paths: list[str], *, authority=None):
    return dispatch_plan.plan_dispatch(
        node=crew.TaskNode(
            id="candidate",
            goal="check candidate scope against live claims",
            plan="fixture",
            section="dispatch",
            spec_level="exact",
            done_when="pytest reports all candidate scope checks passing",
            write_paths=paths,
            time_budget="20m",
        ),
        config=CONFIG,
        authority=authority,
        project=repo.name,
        repo=repo,
        report_live_conflicts=True,
        route="deterministic",
        allow_unreviewed_plan=True,
    )


def _verdict(preview):
    return {
        "admission": preview.admission,
        "conflicts": preview.live_conflicts,
        "validation": preview.validation.as_dict(),
        "warnings": preview.warnings,
    }


@pytest.mark.parametrize("path", ["package/alpha/item-1.py", FIGURE_PATH])
def test_preview_scopes_cost_only_claims_the_candidate_can_meet(
    fleet, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    repositories, _pointers = fleet
    repo = repositories[0]
    read_claims = dispatch_claims._repository_scope_claims
    # An exhaustive read is a fresh reference, independent of the preview's
    # narrowed read. Its overlapping owner proves the instrument sees claims.
    exhaustive = read_claims()
    assert len({claim.run_id for claim in exhaustive}) == 240
    assert all(not claim.unread for claim in exhaustive)
    with monkeypatch.context() as reference_patch:
        reference_patch.setattr(
            dispatch_plan, "_repository_scope_claims", lambda **_kwargs: exhaustive
        )
        reference = _verdict(_preview(repo, [path]))
    real_run = subprocess.run
    calls = []
    measurements = []

    def count_git(argv, *args, **kwargs):
        if str(argv[0]) == "git":
            calls.append([str(item) for item in argv])
        return real_run(argv, *args, **kwargs)

    def measured_claims(**kwargs):
        start = time.monotonic()
        with monkeypatch.context() as probe:
            probe.setattr(subprocess, "run", count_git)
            claims = read_claims(**kwargs)
        measurements.append(time.monotonic() - start)
        return claims

    monkeypatch.setattr(dispatch_plan, "_repository_scope_claims", measured_claims)
    actual = _verdict(_preview(repo, [path]))
    assert actual == reference
    assert len(measurements) == 1
    print(
        f"scope measurement: pointers=240 git_calls={len(calls)} seconds={measurements[0]:.6f}"
    )
    assert calls, "the git probe must observe repository identity resolution"
    assert len(calls) <= 20, f"candidate scope used {len(calls)} git subprocess calls"
    assert measurements[0] <= 6


def test_exclusive_figure_refusal_matches_reference_and_releases_with_claim(
    fleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    repositories, pointers = fleet
    repo = repositories[0]
    exhaustive = dispatch_claims._repository_scope_claims()
    with monkeypatch.context() as reference_patch:
        reference_patch.setattr(
            dispatch_plan, "_repository_scope_claims", lambda **_kwargs: exhaustive
        )
        reference = _verdict(_preview(repo, [FIGURE_PATH]))
    refused = _verdict(_preview(repo, [FIGURE_PATH]))
    assert refused == reference
    assert refused["admission"]["state"] == "refused"
    assert refused["admission"]["error"] == "scope-conflict"
    assert refused["admission"]["conflicting_run_id"] == pointers[0]["run_id"]
    assert refused["admission"]["candidate_path"] == FIGURE_PATH
    crew.pointer_path(pointers[0]["run_id"]).unlink()
    assert _preview(repo, [FIGURE_PATH]).admission == {"state": "admitted"}


def test_absolute_foreign_scope_keeps_its_conflict(fleet) -> None:
    repositories, _pointers = fleet
    foreign = repositories[1]
    authority = crew.resolve_dispatch_authority(repositories[0].name, repositories[0])
    authority["repositories"] = [str(repo) for repo in repositories]
    preview = _preview(
        repositories[0], [str(foreign / FIGURE_PATH)], authority=authority
    )
    assert preview.admission["state"] == "refused"
    assert preview.admission["conflicting_run_id"] == "r-beta-000"
