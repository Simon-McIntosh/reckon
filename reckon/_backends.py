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

Throughput keeps the largest single request and the cumulative input over a run
as separately named quantities. Neither may be filled from the other: only the
request maximum can be compared with a context window, while cumulative input
describes the whole run and can legitimately exceed that window many times over.
"""

from __future__ import annotations

import copy
import hashlib
import json
import queue
import re
import subprocess
import tempfile
import threading
import urllib.request
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from reckon._observations import optional_number
from reckon._timestamps import parse_iso, parse_utc
from reckon._worker_fence import (  # noqa: F401
    _HARNESS_HOME,
    CLAUDE_CREDENTIAL_FILENAME,
    CODEX_AUTH_FILENAME,
    CODEX_LOGIN_RECORD_NAME,
    ENABLED_PROJECT_MCP_SERVERS_KEY,
    FENCE_BINARY,
    PROJECT_MCP_SERVERS_ENABLED_KEY,
    PROTECTED_BIND_WAIT_ATTEMPTS,
    PROTECTED_BIND_WAIT_INTERVAL_SECONDS,
    PROTECTED_PATHS_KEY,
    RUN_STDERR_LOG_NAME,
    UNPROTECTED_PATHS_KEY,
    WORKER_MCP_SERVERS,
    BackendError,
    _await_protected_sources,
    _bind_reckon_hook_commands,
    _copy_tree_without_overwrite,
    _copy_writable_mode,
    _declared_paths,
    _declared_worker_mcp_servers,
    _default_protected_paths,
    _fence_probe_verdicts,
    _fenced_worktree_refusal,
    _filter_top_level_keys,
    _load_json_mapping,
    _merge_harness_entry,
    _named_protected_paths,
    _permission_bits,
    _probe_user_namespace,
    _report_codex_login_truncation,
    _resolve_declared_path,
    _seed_harness_entry,
    _seed_harness_session,
    _seed_worker_mcp_settings,
    _within_any,
    _write_private_json,
    claude_credential_source,
    codex_auth_source,
    codex_login_record_path,
    codex_login_size,
    codex_login_truncation_detail,
    create_write_roots,
    declared_protected_paths,
    fence_argv,
    fence_capability_problem,
    fence_unprotected_paths,
    harness_home,
    observe_codex_login,
    protected_checkouts,
    protected_paths,
    protected_read_only_binds,
    record_codex_login_size,
    resolved_destination,
    seed_harness_home,
    seed_write_lock_namespace,
    worktree_git_write_roots,
    write_lock_directory,
)

# Sandbox tiers named by the flight schema. The mapping to concrete flags is
# per-dialect; the tier names are shared vocabulary.
READ_ONLY = "read-only"
WORKSPACE_WRITE = "workspace-write"
WORKTREE_FULL = "worktree-full"


def delivery_write_roots(
    *,
    run_directory: str | Path,
    reports_directory: str | Path,
    manifest_path: str | Path | None = None,
    review_store_directory: str | Path | None = None,
) -> set[Path]:
    """Return the durable roots a node delivers into, independent of tier.

    The delivery stores a caller passes are granted to every restricted tier,
    because a role that may not touch the repository it grades still has to
    write the artifact it was dispatched to produce. A role's own store must
    therefore be named here alongside the run directory and the shared reports
    root: granting one durable store and withholding a sibling leaves a node
    whose declared delivery path is refused as unreachable, which no amount of
    correct work on the worker's side can overcome.

    The set is split out from :func:`sandbox_write_roots` because a *fenced*
    run needs the same roots whatever its tier: a `worktree-full` node is
    unrestricted only while nothing seals the machine, and under a fence the
    seal is absolute, so the roots it delivers into have to be named exactly as
    a restricted tier's are. Two lists last until the first divergence; one
    function is why this one exists.
    """
    roots = {
        Path(run_directory).expanduser().resolve(),
        Path(reports_directory).expanduser().resolve(),
        Path(tempfile.gettempdir()).expanduser().resolve(),
    }
    if review_store_directory:
        roots.add(Path(review_store_directory).expanduser().resolve())
    if manifest_path:
        manifest = Path(manifest_path).expanduser()
        if manifest.is_absolute():
            roots.add(manifest.resolve().parent)
    return roots


def sandbox_write_roots(
    backend: Mapping[str, Any],
    *,
    repository: str | Path,
    run_directory: str | Path,
    reports_directory: str | Path,
    manifest_path: str | Path | None = None,
    review_store_directory: str | Path | None = None,
) -> tuple[Path, ...] | None:
    """Return writable roots for a resolved sandbox, or None if unrestricted.

    ``None`` is the *unfenced* reading of an unrestricted tier. A fenced run
    cannot use it — the fence seals every protected path and re-binds only the
    roots it is given — so the composition asks :func:`fenced_write_roots`
    instead, which starts from the same delivery set and never returns None.
    """
    tier = str(backend.get("sandbox") or READ_ONLY)
    if tier == WORKTREE_FULL:
        return None
    roots = delivery_write_roots(
        run_directory=run_directory,
        reports_directory=reports_directory,
        manifest_path=manifest_path,
        review_store_directory=review_store_directory,
    )
    if tier == WORKSPACE_WRITE:
        roots.add(Path(repository).expanduser().resolve())
    return tuple(sorted(roots, key=lambda path: path.as_posix()))


def _declared_write_grant(
    path: str | Path,
    *,
    repository: str | Path,
    worktree: str | Path | None,
) -> Path | None:
    """Return the directory a declared write path needs bound writable, or None.

    A declared path that names a file is granted through its parent directory:
    the fence binds directories, so a file declaration is realised by binding
    the directory the file will be created in. Declaring the file itself as a
    directory would create a directory named ``report.md``, which is not the
    artifact the node declared and cannot be written as one.

    A path that already sits inside the worktree is skipped: the worktree is
    bound writable already, so a second grant for a path beneath it says
    nothing new and only lengthens the argv.

    A relative declared path names a file in the worker's own checkout, so it
    resolves against the *worktree* when one is given rather than against the
    repository. The repository is the main checkout; joining a node's
    repo-relative declaration to it would grant a root inside the very checkout
    the fence exists to seal, re-opening it for a worker whose file actually
    lands in its own tree. Only an absolute declared path can name a location
    outside the worktree.

    A path whose nearest existing ancestor is a regular file cannot be realised
    at all. That is refused by name rather than composed against a wider
    ancestor, because the launch would otherwise fail inside bubblewrap with
    the missing-source condition this composition exists to remove.
    """
    raw = Path(path).expanduser()
    if raw.is_absolute():
        resolved = raw.resolve()
    else:
        base = worktree if worktree is not None else repository
        resolved = (Path(base).expanduser() / raw).resolve()
    if worktree is not None and resolved.is_relative_to(resolved_destination(worktree)):
        return None
    named_as_file = resolved.is_file() or bool(resolved.suffix)
    if not named_as_file:
        return resolved
    parent = resolved.parent
    if parent.is_dir():
        return parent
    ancestor = parent
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if ancestor.is_file():
        raise BackendError(
            f"cannot create the declared write path {resolved}: {ancestor} is "
            "not a directory, so the path cannot be created and cannot be "
            "bound writable"
        )
    return parent


def fenced_write_roots(
    backend: Mapping[str, Any],
    *,
    repository: str | Path,
    run_directory: str | Path,
    reports_directory: str | Path,
    declared_write_paths: Iterable[str | Path] = (),
    manifest_path: str | Path | None = None,
    review_store_directory: str | Path | None = None,
    worktree: str | Path | None = None,
) -> tuple[Path, ...]:
    """Return every root a fenced launch must re-bind writable.

    A fence seals each protected path read-only and re-opens only the roots
    named to it, so the tier's own write roots are not sufficient on their own:
    ``sandbox_write_roots`` for a ``worktree-full`` node returns ``None`` —
    "unrestricted" — which is true only while nothing seals the machine. Under
    the fence that node could write its worktree and nothing else, so a declared
    delivery path outside the worktree stayed read-only and the worker could not
    produce the artifact its node exists for.

    What is granted here is therefore: the same delivery roots every restricted
    tier gets, the repository for a ``workspace-write`` run, and every declared
    write path that lies outside the worktree — whatever the tier. A declared
    path is resolved against the worktree when it is not absolute, because a
    relative declaration names a file in the worker's own checkout; a path
    naming a file is granted through its parent directory.
    """
    roots = delivery_write_roots(
        run_directory=run_directory,
        reports_directory=reports_directory,
        manifest_path=manifest_path,
        review_store_directory=review_store_directory,
    )
    if str(backend.get("sandbox") or READ_ONLY) == WORKSPACE_WRITE:
        roots.add(Path(repository).expanduser().resolve())
    for declared in declared_write_paths:
        grant = _declared_write_grant(
            declared, repository=repository, worktree=worktree
        )
        if grant is None:
            continue
        roots.add(grant)
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
    # The fold's own resumable state, so a later read of the same stream can be
    # extended from where this one stopped instead of re-read whole. It is a
    # cache's private business rather than part of the observation a reader
    # consumes, so it is excluded from ``as_dict`` and from equality.
    stream_state: dict[str, Any] = field(
        default_factory=dict, compare=False, repr=False
    )

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
    fence_waiver: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return the plan as sorted JSON-ready data, prompt text excluded."""
        return {
            "argv": list(self.argv),
            "backend": self.backend,
            "cwd": self.cwd,
            "dialect": self.dialect,
            "final_message_path": self.final_message_path,
            "fence_waiver": self.fence_waiver,
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
        "cumulative_input_tokens": None,
        "cumulative_cached_input_tokens": None,
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


def _resolved_window(
    announced: int | None, configured: int | None
) -> tuple[int | None, str]:
    """Resolve the window a utilisation is divided by, and its provenance.

    The configured lane window is the authority that enforces a ceiling, so it
    wins outright when present — the stream's own declared window is never
    preferred on a conflict and the two are never averaged. Absence of the
    configured figure defers to the stream's declared window, which is the
    only other figure a reader is told the run was measured against; absence of
    both resolves to no window at all, so the utilisation is unknown rather
    than divided by any constant.
    """
    if configured is not None:
        return configured, "configured"
    if announced is not None:
        return announced, "announced"
    return None, "absent"


def throughput_block(
    *,
    generated_tokens: int | None,
    generation_seconds: float | None,
    elapsed_seconds: float | None,
    peak_input_tokens: int | None,
    cumulative_input_tokens: int | None,
    cumulative_cached_input_tokens: int | None,
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
            "cumulative_input_tokens": cumulative_input_tokens,
            "cumulative_cached_input_tokens": cumulative_cached_input_tokens,
            "input_budget_tokens": input_budget_tokens,
            # A context window is spent by one request. A run total can exceed
            # it repeatedly, so absence of a request maximum leaves this unknown.
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


# The account surface the claude-shaped harness publishes and the stored
# credential that authenticates it. The endpoint is the client's internal
# contract rather than a published API; a moved or unreachable surface folds
# to an unknown reading, and the strict parse below folds a shape change to
# unknown the same way, so a lane can be blind but never falsely clear.
# Times attach UTC via timezone.utc, not the datetime.UTC alias, because this
# runtime's datetime class does not define the alias despite exporting its name.
CLAUDE_ACCOUNT_USAGE_URL = "https://claude.ai/api/usage_this_cycle"
CLAUDE_CREDENTIAL_PATH = Path("~/.claude/.credentials.json")
# The named window a claude lane's reading is fenced against: the account-wide
# figure, never a scoped window whose identity the client only names by label.
ACCOUNT_WINDOW = "weekly"


def _load_claude_credential() -> dict[str, Any]:
    """Read the harness's stored OAuth block for the account read.

    Raises when the store is missing or carries no usable authentication, so a
    caller reports that loudly rather than letting the transport fail later on
    a missing token.
    """
    path = CLAUDE_CREDENTIAL_PATH.expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"no stored credential at {path}")
    with path.open() as handle:
        try:
            stored = json.load(handle)
        except ValueError as exc:
            raise ValueError(f"credential at {path} is not JSON — {exc}") from exc
    oauth = stored.get("claudeAiOauth") if isinstance(stored, Mapping) else None
    if not isinstance(oauth, Mapping) or not oauth.get("accessToken"):
        raise ValueError(f"credential at {path} carries no usable authentication")
    return oauth


def _claude_credential_expiry(oauth: Mapping[str, Any]) -> datetime | None:
    """Return when the stored login's lifetime ends, or None if undeclared.

    The access token lapses in hours but is refreshable while its refresh token
    lives, so the login's end is the refresh token's expiry; an access token
    with no living refresh token is judged by its own expiry.
    """
    declared = oauth.get("refreshTokenExpiresAt")
    if declared is None:
        declared = oauth.get("expiresAt")
    if not isinstance(declared, (int, float)) or isinstance(declared, bool):
        return None
    try:
        return datetime.fromtimestamp(float(declared), tz=timezone.utc)  # noqa: UP017
    except (OverflowError, OSError, ValueError):
        return None


def _fetch_claude_account(oauth: Mapping[str, Any]) -> object:
    """Read the account position over the client's HTTPS account surface.

    The read is account metadata rather than an inference request, so it runs
    no model. A moved or unreachable surface raises; the caller folds the
    failure into an unknown reading.
    """
    request = urllib.request.Request(
        CLAUDE_ACCOUNT_USAGE_URL,
        headers={"Authorization": f"Bearer {oauth['accessToken']}"},
    )
    # The URL is the module constant above, pinned to https; no scheme comes
    # from any input, so the open cannot be steered toward a file or custom
    # scheme.
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        return json.load(response)


def _parse_claude_account(payload: object) -> dict[str, Any]:
    """Parse the account answer strictly into the shared budget block.

    The answer's shape is the client's internal contract and can change without
    notice. The named window a lane is fenced against, a numeric utilisation
    inside it and a reset time are required; an unrecognised shape, a missing
    window, or a non-numeric figure resolves to an honest unknown rather than
    to a plausible low utilisation, because the quiet failure runs toward
    headroom.
    """
    if not isinstance(payload, Mapping):
        return unknown_budget("account answer was not an object")
    windows = payload.get("windows")
    if not isinstance(windows, Mapping) or not windows:
        return unknown_budget("account answer carried no named windows")
    window = windows.get(ACCOUNT_WINDOW)
    if not isinstance(window, Mapping):
        return unknown_budget(f"account answer carried no {ACCOUNT_WINDOW} window")
    utilisation = window.get("utilization")
    if not isinstance(utilisation, (int, float)) or isinstance(utilisation, bool):
        return unknown_budget(f"account {ACCOUNT_WINDOW} utilisation is not numeric")
    resets_at = _epoch_to_iso(window.get("resetsAt"))
    if resets_at is None:
        return unknown_budget(f"account {ACCOUNT_WINDOW} window names no reset time")
    budget = unknown_budget("")
    budget.update(
        {
            "headroom": "known",
            "utilisation_pct": round(100.0 * float(utilisation), 1),
            "rate_limit_type": ACCOUNT_WINDOW,
            "rate_limit_period_minutes": window.get("windowMinutes"),
            "resets_at": resets_at,
            "detail": "account surface reports utilisation and reset time",
        }
    )
    return budget


# The on-disk copy of the account block is a last-known position, never a
# heartbeat. It is read only as a fallback and only ever rendered beside the
# age of the stamp the copy carries, because a past position shown bare reads
# as now. The cache payload is the same answer shape the live surface returns,
# plus one key naming when it was written; a stamp that cannot be trusted
# leaves the reading unknown rather than showing a figure with no age.
ACCOUNT_CACHE_STAMP = "fetch_stamp"


# The cache stamp is trusted only when the parsed value names a zone, which the
# reader decides from the result's own ``tzinfo`` rather than the text's tail.
def _parse_cached_fetch_stamp(payload: object) -> datetime | None:
    """Return when the cached block was written, or None when untrustworthy.

    The stamp is accepted as epoch seconds or a zoned ISO-8601 string. A
    missing, malformed or unzoned value returns None: without a trusted moment
    the copy's age cannot be stated, so the copy must not be shown bare. The
    shared parser reads a zoneless value as UTC, so a value whose text names no
    zone is refused here before it is parsed — decided from the parsed result's
    ``tzinfo`` so every offset spelling an ISO-8601 parser accepts is honoured.
    A lowercase ``z`` is refused too: the shared parser normalises it to a zero
    offset, but it is not the strict ISO-8601 designator this reader trusts.
    """
    if not isinstance(payload, Mapping):
        return None
    stamp = payload.get(ACCOUNT_CACHE_STAMP)
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float, str)):
        return None
    if isinstance(stamp, (int, float)):
        return parse_utc(stamp)
    text = stamp.strip()
    if text != stamp or text.endswith("z"):
        return None
    moment = parse_iso(text)
    if moment is None or moment.tzinfo is None:
        return None
    return parse_utc(text)


