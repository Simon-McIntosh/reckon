from __future__ import annotations

import contextlib
import fcntl
import hashlib
import importlib
import json
import math
import os
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timezone
from functools import lru_cache
from pathlib import Path
from statistics import median
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from reckon import ledger, review_tiers
from reckon._timestamps import parse_utc
from reckon.capabilities import _charged_input_from_usage
from reckon.crew import lane_document as _lane_document
from reckon.crew import metering, plan_review, quota_weight, runs
from reckon.crew import repair as repair_module
from reckon.crew import review as review_module
from reckon.crew import review_need
from reckon.crew.host_lease import LEASE_RENEW_SECONDS
from reckon.crew.node import (
    _TERMINAL_RUN_PHASES,
    DEFAULT_WATCH_STALL_WINDOW,
    INTERRUPTED_RUN_PHASE,
    LOG_STALE_AFTER_SECONDS,
    CrewError,
    parse_duration,
)
from reckon.crew.reports import (
    NON_TERMINAL_MANIFEST_STATUSES,
    TERMINAL_MANIFEST_STATUSES,
    ManifestParseError,
    manifest_status_is_template,
    parse_manifest,
)
from reckon.crew.routing import _signal_process_group
from reckon.crew.runs import (
    _manifest_freshness,
    _mutate_pointer,
    _process_start_time,
    _project_watch_claim,
    _read_watch_record,
    _stream_quiet_seconds,
    _utc_now,
    _write_watch_record,
    list_live,
    producer_lease_seconds,
    read_pointer,
    update_watch_registration,
    watch_lease_renewed_at,
    watch_lock_path,
)
from reckon.crew.ticker import NEEDS_ACTION, Ticker, _agent_label



def _background_wait_signal(record: Mapping[str, Any]) -> str | None:
    """The one sentence proving a vanished process was waiting on background work.

    A dead process with no complete manifest is indistinguishable from one
    that simply crashed, unless the run directory itself says otherwise. Two
    traces say otherwise: the harness's own ceiling message on stderr, or the
    agent's last turn stating in its own words that it was waiting on
    background work before finalizing the manifest — with nothing after that
    turn because a print-mode invocation has no next one to write. Neither is
    a crash; both name a run whose session is intact and whose only
    outstanding step is a resume long enough to collect the manifest it was
    already about to write.
    """
    stderr_path = record.get("stderr_path")
    if stderr_path:
        try:
            stderr_text = Path(str(stderr_path)).read_text()
        except OSError:
            stderr_text = ""
        if _BACKGROUND_WAIT_CEILING_RE.search(stderr_text):
            return (
                "the worker's stderr recorded the background-wait ceiling "
                "before the process terminated"
            )

    final_message = str(record.get("final_message") or "")
    if not final_message and record.get("launch") == "cli":
        # observe() folds the stream's final message onto the pointer, but a
        # caller reading the raw pointer — the watch producer's path — has
        # none of it cached yet. Reading the log directly keeps that path
        # answering the same question the folded record would; the shared read
        # means a classification that already parsed this stream pays nothing
        # for asking a second question of the same bytes.
        seen = _observed_stream(record, memo=_memo_for(record))
        if seen is not None:
            final_message = str(seen.get("final_message") or "")

    if final_message and _BACKGROUND_WAIT_FINAL_MESSAGE_RE.search(final_message):
        return (
            "the worker's last turn reported waiting on background work "
            f"before finalizing the manifest: {final_message.strip()}"
        )
    return None


# A tool call whose own contract ends the wait. A quiet stream is read as a hang
# unless the last thing the worker asked for was something that ends on its own:
# a bounded sleep, the peer channel's bounded read, or a task wait that cannot
# outlive its window. Only the last assistant turn is consulted, so a hang that
# follows an earlier sleep still reads as a hang.
_BOUNDED_WAIT_TOOL_NAMES = frozenset({"TaskOutput", "TaskOutputFull", "ScheduleWakeup"})
# The double dash takes no leading word boundary — ``--wait`` follows a space,
# and neither is a word character — so only the trailing boundary is anchored.
_PEER_CHANNEL_WAIT_RE = re.compile(r"peer-read[^\n]*--wait\b", re.IGNORECASE)
_SLEEP_RE = re.compile(r"(?:\btime\.)?\bsleep\s+(\d+)", re.IGNORECASE)


def _last_bounded_wait(record: Mapping[str, Any]) -> str | None:
    """The last tool call's bounded wait, named, or None when there is none.

    Reads only the tool call the worker most recently started, because that is
    the call a quiet stream is currently sitting in. A poll loop that sleeps is
    bounded by its own sleeps; a peer-channel read with a ``--wait`` is bounded
    by that duration; a task wait is bounded by its own contract. None of these
    need a person — each wakes itself, which is what separates them from a hang.
    """
    log = Path(str(record.get("log_path") or ""))
    if not log.is_file():
        return None
    try:
        with log.open(encoding="utf-8", errors="replace") as handle:
            last_name = ""
            last_command = ""
            for line in handle:
                try:
                    event = json.loads(line)
                except (ValueError, TypeError):
                    continue
                message = event.get("message")
                if not isinstance(message, Mapping):
                    continue
                content = message.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if (
                        not isinstance(block, Mapping)
                        or block.get("type") != "tool_use"
                    ):
                        continue
                    name = str(block.get("name") or "")
                    command = ""
                    if isinstance(block.get("input"), Mapping):
                        command = str(
                            block["input"].get("command")
                            or block["input"].get("prompt")
                            or ""
                        )
                    last_name, last_command = name, command
    except OSError:
        return None
    if not last_name and not last_command:
        return None
    if last_name in _BOUNDED_WAIT_TOOL_NAMES:
        return f"a {last_name} task wait"
    if _PEER_CHANNEL_WAIT_RE.search(last_command):
        return "a peer-channel read with a bounded wait"
    match = _SLEEP_RE.search(last_command)
    if match is not None:
        return f"a {match.group(1)}s sleep"
    return None


