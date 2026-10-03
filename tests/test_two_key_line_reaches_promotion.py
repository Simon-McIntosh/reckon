"""A manifest line carrying two fields is refused at promotion, naming the line.

The reader refuses such a line where it is introduced; this case carries the
same manifest through the promotion path, so a run delivered in the one-line
form is stopped there rather than promoting on a ``changed_paths`` the reader
silently emptied — the half that disarms the changed-paths-without-commit
guard. The separate-line form is the control: the same run promotes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon import crew
from reckon.crew.runs import _write_json, pointer_path
from tests.conftest import EXECUTABLE_GATE_COMMAND
from tests.test_review_gate_coverage import (  # noqa: F401 - registered as a fixture
    PROJECT,
    _seed_candidate,
    repository,
)

CHANGED_PATH = "candidate.txt"
ONE_LINE_RUN = "r-20261003T093000000000-two-key-line"
SEPARATE_LINES_RUN = "r-20261003T093100000000-separate-lines"


def _write_delivered_run(
    root: Path,
    tmp_path: Path,
    run_id: str,
    *,
    commit: str,
    base: str,
    one_line: bool,
) -> Path:
    """Deliver a run whose manifest names its commit and changed path.

    In the one-line form the two fields are joined by a semicolon on the third
    line — the shape this section measured, which the tolerant reader would
    otherwise take whole as the ``commits`` value, leaving ``changed_paths``
    absent and so empty.
    """
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    if one_line:
        fields = f"commits: {commit}; changed_paths: {CHANGED_PATH}\n"
    else:
        fields = f"commits: {commit}\nchanged_paths: {CHANGED_PATH}\n"
    manifest.write_text(
        f"node: node-a\nstatus: complete\n{fields}tests: {EXECUTABLE_GATE_COMMAND}\n",
        encoding="utf-8",
    )
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(root),
            "worktree": str(root),
            "base_sha": base,
            "launch": "in-harness",
            "role": "documentation",
            "backend": "native",
            "created_at": "2026-10-03T09:30:00Z",
            "manifest_path": str(manifest),
            "node": {
                "id": "node-a",
                "plan": "plan-a",
                "section": "two-key-line",
                "time_budget": "25m",
                "write_paths": [CHANGED_PATH],
            },
        },
    )
    return manifest


def test_a_two_key_manifest_line_is_refused_at_promotion(
    repository: Path,  # noqa: F811 - imported fixture, requested by name
    tmp_path: Path,
) -> None:
    base, commit = _seed_candidate(repository)
    _write_delivered_run(
        repository, tmp_path, ONE_LINE_RUN, commit=commit, base=base, one_line=True
    )

    with pytest.raises(crew.CrewError) as refusal:
        crew.complete(ONE_LINE_RUN, gate="passed", commits=[commit], root=repository)

    message = str(refusal.value)
    assert "line 3" in message
    assert f"commits: {commit}; changed_paths: {CHANGED_PATH}" in message
    assert "'commits:' is followed by 'changed_paths:'" in message
    assert pointer_path(ONE_LINE_RUN).exists()


def test_the_separate_line_form_still_promotes(
    repository: Path,  # noqa: F811 - imported fixture, requested by name
    tmp_path: Path,
) -> None:
    base, commit = _seed_candidate(repository)
    _write_delivered_run(
        repository,
        tmp_path,
        SEPARATE_LINES_RUN,
        commit=commit,
        base=base,
        one_line=False,
    )

    crew.complete(SEPARATE_LINES_RUN, gate="passed", commits=[commit], root=repository)

    assert not pointer_path(SEPARATE_LINES_RUN).exists()
