"""An admission pass resolves each scope path once, and nothing outside a pass.

Every launch compares its scope with every live pointer's declared paths, and
resolving each one walks the path on shared storage: about an eighth of each
launch in a profile of the admit service on 14 November. Within one pass the
same paths are resolved for launch after launch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon.crew import runs
from reckon.crew.admission import service


@pytest.fixture
def resolves(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    real = Path.resolve

    def counting(self, *args, **kwargs):
        seen.append(str(self))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", counting)
    return seen


def test_a_scope_resolves_each_path_once(tmp_path, resolves):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    with runs.resolution_scope():
        for _ in range(5):
            assert runs._repository_relative_scope("pkg/a.py", repo) == "pkg/a.py"
    assert resolves.count(str(repo / "pkg/a.py")) == 1


def test_outside_a_scope_every_call_resolves(tmp_path, resolves):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    for _ in range(3):
        runs._repository_relative_scope("pkg/a.py", repo)
    assert resolves.count(str(repo / "pkg/a.py")) == 3


def test_a_link_made_after_a_scope_is_read_by_the_next_one(tmp_path):
    repo = tmp_path / "repo"
    (repo / "real").mkdir(parents=True)
    with runs.resolution_scope():
        assert runs._repository_relative_scope("alias/a.py", repo) == "alias/a.py"
    (repo / "alias").symlink_to(repo / "real")
    with runs.resolution_scope():
        assert runs._repository_relative_scope("alias/a.py", repo) == "real/a.py"


def test_an_admission_pass_runs_inside_a_scope(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "crew"))
    opened: list[bool] = []

    def admit_once(**_options):
        opened.append(runs._RESOLVED_PATHS.get() is not None)
        return {"verdicts": [], "launched": []}

    monkeypatch.setattr(service._selection, "admit_once", admit_once)
    service.admit_until_stopped(
        once=True,
        lander=lambda: {"landed": [], "skipped": [], "obligations": []},
    )
    assert opened == [True]
    assert runs._RESOLVED_PATHS.get() is None