def _stall_wait_reason(record: Mapping[str, Any]) -> str | None:
    """Why a quiet, alive run is paused rather than hung, or None.

    Three shapes turn a quiet stream into a wait instead of a stall: the worker
    is mid-retry on a rate limit (the lane's window resets on its own), its
    last request was refused on a rate-limit window that resets, or its last
    tool call was a bounded wait (it wakes itself). A run with none of these is
    genuinely hung and must stay stalled, and a live rate-limit retry loop that
    is still emitting keeps reading as working — only a quiet one is arbitrated
    here.
    """
    budget = _stream_budget(record)
    if budget is not None and not budget.get("refusal"):
        retry = _stream_retry_block(record, budget)
        if retry is not None:
            return (
                f"a rate-limit retry loop ({retry['retries']} retries); "
                "the lane's window resets and the loop keeps the session alive"
            )
        hold = _budget_hold_block(record, budget)
        if hold is not None:
            return (
                f"a rejected {hold['limit_kind']} window that resets "
                f"{hold['resets_at']}"
            )
    return _last_bounded_wait(record)


def _wait_probe(value: Any) -> list[str]:
    """Read a shell-free argument vector from a waiting manifest."""
    if isinstance(value, list):
        probe = value
    else:
        try:
            probe = json.loads(str(value))
        except (TypeError, json.JSONDecodeError):
            return []
    if not isinstance(probe, list) or not probe:
        return []
    if any(not isinstance(item, str) or not item.strip() for item in probe):
        return []
    return [item.strip() for item in probe]


# The shapes a wait declaration's condition can take, named wherever one is
# refused. Three workers wrote the wait block three wrong ways in one hour, each
# with the right key names and a value the reader discarded: the run then read
# to a coordinator as a worker that had declared nothing, so the repair could
# not be made from the row. A reader that silently reduces an unrecognised shape
# to the absence of a declaration is the defect; naming what it does accept in
# the refusal is what removes it.
_WAIT_ACCEPTED_SHAPES = (
    "the reader accepts a shell-free argument vector whose first element is a "
    "bare program name, or a file condition declared as wait_file with one "
    "path or a JSON array of paths"
)


def _wait_file_paths(value: Any) -> list[str]:
    """Read the paths a file condition names.

    One path is a plain scalar and several are a JSON array, the same pair of
    forms the terminal list accepts -- except that a scalar is never
    comma-split here, because a comma inside a path is part of the path and a
    reader that split it would invent two paths that do not exist. A value that
    is present and is neither of those forms reads as no paths, so a caller
    telling the shapes apart can refuse it rather than read it as an undeclared
    condition.
    """
    if isinstance(value, list):
        if any(not isinstance(item, str) or not item.strip() for item in value):
            return []
        return [item.strip() for item in value]
    text = str(value or "").strip()
    if not text:
        return []
    if not text.lstrip().startswith("["):
        return [text]
    try:
        parsed = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list) or any(
        not isinstance(item, str) or not item.strip() for item in parsed
    ):
        return []
    return [item.strip() for item in parsed]


def _wait_probe_shape_refusal(value: Any) -> str:
    """The reason a declared probe is not a shape the reader can run, or "".

    Absence is not a refusal: a declaration carrying no probe is incomplete,
    which the reader reports by naming the missing field, and the two outcomes
    stay distinguishable. Refused is a probe that is present and unreadable,
    because that is the shape that used to reduce silently to the absence of a
    probe -- and a worker reading its own row was then told it had declared
    nothing when it had declared something.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return ""
    candidate: Any = value
    if isinstance(value, str):
        try:
            candidate = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return (
                "wait_probe is a single value the reader cannot parse as a "
                f"JSON array; {_WAIT_ACCEPTED_SHAPES}"
            )
    if not isinstance(candidate, list):
        return f"wait_probe is a scalar rather than a list; {_WAIT_ACCEPTED_SHAPES}"
    if not candidate:
        return ""
    if any(not isinstance(item, str) or not item.strip() for item in candidate):
        return (
            "wait_probe is a list of mappings rather than of program "
            f"arguments; {_WAIT_ACCEPTED_SHAPES}"
        )
    first = candidate[0].strip()
    if not first or " " in first or "\t" in first:
        return (
            f"wait_probe starts with {first!r}, a whole command line rather "
            f"than a program name, so nothing can exec it; {_WAIT_ACCEPTED_SHAPES}"
        )
    return ""


def _wait_file_shape_refusal(value: Any) -> str:
    """The reason a declared file condition is not a shape the reader reads."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return ""
    if isinstance(value, list):
        if not value:
            return ""
        if any(not isinstance(item, str) or not item.strip() for item in value):
            return (
                "wait_file is a list of mappings rather than of paths; "
                f"{_WAIT_ACCEPTED_SHAPES}"
            )
        return ""
    if isinstance(value, str):
        if not value.lstrip().startswith("["):
            return ""
        if not _wait_file_paths(value):
            return (
                "wait_file opens with a bracket but does not parse as a JSON "
                f"array of paths; {_WAIT_ACCEPTED_SHAPES}"
            )
        return ""
    return f"wait_file is neither a path nor a list of paths; {_WAIT_ACCEPTED_SHAPES}"


# A terminal value names a state the probe prints. The exit-code sentinel is
# not one: the observation the reader matches against is the probe's own output,
# so a declaration whose terminal is an exit status reads as pending on every
# sweep of a job that has already ended, and the run never lifts.
_WAIT_EXIT_SENTINEL = re.compile(r"exit:\s*\d+\s*$", re.IGNORECASE)


