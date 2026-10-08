import contextlib
import functools
import json
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import click

from reckon._store import write_json_atomically
from reckon.cli_entry import main


@main.group(name="crew")
def crew():
    """Dispatch and observe workers through one backend-agnostic call.

    These are agent-callable primitives rather than a human interface: output is
    JSON on stdout, each call is atomic, nothing is interactive, and exit codes
    are branchable — 0 succeeded, 1 the configuration or request is wrong, 2 the
    node is not dispatchable and names which property it failed, 3 the wave is
    held on budget and names the backend, the utilisation and when it resets, 4
    the named plan section is unavailable at the worktree base ref, 5 the
    selected worker configuration has a typed competence refusal, 6 terminal
    pointers need reconciliation, 7 a live run holds a conflicting write scope,
    8 no live project watcher is waiting for the dispatched work.
    """



@crew.command(name="gate")
@click.option("--pause", default=None, metavar="REASON")
@click.option("--open", "open_gate", is_flag=True)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_gate(pause, open_gate, pretty):
    """Pause or open shared crew gate state across every backend."""
    from reckon.crew.dispatch import _dispatch_fleet_gate, fleet_gate_path

    if (pause is None and not open_gate) or (pause is not None and open_gate):
        raise click.ClickException("name exactly one of --pause REASON or --open")
    if pause is not None and not pause.strip():
        raise click.ClickException("--pause requires a reason")
    before = _dispatch_fleet_gate()
    if before["state"] == "unreadable":
        raise click.ClickException(before["detail"])
    path = fleet_gate_path()
    document = {"paused": not open_gate, "reason": pause.strip() if pause else None}
    write_json_atomically(path, document)
    after = _dispatch_fleet_gate()
    if after["state"] == "unreadable":
        raise click.ClickException(after["detail"])
    _emit_crew_result(
        {
            "gate_path": str(path),
            "before": {"paused": before["paused"], "reason": before["reason"]},
            "after": {"paused": after["paused"], "reason": after["reason"]},
        },
        pretty,
    )



def _crew_modules():
    """Import the crew and flight helpers on demand."""
    from reckon import crew as crew_module
    from reckon import flight as flight_module

    return crew_module, flight_module



class _OneDocumentStdout:
    """stdout during a JSON command: the payload stream, or the notice channel.

    A caller decodes a command's stdout, so anything written there that is not
    the command's own document breaks the read — a second document or a bare
    sentence both make ``json.loads`` raise, and the caller is left with
    nothing. This stream forwards every ordinary write to stderr and keeps the
    real stdout aside for the one document the command emits through
    :func:`_emit`, so a notice printed anywhere beneath — a helper's warning, a
    library's status line, code this module does not own — cannot corrupt it.
    """

    def __init__(self, payload_stream: Any) -> None:
        self.payload_stream = payload_stream

    def write(self, text: str) -> int:
        return sys.stderr.write(text)

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return False

    def flush(self) -> None:
        sys.stderr.flush()

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        # A writer that reaches for the descriptor still lands on the notice
        # channel rather than on the stream the caller is parsing.
        return sys.stderr.fileno()



@contextlib.contextmanager
def _single_document_stdout() -> Iterator[None]:
    """Hold stdout for one JSON document while a command runs.

    Everything the command calls runs with stdout pointing at the notice
    channel, so notices reach stderr wherever they are printed; the emission
    helper writes to the stream held aside here.
    """
    channel = _OneDocumentStdout(sys.stdout)
    with contextlib.redirect_stdout(channel):
        yield



def _holds_stdout_for_one_document(callback):
    """Run a JSON command under :func:`_single_document_stdout`.

    Applied to the callback itself rather than to a block of its body, so the
    guarantee holds for every outcome and for code the command calls that this
    module cannot edit.
    """

    @functools.wraps(callback)
    def run(*args, **kwargs):
        with _single_document_stdout():
            return callback(*args, **kwargs)

    return run



def _emit(payload, pretty: bool) -> None:
    """Print one JSON document, sorted so two runs diff only on real change."""
    stream = getattr(sys.stdout, "payload_stream", sys.stdout)
    click.echo(
        json.dumps(payload, indent=2 if pretty else None, sort_keys=True),
        file=stream,
    )



def _crew_result_ok(result: Mapping[str, Any], *, observation: bool = False) -> bool:
    """Refusals and missing measures fail; deliberate skips remain successful."""
    if "ok" in result:
        return result["ok"]
    if not result:
        return False
    if observation:
        return True
    if any(
        result.get(key)
        for key in (
            "error",
            "refusal",
            "refused",
            "unusable",
            "finding",
            "timed_out",
            "over_budget",
            "reviews_refused",
        )
    ):
        return False
    if "exit_status" in result and result["exit_status"] != 0:
        return False
    if any(result.get(key) is False for key in ("ran", "measured", "completed")):
        return False
    if result.get("held") and "held_backends" in result:
        return False
    if result.get("action") in {"hold", "refuse", "unmeasured"}:
        return False
    validation = result.get("validation")
    if isinstance(validation, Mapping) and validation.get("ok") is False:
        return False
    reviews = result.get("reviews")
    return not (
        isinstance(reviews, Mapping)
        and (reviews.get("error") or reviews.get("refused"))
    )



def _emit_crew_result(
    result: Mapping[str, Any],
    pretty: bool,
    *,
    failure_exit: int = 1,
    observation: bool = False,
) -> None:
    """Publish one crew verdict and give a negative verdict a matching exit."""
    payload = dict(result)
    payload["ok"] = _crew_result_ok(payload, observation=observation)
    _emit(payload, pretty)
    if not payload["ok"]:
        raise click.exceptions.Exit(failure_exit)



def _emit_dry_run_request_error(
    pretty: bool, detail: str, resolution: dict | None = None
) -> None:
    """Answer a request error on the dry run's JSON channel.

    A request error is the same fact whether it came from a leaf key the
    flight schema refuses or from a keyed-map name no layer defines, so both
    answer in the same decodable document rather than one in plain text on
    stderr. A caller holding the --set resolution passes it, so a request
    error also tells an override that resolved from one that never applied.
    """
    _emit(
        _with_resolved_overrides(
            {"ok": False, "dry_run": True, "error": "request-error", "detail": detail},
            resolution or {},
        ),
        pretty,
    )



def _validation_detail(validation) -> str:
    """Render a node validation's findings as the detail of a refusal.

    Every refusal answers with error and detail, so the contract-validation
    refusal must name what failed in a sentence rather than only under the
    structured ``validation`` key — a caller that keys on ``error`` and stops
    there would otherwise read a failed contract as success.
    """
    findings = [
        f"{finding.get('property')}: {finding.get('detail')}"
        for finding in validation.findings
    ]
    return "; ".join(findings) or "the node failed contract validation"



def _lane_paused_detail(gate) -> str:
    """Render the sentence a lane-paused result carries as its detail.

    The defect or the gate's own reason, and a placeholder only when neither is
    present: a paused gate with no reason is valid, and the result still has to
    say what held the dispatch rather than carry an empty detail.
    """
    detail = str(gate.get("detail") or "").strip()
    if detail:
        return detail
    reason = str(gate.get("reason") or "").strip()
    if reason:
        return reason
    return f"the lane gate is {gate.get('state')!r}"



