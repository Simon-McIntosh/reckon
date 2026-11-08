"""A batch entry is measured against its lane's window before it is queued.

A request whose own repository inputs cannot fit the lane it is resolved for
fits in the queue and is refused at the first launch that tries it, hours
later, on a pointer nobody is watching. The batch measures the same estimate a
launch charges a node against and refuses the entry in its own row, naming the
estimate and the window, before a pointer exists.

A path declared a regenerate path is not read, so it pays the flat reference
cost rather than the size of the record it replaces. That is what distinguishes
a node rewriting a large generated file from one loading it, and it is the case
an estimate that charged every declared write path by size got wrong.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

import pytest

from reckon.crew import dispatch_batch, runs
from reckon.crew.routing import _CONTEXT_BYTES_PER_TOKEN

PROJECT = "proj"
SESSION = "coordinator-a"
PLAN = "plan-a"
DONE_WHEN = (
    "pytest -q tests/test_a_batch_entry_fits_its_window_when_queued.py reports passed."
)
NODE = "batch-fits"

#: A window that leaves room for the standing instruction chain every request
#: is charged (the launch context floor) and still misses one large file.
WINDOW_TOKENS = 200_000
#: The oversized file: its byte count at the census rate puts it well past the
#: window on its own, so no rounding can bring it back inside.
OVERSIZED_BYTES = 1_600_000
INSIDE_BYTES = 40_000

CONFIG = {
    "default_backend": "stub-lane",
    "local_backend": "stub-lane",
    "backends": {
        "stub-lane": {
            "launch": "cli",
            "command": "codex",
            "model": "local-model",
            "effort": "medium",
            "sandbox": "worktree-full",
            "time_budget": "20m",
            "usable_input_window": WINDOW_TOKENS,
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "2h", "needs_help_after_failures": 2},
}


def _plan_document(payload_path: str) -> str:
    quote = '"'
    prose = (
        "<p>Measure a batch entry against its lane's window.</p>"
        f"<p>Write paths: <code>{payload_path}</code>.</p>"
        "<p><strong>Done when</strong> pytest reports the batch window check "
        "passing.</p>"
    )
    return (
        "<!doctype html>\n"
        f"<meta name={quote}docs-project{quote} content={quote}{PROJECT}{quote}>\n"
        f"<meta name={quote}reckon-type{quote} content={quote}plan{quote}>\n"
        f"<meta name={quote}plan-slug{quote} content={quote}{PLAN}{quote}>\n"
        f"<meta name={quote}plan-status{quote} content={quote}active{quote}>\n"
        f"<h2 id={quote}s1{quote}>§1</h2>{prose}\n"
    )


def _entry(payload_path: str, **overrides: object) -> dict:
    entry: dict = {
        "project": PROJECT,
        "plan": PLAN,
        "section": "s1",
        "node": NODE,
        "goal": "Measure a batch entry against its lane's window.",
        "time_budget": "20m",
        "spec_level": "exact",
        "done_when": DONE_WHEN,
        "write_paths": [payload_path],
        "negative_control": "none: this request writes no check",
    }
    entry.update(overrides)
    return entry


def _run(repository: Path, *arguments: str) -> None:
    import subprocess

    subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True)


@pytest.fixture()
def batch_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    # The standing instruction chain this host's home carries is measured into
    # every estimate, so the test's home is empty: the window is being judged
    # against the entry's own repository inputs and nothing else.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    repository = tmp_path / "checkout"
    state = repository / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n",
        encoding="utf-8",
    )
    plans = repository / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / f"{PLAN}.html").write_text(
        _plan_document("src/payload.py"), encoding="utf-8"
    )
    payload = repository / "src" / "payload.py"
    payload.parent.mkdir(parents=True)
    payload.write_bytes(b"x" * OVERSIZED_BYTES)
    _run(repository, "init", "-q", "-b", "main")
    _run(repository, "config", "user.email", "worker@example.invalid")
    _run(repository, "config", "user.name", "Worker")
    _run(repository, "add", "--", "docs", "src")
    _run(repository, "commit", "-q", "-m", "chore: seed checkout")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    return repository


def _queue(repository: Path, entries: list[dict]) -> dict:
    return dispatch_batch.queue_batch(
        entries,
        session=SESSION,
        config_for=lambda _project: json.loads(json.dumps(CONFIG)),
        repo_root=repository,
    )


def _pointers() -> list[str]:
    """Every pointer the queue wrote, read from the crew home it resolved."""
    live = runs.live_dir()
    if not live.is_dir():
        return []
    return sorted(path.name for path in live.glob("*.json"))


def test_an_entry_larger_than_its_window_is_refused_in_its_row(
    batch_repo: Path,
) -> None:
    before = _pointers()
    estimate = math.ceil(OVERSIZED_BYTES / _CONTEXT_BYTES_PER_TOKEN)

    result = _queue(batch_repo, [_entry("src/payload.py")])

    assert result["queued"] == 0, result
    assert result["refused"] == 1, result
    row = result["results"][0]
    assert row["status"] == "refused", row
    reason = str(row["reason"])
    reported = re.search(r"estimated (\d+) usable input tokens", reason)
    assert reported is not None, reason
    assert int(reported.group(1)) > WINDOW_TOKENS, "the estimate exceeds the window"
    assert f"against a {WINDOW_TOKENS} token window" in reason, reason
    assert f"({estimate} tokens)" in reason, "the dominant term is named"
    assert "src/payload.py" in reason, "the dominant term is named"
    assert _pointers() == before, "a refused entry writes no pointer"


def test_an_entry_whose_largest_path_is_regenerated_is_queued(
    batch_repo: Path,
) -> None:
    payload = batch_repo / "src" / "payload.py"

    result = _queue(
        batch_repo, [_entry("src/payload.py", regenerate_paths=["src/payload.py"])]
    )

    assert result["refused"] == 0, result
    assert result["results"][0]["status"] in {"queued", "awaiting-upstream"}, result
    assert payload.stat().st_size == OVERSIZED_BYTES, "the record is still large"


def test_an_entry_inside_the_window_is_queued(batch_repo: Path) -> None:
    small = batch_repo / "src" / "small.py"
    small.write_bytes(b"x" * INSIDE_BYTES)

    result = _queue(batch_repo, [_entry("src/small.py")])

    assert result["refused"] == 0, result
    row = result["results"][0]
    assert row["status"] in {"queued", "awaiting-upstream"}, row
    assert row.get("run_id"), "a queued entry is stored under its own run id"
