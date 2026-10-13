"""One resolver names the mounted project behind a linked worktree.

A dispatch's worktree lives outside the checkout it belongs to, under its own
directory tree. Resolving a repository path to its projects by the resolved
path therefore misses a worktree of a mounted repository entirely, while
resolving by repository identity — the git common directory, which every
linked worktree shares with its main checkout — names the same project for
both spellings. These tests require the identity-based resolution to be the
one every site uses, not a private lookup repeated per site.
"""

from __future__ import annotations

import contextlib
import importlib
import os
import subprocess
from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew.node import CrewError

# ``reckon.crew`` exposes a ``dispatch`` callable in its own namespace, which
# shadows the submodule of the same name, so the submodules are reached through
# the import system rather than through attribute access on the package.
crew_dispatch_commands = importlib.import_module("reckon.crew_dispatch_commands")
dispatch_module = importlib.import_module("reckon.crew.dispatch")
dispatch_claims_module = importlib.import_module("reckon.crew.dispatch_claims")
promotion_records_module = importlib.import_module("reckon.crew.promotion_records")
routing_module = importlib.import_module("reckon.crew.routing")


def _git(cwd: Path, *args: str) -> None:
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "resolver-test",
            "GIT_AUTHOR_EMAIL": "resolver-test@example.invalid",
            "GIT_COMMITTER_NAME": "resolver-test",
            "GIT_COMMITTER_EMAIL": "resolver-test@example.invalid",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        }
    )
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=env,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _mounted_repository_with_linked_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """Build a git repository and a linked worktree that holds no crew.json.

    The worktree's ``docs/state`` carries no ``crew.json``, so a resolver that
    can only scan the tree finds nothing there and has to fall back to the
    mounted repository identity to name a project at all.
    """
    main = tmp_path / "mounted"
    (main / "docs" / "state").mkdir(parents=True)
    _git(main, "init", "-q", "-b", "main")
    (main / "docs" / "state" / ".keep").write_text("", encoding="utf-8")
    _git(main, "add", "docs/state/.keep")
    _git(main, "commit", "-q", "-m", "init")

    worktree = tmp_path / "linked"
    _git(main, "worktree", "add", "--detach", "-q", str(worktree))
    return main, worktree


def _mounted_table(main: Path) -> dict[Path, tuple[str, ...]]:
    return {main.resolve(): ("alpha",)}


def test_resolver_names_the_mounted_project_for_a_linked_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main, worktree = _mounted_repository_with_linked_worktree(tmp_path)
    monkeypatch.setattr(
        routing_module, "mounted_repository_projects", lambda: _mounted_table(main)
    )

    resolver = routing_module.repository_projects
    assert resolver(worktree) == ("alpha",)
    assert promotion_records_module._project_for_repository(worktree) == "alpha"


def test_shadow_source_reads_the_mounted_project_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main, worktree = _mounted_repository_with_linked_worktree(tmp_path)
    monkeypatch.setattr(
        routing_module, "mounted_repository_projects", lambda: _mounted_table(main)
    )
    seen: list[str] = []

    def fake_runs(project: str, *, root: object = None) -> list[dict[str, object]]:
        seen.append(project)
        return []

    monkeypatch.setattr(ledger, "runs", fake_runs)

    with pytest.raises(CrewError):
        dispatch_module.shadow_source("r-not-in-a-ledger", repo=worktree)

    assert seen == ["alpha"]


def test_every_site_calls_the_one_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main, worktree = _mounted_repository_with_linked_worktree(tmp_path)
    seen: list[Path] = []

    def fake_resolver(path: object) -> tuple[str, ...]:
        seen.append(Path(str(path)).resolve())
        return ("alpha",)

    monkeypatch.setattr(
        routing_module, "mounted_repository_projects", lambda: _mounted_table(main)
    )
    monkeypatch.setattr(routing_module, "repository_projects", fake_resolver)
    monkeypatch.setattr(ledger, "runs", lambda *a, **k: [])
    monkeypatch.setattr(
        routing_module.flight,
        "mounted_project_docs",
        lambda: {"alpha": main.resolve() / "docs"},
    )

    def calls_it(invoke) -> bool:
        seen.clear()
        # A site may refuse the fixture (no ledger record, for instance); the
        # resolver call is the observable the test is after.
        with contextlib.suppress(Exception):
            invoke()
        return bool(seen)

    assert calls_it(
        lambda: crew_dispatch_commands._repository_owning_projects(worktree)
    )
    assert calls_it(
        lambda: dispatch_claims_module._scope_derivation_project("beta", worktree)
    )
    assert calls_it(
        lambda: dispatch_claims_module._resolved_scope_entries(
            ["reckon/crew/routing.py"],
            base_repository=worktree,
            repositories=(worktree,),
            project="alpha",
        )
    )
    assert calls_it(
        lambda: dispatch_module.shadow_source("r-not-in-a-ledger", repo=worktree)
    )
    assert calls_it(lambda: promotion_records_module._project_for_repository(worktree))
    assert calls_it(
        lambda: routing_module.resolve_dispatch_authority("alpha", worktree)
    )
