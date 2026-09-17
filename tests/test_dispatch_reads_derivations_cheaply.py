from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from reckon import _plan_html, crew, flight, project_state
from reckon.crew.dispatch import _repository_scope_claims


def _path_signature(path: Path) -> tuple[int, int, int, int, int] | None:
    if not path.exists():
        return None
    stat = path.stat()
    return (
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
    )


def _plan_document(project: str, slug: str) -> str:
    return (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{project}">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{slug}">'
        f'<meta name="plan-title" content="{slug}">'
        '<meta name="plan-status" content="active">'
        '<meta name="plan-impl" content="0">'
        '<meta name="plan-version" content="0">'
        f"</head><body><h2>{slug}</h2></body></html>"
    )


def _legacy_repository(
    root: Path,
    project: str,
    *,
    derivations: dict[str, list[str]] | None,
    plan_count: int = 3,
) -> Path:
    docs = root / "docs"
    plans = docs / "plans"
    state = docs / "state" / project
    plans.mkdir(parents=True)
    state.mkdir(parents=True)
    slugs = [f"{project}-subject-{position}" for position in range(plan_count)]
    for slug in slugs:
        (plans / f"{slug}.html").write_text(
            _plan_document(project, slug), encoding="utf-8"
        )
    project_manifest: dict[str, Any] = {"name": project}
    if derivations is not None:
        project_manifest["derivations"] = derivations
    (state / "index.json").write_text(
        json.dumps(
            {
                "project": project,
                "doc": "index",
                "data": {
                    "_version": 0,
                    "projects": [project_manifest],
                    "sprints": [{"id": "current", "items": slugs}],
                },
            }
        ),
        encoding="utf-8",
    )
    return root


def _distributed_repository(
    root: Path,
    project: str,
    *,
    derivations: dict[str, list[str]] | None,
) -> Path:
    docs = root / "docs"
    marker = docs / ".reckon" / "project-state-migration.json"
    state = docs / "state" / project
    marker.parent.mkdir(parents=True)
    state.mkdir(parents=True)
    marker.write_text(
        json.dumps(
            {
                "format": "distributed",
                "status": "complete",
                "project": project,
                "resources": [{"type": "project", "id": "project", "version": 0}],
            }
        ),
        encoding="utf-8",
    )
    data: dict[str, Any] = {
        "project": project,
        "id": "project",
        "type": "project",
        "version": 0,
    }
    if derivations is not None:
        data["derivations"] = derivations
    (state / "project.json").write_text(
        json.dumps({"project": project, "doc": "project", "data": data}),
        encoding="utf-8",
    )
    return root


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


@pytest.mark.parametrize("storage", ["legacy", "distributed"])
def test_derivation_reader_matches_composed_project_manifest(
    tmp_path: Path, storage: str
) -> None:
    project = f"{storage}-project"
    expected = {"schema/source.yaml": ["generated/first.py", "generated/second.py"]}
    builder = _legacy_repository if storage == "legacy" else _distributed_repository
    repository = builder(tmp_path / storage, project, derivations=expected)
    docs = repository / "docs"

    direct = project_state.read_project_derivations(docs, project)
    composed = project_state.compose_project_state(docs, project)["projects"][0][
        "derivations"
    ]

    assert _canonical_bytes(direct) == _canonical_bytes(composed)


@pytest.mark.parametrize("storage", ["legacy", "distributed"])
def test_derivation_reader_returns_empty_mapping_when_none_is_authored(
    tmp_path: Path, storage: str
) -> None:
    project = f"empty-{storage}-project"
    builder = _legacy_repository if storage == "legacy" else _distributed_repository
    repository = builder(tmp_path / storage, project, derivations=None)

    assert project_state.read_project_derivations(repository / "docs", project) == {}


def test_scope_claim_plan_reads_do_not_grow_with_live_pointer_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_crew_home = crew.crew_home()
    real_docs = tuple(flight.mounted_project_docs().values())
    real_signatures = {
        path: _path_signature(path) for path in (real_crew_home, *real_docs)
    }

    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    derivations = {"schema/source.yaml": ["generated/output.py"]}
    repositories: dict[str, Path] = {}
    for name in ("alpha", "beta", "gamma"):
        project = f"project-{name}"
        repositories[project] = _legacy_repository(
            tmp_path / name,
            project,
            derivations=derivations,
        )
    (config_home / "mounts.json").write_text(
        json.dumps(
            {
                project: str(repository / "docs")
                for project, repository in repositories.items()
            }
        ),
        encoding="utf-8",
    )

    read_count = 0
    original_read_state = _plan_html.read_state

    def counted_read_state(text: str) -> dict[str, Any]:
        nonlocal read_count
        read_count += 1
        return original_read_state(text)

    monkeypatch.setattr(_plan_html, "read_state", counted_read_state)
    observed_counts: list[int] = []
    project_names = tuple(repositories)
    for pointer_count in (1, 3, 7):
        live = crew.live_dir()
        live.mkdir(parents=True, exist_ok=True)
        for path in live.glob("*.json"):
            path.unlink()
        for position in range(pointer_count):
            project = project_names[position % len(project_names)]
            repository = repositories[project]
            run_id = f"run-{position}"
            crew._write_json(
                crew.pointer_path(run_id),
                {
                    "run_id": run_id,
                    "project": project,
                    "repo": str(repository),
                    "phase": "working",
                    "node": {
                        "id": f"worker-{position}",
                        "write_paths": ["schema/source.yaml"],
                    },
                },
            )
        project_state._PLAN_STATE_CACHE.clear()
        read_count = 0

        claims = _repository_scope_claims()

        observed_counts.append(read_count)
        assert len(claims) == pointer_count * 2

    assert observed_counts == [0, 0, 0]
    assert crew.crew_home().is_relative_to(config_home)
    assert {
        path: _path_signature(path) for path in (real_crew_home, *real_docs)
    } == real_signatures