def _human_age(seconds: float) -> str:
    """Render a span compactly, in the shapes a reader scans by eye."""
    whole = round(seconds)
    sign = "-" if whole < 0 else ""
    whole = abs(whole)
    minutes, seconds = divmod(whole, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{sign}{days}d{hours:02d}h"
    if hours:
        return f"{sign}{hours}h{minutes:02d}m"
    if minutes:
        return f"{sign}{minutes}m"
    return f"{sign}{seconds}s"


def cached_account_budget(
    *,
    path: str | Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read the on-disk copy of the account block as a fallback position.

    The copy is a last-known position, never a substitute for a live read, so
    any figure this returns is rendered beside the age of the copy's own fetch
    stamp — the numeric age and the stamp are carried in the same block as the
    figure, and the detail string names the age too. A copy with no trusted
    stamp yields unknown rather than a figure, because a bare past position
    reads as now. The body is parsed under the same strict rule as a live
    answer, so an unrecognised cached shape also stays unknown.
    """
    if now is None:
        now = datetime.now(timezone.utc)  # noqa: UP017
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        return unknown_budget(f"account cache unreadable — {exc}")
    stamp = _parse_cached_fetch_stamp(payload)
    if stamp is None:
        return unknown_budget(
            f"account cache carries no usable {ACCOUNT_CACHE_STAMP!r} stamp"
        )
    block = _parse_claude_account(payload)
    if block.get("headroom") != "known":
        return block
    age = (now - stamp).total_seconds()
    block["fetch_stamp"] = stamp.isoformat()
    block["fetch_age_seconds"] = round(age, 1)
    block["detail"] = f"cached account surface, fetched {_human_age(age)} ago"
    return block


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
    """Record a metered lane's retry exhaustion as a refusal block.

    The retries are the magnitude and the terminal error result is the verdict.
    No retry record carries a reset moment, so the block states the limit and
    the observed count and leaves the reset unset — the same honest absence a
    spend-limit refusal with no reset records. Only metered backends take this
    shape; an unmetered lane has no budget to exhaust, so its identical stream
    is recorded as backpressure by :func:`_backpressure_budget` instead.
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


def _backpressure_budget(budget: Mapping[str, Any], retries: int) -> dict[str, Any]:
    """Record an unmetered lane's retry exhaustion as lane backpressure.

    A consumer-queue 429 is the server telling the worker to stop asking for a
    while, not the account running dry — an unmetered backend has no metered
    window to spend, so its retry stream cannot be recording budget exhaustion.
    The block states the refusal with none of the metered-budget semantics a
    reader keys on: refusal stays false, headroom stays unknown, and the marker
    names the lane rather than the account. A dispatch fence may hold new work
    on the marker; nothing gates an existing run's resume on it, because a lane
    that shed a worker is not the same lane an hour later.
    """
    block = dict(budget)
    block.update(
        {
            "headroom": "unknown",
            "utilisation_pct": None,
            "rate_limit_type": "backpressure",
            "resets_at": None,
            "threshold_status": None,
            "surpassed_threshold": None,
            "refusal": False,
            "lane_backpressure": True,
            "detail": (
                f"run died after {retries} consumer-queue retries "
                "(api_retry 429); the lane refused (queue full) — backpressure "
                "on an unmetered lane, not a spent budget"
            ),
        }
    )
    return block


def _backend_is_unmetered(backend_name: str | None) -> bool:
    """Whether a named backend carries no metered per-token price.

    Read through the module rather than imported at the top of this file: the
    cost model lives in the ledger, and this translation module does not need
    it until a retry exhaustion has to be classified. A name that is absent or
    unknown reads as metered — the direction that still holds a wave, so a lane
    the cost model has not heard of can never silently unblock dispatch.
    """
    if not backend_name:
        return False
    from reckon import ledger

    return ledger.is_unmetered_backend(str(backend_name))


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
    # Whether a lane speaking this dialect authenticates with the operator's
    # stored subscription login, which must be bound writable into the run's
    # harness home. False for a lane whose account lives elsewhere — the local
    # clive lane authenticates against its own server — so no credential bind
    # is composed for it.
    subscription_login = False
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
        images: Sequence[str] = (),
    ) -> list[str]:
        raise NotImplementedError

    def _lane(self, command: str) -> str:
        """Name this dialect as the caller names the lane.

        Dialect resolution keys on the command's own name, so that is the
        identity a refusal must carry: the local harness that shares the claude
        flag grammar is a separate configured lane, and must report the name of
        the lane that refused rather than the name of the dialect family behind
        it, so the operator can tell which configured lane is at fault.
        """
        return Path(command).name

    def _refuse_images(self, command: str, images: Iterable[str]) -> None:
        """Raise for a dialect that has no way to hand an image to its harness.

        A dialect that cannot carry the attachment must not drop it and run a
        well-formed prompt without the figure: the verdict that comes back is
        formed from the filename and the diff, and a review reporting a
        judgement it could not have made is worse than no review. Named by
        lane, and emphatically not by substituting a lane that can.
        """
        attached = list(images)
        if not attached:
            return
        raise BackendError(
            f"the {self._lane(command)!r} dialect cannot carry attached images, "
            f"so {len(attached)} figure(s) would be dropped from the command "
            "line; route a figure review to a dialect that emits an image flag"
        )

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
        backend_name: str | None = None,
        usable_input_window: int | None = None,
        carry: Mapping[str, Any] | None = None,
    ) -> Observation:
        """Fold a stream into one observation.

        ``elapsed_seconds`` is the caller's wall clock for the run, offered
        because not every dialect reports a span of its own. A dialect that does
        report one prefers its own figure and ignores this. ``backend_name`` is
        the configured name of the backend the stream came from; only the
        claude-shaped dialect needs it, because one wire shape has to mean
        different things on a metered lane and an unmetered one.
        ``usable_input_window`` is the configured lane window that enforces a
        ceiling; the claude-shaped dialect divides its utilisation by that
        authority when one is declared.

        ``carry`` is a previous fold's state for the same stream, so the fold
        can be extended with only the records written since that fold instead of
        re-read from the first byte. A dialect opens with those values in place
        of its own initial ones, processes the events given to it, and leaves
        its new state on ``Observation.stream_state``. The state is taken
        immediately before the fold's own closing step, because that step reads
        what the loop accumulated and is applied once per call rather than
        stored.
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

    def read_account_surface(
        self,
        *,
        backend: Mapping[str, Any],
        fetch: Callable[..., object] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Read remaining headroom over this dialect's own transport, or None.

        None is the honest default for a dialect whose account is only reachable
        through the shared probe exchange (:meth:`budget_probe`); a caller falls
        back to it. A dialect that answers here owns the whole read — credential,
        transport and strict parse — and must return an unknown block naming the
        reason on every failure rather than raising, matching the funnel's
        contract so a pre-flight is never stopped by its own instrument.
        """
        return None

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


