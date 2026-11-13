"""A batch dispatch decodes the ledger index once for the whole batch.

Every entry of a batch reads the same project's ledger, to decide what is
already promoted, what a section still waits on and what its lane has cost.
Left to itself each entry rebuilt and decoded the whole index again, so a
batch's cost grew with both its entries and its history: an eight-entry review
batch at a live row count spent about 21 percent of its profile in
``ledger.load``, one decode per entry.

A batch dispatch now opens one reuse region around the whole batch, so the
index decoded for the first entry answers every later entry of the same read
shape. This test builds a crew home whose ledger holds 200 rows, dispatches 10
entries across three plans, and counts the decodes the batch costs.

The negative control is the base revision's behaviour: with the region removed
each entry decodes the index for itself, and the count grows with the entries.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

import pytest

from reckon import crew_dispatch_commands, ledger

TREE = Path(__file__).resolve().parents[1]
#: The revision the change under test is measured against, and the code the
#: parity arm runs the same entries through.
BASE_REVISION = "2269d5003c3c58f265de4e260e687fef1ef87106"

PROJECT = "proj"
SESSION = "coordinator-batch"
PLANS = ("plan-a", "plan-b", "plan-c")
SECTIONS = ("s1", "s2", "s3", "s4")
#: A section every plan waits on and no entry dispatches, declared done. A wait
#: that resolves onto a done section asks the ledger whether that section's run
#: has landed, which is the per-entry ledger read a batch pays for.
DONE_SECTION = "s5"
ROW_COUNT = 200

#: Ten entries over three plans: every section of the first, and three of each
#: of the others. The count is the done-when's batch size.
ENTRIES = [{"plan": PLANS[0], "section": section} for section in SECTIONS] + [
    {"plan": slug, "section": section} for slug in PLANS[1:] for section in SECTIONS[:3]
]


def _config() -> dict:
    return {
        "default_backend": "clive",
        "local_backend": "clive",
        "backends": {
            "clive": {
                "launch": "cli",
                "command": "codex",
                "model": "local-model",
                "spec_level": "exact",
                "sandbox": "worktree-full",
                "time_budget": "25m",
            }
        },
        "roles": {"implement": {}},
        "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
    }


def _plan(slug: str) -> str:
    quote = '"'
    declarations = json.dumps(
        dict.fromkeys(SECTIONS, "implementable") | {DONE_SECTION: "done"}
    )
    depends_on = json.dumps(
        {section: [f"{PLANS[1]}#{DONE_SECTION}"] for section in SECTIONS}
    )
    body = "".join(
        f"<h2 id={quote}{section}{quote}>§{section[1:]}</h2>"
        f"<p>Build {slug} {section}.</p>"
        f"<p>Write paths: <code>mod_{slug.replace('-', '_')}.py</code>.</p>"
        f"<p><strong>Done when</strong> the {slug} {section} check passes.</p>"
        for section in SECTIONS
    )
    body += (
        f"<h2 id={quote}{DONE_SECTION}{quote}>§{DONE_SECTION[1:]}</h2>"
        f"<p>The shared section every plan waits on.</p>"
    )
    return (
        "<!doctype html>\n"
        f"<meta name={quote}docs-project{quote} content={quote}{PROJECT}{quote}>\n"
        f"<meta name={quote}reckon-type{quote} content={quote}plan{quote}>\n"
        f"<meta name={quote}plan-slug{quote} content={quote}{slug}{quote}>\n"
        f"<meta name={quote}plan-status{quote} content={quote}active{quote}>\n"
        f"<meta name={quote}plan-section-declarations{quote} content={quote}"
        f"{declarations.replace(quote, '&quot;')}{quote}>\n"
        f"<meta name={quote}plan-section-depends-on{quote} content={quote}"
        f"{depends_on.replace(quote, '&quot;')}{quote}>\n"
        f"{body}\n"
    )


def _ledger_row(position: int) -> dict:
    """One completed run row, shaped like a promoted implement run."""
    return {
        "run_id": f"r-20260101T{position // 3600 % 24:02d}{position // 60 % 60:02d}"
        f"{position % 60:02d}000000-fixture-{position:04d}",
        "project": PROJECT,
        "node": f"fixture-node-{position:04d}",
        "plan": PLANS[position % len(PLANS)],
        "section": SECTIONS[position % len(SECTIONS)],
        "role": "implement",
        "gate": "passed",
        "status": "complete",
        "dispatched_at": "2026-01-01T00:00:00Z",
        "completed_at": "2026-01-01T00:05:00Z",
        "wall_seconds": 300.0,
    }


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True)


def _seed_tree(root: Path, config_home: Path) -> Path:
    """A checkout carrying three plans and a ledger of ``ROW_COUNT`` rows."""
    state = root / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n",
        encoding="utf-8",
    )
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True)
    for slug in PLANS:
        (plans / f"{slug}.html").write_text(_plan(slug), encoding="utf-8")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "worker@example.invalid")
    _git(root, "config", "user.name", "Worker")
    _git(root, "add", "--", "docs")
    _git(root, "commit", "-q", "-m", "chore: seed checkout")
    # The config-home state root resolves to the checkout's own state, which is
    # the symlinked shape a real crew home has: a read with no root and a read
    # naming the checkout then reach the same ledger file.
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    ledger.write(
        PROJECT,
        {
            "members": [],
            "runs": [_ledger_row(position) for position in range(ROW_COUNT)],
            "holds": [],
        },
        0,
        root=root,
    )
    rows, _version = ledger.load(PROJECT, root=root)
    assert len(rows["runs"]) == ROW_COUNT
    return root


def _home(tmp_path: Path, name: str, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / name / "config"
    config_home.mkdir(parents=True)
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv(
        "RECKON_STATE_ROOT", str(tmp_path / name / "repo" / "docs" / "state")
    )
    monkeypatch.delenv("RECKON_RUN_ID", raising=False)
    return config_home


def _batch_file(tmp_path: Path) -> Path:
    entries = [
        {
            "project": PROJECT,
            "plan": entry["plan"],
            "section": entry["section"],
            "node": f"{entry['plan']}-{entry['section']}",
            "goal": f"build {entry['plan']} {entry['section']}",
            "time_budget": "20m",
            "spec_level": "exact",
        }
        for entry in ENTRIES
    ]
    path = tmp_path / "batch.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


class _Counter:
    """Decode counts for the two functions that decode a stored index."""

    def __init__(self) -> None:
        self.payload = 0
        self.payload_keys: list[tuple] = []
        self.last_good = 0

    def install(self):
        original_payload = ledger._index_payload
        original_last_good = ledger._last_good_index_data

        def counting_payload(connection, meta, **options):
            database = connection.execute("PRAGMA database_list").fetchone()[2]
            self.payload += 1
            self.payload_keys.append(
                (
                    str(database),
                    bool(options.get("headers_only")),
                    bool(options.get("picker_only")),
                    options.get("activity_since"),
                )
            )
            return original_payload(connection, meta, **options)

        def counting_last_good(*args, **kwargs):
            self.last_good += 1
            return original_last_good(*args, **kwargs)

        return mock.patch.multiple(
            ledger,
            _index_payload=counting_payload,
            _last_good_index_data=counting_last_good,
        )


def _dispatch(repository: Path, batch: Path, *, reuse: bool = True) -> dict:
    """Dispatch the batch through the CLI command's batch path."""
    region = ledger.index_reuse() if reuse else nullcontext()
    result: dict = {}

    def emit(payload, _pretty):
        result.update(payload)

    with (
        mock.patch.object(
            crew_dispatch_commands,
            "_dispatch_resolved_flight",
            return_value=json.loads(json.dumps(_config())),
        ),
        mock.patch.object(crew_dispatch_commands, "_emit_crew_result", emit),
        mock.patch.object(ledger, "index_reuse", return_value=region),
    ):
        crew_dispatch_commands._queue_dispatch_batch(
            batch,
            session=SESSION,
            repo=str(repository),
            checkout_path=repository,
            overrides={},
            launch_kind="cli",
            pretty=False,
        )
    return result