def _wait_terminal_names_no_probe_state(terminal: Sequence[str]) -> str:
    """Name a terminal value that is not a state any probe prints, or ""."""
    for value in terminal:
        spelled = str(value).strip()
        if spelled and _WAIT_EXIT_SENTINEL.match(spelled):
            return spelled
    return ""


def _wait_file_probe(files: Sequence[str]) -> list[str]:
    """The shell-free argument vector a file condition derives.

    The vector is the shape a file condition takes so it reads like every other
    wait: ``test -e`` per path joined by ``-a`` exits 0 exactly when every
    declared path exists, and the terminal the declaration needs is the
    ``exit:0`` sentinel that exit status prints.

    Two readers exist, and only one of them runs this vector. The sweep's
    reader in ``reckon/crew/resumption.py`` executes it and is what decides a
    lift, so this vector is the load-bearing half for a park. The classifier's
    reader -- ``_run_wait_condition_probe`` below -- answers a file condition by
    looking for the paths themselves and returns before any vector runs, so a
    row can name which paths are still missing. The two agree on the answer and
    not on the mechanism; nothing here is run by the classifier.
    """
    argv = ["test"]
    for index, path in enumerate(files):
        if index:
            argv.append("-a")
        argv.extend(["-e", path])
    return argv


def _wait_terminal_values(value: Any) -> list[str]:
    """Read the external states that mean a condition has terminated.

    A terminal-state list is read from the same forms the probe accepts: a
    list, or a JSON array written as a string, each validated the same way --
    a list whose members are all non-empty strings. A value whose first
    non-space character is an opening square bracket is read as JSON only:
    when it parses to a list of non-empty strings those strings are the
    states, and when it does not parse the result is no states at all, so a
    manifest written that way is reported as an incomplete wait declaration
    rather than honoured with state names carrying JSON punctuation. The
    comma-separated form keeps working for briefs and manifests in flight,
    and is never applied to a value that opens with a bracket: comma-splitting
    a malformed array is what produces a state name containing a bracket.
    """
    if isinstance(value, list):
        if any(not isinstance(i, str) or not i.strip() for i in value):
            return []
        states = value
    else:
        text = str(value or "")
        if text.lstrip().startswith("["):
            try:
                parsed = json.loads(text)
            except (TypeError, json.JSONDecodeError):
                return []
            if not isinstance(parsed, list) or any(
                not isinstance(i, str) or not i.strip() for i in parsed
            ):
                return []
            states = parsed
        else:
            states = text.split(",")
    return [str(item).strip() for item in states if str(item).strip()]


def _wait_condition_observation(
    value: Any, *, terminal_values: list[str]
) -> dict[str, str]:
    """Normalise a probe result into pending, met, or unknown."""
    if isinstance(value, bool):
        return {
            "state": "met" if value else "pending",
            "observed": "terminal" if value else "pending",
            "detail": "condition test returned a boolean verdict",
        }
    if isinstance(value, Mapping):
        state = str(value.get("state") or "").strip().lower()
        if state not in WAIT_CONDITION_STATES:
            terminal = value.get("terminal")
            state = (
                "met"
                if terminal is True
                else "pending"
                if terminal is False
                else "unknown"
            )
        return {
            "state": state,
            "observed": str(value.get("observed") or state),
            "detail": str(value.get("detail") or "condition test returned a verdict"),
        }
    observed = str(value or "").strip()
    terminal = {item.casefold() for item in terminal_values}
    return {
        "state": "met" if observed.casefold() in terminal else "unknown",
        "observed": observed or "unavailable",
        "detail": "condition test returned an unstructured observation",
    }


def _wait_file_condition_observation(
    record: Mapping[str, Any], files: Sequence[str]
) -> dict[str, str]:
    """Read a file condition by looking for the paths it declares.

    The condition is met when every path exists, and the paths still missing
    are named, so a row says which one the wait is on rather than only that
    something is absent. A relative path resolves against the run's worktree,
    which is where the worker that declared it was running.
    """
    worktree = Path(str(record.get("worktree") or "."))
    root = worktree if worktree.is_dir() else Path(".")

    def _resolved(path: str) -> Path:
        candidate = Path(path)
        return candidate if candidate.is_absolute() else root / candidate

    missing = [path for path in files if not _resolved(path).exists()]
    if missing:
        return {
            "state": "pending",
            "observed": "absent",
            "detail": (
                f"{len(missing)} of {len(files)} declared paths are not there "
                f"yet: {', '.join(missing)}"
            ),
        }
    return {
        "state": "met",
        "observed": "present",
        "detail": f"all {len(files)} declared paths exist",
    }


def _run_wait_condition_probe(
    record: Mapping[str, Any], wait: Mapping[str, Any]
) -> dict[str, str]:
    """Answer a declared condition for a classifier row, as a tri-state.

    This is the classifier's reader, not the sweep's. Two things separate them,
    and both are deliberate:

    * A file condition is answered here by looking for each declared path, so
      the row can name the ones still missing, and this function returns before
      any argument vector runs. The vector the declaration derives is run by
      the sweep's reader in ``reckon/crew/resumption.py`` -- the reader that
      decides whether a park lifts -- and not here.
    * A vector that prints nothing and exits is a terminal state only for the
      sweep's reader, which falls back to ``exit:<code>`` as the worker
      protocol documents. Here an empty answer is ``unknown``: a classifier row
      is read by a person, and reporting a state the probe never printed would
      have the row assert more than the probe said.

    The two are therefore not one implementation under two names, and a caller
    must not treat them as interchangeable: a verdict from here reaches a
    reader, a verdict from the sweep's reader lifts a run.
    """
    files = [str(path) for path in (wait.get("files") or ())]
    if files:
        return _wait_file_condition_observation(record, files)
    worktree = Path(str(record.get("worktree") or "."))
    try:
        completed = subprocess.run(
            list(wait.get("probe") or ()),
            cwd=worktree if worktree.is_dir() else None,
            text=True,
            capture_output=True,
            check=False,
            timeout=WAIT_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "state": "unknown",
            "observed": "unavailable",
            "detail": f"condition probe could not answer: {exc}",
        }
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    observed = lines[-1] if lines else "unavailable"
    if completed.returncode != 0 or not lines:
        return {
            "state": "unknown",
            "observed": observed,
            "detail": (
                f"condition probe did not answer successfully; exit "
                f"{completed.returncode}"
            ),
        }
    candidates = {observed.casefold()}
    candidates.update(
        line.split(maxsplit=1)[0].rstrip("+").casefold() for line in lines
    )
    terminal = {str(value).strip().casefold() for value in wait.get("terminal") or ()}
    if candidates & terminal:
        return {
            "state": "met",
            "observed": observed,
            "detail": f"condition probe reported terminal state {observed!r}",
        }
    return {
        "state": "unknown",
        "observed": observed,
        "detail": (
            f"condition probe reported {observed!r}, which matches no declared "
            "terminal state"
        ),
    }


