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
    stream interpretation   terminal event, final message, budget signal

Budget is deliberately asymmetric and must stay that way. One harness reports
utilisation and a reset time on its run stream; another reports only tokens
spent there, with no headroom at all. So an observation carries
``headroom: "unknown"`` rather than a guess, and :func:`budget_exhausted`
answers ``None`` where nothing is known. Absence of a signal is never read as
exhaustion.

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
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

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
    """

    backend: str
    session_id: str | None = None
    phase: str = "starting"
    terminal: bool = False
    exit_status: str | None = None
    final_message: str | None = None
    budget: dict[str, Any] = field(default_factory=dict)
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
        }


@dataclass(frozen=True)
class LaunchPlan:
    """A runnable invocation: what to execute, where, and what to feed it."""

    backend: str
    dialect: str
    argv: list[str]
    cwd: str
    stdin_text: str
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
        "detail": reason,
    }


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

    def observe(self, events: Iterable[Mapping[str, Any]]) -> Observation:
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

    def observe(self, events: Iterable[Mapping[str, Any]]) -> Observation:
        obs = Observation(backend=self.name, budget=unknown_budget(""))
        message: str | None = None
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
                obs.budget = self._budget(event.get("usage"))
            elif kind in ("turn.failed", "thread.error", "error", "stream.error"):
                obs.terminal = True
                obs.exit_status = "error"
                obs.detail = _error_detail(event)
        obs.final_message = message
        obs.phase = _phase(obs)
        return obs

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
        # a prompt argument after it is read as another directory.
        argv += ["--add-dir", worktree, *writable_directories]
        return argv

    def _sandbox_flags(self, tier: str | None) -> list[str]:
        if tier == WORKTREE_FULL:
            return ["--dangerously-skip-permissions"]
        if tier in (READ_ONLY, WORKSPACE_WRITE):
            # This harness expresses restraint as a permission mode rather than
            # a filesystem sandbox; `plan` is the mode that withholds writes.
            return ["--permission-mode", "plan"]
        return ["--permission-mode", "plan"]

    def observe(self, events: Iterable[Mapping[str, Any]]) -> Observation:
        obs = Observation(backend=self.name, budget=unknown_budget(""))
        message: str | None = None
        budget = unknown_budget("no rate-limit event in the stream yet")
        for event in events:
            obs.events += 1
            kind = event.get("type")
            if kind == "system" and event.get("subtype") == "init":
                obs.session_id = event.get("session_id") or obs.session_id
            elif kind == "rate_limit_event":
                budget = self._budget(event.get("rate_limit_info"))
            elif kind == "assistant":
                message = _assistant_text(event) or message
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
            if obs.session_id is None:
                # Every event of this stream carries the session id, including
                # the hook events a host configuration may emit before init.
                obs.session_id = event.get("session_id") or obs.session_id
        obs.budget = budget
        obs.final_message = message
        obs.phase = _phase(obs)
        return obs

    def _budget(self, info: Any) -> dict[str, Any]:
        """Parse reported utilisation and reset time into the shared block.

        This dialect declares no probe because it needs none: headroom arrives on
        the run stream every worker already writes, so a pre-flight reading past
        runs learns it for free and a separate process would add nothing.
        """
        if not isinstance(info, Mapping):
            return unknown_budget("rate-limit event carried no information")
        utilisation = info.get("utilization")
        if not isinstance(utilisation, (int, float)) or isinstance(utilisation, bool):
            return unknown_budget("rate-limit event carried no numeric utilisation")
        budget = unknown_budget("")
        budget.update(
            {
                "headroom": "known",
                "utilisation_pct": float(utilisation),
                "rate_limit_type": info.get("rateLimitType"),
                "rate_limit_period_minutes": info.get("windowDurationMins"),
                "resets_at": _epoch_to_iso(info.get("resetsAt")),
                "threshold_status": info.get("status"),
                "surpassed_threshold": info.get("surpassedThreshold"),
                "detail": "backend reports utilisation and reset time",
            }
        )
        return budget


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
    """Derive the reported phase from what the stream contains."""
    if obs.terminal:
        return "complete" if obs.exit_status == "ok" else "failed"
    return "working" if obs.events else "starting"


_DIALECTS: dict[str, Dialect] = {
    _CodexDialect.name: _CodexDialect(),
    _ClaudeDialect.name: _ClaudeDialect(),
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
) -> Observation:
    """Fold a backend's recorded event stream into one normalised observation."""
    dialect = dialect_for(backend)
    events, malformed = parse_events(lines)
    obs = dialect.observe(events)
    obs.backend = backend_name
    obs.malformed_lines = malformed
    return obs


def observe_log(
    *,
    backend_name: str,
    backend: Mapping[str, Any],
    log_path: str | Path,
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
            detail=f"event log not written yet: {path}",
        )
        return obs
    with path.open(encoding="utf-8", errors="replace") as handle:
        return observe_stream(backend_name=backend_name, backend=backend, lines=handle)
