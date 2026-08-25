const SPRINT_HORIZONS = {
  "1hr": { milliseconds: 60 * 60 * 1000, subDay: true },
  "1D": { milliseconds: 24 * 60 * 60 * 1000, subDay: true },
  "4w": { days: 28 },
  "8w": { days: 56 },
  "6m": { days: 183 },
  all: {},
};

const CLOSED_ITEM_STATUSES = new Set(["shipped", "done", "superseded", "abandoned", "historical"]);

function sprintInventoryItems(sprint, inventory) {
  return (sprint.items || []).map(item => {
    const slug = typeof item === "string" ? item : item.slug;
    const plan = (inventory || []).find(row => row.slug === slug);
    if (!plan) return null;
    return {
      ...plan,
      whyNow: typeof item === "object" ? item.why_now : null,
      doneWhen: typeof item === "object" ? item.done_when : null,
    };
  }).filter(Boolean);
}

function sprintOpenCount(sprint, inventory) {
  return sprintInventoryItems(sprint, inventory)
    .filter(plan => !CLOSED_ITEM_STATUSES.has(plan.effective_status || plan.status)).length;
}

function sprintOverviewRows(sprints, inventory, foldClosed) {
  const rows = (sprints || []).map(sprint => ({
    sprint,
    openCount: sprintOpenCount(sprint, inventory),
  }));
  const folded = foldClosed ? rows.filter(row => row.openCount === 0) : [];
  return {
    visible: foldClosed ? rows.filter(row => row.openCount > 0) : rows,
    folded,
    foldedCount: folded.length,
  };
}

function parseSprintDate(value) {
  if (!value) return null;
  const date = new Date(`${value}T00:00:00Z`);
  return Number.isNaN(date.getTime()) ? null : date;
}

function completedRunTime(run) {
  for (const field of ["completed_at", "dispatched_at"]) {
    const timestamp = Date.parse(run?.[field] || "");
    if (!Number.isNaN(timestamp)) return timestamp;
  }
  return null;
}

function sprintCompletedRuns(sprint, runsByPlan) {
  const memberPlans = new Set((sprint?.items || []).map(item => typeof item === "string" ? item : item.slug));
  return Object.entries(runsByPlan || {})
    .filter(([slug]) => memberPlans.has(slug))
    .flatMap(([, runs]) => Array.isArray(runs) ? runs : [])
    .sort((left, right) => (completedRunTime(right) || 0) - (completedRunTime(left) || 0));
}

function sprintAxis(sprints, horizon, todayValue, completedRuns = []) {
  const starts = (sprints || []).map(row => parseSprintDate(row.starts)).filter(Boolean);
  const ends = (sprints || []).map(row => parseSprintDate(row.ends)).filter(Boolean);
  const today = parseSprintDate(todayValue) || new Date();
  const setting = SPRINT_HORIZONS[horizon] || SPRINT_HORIZONS["8w"];
  const recordedTimes = completedRuns.map(completedRunTime).filter(value => value !== null);
  const latestRecorded = recordedTimes.length ? Math.max(...recordedTimes) : today.getTime();
  let start;
  let end;
  if (setting.subDay) {
    end = new Date(latestRecorded);
    start = new Date(end.getTime() - setting.milliseconds);
  } else if (setting.days) {
    start = new Date(today.getTime() - 7 * 86400000);
    end = new Date(start.getTime() + setting.days * 86400000);
  } else {
    start = new Date(Math.min(...starts.map(date => date.getTime()), today.getTime()));
    end = new Date(Math.max(...ends.map(date => date.getTime()), today.getTime()));
  }
  if (end <= start) end = new Date(start.getTime() + 86400000);
  const span = end - start;
  const position = sprint => {
    const itemStart = parseSprintDate(sprint.starts) || start;
    const itemEnd = parseSprintDate(sprint.ends) || itemStart;
    const left = Math.max(0, Math.min(100, ((itemStart - start) / span) * 100));
    const right = Math.max(left + 1.5, Math.min(100, ((itemEnd - start) / span) * 100));
    return { left, width: Math.max(1.5, right - left) };
  };
  const timestampPosition = run => {
    const timestamp = completedRunTime(run);
    if (timestamp === null || timestamp < start.getTime() || timestamp > end.getTime()) return null;
    return Math.max(0, Math.min(100, ((timestamp - start) / span) * 100));
  };
  const tickCount = 6;
  const ticks = Array.from({ length: tickCount }, (_, index) => {
    const date = new Date(start.getTime() + span * index / (tickCount - 1));
    const format = setting.subDay
      ? { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "UTC" }
      : { month: "short", day: "numeric", timeZone: "UTC" };
    return { left: index * 100 / (tickCount - 1), label: date.toLocaleString(undefined, format) };
  });
  return { position, timestampPosition, ticks, start: start.toISOString(), end: end.toISOString(), subDay: Boolean(setting.subDay) };
}