_WAIT_HORIZON_FIELDS = (
    "wait_expected_seconds",
    "wait_expected",
    "wait_horizon",
)
_WAIT_DECLARATION_SCALAR_FIELDS = (
    *_WAIT_HORIZON_FIELDS,
    "wait_started_at",
)


def _unquote_wait_declaration_scalar(value: Any) -> str:
    """Return a scalar value after removing one matched pair of quotes."""
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    return text


def _wait_expected_seconds(
    manifest_data: Mapping[str, Any], *, default_seconds: int
) -> tuple[int, str]:
    """Read a declared wait horizon, retaining the existing default if absent."""
    field = ""
    value: Any = None
    for candidate in _WAIT_HORIZON_FIELDS:
        if candidate in manifest_data:
            field = candidate
            value = manifest_data.get(candidate)
            break
    if not field:
        return int(default_seconds), ""
    try:
        if field == "wait_expected_seconds" and not isinstance(value, str):
            seconds = int(value)
        else:
            seconds = parse_duration(_unquote_wait_declaration_scalar(value))
    except (CrewError, TypeError, ValueError):
        return int(default_seconds), f"readable positive {field}"
    if seconds <= 0:
        return int(default_seconds), f"positive {field}"
    return seconds, ""


def _wait_condition_declares_no_wait(condition: str) -> bool:
    """True when the condition's own prose says there is nothing to wait on.

    A worker told to write its manifest before starting long output writes its
    first one at orientation, and a healthy worker offered wait fields at that
    moment fills them with a note about where it is rather than what is awaited.
    The same happens later and on purpose: a worker recording a durable
    checkpoint before a long compose step writes the fields deliberately and
    says so in the condition. Recorded notes read: a condition opening with the
    word ``none`` ("none - this is an interim checkpoint, not a held wait"),
    read here exactly as the changed-paths prose-none rule reads that word — the
    sentence must *open* with it, so a real condition that merely mentions
    ``none`` later is left alone; the phrase written while the condition is
    still unestablished ("exploring; not yet set"); and a sentence stating in
    plain English that nothing is being awaited, in either wording a worker
    reached for: "no external condition is awaited; this is an interim progress
    record written before the report is composed", and the ledger's own
    "no external resource is awaited - this first write records orientation
    before the first edit". Every one of those declared its own absence, and
    each was escalated anyway on the presence of the field alone.
    """
    text = condition.strip()
    if re.match(r"none(?:\s|$)", text, re.IGNORECASE):
        return True
    if re.search(r"\bnot yet set\b", text, re.IGNORECASE):
        return True
    if re.search(r"\bno\s+external\b[^.;]{0,60}\bawaited\b", text, re.IGNORECASE):
        return True
    return bool(
        re.search(r"\bnothing\s+(?:is\s+|to\s+be\s+)?awaited\b", text, re.IGNORECASE)
    )


# A probe that cannot report a pending state is not a probe: the null command
# succeeds whatever is happening and prints nothing, so a declaration resting on
# it reads terminal on every sweep, however completely the other fields are
# filled in.
_WAIT_PROBE_NO_OP_COMMANDS = frozenset({"true", ":", "exit"})

# A probe also has to be able to differ. ``echo pending`` prints a constant and
# ``git rev-parse HEAD`` reports the worker's own tree; neither can change
# between sweeps however the awaited work is doing, so both are satisfied
# unconditionally and a wait resting on one tests nothing. What makes a probe
# able to differ is a reference to something outside the worker, and the
# references a wait actually rests on are a job id, a pid, a port and a path.
# That reference is resolved rather than inferred from the characters the
# vector happens to carry: ``git rev-parse HEAD2`` carries a digit and still
# names nothing a sweep could read, so a token counts only when its place in
# the vector gives it a kind. A job id and a port are named by the flag they
# follow (``squeue -j 1271081``, ``ssh -p 2222``) or, for the commands that
# take them by position, by the operand's place (``nc -z a-host 8765``); a pid
# is a numeric operand of a process command; and a path is a token that
# resolves on the filesystem, the command token included when it is given as a
# path rather than as a bare name. The file-test commands are read for an
# operand as well, because ``test -f checkpoints/done`` and ``test -f DONE``
# are the same kind of wait and the derived probe of a ``wait_file`` condition
# is exactly this shape: there the operand is the path being waited for, so it
# need not exist yet.
_WAIT_FILE_TEST_COMMANDS = frozenset({"test", "["})
_WAIT_PROBE_JOB_FLAGS = frozenset({"-j", "--job", "--jobid", "--job-id"})
_WAIT_PROBE_PID_FLAGS = frozenset({"--pid"})
_WAIT_PROBE_PORT_FLAGS = frozenset({"--port", "--local-port"})
# ``-p`` names a pid or a port depending on the command it belongs to.
_WAIT_PROBE_SHORT_FLAG = "-p"
_WAIT_PROBE_PROCESS_COMMANDS = frozenset({"kill", "ps", "pgrep", "pkill"})
_WAIT_PROBE_SHELL_COMMANDS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
_WAIT_PROBE_PORT_COMMANDS = frozenset(
    {"nc", "netcat", "ncat", "ss", "netstat", "curl", "lsof", "telnet", "ssh"}
)
_WAIT_PROBE_COUNT = re.compile(r"[0-9]+")
_WAIT_PROBE_PORT_RANGE = (1, 65535)
# A shell-free vector gives a printer no way to read anything: ``echo`` and
# ``printf`` write their own arguments whatever is happening outside the
# worker, so a probe spelled with one is a command that cannot fail even when
# its text mentions a path. A probe that needs a shell to reach the scheduler
# keeps its ``bash -lc`` head, which is not a printer and is read by the
# reference rule instead.
_WAIT_PROBE_PRINTER_COMMANDS = frozenset({"echo", "printf"})


