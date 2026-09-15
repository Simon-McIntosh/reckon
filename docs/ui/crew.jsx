const { useEffect, useState } = React;

const CREW_POLL_INTERVAL_MS = 3000;

const CREW_CARD_STYLES = String.raw`.r-crew-contract{-webkit-line-clamp:1}`;

const RECORDED_FLOW_STATUSES = new Set(["shipped", "done", "superseded", "historical"]);
const ACTIVE_FLOW_STATUSES = new Set(["active", "in-progress"]);

function flowPackLanes(items) {
  const lanes = [];
  (items || []).forEach(item => {
    const free = lanes
      .filter(candidate => candidate.lastEnd <= item.start + 0.01)
      .sort((left, right) => right.lastEnd - left.lastEnd)[0];
    const target = free || { lastEnd: Number.NEGATIVE_INFINITY, items: [] };
    if (!free) lanes.push(target);
    target.items.push(item);
    target.lastEnd = item.end;
  });
  return lanes;
}

function flowTicks(low, high) {
  const step = high - low > 96 ? 24 : 12;
  const ticks = [];
  for (let hour = Math.ceil(low / step) * step; hour <= high; hour += step) {
    ticks.push({ hour, label: hour === 0 ? "now" : hour < 0 ? `${hour}h` : `+${hour}h` });
  }
  return ticks;
}

function flowPercent(hour, schedule) {
  return ((hour - schedule.low) / (schedule.high - schedule.low || 1)) * 100;
}

function flowPlanLiveRun(plan, runs, project) {
  return (runs || []).find(run =>
    run.plan === plan.slug && (!project || !run.project || run.project === project)
  );
}

