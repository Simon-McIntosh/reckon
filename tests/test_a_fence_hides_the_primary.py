"""A shadow's launch cannot read its primary's records, proved by running the fence.

A shadow re-runs a promoted node at its original base so the two arms can be
compared, which is only meaningful when the shadow never read the primary's
answer. Three of those channels live in reckon's own stores rather than in the
landed code: the primary's run directory, its review record files, and the
worktree its row retains. A read-only overlay is not enough — the primary's
records would still be *readable*, only not writable — so a shadow's launch
masks them: an empty tmpfs over each directory and ``/dev/null`` over each
review record.

The mask is asserted by *executing* the argument vector ``launch_plan``
composes, never by inspecting its shape. A shape assertion cannot tell a mask
that holds from one that is spelled correctly and does nothing. A stub stands
in for the harness: it lists the primary's run directory, reads each of the
primary's review records, and writes a probe into its own run directory and its
own worktree, recording what happened in the run directory. The test reads that
record back.

Two properties, and the second is what makes the first mean anything:

* the mask holds: the primary's run directory lists empty, every review record
  reads empty, and the shadow's own run directory and worktree are writable;
* the declared negative control drops the masks, and the empty-listing
  assertion then fails. Without it the suite would pass against a fence that
  never masked anything.

Running this file directly reproduces the red log: its first line is the
declared mutation, verbatim, and what follows is the observed non-empty
listing.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from reckon import _backends, ledger
from reckon.crew import dispatch_sessions
from reckon.crew.review import review_path, review_store_root
from reckon.crew.runs import run_dir

PROJECT = "proj"
PRIMARY = "r-20261016T145650209779-xpoint-cell-owner"
SHADOW = "r-20261016T165229949973-xpoint-cell-owner-clive"

# The environment variable the declared mutation is applied through: with it
# set the shadow launch composes no masks, so the empty-listing assertion fails.
DROP_MASKS_ENV = "RECKON_TEST_DROP_SHADOW_MASKS"

NEGATIVE_CONTROL_MUTATION = (
    "the masks dropped for a shadow launch, so the empty-listing assertion must fail"
)

requires_bwrap = pytest.mark.skipif(
    shutil.which("bwrap") is None, reason="bubblewrap is not installed"
)

# Stands in for the harness: exercises the mask and records the outcome. The
# primary's run directory is listed with ``-A`` because the mask overlays an
# empty tmpfs, so a listing that is not empty is exactly the leak under test.
_STUB = """#!/bin/sh
set -u
: > "$FENCE_RESULTS"
printf 'primary-run-dir [' >> "$FENCE_RESULTS"
ls -A "$FENCE_PRIMARY_RUN_DIR" 2>&1 | tr '\\n' ',' >> "$FENCE_RESULTS"
printf ']\\n' >> "$FENCE_RESULTS"
while IFS= read -r f; do
  [ -n "$f" ] || continue
  printf 'review %s [' "$f" >> "$FENCE_RESULTS"
  cat "$f" 2>&1 >> "$FENCE_RESULTS"
  printf ']\\n' >> "$FENCE_RESULTS"
done < "$FENCE_REVIEW_FILE"
if touch "$FENCE_OWN_RUN/.shadow-write-probe" 2>/dev/null; then
  echo "own-run-write-ok" >> "$FENCE_RESULTS"
else
  echo "own-run-write-refused" >> "$FENCE_RESULTS"
fi
if touch "$FENCE_OWN_WORKTREE/.shadow-write-probe" 2>/dev/null; then
  echo "own-worktree-write-ok" >> "$FENCE_RESULTS"
else
  echo "own-worktree-write-refused" >> "$FENCE_RESULTS"