def budget_from_rate_limit_info(info: Any) -> dict[str, Any]:
    """Parse a rate-limit event's reported position into the shared budget block.

    Any dialect whose stream carries ``rate_limit_event`` records the position
    in this same shape, so a lane that reports its quota position has that
    position recorded on the run whatever the harness is named.

    The event carries two figures that answer different questions. The
    top-level ``utilization`` is the account's calendar-window position — for
    an overage record its own ``resetsAt`` lands on a month boundary — and it
    reads as a bare fraction that can exceed 1. ``unifiedWindows`` carries the
    rate-limit windows a dispatch actually runs into, each with its own
    fractional ``utilization`` and its own ``resetsAt``. Only the latter is
    read; the former is never consulted, so it can never leak into
    ``utilisation_pct`` as a percentage a hundred times too large. The binding
    window is whichever is furthest through, exactly as
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
        images: Sequence[str] = (),
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
        # `codex exec` reads attached figures as `-i <path>`, one flag per
        # file, and they must precede `resume`: a figure placed after the
        # subcommand is rejected, the subcommand taking only a session id and
        # the prompt on stdin.
        for image in images:
            argv += ["-i", str(image)]
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
        backend_name: str | None = None,
        usable_input_window: int | None = None,
        carry: Mapping[str, Any] | None = None,
    ) -> Observation:
        # This stream reports no context window to divide a utilisation by, so
        # the supplied configured window is accepted and unused.
        del usable_input_window
        carried = carry if isinstance(carry, Mapping) else {}
        obs = Observation(backend=self.name, budget=unknown_budget(""))
        message: str | None = carried.get("message")
        usage: dict[str, int | float] = dict(carried.get("usage") or {})
        completed_turn = bool(carried.get("completed_turn"))
        obs.events = int(carried.get("events") or 0)
        obs.session_id = carried.get("session_id")
        obs.terminal = bool(carried.get("terminal"))
        obs.exit_status = carried.get("exit_status")
        obs.detail = str(carried.get("detail") or "")
        if carried.get("budget") is not None:
            obs.budget = dict(carried["budget"])
        for event in events:
            obs.events += 1
            kind = event.get("type")
            if kind == "thread.started":
                obs.session_id = event.get("thread_id") or obs.session_id
            elif kind == "item.completed":
                item = event.get("item")
                if isinstance(item, Mapping) and item.get("type") == "agent_message":
                    message = item.get("text") or message
            elif kind == "rate_limit_event":
                # A stream that reports its quota position has that position
                # recorded; the completion path below must not replace it.
                obs.budget = budget_from_rate_limit_info(event.get("rate_limit_info"))
            elif kind == "turn.completed":
                completed_turn = True
                obs.terminal = True
                obs.exit_status = "ok"
                turn_usage = event.get("usage")
                if isinstance(turn_usage, Mapping):
                    _accumulate_usage(usage, turn_usage)
                # A usage-less turn contributes nothing. It must not reuse the
                # preceding turn's mapping as though that mapping described it.
            elif kind in ("turn.failed", "thread.error", "error", "stream.error"):
                obs.terminal = True
                obs.exit_status = "error"
                obs.detail = _error_detail(event)
                refused = refusal_budget(obs.detail)
                if refused is not None:
                    obs.budget = refused
        obs.stream_state["fold"] = {
            "events": obs.events,
            "session_id": obs.session_id,
            "terminal": obs.terminal,
            "exit_status": obs.exit_status,
            "detail": obs.detail,
            "message": message,
            "budget": obs.budget,
            "usage": usage,
            "completed_turn": completed_turn,
        }
        measured_usage = usage or None
        if completed_turn:
            measured_budget = self._budget(measured_usage)
            if obs.budget.get("refusal"):
                obs.budget["tokens"] = measured_budget["tokens"]
            elif obs.budget.get("headroom") == "known":
                # A fold above already recorded the stream's quota position;
                # keep that reading and attach this turn's tokens rather than
                # replacing the block with a tokens-only one.
                obs.budget["tokens"] = measured_budget["tokens"]
            else:
                obs.budget = measured_budget
        obs.final_message = message
        obs.throughput = self._throughput(measured_usage, elapsed_seconds)
        obs.phase = _phase(obs)
        return obs

    def _throughput(
        self, usage: Mapping[str, Any] | None, elapsed_seconds: float | None
    ) -> dict[str, Any]:
        """Rate this harness's run against the caller's clock.

        This stream reports what each turn consumed but not how long the run took,
        so the span has to come from the caller. Without one there is no rate —
        the token counts alone cannot say whether they took a minute or an hour,
        and that distinction is the whole question.
        """
        generated = cumulative_input = cumulative_cached_input = None
        if isinstance(usage, Mapping):
            generated = _sum_tokens(usage, ("output_tokens", "reasoning_output_tokens"))
            cumulative_input = _sum_tokens(
                usage, ("input_tokens", "cached_input_tokens")
            )
            cumulative_cached_input = _sum_tokens(usage, ("cached_input_tokens",))
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
            # The exec stream reports usage once per turn rather than once per
            # request, so a request maximum is not derivable from this record.
            # Per-session rollout files do carry the individual request records.
            peak_input_tokens=None,
            cumulative_input_tokens=cumulative_input,
            cumulative_cached_input_tokens=cumulative_cached_input,
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

        This harness's run stream reports what each turn consumed and nothing
        about what remains, so the honest record is accumulated tokens plus an
        unknown headroom. A later reader must not mistake the presence of token
        counts for a budget. Headroom is obtainable from this harness — just not
        here; see :meth:`budget_probe`.
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

        Several metered windows can be reported at once. They remain keyed by
        their own duration for readers that need every horizon, while the
        compatibility fields still describe whichever window is furthest
        through. The per-bucket map keys on identifiers this module must not
        record.

        ``usedPercent`` is a percentage on its own scale, so a value below one
        is a ratio wearing a percentage's name: recorded verbatim it reads as a
        plausible low utilisation, which is the same quiet failure this module
        refuses everywhere else. Such an answer is rejected outright rather than
        half-recorded.
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
        if any(0.0 < float(window["usedPercent"]) < 1.0 for _, window in windows):
            return unknown_budget(
                "rate-limit answer carried a ratio where a percentage is expected"
            )
        window_type, binding = max(
            windows, key=lambda item: float(item[1]["usedPercent"])
        )
        quota_windows = {
            int(window["windowDurationMins"]): {
                "window_minutes": int(window["windowDurationMins"]),
                "used_percent": float(window["usedPercent"]),
                "resets_at": _epoch_to_iso(window.get("resetsAt")),
                "rate_limit_type": kind,
            }
            for kind, window in windows
            if isinstance(window.get("windowDurationMins"), (int, float))
            and not isinstance(window.get("windowDurationMins"), bool)
            and float(window["windowDurationMins"]).is_integer()
            and int(window["windowDurationMins"]) > 0
        }
        budget = unknown_budget("")
        budget.update(
            {
                "headroom": "known",
                "utilisation_pct": float(binding["usedPercent"]),
                "rate_limit_type": window_type,
                "rate_limit_period_minutes": binding.get("windowDurationMins"),
                "resets_at": _epoch_to_iso(binding.get("resetsAt")),
                "threshold_status": snapshot.get("rateLimitReachedType"),
                "quota_windows": quota_windows,
                "detail": "backend's account surface reports utilisation and reset time",
            }
        )
        return budget


class _ClaudeDialect(Dialect):
    """Claude Code: `-p` stream-json, session ids, rate-limit headroom."""

    name = "claude"
    # A claude subscription lane reads the operator's stored login, so the
    # fence binds it writable into the run's harness home.
    subscription_login = True

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
        images: Sequence[str] = (),
    ) -> list[str]:
        self._refuse_images(command, images)
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
        backend_name: str | None = None,
        usable_input_window: int | None = None,
        carry: Mapping[str, Any] | None = None,
    ) -> Observation:
        carried = carry if isinstance(carry, Mapping) else {}
        obs = Observation(backend=self.name, budget=unknown_budget(""))
        message: str | None = carried.get("message")
        budget = (
            dict(carried["budget"])
            if carried.get("budget") is not None
            else unknown_budget("no rate-limit event in the stream yet")
        )
        throughput = carried.get("throughput") or unknown_throughput(
            "no completed result to measure yet"
        )
        peak_input = int(carried.get("peak_input") or 0)
        rate_limit_retries = int(carried.get("rate_limit_retries") or 0)
        obs.events = int(carried.get("events") or 0)
        obs.session_id = carried.get("session_id")
        obs.terminal = bool(carried.get("terminal"))
        obs.exit_status = carried.get("exit_status")
        obs.detail = str(carried.get("detail") or "")
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
                throughput = self._throughput(
                    event, peak_input, elapsed_seconds, usable_input_window
                )
            if obs.session_id is None:
                # Every event of this stream carries the session id, including
                # the hook events a host configuration may emit before init.
                obs.session_id = event.get("session_id") or obs.session_id
        obs.stream_state["fold"] = {
            "events": obs.events,
            "session_id": obs.session_id,
            "terminal": obs.terminal,
            "exit_status": obs.exit_status,
            "detail": obs.detail,
            "message": message,
            "budget": budget,
            "throughput": throughput,
            "peak_input": peak_input,
            "rate_limit_retries": rate_limit_retries,
        }
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
                # The stream cannot tell a spent consumer from a full queue —
                # every 429 retry carries the identical field set — so the
                # backend's meteredness is the only discriminator there is. An
                # unmetered lane has no metered budget to exhaust, so the shape
                # records lane backpressure rather than a budget refusal: new
                # dispatch may be held on it, but a dying run's resume never is.
                if _backend_is_unmetered(backend_name):
                    budget = _backpressure_budget(budget, rate_limit_retries)
                else:
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
        usable_input_window: int | None = None,
    ) -> dict[str, Any]:
        """Rate a finished run from the spans and totals its result carries.

        The totals are read from the result and never summed from the assistant
        events, which report a message's opening usage rather than its final one
        and are emitted once per content block besides — summing them undercounts
        by roughly two orders of magnitude and does so silently. Peak input is
        the largest single prompt the run sent, which is the figure a context
        window is actually spent against; the cumulative totals on the result are
        the sum over every request and would read far past any window.

        The window the utilisation is divided by is resolved by
        :func:`_resolved_window`: the configured lane window pins the
        denominator when one is declared, so the reading names the authority
        that enforces the ceiling rather than whatever window the client
        happened to announce.
        """
        elapsed = _seconds(result.get("duration_ms"))
        if elapsed is None:
            elapsed = elapsed_seconds
        generation = _seconds(result.get("duration_api_ms"))
        model_usage = result.get("modelUsage")
        generated: int | None = None
        cumulative_input: int | None = None
        cumulative_cached_input: int | None = None
        window: int | None = None
        if isinstance(model_usage, Mapping):
            per_model = [
                entry for entry in model_usage.values() if isinstance(entry, Mapping)
            ]
            totals = [optional_number(entry.get("outputTokens")) for entry in per_model]
            measured = [value for value in totals if value is not None]
            generated = int(sum(measured)) if measured else None
            input_totals = [
                _sum_tokens(
                    entry,
                    ("inputTokens", "cacheReadInputTokens", "cacheCreationInputTokens"),
                )
                for entry in per_model
            ]
            measured_inputs = [value for value in input_totals if value is not None]
            cumulative_input = int(sum(measured_inputs)) if measured_inputs else None
            cached_totals = [
                optional_number(entry.get("cacheReadInputTokens"))
                for entry in per_model
            ]
            measured_cached = [value for value in cached_totals if value is not None]
            cumulative_cached_input = (
                int(sum(measured_cached)) if measured_cached else None
            )
            windows = [
                optional_number(entry.get("contextWindow")) for entry in per_model
            ]
            usable = [value for value in windows if value]
            # One usable window even when several models ran: the run is held by
            # the smallest, since that is the one a shared prompt overflows first.
            announced = int(min(usable)) if usable else None
        else:
            announced = None
        window, window_basis = _resolved_window(announced, usable_input_window)
        usage = result.get("usage")
        if isinstance(usage, Mapping):
            if generated is None:
                generated = _sum_tokens(usage, ("output_tokens",))
            if cumulative_input is None:
                cumulative_input = _sum_tokens(
                    usage,
                    (
                        "input_tokens",
                        "cache_read_input_tokens",
                        "cache_creation_input_tokens",
                    ),
                )
            if cumulative_cached_input is None:
                cumulative_cached_input = _sum_tokens(
                    usage, ("cache_read_input_tokens",)
                )
        span = (
            "generation and wall clock reported separately by the backend"
            if generation is not None
            else "wall clock only; the backend reported no inference span"
        )
        if window is not None:
            basis = "configured lane" if window_basis == "configured" else "stream"
            window_clause = f"; utilisation divided by the {basis} window {window}"
        else:
            window_clause = "; no window resolved, so utilisation is unknown"
        return throughput_block(
            generated_tokens=generated,
            generation_seconds=generation,
            elapsed_seconds=elapsed,
            peak_input_tokens=peak_input or None,
            cumulative_input_tokens=cumulative_input,
            cumulative_cached_input_tokens=cumulative_cached_input,
            input_budget_tokens=window,
            detail=span + window_clause,
        )

    def _budget(self, info: Any) -> dict[str, Any]:
        """Parse reported utilisation and reset time into the shared block.

        This dialect declares no probe because it needs none: headroom arrives on
        the run stream every worker already writes, so a pre-flight reading past
        runs learns it for free and a separate process would add nothing. The
        parsing itself is the shared :func:`budget_from_rate_limit_info`, so a
        position recorded here has the same shape a codex stream's reading does.
        """
        return budget_from_rate_limit_info(info)

    def read_account_surface(
        self,
        *,
        backend: Mapping[str, Any],
        fetch: Callable[[Mapping[str, Any]], object] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Read remaining headroom from the harness's own account surface.

        The dialect owns the whole read: the OAuth credential comes from the
        harness's own store, the transport is an HTTPS account read that runs
        no model, and the answer is parsed strictly so a shape change resolves
        to an honest unknown rather than to a plausible low utilisation. The
        one window a lane is fenced against is required; anything else — an
        unrecognised shape, a missing window, a non-numeric figure — returns
        unknown, because the quiet parse failure runs toward headroom.

        Every way this can fail returns an unknown block naming the reason,
        matching the probe exchange's contract, so a pre-flight is never
        stopped by its own instrument.
        """
        if now is None:
            now = datetime.now(timezone.utc)  # noqa: UP017
        try:
            credential = _load_claude_credential()
        except (OSError, ValueError) as exc:
            return unknown_budget(f"account credential unreadable — {exc}")
        expiry = _claude_credential_expiry(credential)
        if expiry is not None:
            age = (now - expiry).total_seconds()
            if age >= 0:
                return unknown_budget(
                    f"account credential expired {age:.0f}s ago — "
                    "the lane's position is unknown until the login is renewed"
                )
        try:
            payload = (fetch or _fetch_claude_account)(credential)
        except (OSError, ValueError) as exc:
            return unknown_budget(f"account read failed — {exc}")
        return _parse_claude_account(payload)


