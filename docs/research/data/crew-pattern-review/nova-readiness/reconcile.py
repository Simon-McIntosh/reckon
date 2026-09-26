"""Reconcile saved pytest receipts and write the deterministic readiness record.

This does not execute tests. The full logs, outcome events, pinned snapshots and
per-snapshot receipts are the evidence inputs; their hashes are checked before
counts and classifications are derived. A repeat run reproduces the same JSON.
"""

import collections
import csv
import hashlib
import io
import json
import re
import tomllib
from pathlib import Path

import census


def read_log(record):
    path = Path(record["log"])
    if not path.is_absolute():
        path = census.DATA_DIR / path
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == record["log_sha256"], path
    return raw.decode()


def outcome_states(events):
    states = dict.fromkeys(events.get("selected", []), "not_run")
    for report in events.get("tests", []):
        node = report["nodeid"]
        if report["outcome"] == "failed":
            states[node] = "failed" if report["when"] == "call" else "errored"
        elif report["outcome"] == "skipped":
            states[node] = "xfailed" if report["wasxfail"] else "skipped"
        elif report["when"] == "call" and states.get(node) != "errored":
            states[node] = "xpassed" if report["wasxfail"] else "passed"
    return states


def classify_entry(entry, tree, source_paths):
    message = entry["message"]
    local_import = re.search(r"No module named ['\"]([^'\"]+)", message)
    if local_import and any(
        Path(path).name == local_import.group(1).split(".")[-1] + ".py"
        for path in source_paths
    ):
        return {
            **entry,
            "classification": "code",
            "reason": "The missing import names a module present in this committed tree; its import path does not resolve during the default collection.",
        }
    if "cannot import name" in message and str(tree) in message:
        return {
            **entry,
            "classification": "code",
            "reason": "An importer and the module lacking its required symbol both belong to the committed snapshot.",
        }
    environment, _ = census.classify([entry])
    if environment:
        return {
            **entry,
            "classification": "environment",
            "reason": environment[0]["rule"],
        }
    return {
        **entry,
        "classification": "unclassified",
        "reason": "Raw exception retained; no environment cause inferred without matching evidence.",
    }


def enrich(snapshot):
    source_paths = census.git_out(
        "ls-tree", "-r", "--name-only", snapshot["sha"]
    ).splitlines()
    q = snapshot["pytest"]
    text = read_log(q)
    parsed = census.parse_pytest_log(text)
    q.update(parsed["counts"])
    q["summary_seen"] = parsed["summary_seen"]
    event_path = Path(q["outcomes_log"]) if q.get("outcomes_log") else None
    events = {}
    if event_path:
        raw = event_path.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == q["outcomes_sha256"]
        events = json.loads(raw)
    q["selected"] = len(events["selected"]) if "selected" in events else None
    q["executed_unique_tests"] = len({e["nodeid"] for e in events.get("tests", [])})
    q["collection_errors"] = []
    for event in events.get("collection", []):
        if event["outcome"] != "failed":
            continue
        error_lines = [
            line for line in event["message"].splitlines() if line.startswith("E ")
        ]
        message = "\n".join(error_lines) or event["message"]
        q["collection_errors"].append(
            classify_entry(
                {"nodeid": event["nodeid"], "message": message},
                Path(snapshot["measurement_cwd"]),
                source_paths,
            )
        )
    q["runtime_failures"] = [
        classify_entry(entry, Path(snapshot["measurement_cwd"]), source_paths)
        for entry in parsed["failures"] + parsed["errors"]
        if entry["message"]
    ]
    classified = q["collection_errors"] + q["runtime_failures"]
    q["environment"] = [e for e in classified if e["classification"] == "environment"]
    q["code"] = [e for e in classified if e["classification"] == "code"]
    q["unclassified"] = [e for e in classified if e["classification"] == "unclassified"]
    q["environment_error_count"] = sum(
        e["classification"] == "environment" for e in q["collection_errors"]
    )
    q["code_collection_error_count"] = sum(
        e["classification"] == "code" for e in q["collection_errors"]
    )
    q["collection_error_unique_modules"] = len(
        {e["nodeid"] for e in q["collection_errors"]}
    )
    q["test_outcomes_measured"] = (
        q["summary_seen"] and not q["truncated"] and q["exit_status"] in (0, 1)
    )
    q["measurement_status"] = (
        "complete"
        if q["test_outcomes_measured"]
        else "collection_failed"
        if q["collection_errors"]
        else "incomplete"
    )
    q["not_measured_reason"] = (
        None
        if q["test_outcomes_measured"]
        else "Default pytest stopped at collection; zero passed/failed records are not a zero-percent pass rate."
        if q["collection_errors"]
        else "No complete pytest summary; inspect the retained log and process exit status."
    )
    q["zero_outcome_interpretation"] = (
        "observed counts in the complete pytest summary"
        if q["test_outcomes_measured"]
        else "no executed test outcomes; collection counts and errors are observed"
    )
    if events:
        assert (
            len([e for e in events["collection"] if e["outcome"] == "failed"])
            <= q["errored"]
        )
    ruff = json.loads(read_log(snapshot["ruff"]))
    assert len(ruff) == snapshot["ruff"]["findings"]
    config = tomllib.loads(census.git_out("show", f"{snapshot['sha']}:pyproject.toml"))[
        "tool"
    ]
    snapshot["ruff"]["config_sha256"] = hashlib.sha256(
        json.dumps(config.get("ruff", {}), sort_keys=True).encode()
    ).hexdigest()
    q["collection_policy_sha256"] = hashlib.sha256(
        census.git_out("show", f"{snapshot['sha']}:conftest.py").encode()
    ).hexdigest()
    snapshot["ruff"]["files_with_findings"] = len({r["filename"] for r in ruff})
    snapshot["markers"]["scope"] = (
        "Python files under nova/ and tests/; TODO/FIXME in COMMENT tokens, pytest marker spellings anywhere in those files"
    )
    return snapshot, events


