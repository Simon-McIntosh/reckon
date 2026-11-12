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

A further code review is judged in shadow. The probability that it raises a
blocking finding is recorded against the review run that was dispatched, with
the diff, goals and earlier findings it was given, and the review's own outcome
is recorded beside it once the review is stored. Every re-review is dispatched
whatever the judgement says: the record exists so that a decision to skip some
re-reviews can be taken on measured rows rather than on a rule, and the
:func:`shadow_view` counts what each threshold would have cost.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import math
import re
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
# A further code review of work a review already read. The question is not
# whether the change needs a review — that one is asked of a plan edit — but how
# likely it is that reviewing the change raises a finding that blocks the work.
REREVIEW_SUBJECT = "re-review"

# The checklist and the wording of the re-review question. It is held here
# rather than in the shared prompt file so the probability question a re-review
# asks cannot be confused with the yes-or-no question the plan-edit reflex
# asks: this one is only ever recorded, never acted on.
_REREVIEW_PROMPT: dict[str, Any] = {
    "instructions": (
        "A code review of the work described by `goals` already ran, at the "
        "revision before the change shown in `changes[{index}].diff`. That "
        "change is what the work has gained since. Decide the probability that "
        "a further review of the work as it now stands, read against "
        "`review_checks`, would raise a finding that blocks landing. Judge what "
        "the change alters and what the prior review found. Return a "
        "probability between 0 and 1."
    ),
    "criteria": {
        "high": (
            "The change alters what the work does, decides, depends on or "
            "measures in a way the earlier review never read: a changed "
            "mechanism, interface, dependency, scope or measure, or new "
            "behaviour with no test that would fail without it."
        ),
        "low": (
            "The change settles what the earlier review already read or "
            "recorded: a repair answering a finding the review named, wording, "
            "formatting, evidence or bookkeeping with the same meaning, or a "
            "restatement of content already reviewed."
        ),
    },
    "checks": {
        "re-review": [
            "the change does what the run's goal states and its done-when measures",
            "nothing outside the goal regresses and no contract breaks",
            "the tests prove the behaviour, with a negative control that fails without it",
            "the change stays within the run's declared write paths",
            "names and comments follow the repository's rules",
        ]
    },
}

# The largest diff the judge is shown whole. A change past it is not a minor
# edit of reviewed content, so it is left to the caller's rule rather than
# judged from a truncated view that could hide the part that matters.
MAX_DIFF_CHARS = 12_000


@dataclass(frozen=True)
class Change:
    """One reviewed unit's text as the review read it and as it stands now.

    ``identity`` names the unit to the caller and never reaches the model.
    ``goal`` is what the unit itself must achieve, such as a section's
    done-when or a run's goal, beside the goals shared by every change. A
    caller holding a diff already, such as a run's ``git diff``, passes it as
    ``diff`` and the two texts are not compared.
    """

    identity: str
    reviewed: str
    present: str
    goal: str = ""
    diff: str = ""


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
def _shared_prompt() -> dict[str, Any]:
    return json.loads(PROMPT_PATH.read_text(encoding="utf-8"))