def _resolved_flight(flight_module, project, checkout_path, overrides):
    """Resolve flight config for a dispatch, prompt overrides winning."""
    try:
        return flight_module.resolve(
            project,
            overrides=flight_module.parse_overrides(overrides) if overrides else None,
            checkout_path=checkout_path,
        ).config
    except flight_module.FlightConfigError as exc:
        raise click.ClickException(str(exc)) from exc



def _dispatch_resolved_flight(flight_module, project, checkout_path, overrides):
    """Resolve dispatch flight data with its operator-facing recovery command."""
    from reckon.crew.refusals import format_refusal

    try:
        return _resolved_flight(flight_module, project, checkout_path, overrides)
    except click.ClickException as exc:
        raise click.ClickException(format_refusal("D06", str(exc))) from exc



def _flight_default_backend_override(flight_module, config, overrides):
    """Return the resolved default only when the prompt layer supplied it."""
    if not overrides:
        return None
    prompt_layer = flight_module.parse_overrides(overrides)
    if "default_backend" not in prompt_layer:
        return None
    return str(config.get("default_backend") or "").strip() or None



def _with_resolved_overrides(payload: dict, override_resolution: dict) -> dict:
    """Attach each --set path's resolution to a dry-run document.

    Every dry-run document emitted after the resolution carries it, refusal
    documents included, so a caller reading one can tell an override that
    resolved from one that never applied.
    """
    if override_resolution:
        payload["overrides"] = override_resolution
    return payload



def _config_value_at(config, path: str):
    """Return the value a dotted flight-config path holds; None when unset."""
    node = config
    for segment in (part for part in path.split(".") if part):
        if not isinstance(node, Mapping) or segment not in node:
            return None
        node = node[segment]
    return node



def _layer_flight_config(flight_module, project, checkout_path):
    """Resolve the file layers alone, without this dispatch's prompt layer."""
    from reckon.crew.refusals import format_refusal

    try:
        return flight_module.resolve(project, checkout_path=checkout_path).config
    except flight_module.FlightConfigError as exc:
        raise click.ClickException(format_refusal("D06", str(exc))) from exc



def _require_configured_override_paths(flight_module, overrides, base) -> None:
    """Refuse any --set path naming a backend or role no layer defines.

    The preview and the launch apply this one check, so a name no config
    layer defines cannot be refused by one path and merged into a section
    nothing routes to by the other.
    """
    for pair in overrides:
        _require_configured_override_path(
            flight_module, pair.partition("=")[0].strip(), base
        )



def _require_configured_override_path(flight_module, path: str, base) -> None:
    """Refuse a --set path naming a backend or role no layer defines.

    An override under a keyed map the configuration does not carry — a
    misspelled backend or role name — merges into a section nothing routes to,
    so a dry run that echoed it would confirm an override that can change
    nothing. The refusal names the path and the defined names, matching the
    resolution already applied to ``default_backend`` and ``local_backend``.
    """
    from reckon.crew.refusals import format_refusal

    segments = [part for part in path.split(".") if part]
    for index in range(len(segments) - 1):
        if segments[index] not in flight_module._KEYED_MAPS:
            continue
        container = base.get(segments[index])
        name = segments[index + 1]
        if isinstance(container, Mapping) and name in container:
            continue
        defined = (
            ", ".join(sorted(str(entry) for entry in container))
            if isinstance(container, Mapping)
            else "none"
        )
        raise click.ClickException(
            format_refusal(
                "D06",
                f"--set path {path!r} names {name!r} under "
                f"{segments[index]!r}, which no config layer defines "
                f"(defined {segments[index]}: {defined}); override a path the "
                "configuration knows",
            )
        )



def _dispatch_override_resolution(
    flight_module, overrides, resolved, base, *, local: bool
) -> dict:
    """Report how each --set override resolved against the config layers.

    A dry run exists to confirm an override before a real dispatch spends a
    run, so every --set path is echoed with the value the resolved flight
    configuration holds there — null included, so an override to null is
    distinct from one that never applied — beside the value the layers
    beneath it held without it.
    """
    from reckon.crew.refusals import format_refusal

    if not overrides:
        return {}
    if local:
        try:
            base = flight_module.select_local_backend(base)
        except flight_module.FlightConfigError as exc:
            raise click.ClickException(format_refusal("D06", str(exc))) from exc
    resolution: dict[str, dict[str, Any]] = {}
    for pair in overrides:
        path = pair.partition("=")[0].strip()
        resolution[path] = {
            "before": _config_value_at(base, path),
            "resolved": _config_value_at(resolved, path),
        }
    return resolution



def _picker_routed_backend(config, selection):
    """The backend a picker selection sends the dispatch to, or ``""``.

    Mirrors the routing ``plan_dispatch`` applies to the same selection: a
    ``route`` action names its own backend, while a ``fallback``, a ``refuse``
    and a ``route`` carrying no backend all continue to the configured default.
    A ``hold`` resolves no backend — it is a budget decision, not a lane — so it
    is left to the picker's own hold path rather than resolving one here.
    """
    action = str(selection.get("action") or "")
    if action == "route":
        return str(selection.get("backend") or "") or str(
            config.get("default_backend") or ""
        )
    if action in {"fallback", "refuse"}:
        return str(config.get("default_backend") or "")
    return ""



def _model_availability_refusal(
    crew_module, flight_module, config, node, *, picker_selection=None
):
    """Return a typed refusal when the dispatch's backend does not serve its model.

    On the picker route the backend the dispatch will use is the one the picker
    resolved to, not the role's configured lane, so a selection is read here and
    the same availability probe the deterministic route runs is applied to that
    backend. A selection that resolves no concrete backend (a hold or a refusal)
    is left to the picker's own refusal path, so this returns no refusal for it.
    """
    if picker_selection is not None:
        backend_name = _picker_routed_backend(config, picker_selection)
        if not backend_name:
            return None
        from reckon.crew.routing import resolve_role_override

        try:
            backend_name, backend = resolve_role_override(
                config, node.role, node.spec_level, backend_name
            )
        except crew_module.CrewError:
            # A backend the picker named but no layer defines is not this
            # check's to refuse: routing resolves it moments later and reports
            # it with the routing context a caller needs.
            return None
    else:
        backend_name, backend = crew_module.resolve_role(
            config, node.role, node.spec_level
        )
    entry = flight_module.probe_availability({"backends": {backend_name: backend}})[
        backend_name
    ]
    if entry.get("model_served") is not False:
        return None
    return {
        "allowed": False,
        "backend": backend_name,
        "model": str(backend.get("model") or ""),
        "reason": entry["detail"],
        "refusal": "model-unavailable",
    }



def _repo_root(repo) -> Path:
    """Resolve the repository root a dispatch cuts its worktree from."""
    import subprocess

    if repo:
        return Path(repo).resolve()
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise click.ClickException(
            "not inside a git repository; pass --repo with the repository root"
        )
    return Path(result.stdout.strip()).resolve()



