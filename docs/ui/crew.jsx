const CREW_POLL_INTERVAL_MS = 3000;

const CREW_CARD_STYLES = String.raw`
.r-crew-heading{display:flex;align-items:baseline;gap:12px;margin-bottom:18px}.r-crew-heading h1{margin:2px 0 0;font-size:22px}.r-crew-heading>span,.r-crew-activity{font:11px var(--mono);color:var(--muted)}.r-crew-error{color:var(--bad);margin-bottom:12px}.r-crew-empty{color:var(--muted)}.r-crew-list{display:grid;gap:8px}
.r-crew-card{display:grid;grid-template-columns:minmax(280px,1fr) 150px minmax(180px,220px);gap:16px 22px;align-items:center;padding:13px 15px;border:1px solid var(--line);border-radius:8px;background:var(--card)}.r-crew-card.needs-attention{border-color:var(--bad);background:var(--bad-2)}.r-crew-main{min-width:0}.r-crew-identity,.r-crew-location{display:flex;align-items:baseline;min-width:0;gap:8px}.r-crew-identity strong{font-size:14px;color:var(--ink)}.r-crew-identity code,.r-crew-location code{padding:1px 5px;border:1px solid var(--line-2);border-radius:4px;color:var(--ink-2);font-size:11px}.r-crew-identity>span:not(.r-crew-phase-dot):not(.r-crew-phase){color:var(--muted);font-size:12px}
.r-crew-phase-dot{width:7px;height:7px;flex:none;border-radius:50%;background:var(--accent);box-shadow:0 0 0 3px var(--accent-3)}.r-crew-card.needs-attention .r-crew-phase-dot{background:var(--bad);box-shadow:0 0 0 3px var(--bad-2)}.r-crew-phase{margin-left:auto;color:var(--accent);font:11px var(--mono)}.r-crew-card.needs-attention .r-crew-phase{color:var(--bad)}.r-crew-location{margin-top:5px;font-size:13px}.r-crew-location a:not(.r-crew-sprint),.r-crew-location>span:first-of-type{font-weight:600}.r-crew-location>span:last-of-type{color:var(--accent);font:12px var(--mono)}.r-crew-sprint{margin-left:auto;color:var(--muted);font-size:11px}
.r-crew-label{color:var(--faint);font:10.5px var(--mono);letter-spacing:.06em;text-transform:uppercase}.r-crew-done-when{min-width:0;margin-top:5px}.r-crew-done-when summary{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:7px;align-items:baseline;cursor:pointer;list-style:none}.r-crew-done-when summary::-webkit-details-marker{display:none}.r-crew-contract{display:-webkit-box;overflow:hidden;color:var(--muted);font-size:12px;line-height:1.35;-webkit-box-orient:vertical;-webkit-line-clamp:1}.r-crew-done-when p{margin:8px 0 0;padding:8px 10px;border-left:2px solid var(--line-2);color:var(--ink-2);font-size:12px;line-height:1.45;white-space:pre-wrap}
.r-crew-budget-values,.r-crew-gate-head{display:flex;justify-content:space-between;align-items:baseline;gap:8px}.r-crew-budget-values{margin:4px 0;font:12px var(--mono)}.r-crew-budget-values span{color:var(--muted)}.r-crew-meter{height:4px;overflow:hidden;border-radius:3px;background:var(--bg-3)}.r-crew-meter i{display:block;height:100%;border-radius:inherit;background:var(--accent)}.r-crew-card.needs-attention .r-crew-meter i{background:var(--bad)}.r-crew-activity{margin-top:5px}.r-crew-verdict{color:var(--ink-2);font:10.5px var(--mono);text-transform:uppercase}.r-crew-verdict.passed{color:var(--good)}.r-crew-verdict.failed{color:var(--bad)}.r-crew-gate-marks{display:flex;gap:4px;margin:6px 0}.r-crew-gate-marks i{flex:1;height:5px;border-radius:2px;background:var(--bg-3)}.r-crew-gate-marks i.measured{background:var(--accent-2)}.r-crew-current-gate{overflow:hidden;color:var(--ink-2);font-size:12px;line-height:1.35;text-overflow:ellipsis;white-space:nowrap}.r-crew-current-gate span{color:var(--muted)}
.r-crew-connect{grid-column:1/-1;border-top:1px solid var(--line);padding-top:8px}.r-crew-connect>summary{cursor:pointer;color:var(--muted);font:11px var(--mono)}.r-crew-connect-grid{display:flex;gap:24px;margin-top:10px}.r-crew-connect-grid>span{display:flex;align-items:baseline;gap:7px}.r-crew-attach{display:flex;align-items:center;gap:10px;margin-top:8px;padding:7px 9px;border:1px solid var(--line);border-radius:5px;background:var(--bg-2)}.r-crew-attach code{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.r-crew-attach button{flex:none;margin-left:auto}
@media(max-width:920px){.r-crew-card{grid-template-columns:minmax(0,1fr) minmax(180px,220px)}.r-crew-main{grid-column:1/-1}}@media(max-width:620px){.r-crew-heading{align-items:flex-start;flex-direction:column}.r-crew-card{grid-template-columns:1fr}.r-crew-main,.r-crew-connect{grid-column:1}.r-crew-connect-grid{align-items:flex-start;flex-direction:column;gap:5px}}
`;

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

function crewCardProjection(run) {
  const elapsed = Number(run.elapsed_seconds);
  const budgetSeconds = crewDurationSeconds(run.budget_seconds ?? run.time_budget);
  const budgetRatio = budgetSeconds && Number.isFinite(elapsed)
    ? Math.min(1, Math.max(0, elapsed / budgetSeconds))
    : 0;
  return {
    identity: run.member || run.run_id || "unassigned",
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
  const card = crewCardProjection(run);
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
          <code>{run.model || run.backend || "—"}</code>
          <span>{run.role || "—"}{run.effort ? ` · ${run.effort}` : ""}</span>
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

function CrewView() {
  const [runs, setRuns] = useState([]);
  const [error, setError] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [refreshedAt, setRefreshedAt] = useState(null);

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

  return (
    <div className="r-page wide">
      <style>{CREW_CARD_STYLES}</style>
      <div className="r-crew-heading">
        <div>
          <div className="r-eyebrow">Crew</div>
          <h1>Live runs</h1>
        </div>
        <span>
          {runs.length} visible · polls every {CREW_POLL_INTERVAL_MS / 1000}s
          {refreshedAt ? ` · refreshed ${refreshedAt.toLocaleTimeString()}` : ""}
        </span>
      </div>

      {error && <div role="status" className="r-crew-error">{error}</div>}

      {!loaded ? (
        <div className="r-crew-empty">Loading live runs…</div>
      ) : runs.length === 0 ? (
        <div className="r-crew-empty">No live runs.</div>
      ) : (
        <div className="r-crew-list" aria-label="Live crew runs">
          {runs.map(run => <CrewRunCard key={`${run.project || ""}:${run.run_id}`} run={run} />)}
        </div>
      )}
    </div>
  );
}

window.CrewView = CrewView;
