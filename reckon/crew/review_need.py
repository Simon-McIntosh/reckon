"""Judge whether a change since a review needs a new review, through Jev.

One typed judgment serves the plan review and the run review. A review read the
work at one revision; the work has changed since. Given what the review checks,
the diff since it read the work and the goals the work serves, Jev returns the
probability that the change could alter a verdict the review reached or adds
content of a kind it never read. A word-count share cannot tell a typo fix from
a changed measure of the same length, which is what made every small edit to a
large plan buy another whole-plan review; the diff and the goals can.

Code keeps the policy. Every change of one call is asked in one request, as one
Noul question each over a shared state, and a probability at or above the
threshold means a new review is owed. A change the judge did not answer — the
service disabled or failing, an answer of the wrong shape, a diff too large to
read whole — comes back as ``required=None`` with the reason, so the caller
applies its own deterministic rule and nothing is decided by an absence. An
answer is cached by the exact inputs it judged, so the edit tool and the
dispatch gate reading the same change ask once and cannot disagree.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from reckon import _store

PROMPT_PATH = Path(__file__).parent / "prompts" / "review_need.json"

# The subjects a review is taken of, naming the checklist the judge is shown.
PLAN_SUBJECT = "plan"
RUN_SUBJECT = "run"

# The largest diff the judge is shown whole. A change past it is not a minor
# edit of reviewed content, so it is left to the caller's rule rather than
# judged from a truncated view that could hide the part that matters.
MAX_DIFF_CHARS = 12_000


@dataclass(frozen=True)
class Change:
    """One reviewed unit's text as the review read it and as it stands now.

    ``identity`` names the unit to the caller and never reaches the model.
    ``goal`` is what the unit itself must achieve, such as a section's
    done-when or a run's goal, beside the goals shared by every change.
    """

    identity: str
    reviewed: str
    present: str
    goal: str = ""


@dataclass(frozen=True)
class Verdict:
    """Whether one change needs a new review, and what decided it.

    ``required`` is None when the judge gave no answer; ``reason`` then says
    why, and the caller's deterministic rule decides. ``source`` is ``jev``
    for a fresh answer and ``cache`` for one read back from an earlier call.
    """

    required: bool | None
    probability: float | None
    source: str
    reason: str = ""


@lru_cache(maxsize=1)
def _prompt() -> dict[str, Any]:
    return json.loads(PROMPT_PATH.read_text(encoding="utf-8"))


def unified_diff(reviewed: str, present: str) -> str:
    """The line diff from the reviewed text to the present text.

    Unit prose arrives whitespace-normalised onto one line, so it is split at
    sentence ends first; a one-line diff would show the whole unit replaced
    for a one-word edit.
    """

    def lines(text: str) -> list[str]:
        return [part for part in text.replace(". ", ".\n").splitlines() if part]

    return "\n".join(
        difflib.unified_diff(
            lines(reviewed), lines(present), "reviewed", "present", lineterm="", n=1
        )
    )


def _cache_root() -> Path:
    return _store._config_home() / "crew" / "review-need"


def _client():
    # Imported on use: the picker package initialises the dispatch machinery,
    # which itself reads plan reviews, so a module-level import is circular.
    from reckon.crew.picker import client

    return client


def _cache_key(
    subject: str, checks: Sequence[str], goals: Mapping[str, Any], entry: Mapping
) -> str:
    payload = json.dumps(
        {
            "model": _client().JEV_MODEL,
            "prompt": _prompt()["instructions"],
            "criteria": _prompt()["criteria"],
            "subject": subject,
            "checks": list(checks),
            "goals": goals,
            "change": entry,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_cached(key: str) -> float | None:
    path = _cache_root() / key[:2] / f"{key}.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8")).get("probability")
    except (OSError, ValueError, AttributeError):
        return None
    return value if _is_probability(value) else None


def _write_cached(key: str, probability: float, model: str | None) -> None:
    path = _cache_root() / key[:2] / f"{key}.json"
    try:
        _store.write_json_atomically(
            path,
            {"probability": probability, "model": model, "at": time.time()},
            fsync=False,
        )
    except OSError:
        # The cache saves a request; an unwritable one costs the next caller a
        # second request and changes no verdict.
        return


def _is_probability(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0.0 <= value <= 1.0
    )


def judge(
    changes: Sequence[Change],
    *,
    subject: str,
    goals: Mapping[str, Any],
    threshold: float,
    caller: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Verdict]:
    """Return one verdict per change, keyed by its identity.

    Changes whose diff is empty are not asked about: nothing changed, so
    nothing is owed. Every remaining change is answered from the cache or
    asked in a single request; a failure of that request leaves each unasked
    change unanswered, with the failure's type as its reason, never its text,
    which may carry provider content.
    """
    prompt = _prompt()
    checks = list(prompt["checks"][subject])
    verdicts: dict[str, Verdict] = {}
    pending: list[tuple[str, str, dict[str, str]]] = []
    for change in changes:
        diff = unified_diff(change.reviewed, change.present)
        if not diff:
            verdicts[change.identity] = Verdict(False, 0.0, "unchanged")
            continue
        if len(diff) > MAX_DIFF_CHARS:
            verdicts[change.identity] = Verdict(
                None, None, "unjudged", f"diff exceeds {MAX_DIFF_CHARS} characters"
            )
            continue
        entry = {"goal": change.goal, "diff": diff}
        key = _cache_key(subject, checks, goals, entry)
        cached = _read_cached(key)
        if cached is not None:
            verdicts[change.identity] = Verdict(cached >= threshold, cached, "cache")
            continue
        pending.append((change.identity, key, entry))
    if not pending:
        return verdicts
    state = {
        "subject": subject,
        "review_checks": checks,
        "goals": dict(goals),
        "changes": [entry for _identity, _key, entry in pending],
    }
    questions = {
        f"change_{index}": {
            "type": "noul",
            "instructions": prompt["instructions"].format(subject=subject, index=index),
            "criteria": prompt["criteria"],
        }
        for index in range(len(pending))
    }
    client = _client()
    ask = caller if caller is not None else client.ask
    try:
        payload = ask(state, questions, env_path=client.credential_path())
        answers = payload["answers"]
        model = payload.get("model")
    except Exception as exc:  # noqa: BLE001 - every failure leaves the caller's rule
        for identity, _key, _entry in pending:
            verdicts[identity] = Verdict(
                None, None, "unjudged", f"jev-error: {type(exc).__name__}"
            )
        return verdicts
    for index, (identity, key, _entry) in enumerate(pending):
        answer = answers.get(f"change_{index}") if isinstance(answers, dict) else None
        value = answer.get("noul") if isinstance(answer, dict) else None
        if not _is_probability(value):
            verdicts[identity] = Verdict(
                None, None, "unjudged", "jev answer is not a probability"
            )
            continue
        _write_cached(key, float(value), model if isinstance(model, str) else None)
        verdicts[identity] = Verdict(float(value) >= threshold, float(value), "jev")
    return verdicts
