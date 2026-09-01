const { useEffect, useMemo, useState } = React;

const HORIZON_HOURS = 48;
const HORIZON_REFRESH_MS = 30 * 1000;
const HOUR_MS = 60 * 60 * 1000;

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

function readyLaneRows(readySet, sprints, inventory) {
  const contractsBySlug = new Map();
  (sprints || []).forEach(sprint => (sprint.items || []).forEach(item => {
    if (item && typeof item === "object" && item.slug) contractsBySlug.set(item.slug, item);
  }));
  const plansBySlug = new Map((inventory || []).map(plan => [plan.slug, plan]));
  return (readySet?.ready || []).flatMap(row => {
    const plan = plansBySlug.get(row.slug) || {};
    const contract = contractsBySlug.get(row.slug) || {};
    const effectiveStatus = plan.effective_status || plan.status || "pending";
    const landed = Boolean(row.landed) || CLOSED_ITEM_STATUSES.has(effectiveStatus) || Number(row.progress_pct || 0) >= 100;
    const sections = Array.isArray(row.section_readiness) && row.section_readiness.length
      ? row.section_readiness
      : [{ section: null, ready: true, blockers: [] }];
    return sections.map(sectionRow => {
      const blockers = Array.isArray(sectionRow.blockers) ? sectionRow.blockers : [];
      const causeClasses = [...new Set(blockers.map(blocker => {
        if (blocker.kind === "explicit") return "explicit";
        if (blocker.kind === "gate" || blocker.gate || blocker.verdict) return "gate";
        if (blocker.kind === "decision" || blocker.decision || blocker.choice !== undefined) return "decision";
        return "dependency";
      }))];
      const section = sectionRow.section || null;
      const invocationSection = section ? String(section).replace(/^s(?=\d)/, "§") : "";
      return {
        ...row,
        title: plan.title || row.title || row.slug,
        description: plan.summary || plan.description || "No description supplied",
        whyNow: row.why_now || contract.why_now || row.reason || "Not supplied",
        doneWhen: row.done_when || contract.done_when || "Not supplied",
        section,
        ready: sectionRow.ready !== false,
        blockers,
        causeClasses,
        effectiveStatus,
        stateLabel: readyLaneState(plan),
        landed,
        invocation: `/reckon-ship ${row.slug}${invocationSection ? ` ${invocationSection}` : ""}`,
      };
    });
  }).sort((left, right) => Number(left.landed) - Number(right.landed));
}

function readyLaneState(plan) {
  const authored = plan.workflow_status || plan.status || "pending";
  const effective = plan.effective_status || authored;
  const openGates = (plan.gates || []).filter(gate => !(gate.passed || gate.verdict === "passed")).length;
  const gateLabel = `${openGates} open ${openGates === 1 ? "gate" : "gates"}`;
  return authored === effective ? `${effective} · ${gateLabel}` : `${authored} → ${effective} · ${gateLabel}`;
}

