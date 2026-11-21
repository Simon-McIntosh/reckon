"""A fragment's revision claim is held against the log it cites beside it.

A landing fragment cites the runs behind its measurements by log name, and a
sentence citing a log can also name the revision it believes that log records.
The run directory's logs are rewritten in place whenever a gate is rerun, so the
prose copy is contradicted by the very artefact the sentence points at and the
record stops being recheckable — measured on 2026-11-20, when a merged-head
sentence named main ``c422a8f5`` beside ``logs/merged-head.log`` while that log's
header had already been rewritten to record ``46602c8f``. The gate, both suite
arms and an independent review read past it, because nothing resolved the
citation against the file it named.

These cases pin the audit to that resolution: a claim naming another revision
than the cited log's header is a finding, a claim the log agrees with is not,
and a log header quoted into the fragment is the log's own words rather than a
claim the fragment makes, so it is not judged.

Every path here is a fixture under ``tmp_path``; nothing touches the operator's
own run store or a real worktree.
"""

from __future__ import annotations

from pathlib import Path

from reckon.crew.reports import audit_manifest

PLAN = "a-plan-with-a-fragment"
NODE = "a-node-with-a-log"
MAIN_HEADER = "46602c8fe42a7f922fd80ab68e599d843ed0cec6"
CLONE_REVISION = "2d8d5a7e3adb87cea5ddb701e40a6610583e3e23"
STALE_MAIN = "c422a8f588d6679090959a4927b1fc2a84e6fef3"


def _run(tmp_path: Path, fragment_body: str) -> Path:
    """A synthetic worktree holding one fragment and the run directory citing it."""
    tree = tmp_path / "tree"
    fragment = tree / "docs" / "evidence" / "fragments" / PLAN / f"{NODE}.html"
    fragment.parent.mkdir(parents=True)
    fragment.write_text(f"<html><body>{fragment_body}</body></html>\n")
    run = tmp_path / "runs" / "r-20260101T000000-fixture"
    (run / "logs").mkdir(parents=True)
    (run / "logs" / "merged-head.log").write_text(
        f"# revision: {CLONE_REVISION}\n"
        f"# main: {MAIN_HEADER}\n"
        "# command: pytest -q tests/example.py\n"
        "1 passed\n"
        "EXIT=0\n",
        encoding="utf-8",
    )
    (run / "manifest.md").write_text("node: fixture\n", encoding="utf-8")
    return run


def _manifest(run: Path, fragment: Path) -> tuple[str, Path]:
    """The manifest body a run of this shape delivers, and its own path."""
    body = "\n".join(
        [
            "node: a-node-with-a-log",
            "status: complete",
            "commits: " + CLONE_REVISION,
            f"changed_paths: docs/evidence/fragments/{PLAN}/{NODE}.html",
            "tests: pytest -q tests/example.py -> 1 passed",
        ]
    )
    return body, run / "manifest.md"


def _findings(tmp_path: Path, fragment_body: str) -> list[str]:
    run = _run(tmp_path, fragment_body)
    tree = tmp_path / "tree"
    body, manifest_path = _manifest(run, tree)
    audit = audit_manifest(body, worktree=tree, manifest_path=manifest_path)
    return [
        finding
        for finding in audit["findings"]
        if "on the same line that cites" in finding
    ]


def test_a_stale_main_beside_its_cited_log_is_reported(tmp_path: Path):
    body = (
        "<p>A throwaway clone of main "
        f"{STALE_MAIN} reads 1 passed, exit 0, in logs/merged-head.log.</p>"
    )

    findings = _findings(tmp_path, body)

    assert len(findings) == 1
    assert STALE_MAIN in findings[0]
    assert "logs/merged-head.log" in findings[0]
    assert MAIN_HEADER in findings[0]


def test_a_claim_the_cited_log_agrees_with_is_not_a_finding(tmp_path: Path):
    body = (
        f"<p>The clone of main {MAIN_HEADER[:12]} reads 1 passed, exit 0, "
        "in logs/merged-head.log.</p>"
    )

    assert _findings(tmp_path, body) == []


def test_a_log_citing_without_a_claim_left_to_the_header(tmp_path: Path):
    body = (
        "<p>The merged-head clone reads 1 passed, exit 0, in logs/merged-head.log, "
        "whose header names the revision it was cut from.</p>"
    )

    assert _findings(tmp_path, body) == []


def test_a_header_quoted_into_the_fragment_is_not_judged(tmp_path: Path):
    body = (
        "<p>The merged-head clone reads 1 passed, exit 0, in logs/merged-head.log.</p>"
        "<pre># revision: "
        f"{CLONE_REVISION}\n# main: {STALE_MAIN}\nEXIT=0</pre>"
    )

    assert _findings(tmp_path, body) == []
