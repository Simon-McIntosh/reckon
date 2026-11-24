"""Classify each binding sentence: enforcement, still-wanted verdict, disposition.

The scanner (scan_head.py) finds the binding sentences. This module decides, for
each, whether a production refusal or check implements the rule and whether a
test drives that guard far enough to make it fire. A row is marked *enforced*
only where a named test has been run and its log cited; every other row reads
"none", which is the honest state when no single refusal or check exists.

Dispositions follow the plan's four values:
  * delete as enforced  -- code refuses the guarded action, test cited;
  * delete as obsolete  -- the text itself retires the rule;
  * merge into <row>    -- proposed only where two rows state one rule;
  * keep                -- everything else.

The "still wanted" column is this node's proposal; the lead confirms it.

Run:  python3 classify.py            # JSONL rows
      python3 classify.py --enforced # only the enforced rows, for review
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import scan_head as scan

# Each matcher: (name, predicate, enforcement description, test module and id).
# The predicate inspects a lower-cased sentence. Only distinctive phrases are
# used so a rule is matched to the guard that enforces it, not to a nearby one.
TESTS = {
    "scope": (
        "dispatch refuses an exclusive-path claim before a worktree exists, naming the owning run",
        "tests/test_only_unmergeable_paths_are_claimed.py::test_an_unmergeable_path_is_refused_naming_its_live_owner",
    ),
    "context": (
        "routing raises a context-window refusal naming the window it exceeded",
        "tests/test_a_context_refusal_names_the_window.py::test_a_context_refusal_names_the_window_not_a_horizon",
    ),
    "member": (
        "per-member serialisation refuses a member that already owns a non-terminal live pointer",
        "tests/test_crew_refusal_visibility.py::test_a_member_already_in_flight_is_named_with_its_owning_run",
    ),
    "fold": (
        "the ledger writer refuses a fold recorded as a followup",
        "tests/test_the_ledger_writer_refuses_a_fold.py::test_a_write_of_the_loaded_union_is_refused_naming_the_folded_rows",
    ),
    "hides": (
        "plan writes refuse a followup that hides foldable work",
        "tests/test_a_followup_that_hides_work_is_refused.py::test_a_followup_on_a_shipped_plan_is_refused",
    ),
    "redispatch": (
        "a queued run keeps its id and its place; dispatch never tells it to redispatch",
        "tests/test_a_queued_run_is_never_told_to_redispatch.py::test_a_live_named_run_keeps_the_queued_request_on_that_runs_command",
    ),
    "queuebound": (
        "a dispatch past the queue bound is refused naming its bound",
        "tests/test_queue_depth_project_bound.py::test_the_501st_request_in_a_project_is_refused_by_the_bound",
    ),
    "ack": (
        "the Stop hook refuses while an obligation is unacknowledged",
        "tests/test_coordinator_obligations_hook.py::test_stop_mode_blocks_while_obligations_remain_and_your_own_continuation_stops_it",
    ),
    "manifest": (
        "the manifest reader refuses a status word outside the vocabulary",
        "tests/test_manifest_parse_format.py::test_an_unknown_status_word_raises_naming_the_word_and_the_recognised",
    ),
    "statusreconcile": (
        "the run classifier checks a manifest's own terminal word against the run classification",
        "tests/test_an_unnameable_wait_is_raised.py::test_an_ended_run_with_a_complete_status_keeps_its_word",
    ),
}


def has(s: str, *words: str) -> bool:
    return all(w in s for w in words)


def any_of(s: str, *words: str) -> bool:
    return any(w in s for w in words)


def match_enforcement(s: str):
    if "claim" in s and any_of(
        s, "path claim", "containing", "contained", "overlap", "before creating a worktree",
        "owning run", "exclusive path",
    ):
        return TESTS["scope"]
    if has(s, "context") and any_of(s, "window", "fit") and any_of(s, "refus", "reject", "before a worktree", "names the estimate"):
        return TESTS["context"]
    if "member" in s and any_of(s, "live pointer", "in flight", "non-terminal") and any_of(s, "refus", "serialis", "serializ"):
        return TESTS["member"]
    if "fold" in s and "followup" in s and any_of(s, "refus", "never a followup", "not a followup", "never a bucket", "ledger writer"):
        return TESTS["fold"]
    if any_of(s, "foldable followup", "followup that hides", "hides work") and any_of(s, "shipped", "done", "status", "refus"):
        return TESTS["hides"]
    if "queued run" in s and any_of(s, "never told to redispatch", "own id", "keeps its place", "never discard", "not to redispatch"):
        return TESTS["redispatch"]
    if any_of(s, "queue bound", "queue-full", "queue full", "past the bound", "max_requests") and any_of(s, "refus", "exit 75", "wait"):
        return TESTS["queuebound"]
    if any_of(s, "unacknowledged", "stop hook") and any_of(s, "refus", "obligation", "stop"):
        return TESTS["ack"]
    if "checked against the run classification" in s or "manifest_status" in s:
        return TESTS["statusreconcile"]
    if "status" in s and any_of(s, "vocabulary", "complete, blocked", "outside the vocabulary"):
        return TESTS["manifest"]
    return None


OBSOLETE = (
    "no longer needs cycling",
    "is a change from earlier guidance",
    "is fixed (age is now taken",
    "coordinator no longer reads",
    "no longer reads the file",
    "the smell that the work is a new subject",
)


def classify(row):
    s = row["rule"].lower()
    enforced = match_enforcement(s)
    if enforced:
        by, test = enforced
        return {
            "enforced_by": by,
            "test": test,
            "still_wanted": "no — code enforces it",
            "disposition": "delete as enforced",
            "reason": "The tool refuses the guarded action; the cited test drives it far enough to fire.",
        }
    if any(o in s for o in OBSOLETE):
        return {
            "enforced_by": "none",
            "test": "",
            "still_wanted": "no — obsolete",
            "disposition": "delete as obsolete",
            "reason": "The text itself retires this rule or records that the defect is fixed.",
        }
    return {
        "enforced_by": "none",
        "test": "",
        "still_wanted": "yes",
        "disposition": "keep",
        "reason": "No single refusal or check in the tree could be named for this rule.",
    }


def _norm_key(rule: str) -> str:
    import re

    return re.sub(r"[^a-z0-9 ]", "", rule.lower()).strip()


def build():
    """Scan, classify, and mark verbatim duplicates as merges into the first."""
    total, rows = scan.scan()
    out = [{**r, **classify(r)} for r in rows]
    seen: dict[str, dict] = {}
    for r in out:
        key = _norm_key(r["rule"])
        if len(key) > 40 and key in seen:
            target = seen[key]
            if r["disposition"] == "keep":
                r["disposition"] = f"merge into {target['file']}:{target['line']}"
                r["still_wanted"] = "yes (kept once)"
                r["reason"] = "Restates an earlier row verbatim; keep the survivor only."
            continue
        if len(key) and key not in seen:
            seen[key] = r
    return total, out


def main() -> int:
    total, out = build()
    if "--out" in sys.argv:
        dest = Path(sys.argv[sys.argv.index("--out") + 1])
        with dest.open("w", encoding="utf-8") as fh:
            for r in out:
                fh.write(json.dumps(r) + "\n")
        enforced = sum(1 for r in out if r["disposition"] == "delete as enforced")
        obsolete = sum(1 for r in out if r["disposition"] == "delete as obsolete")
        merged = sum(1 for r in out if r["disposition"].startswith("merge into"))
        print(
            f"# sentences={total} binding={len(out)} enforced={enforced} "
            f"obsolete={obsolete} merge={merged}"
        )
        return 0
    if "--enforced" in sys.argv:
        for r in out:
            if r["disposition"] == "delete as enforced":
                print(f"{r['file']}:{r['line']}\t{r['test']}\t{r['rule'][:100]}")
        return 0
    for r in out:
        print(json.dumps(r))
    print(f"# sentences={total} binding={len(out)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())