fi
"""


class Shadow:
    """A synthetic home holding a primary's records and a shadow's own roots."""

    def __init__(
        self, root: Path, monkeypatch: pytest.MonkeyPatch | None = None
    ) -> None:
        self.root = root
        self.home = root / "home"
        # The crew home sits under ``~/.config/reckon`` so the fence's protected
        # overlay covers the primary's run directory; the mask must then be the
        # last word on that path rather than the overlay re-exposing it.
        self.reckon_home = self.home / ".config" / "reckon"
        self.reckon_home.mkdir(parents=True)
        if monkeypatch is not None:
            monkeypatch.setenv("RECKON_HOME", str(self.reckon_home))
            monkeypatch.setenv("HOME", str(self.home))
        else:
            os.environ["RECKON_HOME"] = str(self.reckon_home)
            os.environ["HOME"] = str(self.home)
        self.run = run_dir(SHADOW)
        self.run.mkdir(parents=True)
        self.worktree = root / "shadow-worktree"
        self.worktree.mkdir()
        self.stub = root / "stub.sh"
        self.stub.write_text(_STUB)
        self.stub.chmod(0o755)
        # The primary's three surfaces.
        self.primary_run = run_dir(PRIMARY)
        self.primary_run.mkdir(parents=True)
        (self.primary_run / "manifest.md").write_text("status: complete\n")
        (self.primary_run / "stream.jsonl").write_text('{"secret": "primary"}\n')
        self.primary_worktree = root / "primary-worktree"
        self.primary_worktree.mkdir()
        (self.primary_worktree / "answer.txt").write_text("the primary's answer\n")
        store = review_store_root()
        self.review_records: list[Path] = []
        staging = review_path(PROJECT, PRIMARY)
        staging.parent.mkdir(parents=True, exist_ok=True)
        staging.write_text('{"verdict": "primary held"}')
        self.review_records.append(staging)
        sibling = store / PROJECT / f"{PRIMARY}.at-abc1234.json"
        sibling.write_text('{"verdict": "primary held at abc1234"}')
        self.review_records.append(sibling)
        self.results = self.run / "results.txt"
        self.review_file = self.run / "reviews.txt"
        self.primary_row = {
            "run_id": PRIMARY,
            "project": PROJECT,
            "worktree_retention": {"worktree": str(self.primary_worktree)},
        }
        self.lineage = {
            "kind": "shadow",
            "primary_run_id": PRIMARY,
            "worktree_component": "deadbeef0000",
        }

    def masks(self) -> tuple[str, ...]:
        return dispatch_sessions._shadow_mask_paths(
            self.lineage, self.primary_row, PROJECT
        )

    def argv(self, *, drop_masks: bool = False) -> list[str]:
        masks = (
            ()
            if drop_masks
            else dispatch_sessions._shadow_mask_paths(
                self.lineage, self.primary_row, PROJECT
            )
        )
        return _backends.launch_plan(
            backend_name="b",
            backend={
                "launch": "cli",
                "command": str(self.stub),
                "dialect": "claude",
                "sandbox": "worktree-full",
            },
            prompt="p",
            worktree=str(self.worktree),
            manifest_path=str(self.run / "manifest.md"),
            writable_directories=[str(self.run)],
            fence=True,
            fence_home=str(self.home),
            masked_paths=masks,
        ).argv

    def run_fence(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.review_file.write_text(
            "\n".join(str(p) for p in self.review_records) + "\n"
        )
        env = {
            **os.environ,
            "HOME": str(self.home),
            "FENCE_RESULTS": str(self.results),
            "FENCE_PRIMARY_RUN_DIR": str(self.primary_run),
            "FENCE_REVIEW_FILE": str(self.review_file),
            "FENCE_OWN_RUN": str(self.run),
            "FENCE_OWN_WORKTREE": str(self.worktree),
        }
        return subprocess.run(
            argv,
            env=env,
            cwd=str(self.worktree),
            capture_output=True,
            text=True,
            check=False,
        )

    def results_text(self) -> str:
        return self.results.read_text()


@requires_bwrap
def test_a_shadow_launch_cannot_read_its_primary_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The masked surfaces read as absent while the shadow's own roots stay writable."""
    shadow = Shadow(tmp_path, monkeypatch)
    drop = os.environ.get(DROP_MASKS_ENV) == "1"
    argv = shadow.argv(drop_masks=drop)

    proc = shadow.run_fence(argv)
    assert proc.returncode == 0, proc.stderr
    results = shadow.results_text()
    assert results.strip(), "the stub wrote nothing; the fence did not run it"

    # The mask holds: the primary's run directory lists empty and each review
    # record reads empty, while each held real content in the unmasked home.
    assert (shadow.primary_run / "stream.jsonl").read_text().strip()
    assert "primary-run-dir []" in results, results
    for record in shadow.review_records:
        assert record.read_text(encoding="utf-8").strip(), record
        assert f"review {record} []" in results, results

    # The shadow's own roots are untouched by the mask.
    assert "own-run-write-ok" in results, results
    assert "own-worktree-write-ok" in results, results
    assert (shadow.run / ".shadow-write-probe").exists()
    assert (shadow.worktree / ".shadow-write-probe").exists()