def _seconds(milliseconds: Any) -> float | None:
    """Convert a reported millisecond span to seconds, or None."""
    value = optional_number(milliseconds)
    return None if value is None else round(value / 1000.0, 3)


def _sum_tokens(usage: Mapping[str, Any], keys: Sequence[str]) -> int | None:
    """Total the named token counts, or None when none of them was reported."""
    measured = [
        value for key in keys if (value := optional_number(usage.get(key))) is not None
    ]
    return int(sum(measured)) if measured else None


def _accumulate_usage(total: dict[str, int | float], usage: Mapping[str, Any]) -> None:
    """Add one turn's numeric counters to a run-level usage mapping."""
    for key, value in usage.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        total[key] = total.get(key, 0) + value


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


class _CliveDialect(_ClaudeDialect):
    """clive — the local lane's claude wrapper, pointed at the GPU server.

    It speaks claude's flags and stream, so it shares the claude dialect's
    translation, but it authenticates against its own server and keeps no
    subscription login, so the fence composes no credential bind for it.
    """

    subscription_login = False


_DIALECTS: dict[str, Dialect] = {
    _CodexDialect.name: _CodexDialect(),
    _ClaudeDialect.name: _ClaudeDialect(),
    # clive wraps `claude` with env vars pointing at the local GPU server
    # (ANTHROPIC_BASE_URL, ANTHROPIC_AUTH_TOKEN, ANTHROPIC_MODEL). Its flags
    # and JSON-lines event stream are identical to claude's since it passes
    # all args through via `exec claude "${ARGS[@]}"`.
    "clive": _CliveDialect(),
}


