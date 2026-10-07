"""The import's dry run lists the records it would commit, beside their count.

The actionable set is a recognised record whose committed path is absent. The
dry run must name each such record — by review run id, subject and plan version —
and count them, from the same decision ``--write`` acts on, so a reader of the
dry run sees exactly the written set and a censusing node does not have to call
the script's private functions. A record already committed is listed by the
inventory (which reports the staging entry) but not in the actionable listing.

These cases drive the script against a synthesised config home and checkout: a
store holding two plan-review records, one whose committed path exists and one
whose does not, must list only the second, and the listing must survive the
round trip to ``--write``.
"""

from __future__ import annotations

import importlib.util
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import pytest

PROJECT = "demo-store"

ACTIONABLE_RUN = "r-actionable-review"
COMMITTED_RUN = "r-committed-review"


def _load_script():
    """Import ``scripts/import_host_reviews.py`` by path, as the CLI runs it."""
    script = Path(__file__).resolve().parents[1] / "scripts" / "import_host_reviews.py"
    spec = importlib.util.spec_from_file_location("import_host_reviews", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A synthesised config home and checkout, isolated from the real ones."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    (tmp_path / "config").mkdir()
    store = tmp_path / "config" / "crew" / "reviews" / PROJECT
    store.mkdir(parents=True)
    repo = tmp_path / "repo"
    (repo / "docs" / "state" / PROJECT).mkdir(parents=True)
    return {"tmp": tmp_path, "store": store, "repo": repo}


def _plan_record(store: Path, name: str, run_id: str, slug: str, version: int) -> Path:
    path = store / name
    path.write_text(
        json.dumps(
            {
                "project": PROJECT,
                "review_run_id": run_id,
                "plan_slug": slug,
                "plan_version": version,
                "status": "ready",
                "findings": [],
            }
        ),
        encoding="utf-8",
    )
    return path


def _commit_plan_record(repo: Path, slug: str, run_id: str) -> Path:
    """Write the committed path a plan review resolves, so it is already present."""
    path = (
        repo / "docs" / "state" / PROJECT / "reviews" / "plan" / slug / f"{run_id}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"project": PROJECT, "plan_slug": slug, "review_run_id": run_id}),
        encoding="utf-8",
    )
    return path


def _run_cli(module, argv: list[str]) -> tuple[int, str]:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = module.main(argv)
    return code, buffer.getvalue()


def test_dry_run_lists_the_records_the_write_path_would_commit(harness) -> None:
    module = _load_script()
    store = harness["store"]
    repo = harness["repo"]
    # One record whose committed path is absent, one already committed. The two
    # differ only in whether the committed file exists, so the listing isolates
    # the actionable decision rather than any other property of the records.
    _plan_record(store, "actionable.json", ACTIONABLE_RUN, "demo", 4)
    _plan_record(store, "committed.json", COMMITTED_RUN, "demo-committed", 2)
    _commit_plan_record(repo, "demo-committed", COMMITTED_RUN)

    code, out = _run_cli(module, ["--project", PROJECT, "--root", str(repo)])

    assert code == 0
    # Both are recognised records: the inventory counts them.
    assert "recognised records: 2" in out
    # The actionable listing names the absent record by its review run id, its
    # subject and its plan version, beside their count.
    assert "records to import: 1" in out
    assert ACTIONABLE_RUN in out
    assert "plan demo v4" in out
    # The already-committed record is not in the actionable listing. Its id is
    # nowhere else in the output: the inventory reports the staging path.
    assert COMMITTED_RUN not in out
    assert "dry run: nothing written" in out
    # The dry run wrote nothing.
    assert not (
        repo / "docs" / "state" / PROJECT / "reviews" / "plan" / "demo"
    ).exists()


def test_the_listed_set_is_the_written_set(harness) -> None:
    module = _load_script()
    store = harness["store"]
    repo = harness["repo"]
    _plan_record(store, "actionable.json", ACTIONABLE_RUN, "demo", 4)
    _plan_record(store, "committed.json", COMMITTED_RUN, "demo-committed", 2)
    _commit_plan_record(repo, "demo-committed", COMMITTED_RUN)

    dry_out = _run_cli(module, ["--project", PROJECT, "--root", str(repo)])[1]
    write_code, write_out = _run_cli(
        module, ["--project", PROJECT, "--root", str(repo), "--write"]
    )

    assert write_code == 0
    # The write commits exactly the one the dry run listed.
    assert "imported: 1" in write_out
    assert ACTIONABLE_RUN in dry_out
    committed = (
        repo
        / "docs"
        / "state"
        / PROJECT
        / "reviews"
        / "plan"
        / "demo"
        / f"{ACTIONABLE_RUN}.json"
    )
    assert committed.is_file()
    # The already-committed record was left as it was, not rewritten.
    present = (
        repo
        / "docs"
        / "state"
        / PROJECT
        / "reviews"
        / "plan"
        / "demo-committed"
        / f"{COMMITTED_RUN}.json"
    )
    assert (
        json.loads(present.read_text(encoding="utf-8"))["plan_slug"] == "demo-committed"
    )
