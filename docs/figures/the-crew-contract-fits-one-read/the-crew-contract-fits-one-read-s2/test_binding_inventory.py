"""Check the binding inventory's page against the facts it claims.

Run by path, from the repository root:

    PYTHONPATH=. .venv/bin/python -m pytest -p no:cacheprovider -q \
      docs/figures/the-crew-contract-fits-one-read/the-crew-contract-fits-one-read-s2

Each arm fails on the defect it checks, so the page cannot drift from its
synthetic logs or its source locators without a red test.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = Path(__file__).resolve().parents[4]
for entry in (str(HERE), str(REPO)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import classify  # noqa: E402
import render_fragment  # noqa: E402
import scan_binding  # noqa: E402
import scan_head  # noqa: E402


def _synthetic_logs(tmp_path: Path, tests: list[str]) -> Path:
    """One log per test, each naming the node id its command ran."""
    logs = tmp_path / "logs"
    logs.mkdir()
    for index, test in enumerate(tests):
        name = f"synthetic-arm-{index}.log"
        body = (
            "# rev=deadbeef00 tree=/synthetic\n"
            f"# command: python -m pytest {test}\n"
            "============================== 1 passed in 0.01s "
            "===============================\n"
        )
        (logs / name).write_text(body, encoding="utf-8")
    return logs


def test_every_log_the_page_names_exists_in_the_log_directory(tmp_path: Path) -> None:
    """A cited log is the arm that ran the test, not a name built from its file."""
    total, rows = classify.build()
    tests = sorted({r["test"] for r in rows if r["disposition"] == "delete as enforced"})
    assert tests
    logs = _synthetic_logs(tmp_path, tests)
    statements = render_fragment.log_statements(logs)
    page = render_fragment.render(rows, total, statements)
    named = sorted(set(re.findall(r"logs/enforced/([A-Za-z0-9_.-]+\.log)", page)))
    assert named, "the page names no log at all"
    for name in named:
        assert (logs / name).is_file(), name


def test_every_binding_row_is_located_at_the_line_its_sentence_starts() -> None:
    """A locator points at the sentence a reader following it must find."""
    offenders = []
    for rel in scan_head.FILES:
        text = (scan_head.SKILLS / rel).read_text(encoding="utf-8")
        lines = text.splitlines()
        for line, sentence in scan_head.sentences_with_lines(text):
            head = " ".join(sentence.split())[:45]
            window = " ".join(
                " ".join(scan_head.clean(raw) for raw in lines[line - 1 : line + 20]).split()
            )
            if head not in window:
                offenders.append((rel, line, head))
    assert offenders == [], offenders[:5]


def test_the_renderer_refuses_a_citation_no_log_carries(tmp_path: Path) -> None:
    """The page cannot claim an enforcement no log records."""
    row = {
        "file": "skills/reckon-build/SKILL.md",
        "line": 1,
        "rule": "A rule that names a test nobody ran.",
        "enforced_by": "a check",
        "test": "tests/test_a_guard_that_does_not_exist.py::test_never_run",
        "still_wanted": "no",
        "disposition": "delete as enforced",
        "reason": "synthetic",
    }
    rows_file = tmp_path / "rows.jsonl"
    rows_file.write_text(json.dumps(row) + "\n", encoding="utf-8")
    logs = _synthetic_logs(tmp_path, [])
    out = tmp_path / "page.html"
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPO)
    result = subprocess.run(
        [
            sys.executable,
            str(HERE / "render_fragment.py"),
            "--rows",
            str(rows_file),
            "--logs",
            str(logs),
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        cwd=str(HERE),
        env=environment,
    )
    assert result.returncode == 2, result.stderr
    assert "test_a_guard_that_does_not_exist" in result.stderr
    assert not out.exists()


def test_the_scanner_uses_the_owner_vocabulary() -> None:
    """One definition of a rule: the owner's, imported rather than copied."""
    assert scan_head.KEYWORDS is scan_binding.KEYWORDS
    assert scan_head.IMPERATIVE_VERBS is scan_binding.IMPERATIVE_VERBS
    assert scan_head.ABBREVIATIONS is scan_binding.ABBREVIATIONS

def test_the_status_reconciliation_row_cites_a_test_that_judges_the_complete_word() -> None:
    """An enforced row's test must drive the rule, not only refuse a bad word."""
    total, rows = classify.build()
    matched = [r for r in rows if "checked against the run classification" in r["rule"]]
    assert matched, "the reconciliation rule has no row"
    row = matched[0]
    assert row["disposition"] == "delete as enforced"
    source = (REPO / row["test"].split("::")[0]).read_text(encoding="utf-8")
    assert 'manifest_status"] == "complete"' in source
