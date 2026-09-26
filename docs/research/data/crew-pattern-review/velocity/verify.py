"""Verify census repeatability, partition totals, and observable line identity."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import census


def main():
    root = census.HERE.parents[4]
    revision = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    print(
        f"revision={revision} tree={root} command={sys.executable} {Path(__file__).resolve()}",
        flush=True,
    )
    print(
        f"measurement_module={census.__file__}\nmeasurement_cwd={Path.cwd().resolve()}",
        flush=True,
    )
    expected = census.FULL_OUTPUT.read_bytes()
    expected_compact = census.COMPACT_OUTPUT.read_bytes()
    subprocess.run([sys.executable, str(census.HERE / "census.py")], check=True)
    assert census.FULL_OUTPUT.read_bytes() == expected, (
        "Full census re-run differs byte for byte"
    )
    assert census.COMPACT_OUTPUT.read_bytes() == expected_compact, (
        "Compact census re-run differs byte for byte"
    )
    assert len(expected_compact) < 300_000
    assert not (census.HERE / "velocity.json").exists()
    assert not (census.HERE / "inputs.json.gz").exists()
    data = json.loads(expected)
    compact = json.loads(expected_compact)
    for key in (
        "total",
        "by_project",
        "by_lane",
        "by_day",
        "coverage",
        "august_baseline",
        "provenance",
    ):
        assert compact[key] == data[key], key
    totals = data["total"]
    partitions = {
        key: data[key]
        for key in ("by_project", "by_lane", "by_day", "by_project_day_lane")
    }
    partitions["by_week"] = compact["by_week"]
    for rows in partitions.values():
        cells = [cell["metrics"] for cell in rows]
        assert (
            sum(c["promoted_nodes"]["denominator"] for c in cells)
            == totals["promoted_nodes"]["denominator"]
        )
        assert (
            sum(c["primary_commits"]["denominator"] for c in cells)
            == totals["primary_commits"]["denominator"]
        )
        for category in census.CLASSES:
            for counter in ("added", "removed", "binary_file_changes", "file_changes"):
                assert (
                    sum(c["lines"][category][counter] for c in cells)
                    == totals["lines"][category][counter]
                )
        assert (
            sum(c["durable_product_lines_seven_days"] for c in cells)
            == totals["durable_product_lines_seven_days"]
        )
    assert len(data["by_project_day_lane"]) == len(census.PROJECTS) * 15 * len(
        census.LANES
    )
    assert totals["product_deleted_within_seven_days"]["numerator"] > 0, (
        "Known churn must be visible"
    )
    assert totals["crew_attributed_product_additions"]["numerator"] > 0, (
        "Known crew product must be visible"
    )
    assert totals["right_censored_product_additions"] > 0, (
        "Late additions must be censored"
    )
    assert (
        sum(
            cell["metrics"]["promoted_nodes"]["denominator"] > 0
            for cell in data["by_project"]
        )
        == 5
    )
    assert all(not row["promotion_ids_without_record"] for row in data["coverage"])
    repo = census.CODE / "reckon"
    revisions = [
        "d2c015201c69d39841188e75585821f571786f6f",
        "fc223f8bb7c1cbd2229a56a5b29b11751aeafa52",
    ]
    path = "tests/test_crew_workspace_requirements.py"
    normal = (
        census.git(repo, "diff", "--numstat", *revisions, "--", path)
        .decode()
        .splitlines()[0]
    )
    zero = (
        census.git(repo, "diff", "--numstat", "--unified=0", *revisions, "--", path)
        .decode()
        .splitlines()[0]
    )
    parsed = census.parse_hunks(
        census.git(repo, "diff", "--unified=3", *revisions, "--", path)
    )
    assert normal.split("\t")[:2] == ["28", "21"]
    assert zero.split("\t")[:2] == ["30", "23"]
    assert sum(h[3] for p in parsed for h in p["hunks"]) == 28
    assert sum(h[1] for p in parsed for h in p["hunks"]) == 21
    print(
        "PASS: real-commit control at "
        + revisions[1]
        + ": normal numstat 28/21; zero-context 30/23; parsed normal-context 28/21"
    )
    census.positive_controls()
    print(
        "PASS: full and compact files reproduce byte for byte at their declared locations; compact is under 300 KB; every project/day/lane/week partition conserves counts and lines; all five projects visible; real churn and censoring nonzero"
    )
    print("JSON_SHA256=" + hashlib.sha256(expected).hexdigest())
    print("COMPACT_SHA256=" + hashlib.sha256(expected_compact).hexdigest())
    print("INPUT_SHA256=" + data["provenance"]["input_sha256"])


if __name__ == "__main__":
    main()
