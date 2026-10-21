"""A figure review's node id resolves to the run it reviews.

A figure review is filed under its own rubric, so its node id carries that
rubric after the head it read. Every reader of a review node id recovers the
reviewed source through one rule owned by the round-id grammar, so the rubric
suffix has to be part of that rule rather than appended beside it: an id whose
tail the reader cannot parse names no run, and admission then finds no record
directory to grant the review's write paths.

These cases hold the rule and its inverses, the mint that routes through it,
the admission grant that reads it back, and the dry-run figure dispatch an
image-declaring lane composes. The repository, crew home and flight all live in
the test's temporary directory, and nothing outside it is read.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon import crew, flight
from reckon.crew import recovery_review_dispatch as dispatch_module
from reckon.crew import recovery_review_subject as review_subject
from reckon.crew import review as review_module
from reckon.crew import runs
from reckon.crew.dispatch_admission import _reviewed_run_id_for_node
from reckon.crew.recovery_vocabulary import REVIEW_NODE_PREFIX
from reckon.crew.repair import (
    round_node_head_suffix,
    round_node_id,
    round_node_source,
)

PROJECT = "sample"
RUN_ID = "r-figure-source"
SOURCE_NODE = "cold-test-detection-margins"
HEAD = "4c65b26cd6d9aabbccddeeff00112233445566"
FIGURE = "docs/figures/topic/panel.png"

# A one-pixel PNG, so the figure path is a real image the change detector finds.
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6360000002000100ffff03000006000557bfabd4000000"
    "0049454e44ae426082"
)

CONFIG = {
    "default_backend": "clive",
    "local_backend": "clive",
    "backends": {
        "clive": {
            "launch": "cli",
            "command": "clive",
            "model": "local-model",
            "effort": "medium",
            "sandbox": "read-only",
            "time_budget": "20m",
            "execution_capable": True,
            "input_modalities": ["text", "image"],
        }
    },
    "roles": {"review": {"backend": "clive", "execution_capable": True}},
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _figure_node_id(source: str = SOURCE_NODE, head: str = HEAD) -> str:
    return round_node_id(
        REVIEW_NODE_PREFIX,
        source,
        head,
        rubric=review_module.FIGURE_REVIEW_RUBRIC,
    )


def test_a_figure_node_id_resolves_to_its_source_and_head() -> None:
    node_id = _figure_node_id()
    assert node_id == f"{REVIEW_NODE_PREFIX}{SOURCE_NODE}-{HEAD[:12]}-figure"
    assert round_node_source(node_id, REVIEW_NODE_PREFIX) == SOURCE_NODE
    assert round_node_head_suffix(node_id, REVIEW_NODE_PREFIX) == HEAD[:12]


def test_a_text_round_and_a_bare_id_resolve_as_before() -> None:
    text_id = round_node_id(REVIEW_NODE_PREFIX, SOURCE_NODE, HEAD)
    assert round_node_source(text_id, REVIEW_NODE_PREFIX) == SOURCE_NODE
    assert round_node_head_suffix(text_id, REVIEW_NODE_PREFIX) == HEAD[:12]

    bare = f"{REVIEW_NODE_PREFIX}{SOURCE_NODE}"
    assert round_node_source(bare, REVIEW_NODE_PREFIX) == SOURCE_NODE
    assert round_node_head_suffix(bare, REVIEW_NODE_PREFIX) == ""


def test_a_source_ending_in_the_rubric_word_resolves_to_itself() -> None:
    # A source whose own name ends in the rubric word, with no head written
    # after it, is a bare round and is read whole rather than mistaken for a
    # suffixed one.
    bare = f"{REVIEW_NODE_PREFIX}plot-figure"
    assert round_node_source(bare, REVIEW_NODE_PREFIX) == "plot-figure"
    assert round_node_head_suffix(bare, REVIEW_NODE_PREFIX) == ""

    # The same name with a head after it is a head-keyed round whose source is
    # the whole name.
    keyed = round_node_id(REVIEW_NODE_PREFIX, "plot-figure", HEAD)
    assert round_node_source(keyed, REVIEW_NODE_PREFIX) == "plot-figure"
    assert round_node_head_suffix(keyed, REVIEW_NODE_PREFIX) == HEAD[:12]


def test_the_mint_routes_the_figure_rubric_through_the_round_rule(
    project: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}
    real = review_subject.round_node_id

    def spy(prefix, source, head, **kwargs):
        seen["prefix"] = prefix
        seen["source"] = source
        seen["head"] = head
        seen["rubric"] = kwargs.get("rubric")
        return real(prefix, source, head, **kwargs)

    monkeypatch.setattr(review_subject, "round_node_id", spy)
    fields = review_subject._review_dispatch_fields(
        project["record"],
        delta=None,
        rubric=review_module.FIGURE_REVIEW_RUBRIC,
        images=[FIGURE],
    )

    assert seen["rubric"] == review_module.FIGURE_REVIEW_RUBRIC
    assert fields["node_id"] == _figure_node_id(SOURCE_NODE, fields["head"])


def test_admission_resolves_a_minted_figure_id_to_its_reviewed_run(
    project: dict,
) -> None:
    node_id = _figure_node_id()
    assert _reviewed_run_id_for_node(node_id) == RUN_ID


def test_the_dry_run_figure_dispatch_composes_without_a_scoped_refusal(
    project: dict,
) -> None:
    report = dispatch_module.dispatch_review_for_run(
        project["record"], config=CONFIG, dry_run=True
    )

    figure = report["figure_review"]
    assert figure["dry_run"] is True, figure
    scoped = [
        finding
        for finding in figure["validation"]["findings"]
        if finding["property"] == "scoped"
    ]
    assert scoped == [], figure["validation"]["findings"]
    assert figure["refused"] is False, figure["validation"]["findings"]

    record_directory = review_module.run_review_record_directory(runs.run_dir(RUN_ID))
    assert len(figure["write_paths"]) == 2, figure["write_paths"]
    for declared in figure["write_paths"]:
        assert Path(declared).parent == record_directory, declared


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A git repository, a crew home and a scoring run, all inside the test tree."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    # A restricted tier grants the system temp directory writable, so a crew
    # home the system temp directory happens to contain would make every
    # declared path reachable and the scope check a no-op. Point the system
    # temp directory at its own tree so the crew home is a distinct place, as
    # it is on a real host.
    system_temp = tmp_path / "system-temp"
    system_temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(system_temp))

    repo = tmp_path / "repo"
    repo.mkdir()
    docs = repo / "docs"
    docs.mkdir()
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(repo, *args)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-q", "-m", "chore: seed")
    base_sha = _git(repo, "rev-parse", "HEAD")

    figure_path = repo / FIGURE
    figure_path.parent.mkdir(parents=True)
    figure_path.write_bytes(PNG_BYTES)
    _git(repo, "add", FIGURE)
    _git(repo, "commit", "-q", "-m", "feat: add figure")
    head_sha = _git(repo, "rev-parse", "HEAD")

    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    manifest = home / "manifests" / f"{RUN_ID}.md"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        f"node: {SOURCE_NODE}\nstatus: complete\ncommits: {head_sha}\n",
        encoding="utf-8",
    )
    record = {
        "run_id": RUN_ID,
        "project": PROJECT,
        "session": "coordinator",
        "process_alive": False,
        "repo": str(repo),
        "worktree": str(repo),
        "base_sha": base_sha,
        "backend": "clive",
        "launch": "cli",
        "phase": "starting",
        "node": {"id": SOURCE_NODE, "plan": "", "section": ""},
        "manifest_path": str(manifest),
    }
    monkeypatch.setattr(
        flight, "resolve", lambda **kwargs: SimpleNamespace(config=CONFIG)
    )
    # The run is live and awaiting review, which is the state a figure review is
    # composed for: the reviewed run is recovered from the fleet by its node id.
    crew._write_json(crew.pointer_path(RUN_ID), record)
    return {"home": home, "repo": repo, "record": record, "head_sha": head_sha}
