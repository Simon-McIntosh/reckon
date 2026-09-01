// Reckon shell overview module.
const { useCallback, useEffect, useMemo, useRef, useState } = React;

// Leaner cockpit body — plan list is in column 2 so cockpit is project-only.
function blockerIsUnresolved(blocker) {
  const state = String(blocker.status || "").toLowerCase();
  const summary = String(blocker.summary || "").trim().toLowerCase();
  const next = String(blocker.next || "").trim().toLowerCase();
  return blocker.resolved !== true
    && !blocker.resolved_at
    && !["resolved", "done", "closed"].includes(state)
    && !summary.startsWith("resolved:")
    && !next.startsWith("resolved ");
}

function projectActiveSprints(state) {
  const active = Array.isArray(state.active_sprints) && state.active_sprints.length
    ? state.active_sprints
    : (state.sprints || []).filter(sprint => sprint.status === "active");
  const focus = state.active_sprint_id || null;
  const conflict = state.active_sprint_conflict ?? (
    active.length === 0 ? focus !== null : active.length !== 1 || active[0].id !== focus
  );
  return { active, focus, conflict };
}

function blockerGatedPlans(state, blocker) {
  if (Array.isArray(blocker.gated_plans)) return blocker.gated_plans;
  return [...new Set((state.sprints || []).flatMap(sprint =>
    (sprint.items || [])
      .filter(item => (item.blocked_by || []).includes(blocker.id))
      .map(item => item.slug)
      .filter(Boolean)
  ))].sort();
}

function overviewProjectRows(projects, currentState, fleetRuns, includeCurrent = true) {
  const currentProject = currentState?.project || null;
  const sources = (projects || []).map(project => ({
    ...project,
    state: project.project === currentProject ? currentState : (project.state || {}),
  }));
  if (includeCurrent && currentProject && !sources.some(project => project.project === currentProject)) {
    sources.unshift({ project: currentProject, state: currentState });
  }
  return sources.map(project => {
    const state = project.state || {};
    const rawInventory = Array.isArray(state.inventory)
      ? state.inventory
      : (Array.isArray(state.plans) ? state.plans : []);
    const inventory = rawInventory.filter(item => (item.type || "plan") === "plan");
    const sprintState = projectActiveSprints(state);
    const blockers = (state.blockers || [])
      .filter(blockerIsUnresolved)
      .map(blocker => ({ ...blocker, gated_plans: blockerGatedPlans(state, blocker) }));
    return {
      project: project.project,
      plans: Number(project.plans_count ?? inventory.length),
      activePlans: inventory.filter(plan => ["active", "in-progress"].includes(plan.effective_status || plan.status)).length,
      live: (fleetRuns || []).filter(run => run.project === project.project).length,
      held: inventory.reduce((count, plan) => count + Number(plan.dec_open || 0), 0),
      blockers,
      ...sprintState,
    };
  });
}

function overviewBlockerScopes(rows) {
  const scopes = new Map();
  for (const row of rows) {
    for (const blocker of row.blockers) {
      for (const slug of blocker.gated_plans || []) {
        const key = `${row.project}:${slug}`;
        scopes.set(key, { key, project: row.project, slug });
      }
    }
  }
  return [...scopes.values()].sort((left, right) =>
    left.project.localeCompare(right.project) || left.slug.localeCompare(right.slug)
  );
}

function blockersForPlanScope(rows, scope) {
  const [project, ...slugParts] = String(scope || "").split(":");
  const slug = slugParts.join(":");
  if (!project || !slug) return [];
  const row = rows.find(candidate => candidate.project === project);
  return (row?.blockers || [])
    .filter(blocker => (blocker.gated_plans || []).includes(slug))
    .map(blocker => ({ ...blocker, project }));
}

function overviewOptionalSections(state) {
  const project = state?.projects?.[0]
    || { project: state?.project || "", milestones: state?.milestones || [] };
  return {
    northStars: state?.north_stars || [],
    milestones: project.milestones || [],
  };
}