def known_dialects() -> tuple[str, ...]:
    """Return the harness commands this module can translate."""
    return tuple(sorted(_DIALECTS))


def dialect_for(backend: Mapping[str, Any]) -> Dialect:
    """Select a dialect from the backend's command, or say what is missing.

    The command is matched rather than the backend's name because the name is
    free-form user data — a config may call a backend ``fast`` or ``reviewer``
    — while the command is the executable whose flags have to be spoken.

    A placed launch prefixes the scheduler onto the argv, so a run record read
    back later begins with the scheduler and the command's stem names that
    scheduler rather than the harness. The identity the launch resolved to is
    recorded beside the command for exactly that case, and is consulted only
    when the stem names no dialect — so a configuration's command stays the
    authority at dispatch, where a mapping carrying no recorded identity and an
    untranslatable command is still refused.
    """
    command = backend.get("command")
    if not command:
        raise BackendError("backend declares launch: cli but names no command")
    stem = Path(str(command)).name
    dialect = _DIALECTS.get(stem)
    if dialect is None:
        for key in ("dialect", "backend"):
            recorded = str(backend.get(key) or "").strip()
            if recorded and (candidate := _DIALECTS.get(Path(recorded).name)):
                return candidate
        known = ", ".join(known_dialects())
        raise BackendError(
            f"no launch translation for command '{stem}'; reckon can translate: {known}"
        )
    return dialect