def _wait_probe_reference_kind(probe: Sequence[str]) -> str | None:
    """The kind of external reference the probe names, or None.

    The kinds a wait rests on are a job id, a pid, a port and a path, and one
    is named here only when the vector places a token as that kind: a value
    after a flag that names it (``-j 1271081``, ``-p 2222``), a numeric operand
    of a command that takes one by position (``nc -z a-host 8765``,
    ``kill -0 424242``), an operand of a file-test command, or a token that
    resolves on the filesystem. Anything else names nothing a sweep can read,
    however many digits, slashes, dollars or backticks its text carries:
    ``git rev-parse HEAD2`` resolves to no kind, and that is the difference
    between a reference and a character.

    Two shapes are read through rather than taken at face value. A shell
    keeps its string argument in one token, so a probe spelled
    ``bash -lc 'q=$(squeue -h -j 1274028); …'`` reaches the job flag only
    after the string is read as the shell vector it is. And the path kind is
    resolved against the filesystem, so a token counts only while it exists —
    except the operand of a file-test command, which is the path being waited
    *for*: ``test -f DONE`` is satisfied by DONE appearing, so that operand
    need not exist yet.
    """
    command = Path(str(probe[0])).name
    words = [str(item) for item in probe[1:]]
    if command in _WAIT_PROBE_SHELL_COMMANDS:
        words = [word for item in words for word in _wait_probe_shell_words(item)]
    takes_pids = command in _WAIT_PROBE_PROCESS_COMMANDS
    takes_ports = command in _WAIT_PROBE_PORT_COMMANDS
    tokens = [command, *words]
    for index, token in enumerate(tokens):
        name, separator, inline = token.partition("=")
        value = (
            inline
            if separator
            else (tokens[index + 1] if index + 1 < len(tokens) else "")
        )
        if name in _WAIT_PROBE_JOB_FLAGS and _wait_probe_names_a_count(value):
            return "job"
        if name in _WAIT_PROBE_PID_FLAGS and _wait_probe_names_a_count(value):
            return "pid"
        if name in _WAIT_PROBE_PORT_FLAGS and _wait_probe_names_a_port(value):
            return "port"
        if name == _WAIT_PROBE_SHORT_FLAG:
            if takes_pids and _wait_probe_names_a_count(value):
                return "pid"
            if takes_ports and _wait_probe_names_a_port(value):
                return "port"
        if name in _WAIT_FILE_TEST_COMMANDS and any(
            _wait_probe_names_an_operand(item) for item in tokens[index + 1 :]
        ):
            return "path"
        if token.isdigit():
            if takes_pids:
                return "pid"
            if takes_ports and _wait_probe_names_a_port(token):
                return "port"
        if Path(token).exists():
            return "path"
    return None


def _wait_probe_shell_words(text: str) -> list[str]:
    """The words a shell argument is spelled with, stripped of its punctuation.

    A shell reaches its reference through a string rather than through a token
    of the vector, so the string is read as the words it is: separators and
    control punctuation split it, and quoting and expansion characters are
    dropped from each word's edges. A job id written ``1274028);`` therefore
    reads as the number it is, and the flag before it still names the kind.
    """
    words: list[str] = []
    for word in re.split(r"[\s;|&()]+", text):
        cleaned = word.strip("'\"`${}<>")
        if cleaned:
            words.append(cleaned)
    return words


def _wait_probe_names_an_operand(item: str) -> bool:
    """True when a token is an operand rather than a flag."""
    return bool(item.strip()) and not item.startswith("-")


def _wait_probe_names_a_count(value: str) -> bool:
    """True when a value is a plain number, as job ids and pids are written."""
    return bool(_WAIT_PROBE_COUNT.fullmatch(value.strip()))


def _wait_probe_names_a_port(value: str) -> bool:
    """True when a value is a number a port could be."""
    if not _wait_probe_names_a_count(value):
        return False
    low, high = _WAIT_PROBE_PORT_RANGE
    return low <= int(value.strip()) <= high


def _wait_probe_cannot_fail(probe: Sequence[str]) -> bool:
    """True when a present probe's result cannot differ between sweeps.

    The discriminator is whether anything the probe reads lives outside the
    worker: a job id, a pid, a port or a path. A printer is refused outright
    because in a shell-free vector it can only echo its own text, and any
    other probe must resolve one of those references through
    :func:`_wait_probe_reference_kind`: ``["squeue", "-h", "-j", "1271081"]``
    resolves a job id; ``["echo", "pending"]`` and ``["git", "rev-parse",
    "HEAD2"]`` resolve nothing, run whatever is happening, and read the same on
    every sweep. Only a probe that is actually present is read this way: an
    absent one is the incomplete-declaration case the reader already reports,
    so the two outcomes stay distinguishable.
    """
    if not probe:
        return False
    command = Path(str(probe[0])).name
    if command in _WAIT_PROBE_NO_OP_COMMANDS:
        return True
    if command in _WAIT_PROBE_PRINTER_COMMANDS:
        return True
    return _wait_probe_reference_kind(probe) is None


