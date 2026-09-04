// Read-only fleet home rendered inside the Reckon shell.
const { useMemo: useHomeMemo, useState: useHomeState } = React;

const HOME_LANDED_WINDOW_MS = 72 * 60 * 60 * 1000;

function homeVisibleSummary(projects, runs) {
  const moving = (projects || []).filter(project => Number(project.plans_count) > 0);
  return {
    moving: moving.length,
    plans: moving.reduce((total, project) => total + Number(project.plans_count || 0), 0),
    active: moving.reduce((total, project) => total + Number(project.active || 0), 0),
    inFlight: (runs || []).length,
    held: moving.reduce((total, project) => total + Number(project.blocked || 0), 0),
    shipped: moving.reduce((total, project) => total + Number(project.shipped || 0), 0),
  };
}

function homeProjectRows(projects) {
  return (projects || []).filter(project => Number(project.plans_count) > 0)
    .sort((left, right) => String(right.last_edited || "").localeCompare(String(left.last_edited || "")));
}

function homeDormantRows(projects) {
  return (projects || []).filter(project => Number(project.plans_count) === 0);
}

function homeVisibleRuns(runs, projects) {
  const visible = new Set((projects || []).map(project => project.project));
  return (runs || []).filter(run => visible.has(run.project));
}

function homeActivityProjection(series) {
  if (!Array.isArray(series) || series.length === 0) return null;
  const values = series.map(value => Math.max(0, Number(value) || 0));
  const peak = Math.max(1, ...values);
  const line = values.map((value, index) => {
    const x = values.length === 1 ? 180 : (index / (values.length - 1)) * 180;
    const y = 27 - (value / peak) * 24;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  const total = values.reduce((sum, value) => sum + value, 0);
  const recent = values.slice(-3).reduce((sum, value) => sum + value, 0);
  return { line, area: `0,30 ${line} 180,30`, total, recent };
}

function homeJustLanded(projects, now = Date.now()) {
  return (projects || []).flatMap(project => (project.artifacts || []).map(item => ({...item, project: project.project})))
    .filter(item => {
      const kind = item.type || "plan";
      const landed = kind === "evidence" || (kind === "plan" && ["done", "shipped"].includes(item.effective_status || item.status));
      const stamp = new Date(item.edited || item.last || 0).getTime();
      return landed && Number.isFinite(stamp) && stamp >= now - HOME_LANDED_WINDOW_MS && stamp <= now;
    })
    .sort((left, right) => new Date(right.edited || right.last) - new Date(left.edited || left.last))
    .slice(0, 7);
}

function homeRelativeTime(value, now = Date.now()) {
  const stamp = new Date(value || 0).getTime();
  if (!Number.isFinite(stamp) || !value) return "no edits recorded";
  const seconds = Math.max(0, Math.floor((now - stamp) / 1000));
  if (seconds < 60) return "edited just now";
  if (seconds < 3600) return `edited ${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `edited ${Math.floor(seconds / 3600)}h ago`;
  return `edited ${Math.floor(seconds / 86400)}d ago`;
}

function homeBudget(run) {
  const elapsed = Math.max(0, Number(run.elapsed_seconds || 0));
  const total = Math.max(0, Number(run.budget_seconds || run.time_budget_seconds || 0));
  return {
    percent: total ? Math.min(100, Math.round((elapsed / total) * 100)) : 0,
    label: total ? `${Math.round(elapsed / 60)}m / ${Math.round(total / 60)}m` : `${Math.round(elapsed / 60)}m`,
  };
}

function HomeStreak({ series }) {
  const activity = homeActivityProjection(series);
  if (!activity) return <span className="r-home-no-activity">no recorded activity</span>;
  return <span className="r-home-streak">
    <svg viewBox="0 0 180 30" preserveAspectRatio="none" aria-hidden="true">
      <polygon points={activity.area}></polygon><polyline points={activity.line}></polyline>
    </svg>
    <span>{activity.total} edits · {activity.recent} last 3d</span>
  </span>;
}

function HomeStatusBar({ project }) {
  const total = Math.max(1, Number(project.plans_count || 0));
  const buckets = [["shipped", project.shipped], ["active", project.active], ["held", project.blocked], ["pending", project.pending]];
  return <span className="r-home-status-wrap">
    <span className="r-home-status-bar" aria-label={`${project.project} plan status composition`}>
      {buckets.map(([name, value]) => Number(value) > 0 && <i key={name} className={name} style={{width: `${(Number(value) / total) * 100}%`}} title={`${value} ${name}`}></i>)}
    </span>
    <span className="r-home-status-labels"><span>{project.plans_count} plans</span>{Number(project.blocked) > 0 && <span className="held">{project.blocked} held</span>}</span>
  </span>;
}

function FleetHome({ projects, fleetRuns, mountedProjectCount, onConfigureVisibility }) {
  const [dormantOpen, setDormantOpen] = useHomeState(false);
  const rows = useHomeMemo(() => homeProjectRows(projects), [projects]);
  const dormant = useHomeMemo(() => homeDormantRows(projects), [projects]);
  const runs = useHomeMemo(() => homeVisibleRuns(fleetRuns, projects), [fleetRuns, projects]);
  const landed = useHomeMemo(() => homeJustLanded(projects), [projects]);
  const summary = useHomeMemo(() => homeVisibleSummary(projects, runs), [projects, runs]);
  const stats = [["projects moving", summary.moving], ["plans", summary.plans], ["active", summary.active], ["in flight", summary.inFlight], ["held", summary.held], ["shipped", summary.shipped]];
  const today = new Date().toLocaleDateString([], {year: "numeric", month: "short", day: "numeric"});
  const openProject = project => { window.location.href = `/${project}/#plans`; };
  const openArtifact = item => { window.location.href = `/${item.project}/#plan/${encodeURIComponent(item.nav_key || item.slug)}`; };

  return <main className="r-home" aria-label="Fleet home"><div className="r-home-inner">
    <header className="r-home-eyebrow"><span>Fleet · {today}</span><span className="r-home-scope">{projects.length} of {mountedProjectCount} shown</span><button type="button" onClick={onConfigureVisibility}>configure</button></header>
    <section className="r-home-stats" aria-label="Visible fleet totals">
      {stats.map(([label, value]) => <div className="r-home-stat" key={label}><span>{label}</span><strong>{value}</strong></div>)}
    </section>
    <section className="r-home-projects" aria-label="Projects with recorded plans">
      {rows.map(project => <button className="r-home-project" type="button" key={project.project} onClick={() => openProject(project.project)}>
        <span className="r-home-project-id"><span><i className={`r-live-dot ${project.live ? "is-live" : ""}`}></i><strong>{project.project}</strong></span><small>{homeRelativeTime(project.last_edited)}</small></span>
        <HomeStreak series={project.activity30}/><HomeStatusBar project={project}/>
        <span className="r-home-sprint"><strong>{project.active_sprint?.id || "no active sprint"}</strong><small>{project.active_sprint?.theme || ""}</small></span>
      </button>)}
      {dormant.length > 0 && <div className="r-home-dormant">
        <button type="button" onClick={() => setDormantOpen(open => !open)} aria-expanded={dormantOpen}>{dormant.length} visible projects with no recorded work — {dormantOpen ? "hide" : "show"}</button>
        {dormantOpen && <div className="r-home-dormant-chips">{dormant.map(project => <button type="button" key={project.project} onClick={() => openProject(project.project)}>{project.project}</button>)}</div>}
      </div>}
    </section>
    <div className="r-home-lower">
      <section className="r-home-panel r-home-flight" aria-labelledby="home-flight-heading">
        <header><h2 id="home-flight-heading">In flight</h2><span>{runs.length} runs · polling every 3s</span></header>
        {runs.map(run => { const budget = homeBudget(run); return <article key={run.run_id || `${run.project}-${run.plan}`}>
          <div className="r-home-run-title"><i className="r-live-dot is-live"></i><strong>{run.title || run.plan || run.node || "Untitled run"}</strong><span>{run.section || ""}</span><code>{run.project}</code></div>
          <p>{run.current_line || run.gate || run.done_when || "Waiting for worker activity."}</p>
          <div className="r-home-budget"><span><i style={{width: `${budget.percent}%`}}></i></span><code>{budget.label}</code><code>{run.phase || "working"}</code></div>
        </article>; })}
        {runs.length === 0 && <p className="r-home-empty">No runs in flight across the visible set.</p>}
      </section>
      <section className="r-home-panel r-home-landed" aria-labelledby="home-landed-heading">
        <header><h2 id="home-landed-heading">Just landed</h2><span>last 72 hours</span></header>
        {landed.map(item => <button type="button" key={`${item.project}:${item.nav_key || item.slug}`} onClick={() => openArtifact(item)}><span className={`r-home-kind ${item.type || "plan"}`}>{item.type || "plan"}</span><strong>{item.title || item.slug}</strong><time>{homeRelativeTime(item.edited || item.last).replace(/^edited /, "")}</time></button>)}
        {landed.length === 0 && <p className="r-home-empty">Nothing landed in the visible set during the last 72 hours.</p>}
      </section>
    </div>
  </div></main>;
}

window.ReckonShell = window.ReckonShell || {};
window.ReckonShell.home = {HOME_LANDED_WINDOW_MS, homeVisibleSummary, homeProjectRows, homeDormantRows, homeVisibleRuns, homeActivityProjection, homeJustLanded, homeRelativeTime, homeBudget, HomeStreak, HomeStatusBar, FleetHome};
