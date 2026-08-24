from __future__ import annotations

import re
from typing import Any

from reckon.crew.node import NEEDS_HELP_FIELDS, NEEDS_HELP_MARKER, TaskNode
from reckon.crew.runs import _utc_now

# ── Worker reports ──────────────────────────────────────────────────────────

_MANIFEST_LIST_KEYS = (
    "commits",
    "changed_paths",
    "test_logs",
    "artifacts",
    "evidence_inputs",
    "follow_ons",
    "blockers",
)
_NONE_VALUES = {"", "none", "n/a", "-", "nil"}


def parse_manifest(text: str) -> dict[str, Any]:
    """Parse a worker manifest into structured fields.

    Tolerant on purpose: a worker writes prose around its manifest and a strict
    parser would reject a delivered report over formatting. Unknown keys are
    kept so nothing a worker took the trouble to state is silently dropped.
    """
    fields: dict[str, Any] = {}
    key = None
    for raw in text.splitlines():
        line = raw.strip()
        match = re.match(r"^([a-z][a-z0-9_-]*)\s*:\s*(.*)$", line, re.IGNORECASE)
        if match:
            key = match.group(1).lower().replace("-", "_")
            fields[key] = match.group(2).strip()
        elif key and line.startswith(("-", "*")):
            addition = line.lstrip("-* ").strip()
            fields[key] = f"{fields[key]}, {addition}" if fields[key] else addition
    for name in _MANIFEST_LIST_KEYS:
        fields[name] = _as_list(fields.get(name))
    fields["needs_help"] = parse_needs_help(text) if NEEDS_HELP_MARKER in text else None
    return fields


def _as_list(value: Any) -> list[str]:
    """Split a manifest field into items, treating explicit nothing as empty."""
    if value is None:
        return []
    if isinstance(value, list):
        items = [str(item).strip() for item in value]
    else:
        items = [part.strip() for part in re.split(r"[,\n]", str(value))]
    return [item for item in items if item and item.lower() not in _NONE_VALUES]


def parse_needs_help(text: str) -> dict[str, Any]:
    """Parse an escape-hatch report, naming any of the four fields missing.

    A vague "I'm stuck" wastes as much time as thrashing, so the four fields are
    required: together they turn a plea into a decision brief the orchestrator
    can answer in one turn.
    """
    lines = text.splitlines()
    headline = ""
    for line in lines:
        if NEEDS_HELP_MARKER in line:
            headline = line.split(NEEDS_HELP_MARKER, 1)[1].strip()
            break
    fields: dict[str, str] = {}
    current: str | None = None
    for line in lines:
        stripped = line.strip()
        match = re.match(
            r"^(tried|options|leaning|cost-if-wrong)\s*:\s*(.*)$", stripped, re.I
        )
        if match:
            current = match.group(1).lower()
            fields[current] = match.group(2).strip()
        elif current and stripped:
            fields[current] = f"{fields[current]} {stripped}".strip()
    missing = [name for name in NEEDS_HELP_FIELDS if not fields.get(name)]
    return {
        "headline": headline,
        "fields": {name: fields.get(name, "") for name in NEEDS_HELP_FIELDS},
        "missing": missing,
        "complete": not missing and bool(headline),
    }


def audit_manifest(text: str, node: TaskNode | None = None) -> dict[str, Any]:
    """Judge a delivered manifest: is it complete, and does it stay in scope?"""
    manifest = parse_manifest(text)
    findings: list[str] = []
    status = str(manifest.get("status", "")).lower()
    if status not in ("complete", "blocked", "failed"):
        findings.append(f"status {status!r} is not complete, blocked or failed")
    if status == "complete" and not manifest["commits"]:
        findings.append("status is complete but no commit is recorded")
    if status == "complete" and not manifest.get("tests"):
        findings.append("status is complete but no test result is recorded")
    if node is not None and manifest["changed_paths"]:
        allowed = set(node.write_paths)
        stray = sorted(
            path for path in manifest["changed_paths"] if path not in allowed
        )
        if stray:
            findings.append(
                "changed paths outside the write scope: " + ", ".join(stray)
            )
    return {"manifest": manifest, "findings": findings, "ok": not findings}


def followup_ops_from_manifest(
    text: str,
    *,
    slug: str,
    section: str = "",
    written_by: str = "reckon-ship",
    now: str | None = None,
) -> list[dict[str, Any]]:
    """Turn a manifest's candidate follow-ons into plan followup append ops.

    This is the worker end of the continuation chain. A worker fenced out of
    work it discovered has nowhere to put it but prose, where it is lost; an op
    per candidate carries it into plan state, and the one-line invocation keeps
    the live plan as the only place guidance lives.
    """
    manifest = parse_manifest(text)
    stamp = now or _utc_now()
    invocation = f"/reckon-ship {slug}" + (f" {section}" if section else "")
    ops: list[dict[str, Any]] = []
    for index, candidate in enumerate(manifest["follow_ons"], start=1):
        ops.append(
            {
                "op": "append",
                "target": "followups",
                "item": {
                    "id": f"f-{re.sub(r'[^a-z0-9]+', '-', slug.lower())}-{stamp.replace(':', '').replace('-', '')}-{index}",
                    "status": "open",
                    "written_by": written_by,
                    "written_at": stamp,
                    "title": candidate[:120],
                    "body": (
                        f"<p>Found by a worker on {slug} and fenced out of its "
                        f"write scope: {candidate}</p>"
                    ),
                    "recommends_skill": invocation,
                    "prompt": invocation,
                },
            }
        )
    return ops
