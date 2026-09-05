"""Launch translation — the only module that speaks a worker harness's dialect.

Everything above this file states *what* a worker should do; this file decides
*how* one is started and observed. It turns a resolved flight config plus a node
into a concrete argument vector, and turns the resulting machine-readable event
stream back into one normalised observation.

That containment is the point. Because per-backend flags live here and nowhere
else, no skill, plan or prompt can name a harness flag, so two execution paths
cannot drift apart by wording — a difference between backends is either in this
file or it does not exist. It is also why this is the one module allowed to
contain a harness's vocabulary: the ban on naming providers and models applies
to the surfaces an agent reads, and translation is not one of them.

A dialect is selected by the backend's ``command``, which is user data from the
config file rather than a schema-fixed name. Adding a harness adds a dialect
here; it adds no branch anywhere else, because callers branch only on
``launch`` kind — an external process reckon can spawn, or the calling harness's
own delegation primitive, which it cannot.

Three things every dialect must supply, all verified against streams recorded
from live runs (``tests/fixtures/backends/``):

    argument construction   including sandbox tier, model, effort and worktree
    session capture         the resumable id, so a worker outlives its workspace
    stream interpretation   terminal event, final message, budget signal,
                            and how fast the worker is generating

Budget is deliberately asymmetric and must stay that way. One harness reports
utilisation and a reset time on its run stream; another reports only tokens
spent there, with no headroom at all. So an observation carries
``headroom: "unknown"`` rather than a guess, and :func:`budget_exhausted`
answers ``None`` where nothing is known. Absence of a signal is never read as
exhaustion. The converse holds too: a harness that *refuses* a turn because the
account is spent is stating headroom, in prose rather than in a field, and
folding that refusal to unknown reported a clear backend for six days while it
was exhausted. A recognised refusal is therefore a measurement; an
unrecognised failure still is not.

The asymmetry is in the *stream*, not necessarily in the harness: a dialect may
also own a probe (:func:`probe_budget`) that asks the harness's own account
surface what remains. Such a read costs no worker budget and runs no model, so
it can serve a free pre-flight — but it spawns a process, so it happens only for
a backend whose config asks for it. A dialect with no such surface returns no
probe, and the answer stays honestly unknown.
"""

from __future__ import annotations

import json
import queue
import re
import subprocess
import tempfile
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Sandbox tiers named by the flight schema. The mapping to concrete flags is
# per-dialect; the tier names are shared vocabulary.
READ_ONLY = "read-only"
WORKSPACE_WRITE = "workspace-write"
WORKTREE_FULL = "worktree-full"


def sandbox_write_roots(
    backend: Mapping[str, Any],
    *,
    repository: str | Path,
    run_directory: str | Path,
    reports_directory: str | Path,
    manifest_path: str | Path | None = None,
) -> tuple[Path, ...] | None:
    """Return writable roots for a resolved sandbox, or None if unrestricted."""
    tier = str(backend.get("sandbox") or READ_ONLY)
    if tier == WORKTREE_FULL:
        return None
    roots = {
        Path(run_directory).expanduser().resolve(),
        Path(reports_directory).expanduser().resolve(),
        Path(tempfile.gettempdir()).expanduser().resolve(),
    }
    if manifest_path:
        manifest = Path(manifest_path).expanduser()
        if manifest.is_absolute():
            roots.add(manifest.resolve().parent)
    if tier == WORKSPACE_WRITE:
        roots.add(Path(repository).expanduser().resolve())
    return tuple(sorted(roots, key=lambda path: path.as_posix()))


def sandbox_can_write(
    path: str | Path,
    *,
    repository: str | Path,
    write_roots: tuple[Path, ...] | None,
) -> bool:
    """Return whether a declared path is reachable through resolved grants."""
    if write_roots is None:
        return True
    repository_root = Path(repository).expanduser().resolve()
    raw = Path(path).expanduser()
    resolved = (raw if raw.is_absolute() else repository_root / raw).resolve()
    return any(resolved.is_relative_to(root) for root in write_roots)


class BackendError(Exception):
    """A backend cannot be translated into a runnable invocation.

    Raised for a backend whose command has no dialect, for a launch kind that
    cannot be spawned, and for a missing command — never for a *reported*
    condition such as an unavailable binary, which the caller decides about.
    """


# ── Normalised observation ──────────────────────────────────────────────────