function activeSprintConflict(activeSprints, activePointer) {
  const ids = (activeSprints || []).map(row => typeof row === "string" ? row : row.id).filter(Boolean);
  return ids.length !== 1 || ids[0] !== activePointer;
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

function horizonStrip(currentInstant, completedRuns = [], liveRuns = []) {
  const instant = new Date(currentInstant);
  const safeInstant = Number.isNaN(instant.getTime()) ? new Date() : instant;
  const start = new Date(
    safeInstant.getFullYear(),
    safeInstant.getMonth(),
    safeInstant.getDate()
  ).getTime();
  const span = HORIZON_HOURS * HOUR_MS;
  const end = start + span;
  const tomorrow = new Date(
    safeInstant.getFullYear(),
    safeInstant.getMonth(),
    safeInstant.getDate() + 1
  ).getTime();
  const position = timestamp => {
    if (timestamp === null || timestamp < start || timestamp > end) return null;
    return ((timestamp - start) / span) * 100;
  };
  const events = [
    ...(completedRuns || []).map(run => ({ kind: "completed", run, timestamp: completedRunTime(run) })),
    ...(liveRuns || []).map(run => {
      const timestamp = Date.parse(run?.dispatched_at || "");
      return { kind: "live", run, timestamp: Number.isNaN(timestamp) ? null : timestamp };
    }),
  ].map(event => ({ ...event, left: position(event.timestamp) }))
    .filter(event => event.left !== null)
    .sort((left, right) => left.timestamp - right.timestamp);
  const ticks = Array.from({ length: 9 }, (_, index) => {
    const timestamp = start + index * 6 * HOUR_MS;
    return {
      left: index * 12.5,
      label: new Date(timestamp).toLocaleTimeString(undefined, {
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      }),
    };
  });
  return {
    start: new Date(start).toISOString(),
    end: new Date(end).toISOString(),
    nowPosition: position(safeInstant.getTime()),
    tomorrowPosition: position(tomorrow),
    ticks,
    events,
  };
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
  const [currentInstant, setCurrentInstant] = useState(() => Date.now());
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
    const timer = window.setInterval(() => setCurrentInstant(Date.now()), HORIZON_REFRESH_MS);
    return () => window.clearInterval(timer);
  }, []);

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

  const readyLanes = useMemo(
    () => readyLaneRows(M.ready_set, M.sprints, M.inventory),
    [M.ready_set, M.sprints, M.inventory]
  );
  const decisions = items.flatMap(plan => {
    const rows = Array.isArray(plan.decisions) ? plan.decisions.filter(decision => !decision.choice) : [];
    if (rows.length) return rows.map(decision => ({ plan, label: decision.question || decision.key || "Open decision" }));
    return Array.from({ length: plan.dec_open || 0 }, (_, index) => ({ plan, label: `Open decision ${index + 1}` }));
  });
  const stateRows = sprintStateRows(allSprints, M.today);
  const foldedCount = stateRows.filter(row => row.closed).length;
  const finishedRuns = useMemo(() => sprintCompletedRuns(sprint, finishedRunsByPlan), [sprint, finishedRunsByPlan]);
  const strip = useMemo(
    () => horizonStrip(currentInstant, finishedRuns, liveRuns),
    [currentInstant, finishedRuns, liveRuns]
  );
  const activeConflict = activeSprintConflict(M.active_sprints, M.active_sprint_id);

  const navigateSprint = (event, id) => {
    if (!onNav) return;
    event.preventDefault();
    onNav({ view: "sprint", sprint: id });
  };

  return (
    <div className="r-page wide r-sprint-surface">
      <header className="r-sp-head">
        <div><div className="r-eyebrow">Sprints</div><h1>All sprints</h1></div>
        <div className="r-sprint-tabs" role="tablist" aria-label="Sprint views">
          <button role="tab" aria-selected={surface === "overview"} onClick={() => setSurface("overview")}>Overview</button>
          <button role="tab" aria-selected={surface === "ready"} onClick={() => setSurface("ready")}>Ready lanes</button>
        </div>
      </header>

      {surface === "overview" ? (
        <section className="r-sprint-overview" aria-label="All-sprints state overview">
          <section className="r-horizon-strip" aria-label="48-hour activity strip">
            <header>
              <span>Today</span>
              <span>Tomorrow</span>
            </header>
            <div className="r-horizon-track">
              <i className="r-tomorrow-line" style={{ left: `${strip.tomorrowPosition}%` }} aria-hidden="true"></i>
              {strip.ticks.map(tick => <span key={tick.left} className="r-horizon-tick" style={{ left: `${tick.left}%` }}>{tick.label}</span>)}
              {strip.events.map(event => {
                const run = event.run;
                const title = `${event.kind === "live" ? "Live" : "Completed"}: ${run.node || run.plan || "run"}`;
                return <a
                  key={`${event.kind}-${run.run_id}`}
                  className={`r-horizon-event ${event.kind} ${run.gate || ""}`}
                  href={run.plan ? `#plan/${run.plan}` : "#sprints"}
                  style={{ left: `${event.left}%` }}
                  aria-label={title}
                  title={title}
                ></a>;
              })}
              <i className="r-now-line" style={{ left: `${strip.nowPosition}%` }} aria-label="Current time"></i>
            </div>
            <footer><span><i className="completed"></i> completed</span><span><i className="live"></i> live</span><span>{strip.events.length} timestamped {strip.events.length === 1 ? "event" : "events"}</span></footer>
          </section>
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
        <section className="r-ready-lanes" aria-labelledby="ready-lanes-heading">
          <div className="r-sp-switcher">
            <button className="nav-btn" aria-label="Previous sprint" disabled={idx <= 0} onClick={() => onNav({ view: "sprint", sprint: allSprints[idx - 1].id })}>←</button>
            <div className="current"><span className="id">{sprint.id}</span><span className={`st ${sprint.status}`}>{sprint.status}</span></div>
            <button className="nav-btn" aria-label="Next sprint" disabled={idx >= allSprints.length - 1} onClick={() => onNav({ view: "sprint", sprint: allSprints[idx + 1].id })}>→</button>
            <span className="range">{sprint.starts} → {sprint.ends}</span>
            <button className="gen-prompt" disabled={decisions.length > 0} title={decisions.length ? "Resolve this sprint's open decisions before dispatch" : "Generate fleet prompt for this sprint"} onClick={() => setShowSprintPrompt(true)}>Generate prompt</button>
          </div>
          <div className="r-sp-goal"><div className="lbl">Goal</div><div className="theme">{sprint.theme}</div>{sprint.summary && <div className="summary">{sprint.summary}</div>}</div>
          {decisions.length > 0 && <aside className="r-needs-you"><h2>Needs you <span>{decisions.length}</span></h2><ul>{decisions.map((decision, index) => <li key={`${decision.plan.slug}-${index}`}><a href={`#plan/${decision.plan.slug}`} title={`Open ${decision.plan.title}`}>{decision.label}</a><span>{decision.plan.title}</span></li>)}</ul></aside>}
          <header className="r-ready-lanes-head"><div><span className="r-eyebrow">What can run now</span><h2 id="ready-lanes-heading">Concurrent ready lanes</h2></div><span>{readyLanes.filter(row => row.ready && !row.landed).length} open</span></header>
          {readyLanes.length ? <div className="r-ready-lane-list">{readyLanes.map((lane, index) => {
            const laneState = lane.landed ? "landed" : lane.ready ? "in-progress" : "blocked";
            const causeNames = lane.causeClasses.length ? lane.causeClasses : ["dependency"];
            return <article key={`${lane.slug}-${lane.section || "plan"}-${index}`} className={`r-ready-lane ${laneState} ${causeNames.map(cause => `cause-${cause}`).join(" ")}`}>
              <div className="r-ready-lane-title"><a href={`#plan/${lane.slug}`}>{lane.title}</a>{lane.section && <code>{lane.section}</code>}<span className={`r-ready-lane-state ${laneState}`}>{laneState}</span></div>
              <p className="r-ready-lane-description" title={lane.description}>{lane.description}</p>
              <p className="r-ready-lane-reason">{lane.ready ? lane.reason : `Blocked · ${causeNames.join(" + ")}`}</p>
              <div className="r-ready-lane-meta"><span>{lane.sprint || "No sprint"}</span><span>{lane.progress_pct || 0}% implemented</span><span className="r-ready-lane-plan-state">{lane.stateLabel}</span></div>
              <details className="r-ready-lane-contract"><summary>Contract</summary><dl><dt>Why now</dt><dd>{lane.whyNow}</dd><dt>Done when</dt><dd>{lane.doneWhen}</dd></dl></details>
              <code className="r-ready-lane-invocation">{lane.invocation}</code>
            </article>;
          })}</div> : <p className="r-ready-lanes-empty">No work is currently in the served ready set.</p>}
        </section>
      )}

      {showSprintPrompt && window.reckon?.PromptModal && sprintPromptText != null && <window.reckon.PromptModal planSlug={`sprint-${sprint.id}`} initialPrompt={sprintPromptText} onClose={() => setShowSprintPrompt(false)} />}
    </div>
  );
}

const SprintView = Sprint;
window.Sprint = Sprint;
window.SprintView = SprintView;
