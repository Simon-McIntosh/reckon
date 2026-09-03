const { useEffect, useState } = React;

const CREW_POLL_INTERVAL_MS = 3000;

const CREW_CARD_STYLES = String.raw`.r-crew-contract{-webkit-line-clamp:1}`;

function formatCrewElapsed(seconds) {
  if (seconds == null || Number.isNaN(Number(seconds))) return "—";
  const total = Math.max(0, Math.floor(Number(seconds)));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const remainder = total % 60;
  if (hours) return `${hours}h ${String(minutes).padStart(2, "0")}m`;
  if (minutes) return `${minutes}m ${String(remainder).padStart(2, "0")}s`;
  return `${remainder}s`;
}

function formatCrewActivity(stamp) {
  if (!stamp) return "—";
  const parsed = new Date(stamp);
  if (Number.isNaN(parsed.getTime())) return stamp;
  return parsed.toLocaleString([], {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function crewDurationSeconds(value) {
  if (value == null || value === "") return null;
  if (Number.isFinite(Number(value))) return Math.max(0, Number(value));
  const match = String(value).trim().match(/^(\d+(?:\.\d+)?)\s*(s|m|h)$/i);
  if (!match) return null;
  const unit = { s: 1, m: 60, h: 3600 }[match[2].toLowerCase()];
  return Number(match[1]) * unit;
}

function crewGateProgress(run) {
  const gates = Array.isArray(run.gates) ? run.gates : [];
  const total = Math.max(0, Number(run.gates_total ?? run.gate_total ?? gates.length) || 0);
  const measured = gates.filter(gate => {
    const verdict = String(gate?.verdict || "").toLowerCase();
    return verdict && verdict !== "pending" && verdict !== "unknown";
  }).length;
  const completed = Math.min(total, Math.max(0, Number(run.gates_done ?? run.gate_done ?? measured) || 0));
  const current = run.current_gate || run.gate_name || gates.find(gate => !gate?.verdict)?.measure || "Awaiting measured gate";
  const verdict = run.gate_verdict || run.verdict || gates.findLast?.(gate => gate?.verdict)?.verdict || "";
  return { total, completed, current: String(current), verdict: String(verdict) };
}

function crewPlanEffortHours(run, state) {
  const direct = Number(run.plan_effort_hours ?? run.effort_hours);
  if (Number.isFinite(direct) && direct > 0) return direct;

  const inventory = Array.isArray(state?.inventory) ? state.inventory : [];
  const stateProject = state?.project || "";
  if (run.project && stateProject && run.project !== stateProject) return null;
  const plan = inventory.find(item => item.slug === run.plan && (item.type || "plan") === "plan");
  const hours = Number(plan?.effort_hours);
  return Number.isFinite(hours) && hours > 0 ? hours : null;
}

function formatCrewPlanEffort(run, state) {
  const hours = crewPlanEffortHours(run, state);
  return hours == null ? "worker-hours unavailable" : `${hours} worker-hours`;
}

function crewCardProjection(run, state) {
  const elapsed = Number(run.elapsed_seconds);
  const budgetSeconds = crewDurationSeconds(run.budget_seconds ?? run.time_budget);
  const budgetRatio = budgetSeconds && Number.isFinite(elapsed)
    ? Math.min(1, Math.max(0, elapsed / budgetSeconds))
    : 0;
  return {
    identity: run.member || run.run_id || "unassigned",
    role: run.role || "—",
    planEffort: formatCrewPlanEffort(run, state),
    phase: run.phase || "idle",
    doneWhen: run.done_when || run.gate || "No done-when recorded.",
    lastActivity: formatCrewActivity(run.last_activity),
    elapsed: formatCrewElapsed(run.elapsed_seconds),
    budget: budgetSeconds == null ? "—" : formatCrewElapsed(budgetSeconds),
    budgetPercent: Math.round(budgetRatio * 100),
    gates: crewGateProgress(run),
    session: run.session || run.session_id || "—",
    host: run.host || run.hostname || "—",
    attachCommand: run.attach_command || run.attach_with || "",
  };
}

function CrewRunCard({ run }) {
  const card = crewCardProjection(run, window.STATE);
  const concerning = ["asking", "blocked", "failed", "idle", "stalled"].includes(card.phase.toLowerCase());

  const copyAttach = () => {
    if (!card.attachCommand) return;
    navigator.clipboard?.writeText(card.attachCommand);
    if (window.flashSaved) window.flashSaved("attach command copied");
  };

  return (
    <article className={`r-crew-card ${concerning ? "needs-attention" : ""}`} data-phase={card.phase}>
      <div className="r-crew-main">
        <div className="r-crew-identity">
          <span className="r-crew-phase-dot" aria-hidden="true"></span>
          <strong>{card.identity}</strong>
          <span>{card.role}</span>
          <span className="r-crew-plan-effort">{card.planEffort}</span>
          <span className="r-crew-phase">{card.phase}</span>
        </div>
        <div className="r-crew-location">
          <code>{run.project || "—"}</code>
          {run.plan_href ? <a href={run.plan_href}>{run.plan || "—"}</a> : <span>{run.plan || "—"}</span>}
          <span>{run.section || "—"}</span>
          {run.sprint_href && <a className="r-crew-sprint" href={run.sprint_href}>sprint</a>}
        </div>
        <details className="r-crew-done-when">
          <summary><span className="r-crew-label">done-when</span><span className="r-crew-contract">{card.doneWhen}</span></summary>
          <p>{card.doneWhen}</p>
        </details>
      </div>

      <div className="r-crew-budget">
        <div className="r-crew-label">budget</div>
        <div className="r-crew-budget-values"><strong>{card.elapsed}</strong><span>/ {card.budget}</span></div>
        <div className="r-crew-meter" aria-label={`${card.budgetPercent}% of budget elapsed`}>
          <i style={{ width: `${card.budgetPercent}%` }}></i>
        </div>
        <div className="r-crew-activity" title={run.last_activity || ""}>active {card.lastActivity}</div>
      </div>

      <div className="r-crew-gates">
        <div className="r-crew-gate-head">
          <span className="r-crew-label">gates {card.gates.completed} / {card.gates.total || "—"}</span>
          {card.gates.verdict && <strong className={`r-crew-verdict ${card.gates.verdict.toLowerCase()}`}>{card.gates.verdict}</strong>}
        </div>
        <div className="r-crew-gate-marks" aria-label={`${card.gates.completed} of ${card.gates.total} gates measured`}>
          {Array.from({ length: card.gates.total || 1 }, (_, index) => (
            <i key={index} className={index < card.gates.completed ? "measured" : ""}></i>
          ))}
        </div>
        <div className="r-crew-current-gate"><span>now:</span> {card.gates.current}</div>
      </div>

      <details className="r-crew-connect">
        <summary>Session and attach</summary>
        <div className="r-crew-connect-grid">
          <span><span className="r-crew-label">backend</span><code>{run.backend || run.harness || "—"}</code></span>
          <span><span className="r-crew-label">model</span><code>{run.model || "—"}</code></span>
          <span><span className="r-crew-label">runtime effort</span><code>{run.effort || "—"}</code></span>
          <span><span className="r-crew-label">session</span><code>{card.session}</code></span>
          <span><span className="r-crew-label">host</span><code>{card.host}</code></span>
        </div>
        <div className="r-crew-attach">
          <code>{card.attachCommand || "Attach command unavailable"}</code>
          <button className="btn sm" type="button" disabled={!card.attachCommand} onClick={copyAttach}>Copy attach</button>
        </div>
      </details>
    </article>
  );
}

function crewRunsForVisibleProjects(runs, visibleProjects) {
  if (!Array.isArray(visibleProjects)) return runs || [];
  const visible = new Set(visibleProjects);
  return (runs || []).filter(run => visible.has(run.project));
}

function crewScopedProjects(selectedProject, visibleProjects, allVisible) {
  if (allVisible || !selectedProject) {
    return Array.isArray(visibleProjects) ? visibleProjects : [];
  }
  return [selectedProject];
}

function CrewView({ visibleProjects, mountedProjectCount, selectedProject }) {
  const [runs, setRuns] = useState([]);
  const [error, setError] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [refreshedAt, setRefreshedAt] = useState(null);
  const [allVisible, setAllVisible] = useState(false);

  useEffect(() => {
    let active = true;

    const poll = async () => {
      try {
        const response = await fetch("/crew", { cache: "no-store" });
        if (!response.ok) throw new Error(`crew route returned ${response.status}`);
        const payload = await response.json();
        if (!Array.isArray(payload.runs)) throw new Error("crew route returned no run list");
        if (!active) return;
        setRuns(payload.runs);
        setError("");
        setRefreshedAt(new Date());
      } catch (cause) {
        if (!active) return;
        setError(cause instanceof Error ? cause.message : "crew route unavailable");
      } finally {
        if (active) setLoaded(true);
      }
    };

    poll();
    const timer = window.setInterval(poll, CREW_POLL_INTERVAL_MS);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, []);

  const scopedProjects = crewScopedProjects(selectedProject, visibleProjects, allVisible);
  const visibleRuns = crewRunsForVisibleProjects(runs, scopedProjects);
  const scopeLabel = allVisible || !selectedProject
    ? `All visible · ${visibleRuns.length} runs`
    : `${selectedProject} · ${visibleRuns.length} runs`;

  return (
    <div className="r-crew-surface">
      <style>{CREW_CARD_STYLES}</style>
      <div className="r-crew-heading">
        <h1>{scopeLabel}</h1>
        <button
          type="button"
          className="r-crew-scope-toggle"
          aria-pressed={allVisible}
          onClick={() => setAllVisible(value => !value)}
        >
          All visible
        </button>
        <span>
          {mountedProjectCount || 0} mounted · polls every {CREW_POLL_INTERVAL_MS / 1000}s
          {refreshedAt ? ` · refreshed ${refreshedAt.toLocaleTimeString()}` : ""}
        </span>
      </div>

      {error && <div role="status" className="r-crew-error">{error}</div>}

      {!loaded ? (
        <div className="r-crew-empty">Loading live runs…</div>
      ) : visibleRuns.length === 0 ? (
        <div className="r-crew-empty">No live runs.</div>
      ) : (
        <div className="r-crew-list" aria-label="Live crew runs">
          {visibleRuns.map(run => <CrewRunCard key={`${run.project || ""}:${run.run_id}`} run={run} />)}
        </div>
      )}
    </div>
  );
}

window.CrewView = CrewView;
