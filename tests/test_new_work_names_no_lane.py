"""A printed new-work remedy names no lane; a continuation keeps its run's lane.

The obligations hook edits the lane a composed remedy names before handing the
command to a coordinator to retype. A remedy that dispatches new work — a
missing review, a new node — names no lane, so the picker chooses one or holds;
a remedy that continues an existing run keeps the lane that run was carried on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon.hooks import coordinator_obligations

# The local lane this host declares, so the previous rewrite of ``--backend``
# into ``--local`` would fire if it were still in place. Declaring one makes the
# assertion below a real check rather than a no-op that passes because there is
# nothing to rewrite.
LOCAL_BACKEND = "worker"
FOREIGN_BACKEND = "codex"


@pytest.fixture()
def local_lane_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "flight.yaml"
    path.write_text(
        "\n".join(
            [
                "version: 1",
                f"default_backend: {LOCAL_BACKEND}",
                f"local_backend: {LOCAL_BACKEND}",
                "backends:",
                f"  {LOCAL_BACKEND}:",
                "    launch: in-harness",
                "    sandbox: worktree-full",
                "    session_reuse: false",
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RECKON_FLIGHT_CONFIG", str(path))
    return path


def test_a_missing_review_names_no_lane_while_a_continuation_keeps_its_own(
    local_lane_config: Path,
) -> None:
    missing_review = (
        "reckon crew dispatch --project P --plan L --role review "
        "--node review-of-x --time-budget 20m --session S "
        f"--backend {FOREIGN_BACKEND}"
    )
    routed = coordinator_obligations.follow_local_lane(missing_review, project="P")
    assert FOREIGN_BACKEND not in routed
    assert "--backend" not in routed
    assert "--local" not in routed
    assert routed.startswith("reckon crew dispatch --project P --plan L")

    resume = f"reckon crew redispatch --run r1 --backend {FOREIGN_BACKEND} --reason x"
    assert coordinator_obligations.follow_local_lane(resume, project="P") == resume


def test_a_resume_that_names_no_lane_is_left_as_composed(
    local_lane_config: Path,
) -> None:
    resume = "reckon crew resume --run r1 --advice continue"
    assert coordinator_obligations.follow_local_lane(resume, project="P") == resume