def _harness_credential_binds(
    dialect: Dialect,
    harness: Path | None,
    home: str | Path | None,
) -> list[tuple[Path, Path]]:
    """Return the writable file binds a run's harness home needs, if any.

    A dialect whose lane authenticates with a stored credential has that one
    file exposed into the run's own harness home, so the harness authenticates
    from its run directory rather than from the operator's sealed dot
    directory. Codex reads ``auth.json``; a claude subscription lane reads
    ``.credentials.json`` under ``CLAUDE_CONFIG_DIR``. The local clive lane
    needs neither, because its server holds the account.

    The bind is writable rather than read-only because both harnesses rewrite
    the credential in place on a token refresh (``OpenOptions::truncate(true).
    write(true)`` for codex, the same inode, no rename), so a read-only
    exposure rotates the login server-side and then cannot persist it, burning
    the operator's single-use refresh token. Binding the operator's one file
    writable — rather than copying it into the run — is what lets the refresh
    survive without giving the worker its own stale shadow of the login.

    The list is empty for a dialect that needs no credential (the clive lane),
    for a run with no harness home, and for a machine with no login — the fence
    is then short one bind rather than refusing to start.
    """
    if harness is None:
        return []
    if dialect.name == "codex":
        auth = codex_auth_source(home)
        return [] if auth is None else [(auth, harness / CODEX_AUTH_FILENAME)]
    if dialect.subscription_login:
        credential = claude_credential_source(home)
        if credential is None:
            return []
        return [(credential, harness / CLAUDE_CREDENTIAL_FILENAME)]
    return []


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
    images: Iterable[str | Path] = (),
    fence: bool = True,
    fence_home: str | Path | None = None,
    fence_config: Mapping[str, Any] | None = None,
    fence_waiver: str | None = None,
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
    # The run directory is the manifest's parent — the one location a worker is
    # always granted to write — and it is where the harness keeps its own state
    # rather than in the operator's dot directory the fence seals. The
    # claude-shaped harness needs it either way, because a worker's transcript
    # must land in its run whichever way it was launched. The codex home is a
    # fence artefact: the fence seals the operator's ``~/.codex`` read-only and
    # exposes the login into the run's home, so adopting a home exists to answer
    # the seal and the missing home is what a codex launch uses when it is
    # unfenced. Without the seal there is nothing to answer and no login in the
    # new CODEX_HOME, so pointing it at an empty run home would displace the
    # operator's login with nothing. An unfenced codex launch keeps the
    # operator's own home.
    #
    # A manifest whose directory does not exist is not a live run — a preview
    # composes a plan before anything has been created — so no home is seeded
    # and no variable is invented for a run that has nowhere to keep it. A
    # manifest path that is not absolute names no directory either: its parent
    # is a working directory, and :func:`harness_home` refuses it for that
    # reason rather than seeding a home where the seeding process stood.
    run_directory = None if manifest is None else Path(manifest).parent
    # Under the fence the run's write roots are created *before* the harness
    # home is resolved, because the run directory a manifest lands in may not
    # exist yet and the home is seeded inside it. Creating the roots first also
    # means a manifest declared under a missing directory still gets its home
    # and its config-home variable, so a fenced codex launch does not fall back
    # to the sealed operator home and exit "Read-only file system". An unfenced
    # composition creates nothing, so a preview still invents no directory.
    write_roots: list[str | Path] = []
    lock_directory: Path | None = None
    if fence:
        write_roots = list(writable_directories)
        lock_directory = seed_write_lock_namespace(fence_home)
        if lock_directory is not None:
            write_roots.append(lock_directory)
        create_write_roots(write_roots)
    harness = (
        None
        if run_directory is None or not run_directory.is_dir()
        else harness_home(dialect.name, run_directory)
    )
    # A run adopts its own harness home only when fenced. An unfenced run keeps
    # the operator's home, where the user hooks, user memory and every session
    # recorded before the run existed already live; a per-run home that carries
    # none of them silently drops the hooks and orphans those sessions.
    adopts_harness_home = fence
    if harness is not None and adopts_harness_home:
        from reckon.flight import harness_home_adjacent_files, harness_home_files

        seed_harness_home(
            harness,
            dialect_name=dialect.name,
            operator_home=fence_home if fence_home is not None else Path.home(),
            declaration=harness_home_files(dialect.name, backend),
            adjacent_declaration=harness_home_adjacent_files(dialect.name),
            resume_session=resume_session,
            # A resolved flight configuration states which project this run
            # belongs to and so which servers its harness should start; a bare
            # composition carries none, so it names no project and seeds no
            # MCP record.
            worker_mcp_servers=(
                _declared_worker_mcp_servers(fence_config)
                if fence_config is not None
                else None
            ),
        )
        environment[_HARNESS_HOME[dialect.name][0]] = str(harness)
    argv = dialect.argv(
        command=str(backend["command"]),
        backend=backend,
        worktree=worktree_path,
        working_directory=working_directory,
        writable_directories=tuple(str(path) for path in writable_directories),
        final_message_path=final_path,
        resume_session=resume_session,
        images=tuple(str(image) for image in images),
    )
    # Every dialect, every entry point — fresh dispatch, resume and redispatch
    # all build their argv here — so the fence is applied once and cannot be
    # left off for one of the three. It is opt-in: the read-only overlay seals
    # every checkout under the operator's code root, which also seals a
    # worktree's git directory and the shared object store, so a fenced worker
    # cannot commit until those are granted writable; and its harness home does
    # not yet carry the operator's hooks or instruction files.
    if fence:
        credential_binds = _harness_credential_binds(dialect, harness, fence_home)
        # The login's size is recorded before the fence opens, so the post-run
        # reading has a before-size to compare against; see
        # :func:`observe_codex_login` for why the file needs one.
        if dialect.name == "codex" and harness is not None:
            source = codex_auth_source(fence_home)
            if source is not None:
                record_codex_login_size(run_directory, source)
        argv = fence_argv(
            argv,
            writable_directories=write_roots,
            worktree=worktree_path,
            manifest_path=manifest,
            home=fence_home,
            read_write_binds=credential_binds,
            config=fence_config,
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
        fence_waiver=fence_waiver,
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
    fetch: Callable[[Mapping[str, Any]], object] | None = None,
    now: datetime | None = None,
    cache_path: str | Path | None = None,
) -> dict[str, Any]:
    """Read a backend's remaining headroom from its own account surface.

    Every way this can fail — no dialect, no probe, no answer, a broken exchange
    — returns an unknown block naming the reason rather than raising. A pre-flight
    must never be stopped by its own instrument: an unreadable probe leaves the
    caller exactly where it was, reading what earlier runs recorded, whereas a
    raised error would turn a missing measurement into a blocked wave.

    A caller that keeps the last-known position on disk may pass ``cache_path``;
    it is consulted only when the live surface yields no known reading, and the
    fallback carries the copy's fetch age beside any figure it shows, so a stale
    position is never presented as a freshly read one. A known live reading wins
    outright and carries no age markers, because the age marks provenance of a
    fallback rather than decorating every reading.
    """
    try:
        dialect = dialect_for(backend)
    except BackendError as exc:
        return unknown_budget(str(exc))
    try:
        reading = dialect.read_account_surface(backend=backend, fetch=fetch, now=now)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        reading = unknown_budget(f"account-limit read failed to run — {exc}")
    # The on-disk copy mirrors the account surface, so it is consulted only
    # when that surface yields nothing, and only on an explicit request: a
    # stale figure served by default would be mistaken for a freshly read one.
    if cache_path is not None and (
        reading is None or reading.get("headroom") != "known"
    ):
        cached = cached_account_budget(path=cache_path, now=now)
        if cached.get("headroom") == "known":
            return cached
    if reading is not None:
        return reading
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
    """Parse one event's own timestamp, or None when it carries none usable.

    The shared parser is tolerant of surrounding space and of a lowercase zone
    designator; this reader's recorded contract refuses both, so a stamp whose
    own text is not spelled strictly is read as carrying no usable moment.
    """
    value = event.get("timestamp")
    if not isinstance(value, str) or not value.strip():
        return None
    if value != value.strip() or value.endswith("z"):
        return None
    return parse_utc(value)


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
    split, _state = _timestamp_split(events, carry=None)
    return split


def _timestamp_split(
    events: Iterable[Mapping[str, Any]], *, carry: Mapping[str, Any] | None
) -> tuple[tuple[float | None, float | None], dict[str, Any]]:
    """The span split, plus the state a later read of the same stream resumes from.

    A stream is read once at a time and grows at the end, so the marks the
    split needs are carried rather than recomputed: the first mark's moment,
    the last mark's moment and whether it asked for a tool, and the machine
    seconds accumulated over the gaps already measured. Feeding only the new
    events through the same rule therefore produces the same figures the whole
    stream would, which is what lets a reader extend an observation without
    holding the bytes it came from.
    """
    state = dict(carry) if isinstance(carry, Mapping) else {}
    first: datetime | None = parse_utc(str(state.get("first") or "")) if state else None
    last: datetime | None = parse_utc(str(state.get("last") or "")) if state else None
    last_waits = bool(state.get("last_waits"))
    machine = float(state.get("machine") or 0.0)
    marks = int(state.get("marks") or 0)
    for event in events:
        timestamp = _event_timestamp(event)
        if timestamp is None:
            continue
        waits = _requests_tool(event)
        if last is not None:
            gap = (timestamp - last).total_seconds()
            if gap > 0 and last_waits:
                machine += gap
        else:
            first = timestamp
        last = timestamp
        last_waits = waits
        marks += 1
    state = {
        "first": first.isoformat() if first is not None else None,
        "last": last.isoformat() if last is not None else None,
        "last_waits": last_waits,
        "machine": machine,
        "marks": marks,
    }
    if marks < 2:
        return (None, None), state
    total = (last - first).total_seconds()
    generation = max(0.0, total - machine)
    return (round(generation, 3), round(machine, 3)), state


