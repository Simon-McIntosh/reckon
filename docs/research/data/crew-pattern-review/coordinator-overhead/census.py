#!/usr/bin/env python3
"""Census of coordinator overhead per landed node over the dispatch-pattern window.

Every figure is read from durable records the fleet itself wrote:

* the committed ledgers ``<repo>/docs/state/<project>/crew.json`` identify the
  coordinator session of every run dispatched in the window, through
  ``node_definition.coordinator.runtime_session_id``;
* the coordinator session transcripts under
  ``~/.claude/projects/<project-key>/<session>.jsonl`` carry assistant turns,
  token usage, tool calls, and the refusals the coordinator read;
* the primary branches of the five projects carry the merges, replayed with
  ``git merge-tree`` on the recorded parents.

Definitions this census commits to, because a census that hides its definitions
can only be believed, never argued with:

landed node
    A dispatched run whose work reached its project's primary branch. A run is
    landed when ``promoted_revision`` or ``release`` is recorded on it
    (promotion route), or when at least one of its recorded commits is an
    ancestor of the primary branch head (hand-integration route). The second
    route is this census's proxy for a worker diff the coordinator integrated
    by hand rather than through ``crew complete``.

assistant turn
    One assistant API response, deduplicated by ``message.id``: the harness
    writes several transcript records per response as its content streams, each
    carrying identical usage, so counting records would inflate both turns and
    tokens.

tool-call category
    One exclusive label per tool call, from the command text for shell verbs
    and from the tool name otherwise; the classifier is
    ``tool_call_category`` and its buckets are the ones the study names.

refusal
    A crew command whose result carries ``"ok": false`` with an ``error`` code.
    The code is what the fleet said no to; the reason is the first clause of the
    accompanying detail. The turns between a refusal and the next successful
    command of the same family count assistant turns in that interval.

The script writes ``overhead.json`` beside itself and nothing else; re-running
it reproduces that file byte for byte. It reads other checkouts with read-only
git verbs only, and redirects ``git merge-tree``'s object writes into a
throwaway directory so no repository it inspects is mutated.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

WINDOW_START = "2026-09-12T00:00:00Z"
WINDOW_END = "2026-09-26T10:00:00Z"

REPO_ROOT = Path("/home/ITER/mcintos/Code")
PROJECTS = {
    "reckon": ("reckon", "main"),
    "imas-ambix": ("imas-ambix", "main"),
    "nova": ("nova", "main"),
    "imas-efit": ("imas-efit", "develop"),
    "imas-codex": ("imas-codex", "main"),
}
TRANSCRIPT_ROOT = Path.home() / ".claude" / "projects"

DISPATCH_VERBS = {"dispatch", "shadow", "redispatch", "resume", "resume-ready", "attach"}
FOLLOWER_VERBS = {"follow", "watch", "ticker", "monitor"}
CREW_READ_VERBS = {
    "ledger", "status", "summary", "runs", "observe", "exits", "obligations",
    "directory", "lanes", "budget", "fleet", "rollup", "preflight", "path",
    "crew", "review", "reports", "routing", "plan", "scope", "widen",
}
PROMOTION_VERBS = {"complete", "release", "promote", "discard"}
GIT_MUTATING = {
    "add", "commit", "merge", "push", "pull", "fetch", "checkout", "restore",
    "tag", "rm", "mv", "revert", "cherry-pick", "rebase", "apply", "am",
    "reset", "stash", "worktree", "init", "clone", "gc", "prune",
}
GIT_READING = {
    "log", "status", "show", "diff", "rev-parse", "merge-base", "ls-files",
    "grep", "cat-file", "describe", "branch", "remote", "for-each-ref",
}
PLAN_PATH = re.compile(r"docs/(plans|evidence|research)/")
LOG_PATH = re.compile(r"(\.log\b|manifest\.md|/runs/|/reviews/|gate)")
CREW_VERB = re.compile(r"(?<![\w-])crew\s+([a-z][a-z-]*)")
OK_FALSE = re.compile(r'"ok"\s*:\s*false\s*,\s*"error"\s*:\s*"([a-z0-9_.:-]+)"')
DETAIL = re.compile(r'"detail"\s*:\s*"')
REMEDY = re.compile(r"\s*Resolve with [^.]*\.")
READ_SHELL = re.compile(r"\b(cat|tail|head|grep|less|wc|jqy|jq)\b")

CATEGORY_ORDER = [
    "dispatch",
    "promotion",
    "crew_reads_and_mcp_views",
    "git_and_merges",
    "plan_edits",
    "reading_worker_diffs_and_gate_logs",
    "follower_notifications",
    "other",
]


def iso_in_window(ts):
    return bool(ts) and WINDOW_START <= ts <= WINDOW_END


def run_git(repo, *args, env=None):
    full_env = dict(os.environ)
    full_env.pop("GIT_DIR", None)
    full_env.pop("GIT_WORK_TREE", None)
    if env:
        full_env.update(env)
    proc = subprocess.run(
        ["git", "-C", str(REPO_ROOT / repo), *args],
        capture_output=True, text=True, env=full_env, errors="replace",
    )
    return proc.returncode, proc.stdout


# ---------------------------------------------------------------- sessions

def read_sessions():
    """Group in-window runs by coordinator session, keeping each run's repo."""
    sessions = {}
    for repo, (project, _branch) in PROJECTS.items():
        ledger = REPO_ROOT / repo / "docs" / "state" / project / "crew.json"
        data = json.loads(ledger.read_text())["data"]
        for run in data["runs"]:
            if not iso_in_window(run.get("dispatched_at"):
                continue
            coordinator = ((run.get("node_definition") or {}).get("coordinator")) or {}
            sid = coordinator.get("runtime_session_id")
            if not sid:
                continue
            entry = sessions.setdefault(sid, {
                "labels": set(), "harness": set(), "runs": [],
            entry["runs"].append({"repo": repo, "run": run})
            label = coordinator.get("session_id")
            if label:
                entry["labels"].add(label)
            if coordinator.get("harness"):
                entry["harness"].add(coordinator["harness"])
    return sessions