function openGateCount(plan) {
  return (plan.gates || []).filter(gate => !(gate.passed || gate.verdict === "passed")).length;
}

function planFlag(plan, liveRuns) {
  const authored = plan.workflow_status || plan.status || "pending";
  const effective = plan.effective_status || authored;
  const gates = openGateCount(plan);
  if (authored !== effective) return `${authored} → ${effective} · ${gates} open ${gates === 1 ? "gate" : "gates"}`;
  if (liveRuns.length) return `in flight · ${liveRuns.length}`;
  return effective;
}

function Sprint({ sprintId, onNav }) {
  const M = window.STATE;
  if (!M) return null;
  const allSprints = M.sprints || [];
  const idx = useMemo(() => {
    const requested = allSprints.findIndex(sprint => sprint.id === sprintId);
    return requested >= 0 ? requested : allSprints.findIndex(sprint => sprint.id === M.active_sprint_id);
  }, [sprintId, allSprints]);
  const sprint = allSprints[idx];
  if (!sprint) return <div className="r-page">No sprint.</div>;

  const [surface, setSurface] = useState("overview");
  const [horizon, setHorizon] = useState("8w");
  const [foldClosed, setFoldClosed] = useState(true);
  const [showSprintPrompt, setShowSprintPrompt] = useState(false);
  const [sprintPromptText, setSprintPromptText] = useState(null);
  const [liveRuns, setLiveRuns] = useState([]);
  const [finishedRunsByPlan, setFinishedRunsByPlan] = useState({});
  const [finishedRunsState, setFinishedRunsState] = useState("loading");
  const project = M.project || document.querySelector('meta[name="docs-project"]')?.content || "";
  const items = sprintInventoryItems(sprint, M.inventory);
  const sprintPlanSlugs = useMemo(
    () => (sprint.items || []).map(item => typeof item === "string" ? item : item.slug).filter(Boolean),
    [sprint]
  );
  const sprintPlanKey = sprintPlanSlugs.join("\u0000");

  useEffect(() => {
    if (!project) { setLiveRuns([]); return; }
    let active = true;
    const poll = async () => {
      try {
        const response = await fetch(`/crew/${encodeURIComponent(project)}`, { cache: "no-store" });
        if (!response.ok) return;
        const payload = await response.json();
        if (active && Array.isArray(payload.runs)) setLiveRuns(payload.runs);
      } catch (_) { /* Navigation remains available without live run state. */ }
    };
    poll();
    const timer = window.setInterval(poll, 3000);
    return () => { active = false; window.clearInterval(timer); };
  }, [project]);

  useEffect(() => {
    if (!project) { setFinishedRunsByPlan({}); setFinishedRunsState("empty"); return; }
    let active = true;
    setFinishedRunsState("loading");
    setFinishedRunsByPlan({});
    Promise.all(sprintPlanSlugs.map(async slug => {
      const response = await fetch(`/crew/${encodeURIComponent(project)}/finished/${encodeURIComponent(slug)}`, { cache: "no-store" });
      if (!response.ok) throw new Error(`Finished work request failed with ${response.status}`);
      const payload = await response.json();
      return [slug, Array.isArray(payload.runs) ? payload.runs : []];
    })).then(entries => {
      if (!active) return;
      const records = Object.fromEntries(entries);
      setFinishedRunsByPlan(records);
      setFinishedRunsState(sprintCompletedRuns(sprint, records).length ? "ready" : "empty");
    }).catch(() => {
      if (active) setFinishedRunsState("error");
    });
    return () => { active = false; };
  }, [project, sprint.id, sprintPlanKey]);

  useEffect(() => {
    if (!showSprintPrompt) { setSprintPromptText(null); return; }
    let alive = true;
    const windowLabel = (sprint.starts || "") + (sprint.ends ? ` → ${sprint.ends}` : "");
    const options = { sprint: { id: sprint.id, window: windowLabel } };
    Promise.resolve(window.buildFleetPromptAsync
      ? window.buildFleetPromptAsync(items, window.STATE, sprint.theme, options)
      : window.buildFleetPrompt(items, window.STATE, sprint.theme, options)
    ).then(text => { if (alive) setSprintPromptText(text); });
    return () => { alive = false; };
  }, [showSprintPrompt, sprint.id]);

  const groupedRuns = useMemo(() => {
    const groups = {};
    liveRuns.forEach(run => { if (run.plan) (groups[run.plan] ||= []).push(run); });
    return groups;
  }, [liveRuns]);
  const columns = useMemo(() => {
    const groups = { todo: [], doing: [], done: [] };
    const columnFor = { pending: "todo", draft: "todo", planned: "todo", active: "doing", blocked: "doing", in_progress: "doing", shipped: "done", done: "done" };
    items.forEach(plan => groups[columnFor[plan.effective_status || plan.status] || "doing"].push(plan));
    return groups;
  }, [items]);
  const decisions = items.flatMap(plan => {
    const rows = Array.isArray(plan.decisions) ? plan.decisions.filter(decision => !decision.choice) : [];
    if (rows.length) return rows.map(decision => ({ plan, label: decision.question || decision.key || "Open decision" }));
    return Array.from({ length: plan.dec_open || 0 }, (_, index) => ({ plan, label: `Open decision ${index + 1}` }));
  });
  const overview = sprintOverviewRows(allSprints, M.inventory, foldClosed);
  const finishedRuns = useMemo(() => sprintCompletedRuns(sprint, finishedRunsByPlan), [sprint, finishedRunsByPlan]);
  const axis = sprintAxis(allSprints, horizon, M.today, finishedRuns);
  const activeIds = new Set((M.active_sprints || allSprints.filter(row => row.status === "active")).map(row => typeof row === "string" ? row : row.id));

  const navigateSprint = (event, id) => {
    if (!onNav) return;
    event.preventDefault();
    onNav({ view: "sprint", sprint: id });
  };

  const renderCard = plan => {
    const runs = groupedRuns[plan.slug] || [];
    const flag = planFlag(plan, runs);
    const percent = Math.round((plan.impl || 0) * 100);
    return (
      <article key={plan.slug} className={`r-kcard ${(plan.effective_status || plan.status) === "blocked" ? "blocked" : ""}`}>
        <a className="r-card-title" href={`#plan/${plan.slug}`}>{plan.title}</a>
        <p className="r-card-description" title={plan.summary || plan.description || "No description supplied"}>{plan.summary || plan.description || "No description supplied"}</p>
        <a className="r-card-progress" href={`#plan/${plan.slug}`} aria-label={`${plan.title}: ${percent}% complete`} title={`Open ${plan.title}, ${percent}% complete`}>
          <span className="bar"><i style={{ width: `${percent}%` }}></i></span><span>{percent}%</span>
        </a>
        <a className="r-card-flag" href={`#plan/${plan.slug}`} aria-label={`${plan.title}: ${flag}`} title={`Open ${plan.title}: ${flag}`}>{flag}</a>
        <details className="r-card-contract">
          <summary>Contract</summary>
          <dl><dt>Why now</dt><dd>{plan.whyNow || "Not supplied"}</dd><dt>Done when</dt><dd>{plan.doneWhen || "Not supplied"}</dd></dl>
        </details>
      </article>
    );
  };

  return (
    <div className="r-page wide r-sprint-surface">
      <header className="r-sp-head">
        <div><div className="r-eyebrow">Sprints</div><h1>All sprints</h1></div>
        <div className="r-sprint-tabs" role="tablist" aria-label="Sprint views">
          <button role="tab" aria-selected={surface === "overview"} onClick={() => setSurface("overview")}>Overview</button>
          <button role="tab" aria-selected={surface === "board"} onClick={() => setSurface("board")}>Board</button>
        </div>
      </header>

      {surface === "overview" ? (
        <section className="r-sprint-overview" aria-label="Sprint timeline overview">
          <div className="r-overview-controls">
            <div className="r-horizon" aria-label="Timeline horizon">{Object.keys(SPRINT_HORIZONS).map(value => <button key={value} aria-pressed={horizon === value} onClick={() => setHorizon(value)}>{value}</button>)}</div>
            <label><input type="checkbox" checked={foldClosed} onChange={event => setFoldClosed(event.target.checked)} /> Fold sprints with nothing open</label>
          </div>
          <div className="r-time-axis" aria-hidden="true"><span></span><div>{axis.ticks.map(tick => <span key={tick.left}>{tick.label}</span>)}</div></div>
          {overview.foldedCount > 0 && <div className="r-folded-band"><div className="r-folded-summary"><strong>{overview.foldedCount}</strong><span>{overview.foldedCount === 1 ? "sprint" : "sprints"} with nothing open</span></div><div className="r-folded-track" aria-hidden="true"><i></i></div></div>}
          <div className="r-timeline-rows">
            {overview.visible.map(({ sprint: row, openCount }) => {
              const geometry = axis.position(row);
              const isActive = activeIds.has(row.id);
              const focus = row.id === M.active_sprint_id;
              const label = `${row.id}, ${row.status}, ${openCount} open item${openCount === 1 ? "" : "s"}${focus ? ", legacy focus" : ""}`;
              return <div className="r-timeline-row" key={row.id}>
                <a href={`#sprint/${row.id}`} onClick={event => navigateSprint(event, row.id)} title={`Open ${label}`} aria-label={`Open ${label}`}><strong>{row.id}</strong><span className="r-sprint-title">{row.theme || row.summary}</span>{isActive && <em>active</em>}{focus && <em className="focus">legacy focus</em>}</a>
                <div className="r-timeline-track">{axis.subDay ? (
                  row.id === sprint.id && finishedRuns.map(run => {
                    const left = axis.timestampPosition(run);
                    return left === null ? null : <i className={`r-completed-run-mark ${run.gate || "unknown"}`} key={run.run_id} style={{ left: `${left}%` }} title={`${run.node || run.plan} completed ${run.completed_at || run.dispatched_at}`} />;
                  })
                ) : <a className={`r-sprint-mark ${row.status}`} href={`#sprint/${row.id}`} onClick={event => navigateSprint(event, row.id)} style={{ left: `${geometry.left}%`, width: `${geometry.width}%` }} title={`Open ${label}`} aria-label={`Open ${label}`}><span className="r-sprint-mark-label">{row.id}</span></a>}</div>
              </div>;
            })}
          </div>
          <div className="r-sprint-legend" aria-label="Timeline legend"><span><i className="active"></i> Active</span><span><i className="planned"></i> Planned</span><span><i className="shipped"></i> Shipped</span><span><b>legacy focus</b> Stored board focus</span></div>
          <section className="r-completed-work" aria-live="polite" aria-label={`${sprint.id} completed work`}>
            <header><div><span className="r-eyebrow">Recorded work</span><h2>{sprint.id} · {sprint.theme || sprint.summary}</h2></div><span>{finishedRuns.length} completed {finishedRuns.length === 1 ? "run" : "runs"}</span></header>
            {finishedRunsState === "loading" && <p className="r-completed-work-state">Loading completed work…</p>}
            {finishedRunsState === "error" && <p className="r-completed-work-state bad">Completed work could not be loaded.</p>}
            {finishedRunsState === "empty" && <p className="r-completed-work-state">No completed work is recorded for this sprint.</p>}
            {finishedRunsState === "ready" && <ol>{finishedRuns.map(run => <li key={run.run_id}>
              <div className="r-completed-work-primary"><strong>{run.node || run.plan}</strong><span className={`r-run-verdict ${run.gate || "unknown"}`}>{run.gate || "not recorded"}</span></div>
              <div className="r-completed-work-stamps"><time dateTime={run.dispatched_at}>dispatched {run.dispatched_at || "not recorded"}</time><time dateTime={run.completed_at}>completed {run.completed_at || "not recorded"}</time></div>
              <div className="r-completed-work-meta"><a href={`#plan/${run.plan}`}>{run.plan}</a><span>{run.section || "unsectioned"}</span><code>{(run.commits || [])[0] || "no commit"}</code></div>
            </li>)}</ol>}
          </section>
        </section>
      ) : (
        <section className="r-sprint-board" aria-label={`${sprint.id} board`}>
          <div className="r-sp-switcher">
            <button className="nav-btn" aria-label="Previous sprint" disabled={idx <= 0} onClick={() => onNav({ view: "sprint", sprint: allSprints[idx - 1].id })}>←</button>
            <div className="current"><span className="id">{sprint.id}</span><span className={`st ${sprint.status}`}>{sprint.status}</span></div>
            <button className="nav-btn" aria-label="Next sprint" disabled={idx >= allSprints.length - 1} onClick={() => onNav({ view: "sprint", sprint: allSprints[idx + 1].id })}>→</button>
            <span className="range">{sprint.starts} → {sprint.ends}</span>
            <button className="gen-prompt" disabled={decisions.length > 0} title={decisions.length ? "Resolve this sprint's open decisions before dispatch" : "Generate fleet prompt for this sprint"} onClick={() => setShowSprintPrompt(true)}>Generate prompt</button>
          </div>
          <div className="r-sp-goal"><div className="lbl">Goal</div><div className="theme">{sprint.theme}</div>{sprint.summary && <div className="summary">{sprint.summary}</div>}</div>
          {decisions.length > 0 && <aside className="r-needs-you"><h2>Needs you <span>{decisions.length}</span></h2><ul>{decisions.map((decision, index) => <li key={`${decision.plan.slug}-${index}`}><a href={`#plan/${decision.plan.slug}`} title={`Open ${decision.plan.title}`}>{decision.label}</a><span>{decision.plan.title}</span></li>)}</ul></aside>}
          <div className="r-kanban">{[
            ["todo", "To do"], ["doing", "Doing"], ["done", "Done"],
          ].map(([id, title]) => <div className="r-col" key={id}><div className="col-h"><span>{title}</span><span className="n">{columns[id].length}</span></div>{columns[id].map(renderCard)}{columns[id].length === 0 && <div className="r-empty-column">No items</div>}</div>)}</div>
        </section>
      )}

      {showSprintPrompt && window.reckon?.PromptModal && sprintPromptText != null && <window.reckon.PromptModal planSlug={`sprint-${sprint.id}`} initialPrompt={sprintPromptText} onClose={() => setShowSprintPrompt(false)} />}
    </div>
  );
}

const SprintView = Sprint;
window.Sprint = Sprint;
window.SprintView = SprintView;