def write_changes(first, last, first_events, last_events):
    before, after = outcome_states(first_events), outcome_states(last_events)
    rows = []
    for node in sorted(before.keys() | after.keys()):
        old, new = before.get(node, "not_selected"), after.get(node, "not_selected")
        if old != new:
            rows.append({"nodeid": node, "first": old, "last": new})
    counts = collections.Counter((r["first"], r["last"]) for r in rows)
    shards, current = [], []

    def emit(items):
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=["nodeid", "first", "last"])
        writer.writeheader()
        writer.writerows(items)
        return output.getvalue()

    for row in rows:
        current.append(row)
        if len(emit(current).encode()) >= 280_000:
            last_row = current.pop()
            shards.append(
                census.save_artifact(
                    f"state-changes-{len(shards) + 1}.csv", emit(current)
                )
            )
            current = [last_row]
    if current:
        shards.append(
            census.save_artifact(f"state-changes-{len(shards) + 1}.csv", emit(current))
        )
    return {
        "first": first["key"],
        "last": last["key"],
        "first_selected": len(before),
        "last_selected": len(after),
        "common_selected": len(before.keys() & after.keys()),
        "changed": len(rows),
        "transitions": [
            {"first": old, "last": new, "count": number}
            for (old, new), number in sorted(counts.items())
        ],
        "artifacts": shards,
        "qualification": "not_run means selected but collection prevented execution; not_selected means absent from the filtered collected node IDs, not necessarily deleted. Neither establishes a pass-to-fail regression.",
    }


def main():
    snapshots = json.loads((census.DATA_DIR / "snapshots.json").read_text())
    receipts, events = [], {}
    for snapshot in snapshots:
        raw = json.loads(
            (census.RUN_DIR / "parts" / f"{snapshot['key']}.json").read_text()
        )
        enriched, event = enrich(raw)
        receipts.append(enriched)
        events[snapshot["key"]] = event
    record = census.assemble(receipts, snapshots)
    record["classification_rules"]["note"] = (
        "Collection errors are classified from full event tracebacks; missing imports of modules present in the snapshot are code failures. Matching external-resource messages are environment failures. Remaining runtime failures are unclassified pending inspection."
    )
    record["first_to_last"] = write_changes(
        receipts[0],
        receipts[-1],
        events[receipts[0]["key"]],
        events[receipts[-1]["key"]],
    )
    record["capture"] = {
        "nova_head": record["head_sha"],
        "same_config_at_all_snapshots": len(
            {r["pytest_config_sha256"] for r in receipts}
        )
        == 1,
        "distinct_measurements": len({r["sha"] for r in receipts}),
        "runtime": "shared Nova Python 3.14.2 environment on fleet CPU node; JAX_PLATFORMS=cpu; numerical library thread counts=1",
        "reproduce": "Run reconcile.py against the saved per-snapshot receipts and hashed complete logs; no tests rerun.",
    }
    target = census.DATA_DIR / "readiness.json"
    content = json.dumps(record, indent=2, sort_keys=True) + "\n"
    assert len(content.encode()) < census.MAX_COMMITTED_BYTES, len(content.encode())
    target.write_text(content)
    print(
        f"Reconciled {len(receipts)} snapshot rows from {record['capture']['distinct_measurements']} distinct measurements"
    )
    print(f"readiness.json sha256={hashlib.sha256(content.encode()).hexdigest()}")


if __name__ == "__main__":
    main()