def _stable(results: list[dict]) -> list[dict]:
    """The per-entry outcomes a batch writes, without the minted run ids."""
    keys = ("index", "node", "project", "plan", "section", "status", "waits")
    return [
        {key: row.get(key) for key in keys}
        for row in sorted(results, key=lambda row: int(row["index"]))
    ]


def test_a_batch_decodes_the_index_at_most_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ten entries over three plans cost one index decode, not one per entry."""
    config_home = _home(tmp_path, "head", monkeypatch)
    repository = _seed_tree(tmp_path / "head" / "repo", config_home)
    batch = _batch_file(tmp_path)
    counter = _Counter()

    with counter.install():
        result = _dispatch(repository, batch)

    assert result["refused"] == 0, result.get("results")
    assert result["queued"] == len(ENTRIES)
    assert counter.payload <= 1, (
        "the whole batch decodes the stored index at most once: "
        f"{counter.payload} decodes over {len(ENTRIES)} entries, at "
        f"{counter.payload_keys}"
    )
    assert counter.last_good <= 1, (
        "a batch that never falls back to the last good index decodes it at "
        f"most once: {counter.last_good} last-good decodes"
    )


def test_the_batch_writes_unchanged_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Serving every entry from one decode changes the cost, not the outcome."""
    shared_home = _home(tmp_path, "shared", monkeypatch)
    shared_repo = _seed_tree(tmp_path / "shared" / "repo", shared_home)
    batch = _batch_file(tmp_path)
    shared = _dispatch(shared_repo, batch)

    isolated_home = _home(tmp_path, "isolated", monkeypatch)
    isolated_repo = _seed_tree(tmp_path / "isolated" / "repo", isolated_home)
    # The base revision's behaviour: no region, so each entry decodes afresh.
    base = _dispatch(isolated_repo, _batch_file(tmp_path), reuse=False)

    assert shared["ok"] == base["ok"]
    assert shared["queued"] == base["queued"]
    assert shared["refused"] == base["refused"]
    assert _stable(shared["results"]) == _stable(base["results"])