def test_a_shadow_resolves_its_primary_surfaces_and_a_non_shadow_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mask set is the primary's surfaces for a shadow and empty otherwise."""
    shadow = Shadow(tmp_path, monkeypatch)
    masks = shadow.masks()
    assert str(shadow.primary_run) in masks
    assert str(shadow.primary_worktree) in masks
    for record in shadow.review_records:
        assert str(record) in masks
    assert dispatch_sessions._shadow_mask_paths(None, shadow.primary_row, PROJECT) == ()
    assert (
        dispatch_sessions._shadow_mask_paths(
            {"kind": "redispatch"}, shadow.primary_row, PROJECT
        )
        == ()
    )
    assert dispatch_sessions._shadow_mask_paths(shadow.lineage, None, PROJECT) == ()


def test_a_non_shadow_launch_composes_no_masks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unpaired, non-shadow launch carries neither a tmpfs nor a /dev/null bind."""
    shadow = Shadow(tmp_path, monkeypatch)
    plan = _backends.launch_plan(
        backend_name="b",
        backend={"launch": "cli", "command": "true", "dialect": "claude"},
        prompt="p",
        worktree=str(shadow.worktree),
        manifest_path=str(shadow.run / "manifest.md"),
        writable_directories=[str(shadow.run)],
        fence=True,
        fence_home=str(shadow.home),
    )
    assert "--tmpfs" not in plan.argv
    assert os.devnull not in plan.argv


def test_a_resume_composes_the_same_masks_as_the_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The resume caller hands ``launch_plan`` the same masks the launch does.

    The shadow's record names its primary, so a resume resolves the primary's
    row from the ledger and passes the same surface set through the one
    resolver the first launch reads. ``launch_plan`` is spied so the crew-layer
    wiring the resume adds is isolated; the composed argv's shape is asserted
    by the bwrap test above.
    """
    from tests.test_crew_recover import _refused_run

    shadow = Shadow(tmp_path, monkeypatch)
    record = _refused_run(tmp_path, SHADOW, observed_end=True)
    record["project"] = PROJECT
    record["lineage"] = shadow.lineage
    from reckon.crew.runs import _write_json, pointer_path

    _write_json(pointer_path(SHADOW), record)
    # The primary's row is committed in the run's own project, as a shadow of a
    # promoted primary finds it.
    row = ledger.build_record(
        run_id=PRIMARY,
        plan="plan-a",
        gate="passed",
        agent={"backend": "alpha"},
        completed_at="2026-10-16T15:00:00Z",
    )
    row["worktree_retention"] = {"worktree": str(shadow.primary_worktree)}
    ledger.append_run(PROJECT, row, allow_create=True)

    captured: dict[str, object] = {}
    real = _backends.launch_plan

    def spy(**kwargs):
        captured["masked_paths"] = tuple(kwargs.get("masked_paths") or ())
        return real(**kwargs)

    monkeypatch.setattr(dispatch_sessions._backends, "launch_plan", spy)

    from_record = dispatch_sessions._shadow_masks_for_record(record)
    assert from_record == shadow.masks()

    # The resume may refuse after it composes the plan; the spy has recorded the
    # masks it was handed by then, which is the wiring under test.
    with contextlib.suppress(Exception):
        dispatch_sessions.resume_plan(SHADOW, "continue", config=None)
    assert captured.get("masked_paths") == from_record


def _negative_control_report(root: Path) -> list[str]:
    shadow = Shadow(root)
    proc = shadow.run_fence(shadow.argv(drop_masks=True))
    return [
        NEGATIVE_CONTROL_MUTATION,
        f"stub exit: {proc.returncode}",
        f"primary run dir contents: {sorted(p.name for p in shadow.primary_run.iterdir())}",
        f"results: {shadow.results_text().strip()!r}",
    ]


if __name__ == "__main__":  # pragma: no cover - reproduces the red log
    with tempfile.TemporaryDirectory() as directory:
        for line in _negative_control_report(Path(directory)):
            print(line)
    sys.exit(0)