function OverviewFleet({ projects, fleetRuns, mountedProjectCount }) {
  const rows = overviewProjectRows(projects, window.STATE, fleetRuns, false);
  const blockerScopes = overviewBlockerScopes(rows);
  const [blockerScope, setBlockerScope] = useState(() => blockerScopes[0]?.key || "");
  const effectiveBlockerScope = blockerScopes.some(scope => scope.key === blockerScope)
    ? blockerScope
    : (blockerScopes[0]?.key || "");
  const blockers = blockersForPlanScope(rows, effectiveBlockerScope);
  const totals = [
    { label: "projects", value: rows.length, note: `${rows.length} shown / ${mountedProjectCount} mounted` },
    { label: "plans", value: rows.reduce((count, row) => count + row.plans, 0), note: "in view" },
    { label: "live", value: rows.reduce((count, row) => count + row.live, 0), note: "runs" },
    { label: "held", value: rows.reduce((count, row) => count + row.held, 0), note: "decisions" },
  ];
  return (
    <section className="r-overview-fleet" aria-labelledby="fleet-overview-heading">
      <div className="r-ck-h"><span className="r-eyebrow" id="fleet-overview-heading">Fleet</span></div>
      <div className="r-overview-stats">
        {totals.map(metric => (
          <div className="r-overview-stat" key={metric.label}>
            <div className="r-overview-stat-label">{metric.label}</div>
            <div className="r-overview-stat-value"><strong>{metric.value}</strong><span>{metric.note}</span></div>
          </div>
        ))}
      </div>

      {blockerScopes.length > 0 && (
        <section className="r-overview-blockers" aria-labelledby="overview-blockers-heading">
          <div className="r-overview-blocker-head">
            <h2 id="overview-blockers-heading">Unresolved blockers</h2>
            <label>
              <span>Plan scope</span>
              <select aria-label="Plan scope for unresolved blockers" value={effectiveBlockerScope} onChange={event => setBlockerScope(event.target.value)}>
                {blockerScopes.map(scope => (
                  <option key={scope.key} value={scope.key}>{scope.project} / {scope.slug}</option>
                ))}
              </select>
            </label>
          </div>
          <div className="r-overview-blocker-list">
            {blockers.map(blocker => (
              <article key={`${blocker.project}:${blocker.id}`}>
                <div className="r-overview-blocker-meta">
                  <span className="r-overview-blocker-id">{blocker.id}</span>
                  <span className="r-overview-blocker-project">{blocker.project}</span>
                  <span>{Number(blocker.n || 0)} gated</span>
                  <span className="r-overview-blocker-owner">Owner: {blocker.owner || "unassigned"}</span>
                </div>
                <div className="r-overview-blocker-summary">{blocker.summary || blocker.id}</div>
                <div className="r-overview-blocker-next"><span>Next</span>{blocker.next || "No next action recorded"}</div>
              </article>
            ))}
            {blockers.length === 0 && <p className="r-overview-none">No unresolved blockers gate this plan.</p>}
          </div>
        </section>
      )}

      <section className="r-overview-project-rollup" aria-labelledby="overview-projects-heading">
        <h2 id="overview-projects-heading">Projects</h2>
        <div className="r-overview-projects" role="table" aria-label="Project roll-up">
        <div className="r-overview-project-head" role="row">
          <span>Project</span><span>Active sprints</span><span>Plans</span><span>Active</span><span>Live</span><span>Held</span>
        </div>
        {rows.map(row => (
          <div className="r-overview-project-row" role="row" key={row.project}>
            <strong role="cell">{row.project}</strong>
            <div role="cell" className="r-overview-sprints">
              {row.active.map(sprint => (
                <a key={sprint.id} href={`#sprint/${sprint.id}`}>
                  <span>{sprint.id} · {sprint.theme}</span>
                  {sprint.id === row.focus && <em>legacy focus</em>}
                </a>
              ))}
              {row.active.length === 0 && <span className="r-overview-none">No active sprint</span>}
              {row.conflict && (
                <div className="r-overview-conflict" role="alert">
                  Active sprint resources disagree with legacy focus {row.focus || "none"}.
                  {[...new Set([...row.active.map(sprint => sprint.id), row.focus].filter(Boolean))].map(id => (
                    <a key={id} href={`#sprint/${id}`}>{id}</a>
                  ))}
                </div>
              )}
            </div>
            <span role="cell">{row.plans}</span>
            <span role="cell">{row.activePlans}</span>
            <span role="cell">{row.live}</span>
            <span role="cell">{row.held}</span>
          </div>
        ))}
        </div>
      </section>
    </section>
  );
}

