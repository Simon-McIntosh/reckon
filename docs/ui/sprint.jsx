const SPRINT_HORIZONS = {
  "1hr": { milliseconds: 60 * 60 * 1000, subDay: true },
  "1D": { milliseconds: 24 * 60 * 60 * 1000, subDay: true },
  "4w": { days: 28 },
  "8w": { days: 56 },
  "6m": { days: 183 },
  all: {},
};

const CLOSED_ITEM_STATUSES = new Set(["shipped", "done", "superseded", "abandoned", "historical"]);
const CLOSED_SPRINT_STATUSES = new Set(["shipped", "done", "superseded", "abandoned", "historical"]);

function naturalSprintKey(value) {
  return String(value || "").split(/(\d+)/).filter(Boolean).map(part => /^\d+$/.test(part) ? Number(part) : part.toLowerCase());
}

function compareNaturalSprintIds(left, right) {
  const a = naturalSprintKey(left.id);
  const b = naturalSprintKey(right.id);
  for (let index = 0; index < Math.max(a.length, b.length); index += 1) {
    if (a[index] === undefined) return -1;
    if (b[index] === undefined) return 1;
    if (a[index] === b[index]) continue;
    return a[index] < b[index] ? -1 : 1;
  }
  return 0;
}

function orderedSprints(sprints, review) {
  const natural = [...(sprints || [])].sort(compareNaturalSprintIds);
  const derived = Array.isArray(review?.sprint_order) ? review.sprint_order : [];
  if (!derived.length) return natural;
  const positions = new Map(derived.map((id, index) => [id, index]));
  return [...natural].sort((left, right) => {
    const leftRank = positions.get(left.id);
    const rightRank = positions.get(right.id);
    if (leftRank === undefined && rightRank === undefined) return compareNaturalSprintIds(left, right);
    if (leftRank === undefined) return 1;
    if (rightRank === undefined) return -1;
    return leftRank - rightRank;
  });
}

function openReviewFindings(review) {
  return (review?.findings || []).filter(finding => !finding.resolved_at);
}

function subjectFindings(findings, kind, id) {
  return findings.filter(finding => finding.subject?.kind === kind && finding.subject?.id === id);
}

function FindingBadges({ findings }) {
  if (!findings.length) return null;
  return <span className="r-review-badges">{findings.map(finding => (
    <a key={finding.id} className={`r-review-badge ${finding.severity}`} href={`#review-finding-${finding.id}`} title={finding.evidence?.join(" · ") || finding.code}>
      <span>{finding.severity}</span>{finding.code}
    </a>
  ))}</span>;
}

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

function sprintStateRows(sprints, todayValue) {
  const today = String(todayValue || "").slice(0, 10);
  return (sprints || []).map((sprint, index) => {
    const metrics = sprint.metrics || {};
    const counts = metrics.by_effective_status || {};
    const closedItems = [...CLOSED_ITEM_STATUSES].reduce((total, status) => total + Number(counts[status] || 0), 0);
    const openCount = Math.max(0, Number(metrics.item_count || 0) - closedItems);
    return {
      sprint,
      position: index + 1,
      metrics,
      openCount,
      blockedCount: Number(counts.blocked || 0),
      active: sprint.status === "active",
      delayed: Boolean(today && sprint.ends && sprint.ends < today && openCount > 0),
      closed: CLOSED_SPRINT_STATUSES.has(sprint.status),
    };
  });
}