def _wait_probe_is_a_no_op(probe: Sequence[str]) -> bool:
    """True when a present probe can report nothing but success.

    The null command prints nothing at all, which makes it the extreme member
    of the probes that cannot fail; the wider family is read by
    :func:`_wait_probe_cannot_fail`, and this reading keeps its own meaning so
    a declaration resting on ``["true"]`` still reduces to no declaration at
    all.
    """
    if not probe:
        return False
    return Path(str(probe[0])).name in _WAIT_PROBE_NO_OP_COMMANDS


# A state that means the awaited work has not finished is never a terminal
# state. A job scheduler spells three of them, and a probe may invent its own
# wording for the same situation on a branch it takes only while the job is
# still in the queue — which is how a wait declaring RUNNING terminal reads as
# satisfied on every sweep of a job that has not started. The fixed spellings
# are refused outright; the probe's own live branch is read out of its text,
# because a renamed live state is exactly what the fixed spellings cannot see.
_WAIT_LIVE_STATE_TOKENS = frozenset({"running", "pending", "waiting"})

# The variable a probe fills from `squeue`, whose non-empty test guards the
# branch it takes while the job is still in the queue.
_SQUEUE_GUARDED_VAR = re.compile(
    r"(?P<var>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*\$\(\s*squeue\b"
)
_WAIT_BRANCH_STOP = re.compile(r"\b(?:elif|else|fi)\b")


def _emitted_tokens(branch: str) -> list[str]:
    """Bare word tokens a shell branch prints, one statement at a time."""
    tokens: list[str] = []
    for statement in re.split(r"[;\n]|&&|\|\||\b(?:then|do)\b", branch):
        words = [word.strip("\"'") for word in statement.split()]
        if not words or words[0] not in {"echo", "printf"}:
            continue
        tokens.extend(word for word in words[1:] if word and not word.startswith("-"))
    return tokens


def _probe_live_state_tokens(probe: Sequence[str]) -> list[str]:
    """Tokens a probe prints from a branch guarded by a non-empty squeue result.

    Those tokens describe a job that is still in the queue whatever the wait
    declaration calls the state, so any of them listed as terminal is the
    declaration contradicting its own probe.
    """
    text = " ".join(str(item) for item in probe)
    tokens: list[str] = []
    for assignment in _SQUEUE_GUARDED_VAR.finditer(text):
        var = re.escape(assignment.group("var"))
        guard = re.search(
            r"\[\s*-n\s+\"?(?:\$\{?" + var + r"\}?|\$\{\s*" + var + r"\s*\})\"?\s*\]"
            r"|\[\s*\"?\$\{?" + var + r"\}?\"?\s*\]",
            text,
        )
        if guard is None:
            continue
        branch = text[guard.end() :]
        stop = _WAIT_BRANCH_STOP.search(branch)
        if stop:
            branch = branch[: stop.start()]
        tokens.extend(_emitted_tokens(branch))
    return tokens


def _wait_terminal_names_a_live_state(
    terminal: Sequence[str], probe: Sequence[str]
) -> str:
    """Name the terminal value the probe reports while the awaited job is live.

    Empty when nothing in the terminal list names a live state, which is the
    only case a wait declaration is read at all.
    """
    if not terminal or not probe or _wait_probe_is_a_no_op(probe):
        return ""
    emitted = _probe_live_state_tokens(probe)
    for value in terminal:
        spelled = str(value).strip()
        if spelled.casefold() in _WAIT_LIVE_STATE_TOKENS:
            return spelled
        if spelled and spelled in emitted:
            return spelled
    return ""


def _wait_declaration_signature(
    condition: str,
    probe: Sequence[str],
    terminal: Sequence[str],
    resume_brief: str,
) -> str:
    """Identity of a wait declaration, without its file's modification time.

    A lift is keyed to what the declaration asks for, not to when the file was
    written. A worker that re-parks rewrites its manifest and so advances the
    mtime, which made every re-park a brand-new condition and re-lifted a wait
    whose terminal state had not actually ended anything — the loop that
    resumed one run thirty times. The same declaration arriving twice is the
    same condition; only an edit to it is a new one.
    """
    material = json.dumps(
        {
            "condition": condition,
            "probe": [str(item) for item in probe],
            "terminal": [str(item) for item in terminal],
            "resume_brief": resume_brief,
        },
        sort_keys=True,
    )
    return "wait:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


# The shapes a run's own output takes inside one run directory: the initial
# stream, one per resume turn, and one per lane change. A run that resumes or
# changes lane keeps writing to a new file, so the newest of these is its
# current activity; the file the pointer first named goes stale the moment that
# happens.
RUN_STREAM_GLOBS = ("stream.jsonl", "resume-*.jsonl", "lane-change-*.jsonl")


def stream_paths_newest_first(
    run_dir: str | Path, *, include: Iterable[str | Path] = ()
) -> list[Path]:
    """Every non-empty stream in a run directory, newest write first.

    Empty files are skipped rather than counted: a stream a process has opened
    but written nothing to is not activity, and reading its mtime would report
    a resumed run as producing output the instant its file was created. The
    pointer's own log path joins the candidates when a caller supplies it, so a
    record whose current stream sits outside the run directory is still read.
    """
    directory = Path(run_dir)
    candidates: list[Path] = []
    for pattern in RUN_STREAM_GLOBS:
        candidates.extend(directory.glob(pattern))
    for extra in include:
        if str(extra or ""):
            candidates.append(Path(str(extra)))
    written: dict[Path, float] = {}
    for path in candidates:
        try:
            if path.stat().st_size > 0:
                written[path] = path.stat().st_mtime
        except OSError:
            # A directory entry can vanish between the glob and the stat; that
            # is no stream rather than an error.
            continue
    return sorted(written, key=lambda path: (written[path], str(path)), reverse=True)


