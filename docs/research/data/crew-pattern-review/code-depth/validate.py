"""Check artifact identities, source bindings and append-only document changes."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime
from functools import cache
from pathlib import Path
from xml.etree import ElementTree


def main():
    here = Path(__file__).resolve().parent
    root = here.parents[4]
    sys.path.insert(0, str(root))
    from reckon import doccheck

    print("measurement_module=" + str(Path(__file__).resolve()))
    print("measurement_cwd=" + str(Path.cwd().resolve()))
    print("doccheck_module=" + doccheck.__file__)
    data = json.loads((here / "code-depth.json").read_text())
    assert (here / "code-depth.json").stat().st_size < 300_000
    full_path = Path(data["full_output"]["path"])
    assert not full_path.resolve().is_relative_to(root)
    full_bytes = full_path.read_bytes()
    assert len(full_bytes) == data["full_output"]["bytes"]
    assert hashlib.sha256(full_bytes).hexdigest() == data["full_output"]["sha256"]
    full = json.loads(full_bytes)
    assert (
        data["ranking_source_sha256"]
        == hashlib.sha256((here / "candidates.py").read_bytes()).hexdigest()
    )
    assert data["inputs"] == full["inputs"]
    actual = hashlib.sha256((here / "census.py").read_bytes()).hexdigest()
    assert actual == data["instrument_sha256"]
    assert len(data["snapshots"]) == 8
    assert sum(len(s["scopes"]) for s in data["snapshots"]) == 12
    cutoff = datetime.fromisoformat(data["inputs"]["capture_cutoff"]).timestamp()
    for repository in data["inputs"]["repositories"]:
        repo = Path("/home/ITER/mcintos/Code") / repository["repository"]
        history = subprocess.check_output(
            [
                "git",
                "-C",
                str(repo),
                "log",
                "--first-parent",
                "--format=%H|%ct",
                repository["history_tip"],
            ],
            text=True,
        )
        candidates = [
            (sha, int(stamp))
            for sha, stamp in (line.split("|") for line in history.splitlines())
            if int(stamp) <= cutoff
        ]
        for snapshot in repository["snapshots"]:
            target = datetime.fromisoformat(snapshot["target"]).timestamp()
            selected = min(
                candidates, key=lambda item: (abs(item[1] - target), item[1], item[0])
            )
            assert selected[0] == snapshot["commit"]
    print(
        "PASS: all eight nearest-commit selections independently re-derived from pinned primary history"
    )
    for snapshot, compact_snapshot in zip(
        full["snapshots"], data["snapshots"], strict=True
    ):
        assert snapshot["commit"] == compact_snapshot["commit"]
        for scope, m in snapshot["scopes"].items():
            compact = compact_snapshot["scopes"][scope]
            assert "histogram" not in compact["function_length_distribution"]
            assert "windows" not in compact["clones"]
            for key, value in compact.items():
                if isinstance(value, (int, float)) and key in m:
                    assert value == m[key]
            for key in ("function_length_distribution", "clones"):
                for name, value in compact[key].items():
                    assert value == m[key][name]
            for name, value in compact["interfaces"].items():
                if name == "dispatch_raise_site_count":
                    assert value == len(m["interfaces"]["dispatch_raise_sites"])
                else:
                    assert value == m["interfaces"][name]
            widest = sorted(
                (row for row in m["module_surfaces"] if row["public_surface"] >= 5),
                key=lambda row: (
                    -row["public_surface_per_100_code_lines"],
                    row["path"],
                ),
            )[:5]
            assert compact["module_surfaces"] == widest
            assert m["module_count"] == len(m["module_surfaces"])
            assert m["public_function_count"] == len(m["public_functions"])
            assert m["public_class_count"] == len(m["public_classes"])
            assert m["source_lines"] == sum(
                row["physical_lines"] for row in m["module_surfaces"]
            )
            assert m["function_length_distribution"]["count"] == sum(
                m["function_length_distribution"]["histogram"].values()
            )
            assert m["pass_through_function_count"] == len(m["pass_through_functions"])
            clone = m["clones"]
            assert 0 <= clone["cloned_source_code_lines"] <= clone["source_code_lines"]
            assert clone["cloned_source_code_lines"] == sum(
                row["cloned_code_lines"] for row in clone["by_module"]
            )
            assert clone["duplicate_window_fingerprints"] == len(clone["windows"])
            assert scope.startswith(snapshot["repository"])
    print(
        "PASS: compact census is below 300000 bytes; all twelve scope rows reconcile with the external full census, including counts and clone denominators"
    )

    @cache
    def functions_at(repo, commit, path):
        source = subprocess.check_output(
            [
                "git",
                "-C",
                "/home/ITER/mcintos/Code/" + repo,
                "show",
                commit + ":" + path,
            ],
            text=True,
        )
        return {
            (node.lineno, node.name)
            for node in ast.walk(ast.parse(source))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

    ranked = json.loads((here / "consolidation.json").read_text())
    assert ranked == data["ranked_candidates"]
    assert [row["rank"] for row in ranked] == list(range(1, 11))
    checked = 0
    for candidate in ranked:
        assert candidate["home"] and len(candidate["copies"]) >= 3
        for copy in candidate["copies"]:
            expected = (copy["line"], copy["function"].split(".")[-1])
            assert expected in functions_at(
                candidate["repository"], candidate["commit"], copy["path"]
            )
            checked += 1
    print(
        f"PASS: ten ranked candidates and all {checked} source locations resolve at their pinned commits"
    )
    plan_path = root / "docs/plans/orchestrator-crew-pattern-studies.html"
    base = subprocess.check_output(
        [
            "git",
            "-C",
            str(root),
            "show",
            "457bbec7e691465101ea7c56716fe23eb8477dfe:docs/plans/orchestrator-crew-pattern-studies.html",
        ],
        text=True,
    )
    current = plan_path.read_text()
    stripped = re.sub(
        r"    <!-- code-depth landing start -->.*?    <!-- code-depth landing end -->\n\n",
        "",
        current,
        flags=re.DOTALL,
    )
    assert stripped == base
    before = doccheck.audit_html(base, project="reckon")
    after = doccheck.audit_html(current, project="reckon")

    def signature(finding):
        return finding.severity, finding.code, finding.message

    assert [signature(f) for f in before] == [signature(f) for f in after]
    assert not any(f.severity == "error" for f in after)
    evidence = (
        root / "docs/evidence/archive/orchestrator-crew-pattern-studies-landed.html"
    )
    assert not doccheck.audit_file(evidence, project="reckon")
    print(
        f"PASS: plan append preserves the complete base document; {len(before)} existing warnings, zero added warnings/errors; evidence audit clean"
    )
    svg = root / "docs/figures/orchestrator-crew-pattern-studies/code-depth-trends.svg"
    ElementTree.parse(svg)  # noqa: S314 -- This is the locally generated plot artifact.
    assert (
        "/reckon/figures/orchestrator-crew-pattern-studies/code-depth-trends.svg"
        in evidence.read_text()
    )
    for name in ("census.log", "reproduction.log"):
        log = (here / name).read_text()
        assert log.startswith("revision=") and log.endswith("exit_status=0\n")
    assert "byte-identity check passed" in (here / "reproduction.log").read_text()
    print(
        "PASS: project-absolute figure reference, valid SVG and successful census/reproduction receipts"
    )
    print(
        "artifact_sha256="
        + hashlib.sha256((here / "code-depth.json").read_bytes()).hexdigest()
    )


if __name__ == "__main__":
    main()