function CockpitBody({ onNav, projects, fleetRuns, mountedProjectCount }) {
  const M = window.STATE;
  if (!M) return null;
  const { milestones, northStars } = overviewOptionalSections(M);

  const decisionPlans = M.inventory
    .filter(i => (i.dec_open || 0) > 0)
    .sort((a, b) => (b.dec_open || 0) - (a.dec_open || 0));
  const decisionTotal = decisionPlans.reduce((n, p) => n + (p.dec_open || 0), 0);
  const liveStatuses = new Set(["pending", "active", "in-progress", "blocked"]);
  const planSlugsWithDecs = new Set(decisionPlans.map(p => p.slug));
  const decsByMs = {};
  for (const p of decisionPlans) decsByMs[p.ms] = (decsByMs[p.ms] || 0) + (p.dec_open || 0);
  const decsByRoi = { high: 0, mid: 0, low: 0 };
  for (const p of decisionPlans) decsByRoi[p.roi || "mid"] = (decsByRoi[p.roi || "mid"] || 0) + (p.dec_open || 0);

  return (
    <>
      <OverviewFleet projects={projects} fleetRuns={fleetRuns} mountedProjectCount={mountedProjectCount} />
      {northStars.length > 0 && (
        <>
          <div className="r-ck-h">
            <span className="r-eyebrow">North stars</span>
          </div>
          <table aria-label="North stars" style={{ width: "100%", borderCollapse: "collapse", marginBottom: 22 }}>
            <tbody>
              {northStars.map(direction => {
                const liveCount = M.inventory.filter(plan => plan.type === "plan" && plan.north_star === direction.id && liveStatuses.has(plan.status)).length;
                return (
                  <tr key={direction.id} style={{ borderBottom: "1px solid var(--line)" }}>
                    <th scope="row" style={{ padding: "9px 12px 9px 0", textAlign: "left", verticalAlign: "top", width: "24%" }}>
                      {direction.href ? <a href={direction.href}>{direction.name}</a> : direction.name}
                    </th>
                    <td style={{ padding: "9px 12px", color: "var(--ink-2)" }}>{direction.statement}</td>
                    <td style={{ padding: "9px 0 9px 12px", textAlign: "right", whiteSpace: "nowrap", fontFamily: "var(--mono)" }}>{liveCount} live</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </>
      )}

      {milestones.length > 0 && (
        <>
          <div className="r-ck-h">
            <span className="r-eyebrow">Milestones</span>
          </div>
          <div className="r-ms" style={{ marginBottom: 4 }}>
            {milestones.map(m => (
              <button key={m.id} className={`r-ms-tile ${m.status}`}
                onClick={() => {
                  // Apply milestone filter, navigate to plans view (first active plan in ms).
                  const _proj = window.STATE?.project || "default";
                  try { localStorage.setItem(`reckon:${_proj}:filters`, JSON.stringify({ ms: [m.id] })); } catch {}
                  window.dispatchEvent(new CustomEvent("reckon:set-filters", { detail: { ms: [m.id] } }));
                  const target = M.inventory.find(i => i.ms === m.id && i.status === "active")
                    || M.inventory.find(i => i.ms === m.id);
                  if (target) onNav({ view: "plan", slug: target.slug });
                }}>
                <div className="fill" style={{ "--w": `${m.pct}%` }}></div>
                <div className="lbl">{m.id} · <span className={`stat-${m.status}`}>{m.status}</span></div>
                <div className="nm">{m.name}</div>
                <div className="pct">{m.pct}%</div>
              </button>
            ))}
          </div>
        </>
      )}

      <div className="r-ck-h">
        <span className="r-eyebrow">Decisions</span>
      </div>
      {decisionTotal === 0 ? (
        <div className="r-ck-empty">No open decisions.</div>
      ) : (
        <div className="r-ck-stats">
          <div className="r-stat">
            <div className="r-stat-num">{decisionTotal}</div>
            <div className="r-stat-lbl">open</div>
          </div>
          <div className="r-stat">
            <div className="r-stat-num">{decisionPlans.length}</div>
            <div className="r-stat-lbl">plans affected</div>
          </div>
          <div className="r-stat r-stat-breakdown">
            <div className="r-stat-lbl">by milestone</div>
            <div className="r-stat-bars">
              {Object.entries(decsByMs).sort().map(([ms, n]) => (
                <div key={ms} className="r-stat-bar">
                  <span className="r-stat-bar-k">{ms}</span>
                  <span className="r-stat-bar-v"><i style={{ width: `${(n / decisionTotal) * 100}%` }}></i></span>
                  <span className="r-stat-bar-n">{n}</span>
                </div>
              ))}
            </div>
          </div>
          <div className="r-stat r-stat-breakdown">
            <div className="r-stat-lbl">by ROI</div>
            <div className="r-stat-bars">
              {["high", "mid", "low"].filter(k => decsByRoi[k] > 0).map(k => (
                <div key={k} className="r-stat-bar">
                  <span className="r-stat-bar-k">{k}</span>
                  <span className="r-stat-bar-v"><i className={`roi-${k}`} style={{ width: `${(decsByRoi[k] / decisionTotal) * 100}%` }}></i></span>
                  <span className="r-stat-bar-n">{decsByRoi[k]}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
      {decisionTotal > 0 && (
        <div className="r-ck-list">
          {decisionPlans.map(p => (
            <a key={p.slug} className="r-ck-row" href={`#plan/${p.slug}`}>
              <span className="r-ck-num">{p.dec_open}</span>
              <div className="r-ck-body">
                <div className="r-ck-title">{p.title}</div>
                <div className="r-ck-slug">/{p.slug}</div>
              </div>
              <span className="r-ck-arr">›</span>
            </a>
          ))}
        </div>
      )}

      <div className="r-ck-h"><span className="r-eyebrow">Recent activity</span></div>
      <div className="card">
        <div className="card-body">
          <div className="ledger">
            {(M.timeline || []).slice(0, 6).map((t, i) => (
              <React.Fragment key={i}>
                <span className="when">{t.when}</span>
                <span className={`who ${t.who.startsWith("agent") ? "bot" : ""}`}>{t.who}</span>
                <span className="what">{t.what}</span>
              </React.Fragment>
            ))}
          </div>
        </div>
      </div>
    </>
  );
}


window.ReckonShell = window.ReckonShell || {};
window.ReckonShell.overview = { blockerIsUnresolved, projectActiveSprints, blockerGatedPlans, overviewProjectRows, overviewBlockerScopes, blockersForPlanScope, overviewOptionalSections, OverviewFleet, CockpitBody };