def newest_stream(
    run_dir: str | Path, *, include: Iterable[str | Path] = ()
) -> tuple[Path, float] | None:
    """The newest non-empty stream a run has, and when it was written.

    None means the run holds no readable, non-empty stream, so a caller can
    tell "no measurement taken" from "an infinitely old one". This is the one
    reader for both questions a stream answers — how long the run has been
    quiet, and which session it is continuing — so the stall classifier and the
    session lookup cannot disagree about which stream is current.
    """
    paths = stream_paths_newest_first(run_dir, include=include)
    if not paths:
        return None
    newest = paths[0]
    try:
        return newest, newest.stat().st_mtime
    except OSError:
        return None


def _request_input_tokens(event: Mapping[str, Any]) -> int | None:
    """The charged input one stream record reports for a single request.

    Only per-request records answer: an ``assistant`` record's own
    ``message.usage`` on the claude grammar, and the ``turn.completed`` usage
    on the codex grammar, which is that grammar's only usage record. A claude
    ``result`` record is deliberately not consulted — its modelUsage is a
    run-length aggregate of un-cached input, which grows with the run's length
    rather than describing the context a resumed turn would re-send.
    """
    kind = event.get("type")
    if kind == "assistant":
        message = event.get("message")
        usage = message.get("usage") if isinstance(message, Mapping) else None
    elif kind == "turn.completed":
        usage = event.get("usage")
    else:
        return None
    charged = _charged_input_from_usage(usage)
    if isinstance(charged, bool) or not isinstance(charged, (int, float)):
        return None
    return int(charged)


def _last_recorded_input_tokens(run_id: str, record: Mapping[str, Any]) -> int | None:
    """The session's last recorded request input, from the run's own streams.

    Streams are read newest write first, and the first stream carrying a
    per-request figure answers: a resume attempt that died before reaching the
    model leaves a stream with no usage at all, and it must not blank the count
    an earlier attempt recorded. None means no stream carried a figure, which
    refuses nothing — an unmeasured session is not a session known to be too
    large.
    """
    include = [record.get("log_path")] if record.get("log_path") else []
    for path in stream_paths_newest_first(runs.run_dir(run_id), include=include):
        measured: int | None = None
        try:
            handle = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if not isinstance(event, Mapping):
                    continue
                measured_value = _request_input_tokens(event)
                if measured_value is not None:
                    measured = measured_value
        if measured is not None:
            return measured
    return None


def _lane_input_window(
    record: Mapping[str, Any],
    backend: Mapping[str, Any],
    config: Mapping[str, Any] | None,
) -> int | None:
    """The input window the run's lane publishes, or None when it publishes none.

    Two figures narrow the gate, and the smaller wins for the same reason the
    dispatch-time context-fit check takes the smaller: the declared
    ``usable_input_window`` is what the lane says its endpoint accepts — already
    net of the launcher's output reservation, so a count above it plus the
    reservation exceeds the engine cap — while an ``effective_input_window`` is
    the lowest input the lane's endpoint is recorded refusing. A lane declaring
    neither the run record nor the configuration answers None, and an unstated
    window refuses nothing.
    """
    backends = (config or {}).get("backends")
    name = str(record.get("backend") or "")
    configured = backends.get(name) if isinstance(backends, Mapping) else None
    figures: list[int] = []
    for source in (backend, configured):
        if not isinstance(source, Mapping):
            continue
        for key in ("usable_input_window", "effective_input_window"):
            value = source.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if int(value) > 0:
                figures.append(int(value))
    return min(figures) if figures else None


def resume_window_refusal(
    run_id: str,
    record: Mapping[str, Any],
    *,
    backend: Mapping[str, Any],
    config: Mapping[str, Any] | None = None,
) -> CrewError | None:
    """The refusal a resume owes a session the lane's window cannot hold.

    A resumed turn re-sends the session's whole context, so a session that has
    grown past the lane's input window dies at launch: the endpoint refuses the
    prompt after the attempt file is already open, and the opened attempt then
    makes the delivered manifest read stale to promotion. The count is the
    run's own last recorded request input; the window is the lane's published
    input window. The gate is consulted before anything is written, so a
    refused resume leaves no attempt behind and touches neither the run's
    classification nor its manifest, and the remedy is a fresh repair node,
    because the session itself cannot be continued on this lane.
    """
    window = _lane_input_window(record, backend, config)
    if window is None:
        return None
    count = _last_recorded_input_tokens(run_id, record)
    if count is None or count <= window:
        return None
    lane = str(record.get("backend") or "unknown")
    return CrewError(
        f"run {run_id!r} is not resumed: its session last carried {count} input "
        f"tokens, above backend {lane!r}'s published input window of {window} "
        "tokens, so the resumed turn would die at the endpoint's context limit "
        "and leave an open attempt behind. Dispatch a fresh repair node instead "
        "of resuming this session."
    )


# An engine writes one of these when a turn has run to its own conclusion, so
# the process ending after it is an end of turn rather than a death mid-turn.
# The two read differently on the pane because their remedies differ: a turn
# that ended is continued, while a death mid-turn needs its cause read first.
STREAM_RESULT_RECORD_TYPE = "result"

# The tail a last-record read takes from a stream. The answer is one line, and
# the watcher asks this per run per snapshot, so the whole file — megabytes on a
# long run — is never read for it. It is a floor rather than a limit: a final
# record taller than the window leaves the window inside that one record with no
# line boundary in it, and the read then reaches further back until one is in.
_STREAM_TAIL_BYTES = 64 * 1024


