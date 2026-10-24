"""The crew MCP tool's coordinator write actions.

The eight coordinator verbs a session reaches for most often — ``complete``,
``land``, ``dispose``, ``ack``, ``answer``, ``widen``, ``stop`` and ``discard``
— run here through the same command functions the crew CLI verbs run through.
Each action calls its verb's own implementation function in-process, with the
caller's arguments mapped onto that function's parameters, and returns the one
JSON document the CLI verb emits for the same call. There is no shell, no
argument quoting and no external timeout, which is the whole point: a
coordinator reaching for one of these never has to leave the tool surface, and
never risks a call that has written being killed before it answers.

The JSON parity is by construction rather than by a parallel implementation:
:func:`_call_verb` invokes the CLI verb's callback and captures the document it
emits through the command module's own emission helper, so the tool and the CLI
cannot drift about what one call returns. The argument mapping
(:data:`_PARAM_MAP`) is the one place the two surfaces can disagree, and it is
also the seam the parity test's negative control moves to prove the test bites.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Mapping
from typing import Any

import click

#: The coordinator write actions the crew tool accepts.
CREW_WRITE_ACTIONS: tuple[str, ...] = (
    "complete",
    "land",
    "dispose",
    "ack",
    "answer",
    "widen",
    "stop",
    "discard",
    "queue-edit",
    "queue-reprioritise",
    "queue-drop",
)

#: The parameters each action forwards to its verb's implementation, keyed by
#: the tool-side argument name. An action's value is the ordered mapping from
#: the tool's parameter name to the CLI verb's own parameter name. The seam the
#: negative control moves: change one entry and that action's parity case fails
#: and no other, because no other action reads this map.
_PARAM_MAP: dict[str, dict[str, str]] = {
    "complete": {
        "run_id": "run_id",
        "gate": "gate",
        "failure_classification": "failure_classification",
        "commits": "commits",
        "outcome": "outcome",
        "no_commit": "no_commit",
        "tests_added": "tests_added",
        "scope_changed": "scope_changed",
        "completed_at": "completed_at",
        "checkout_path": "checkout_path",
        "gate_command": "gate_command",
        "gate_exit_status": "gate_exit_status",
        "gate_log_path": "gate_log_path",
        "gate_log_digest": "gate_log_digest",
        "waive_suite_delta": "waive_suite_delta",
        "waive_boundary_refusal": "waive_boundary_refusal",
        "waive_resume_path": "waive_resume_path",
        "waive_unreviewed_promotion": "waive_unreviewed_promotion",
        "waive_negative_control": "waive_negative_control",
        "accepted_paths": "accepted_paths",
        "no_impl_change": "no_impl_change",
        "waive_live_run": "waive_live_run",
        "waive_domain_check": "waive_domain_check",
        "plan_link": "plan_link",
        "unplanned_reason": "unplanned_reason",
        "dry_run": "dry_run",
    },
    "land": {
        "run_id": "run_id",
        "gate": "gate",
        "failure_classification": "failure_classification",
        "commits": "commits",
        "outcome": "outcome",
        "no_commit": "no_commit",
        "tests_added": "tests_added",
        "scope_changed": "scope_changed",
        "completed_at": "completed_at",
        "checkout_path": "checkout_path",
        "gate_command": "gate_command",
        "gate_exit_status": "gate_exit_status",
        "gate_log_path": "gate_log_path",
        "gate_log_digest": "gate_log_digest",
        "waive_suite_delta": "waive_suite_delta",
        "waive_boundary_refusal": "waive_boundary_refusal",
        "waive_resume_path": "waive_resume_path",
        "waive_unreviewed_promotion": "waive_unreviewed_promotion",
        "waive_negative_control": "waive_negative_control",
        "accepted_paths": "accepted_paths",
        "no_impl_change": "no_impl_change",
        "waive_live_run": "waive_live_run",
        "waive_domain_check": "waive_domain_check",
        "plan_link": "plan_link",
        "unplanned_reason": "unplanned_reason",
        "dry_run": "dry_run",
    },
    "dispose": {
        "project": "project",
        "run_id": "reviewed_run_id",
        "dimension": "dimension",
        "finding_index": "finding_index",
        "kind": "kind",
        "node": "node_id",
        "reason": "reason",
    },
    "ack": {
        "run_id": "run_id",
        "notice_id": "notice_id",
        "reason": "reason",
        "until": "until",
    },
    "answer": {
        "project": "project",
        "plan": "plan_slug",
        "finding": "answer",
        "acted": "acted",
        "declined": "declined",
        "session": "session",
    },
    "widen": {
        "run_id": "run_id",
        "write_paths": "write_paths",
    },
    "stop": {
        "run_id": "run_id",
    },
    "discard": {
        "run_id": "run_id",
    },
    "queue-edit": {
        "run_id": "run_id",
        "brief_path": "brief_path",
        "done_when": "done_when",
        "write_paths": "write_paths",
        "time_budget": "time_budget",
        "backend": "backend",
        "model": "model",
        "project": "project",
    },
    "queue-reprioritise": {
        "run_id": "run_id",
        "priority": "priority",
        "reason": "reason",
        "project": "project",
    },
    "queue-drop": {
        "run_id": "run_id",
        "reason": "reason",
        "project": "project",
    },
}

#: The defaults each action's verb supplies for a parameter the tool omits.
#: Kept beside the map so a caller sees the whole tool-to-verb contract in one
#: place.
_PARAM_DEFAULTS: dict[str, dict[str, Any]] = {
    "complete": {
        "gate": "",
        "failure_classification": None,
        "commits": (),
        "outcome": "",
        "no_commit": "",
        "tests_added": None,
        "scope_changed": False,
        "completed_at": "",
        "checkout_path": None,
        "gate_command": "",
        "gate_exit_status": None,
        "gate_log_path": "",
        "gate_log_digest": "",
        "waive_suite_delta": "",
        "waive_boundary_refusal": "",
        "waive_resume_path": "",
        "waive_unreviewed_promotion": "",
        "waive_negative_control": None,
        "accepted_paths": (),
        "no_impl_change": "",
        "waive_live_run": "",
        "waive_domain_check": "",
        "plan_link": "",
        "unplanned_reason": "",
        "dry_run": False,
    },
    "dispose": {
        "dimension": None,
        "finding_index": None,
        "kind": "",
        "node": "",
        "reason": "",
    },
    "ack": {"run_id": None, "notice_id": None, "reason": "", "until": None},
    "answer": {
        "plan": "",
        "finding": "",
        "acted": False,
        "declined": None,
        "session": "",
        "around_plan": "",
        "paths": (),
        "sprint_id": "",
        "portfolio": False,
        "rubric": "design",
        "local": False,
        "dry_run": False,
    },
    "widen": {"write_paths": ()},
    "stop": {},
    "discard": {},
    "queue-edit": {
        "brief_path": "",
        "done_when": None,
        "write_paths": (),
        "time_budget": None,
        "backend": "",
        "model": "",
        "project": None,
    },
    "queue-reprioritise": {"reason": "", "project": None},
    "queue-drop": {"reason": "", "project": None},
}

#: ``land`` runs the promotion verb with the landing step on, so it takes the
#: same arguments ``complete`` takes.
_PARAM_MAP["land"] = _PARAM_MAP["complete"]
_PARAM_DEFAULTS["land"] = _PARAM_DEFAULTS["complete"]


def _verb_callback(module: str, attribute: str) -> Callable[..., Any]:
    """Return a CLI verb's own callback, the function its verb runs.

    The command modules decorate their callbacks into click commands; the
    callback held on the command object is the implementation the verb calls,
    so calling it directly reaches the same code the CLI reaches without a
    shell or an argument-vector round trip.
    """
    import importlib

    command = getattr(importlib.import_module(module), attribute)
    callback = getattr(command, "callback", None)
    return callback if callback is not None else command


def _verb_for(action: str) -> tuple[Callable[..., Any], dict[str, Any]]:
    """The callback and fixed arguments one action runs.

    ``land`` is ``complete`` with the landing step turned on: its verb is the
    promotion verb, and this is where ``land`` is set so the action name alone
    selects the sequence the CLI selects with ``--land``.
    """
    if action in ("complete", "land"):
        callback = _verb_callback("reckon.crew_run_commands", "crew_complete")
        fixed = {"land": action == "land"}
        return callback, fixed
    if action == "dispose":
        return _verb_callback("reckon.crew_run_commands", "crew_dispose"), {}
    if action == "ack":
        return _verb_callback("reckon.crew_run_commands", "crew_ack"), {}
    if action == "widen":
        return _verb_callback("reckon.crew_run_commands", "crew_widen"), {}
    if action == "stop":
        return _verb_callback("reckon.crew_run_commands", "crew_stop"), {}
    if action == "discard":
        return _verb_callback("reckon.crew_run_commands", "crew_discard"), {}
    if action == "queue-edit":
        return _verb_callback("reckon.crew_run_commands", "crew_queue_edit"), {}
    if action == "queue-reprioritise":
        return _verb_callback("reckon.crew_run_commands", "crew_queue_reprioritise"), {}
    if action == "queue-drop":
        return _verb_callback("reckon.crew_run_commands", "crew_queue_drop"), {}
    if action == "answer":
        return _verb_callback("reckon.crew_dispatch_commands", "crew_review"), {}
    raise KeyError(action)


def _call_verb(
    callback: Callable[..., Any], arguments: Mapping[str, Any]
) -> dict[str, Any]:
    """Run one verb's callback and return the JSON document it emits.

    The command modules emit through ``_emit``; both the run-command module's
    binding and the dispatch module's binding are held aside for the call, so
    whichever the verb reaches, its one document is captured here rather than
    printed. A verb that writes nothing before refusing — the promotion verbs
    raise before any emission — leaves no captured document, and its
    ``ClickException`` message becomes the refusal the caller reads.
    """
    from reckon import crew_dispatch_commands, crew_run_commands

    captured: dict[str, Any] = {}

    def _capture(payload: Mapping[str, Any], pretty: bool = False) -> None:
        captured["payload"] = dict(payload)

    with contextlib.ExitStack() as stack:
        stack.enter_context(_patched(crew_run_commands, "_emit", _capture))
        stack.enter_context(_patched(crew_dispatch_commands, "_emit", _capture))
        try:
            callback(**dict(arguments))
        except click.exceptions.Exit:
            pass
        except click.ClickException as exc:
            captured.setdefault(
                "payload",
                {"ok": False, "error": "crew_error", "detail": exc.message},
            )
        except click.Abort:
            captured.setdefault(
                "payload",
                {"ok": False, "error": "crew_error", "detail": "aborted"},
            )
        except Exception as exc:  # noqa: BLE001 - a raised failure is the call's answer
            captured.setdefault(
                "payload",
                {
                    "ok": False,
                    "error": "crew_error",
                    "detail": f"{type(exc).__name__}: {exc}",
                },
            )
    return captured.get("payload") or {}


@contextlib.contextmanager
def _patched(module: Any, attribute: str, replacement: Any):
    """Hold one module attribute replaced for the duration of a block."""
    original = getattr(module, attribute)
    setattr(module, attribute, replacement)
    try:
        yield
    finally:
        setattr(module, attribute, original)


def _mapped_arguments(action: str, kwargs: Mapping[str, Any]) -> dict[str, Any]:
    """Map the tool's arguments onto the verb callback's own parameter names.

    Only parameters the mapping names are forwarded, so an argument one action
    does not take can never reach its verb; a parameter the caller omits keeps
    the verb's own default, so the tool and the CLI supply the same value for
    the same call.
    """
    mapping = _PARAM_MAP[action]
    defaults = _PARAM_DEFAULTS.get(action, {})
    forwarded: dict[str, Any] = {}
    for tool_name in (*mapping, *(key for key in defaults if key not in mapping)):
        if tool_name in kwargs and kwargs[tool_name] is not None:
            forwarded[mapping.get(tool_name, tool_name)] = kwargs[tool_name]
        elif tool_name in defaults:
            forwarded[mapping.get(tool_name, tool_name)] = defaults[tool_name]
    return forwarded


def crew_action(action: str, **kwargs: Any) -> dict[str, Any]:
    """Run one coordinator write action through its CLI verb's implementation.

    ``action`` is one of :data:`CREW_WRITE_ACTIONS`; ``kwargs`` are the tool's
    own parameters. Returns the verb's emitted JSON document, so the tool and
    the CLI answer one call identically.
    """
    if action not in CREW_WRITE_ACTIONS:
        return {
            "ok": False,
            "error": "invalid_action",
            "detail": (f"action must be one of {', '.join(CREW_WRITE_ACTIONS)}"),
        }
    callback, fixed = _verb_for(action)
    arguments = _mapped_arguments(action, kwargs)
    arguments.update(fixed)
    # The tool always answers with the compact document a caller parses; the
    # CLI's indentation is a reading convenience, not part of the answer.
    arguments["pretty"] = False
    return _call_verb(callback, arguments)
