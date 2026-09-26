"""Check the census against positive protocol controls and an exact rerun."""

from __future__ import annotations

import argparse
import gzip
import json
import subprocess
import sys
from pathlib import Path

import census


def verify(replay):
    here = Path(__file__).resolve().parent
    print(f"measurement_module={census.__file__}")
    print(f"measurement_cwd={Path.cwd().resolve()}")
    data = json.loads((here / "overhead.json").read_text())
    pin = json.loads((here / "inputs.json").read_text())
    for ref in [pin["runs"], pin["branches"], *data["detail_artifacts"].values()]:
        census.checked(ref)
    for meta in pin["transcripts"].values():
        if meta["status"] == "captured":
            census.checked(meta["snapshot"])
    assert census.in_window("2026-09-12T00:00:00Z")
    assert census.in_window("2026-09-26T12:00:00+02:00")
    assert not census.in_window("2026-09-26T10:00:00.001Z")
    assert not census.in_window("2026-09-11T23:59:59Z")
    command = (
        'reckon crew resume --run example --advice "then run reckon crew complete"'
    )
    assert census.crew_verbs(command) == {"resume"}
    assert (
        census.receipt_objects(
            "{'ok': False, 'error': 'scope-conflict'}", {"dispatch"}
        )[0]["ok"]
        is False
    )
    nested = '{"ok": true, "record": {"ok": false, "error": "historical"}}'
    assert [r["ok"] for r in census.receipt_objects(nested, {"promotion"})] == [True]
    assert (
        census.receipt_objects("(Bash completed with no output)", {"promotion"}) == []
    )
    sid = "3955c8d2-c817-48fc-81bf-8a29bb14c6cc"
    turns, uses, results, diagnostics = census.read_transcript(pin["transcripts"][sid])
    message_id = "msg_011CfNp6hJEDBk8KmhYgTTyC"
    sample = [t for t in turns if t["id"] == message_id]
    assert len(sample) == 1
    usage = sample[0]["usage"]
    assert (
        usage["input_tokens"]
        + usage["cache_read_input_tokens"]
        + usage["cache_creation_input_tokens"]
        == 111913
    )
    assert usage["output_tokens"] == 197
    assert diagnostics["assistant_records"] > len(turns)
    attempts, _ = census.extract_attempts(turns, uses, results)
    refusal = next(
        r
        for r in attempts
        if "a passing gate requires the check that produced it" in r["reason"]
    )
    assert refusal["family"] == "promotion" and refusal["ok"] is False
    assert refusal["turns_to_next_success"] is not None
    print("POSITIVE CONTROL: repeated message counts once, 111913 input / 197 output")
    print("REFUSAL CONTROL:", json.dumps(refusal, sort_keys=True))
    total = data["totals"]
    sessions = data["sessions"]
    assert total["sessions"] == len(sessions)
    assert total["landed_nodes"] == sum(s["landed_nodes"] for s in sessions)
    captured = [s for s in sessions if s["transcript_status"] == "captured"]
    for row in sessions:
        if row["transcript_status"] != "captured":
            assert row["assistant_turns"] is None and row["tokens"] is None
        else:
            assert sum(row["tool_calls"].values()) == row["tool_calls_total"]
    assert sum(s["assistant_turns"] for s in captured) == total["assistant_turns"]
    assert total["tokens"]["input_tokens"] == sum(
        total["tokens"][k] for k in census.TOKEN_KEYS[:3]
    )
    records = json.loads(gzip.decompress(census.checked(pin["runs"]).read_bytes()))
    assert len(records) == total["runs_in_cohort"]
    assert all(
        (r["run"].get("node_definition") or {}).get("coordinator", {}).get("harness")
        == "claude-code"
        for r in records
    )
    assert data["protocol_controls"]["conflicting_merge"]["conflict_paths"]
    assert data["protocol_controls"]["conflicting_merge"]["exit"] == 1
    assert data["protocol_controls"]["clean_merge"]["exit"] == 0
    assert total["promotion_refusals"]["refusals"] > 0
    assert total["hand_committed_worker_diffs"]["count"] is None
    for family in ("dispatch", "promotion"):
        assert total[family + "_refusals"]["refusals"] == sum(
            s[family + "_refusals"]["refusals"] for s in captured
        )
    print(
        "COVERAGE: missing transcripts remain null; tool/token/run totals reconcile; both merge outcomes observed"
    )
    if replay:
        expected = (here / "overhead.json").read_bytes()
        destination = Path(pin["evidence_directory"]) / "overhead.replayed.json"
        subprocess.run(
            [sys.executable, str(here / "census.py"), "--output", str(destination)],
            check=True,
            timeout=900,
        )
        assert destination.read_bytes() == expected, (
            "census rerun differs from recorded output"
        )
        print("BYTE-IDENTICAL:", census.digest(destination))
    print("PASS: coordinator census verification")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rerun", action="store_true")
    args = parser.parse_args()
    verify(args.rerun)


if __name__ == "__main__":
    main()