#: Dispatches one batch through whichever revision of reckon the interpreter
#: is pointed at, so the same entries can be run over two revisions of the
#: code and their outcomes compared. The batch and the checkout come from the
#: caller, so the only difference between the arms is the code on the path.
_RUNNER = """
import json
import sys
from unittest import mock

import reckon.cli  # noqa: F401  (registers the command tree first)
from reckon import crew_dispatch_commands

batch, repository, session, config = sys.argv[1:5]
result = {}


def emit(payload, _pretty):
    result.update(payload)


with (
    mock.patch.object(
        crew_dispatch_commands,
        "_dispatch_resolved_flight",
        return_value=json.loads(config),
    ),
    mock.patch.object(crew_dispatch_commands, "_emit_crew_result", emit),
):
    crew_dispatch_commands._queue_dispatch_batch(
        batch,
        session=session,
        repo=repository,
        checkout_path=repository,
        overrides={},
        launch_kind="cli",
        pretty=False,
    )
print("RESULT=" + json.dumps(result))
"""


def _extract_revision(target: Path, revision: str = BASE_REVISION) -> Path:
    """The files at ``revision``, extracted from this checkout's own history."""
    archived = subprocess.run(
        ["git", "-C", str(TREE), "archive", revision],
        check=True,
        capture_output=True,
    )
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archived.stdout), mode="r|") as archive:
        archive.extractall(target, filter="data")
    return target


def _run_over_tree(tree: Path, repository: Path, batch: Path, workspace: Path) -> dict:
    """Dispatch ``batch`` using the code at ``tree``, in its own interpreter."""
    script = workspace / "runner.py"
    script.write_text(_RUNNER, encoding="utf-8")
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"RECKON_RUN_ID", "PYTHONPATH", "RECKON_STATE_ROOT"}
    }
    environment["PYTHONPATH"] = str(tree)
    environment["RECKON_HOME"] = str(repository.parent / "config")
    environment["RECKON_STATE_ROOT"] = str(repository / "docs" / "state")
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            str(batch),
            str(repository),
            SESSION,
            json.dumps(_config()),
        ],
        cwd=str(tree),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    marker = [
        line for line in completed.stdout.splitlines() if line.startswith("RESULT=")
    ]
    assert marker, completed.stdout[-2000:]
    return json.loads(marker[-1][len("RESULT=") :])


def test_the_base_revision_writes_the_same_per_entry_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shared decode changes the batch's cost, not what it queues.

    The comparison is against the base revision's code, run over the same
    entries and an identically seeded home, not against this revision with its
    region switched off.
    """
    head_home = _home(tmp_path, "head-code", monkeypatch)
    head_repo = _seed_tree(tmp_path / "head-code" / "repo", head_home)
    head_batch = _batch_file(tmp_path / "head-code")

    base_home = _home(tmp_path, "base-code", monkeypatch)
    base_repo = _seed_tree(tmp_path / "base-code" / "repo", base_home)
    base_batch = _batch_file(tmp_path / "base-code")

    base_tree = _extract_revision(tmp_path / "base-tree")
    head = _run_over_tree(TREE, head_repo, head_batch, tmp_path)
    base = _run_over_tree(base_tree, base_repo, base_batch, tmp_path)

    assert head["queued"] == base["queued"] == len(ENTRIES), (head, base)
    assert head["refused"] == base["refused"] == 0, (head, base)
    assert _stable(head["results"]) == _stable(base["results"])


def test_the_negative_control_decodes_once_per_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the region removed the decode count grows with the entries.

    This is the base revision's behaviour, and it is what the at-most-once
    assertion above fails against when the region is taken out of the batch.
    """
    config_home = _home(tmp_path, "control", monkeypatch)
    repository = _seed_tree(tmp_path / "control" / "repo", config_home)
    batch = _batch_file(tmp_path)
    counter = _Counter()

    with counter.install():
        result = _dispatch(repository, batch, reuse=False)

    assert result["queued"] == len(ENTRIES)
    assert counter.payload >= len(ENTRIES), (
        "without the region each entry decodes the index for itself: "
        f"{counter.payload} decodes over {len(ENTRIES)} entries"
    )