def _prompt_for(subject: str) -> dict[str, Any]:
    """The question a review of ``subject`` is asked, and its checklist.

    The re-review question ships with the judge rather than with the shared
    prompt file, because it asks for a probability about a review's own outcome
    rather than a yes-or-no about a review being owed, and the two must not be
    edited as one text.
    """
    return _REREVIEW_PROMPT if subject == REREVIEW_SUBJECT else _shared_prompt()


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
    prompt = _prompt_for(subject)
    payload = json.dumps(
        {
            "model": _client().JEV_MODEL,
            "prompt": prompt["instructions"],
            "criteria": prompt["criteria"],
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
    prompt = _prompt_for(subject)
    checks = list(prompt["checks"][subject])
    verdicts: dict[str, Verdict] = {}
    pending: list[tuple[str, str, dict[str, str]]] = []
    for change in changes:
        diff = change.diff or unified_diff(change.reviewed, change.present)
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


# ── The shadow record of what a re-review was predicted to find ──────────────
# Every further code review is judged before it is dispatched and the judgement
# is recorded with what it was given, then the review's own outcome is recorded
# beside it once the review is stored. Nothing is skipped on the judgement: it
# is an observation of what a threshold would have cost, and a threshold becomes
# a skip rule only once enough judged rows with outcomes exist to show what it
# would have dropped.
SHADOW_ROOT_NAME = "review-need-rows"


def predict_blocking(
    *,
    goal: str,
    done_when: str,
    diff: str,
    prior: Mapping[str, Any],
    caller: Callable[..., dict[str, Any]] | None = None,
) -> Verdict:
    """The probability that reviewing this change raises a blocking finding.

    Asked of the same judge as every other review question, under its own
    subject, so an unanswerable service or an oversized diff comes back with the
    reason and the caller records the prediction as unavailable rather than as
    a low probability. ``prior`` is what the earlier review found; it is passed
    as the change's goals because it is what the new review is measured against.
    """
    identity = str(prior.get("review_run_id") or "") or REREVIEW_SUBJECT
    changes = [
        Change(identity=identity, reviewed="", present="", goal=done_when, diff=diff)
    ]
    goals = {"goal": goal, "done_when": done_when, "prior_review": dict(prior)}
    verdicts = judge(
        changes,
        subject=REREVIEW_SUBJECT,
        goals=goals,
        threshold=0.5,
        caller=caller,
    )
    return verdicts.get(
        identity, Verdict(None, None, "unjudged", "the judge returned no verdict")
    )


#: The thresholds the shadow view reports at. A re-review whose probability is
#: below one of them would have been skipped had that threshold been in force.
SHADOW_THRESHOLDS: tuple[float, ...] = (0.25, 0.5)

#: The number of judged re-reviews carrying an outcome at which a threshold may
#: be read as a skip rule. Below it the counts describe too few reviews for the
#: cost of a skip to be distinguishable from the run of luck around it.
SHADOW_ROWS_FOR_DECISION = 100

_SAFE_ROW_ID = re.compile(r"[^A-Za-z0-9._-]+")


def _shadow_root() -> Path:
    return _store._config_home() / "crew" / SHADOW_ROOT_NAME


def _shadow_path(project: str, review_run_id: str) -> Path:
    name = _SAFE_ROW_ID.sub("-", str(review_run_id)).strip("-.")
    return _shadow_root() / str(project) / f"{name}.json"


def _read_shadow_row(path: Path) -> dict[str, Any] | None:
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return row if isinstance(row, dict) else None


def record_prediction(
    *,
    project: str,
    review_run_id: str,
    reviewed_run_id: str,
    verdict: Verdict,
    inputs: Mapping[str, Any],
) -> Path | None:
    """Record the judgement a re-review was dispatched on, with its inputs.

    ``verdict.probability`` is written only when the judge answered, so a row
    that records no probability says why rather than reading as a prediction of
    zero. The inputs travel with it, because a probability whose question cannot
    be reconstructed is not a measurement of anything.
    """
    row = {
        "project": str(project),
        "review_run_id": str(review_run_id),
        "reviewed_run_id": str(reviewed_run_id),
        "subject": REREVIEW_SUBJECT,
        "predicted_at": time.time(),
        "available": verdict.probability is not None,
        "probability": verdict.probability,
        "source": verdict.source,
        "reason": verdict.reason,
        "inputs": json.loads(json.dumps(dict(inputs), default=str)),
        "outcome": None,
    }
    path = _shadow_path(project, review_run_id)
    try:
        _store.write_json_atomically(path, row, indent=2, sort_keys=True, fsync=False)
    except OSError:
        # The prediction is an observation. A store that cannot hold it costs
        # the measurement that row and changes nothing about the review.
        return None
    return path


def record_outcome(
    *,
    project: str,
    review_run_id: str,
    outcome: str,
    findings: Mapping[str, Any] | None = None,
) -> Path | None:
    """Record what the review found beside the judgement it was dispatched on.

    Only a row that already carries a judgement is amended: an outcome with no
    prediction to sit beside is not a shadow row, and writing one would make the
    view's counts depend on reviews the shadow never judged.
    """
    path = _shadow_path(project, review_run_id)
    row = _read_shadow_row(path)
    if row is None:
        return None
    row["outcome"] = {
        "verdict": str(outcome),
        "at": time.time(),
        **{str(key): value for key, value in (findings or {}).items()},
    }
    try:
        _store.write_json_atomically(path, row, indent=2, sort_keys=True, fsync=False)
    except OSError:
        return None
    return path


def recorded_rows(project: str) -> list[dict[str, Any]]:
    """Every shadow row recorded for one project, in dispatch order."""
    directory = _shadow_root() / str(project)
    if not directory.is_dir():
        return []
    rows = [
        row
        for row in (_read_shadow_row(path) for path in sorted(directory.glob("*.json")))
        if row is not None
    ]
    rows.sort(
        key=lambda row: (
            float(row.get("predicted_at") or 0.0),
            row.get("review_run_id"),
        )
    )
    return rows


def shadow_view(
    project: str, *, thresholds: Sequence[float] = SHADOW_THRESHOLDS
) -> dict[str, Any]:
    """Report what each threshold would have skipped, over the recorded rows.

    A row is skipped at a threshold when the judge answered and the probability
    fell below it. Its outcome says what that skip would have cost: a blocking
    finding lost, or a clean review saved. A row whose outcome has not been
    recorded yet is counted as pending rather than as either, so an unmeasured
    row can never read as a clean one.
    """
    rows = recorded_rows(project)
    predicted = [
        row
        for row in rows
        if row.get("available") and row.get("probability") is not None
    ]
    counts: dict[str, dict[str, int]] = {}
    for threshold in thresholds:
        skipped = [
            row for row in predicted if float(row["probability"]) < float(threshold)
        ]
        blocking = sum(1 for row in skipped if _outcome_verdict(row) == "blocking")
        clean = sum(1 for row in skipped if _outcome_verdict(row) == "clean")
        counts[_threshold_key(threshold)] = {
            "skipped": len(skipped),
            "skipped_blocking": blocking,
            "skipped_clean": clean,
            "skipped_pending": len(skipped) - blocking - clean,
            "kept": len(predicted) - len(skipped),
        }
    with_outcome = [
        row for row in predicted if _outcome_verdict(row) in {"blocking", "clean"}
    ]
    return {
        "project": project,
        "rows": rows,
        "totals": {
            "recorded": len(rows),
            "predicted": len(predicted),
            "unavailable": len(rows) - len(predicted),
            "awaiting_outcome": len(predicted) - len(with_outcome),
            "blocking": sum(
                1 for row in with_outcome if _outcome_verdict(row) == "blocking"
            ),
            "clean": sum(1 for row in with_outcome if _outcome_verdict(row) == "clean"),
        },
        "thresholds": counts,
        "rows_needed": max(0, SHADOW_ROWS_FOR_DECISION - len(with_outcome)),
    }


def _threshold_key(threshold: float) -> str:
    return f"{float(threshold):.2f}"


def _outcome_verdict(row: Mapping[str, Any]) -> str:
    outcome = row.get("outcome")
    if not isinstance(outcome, Mapping):
        return ""
    return str(outcome.get("verdict") or "")