def _resolved_gc_repo(
    crew_module, flight_module, repo, project, confirm_cross_repo
) -> Path:
    """Resolve the repository a gc pass scans.

    With no ``--project`` the enclosing repository is the only sensible
    default, so this defers to ``_repo_root`` unchanged. With a ``--project``,
    the checkout registered in mounts is the answer gc must give even when the
    caller's current directory belongs to a different repository entirely —
    the measured defect: three different projects returned byte-identical
    counts scanned from the reckon checkout, and one project's reclaimable
    worktrees were reported as zero. An explicit
    ``--repo`` that disagrees with that registration is refused naming both
    paths, unless the caller states the cross-pairing deliberately with
    ``--confirm-cross-repo``.
    """
    registered_checkout: Path | None = None
    if project:
        try:
            mounts = flight_module.mounted_project_docs()
        except flight_module.FlightConfigError as exc:
            raise crew_module.CrewError(str(exc)) from exc
        docs = mounts.get(project)
        if docs is not None:
            registered_checkout = docs.parent.resolve()

    if repo is not None:
        resolved_repo = Path(repo).resolve()
        if (
            registered_checkout is not None
            and resolved_repo != registered_checkout
            and not confirm_cross_repo
        ):
            raise crew_module.CrewError(
                f"--repo {resolved_repo} disagrees with the checkout registered "
                f"for project {project!r} ({registered_checkout}); pass "
                "--confirm-cross-repo to scan --repo deliberately"
            )
        return resolved_repo

    if registered_checkout is not None:
        return registered_checkout

    return _repo_root(None)



def _peer_scopes(values) -> dict:
    """Parse repeated ``name=path[,path]`` peer scope declarations."""
    peers: dict[str, list[str]] = {}
    for value in values:
        name, sep, paths = value.partition("=")
        if not sep or not name.strip():
            raise click.ClickException(
                f"--peer {value!r} must be written as name=path[,path]"
            )
        peers[name.strip()] = [
            part.strip() for part in paths.split(",") if part.strip()
        ]
    return peers



def _parse_ready_node(statement: str) -> dict[str, Any]:
    """Read one ``--ready NAME=GROUP:SCORE`` node statement.

    The score is the node's open-endedness, which is what the bar judges, and
    the group names the wallet it will spend. The name is split on the first
    ``=`` and the score on the last ``:`` so a node name carrying either
    character still reads. A statement missing any of the three parts, or
    carrying a score that is not a number, is refused here rather than passed
    on: a node silently dropped from the admitted set is the failure the
    pre-flight exists to prevent.
    """
    name, separator, rest = statement.partition("=")
    group, score_separator, score = rest.rpartition(":")
    if not (separator and score_separator) or not (name.strip() and group.strip()):
        raise ValueError(f"a ready node must read NAME=GROUP:SCORE, not {statement!r}")
    try:
        value = float(score)
    except ValueError:
        raise ValueError(
            f"ready node {name.strip()!r} needs a numeric score, not {score!r}"
        ) from None
    return {"name": name.strip(), "group": group.strip(), "score": value}