def _refine_throughput_from_timestamps(
    throughput: dict[str, Any],
    events: Sequence[Mapping[str, Any]],
    *,
    carry: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Prefer a timestamp-measured generation/machine split when the stream has one.

    The dialect's own figure (a backend-reported total, or an elapsed-minus-
    generation fallback) stands untouched when the stream carries fewer than
    two usable timestamps — that is the honest unknown of a stream with no
    timestamps, not a defect in this refinement. When timestamps are usable,
    machine seconds comes from them directly and generation is derived as
    elapsed minus machine, so the two always sum back to elapsed exactly.
    """
    (_generation, machine), state = _timestamp_split(events, carry=carry)
    if machine is None:
        return throughput, state
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
    return throughput, state


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


@dataclass
class _ParsedStream:
    identity: tuple[int, int, int, int]
    events: list[dict[str, Any]]
    malformed: int
    head: dict[str, Any]
    ends_newline: bool
    observations: dict[str, Observation] = field(default_factory=dict)
    timestamp_reader: object | None = None
    timestamp_bounds: (
        tuple[tuple[datetime, str] | None, tuple[datetime, str] | None] | None
    ) = None
    pending_events: list[dict[str, Any]] | None = None
    timestamp_version: int = 0


_PARSED_STREAMS: OrderedDict[str, _ParsedStream] = OrderedDict()
_PARSED_STREAM_LOCK = threading.Lock()
_PARSED_STREAM_LIMIT = 32 * 1024 * 1024


def _cached_stream_entry(path: str | Path) -> _ParsedStream:
    """Find or publish one version of a stream without locking during I/O."""
    source = Path(path)
    key = str(source.resolve())
    info = source.stat()
    identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    with _PARSED_STREAM_LOCK:
        stored = _PARSED_STREAMS.get(key)
        if stored is not None and stored.identity == identity:
            _PARSED_STREAMS.move_to_end(key)
            return stored
        prior_identity = stored.identity if stored is not None else None
        prior_events = stored.events if stored is not None else []
        prior_malformed = stored.malformed if stored is not None else 0
        prior_head = stored.head if stored is not None else None
        prior_ends_newline = stored.ends_newline if stored is not None else False
    grows = (
        prior_identity is not None
        and prior_identity[:2] == identity[:2]
        and info.st_size > prior_identity[2]
        and prior_ends_newline
        and stream_head_fingerprint(source, offset=prior_identity[2]) == prior_head
    )
    with source.open("rb") as handle:
        if grows:
            handle.seek(prior_identity[2])
        data = handle.read()
    _count_parsed_bytes(len(data))
    added, malformed = parse_events(data.decode("utf-8", errors="replace").splitlines())
    events = added
    if grows:
        events = [*prior_events, *added]
        malformed += prior_malformed
    head = stream_head_fingerprint(source, offset=info.st_size)
    with _PARSED_STREAM_LOCK:
        live = _PARSED_STREAMS.get(key)
        if live is not None and live.identity == identity:
            _PARSED_STREAMS.move_to_end(key)
            return live
        pending = [*(stored.pending_events or []), *added] if grows else None
        current = _ParsedStream(
            identity=identity,
            events=events,
            malformed=malformed,
            head=head,
            ends_newline=data.endswith(b"\n"),
            timestamp_reader=stored.timestamp_reader if grows else None,
            timestamp_bounds=stored.timestamp_bounds if grows else None,
            pending_events=pending,
        )
        if live is not None and live is not stored:
            return current
        _PARSED_STREAMS[key] = current
        _PARSED_STREAMS.move_to_end(key)
        size = sum(entry.identity[2] for entry in _PARSED_STREAMS.values())
        while size > _PARSED_STREAM_LIMIT and len(_PARSED_STREAMS) > 1:
            _unused, evicted = _PARSED_STREAMS.popitem(last=False)
            size -= evicted.identity[2]
        return current


def cached_stream_events(path: str | Path) -> tuple[list[dict[str, Any]], int]:
    """Return shared read-only-by-contract events, parsing only appended bytes."""
    entry = _cached_stream_entry(path)
    with _PARSED_STREAM_LOCK:
        return entry.events, entry.malformed


def cached_stream_timestamp_bounds(
    path: str | Path, reader: Callable[[object], datetime | None]
) -> tuple[tuple[datetime, str] | None, tuple[datetime, str] | None]:
    """Return the earliest and latest zoned stream stamps, scanning only growth."""
    entry = _cached_stream_entry(path)
    with _PARSED_STREAM_LOCK:
        version = entry.timestamp_version
        if entry.timestamp_reader is reader and entry.timestamp_bounds is not None:
            first, last = entry.timestamp_bounds
            scan = tuple(entry.pending_events or ())
        else:
            first = last = None
            scan = tuple(entry.events)
    for event in scan:
        stamp = event.get("timestamp")
        if not isinstance(stamp, str):
            continue
        parsed = reader(stamp)
        if parsed is None:
            continue
        point = (parsed, stamp)
        if first is None or parsed < first[0]:
            first = point
        if last is None or parsed > last[0]:
            last = point
    with _PARSED_STREAM_LOCK:
        if entry.timestamp_version == version:
            entry.timestamp_reader = reader
            entry.timestamp_bounds = (first, last)
            entry.pending_events = None
            entry.timestamp_version += 1
    return first, last


def _apply_receipt_to_throughput(
    throughput: dict[str, Any], receipt: object
) -> dict[str, Any]:
    """Fold a client rollout's measurements into a codex throughput block.

    The codex exec stream reports only aggregate per-turn usage; the per-session
    rollout is the client's own per-request record, and its measured model span
    and reset-aware cumulative input are authoritative over the stream for a
    run the rollout covers.  Fields the receipt could not measure stay as the
    stream reported them — an absent measurement is never overwritten by a
    marker.
    """
    generation = getattr(receipt, "generation_seconds", None)
    machine = getattr(receipt, "machine_seconds", None)
    if (
        isinstance(generation, (int, float))
        and not isinstance(generation, bool)
        and isinstance(machine, (int, float))
        and not isinstance(machine, bool)
    ):
        throughput["generation_seconds"] = float(generation)
        throughput["machine_seconds"] = float(machine)
        throughput["tokens_per_second"] = _rate(
            throughput.get("generated_tokens"), generation
        )
    cumulative = getattr(receipt, "cumulative_input_tokens", None)
    if isinstance(cumulative, int) and not isinstance(cumulative, bool):
        throughput["cumulative_input_tokens"] = cumulative
    cached = getattr(receipt, "cumulative_cached_input_tokens", None)
    if isinstance(cached, int) and not isinstance(cached, bool):
        throughput["cumulative_cached_input_tokens"] = cached
    throughput["detail"] = (
        "model span bounded from the client rollout's tool spans; "
        "input cumulative summed across resets"
    )
    return throughput


def _carried_malformed(state: Mapping[str, Any]) -> int:
    """The malformed-line total a previous observation of this stream reached."""
    carried = state.get("malformed") if state else None
    if isinstance(carried, int) and not isinstance(carried, bool):
        return carried
    return 0


def observe_stream(
    *,
    backend_name: str,
    backend: Mapping[str, Any],
    lines: Iterable[str],
    elapsed_seconds: float | None = None,
    receipt: object | None = None,
    state: Mapping[str, Any] | None = None,
) -> Observation:
    """Fold a backend's recorded event stream into one normalised observation.

    ``elapsed_seconds`` lets a caller that knows when the run started supply the
    span a dialect's own stream may not report, so a rate is available for every
    harness rather than only the one that times itself.  ``receipt`` is the
    client rollout's per-session reading for a codex run; when supplied and
    measured, it replaces the stream's own generation span and cumulative input
    with the rollout's authoritative figures.

    ``state`` is a previous observation of the same stream, taken from its
    ``stream_state``, and ``lines`` then carries only the records written since
    that observation stopped. The fold is extended rather than repeated, and the
    new state is left on the returned observation for the next read. The
    malformed-line count describes the whole stream rather than the segment just
    parsed, so it is carried in that state as well — a resumed observation
    reports the stream's total, not only the appended tail's.
    """
    events, malformed = parse_events(lines)
    return _observe_events(
        backend_name=backend_name,
        backend=backend,
        events=events,
        malformed=malformed,
        elapsed_seconds=elapsed_seconds,
        receipt=receipt,
        state=state,
    )


def _observe_events(
    *,
    backend_name: str,
    backend: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    malformed: int,
    elapsed_seconds: float | None = None,
    receipt: object | None = None,
    state: Mapping[str, Any] | None = None,
) -> Observation:
    dialect = dialect_for(backend)
    carried = state if isinstance(state, Mapping) else {}
    obs = dialect.observe(
        events,
        elapsed_seconds=elapsed_seconds,
        backend_name=backend_name,
        usable_input_window=backend.get("usable_input_window"),
        carry=carried.get("fold") if carried else None,
    )
    obs.backend = backend_name
    obs.malformed_lines = malformed + _carried_malformed(carried)
    obs.stream_state["malformed"] = obs.malformed_lines
    obs.throughput, timestamps = _refine_throughput_from_timestamps(
        obs.throughput,
        events,
        carry=carried.get("timestamps") if carried else None,
    )
    obs.stream_state["timestamps"] = timestamps
    if receipt is not None and getattr(dialect, "name", "") == "codex":
        obs.throughput = _apply_receipt_to_throughput(obs.throughput, receipt)
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


_STREAM_BOUNDARY_CHUNK = 64 * 1024

# How much of a stream's opening a cursor records, so a later read can tell an
# append from a rewrite. The inode and the size together catch a stream replaced
# or truncated, but a stream rewritten in place at the same inode to a size at or
# above the recorded offset — which is what a producer's stream does between
# in-place rewrites — is otherwise indistinguishable from an append by stat
# alone. The digest covers a bounded opening rather than the whole consumed
# prefix, so the check costs a fixed small read rather than re-reading the bytes
# the cursor exists to skip.
_STREAM_HEAD_SAMPLE = 8 * 1024


def _stream_head_size(offset: int) -> int:
    """How many opening bytes a cursor at ``offset`` records for the rewrite check."""
    return max(0, min(int(offset), _STREAM_HEAD_SAMPLE))


def stream_head_fingerprint(path: str | Path, *, offset: int) -> dict[str, Any]:
    """Digest a stream's opening bytes, for a later read to confirm they hold.

    ``offset`` is the cursor the digest is recorded for: the sample is bounded by
    both the sample size and that offset, so a file shorter than the sample is
    identified by its whole prefix exactly. The digest is returned with the byte
    count it covered, and a later read compares against that same count rather
    than the sample size, so an appended stream that has since grown past the
    sample is not mistaken for one whose opening moved.
    """
    limit = _stream_head_size(offset)
    try:
        with Path(path).open("rb") as handle:
            head = handle.read(limit) if limit else b""
    except OSError:
        return {"bytes": 0, "digest": ""}
    return {"bytes": len(head), "digest": hashlib.sha256(head).hexdigest()}


def _stream_head_intact(path: str | Path, recorded: object) -> bool:
    """Whether a stream's opening is the one the cursor recorded.

    A resume is valid only while the bytes the cursor already consumed are the
    ones it consumed. A stream whose opening is not what the cursor recorded was
    rewritten under it, so the offset means nothing and the read starts at the
    first record; an absent or empty fingerprint is unverifiable and equally
    refuses the resume rather than resuming blind.
    """
    if not isinstance(recorded, Mapping):
        return False
    try:
        count = int(recorded.get("bytes") or 0)
    except (TypeError, ValueError):
        return False
    digest = str(recorded.get("digest") or "")
    if count <= 0 or not digest:
        return False
    try:
        with Path(path).open("rb") as handle:
            head = handle.read(count)
    except OSError:
        return False
    if len(head) != count:
        return False
    return hashlib.sha256(head).hexdigest() == digest


# Bytes of stream records the readers have consumed since the count was last
# taken. A producer takes it once a poll to report how much of the fleet's
# stream traffic that poll actually parsed; no other reader consults it, and a
# poll that resumes from every cursor and finds nothing appended leaves it at
# nothing. It is a one-element cell so the two readers below mutate it without a
# module-level global statement.
_PARSED_STREAM_BYTES = [0]


def _count_parsed_bytes(count: int) -> None:
    if count > 0:
        _PARSED_STREAM_BYTES[0] += count


def take_parsed_stream_bytes() -> int:
    """Bytes of stream records read since this was last taken, then reset."""
    value = _PARSED_STREAM_BYTES[0]
    _PARSED_STREAM_BYTES[0] = 0
    return value


def _last_line_boundary(path: Path) -> int:
    """The byte after the last complete record in a stream, 0 when there is none.

    An offset only means the same thing twice if it sits where a record ends, so
    the byte a resumable read records is the one after the last newline at or
    before the file's size. A stream whose tail holds no newline answers 0,
    which re-reads it whole rather than resuming inside a record.
    """
    try:
        size = path.stat().st_size
        if size <= 0:
            return 0
        with path.open("rb") as handle:
            start = max(0, size - _STREAM_BOUNDARY_CHUNK)
            handle.seek(start)
            tail = handle.read()
    except OSError:
        return 0
    cut = tail.rfind(b"\n")
    if cut < 0:
        return 0
    return start + cut + 1


def _stream_lines_from(path: Path, offset: int) -> tuple[list[str], int]:
    """The complete records a stream holds from one boundary, and the next one.

    Only whole lines are returned, and the offset that comes back sits after
    the last of them. A record still being written is therefore left for the
    read that finds it finished, which is the difference between reading a
    record once and reading it as a fragment followed by its remainder.
    """
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read()
    except OSError:
        return [], offset
    cut = data.rfind(b"\n")
    if cut < 0:
        return [], offset
    text = data[: cut + 1].decode("utf-8", errors="replace")
    _count_parsed_bytes(cut + 1)
    return text.splitlines(keepends=True), offset + cut + 1


def observe_log(
    *,
    backend_name: str,
    backend: Mapping[str, Any],
    log_path: str | Path,
    elapsed_seconds: float | None = None,
    receipt: object | None = None,
    resume: Mapping[str, Any] | None = None,
) -> Observation:
    """Observe a worker from its on-disk event log, absent log included.

    An absent log is the ordinary state of a run whose process has not yet
    written anything, so it reports ``starting`` rather than failing.  A
    ``receipt`` is forwarded to the stream observer for a codex run whose
    client rollout the caller has already read.

    ``resume`` is a previous observation of this stream — its ``stream_state``
    together with the byte offset that observation reached and the fingerprint of
    the stream's opening it read — so a stream that has only grown since is read
    from that offset rather than from the first record. An offset past the end of
    the file, one with no state to extend, or one whose stream's opening is no
    longer the recorded one reads the stream whole, which is what a truncated,
    replaced or in-place-rewritten stream needs.

    The run's login record is read alongside the stream, so a run that blanked
    the operator's codex credential is reported wherever the run is observed
    rather than only where its record is opened by hand; see
    :func:`observe_codex_login`. Every post-run fold writes the observed detail
    onto the run's record, so the truncation lands there as well as in the
    stream's own reading.
    """
    observation = _observe_run_stream(
        backend_name=backend_name,
        backend=backend,
        log_path=log_path,
        elapsed_seconds=elapsed_seconds,
        receipt=receipt,
        resume=resume,
    )
    report = observe_codex_login(Path(log_path).parent)
    if report is not None:
        note = codex_login_truncation_detail(report)
        observation.detail = (
            f"{observation.detail} | {note}" if observation.detail else note
        )
    return observation


def _observe_run_stream(
    *,
    backend_name: str,
    backend: Mapping[str, Any],
    log_path: str | Path,
    elapsed_seconds: float | None = None,
    receipt: object | None = None,
    resume: Mapping[str, Any] | None = None,
) -> Observation:
    """Read one run's event stream into an observation."""
    path = Path(log_path)
    if not path.exists():
        obs = Observation(
            backend=backend_name,
            budget=unknown_budget("no event log yet"),
            throughput=unknown_throughput("no event log yet"),
            detail=f"event log not written yet: {path}",
        )
        return obs
    carried = resume if isinstance(resume, Mapping) else {}
    state = carried.get("state")
    try:
        offset = max(0, int(carried.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0
    key = str(path.resolve())
    with _PARSED_STREAM_LOCK:
        cache_present = key in _PARSED_STREAMS
    if offset > 0 and isinstance(state, Mapping) and not cache_present:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if offset <= size and _stream_head_intact(path, carried.get("head")):
            lines, end = _stream_lines_from(path, offset)
            obs = observe_stream(
                backend_name=backend_name,
                backend=backend,
                lines=lines,
                elapsed_seconds=elapsed_seconds,
                receipt=receipt,
                state=state,
            )
            obs.stream_state["offset"] = end
            return obs
    events, malformed = cached_stream_events(path)
    settings = json.dumps(backend, sort_keys=True, default=repr)
    observation_key = repr((backend_name, settings, elapsed_seconds, receipt))
    with _PARSED_STREAM_LOCK:
        entry = _PARSED_STREAMS.get(key)
        if entry is not None and entry.events is events:
            cached = entry.observations.get(observation_key)
        else:
            entry = None
            cached = None
    if cached is not None:
        return copy.deepcopy(cached)
    obs = _observe_events(
        backend_name=backend_name,
        backend=backend,
        events=events,
        malformed=malformed,
        elapsed_seconds=elapsed_seconds,
        receipt=receipt,
    )
    obs.stream_state["offset"] = _last_line_boundary(path)
    if entry is not None:
        with _PARSED_STREAM_LOCK:
            entry.observations.setdefault(observation_key, copy.deepcopy(obs))
    return obs