def _last_record_type_in(chunk: bytes) -> str | None:
    """The type of the last complete record in a chunk of a stream.

    Lines are read backwards, so the answer is the record the file ends on
    rather than the one it starts with. A line that does not parse is passed
    over: a trailing partial write is not evidence that a record completed, and
    a reader that took it for one would name an end the run never reached.
    """
    for raw in reversed(chunk.splitlines()):
        text = raw.strip()
        if not text:
            continue
        try:
            event = json.loads(text)
        except (TypeError, ValueError):
            continue
        if not isinstance(event, Mapping):
            continue
        record_type = event.get("type")
        if isinstance(record_type, str) and record_type:
            return record_type
    return None


def _newest_stream_last_record_type(record: Mapping[str, Any]) -> str | None:
    """The type of a run's newest stream's last complete record.

    None answers "no last record to read": no stream, one that cannot be read,
    or one whose tail holds no complete record. A trailing partial write is
    skipped rather than parsed — an engine appending a record is not evidence
    that the record completed — and a caller therefore never reads "could not
    tell" as a particular type.

    The window is a floor because a record can be taller than it. A chunk that
    begins inside a record holds only a fragment of the file's last record when
    that record is taller than the window, and a fragment parses as nothing, so
    the read reaches further back until the chunk begins where a record begins
    and the record ending the file is read whole. One window answers the common
    case — a last record of a few hundred bytes behind any length of stream —
    because such a chunk holds that record entire and the read stops there.
    """
    found = _record_newest_stream(record)
    if found is None:
        return None
    try:
        with found[0].open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            window = _STREAM_TAIL_BYTES
            while True:
                start = max(0, size - window)
                handle.seek(start)
                chunk = handle.read()
                record_type = _last_record_type_in(chunk)
                if record_type is not None or start == 0:
                    return record_type
                # Nothing in the chunk read as a record. A chunk beginning at a
                # record boundary holds only complete lines, so the stream has
                # no record to read here and reaching further back would answer
                # the same; a chunk beginning inside one is the tail of a
                # record larger than the window, which the next read must
                # contain.
                handle.seek(start - 1)
                if handle.read(1) == b"\n":
                    return None
                window *= 4
    except OSError:
        return None


# The record an engine writes when the model answers a turn. It is the run's
# own first evidence that work is happening rather than that a launch was
# started: the pointer's phase is a label only ``observe`` advances, while this
# record lands the moment the worker's first turn is under way.
STREAM_ASSISTANT_RECORD_TYPE = "assistant"


def _stream_holds_assistant_record(path: Path) -> bool:
    """Whether a stream holds at least one complete assistant record.

    Read forwards and stopped at the first match, because a worker's first
    assistant turn lands within its first few records: the scan answers after a
    few kilobytes on a stream that grows to megabytes over a long run. A line
    that does not parse is passed over, exactly as the tail read passes over
    one, so a trailing partial write is never read as a record that completed.
    """
    try:
        with path.open("rb") as handle:
            for raw in handle:
                text = raw.strip()
                if not text:
                    continue
                try:
                    event = json.loads(text)
                except (TypeError, ValueError):
                    continue
                if (
                    isinstance(event, Mapping)
                    and event.get("type") == STREAM_ASSISTANT_RECORD_TYPE
                ):
                    return True
    except OSError:
        return False
    return False


def _newest_stream_shows_work(record: Mapping[str, Any]) -> bool:
    """Whether a run's newest stream carries an assistant record.

    The newest stream is the shared reader's answer, so this agrees with the
    stall clock about which file is the run's current one; a run that resumed
    or changed lane wrote a newer file than the one it started with. False
    answers "no such record to read" — no stream, one that cannot be read, or
    one holding no assistant turn — so a caller never reads an absent answer
    as work.
    """
    found = _record_newest_stream(record)
    if found is None:
        return False
    return _stream_holds_assistant_record(found[0])


def _process_exit_reason(record: Mapping[str, Any], last_record_type: str) -> str:
    """Why a dead worker's row says the process exited, in its records' terms.

    The run directory's exit record is the supervisor's account of the end and
    outranks the bare pid, so a kill is named by its signal and a clean end by
    its code; a run with no recorded exit says so rather than inventing one.
    The last record type travels beside it, because a stream that carried no
    result record is what makes the end a death mid-turn rather than a turn
    that finished.
    """
    exit_record = _run_exit_record(record)
    if exit_record is not None:
        end = (
            f"the worker process {_exit_record_end_phrase(exit_record)} "
            f"(recorded at {exit_record.get('exited_at') or 'an unrecorded moment'})"
        )
    else:
        end = "the worker process is gone with no recorded exit"
    return (
        f"{end} before the run completed; the stream's last record is "
        f"{last_record_type}, so the turn did not end"
    )


def _run_directory(record: Mapping[str, Any] | dict[str, Any]) -> Path:
    """The directory holding a run's streams, from its id or its log path."""
    run_id = str(record.get("run_id") or "")
    if run_id:
        return Path(runs.run_dir(run_id))
    return Path(str(record.get("log_path") or ".")).parent


from .recovery_liveness import (  # noqa: E402
    _exit_record_end_phrase,
    _record_newest_stream,
    _run_exit_record,
)
from .recovery_memo import (  # noqa: E402
    _memo_for,
)
from .recovery_stream import (  # noqa: E402
    _BACKGROUND_WAIT_CEILING_RE,
    _BACKGROUND_WAIT_FINAL_MESSAGE_RE,
    _budget_hold_block,
    _observed_stream,
    _stream_budget,
    _stream_retry_block,
)
from .recovery_vocabulary import (  # noqa: E402
    WAIT_CONDITION_STATES,
    WAIT_PROBE_TIMEOUT_SECONDS,
)