@dataclass
class Observation:
    """What a worker's event stream says, in backend-independent terms.

    ``phase`` is derived rather than stored: no events means the process has not
    reported yet, a terminal event means it finished, and anything between is
    work in progress. A stream that stops without a terminal event therefore
    reads ``working`` forever, which is correct — only the process table can
    distinguish a slow worker from a dead one, and that is the caller's job.

    ``throughput`` is what makes a slow worker distinguishable from a stuck one.
    Liveness read from the age of the log answers only whether bytes arrived, so
    it reports a model producing two tokens a second and a model producing none
    identically. The block folds the token counts the stream already carries into
    a rate, keeps generation apart from tool wait because wall clock on a node
    running its own test suite is mostly the suite, and states peak input against
    the usable window so a run approaching a ceiling is visible before it hits
    one rather than after.
    """

    backend: str
    session_id: str | None = None
    phase: str = "starting"
    terminal: bool = False
    exit_status: str | None = None
    final_message: str | None = None
    budget: dict[str, Any] = field(default_factory=dict)
    throughput: dict[str, Any] = field(default_factory=dict)
    events: int = 0
    malformed_lines: int = 0
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Return the observation as sorted JSON-ready data."""
        return {
            "backend": self.backend,
            "budget": dict(sorted(self.budget.items())),
            "detail": self.detail,
            "events": self.events,
            "exit_status": self.exit_status,
            "final_message": self.final_message,
            "malformed_lines": self.malformed_lines,
            "phase": self.phase,
            "session_id": self.session_id,
            "terminal": self.terminal,
            "throughput": dict(sorted(self.throughput.items())),
        }


@dataclass(frozen=True)
class LaunchPlan:
    """A runnable invocation: what to execute, where, and what to feed it."""

    backend: str
    dialect: str
    argv: list[str]
    cwd: str
    stdin_text: str
    environment: dict[str, str]
    final_message_path: str | None
    resumed_session: str | None

    def as_dict(self) -> dict[str, Any]:
        """Return the plan as sorted JSON-ready data, prompt text excluded."""
        return {
            "argv": list(self.argv),
            "backend": self.backend,
            "cwd": self.cwd,
            "dialect": self.dialect,
            "final_message_path": self.final_message_path,
            "resumed_session": self.resumed_session,
        }


@dataclass(frozen=True)
class BudgetProbe:
    """A request-response exchange that asks a harness what headroom remains.

    Modelled as requests written to a held-open stdin rather than as a plain
    command because that is what the one harness offering such a surface needs:
    it serves the answer over a line protocol and exits the moment its input
    closes, so a naive ``command | read`` returns nothing at all.
    """

    argv: list[str]
    requests: list[dict[str, Any]]
    answer_id: Any
    timeout_seconds: int = 20

    def as_dict(self) -> dict[str, Any]:
        """Return the probe as sorted JSON-ready data."""
        return {
            "answer_id": self.answer_id,
            "argv": list(self.argv),
            "requests": [dict(sorted(request.items())) for request in self.requests],
            "timeout_seconds": self.timeout_seconds,
        }


def unknown_budget(reason: str) -> dict[str, Any]:
    """Return a budget block that admits it knows nothing about headroom."""
    return {
        "headroom": "unknown",
        "utilisation_pct": None,
        "rate_limit_type": None,
        "rate_limit_period_minutes": None,
        "resets_at": None,
        "threshold_status": None,
        "surpassed_threshold": None,
        "tokens": None,
        "cost_usd": None,
        "refusal": False,
        "detail": reason,
    }


def unknown_throughput(reason: str) -> dict[str, Any]:
    """Return a throughput block that admits it measured no rate."""
    return {
        "generated_tokens": None,
        "generation_seconds": None,
        "machine_seconds": None,
        "elapsed_seconds": None,
        "tokens_per_second": None,
        "wall_tokens_per_second": None,
        "peak_input_tokens": None,
        "input_budget_tokens": None,
        "input_utilisation_pct": None,
        "detail": reason,
    }


def _rate(tokens: Any, seconds: Any) -> float | None:
    """Return tokens per second, or None when either side is not measured."""
    if not isinstance(tokens, (int, float)) or isinstance(tokens, bool):
        return None
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
        return None
    if seconds <= 0:
        return None
    return round(float(tokens) / float(seconds), 2)


def _percent(part: Any, whole: Any) -> float | None:
    """Return part as a percentage of whole, or None when either is missing."""
    if not isinstance(part, (int, float)) or isinstance(part, bool):
        return None
    if not isinstance(whole, (int, float)) or isinstance(whole, bool):
        return None
    if whole <= 0:
        return None
    return round(100.0 * float(part) / float(whole), 1)


def throughput_block(
    *,
    generated_tokens: int | None,
    generation_seconds: float | None,
    elapsed_seconds: float | None,
    peak_input_tokens: int | None,
    input_budget_tokens: int | None,
    detail: str,
) -> dict[str, Any]:
    """Fold measured tokens and spans into the shared throughput block.

    Two rates rather than one, because they answer different questions. The
    generation rate says how fast the model emits when it is emitting, which is
    the model's speed; the wall-clock rate says how fast the node is progressing,
    which is what a fence is spent against. A node that runs its own suite has a
    wall rate far below its generation rate, and reading either as the other
    misattributes the workstation's load to the model or the reverse.
    """
    block = unknown_throughput(detail)
    machine_seconds = None
    if (
        isinstance(elapsed_seconds, (int, float))
        and isinstance(generation_seconds, (int, float))
        and elapsed_seconds >= generation_seconds
    ):
        machine_seconds = round(float(elapsed_seconds) - float(generation_seconds), 3)
    block.update(
        {
            "generated_tokens": generated_tokens,
            "generation_seconds": generation_seconds,
            "machine_seconds": machine_seconds,
            "elapsed_seconds": elapsed_seconds,
            "tokens_per_second": _rate(generated_tokens, generation_seconds),
            "wall_tokens_per_second": _rate(generated_tokens, elapsed_seconds),
            "peak_input_tokens": peak_input_tokens,
            "input_budget_tokens": input_budget_tokens,
            "input_utilisation_pct": _percent(peak_input_tokens, input_budget_tokens),
        }
    )
    return block


def budget_exhausted(budget: Mapping[str, Any] | None) -> bool | None:
    """Answer whether the budget is spent: True, False, or None for unknown.

    ``None`` is the whole reason this function exists. A backend that reports no
    headroom produces a block indistinguishable, on any single field, from one
    that reports plenty — so a caller reading fields directly can conclude
    "empty" from silence. Routing a run on that inference stops work that had
    budget left, so unknown stays unknown here and the caller must handle it.
    """
    if not budget or budget.get("headroom") != "known":
        return None
    utilisation = budget.get("utilisation_pct")
    if utilisation is None:
        return None
    return float(utilisation) >= 100.0


# A harness that refuses a turn for want of budget says so in prose on its error
# event rather than in a field. Both recorded spellings of a usage refusal state
# the limit and then name the moment it lifts; the second also names the model,
# which this module must not record, so only the two load-bearing parts are
# matched. A spend-ceiling refusal is a different surface from a usage window —
# measured 2026-09-03, an account crossed its spend limit with rate-limit
# utilisation still low — so it is matched and named separately rather than
# folded into the same limit kind.
_USAGE_LIMIT_PHRASE = re.compile(r"hit your usage limit", re.IGNORECASE)
_SPEND_LIMIT_PHRASE = re.compile(r"hit your (?:individual )?spend limit", re.IGNORECASE)
_LIMIT_PHRASES = (
    ("usage-limit", _USAGE_LIMIT_PHRASE),
    ("spend-limit", _SPEND_LIMIT_PHRASE),
)
_RESET_PHRASE = re.compile(
    r"try again at\s+"
    r"(?P<month>[A-Za-z]{3,9})\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+"
    r"(?P<year>\d{4}),?\s+(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<meridiem>[AaPp][Mm])",
    re.IGNORECASE,
)


def _reset_moment_to_iso(text: str) -> str | None:
    """Read the reset moment out of a refusal message, or None.

    The moment is written for a person — an abbreviated month, an ordinal day,
    a twelve-hour clock, no zone — so it is read as local wall clock and stamped
    with this machine's offset. Stamping it as UTC instead would move the hold's
    expiry by the offset, which either releases a wave early or holds it late.
    """
    match = _RESET_PHRASE.search(text)
    if match is None:
        return None
    for month_format in ("%b", "%B"):
        try:
            # Naive on purpose: the message carries no zone, so the moment is
            # local wall clock and is made aware immediately below.
            moment = datetime.strptime(  # noqa: DTZ007
                f"{match['month'][:3] if month_format == '%b' else match['month']} "
                f"{match['day']} {match['year']} "
                f"{match['hour']}:{match['minute']} {match['meridiem'].upper()}",
                f"{month_format} %d %Y %I:%M %p",
            )
        except ValueError:
            continue
        local = moment.astimezone()
        return local.isoformat(timespec="seconds").replace("+00:00", "Z")
    return None


def refusal_budget(text: str) -> dict[str, Any] | None:
    """Turn a quota refusal message into a budget block, or decline.

    Declining is the important half. Only a message naming a recognised limit
    phrase is read as exhaustion; an ordinary failed turn — a bad model id, a
    lost stream, a context overflow — carries none of them and must leave the
    budget exactly as unknown as it was, because a failure read as exhaustion
    holds every later wave on evidence that was never a measurement. The reset
    is read where the message states one; where it does not (a spend-ceiling
    refusal names only a time of day, with no date to anchor it), it is left
    unset rather than guessed, and the caller states that plainly as unknown.
    """
    limit_kind = next(
        (kind for kind, phrase in _LIMIT_PHRASES if phrase.search(text)), None
    )
    if limit_kind is None:
        return None
    budget = unknown_budget("")
    budget.update(
        {
            "headroom": "known",
            # The account refused work outright, so there is no partial figure to
            # report: the window is spent until it resets. Recorded as a full
            # utilisation because that is what every reader of this block already
            # compares against a ceiling.
            "utilisation_pct": 100.0,
            "rate_limit_type": limit_kind,
            "resets_at": _reset_moment_to_iso(text),
            "threshold_status": "exhausted",
            "surpassed_threshold": True,
            "refusal": True,
            "detail": f"backend refused the turn: the account's {limit_kind} is reached",
        }
    )
    return budget


def _epoch_to_iso(value: Any) -> str | None:
    """Convert an epoch-seconds reset time to UTC ISO-8601, or None."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    try:
        return (
            datetime.fromtimestamp(seconds, tz=timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
    except (OverflowError, OSError, ValueError):
        return None


def _is_rate_limit_retry(event: Mapping[str, Any]) -> bool:
    """Whether a ``system/api_retry`` record names rate limiting as its cause.

    The local lane reports a spent consumer on retry records rather than on
    ``rate_limit_event``: each carries ``error: "rate_limit"`` beside
    ``error_status: 429``. A retry for another cause (a 529 server overload) is
    capacity, not a spent lane, and must not count toward exhaustion.
    """
    return (
        event.get("type") == "system"
        and event.get("subtype") == "api_retry"
        and (
            str(event.get("error") or "").casefold() == "rate_limit"
            or event.get("error_status") == 429
        )
    )


def _retry_refusal_budget(budget: Mapping[str, Any], retries: int) -> dict[str, Any]:
    """Record the rate-limit exhaustion shape as a refusal block.

    The retries are the magnitude and the terminal error result is the verdict.
    No retry record carries a reset moment, so the block states the limit and
    the observed count and leaves the reset unset — the same honest absence a
    spend-limit refusal with no reset records.
    """
    block = dict(budget)
    block.update(
        {
            "headroom": "known",
            "utilisation_pct": 100.0,
            "rate_limit_type": "rate-limit",
            "resets_at": None,
            "threshold_status": "exhausted",
            "surpassed_threshold": True,
            "refusal": True,
            "detail": (
                f"the run died after {retries} rate-limit retries "
                "(api_retry 429); the lane is exhausted"
            ),
        }
    )
    return block


def _retry_prose(retries: int, *, terminal: bool) -> str:
    """Prose for a retry-bearing stream whose terminal shape is no verdict.

    Retrying is not refusing: busy lanes carry rate-limit retries and complete,
    so the count alone must never hold a wave. The count is surfaced anyway,
    because a stream that recorded it must not report that it carries no
    rate-limit signal at all.
    """
    if terminal:
        return (
            f"run completed after {retries} rate-limit retries; "
            "retries alone are not a refusal"
        )
    return (
        f"run in flight after {retries} rate-limit retries; "
        "no terminal result yet, and retries alone are not a refusal"
    )


# ── Dialects ────────────────────────────────────────────────────────────────


class Dialect:
    """Translation for one harness command.

    Subclasses own three things and share nothing else: the flags that express a
    sandbox tier, model and effort; the shape of a resume invocation; and the
    event vocabulary of the stream. Anything a subclass would state twice
    belongs on this base class instead.
    """

    name = ""
    # Whether the harness needs its prompt on stdin. Both probed harnesses do,
    # and for the same reason: a prompt passed as an argument can be swallowed
    # by a preceding variadic option, which fails as "no input provided" with
    # the prompt sitting in the argument list.
    stdin_prompt = True

    def argv(
        self,
        *,
        command: str,
        backend: Mapping[str, Any],
        worktree: str,
        working_directory: str,
        writable_directories: Iterable[str] = (),
        final_message_path: str | None,
        resume_session: str | None,
    ) -> list[str]:
        raise NotImplementedError

    def working_directory(
        self,
        *,
        backend: Mapping[str, Any],
        worktree: str,
        manifest_path: str | None,
    ) -> str:
        """Return the process directory for this dialect and sandbox tier."""
        return worktree

    def observe(
        self,
        events: Iterable[Mapping[str, Any]],
        *,
        elapsed_seconds: float | None = None,
    ) -> Observation:
        """Fold a stream into one observation.

        ``elapsed_seconds`` is the caller's wall clock for the run, offered
        because not every dialect reports a span of its own. A dialect that does
        report one prefers its own figure and ignores this.
        """
        raise NotImplementedError

    def _sandbox_flags(self, tier: str | None) -> list[str]:
        raise NotImplementedError

    def budget_probe(self, command: str) -> BudgetProbe | None:
        """Return the exchange that reads remaining headroom, or None.

        None is the honest default: a harness that publishes no account surface
        must not be probed with a guessed one, because a probe that fails looks
        the same to a caller as a harness reporting plenty.
        """
        return None

    def read_probe(self, response: Mapping[str, Any]) -> dict[str, Any]:
        """Fold a probe's answer into the shared budget block."""
        return unknown_budget("dialect declares no budget probe to interpret")

    def classify_stream_failure(
        self,
        events: Sequence[Mapping[str, Any]],
        *,
        process_exited: bool,
        diff_present: bool,
        manifest_present: bool,
    ) -> str | None:
        """Classify a recognised harness failure, or decline to guess."""

        return None


class _CodexDialect(Dialect):
    """codex-cli: `exec --json`, thread ids, token usage without headroom."""

    name = "codex"

    def argv(
        self,
        *,
        command: str,
        backend: Mapping[str, Any],
        worktree: str,
        working_directory: str,
        writable_directories: Iterable[str] = (),
        final_message_path: str | None,
        resume_session: str | None,
    ) -> list[str]:
        argv = [command, "exec", "--json", "-C", working_directory]
        argv += self._sandbox_flags(backend.get("sandbox"))
        if backend.get("sandbox") in (READ_ONLY, WORKSPACE_WRITE):
            working_root = Path(working_directory).resolve()
            for directory in writable_directories:
                root = Path(directory).resolve()
                if root == working_root or root.is_relative_to(working_root):
                    continue
                argv += ["--add-dir", str(root)]
        model = backend.get("model")
        if model:
            argv += ["-m", str(model)]
        effort = backend.get("effort")
        if effort:
            argv += ["-c", f"model_reasoning_effort={effort}"]
        if final_message_path:
            argv += ["-o", final_message_path]
        if resume_session:
            # Every option above belongs to `exec`, not to its `resume`
            # subcommand, so they must precede it. Passing the working directory
            # after `resume` is rejected outright — the subcommand takes only a
            # session id and a prompt.
            argv += ["resume", resume_session]
        # Trailing "-" is how this harness is told the prompt arrives on stdin.
        argv.append("-")
        return argv

    def working_directory(
        self,
        *,
        backend: Mapping[str, Any],
        worktree: str,
        manifest_path: str | None,
    ) -> str:
        if backend.get("sandbox") != READ_ONLY:
            return worktree
        if not manifest_path:
            raise BackendError(
                "the read-only sandbox tier needs an absolute manifest path so "
                "its delivery directory can be the writable workspace"
            )
        delivery = Path(manifest_path)
        if not delivery.is_absolute():
            raise BackendError(
                "the read-only sandbox tier needs an absolute manifest path; "
                f"got {manifest_path!r}"
            )
        return str(delivery.parent)

    def _sandbox_flags(self, tier: str | None) -> list[str]:
        if tier == WORKTREE_FULL:
            # The worktree is the blast-radius boundary, so the process itself
            # runs unsandboxed: a sandbox is inherited by child processes and
            # breaks the test runners and builds a worker's gate depends on.
            return ["--dangerously-bypass-approvals-and-sandbox"]
        if tier == READ_ONLY:
            return ["--sandbox", WORKSPACE_WRITE, "--skip-git-repo-check"]
        if tier == WORKSPACE_WRITE:
            return ["--sandbox", WORKSPACE_WRITE]
        return ["--sandbox", READ_ONLY]

    def observe(
        self,
        events: Iterable[Mapping[str, Any]],
        *,
        elapsed_seconds: float | None = None,
    ) -> Observation:
        obs = Observation(backend=self.name, budget=unknown_budget(""))
        message: str | None = None
        usage: Mapping[str, Any] | None = None
        for event in events:
            obs.events += 1
            kind = event.get("type")
            if kind == "thread.started":
                obs.session_id = event.get("thread_id") or obs.session_id
            elif kind == "item.completed":
                item = event.get("item")
                if isinstance(item, Mapping) and item.get("type") == "agent_message":
                    message = item.get("text") or message
            elif kind == "turn.completed":
                obs.terminal = True
                obs.exit_status = "ok"
                usage = (
                    event.get("usage")
                    if isinstance(event.get("usage"), Mapping)
                    else usage
                )
                obs.budget = self._budget(event.get("usage"))
            elif kind in ("turn.failed", "thread.error", "error", "stream.error"):
                obs.terminal = True
                obs.exit_status = "error"
                obs.detail = _error_detail(event)
                refused = refusal_budget(obs.detail)
                if refused is not None:
                    obs.budget = refused
        obs.final_message = message
        obs.throughput = self._throughput(usage, elapsed_seconds)
        obs.phase = _phase(obs)
        return obs

    def _throughput(
        self, usage: Mapping[str, Any] | None, elapsed_seconds: float | None
    ) -> dict[str, Any]:
        """Rate this harness's turn against the caller's clock.

        This stream reports what a turn consumed but not how long it took, so the
        span has to come from the caller. Without one there is no rate — the
        token counts alone cannot say whether they took a minute or an hour, and
        that distinction is the whole question.
        """
        generated = peak_input = None
        if isinstance(usage, Mapping):
            generated = _sum_tokens(usage, ("output_tokens", "reasoning_output_tokens"))
            peak_input = _sum_tokens(usage, ("input_tokens", "cached_input_tokens"))
        elif elapsed_seconds is None:
            return unknown_throughput("no completed turn to measure yet")
        if elapsed_seconds is None:
            detail = "turn tokens recorded, but no span was supplied to rate them"
        elif generated is None:
            detail = "the run has a span but no completed turn to rate against it"
        else:
            detail = (
                "rated against the caller's wall clock; this stream reports "
                "tokens without a span"
            )
        return throughput_block(
            generated_tokens=generated,
            # This harness separates neither inference from tool wait nor the
            # turn from the run, so claiming a generation span would be inventing
            # one. The wall rate is the honest figure it can support.
            generation_seconds=None,
            elapsed_seconds=elapsed_seconds,
            peak_input_tokens=peak_input,
            input_budget_tokens=None,
            detail=detail,
        )

    def classify_stream_failure(
        self,
        events: Sequence[Mapping[str, Any]],
        *,
        process_exited: bool,
        diff_present: bool,
        manifest_present: bool,
    ) -> str | None:
        """Recognise a wrapper stream that ended before producing any work."""

        kinds = [str(event.get("type") or "") for event in events]
        turn_started = "turn.started" in kinds
        turn_finished = any(kind in {"turn.completed", "turn.failed"} for kind in kinds)
        if (
            process_exited
            and turn_started
            and not turn_finished
            and not diff_present
            and not manifest_present
        ):
            return "infrastructure-failure"
        return None

    def _budget(self, usage: Any) -> dict[str, Any]:
        """Record spent tokens, and state plainly that headroom is not reported.

        This harness's run stream reports what a turn consumed and nothing about
        what remains, so the honest record is tokens plus an unknown headroom. A
        later reader must not mistake the presence of token counts for a budget.
        Headroom is obtainable from this harness — just not here; see
        :meth:`budget_probe`.
        """
        budget = unknown_budget("backend reports token usage but no headroom")
        if isinstance(usage, Mapping):
            budget["tokens"] = {
                key: usage[key] for key in sorted(usage) if usage[key] is not None
            }
        return budget

    def budget_probe(self, command: str) -> BudgetProbe | None:
        """Ask this harness's app server for the account's rate limits.

        The limits this harness shows its own interactive user do exist off the
        non-interactive path — on a different transport. Its `exec` stream has no
        headroom, but its app server answers `account/rateLimits/read` with used
        percentages and reset times, and that read runs no model.

        The handshake is required: the server rejects requests before
        `initialize`. Its input must also stay open, since it stops the moment
        stdin closes — which is why this is a probe rather than a command whose
        output is read.
        """
        return BudgetProbe(
            argv=[command, "app-server"],
            requests=[
                {
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "clientInfo": {
                            "name": "reckon",
                            "title": "reckon pre-flight",
                            "version": "0",
                        }
                    },
                },
                {"id": 2, "method": "account/rateLimits/read", "params": {}},
            ],
            answer_id=2,
        )

    def read_probe(self, response: Mapping[str, Any]) -> dict[str, Any]:
        """Turn an account-limits answer into utilisation and a reset time.

        Several metered windows can be reported at once, and the binding one is
        whichever is furthest through — that is the window a wave would run into,
        and its reset is the moment the hold lifts. Only the single-bucket view
        is read: the per-bucket map keys on identifiers this module must not
        record.
        """
        result = response.get("result")
        snapshot = result.get("rateLimits") if isinstance(result, Mapping) else None
        if not isinstance(snapshot, Mapping):
            return unknown_budget("account-limit answer carried no rate limits")
        windows = [
            (key, window)
            for key in ("primary", "secondary")
            if isinstance(window := snapshot.get(key), Mapping)
            and isinstance(window.get("usedPercent"), (int, float))
            and not isinstance(window.get("usedPercent"), bool)
        ]
        if not windows:
            return unknown_budget("account limits reported no metered window")
        window_type, binding = max(
            windows, key=lambda item: float(item[1]["usedPercent"])
        )
        budget = unknown_budget("")
        budget.update(
            {
                "headroom": "known",
                "utilisation_pct": float(binding["usedPercent"]),
                "rate_limit_type": window_type,
                "rate_limit_period_minutes": binding.get("windowDurationMins"),
                "resets_at": _epoch_to_iso(binding.get("resetsAt")),
                "threshold_status": snapshot.get("rateLimitReachedType"),
                "detail": "backend's account surface reports utilisation and reset time",
            }
        )
        return budget


class _ClaudeDialect(Dialect):
    """Claude Code: `-p` stream-json, session ids, rate-limit headroom."""

    name = "claude"

    def argv(
        self,
        *,
        command: str,
        backend: Mapping[str, Any],
        worktree: str,
        working_directory: str,
        writable_directories: Iterable[str] = (),
        final_message_path: str | None,
        resume_session: str | None,
    ) -> list[str]:
        argv = [command, "-p", "--output-format", "stream-json", "--verbose"]
        if resume_session:
            argv += ["--resume", resume_session]
        model = backend.get("model")
        if model:
            argv += ["--model", str(model)]
        effort = backend.get("effort")
        if effort:
            argv += ["--effort", str(effort)]
        argv += self._sandbox_flags(backend.get("sandbox"))
        # --add-dir is variadic, so it goes last and the prompt goes on stdin;
        # a prompt argument after it is read as another directory. For the
        # read-only tier the worktree is the boundary: the grant admits only
        # the computed write roots, never the repository, so the worker can
        # deliver its declared files without touching the checkout under test.
        add_dirs = list(writable_directories)
        if backend.get("sandbox") != READ_ONLY:
            add_dirs.insert(0, worktree)
        argv += ["--add-dir", *add_dirs]
        return argv

    def _sandbox_flags(self, tier: str | None) -> list[str]:
        if tier == WORKTREE_FULL:
            return ["--dangerously-skip-permissions"]
        if tier == READ_ONLY:
            # A permission mode is the wrong boundary for this tier: `plan`
            # withheld every write, so a node could not deliver its declared
            # files yet still reported a completed turn. Skip approvals
            # outright and let the argv's --add-dir grant admit only the
            # computed write roots, never the repository.
            return ["--dangerously-skip-permissions"]
        return ["--permission-mode", "plan"]

    def observe(
        self,
        events: Iterable[Mapping[str, Any]],
        *,
        elapsed_seconds: float | None = None,
    ) -> Observation:
        obs = Observation(backend=self.name, budget=unknown_budget(""))
        message: str | None = None
        budget = unknown_budget("no rate-limit event in the stream yet")
        throughput = unknown_throughput("no completed result to measure yet")
        peak_input = 0
        rate_limit_retries = 0
        for event in events:
            obs.events += 1
            kind = event.get("type")
            if kind == "system":
                subtype = event.get("subtype")
                if subtype == "init":
                    obs.session_id = event.get("session_id") or obs.session_id
                elif _is_rate_limit_retry(event):
                    # The local lane reports a spent consumer on retry records
                    # rather than on rate_limit_event. The count is the
                    # magnitude; the terminal result below is the verdict.
                    rate_limit_retries += 1
            elif kind == "rate_limit_event":
                budget = self._budget(event.get("rate_limit_info"))
            elif kind == "assistant":
                message = _assistant_text(event) or message
                peak_input = max(peak_input, _prompt_tokens(event))
            elif kind == "result":
                obs.terminal = True
                # Success is read from is_error, never from subtype: a failed
                # turn of this harness carries subtype "success" beside
                # is_error true, so keying off subtype inverts the verdict.
                obs.exit_status = "error" if event.get("is_error") else "ok"
                message = event.get("result") or message
                cost = event.get("total_cost_usd")
                if cost is not None:
                    budget["cost_usd"] = cost
                usage = event.get("usage")
                if isinstance(usage, Mapping):
                    budget["tokens"] = {
                        key: usage[key]
                        for key in sorted(usage)
                        if not isinstance(usage[key], (dict, list))
                    }
                if obs.exit_status == "error":
                    obs.detail = _error_detail(event)
                    refused = refusal_budget(obs.detail)
                    if refused is not None:
                        budget = refused
                throughput = self._throughput(event, peak_input, elapsed_seconds)
            if obs.session_id is None:
                # Every event of this stream carries the session id, including
                # the hook events a host configuration may emit before init.
                obs.session_id = event.get("session_id") or obs.session_id
        if rate_limit_retries:
            # Exhaustion is the terminal shape, never the count: busy lanes
            # carry rate-limit retries and complete, so retrying alone is not
            # refusing. Only a stream whose retries end in an error result
            # records a refusal; one still in flight or one that completed
            # reports the count as a magnitude and no verdict.
            if (
                obs.terminal
                and obs.exit_status == "error"
                and not budget.get("refusal")
            ):
                budget = _retry_refusal_budget(budget, rate_limit_retries)
            elif budget.get("headroom") != "known":
                budget = unknown_budget(
                    _retry_prose(rate_limit_retries, terminal=obs.terminal)
                )
        obs.budget = budget
        obs.final_message = message
        obs.throughput = throughput
        obs.phase = _phase(obs)
        return obs

    def _throughput(
        self,
        result: Mapping[str, Any],
        peak_input: int,
        elapsed_seconds: float | None,
    ) -> dict[str, Any]:
        """Rate a finished run from the spans and totals its result carries.

        The totals are read from the result and never summed from the assistant
        events, which report a message's opening usage rather than its final one
        and are emitted once per content block besides — summing them undercounts
        by roughly two orders of magnitude and does so silently. Peak input is
        the largest single prompt the run sent, which is the figure a context
        window is actually spent against; the cumulative totals on the result are
        the sum over every request and would read far past any window.
        """
        elapsed = _seconds(result.get("duration_ms"))
        if elapsed is None:
            elapsed = elapsed_seconds
        generation = _seconds(result.get("duration_api_ms"))
        model_usage = result.get("modelUsage")
        generated: int | None = None
        window: int | None = None
        if isinstance(model_usage, Mapping):
            per_model = [
                entry for entry in model_usage.values() if isinstance(entry, Mapping)
            ]
            totals = [_number(entry.get("outputTokens")) for entry in per_model]
            measured = [value for value in totals if value is not None]
            generated = int(sum(measured)) if measured else None
            windows = [_number(entry.get("contextWindow")) for entry in per_model]
            usable = [value for value in windows if value]
            # One usable window even when several models ran: the run is held by
            # the smallest, since that is the one a shared prompt overflows first.
            window = int(min(usable)) if usable else None
        if generated is None:
            usage = result.get("usage")
            if isinstance(usage, Mapping):
                generated = _sum_tokens(usage, ("output_tokens",))
        return throughput_block(
            generated_tokens=generated,
            generation_seconds=generation,
            elapsed_seconds=elapsed,
            peak_input_tokens=peak_input or None,
            input_budget_tokens=window,
            detail=(
                "generation and wall clock reported separately by the backend"
                if generation is not None
                else "wall clock only; the backend reported no inference span"
            ),
        )

    def _budget(self, info: Any) -> dict[str, Any]:
        """Parse reported utilisation and reset time into the shared block.

        This dialect declares no probe because it needs none: headroom arrives on
        the run stream every worker already writes, so a pre-flight reading past
        runs learns it for free and a separate process would add nothing.

        The event carries two figures that answer different questions. The
        top-level ``utilization`` is the account's calendar-window position —
        for an overage record its own ``resetsAt`` lands on a month boundary —
        and it reads as a bare fraction that can exceed 1. ``unifiedWindows``
        carries the rate-limit windows a dispatch actually runs into, each with
        its own fractional ``utilization`` and its own ``resetsAt``. Only the
        latter is read; the former is never consulted, so it can never leak
        into ``utilisation_pct`` as a percentage a hundred times too large. The
        binding window is whichever is furthest through, exactly as
        :meth:`_CodexDialect.read_probe` picks the binding account window from
        several reported at once.
        """
        if not isinstance(info, Mapping):
            return unknown_budget("rate-limit event carried no information")
        windows = info.get("unifiedWindows")
        candidates = [
            (period, window)
            for period, window in (windows.items() if isinstance(windows, Mapping) else ())
            if isinstance(window, Mapping)
            and isinstance(window.get("utilization"), (int, float))
            and not isinstance(window.get("utilization"), bool)
        ]
        if not candidates:
            return unknown_budget("rate-limit event carried no unifiedWindows")
        period, binding = max(candidates, key=lambda item: float(item[1]["utilization"]))
        budget = unknown_budget("")
        budget.update(
            {
                "headroom": "known",
                "utilisation_pct": float(binding["utilization"]) * 100.0,
                "rate_limit_type": period,
                "rate_limit_period_minutes": binding.get("windowDurationMins"),
                "resets_at": _epoch_to_iso(binding.get("resetsAt")),
                "threshold_status": info.get("status"),
                "surpassed_threshold": info.get("surpassedThreshold"),
                "detail": "backend reports utilisation and reset time",
            }
        )
        return budget


def _number(value: Any) -> float | None:
    """Return a real number, rejecting the bool that would read as 0 or 1."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _seconds(milliseconds: Any) -> float | None:
    """Convert a reported millisecond span to seconds, or None."""
    value = _number(milliseconds)
    return None if value is None else round(value / 1000.0, 3)


def _sum_tokens(usage: Mapping[str, Any], keys: Sequence[str]) -> int | None:
    """Total the named token counts, or None when none of them was reported."""
    measured = [value for key in keys if (value := _number(usage.get(key))) is not None]
    return int(sum(measured)) if measured else None


def _prompt_tokens(event: Mapping[str, Any]) -> int:
    """Return one request's whole prompt size, cached segments included.

    A cached segment still occupies the window: counting only the uncached input
    reports a two-token prompt for a request carrying a quarter of a million.
    """
    message = event.get("message")
    usage = message.get("usage") if isinstance(message, Mapping) else None
    if not isinstance(usage, Mapping):
        return 0
    total = _sum_tokens(
        usage,
        ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"),
    )
    return int(total or 0)


def _assistant_text(event: Mapping[str, Any]) -> str | None:
    """Extract concatenated text blocks from an assistant message event."""
    message = event.get("message")
    if not isinstance(message, Mapping):
        return None
    blocks = message.get("content")
    if not isinstance(blocks, list):
        return None
    parts = [
        str(block.get("text"))
        for block in blocks
        if isinstance(block, Mapping)
        and block.get("type") == "text"
        and block.get("text")
    ]
    return "\n".join(parts) or None


def _error_detail(event: Mapping[str, Any]) -> str:
    """Summarise a terminal failure event without dragging the payload along.

    ``subtype`` is deliberately not consulted. One harness labels a failed turn
    ``subtype: "success"`` while setting its error flag, so a field that looks
    like a verdict reports the opposite of one.
    """
    for key in ("error", "message", "detail", "result", "terminal_reason"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return _clip(value)
        if isinstance(value, Mapping):
            nested = value.get("message") or value.get("detail")
            if isinstance(nested, str) and nested:
                return _clip(nested)
    return "backend reported a terminal failure"


def _clip(text: str, limit: int = 400) -> str:
    """Bound a harness message so a run record stays readable.

    A failure message can be a whole nested error payload. The record needs
    enough to recognise the failure; the log holds the rest.
    """
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1] + "…"


def _phase(obs: Observation) -> str:
    """Derive the reported phase from what the stream contains.

    A terminal event carrying a recognised provider refusal is ``blocked``
    rather than ``failed``: the harness reports the turn as an error like any
    other, but the account is not broken, only spent until a known or unknown
    moment, and blocked is the state a fleet display can triage and resume
    rather than write off.
    """
    if obs.terminal:
        if obs.exit_status == "ok":
            return "complete"
        if obs.budget.get("refusal"):
            return "blocked"
        return "failed"
    return "working" if obs.events else "starting"


_DIALECTS: dict[str, Dialect] = {
    _CodexDialect.name: _CodexDialect(),
    _ClaudeDialect.name: _ClaudeDialect(),
    # clive wraps `claude` with env vars pointing at the local GPU server
    # (ANTHROPIC_BASE_URL, ANTHROPIC_AUTH_TOKEN, ANTHROPIC_MODEL). Its flags
    # and JSON-lines event stream are identical to claude's since it passes
    # all args through via `exec claude "${ARGS[@]}"`.
    "clive": _ClaudeDialect(),
}


def known_dialects() -> tuple[str, ...]:
    """Return the harness commands this module can translate."""
    return tuple(sorted(_DIALECTS))


def dialect_for(backend: Mapping[str, Any]) -> Dialect:
    """Select a dialect from the backend's command, or say what is missing.

    The command is matched rather than the backend's name because the name is
    free-form user data — a config may call a backend ``fast`` or ``reviewer``
    — while the command is the executable whose flags have to be spoken.
    """
    command = backend.get("command")
    if not command:
        raise BackendError("backend declares launch: cli but names no command")
    stem = Path(str(command)).name
    dialect = _DIALECTS.get(stem)
    if dialect is None:
        known = ", ".join(known_dialects())
        raise BackendError(
            f"no launch translation for command '{stem}'; reckon can translate: {known}"
        )
    return dialect


# ── Public translation surface ──────────────────────────────────────────────


def launch_plan(
    *,
    backend_name: str,
    backend: Mapping[str, Any],
    prompt: str,
    worktree: str | Path,
    manifest_path: str | Path | None = None,
    writable_directories: Iterable[str | Path] = (),
    final_message_path: str | Path | None = None,
    resume_session: str | None = None,
) -> LaunchPlan:
    """Translate one backend plus one node's prompt into a runnable invocation.

    Raises :class:`BackendError` for an in-harness backend: reckon cannot spawn
    the calling harness's own delegation primitive on its behalf, and silently
    substituting a different backend would hide the misrouting.
    """
    launch = backend.get("launch")
    if launch != "cli":
        raise BackendError(
            f"backend '{backend_name}' has launch: {launch!r}; only a 'cli' "
            "backend can be spawned — an in-harness backend is dispatched by "
            "the calling harness against a prepared directive"
        )
    from reckon.flight import expand_backend_environment

    environment = expand_backend_environment(backend_name, backend)
    dialect = dialect_for(backend)
    worktree_path = str(Path(worktree))
    manifest = None if manifest_path is None else str(Path(manifest_path))
    working_directory = dialect.working_directory(
        backend=backend,
        worktree=worktree_path,
        manifest_path=manifest,
    )
    final_path = None if final_message_path is None else str(Path(final_message_path))
    argv = dialect.argv(
        command=str(backend["command"]),
        backend=backend,
        worktree=worktree_path,
        working_directory=working_directory,
        writable_directories=tuple(str(path) for path in writable_directories),
        final_message_path=final_path,
        resume_session=resume_session,
    )
    return LaunchPlan(
        backend=backend_name,
        dialect=dialect.name,
        argv=argv,
        cwd=working_directory,
        stdin_text=prompt if dialect.stdin_prompt else "",
        environment=environment,
        final_message_path=final_path,
        resumed_session=resume_session,
    )


def launch_working_directory(
    *,
    backend: Mapping[str, Any],
    worktree: str | Path,
    manifest_path: str | Path | None = None,
) -> str:
    """Resolve the process directory without constructing a launch plan."""
    dialect = dialect_for(backend)
    manifest = None if manifest_path is None else str(Path(manifest_path))
    return dialect.working_directory(
        backend=backend,
        worktree=str(Path(worktree)),
        manifest_path=manifest,
    )


def probe_budget(
    *,
    backend_name: str,
    backend: Mapping[str, Any],
    runner: Callable[[BudgetProbe], Mapping[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Read a backend's remaining headroom from its own account surface.

    Every way this can fail — no dialect, no probe, no answer, a broken exchange
    — returns an unknown block naming the reason rather than raising. A pre-flight
    must never be stopped by its own instrument: an unreadable probe leaves the
    caller exactly where it was, reading what earlier runs recorded, whereas a
    raised error would turn a missing measurement into a blocked wave.
    """
    try:
        dialect = dialect_for(backend)
    except BackendError as exc:
        return unknown_budget(str(exc))
    probe = dialect.budget_probe(str(backend.get("command") or ""))
    if probe is None:
        return unknown_budget(
            f"backend '{backend_name}' exposes no account-limit surface to read"
        )
    try:
        answer = (runner or run_probe)(probe)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return unknown_budget(f"account-limit read failed to run — {exc}")
    if not isinstance(answer, Mapping):
        return unknown_budget(
            f"account-limit read returned no answer within {probe.timeout_seconds}s"
        )
    return dialect.read_probe(answer)


def run_probe(probe: BudgetProbe) -> dict[str, Any] | None:
    """Run one probe exchange and return the answering object, or None.

    Input is held open for the life of the exchange and the reply is read on a
    thread, because the server this serves streams unrelated notifications
    alongside the answer and exits as soon as its input closes. Reading inline
    would block on whichever arrives first; closing stdin would end the process
    before it answered.
    """
    process = subprocess.Popen(
        probe.argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    answers: queue.Queue[dict[str, Any]] = queue.Queue()

    def read() -> None:
        for line in process.stdout or ():
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict) and parsed.get("id") == probe.answer_id:
                answers.put(parsed)
                return

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        for request in probe.requests:
            process.stdin.write(json.dumps(request) + "\n")
        process.stdin.flush()
        try:
            return answers.get(timeout=probe.timeout_seconds)
        except queue.Empty:
            return None
    finally:
        process.kill()
        process.wait(timeout=5)


def _event_timestamp(event: Mapping[str, Any]) -> datetime | None:
    """Parse one event's own timestamp, or None when it carries none usable."""
    value = event.get("timestamp")
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _requests_tool(event: Mapping[str, Any]) -> bool:
    """Return whether this event asked the machine to run a tool.

    A stream that never names a tool call has no machine span to find, so the
    check stays narrow: only an assistant turn carrying a ``tool_use`` content
    block opens a gap this module reads as machine time. Everything else —
    plain text, a system or result event — is generation or the round trip
    around it.
    """
    if event.get("type") != "assistant":
        return False
    message = event.get("message")
    blocks = message.get("content") if isinstance(message, Mapping) else None
    if not isinstance(blocks, list):
        return False
    return any(
        isinstance(block, Mapping) and block.get("type") == "tool_use"
        for block in blocks
    )


def _machine_seconds_from_events(
    events: Sequence[Mapping[str, Any]],
) -> tuple[float | None, float | None]:
    """Split the stream's own span into generation and machine seconds.

    A gap that opens with a tool call is the machine's time — the span between
    the request and its result — and every other gap is the model's own. This
    reads only the timestamps the stream already carries; no counter inside
    the harness is trusted, per this module's own measured case of one that
    reported zero thinking tokens on a turn that generated forty thinking
    blocks, because it counted the request rather than the response.

    Two usable timestamps are the minimum that measures a gap at all. With
    fewer than two, the split is unknown — never a false zero.
    """
    marks: list[tuple[datetime, bool]] = []
    for event in events:
        timestamp = _event_timestamp(event)
        if timestamp is None:
            continue
        marks.append((timestamp, _requests_tool(event)))
    if len(marks) < 2:
        return None, None
    machine = 0.0
    for (start, waits_on_tool), (end, _next_waits) in zip(marks, marks[1:]):
        gap = (end - start).total_seconds()
        if gap <= 0:
            continue
        if waits_on_tool:
            machine += gap
    total = (marks[-1][0] - marks[0][0]).total_seconds()
    generation = max(0.0, total - machine)
    return round(generation, 3), round(machine, 3)


def _refine_throughput_from_timestamps(
    throughput: dict[str, Any], events: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Prefer a timestamp-measured generation/machine split when the stream has one.

    The dialect's own figure (a backend-reported total, or an elapsed-minus-
    generation fallback) stands untouched when the stream carries fewer than
    two usable timestamps — that is the honest unknown of a stream with no
    timestamps, not a defect in this refinement. When timestamps are usable,
    machine seconds comes from them directly and generation is derived as
    elapsed minus machine, so the two always sum back to elapsed exactly.
    """
    _generation, machine = _machine_seconds_from_events(events)
    if machine is None:
        return throughput
    elapsed = throughput.get("elapsed_seconds")
    resolved_generation = (
        round(float(elapsed) - machine, 3)
        if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool)
        else _generation
    )
    throughput["machine_seconds"] = machine
    throughput["generation_seconds"] = resolved_generation
    throughput["tokens_per_second"] = _rate(
        throughput.get("generated_tokens"), resolved_generation
    )
    return throughput


def parse_events(lines: Iterable[str]) -> tuple[list[dict[str, Any]], int]:
    """Parse a JSON-lines stream, returning the objects and a malformed count.

    A partial trailing line is normal while a worker is still writing, so an
    unparseable line is counted rather than raised — the observation stays
    readable mid-run, which is the whole point of reading the log at all.
    """
    events: list[dict[str, Any]] = []
    malformed = 0
    for line in lines:
        text = line.strip()
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except ValueError:
            malformed += 1
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
        else:
            malformed += 1
    return events, malformed


def observe_stream(
    *,
    backend_name: str,
    backend: Mapping[str, Any],
    lines: Iterable[str],
    elapsed_seconds: float | None = None,
) -> Observation:
    """Fold a backend's recorded event stream into one normalised observation.

    ``elapsed_seconds`` lets a caller that knows when the run started supply the
    span a dialect's own stream may not report, so a rate is available for every
    harness rather than only the one that times itself.
    """
    dialect = dialect_for(backend)
    events, malformed = parse_events(lines)
    obs = dialect.observe(events, elapsed_seconds=elapsed_seconds)
    obs.backend = backend_name
    obs.malformed_lines = malformed
    obs.throughput = _refine_throughput_from_timestamps(obs.throughput, events)
    if obs.phase == "blocked":
        obs.detail = _blocked_detail(obs)
    return obs


def _blocked_detail(obs: Observation) -> str:
    """Name what a triager needs to route around a blocked backend.

    A spend limit is the most triageable stop a fleet can suffer: it says
    exactly what is wrong and exactly when it stops being wrong. Naming the
    backend, the limit kind and the reset beside each other means the
    transition line alone answers the triage question, needing nothing else
    read. The reset is stated as unknown rather than left out when the
    refusal carried none, because an omitted field reads as forgotten rather
    than as absent evidence.
    """
    limit_kind = obs.budget.get("rate_limit_type") or "quota"
    resets_at = obs.budget.get("resets_at") or "unknown"
    return (
        f"backend {obs.backend!r} refused the turn on a {limit_kind}; reset {resets_at}"
    )


def classify_stream_failure(
    *,
    backend: Mapping[str, Any],
    lines: Iterable[str],
    process_exited: bool,
    diff_present: bool,
    manifest_present: bool,
) -> str | None:
    """Classify only a dialect-recognised stream failure with no work product."""

    dialect = dialect_for(backend)
    events, _malformed = parse_events(lines)
    return dialect.classify_stream_failure(
        events,
        process_exited=process_exited,
        diff_present=diff_present,
        manifest_present=manifest_present,
    )


def observe_log(
    *,
    backend_name: str,
    backend: Mapping[str, Any],
    log_path: str | Path,
    elapsed_seconds: float | None = None,
) -> Observation:
    """Observe a worker from its on-disk event log, absent log included.

    An absent log is the ordinary state of a run whose process has not yet
    written anything, so it reports ``starting`` rather than failing.
    """
    path = Path(log_path)
    if not path.exists():
        obs = Observation(
            backend=backend_name,
            budget=unknown_budget("no event log yet"),
            throughput=unknown_throughput("no event log yet"),
            detail=f"event log not written yet: {path}",
        )
        return obs
    with path.open(encoding="utf-8", errors="replace") as handle:
        return observe_stream(
            backend_name=backend_name,
            backend=backend,
            lines=handle,
            elapsed_seconds=elapsed_seconds,
        )