@crew.command(name="pick")
@click.option(
    "--project", default=None, help="Project whose flight and ledger are read."
)
@click.option("--role", default=None)
@click.option(
    "--spec-level", type=click.Choice(["exact", "guided", "open"]), default=None
)
@click.option("--goal", default=None)
@click.option("--done-when", default=None)
@click.option("--comment", default="", help="Orchestrator context, passed verbatim.")
@click.option("--estimated-context", type=click.IntRange(min=0), default=0)
@click.option(
    "--capability", default="{}", help="Capability requirements as a JSON object."
)
@click.option("--session", default="")
@click.option("--replay", "replay_count", type=click.IntRange(min=1), default=None)
@click.option(
    "--outcomes", is_flag=True, help="Summarise recorded picker decisions and outcomes."
)
@click.option("--since", default=None, help="Inclusive ISO completion-time boundary.")
@click.option(
    "--all-projects", is_flag=True, help="Read every mounted project's ledger."
)
@click.option("--checkout-path", type=click.Path(path_type=Path), default=None)
@click.option("--pretty", is_flag=True)
def crew_pick(
    project,
    role,
    spec_level,
    goal,
    done_when,
    comment,
    estimated_context,
    capability,
    session,
    replay_count,
    outcomes,
    since,
    all_projects,
    checkout_path,
    pretty,
):
    """Read one node's live routing state to select a backend; dispatch no worker."""
    from reckon.crew.node import TaskNode
    from reckon.crew.picker import PickRequest, pick
    from reckon.crew.picker.replay import replay

    if outcomes:
        from reckon.crew.picker.outcomes import read_outcomes

        if replay_count:
            raise click.ClickException("--outcomes and --replay cannot be combined")
        if all_projects and project:
            raise click.ClickException(
                "--project and --all-projects cannot be combined"
            )
        try:
            payload = read_outcomes(
                project=None if all_projects else project, since=since
            )
        except (ValueError, OSError) as exc:
            raise click.ClickException(str(exc)) from exc
        _emit_crew_result(payload, pretty)
        return
    if since or all_projects:
        raise click.ClickException("--since and --all-projects require --outcomes")

    if not project:
        from bs4 import BeautifulSoup

        index = (checkout_path or Path.cwd()) / "docs" / "index.html"
        metadata = BeautifulSoup(index.read_text(), "html.parser").find(
            "meta", attrs={"name": "docs-project"}
        )
        if metadata is None or not metadata.get("content"):
            raise click.ClickException(
                "pass --project: docs/index.html declares no docs-project"
            )
        project = str(metadata["content"])
    _, flight_module = _crew_modules()
    config = _dispatch_resolved_flight(flight_module, project, checkout_path, ())
    repo = (checkout_path or Path.cwd()).resolve()
    try:
        if replay_count:
            payload = replay(project, replay_count, config, repo=repo)
        else:
            if any(value is None for value in (role, spec_level, goal, done_when)):
                raise click.ClickException(
                    "pick requires --role, --spec-level, --goal and --done-when"
                )
            requirements = json.loads(capability)
            if not isinstance(requirements, dict):
                raise click.ClickException("--capability must be a JSON object")
            node = TaskNode(
                id="pick",
                plan="",
                role=role,
                spec_level=spec_level,
                goal=goal,
                done_when=done_when,
            )
            payload = pick(
                PickRequest(
                    project, node, requirements, estimated_context, comment, session
                ),
                config,
                repo=repo,
            ).as_dict()
    except (ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_crew_result(payload, pretty)



@crew.command(name="preflight")
@click.option("--project", required=True, help="Project whose run records are read.")
@click.option(
    "--role",
    "roles",
    multiple=True,
    help="Role taking part in the wave; its backend is checked. Repeat as needed.",
)
@click.option(
    "--backend",
    "backends",
    multiple=True,
    help="Backend to check by name, instead of resolving roles.",
)
@click.option(
    "--purpose",
    type=click.Choice(["dispatch", "resume"]),
    default="dispatch",
    show_default=True,
    help="A dispatch keeps back the resume reserve; a resume may spend it.",
)
@click.option(
    "--checkout-path",
    default=None,
    type=click.Path(path_type=Path),
    help="Repo root whose ledger and project flight layer are read.",
)
@click.option(
    "--set",
    "overrides",
    multiple=True,
    metavar="KEY=VALUE",
    help="Flight override for this check; always wins over config layers.",
)
@click.option(
    "--ready",
    "ready",
    multiple=True,
    metavar="NAME=GROUP:SCORE",
    help="A node a wave would open with: its name, its declared wallet and its "
    "open-endedness score. Repeat as needed.",
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_preflight(
    project, roles, backends, purpose, checkout_path, overrides, ready, pretty
):
    """Report whether backend budget state allows a wave to open, without spending it.

    Reads the budget signal that earlier runs already recorded, so the check
    costs no worker budget; a backend whose config sets ``budget_check`` also has
    its own account surface read, which runs no model either. Exits 3 when any
    backend is held, naming its utilisation and reset time; a backend reporting no
    headroom is never held, because absence of a signal is not exhaustion.

    The pace beside the hold is read per backend, from whichever source speaks
    for that backend: a fresh reading in the published headroom document when the
    document carries one, else the recorded evidence -- each declared group's
    members' run receipts or a window a run's stream reported. One backend
    falling back never drags its sibling down, and the report names the source
    that spoke for each. Neither is invented: a group no reading reached reports
    unknown for both clocks.
    """
    from reckon import budget as budget_module
    from reckon import ledger as ledger_module

    crew_module, flight_module = _crew_modules()
    config = _dispatch_resolved_flight(flight_module, project, checkout_path, overrides)
    try:
        ready_nodes = [_parse_ready_node(statement) for statement in ready]
        windows = budget_module.recorded_windows(project, config, root=checkout_path)
        report = budget_module.preflight(
            project,
            config,
            backends=list(backends) or None,
            roles=list(roles) or None,
            root=checkout_path,
            purpose=purpose,
            windows=windows,
            document_path=budget_module.published_document_path(),
            ready=ready_nodes,
        )
        report["hold_history"] = budget_module.record_checks(
            project,
            report["backends"],
            root=checkout_path,
            resumption_fired=purpose == "resume",
        )
    except (crew_module.CrewError, ledger_module.LedgerError, ValueError) as exc:
        from reckon.crew.refusals import format_refusal

        raise click.ClickException(format_refusal("D01", str(exc))) from exc
    _emit_crew_result(report, pretty, failure_exit=3)
    raise click.exceptions.Exit(3 if report["held"] else 0)



@crew.command(name="dispatch")
@click.option("--project", required=True, help="Project owning the plan.")
@click.option("--plan", "plan_slug", default="", help="Plan slug the node serves.")
@click.option(
    "--brief",
    "brief_path",
    default="",
    help=(
        "Path to a stored brief. Alone, it is the node's authority in place of "
        "a committed plan section. Beside --plan and --section it is the "
        "coordinator's instructions for that section, carried verbatim while "
        "the plan stays the authority and every plan gate still runs."
    ),
)
@click.option("--section", default="", help="Plan section the node implements.")
@click.option("--role", default="implement", show_default=True, help="Routing role.")
@click.option(
    "--backend",
    default="",
    help="Backend name resolved through flight config.",
)
@click.option(
    "--route",
    type=click.Choice(["picker", "deterministic"]),
    default=None,
    help="Override routing.picker with picker or deterministic routing.",
)
@click.option("--comment", default="", help="Context passed verbatim to the picker.")
@click.option(
    "--spec-level",
    type=click.Choice(["exact", "guided", "open"]),
    default=None,
    help="Declared specification ownership level; omitted means undeclared.",
)
@click.option(
    "--negative-control",
    "negative_control",
    default="",
    help=(
        "The mutation this node's checks must fail against, declared when its "
        "write paths include a test file; `none: <reason>` for a check that "
        "admits no applicable mutation."
    ),
)
@click.option("--node", "node_id", required=True, help="Stable node id.")
@click.option("--goal", default="", help="The one deliverable this node produces.")
@click.option("--done-when", default="", help="The measure that emits evidence.")
@click.option(
    "--write-path",
    "write_paths",
    multiple=True,
    help="Exclusive write path; repeat for each.",
)
@click.option(
    "--peer",
    "peers",
    multiple=True,
    metavar="NAME=PATHS",
    help="Concurrent node and its paths, for the shared-file check.",
)
@click.option(
    "--requires-decision",
    "required_decisions",
    multiple=True,
    help="Decision key this node needs locked first.",
)
@click.option(
    "--locked-decision",
    "locked_decisions",
    multiple=True,
    help="Decision key already locked in the plan.",
)
@click.option("--time-budget", default="", help="Wall-clock allowance, e.g. 25m.")
@click.option(
    "--estimated-hours",
    type=click.FloatRange(min=0.0, min_open=True),
    default=None,
    help="Neutral worker-hours for this node; otherwise the plan estimate is labelled as fallback.",
)
@click.option("--manifest", default="", help="Manifest path the worker must write.")
@click.option(
    "--member",
    default="",
    help="Roster member to run this node, reusing its long-lived session.",
)
@click.option("--session", required=True, help="Opaque session id grouping worktrees.")
@click.option(
    "--wave",
    default="",
    help="Open wave id override; otherwise joins this session's open wave.",
)
@click.option("--base", default="HEAD", show_default=True, help="Worktree base ref.")
@click.option(
    "--repo",
    default=None,
    type=click.Path(path_type=Path),
    help="Repository root (default: the project's registered mount).",
)
@click.option(
    "--checkout-path",
    default=None,
    type=click.Path(path_type=Path),
    help="Repo root to read the project flight layer from.",
)
@click.option(
    "--set",
    "overrides",
    multiple=True,
    metavar="KEY=VALUE",
    help=(
        "Override one flight key after file layers. For routed effort, "
        "roles.<role>.by_spec_level.<level>.effort overlays "
        "backends.<name>.effort. A path naming a backend or role no config "
        "layer defines is refused."
    ),
)
@click.option(
    "--allow-execution-mismatch",
    is_flag=True,
    help=(
        "Dispatch despite an execution measure routed to a role declaring it "
        "cannot execute; the exception is recorded on the run."
    ),
)
@click.option(
    "--allow-orchestrator-lane",
    "orchestrator_lane_reason",
    default=None,
    metavar="REASON",
    help=(
        "Deliberately dispatch to a lane serving orchestrators and record REASON "
        "on the run and ledger row. Background work there spends orchestrator "
        "capacity; saturation stops every session."
    ),
)
@click.option(
    "--allow-unreconciled-runs",
    is_flag=True,
    help=(
        "Dispatch despite terminal run pointers older than the configured grace; "
        "the waived backlog is recorded on the new run."
    ),
)
@click.option(
    "--no-watch",
    is_flag=True,
    help=(
        "Dispatch without a live project watcher and record the explicit waiver "
        "on the run."
    ),
)
@click.option(
    "--allow-unreviewed-plan",
    is_flag=True,
    help=(
        "Dispatch a build against a plan carrying no answered review; the waived "
        "plan is recorded on the new run."
    ),
)
@click.option(
    "--local",
    is_flag=True,
    help="Route through the backend named by the resolved local_backend key.",
)
@click.option(
    "--repairs",
    "repairs",
    default="",
    help=(
        "Run id this dispatch repairs; must be a promoted run in the project's "
        "ledger. Its plan movement carries forward, so promotion needs no "
        "--no-impl-change."
    ),
)
@click.option(
    "--accept-directory-claim",
    "accept_directory_claim",
    is_flag=True,
    help=(
        "Proceed when a declared write path is a directory overlapping a live "
        "run's claim, recording the exception instead of refusing."
    ),
)
@click.option(
    "--no-fence",
    "no_fence_reason",
    default="",
    help=(
        "Launch the worker without the filesystem fence and record REASON on "
        "the run and its ledger row. This is the only way through when bwrap "
        "is absent or a user namespace cannot be created."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Validate and resolve only: no worktree, no process, no record.",
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
@_holds_stdout_for_one_document
def crew_dispatch(
    project,
    plan_slug,
    brief_path,
    section,
    role,
    backend,
    route,
    comment,
    spec_level,
    negative_control,
    node_id,
    goal,
    done_when,
    write_paths,
    peers,
    required_decisions,
    locked_decisions,
    time_budget,
    estimated_hours,
    manifest,
    member,
    session,
    wave,
    base,
    repo,
    checkout_path,
    overrides,
    allow_execution_mismatch,
    orchestrator_lane_reason,
    allow_unreconciled_runs,
    no_watch,
    allow_unreviewed_plan,
    local,
    repairs,
    accept_directory_claim,
    no_fence_reason,
    dry_run,
    pretty,
):
    """Launch a node whose contract, routing, budget, watcher, and scope allow it.

    Do not dispatch background work to an orchestrator lane: it runs the
    orchestrators; background work there costs orchestrator capacity, and
    saturating it stops every session rather than one node.

    One instruction covers every backend. Which harness runs, at what model,
    effort and sandbox tier, is resolved from flight config — so this command
    names none of them, and the caller branches only on the returned
    ``launch`` kind.
    """
    crew_module, flight_module = _crew_modules()
    from reckon.crew.dispatch import (
        LaneHeld,
        LanePaused,
        TmpHeadroomError,
        resolve_dispatch_route,
    )
    from reckon.crew.node import PlanReviewMissingError

    base_config = None
    try:
        if overrides:
            base_config = _layer_flight_config(flight_module, project, checkout_path)
            _require_configured_override_paths(flight_module, overrides, base_config)
        config = _dispatch_resolved_flight(
            flight_module, project, checkout_path, overrides
        )
    except click.ClickException as exc:
        if not dry_run:
            raise
        # A resolution the prompt layer cannot join — a leaf key the schema
        # refuses as well as a name no layer defines — is a request error, and
        # a dry run answers every request error on its JSON channel so a
        # caller that decodes stdout reads the refusal rather than nothing.
        _emit_dry_run_request_error(pretty, str(exc))
        raise click.exceptions.Exit(1) from exc
    flight_backend_override = _flight_default_backend_override(
        flight_module, config, overrides
    )
    # A lane named on the command line is itself a routing instruction: the
    # dispatch must run on that backend, so the picker has nothing to select.
    # When no --route was given the lane implies deterministic routing and the
    # named lane stands; only an explicit --route picker is a contradiction.
    named_lane = bool(backend or local or flight_backend_override)
    if route is None and named_lane:
        route = "deterministic"
    effective_route = resolve_dispatch_route(config, route)
    if effective_route == "picker" and (backend or local):
        raise click.UsageError(
            "picker routing cannot be combined with --backend or --local; "
            "use --route deterministic to override routing.picker"
        )
    if local:
        try:
            config = flight_module.select_local_backend(config)
        except flight_module.FlightConfigError as exc:
            from reckon.crew.refusals import format_refusal

            detail = format_refusal("D03", str(exc))
            if dry_run:
                # The local selection is what failed, so the echo reports what
                # the layers and the prompt alone resolved: a caller can still
                # tell a --set override that resolved from one that never
                # applied on this refusal.
                _emit_dry_run_request_error(
                    pretty,
                    detail,
                    _dispatch_override_resolution(
                        flight_module, overrides, config, base_config, local=False
                    ),
                )
            else:
                _emit(
                    {"ok": False, "error": "request-error", "detail": detail},
                    pretty,
                )
            raise click.exceptions.Exit(1) from exc

    node = crew_module.TaskNode(
        id=node_id,
        goal=goal,
        plan=plan_slug,
        section=section,
        brief=str(brief_path) if brief_path else "",
        role=role,
        spec_level=spec_level or "",
        negative_control=negative_control or "",
        done_when=done_when,
        write_paths=list(write_paths),
        time_budget=time_budget,
        estimated_hours=estimated_hours,
        manifest_path=manifest,
        requires_decisions=list(required_decisions),
        peer_scopes=_peer_scopes(peers),
    )

    override_resolution: dict = {}
    if dry_run:
        try:
            override_resolution = _dispatch_override_resolution(
                flight_module, overrides, config, base_config, local=local
            )
        except click.ClickException as exc:
            # A --set path the configuration does not know is a request error
            # on the same channel every other dry-run refusal answers on, so a
            # caller keying on ``error`` reads a refusal rather than a preview.
            _emit_dry_run_request_error(pretty, str(exc))
            raise click.exceptions.Exit(1) from exc

    from reckon.crew.dispatch import _dispatch_fleet_gate

    fleet_gate = _dispatch_fleet_gate()
    if fleet_gate["state"] in {"paused", "unreadable"}:
        held = {
            "ok": False,
            "error": "lane-paused",
            "detail": _lane_paused_detail(fleet_gate),
            "reason": fleet_gate.get("reason"),
            "lane_gate": fleet_gate,
        }
        if dry_run:
            held = _with_resolved_overrides(
                {**held, "dry_run": True}, override_resolution
            )
        _emit(held, pretty)
        raise click.exceptions.Exit(75)

    # The picker's own filter reads only cached observations, so a model the
    # account does not serve stays eligible for it when no probe has been
    # recorded. Ask the picker once here, then apply the same deterministic
    # availability probe to whichever backend it resolves — a routed backend, a
    # fallback to the default, or the default the refusal falls through to — so
    # the picker route can never send a dispatch to a backend the deterministic
    # route would have refused.
    picker_selection = None
    if effective_route == "picker":
        from reckon.crew.dispatch import (
            build_picker_inputs,
            dispatch_picker_selection,
            resolve_project_repository,
        )

        # The ledger rows, the verdict inputs and the budget snapshot are read
        # here, outside the picker's own latency bound, so the picker thread
        # spends its time picking rather than re-reading what the caller needs.
        # A failed build is recorded and handed on rather than raised, so the
        # picker falls back with the failure named instead of the dispatch
        # aborting before it is consulted.
        picker_repo = resolve_project_repository(project, repo)
        (
            picker_records,
            picker_verdict_inputs,
            picker_budget,
            picker_input_errors,
        ) = build_picker_inputs(project, config, picker_repo)
        picker_selection = dispatch_picker_selection(
            node=node,
            config=config,
            project=project,
            repo=picker_repo,
            session=session,
            comment=comment,
            records=picker_records,
            verdict_inputs=picker_verdict_inputs,
            budget_snapshot=picker_budget,
            input_errors=picker_input_errors,
        )

    availability_refusal = _model_availability_refusal(
        crew_module,
        flight_module,
        config,
        node,
        picker_selection=picker_selection if effective_route == "picker" else None,
    )
    if availability_refusal is not None:
        from reckon.crew.refusals import format_refusal

        refusal = {
            "ok": False,
            "error": "competence-refusal",
            "detail": format_refusal("D04", str(availability_refusal["reason"])),
            "competence": availability_refusal,
        }
        if picker_selection is not None:
            # The picker answer that led to the refusal rides on the payload,
            # so a caller reads which backend the picker resolved and why.
            refusal["picker_selection"] = picker_selection
        if dry_run:
            # A dry-run refusal document, so it carries the same dry_run
            # marker and --set echo every other refusal the preview emits
            # does, and a caller can tell an override that resolved from one
            # that never applied on this refusal too.
            refusal["dry_run"] = True
            refusal = _with_resolved_overrides(refusal, override_resolution)
        _emit(refusal, pretty)
        raise click.exceptions.Exit(5)

    if dry_run:
        try:
            resolution = crew_module.plan_dispatch(
                node=node,
                config=config,
                locked_decisions=locked_decisions,
                peer_scopes=node.peer_scopes,
                project=project,
                repo=repo,
                base=base,
                execution_override=allow_execution_mismatch,
                orchestrator_lane_reason=orchestrator_lane_reason,
                report_live_conflicts=True,
                local=local,
                backend_override=backend,
                default_backend_override=flight_backend_override,
                member=member,
                allow_unreviewed_plan=allow_unreviewed_plan,
                session=session,
                watch_required=True,
                watch_override=no_watch,
                repairs=repairs,
                accept_directory_claim=accept_directory_claim,
                route=route,
                picker_selection=picker_selection,
            )
        except LanePaused as exc:
            _emit(
                _with_resolved_overrides(
                    {
                        "ok": False,
                        "dry_run": True,
                        "error": "lane-paused",
                        "detail": _lane_paused_detail(exc.gate),
                        "reason": exc.gate.get("reason"),
                        "lane_gate": exc.gate,
                    },
                    override_resolution,
                ),
                pretty,
            )
            raise click.exceptions.Exit(75) from exc
        except crew_module.BudgetHold as exc:
            _emit(
                _with_resolved_overrides(
                    {
                        "ok": False,
                        "dry_run": True,
                        "error": "budget-hold",
                        "detail": str(exc),
                        "hold": exc.verdict,
                    },
                    override_resolution,
                ),
                pretty,
            )
            raise click.exceptions.Exit(3) from exc
        except crew_module.PlanVisibilityError as exc:
            _emit(
                _with_resolved_overrides(
                    {"ok": False, "error": "plan-unavailable", "detail": str(exc)},
                    override_resolution,
                ),
                pretty,
            )
            raise click.exceptions.Exit(4) from exc
        except PlanReviewMissingError as exc:
            # The plan is readable but unreviewed: one refusal class, one error
            # key, on both the validating and the launching path, so an operator
            # diagnosing with --dry-run is pointed at the composed review rather
            # than at mounts.
            _emit(
                _with_resolved_overrides(
                    {"ok": False, "error": "plan-review-missing", "detail": str(exc)},
                    override_resolution,
                ),
                pretty,
            )
            raise click.exceptions.Exit(4) from exc
        except crew_module.CompetenceLimit as exc:
            _emit(
                _with_resolved_overrides(
                    {
                        "ok": False,
                        "error": "competence-refusal",
                        "detail": str(exc),
                        "competence": exc.verdict,
                    },
                    override_resolution,
                ),
                pretty,
            )
            raise click.exceptions.Exit(5) from exc
        except crew_module.WatcherRequired as exc:
            # The same refusal class, error key and exit code the launching path
            # carries, so a validating caller reaches the admission judgement a
            # real dispatch reaches rather than a generic dispatch refusal.
            _emit(
                _with_resolved_overrides(
                    {
                        "ok": False,
                        "dry_run": True,
                        "error": "watcher-required",
                        "detail": str(exc),
                        "watch": exc.watch,
                    },
                    override_resolution,
                ),
                pretty,
            )
            raise click.exceptions.Exit(8) from exc
        except TmpHeadroomError as exc:
            _emit(
                _with_resolved_overrides(
                    {
                        "ok": False,
                        "dry_run": True,
                        "error": "tmp-headroom-refusal",
                        "detail": str(exc),
                        "free_bytes": exc.free_bytes,
                        "floor_bytes": exc.floor_bytes,
                    },
                    override_resolution,
                ),
                pretty,
            )
            raise click.exceptions.Exit(75) from exc
        except crew_module.CrewError as exc:
            _emit(
                _with_resolved_overrides(
                    {
                        "ok": False,
                        "dry_run": True,
                        "error": "dispatch-refused",
                        "detail": str(exc),
                    },
                    override_resolution,
                ),
                pretty,
            )
            click.echo(f"Error: {exc}", err=True)
            raise click.exceptions.Exit(1) from exc
        if resolution.competence and not resolution.competence["allowed"]:
            _emit(
                _with_resolved_overrides(
                    {
                        "ok": False,
                        "dry_run": True,
                        "error": "competence-refusal",
                        "detail": str(
                            resolution.competence.get("reason")
                            or "the node exceeds the competence horizon"
                        ),
                        "competence": resolution.competence,
                    },
                    override_resolution,
                ),
                pretty,
            )
            raise click.exceptions.Exit(5)
        if not resolution.validation.ok:
            # A refused contract still answers on the channel every other
            # refusal uses: a caller keying on ``error`` otherwise reads a
            # failed validation as a dispatch that would proceed, which is the
            # reading the skill's "every refusal carries error and detail"
            # promise exists to prevent. The findings stay under ``validation``
            # for a caller that wants them structured.
            _emit(
                _with_resolved_overrides(
                    {
                        "ok": False,
                        "dry_run": True,
                        "error": "contract-validation",
                        "detail": _validation_detail(resolution.validation),
                        **resolution.as_dict(),
                    },
                    override_resolution,
                ),
                pretty,
            )
            raise click.exceptions.Exit(2)
        admission = resolution.admission or {}
        if admission.get("state") == "refused":
            _emit(
                _with_resolved_overrides(
                    {
                        "dry_run": True,
                        **resolution.as_dict(),
                        "ok": False,
                        "error": "scope-conflict",
                        "detail": admission["detail"],
                        "run_id": None,
                        "conflicting_run_id": admission["conflicting_run_id"],
                        "conflicting_node_id": admission["conflicting_node_id"],
                        "candidate_path": admission["candidate_path"],
                        "claimed_path": admission["claimed_path"],
                    },
                    override_resolution,
                ),
                pretty,
            )
            raise click.exceptions.Exit(7)
        lane_gate = resolution.lane_gate or {}
        if lane_gate.get("state") in {"paused", "unreadable"}:
            # The dry run is the same decision as the launch, so a gate that
            # would hold the launch holds the preview: the caller reaches the
            # temporary failure a real dispatch would exit on rather than a
            # validation that reads as a go-ahead.
            _emit(
                _with_resolved_overrides(
                    {
                        "ok": False,
                        "dry_run": True,
                        "error": "lane-paused",
                        "detail": _lane_paused_detail(lane_gate),
                        "reason": lane_gate.get("reason"),
                        "lane_gate": lane_gate,
                    },
                    override_resolution,
                ),
                pretty,
            )
            raise click.exceptions.Exit(75)
        payload = {"dry_run": True, **resolution.as_dict()}
        _emit_crew_result(
            _with_resolved_overrides(payload, override_resolution), pretty
        )
        raise click.exceptions.Exit(0)

    try:
        record = crew_module.dispatch(
            node=node,
            project=project,
            repo=repo,
            config=config,
            session=session,
            wave=wave,
            base=base,
            locked_decisions=locked_decisions,
            peer_scopes=node.peer_scopes,
            member=member,
            execution_override=allow_execution_mismatch,
            orchestrator_lane_reason=orchestrator_lane_reason,
            unreconciled_override=allow_unreconciled_runs,
            unreviewed_plan_override=allow_unreviewed_plan,
            watch_required=True,
            watch_override=no_watch,
            local=local,
            backend_override=backend,
            default_backend_override=flight_backend_override,
            repairs=repairs,
            accept_directory_claim=accept_directory_claim,
            no_fence_reason=no_fence_reason,
            route=route,
            comment=comment,
            picker_selection=picker_selection,
            dispatch_options=json.loads(
                json.dumps(click.get_current_context().params, default=str)
            ),
        )
    except crew_module.PlanVisibilityError as exc:
        _emit(
            {"ok": False, "error": "plan-unavailable", "detail": str(exc)},
            pretty,
        )
        raise click.exceptions.Exit(4) from exc
    except PlanReviewMissingError as exc:
        # The plan is readable but unreviewed: a refusal the caller resolves by
        # dispatching the composed review, so it carries its own error key on
        # this path exactly as it does on the dry run, and rides the
        # plan-unavailable exit code rather than the generic dispatch refusal.
        _emit(
            {"ok": False, "error": "plan-review-missing", "detail": str(exc)},
            pretty,
        )
        raise click.exceptions.Exit(4) from exc
    except crew_module.BudgetHold as exc:
        # Held, not failed: nothing was created and the node is still ready, so
        # this exits on its own code rather than as an error the caller would
        # otherwise answer by reshaping work that is fine.
        _emit(
            {
                "ok": False,
                "error": "budget-hold",
                "detail": str(exc),
                "hold": exc.verdict,
            },
            pretty,
        )
        raise click.exceptions.Exit(3) from exc
    except LanePaused as exc:
        # Neither a launch nor a refusal: the lane's own gate says paused, or
        # cannot be answered, so the dispatch waits. Nothing was created and the
        # node is still ready, so it exits on the conventional temporary-failure
        # code a caller retries after, and the gate the decision was read from
        # rides the result.
        _emit(
            {
                "ok": False,
                "error": "lane-paused",
                "detail": _lane_paused_detail(exc.gate),
                "reason": exc.gate.get("reason"),
                "lane_gate": exc.gate,
                **(
                    exc.queued
                    if isinstance(exc, LaneHeld) and hasattr(exc, "queued")
                    else {}
                ),
            },
            pretty,
        )
        raise click.exceptions.Exit(75) from exc
    except crew_module.CompetenceLimit as exc:
        _emit(
            {
                "ok": False,
                "error": "competence-refusal",
                "detail": str(exc),
                "competence": exc.verdict,
            },
            pretty,
        )
        raise click.exceptions.Exit(5) from exc
    except crew_module.UnreconciledRuns as exc:
        _emit(
            {
                "ok": False,
                "error": "unreconciled-runs",
                "detail": str(exc),
                "runs": exc.runs,
                "peer_runs": getattr(exc, "peer_runs", []),
            },
            pretty,
        )
        raise click.exceptions.Exit(6) from exc
    except crew_module.ScopeConflict as exc:
        # The refused call's own run id is not on the refusal: it is minted
        # inside the dispatch and released with the claim, so reporting it here
        # would name a run that does not exist. The live run holding the path is
        # the claim's own, and is named as such.
        _emit(
            {
                "ok": False,
                "error": "scope-conflict",
                "detail": str(exc),
                "run_id": None,
                "node": node_id,
                "conflicting_run_id": exc.run_id,
                "conflicting_node_id": exc.node_id,
                "candidate_path": exc.candidate_path,
                "claimed_path": exc.claimed_path,
            },
            pretty,
        )
        raise click.exceptions.Exit(7) from exc
    except crew_module.WatcherRequired as exc:
        _emit(
            {
                "ok": False,
                "error": "watcher-required",
                "detail": str(exc),
                "watch": exc.watch,
            },
            pretty,
        )
        raise click.exceptions.Exit(8) from exc
    except crew_module.MemberInFlight as exc:
        _emit(
            {
                "ok": False,
                "error": "member-in-flight",
                "detail": str(exc),
                "member": exc.member,
                "run_id": exc.run_id,
            },
            pretty,
        )
        raise click.exceptions.Exit(9) from exc
    except TmpHeadroomError as exc:
        _emit(
            {
                "ok": False,
                "error": "tmp-headroom-refusal",
                "detail": str(exc),
                "free_bytes": exc.free_bytes,
                "floor_bytes": exc.floor_bytes,
            },
            pretty,
        )
        raise click.exceptions.Exit(75) from exc
    except crew_module.CrewError as exc:
        if str(exc).startswith("node is not dispatchable"):
            _emit(
                {"ok": False, "error": "not-dispatchable", "detail": str(exc)}, pretty
            )
            raise click.exceptions.Exit(2) from exc
        # The caller reads stdout, so a refusal that writes only to stderr is
        # indistinguishable from a dispatch that produced nothing — and one
        # chained behind another in the same shell has the successor's status
        # to hide behind. Every refusal answers on the documented channel;
        # stderr keeps the sentence for whoever is reading a terminal.
        _emit({"ok": False, "error": "dispatch-refused", "detail": str(exc)}, pretty)
        click.echo(f"Error: {exc}", err=True)
        raise click.exceptions.Exit(1) from exc
    _emit_crew_result(record, pretty)



@crew.command(name="shadow")
@click.option("--run", "run_id", required=True, help="Committed primary run id.")
@click.option(
    "--session", required=True, help="Dispatching session that owns this shadow."
)
@click.option(
    "--wave",
    default="",
    help="Open wave id override; otherwise joins this session's open wave.",
)
@click.option(
    "--backend",
    required=True,
    help="Candidate backend name resolved through flight config.",
)
@click.option(
    "--set",
    "overrides",
    multiple=True,
    metavar="KEY=VALUE",
    help="Flight override for this shadow; always wins over config layers.",
)
@click.option(
    "--member",
    default="",
    help="Roster member to run the shadow, reusing its model-matched session.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Validate and resolve only: no worktree, process or live pointer.",
)
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_shadow(run_id, session, wave, backend, overrides, member, dry_run, pretty):
    """Re-run a committed run at its original base as isolated evidence."""
    from reckon.crew.dispatch import shadow, shadow_source

    crew_module, flight_module = _crew_modules()
    repo = _repo_root(None)
    try:
        source = shadow_source(run_id, repo=repo)
        node = source["node"]
        routed_overrides = [*overrides, f"roles.{node.role}.backend={backend}"]
        if node.spec_level:
            routed_overrides.append(
                f"roles.{node.role}.by_spec_level.{node.spec_level}.backend={backend}"
            )
        config = _dispatch_resolved_flight(
            flight_module,
            source["project"],
            repo,
            routed_overrides,
        )
        configuration_fields = {
            "launch",
            "model",
            "effort",
            "sandbox",
            "time_budget",
        }
        applicable_prefixes = {
            f"backends.{backend}",
            f"roles.{node.role}",
        }
        if node.spec_level:
            applicable_prefixes.add(
                f"roles.{node.role}.by_spec_level.{node.spec_level}"
            )
        configuration_overrides = set()
        for pair in overrides:
            key, separator, _value = pair.partition("=")
            prefix, _dot, field = key.rpartition(".")
            if (
                separator
                and prefix in applicable_prefixes
                and field in configuration_fields
            ):
                configuration_overrides.add(field)
        record = shadow(
            run_id,
            candidate_backend=backend,
            config=config,
            repo=repo,
            session=session,
            wave=wave,
            member=member,
            configuration_overrides=configuration_overrides,
            dry_run=dry_run,
        )
    except crew_module.PlanVisibilityError as exc:
        _emit(
            {"ok": False, "error": "plan-unavailable", "detail": str(exc)},
            pretty,
        )
        raise click.exceptions.Exit(4) from exc
    except crew_module.BudgetHold as exc:
        _emit(
            {
                "ok": False,
                "error": "budget-hold",
                "detail": str(exc),
                "hold": exc.verdict,
            },
            pretty,
        )
        raise click.exceptions.Exit(3) from exc
    except crew_module.CompetenceLimit as exc:
        _emit(
            {
                "ok": False,
                "error": "competence-refusal",
                "detail": str(exc),
                "competence": exc.verdict,
            },
            pretty,
        )
        raise click.exceptions.Exit(5) from exc
    except crew_module.UnreconciledRuns as exc:
        _emit(
            {
                "ok": False,
                "error": "unreconciled-runs",
                "detail": str(exc),
                "runs": exc.runs,
                "peer_runs": getattr(exc, "peer_runs", []),
            },
            pretty,
        )
        raise click.exceptions.Exit(6) from exc
    except crew_module.CrewError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_crew_result(record, pretty)



@crew.command(name="review-plan")
@click.option("--project", required=True)
@click.option("--plan", "plan_slug", required=True)
@click.option("--session", default="")
@click.option("--rubric", type=click.Choice(["design", "content"]), default="design")
@click.option("--local", is_flag=True)
@click.option("--dry-run", is_flag=True)
@click.option("--pretty", is_flag=True)
@click.option("--answer", default="")
@click.option("--acted", is_flag=True)
@click.option("--declined", default=None, help="Decline a finding with this reason.")
@_holds_stdout_for_one_document
def crew_review_plan(
    project, plan_slug, session, rubric, local, dry_run, pretty, answer, acted, declined
):
    """Review one plan's current content, or answer a finding its review raised.

    A review is composed under the content rubric when an author finishes
    writing and under the design rubric once before the plan's first
    implementation node. An answer is written to the newest stored review that
    raised the finding, acted on or declined with a reason.
    """
    from reckon.crew import plan_review, recovery
    from reckon.crew.node import CrewError

    detail = ""
    if answer:
        if acted == (declined is not None):
            detail = "--answer requires exactly one of --acted or --declined REASON"
        elif dry_run or local:
            detail = "--dry-run and --local apply only to review dispatch"
    elif acted or declined is not None:
        detail = "--acted and --declined require --answer FINDING_ID"
    elif not session:
        detail = "review dispatch requires --session"
    if detail:
        _emit({"error": "crew_error", "detail": detail}, pretty)
        raise click.exceptions.Exit(1)
    try:
        if answer:
            from reckon._store import _resolve_html_file

            # A review exists only as a delivered sidecar until something stores
            # it, and the answer verb is often the first reader to touch the
            # plan after its review lands, so it stores the delivery here rather
            # than failing as though no review existed.
            plan_review.store_delivered_reviews(project, plan_slug)
            plan_path = _resolve_html_file(project, plan_slug, artifact_type="plan")
            record = plan_review.read_plan_review(
                project, plan_slug, plan=plan_path if plan_path is not None else ""
            )
            if record is None:
                from reckon.mcp import _stale_plan_review

                stale = _stale_plan_review(project, plan_slug)
                detail = (
                    stale[1] if stale else f"no stored review for {project}:{plan_slug}"
                )
                _emit({"error": "crew_error", "detail": detail}, pretty)
                raise click.exceptions.Exit(1)
            if answer not in plan_review.finding_ids(record):
                # A finding of the plan's one design review stays owed after a
                # later content review becomes the newest record, so it is
                # answered on the design review that raised it.
                design = plan_review.newest_design_review(project, plan_slug)
                if design is not None and answer in plan_review.finding_ids(design):
                    record = design
            path = plan_review.record_response(
                record,
                answer,
                action="acted" if acted else "declined",
                reason=declined,
                by=session,
            )
            updated = json.loads(path.read_text(encoding="utf-8"))
            _emit(
                {
                    "record": updated,
                    "unanswered": plan_review.unanswered_findings(updated),
                },
                pretty,
            )
            return
        subject = recovery.plan_review_subject(
            project, plan_slug, session, rubric=rubric, local=local
        )
        result = recovery.dispatch_review_for_run(subject, dry_run=dry_run)
        _emit(result, pretty)
        if result.get("refused") or result.get("awaiting_lane") or result.get("error"):
            raise click.exceptions.Exit(1)
    except (ValueError, OSError, CrewError) as exc:
        _emit({"error": "crew_error", "detail": str(exc)}, pretty)
        raise click.exceptions.Exit(1) from exc



@crew.command(name="attach")
@click.option("--run", "run_id", required=True, help="Run id returned by dispatch.")
@click.option("--task", required=True, help="The harness's own task identifier.")
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_attach(run_id, task, pretty):
    """Bind a prepared in-harness run to the task executing it."""
    crew_module, _ = _crew_modules()
    try:
        record = crew_module.attach(run_id, task)
    except crew_module.CrewError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_crew_result(record, pretty)



def _resolved_session(run_id, record=None) -> dict[str, Any]:
    """The session answer every surface reports, in one shape.

    Three keys rather than one, because a bare id cannot say whether an absence
    means nobody has looked yet: the id, the source that supplied it, and the
    resolution naming every source consulted. Each surface reports the same
    three, so two of them asked about the same run cannot disagree.
    """
    from reckon.crew.resumption import resolve_session

    answer = resolve_session(run_id, record=record)
    return {
        "session_id": answer["session_id"],
        "session_source": answer["source"],
        "session_resolution": answer,
    }



@crew.command(name="observe")
@click.option("--run", "run_id", required=True, help="Run id to read from disk.")
@click.option("--project", default=None, help="Project whose flight layer applies.")
@click.option("--pretty", is_flag=True, help="Indent the JSON for reading.")
def crew_observe(run_id, project, pretty):
    """Refresh a live run record from its stream, manifest, and process state.

    Reports the phase, the resolved session id and whatever budget signal the
    backend emitted — which may legitimately read ``unknown``. Absence of a
    signal is never reported as exhaustion, and absence of a session is
    reported as a stated absence naming what was consulted rather than as a
    bare null a reader has to interpret.
    """
    crew_module, flight_module = _crew_modules()
    config = None
    if project:
        config = _resolved_flight(flight_module, project, None, ())
    # Resolved before the fold, not after: this reports where the id was found,
    # and folding it into the pointer first would make every observation
    # answer "pointer" and hide which source actually held it.
    resolved = _resolved_session(run_id)
    try:
        record = crew_module.observe(run_id, config=config)
    except crew_module.CrewError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_crew_result({**record, **resolved}, pretty, observation=True)