function activeSprintConflict(activeSprints, activePointer) {
  const ids = (activeSprints || []).map(row => typeof row === "string" ? row : row.id).filter(Boolean);
  return ids.length !== 1 || ids[0] !== activePointer;
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
  const review = M.review || null;
  const allSprints = useMemo(() => orderedSprints(M.sprints, review), [M.sprints, review]);
  const reviewFindings = useMemo(() => openReviewFindings(review), [review]);
  const idx = useMemo(() => {
    const requested = allSprints.findIndex(sprint => sprint.id === sprintId);
    return requested >= 0 ? requested : allSprints.findIndex(sprint => sprint.id === M.active_sprint_id);
  }, [sprintId, allSprints]);
  const sprint = allSprints[idx];
  if (!sprint) return <div className="r-page">No sprint.</div>;

  const [surface, setSurface] = useState("overview");
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
  const stateRows = sprintStateRows(allSprints, M.today);
  const foldedCount = stateRows.filter(row => row.closed).length;
  const finishedRuns = useMemo(() => sprintCompletedRuns(sprint, finishedRunsByPlan), [sprint, finishedRunsByPlan]);
  const activeConflict = activeSprintConflict(M.active_sprints, M.active_sprint_id);

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
        <div className="r-card-heading"><a className="r-card-title" href={`#plan/${plan.slug}`}>{plan.title}</a><FindingBadges findings={subjectFindings(reviewFindings, "plan", plan.slug)} /></div>
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
        <section className="r-sprint-overview" aria-label="All-sprints state overview">
          <section className="r-sprint-state" aria-labelledby="sprint-state-heading">
            <header>
              <div><span className="r-eyebrow">Project state</span><h2 id="sprint-state-heading">Every sprint</h2></div>
              <div className="r-sprint-state-summary">
                <span>{stateRows.length} total</span>
                {activeConflict && <strong className="r-sprint-conflict">Active pointer conflict</strong>}
                <label><input type="checkbox" checked={foldClosed} onChange={event => setFoldClosed(event.target.checked)} /> Fold closed</label>
              </div>
            </header>
            <div className="r-sprint-table-wrap">
              <table className="r-sprint-table">
                <thead><tr><th scope="col">Order</th><th scope="col">Sprint</th><th scope="col">Status</th><th scope="col">Implementation</th><th scope="col">Flags</th><th scope="col">Current work</th></tr></thead>
                <tbody>{stateRows.map(row => {
                  const { sprint: listedSprint, metrics } = row;
                  const percent = Math.round(Number(metrics.mean_impl || 0) * 100);
                  const findings = subjectFindings(reviewFindings, "sprint", listedSprint.id);
                  return <tr key={listedSprint.id} hidden={foldClosed && row.closed} className={row.closed ? "closed" : ""}>
                    <td className="r-sprint-order"><span>{row.position}</span></td>
                    <th scope="row"><a href={`#sprint/${listedSprint.id}`} onClick={event => navigateSprint(event, listedSprint.id)}><strong>{listedSprint.id}</strong><span>{listedSprint.theme || listedSprint.summary || "Untitled sprint"}</span></a></th>
                    <td><span className={`r-sprint-status ${listedSprint.status || "planned"}`}>{listedSprint.status || "planned"}</span></td>
                    <td><div className="r-sprint-implementation" aria-label={`${listedSprint.id}: ${percent}% implemented`}><span><i style={{ width: `${percent}%` }}></i></span><strong>{percent}%</strong></div></td>
                    <td><div className="r-sprint-flags">{row.active && <span className="active">active</span>}{row.blockedCount > 0 && <span className="blocked">blocked {row.blockedCount}</span>}{row.delayed && <span className="delayed">delayed</span>}{listedSprint.id === M.active_sprint_id && <span className="focus">focus</span>}<FindingBadges findings={findings} /></div></td>
                    <td><div className="r-sprint-current">{(metrics.current_work || []).map(item => <a key={item.slug} href={`#plan/${item.slug}`}>{item.title || item.slug}</a>)}{!(metrics.current_work || []).length && <span>None</span>}</div></td>
                  </tr>;
                })}</tbody>
              </table>
            </div>
            {foldClosed && foldedCount > 0 && <button className="r-folded-sprints" onClick={() => setFoldClosed(false)}><strong>{foldedCount}</strong> closed {foldedCount === 1 ? "sprint" : "sprints"} folded · show all</button>}
          </section>
          {review && <section className="r-priority-panel" aria-label="Review priority">
            <header><div><span className="r-eyebrow">Review priority</span><h2>Ranked plans</h2></div><span>{review.priority?.length || 0} ranked</span></header>
            {(review.priority || []).length ? <ol>{[...(review.priority || [])].sort((left, right) => Number(left.landed) - Number(right.landed) || left.rank - right.rank).map(row => (
              <li key={row.ref} className={row.landed ? "landed" : ""}>
                <span className="r-priority-rank">{row.rank}</span>
                <span className="r-priority-name"><a href={`#plan/${row.ref}`}>{row.title || row.ref}</a><FindingBadges findings={subjectFindings(reviewFindings, "plan", row.ref)} /></span>
                <span className={`r-priority-status ${row.effective_status || row.status}`}>{row.effective_status || row.status || "unknown"}</span>
                <span className="r-priority-impl">{Math.round((row.impl || 0) * 100)}%</span>
                <span className="r-reason-chips">{(row.reasons || []).map(reason => <span key={reason}>{reason}</span>)}</span>
                <span className="r-priority-detail">{row.detail}</span>
              </li>
            ))}</ol> : <p className="r-priority-empty">No plans are ranked in the current review.</p>}
          </section>}
          {review && reviewFindings.length > 0 && <section className="r-review-findings" aria-label="Open review findings"><header><span className="r-eyebrow">Open findings</span><strong>{reviewFindings.length}</strong></header>{reviewFindings.map(finding => <article id={`review-finding-${finding.id}`} key={finding.id}><FindingBadges findings={[finding]} /><span>{finding.subject?.kind}: {finding.subject?.id}</span><p>{(finding.evidence || []).join(" ")}</p></article>)}</section>}
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