function DerivedFlow({ runs, project }) {
  const [selectedSprint, setSelectedSprint] = useState(null);
  const payload = window.STATE?.schedule || null;
  const items = Array.isArray(payload?.bars) ? payload.bars : [];
  const low = Number.isFinite(Number(payload?.low)) ? Number(payload.low) : -24;
  const high = Number.isFinite(Number(payload?.high)) ? Number(payload.high) : 24;
  const schedule = payload
    ? { items, lanes: flowPackLanes(items), low, high, ticks: flowTicks(low, high) }
    : null;

  if (!schedule) {
    return (
      <section className="r-derived-flow" aria-label="Dependency-derived crew flow">
        <header className="r-derived-flow-head">
          <span className="r-crew-label">Derived flow</span>
          <span className="r-derived-flow-unavailable">no schedule payload served</span>
        </header>
      </section>
    );
  }

  const sprints = [...new Set(schedule.items.map(item => item.plan.sprint).filter(Boolean))].sort();
  const sprintChips = [{ id: null, label: "All" }, ...sprints.map(id => ({ id, label: id }))];
  const liveByPlan = new Map(
    schedule.items.map(item => [item.plan.slug, flowPlanLiveRun(item.plan, runs, project)])
  );
  const nowLeft = `${flowPercent(0, schedule)}%`;

  return (
    <section className="r-derived-flow" aria-label="Dependency-derived crew flow">
      <header className="r-derived-flow-head">
        <span className="r-crew-label">Derived flow</span>
        <strong>{schedule.items.length} items · {schedule.lanes.length} concurrent session{schedule.lanes.length === 1 ? "" : "s"}</strong>
        <div className="r-derived-flow-key" aria-label="Flow state key">
          <span className="recorded"><i></i>recorded</span>
          <span className="in-flight"><i></i>in flight</span>
          <span className="predicted"><i></i>predicted</span>
          <span className="held"><i></i>held</span>
        </div>
      </header>
      <div className="r-derived-flow-sprints">
        <span className="r-crew-label">Sprint</span>
        {sprintChips.map(chip => (
          <button
            type="button"
            key={chip.id || "all"}
            aria-pressed={selectedSprint === chip.id}
            onClick={() => setSelectedSprint(chip.id)}
          >
            {chip.label}<span>{chip.id ? schedule.items.filter(item => item.plan.sprint === chip.id).length : schedule.items.length}</span>
          </button>
        ))}
      </div>
      <div className="r-derived-flow-scroll">
        <div className="r-derived-flow-stage">
          <div className="r-derived-flow-axis">
            {schedule.ticks.map(tick => (
              <span key={tick.hour} className={tick.hour === 0 ? "now" : ""} style={{ left: `${flowPercent(tick.hour, schedule)}%` }}>{tick.label}</span>
            ))}
            <i className="r-derived-flow-now" style={{ left: nowLeft }}></i>
          </div>
          {schedule.lanes.map((lane, index) => {
            const live = lane.items.map(item => liveByPlan.get(item.plan.slug)).find(Boolean);
            const wallHours = lane.items.reduce((total, item) => total + item.wall_hours, 0);
            return (
              <div className="r-derived-flow-lane" key={index}>
                <div className="r-derived-flow-lane-label">
                  <strong>session {index + 1}</strong>
                  <span>{live ? `live · ${live.role || "worker"}` : `${lane.items.length} item${lane.items.length === 1 ? "" : "s"} · ${wallHours}h`}</span>
                </div>
                <div className="r-derived-flow-track">
                  <i className="r-derived-flow-now" style={{ left: nowLeft }}></i>
                  {lane.items.map(item => {
                    const plan = item.plan;
                    const status = String(plan.status || "pending").toLowerCase();
                    const dimmed = Boolean(selectedSprint && plan.sprint !== selectedSprint);
                    return (
                      <a
                        href={`#plan/${plan.slug}`}
                        key={plan.slug}
                        className={`r-derived-flow-bar ${RECORDED_FLOW_STATUSES.has(status) ? "recorded" : ACTIVE_FLOW_STATUSES.has(status) ? "in-flight" : status === "blocked" ? "held" : "predicted"}`}
                        style={{
                          left: `${flowPercent(item.start, schedule)}%`,
                          width: `${Math.max(0, flowPercent(item.end, schedule) - flowPercent(item.start, schedule))}%`,
                          opacity: dimmed ? 0.28 : 1,
                        }}
                        title={`${plan.title || plan.slug} · ${plan.sprint || "unscheduled"} · ${status} · ${item.wall_hours}h wall-clock`}
                      >
                        <span className="r-derived-flow-sprint-chip">{plan.sprint || "—"}</span>
                        <strong>{plan.title || plan.slug}</strong>
                        <span className="r-derived-flow-hours">{item.wall_hours}h</span>
                      </a>
                    );
                  })}
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </section>
  );
}

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

function CrewView({ visibleProjects, mountedProjectCount, selectedProject, quota }) {
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
  const flowProject = selectedProject || window.STATE?.project || "";
  const flowRuns = crewRunsForVisibleProjects(runs, flowProject ? [flowProject] : []);
  const flowPreview = window.STATE?.schedule || null;
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
          {flowPreview?.lane_count ?? "—"} sessions · polling every {CREW_POLL_INTERVAL_MS / 1000}s
          {` · ${Array.isArray(visibleProjects) ? visibleProjects.length : 0} `}shown / {mountedProjectCount || 0} mounted
          {refreshedAt ? ` · refreshed ${refreshedAt.toLocaleTimeString()}` : ""}
        </span>
      </div>

      {error && <div role="status" className="r-crew-error">{error}</div>}

      <DerivedFlow runs={flowRuns} project={flowProject} />

      <CrewQuotaMeters readings={quota} />

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

function crewFormatStamp(stamp) {
  if (!stamp) return "";
  const parsed = new Date(stamp);
  if (Number.isNaN(parsed.getTime())) return String(stamp);
  return parsed.toISOString().replace(/\.\d{3}Z$/, "Z");
}

function crewClampPercent(value) {
  return Math.max(0, Math.min(100, value));
}

function crewPeriodLabel(minutes) {
  const value = Number(minutes);
  if (!Number.isFinite(value) || value <= 0) return null;
  if (value % 1440 === 0) {
    return `${Math.round(value / 1440)}-day window`;
  }
  if (value % 60 === 0 && value < 1440) {
    return `${Math.round(value / 60)}-hour window`;
  }
  return `${value}-minute window`;
}

function crewQuotaWindow(reading) {
  if (!reading || typeof reading !== "object") return null;
  const used = reading.used_percent ?? reading.utilisation_pct;
  if (typeof used !== "number" || !Number.isFinite(used)) return null;
  const period = reading.rate_limit_period_minutes ?? reading.window_minutes ?? reading.windowDurationMins;
  const periodLabel = crewPeriodLabel(period);
  if (!periodLabel) return null;
  const resetMs = new Date(reading.resets_at).getTime();
  if (!Number.isFinite(resetMs)) return null;
  const windowDurationMs = Number(period) * 60000;
  const windowStartMs = resetMs - windowDurationMs;
  const observedStamp = reading.observed_at || null;
  const projectedStamp = reading.projected_exhaustion_at || null;
  const projectedMs = projectedStamp ? new Date(projectedStamp).getTime() : NaN;
  let projectionPct = null;
  let warned = false;
  if (Number.isFinite(projectedMs)) {
    projectionPct = crewClampPercent(((projectedMs - windowStartMs) / windowDurationMs) * 100);
    warned = projectedMs < resetMs;
  }
  return {
    usedPct: crewClampPercent(used),
    periodLabel,
    resetStamp: reading.resets_at,
    observedStamp,
    projectedStamp,
    projectionPct,
    warned,
  };
}

function crewRateFigures(reading) {
  if (!reading || typeof reading !== "object") return [];
  const rows =
    Array.isArray(reading.rate_windows) && reading.rate_windows.length
      ? reading.rate_windows
      : null;
  const figures = [];
  if (rows) {
    rows.forEach(row => {
      const burn = Number(row?.burn_multiple);
      const periodLabel = crewPeriodLabel(row?.period_minutes ?? row?.window_minutes);
      if (!Number.isFinite(burn) || burn <= 0 || !periodLabel) return;
      figures.push({
        burn,
        periodLabel,
        observedStamp: row.observed_at || reading.observed_at || null,
      });
    });
  } else {
    const burn = Number(reading.burn_multiple);
    const periodLabel = crewQuotaWindow(reading)?.periodLabel || null;
    if (Number.isFinite(burn) && burn > 0 && periodLabel) {
      figures.push({ burn, periodLabel, observedStamp: reading.observed_at || null });
    }
  }
  return figures.map(figure => ({ ...figure, only: figures.length === 1 }));
}

function crewFormatBurn(multiple) {
  const value = Number(multiple);
  if (!Number.isFinite(value)) return "—";
  return `${parseFloat(value.toFixed(2))}×`;
}

function renderCrewQuotaMeter(reading) {
  const quotaWindow = crewQuotaWindow(reading);
  if (!quotaWindow) return null;
  const figures = crewRateFigures(reading);
  const warningText = quotaWindow.warned
    ? "projected exhaustion before the reset"
    : "projection at or after the reset";
  return (
    <article className="r-crew-quota-row" data-backend={reading.backend || ""}>
      <header className="r-crew-quota-row-head">
        <strong>{reading.backend || "backend"}</strong>
        <span>{quotaWindow.periodLabel}</span>
        <span className="r-crew-figure" data-observed-at={quotaWindow.observedStamp || ""}>
          observed {crewFormatStamp(quotaWindow.observedStamp)}
        </span>
      </header>
      <div
        className={`r-crew-meter r-crew-meter--quota${quotaWindow.warned ? " r-crew-meter--warn" : ""}`}
        aria-label={`${quotaWindow.usedPct}% used in ${quotaWindow.periodLabel}; ${warningText}`}
      >
        <i className="r-crew-position" style={{ width: `${quotaWindow.usedPct}%` }}></i>
        {quotaWindow.projectionPct != null && (
          <i
            className="r-crew-projection"
            data-observed-at={quotaWindow.observedStamp || ""}
            style={{ left: `${quotaWindow.projectionPct}%` }}
            title={`projected exhaustion ${crewFormatStamp(quotaWindow.projectedStamp)}`}
          ></i>
        )}
      </div>
      <footer className="r-crew-quota-row-foot">
        <span>reset {crewFormatStamp(quotaWindow.resetStamp)}</span>
        <span className="r-crew-rate-figures">
          {figures.length === 0 ? (
            <span className="r-crew-norate">no rate recorded</span>
          ) : (
            figures.map((figure, index) => (
              <span
                key={`${figure.periodLabel}-${index}`}
                className="r-crew-figure r-crew-figure--rate"
                data-observed-at={figure.observedStamp || ""}
              >
                {crewFormatBurn(figure.burn)} <em>{figure.periodLabel}</em>{" "}
                <span
                  className="r-crew-bound"
                  title="run-level input is an upper bound, not a measurement: a resumed session records its whole accumulated context on every turn, so input over-counts"
                >
                  upper bound · resumed sessions re-read full context each turn
                </span>
                {figure.only && <span className="r-crew-only-window">· only window</span>}
              </span>
            ))
          )}
        </span>
      </footer>
    </article>
  );
}

function CrewQuotaMeter({ reading }) {
  return renderCrewQuotaMeter(reading);
}

function CrewQuotaMeters({ readings }) {
  if (!Array.isArray(readings) || readings.length === 0) return null;
  const meters = readings.map(renderCrewQuotaMeter).filter(Boolean);
  if (meters.length === 0) return null;
  return (
    <section className="r-crew-quota" aria-label="Backend quota meters">
      <header className="r-crew-quota-head">
        <span className="r-crew-label">Lane quota</span>
        <span>one meter per backend reporting a window, the projection beside its reset</span>
      </header>
      <div className="r-crew-quota-list">{meters}</div>
    </section>
  );
}

window.ReckonCrewSchedule = {
  farEnd() {
    return window.STATE?.schedule?.high ?? 0;
  },
};
window.ReckonCrewQuota = { CrewQuotaMeter, CrewQuotaMeters };
window.CrewView = CrewView;
