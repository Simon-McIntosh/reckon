const { useEffect, useMemo, useState } = React;

const CLOSED_ITEM_STATUSES = new Set(["shipped", "done", "superseded", "abandoned", "historical"]);
const CLOSED_SPRINT_AUTHORED_STATUSES = new Set(["done", "superseded", "abandoned", "historical"]);

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

function sprintRefSlug(ref) {
  return typeof ref === "string" ? ref : ref?.slug;
}

function sprintMemberSlugs(sprint) {
  return (sprint?.items || []).map(item => typeof item === "string" ? item : item.slug).filter(Boolean);
}

function sprintMembers(sprint, inventory) {
  const bySlug = new Map((inventory || []).map(plan => [plan.slug, plan]));
  return sprintMemberSlugs(sprint).map(slug => bySlug.get(slug)).filter(Boolean);
}

function sprintHoursSummary(members) {
  const total = (members || []).reduce((sum, plan) => sum + Number(plan.effort_hours || 0), 0);
  const left = (members || []).reduce(
    (sum, plan) => sum + Number(plan.effort_hours || 0) * (1 - Math.min(1, Number(plan.impl || 0))),
    0
  );
  return { total, left };
}

function derivedFlowChainHours(inventory, runs, project, now = new Date()) {
  if (!window.ReckonCrewSchedule) return 0;
  return window.ReckonCrewSchedule.farEnd(inventory, runs, project, now);
}

// State is derived from members, never read from the sprint's own status
// field, because the field can silently drift from what actually landed.
function derivedSprintState(sprint, inventory) {
  const members = sprintMembers(sprint, inventory);
  const authored = sprint?.status || "planned";
  let state;
  if (!members.length) state = "empty";
  else if (members.every(plan => Number(plan.impl || 0) >= 1)) state = "shipped";
  else if (members.some(plan => Number(plan.impl || 0) > 0 || plan.status === "active")) state = "active";
  else state = "planned";
  const heldCount = members.filter(plan => plan.status === "blocked").length;
  const flag = authored !== state
    ? `was ${authored}`
    : (heldCount > 0 ? `${heldCount} held` : null);
  const meanImpl = members.length
    ? members.reduce((sum, plan) => sum + Number(plan.impl || 0), 0) / members.length
    : 0;
  return { state, flag, heldCount, members, meanImpl };
}

function sprintStateRows(sprints, inventory) {
  return (sprints || []).map(sprint => {
    const derived = derivedSprintState(sprint, inventory);
    const hours = sprintHoursSummary(derived.members);
    const closed = derived.state === "shipped" || CLOSED_SPRINT_AUTHORED_STATUSES.has(sprint.status);
    return { sprint, ...derived, hours, closed };
  });
}

// The transitive prerequisite closure of a sprint's members that falls
// outside the sprint, tagged as ghost context for the DAG body. Reading
// `depends_on` only keeps this in step with the Graph tab's closure.
function transitivePrerequisiteGhosts(members, inventory) {
  const bySlug = new Map((inventory || []).map(plan => [plan.slug, plan]));
  const memberSlugs = new Set((members || []).map(plan => plan.slug));
  const ghosts = new Map();
  const visit = (slug, seen) => {
    const plan = bySlug.get(slug);
    if (!plan || seen.has(slug)) return;
    seen.add(slug);
    (plan.depends_on || []).map(sprintRefSlug).filter(Boolean).forEach(depSlug => {
      const dependency = bySlug.get(depSlug);
      if (!dependency) return;
      if (!memberSlugs.has(depSlug) && !ghosts.has(depSlug)) {
        ghosts.set(depSlug, { ...dependency, ghost: true });
      }
      visit(depSlug, seen);
    });
  };
  (members || []).forEach(plan => visit(plan.slug, new Set()));
  return [...ghosts.values()];
}

function sprintDagPlans(sprint, inventory) {
  const members = sprintMembers(sprint, inventory);
  const ghosts = transitivePrerequisiteGhosts(members, inventory);
  return [...members, ...ghosts];
}

function planOpenDecisionCount(plan) {
  if (Array.isArray(plan?.decisions)) return plan.decisions.filter(decision => !decision.choice).length;
  return Number(plan?.dec_open || 0);
}

function sprintDetailStats(sprint, inventory) {
  const members = sprintMembers(sprint, inventory);
  const ghosts = transitivePrerequisiteGhosts(members, inventory);
  const hours = sprintHoursSummary(members);
  const layout = window.ReckonGraph ? window.ReckonGraph.layout([...members, ...ghosts], "sprint-detail-stats") : null;
  const depth = layout ? Math.max(0, ...Object.values(layout.depth)) : 0;
  return {
    plans: members.length,
    workerHours: hours.total,
    depth,
    held: members.filter(plan => plan.status === "blocked").length,
    prerequisites: ghosts.length,
    openDecisions: members.reduce((total, plan) => total + planOpenDecisionCount(plan), 0),
  };
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

function SprintDetail({ sprint, inventory, onBack, onNav }) {
  const dagPlans = useMemo(() => sprintDagPlans(sprint, inventory), [sprint, inventory]);
  const layout = useMemo(
    () => (window.ReckonGraph ? window.ReckonGraph.layout(dagPlans, `sprint-${sprint.id}`) : null),
    [dagPlans, sprint.id]
  );
  const stats = useMemo(() => sprintDetailStats(sprint, inventory), [sprint, inventory]);
  const openDecisions = stats.openDecisions;

  const copyShipLine = async () => {
    if (openDecisions > 0) return;
    await navigator.clipboard?.writeText(`/reckon-ship ${sprint.id}`);
    if (window.flashSaved) window.flashSaved("ship line copied");
  };

  return (
    <section className="r-sprint-detail" aria-label={`${sprint.id} detail`}>
      <header className="r-sprint-detail-head">
        <button type="button" className="r-sprint-back" onClick={onBack}>← Sprints</button>
        <strong>{sprint.id}</strong>
        <span className="r-sprint-detail-theme">{sprint.theme || sprint.summary}</span>
        <button
          type="button"
          className="r-sprint-ship"
          disabled={openDecisions > 0}
          title={openDecisions ? `${openDecisions} open decision${openDecisions === 1 ? "" : "s"}` : `Copy /reckon-ship ${sprint.id}`}
          onClick={copyShipLine}
        >
          {openDecisions > 0 ? `${openDecisions} open decision${openDecisions === 1 ? "" : "s"}` : `/reckon-ship ${sprint.id}`}
        </button>
      </header>
      <div className="r-sprint-detail-stats">
        <div><span>plans</span><strong>{stats.plans}</strong></div>
        <div><span>worker-hours</span><strong>{Math.round(stats.workerHours)}</strong></div>
        <div><span>depth</span><strong>{stats.depth}</strong></div>
        <div><span>held</span><strong>{stats.held}</strong></div>
        <div><span>prerequisites</span><strong>{stats.prerequisites}</strong></div>
        <div><span>open decisions</span><strong>{stats.openDecisions}</strong></div>
      </div>
      <div className="r-sprint-detail-legend">
        <span><i className="solid"></i>shipped prerequisite</span>
        <span><i className="dashed"></i>unshipped prerequisite</span>
      </div>
      {layout ? (
        <div className="r-sprint-dag-scroll">
          <div className="r-sprint-dag-stage" style={{ width: layout.width, height: layout.height }}>
            <svg width={layout.width} height={layout.height} aria-hidden="true">
              {layout.edges.map(edge => (
                <g key={edge.key}>
                  <path d={edge.d} stroke={edge.stroke} strokeWidth={edge.strokeWidth} strokeDasharray={edge.dash} fill="none" />
                  <polygon points={edge.head} fill={edge.stroke} />
                </g>
              ))}
            </svg>
            {layout.nodes.map(node => (
              <a
                key={node.key}
                className={`r-sprint-dag-card ${node.ghost ? "ghost" : ""} ${node.blocked ? "blocked" : ""}`}
                href={node.ghost ? undefined : `#plan/${node.slug}`}
                style={{
                  left: node.x,
                  top: node.y,
                  width: node.width,
                  height: node.height,
                  borderStyle: node.borderStyle,
                  background: node.background,
                  opacity: node.opacity,
                }}
                onClick={event => {
                  event.preventDefault();
                  if (node.ghost) return;
                  onNav && onNav({ view: "plan", slug: node.slug });
                }}
              >
                <strong>{node.title}</strong>
                <span>{node.statusText}</span>
              </a>
            ))}
          </div>
        </div>
      ) : <p className="r-sprint-dag-empty">Graph layout is unavailable.</p>}
    </section>
  );
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
  const [detailSprintId, setDetailSprintId] = useState(null);
  const project = M.project || document.querySelector('meta[name="docs-project"]')?.content || "";
  const items = sprintInventoryItems(sprint, M.inventory);

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
  const stateRows = useMemo(() => sprintStateRows(allSprints, M.inventory), [allSprints, M.inventory]);
  const foldedCount = stateRows.filter(row => row.closed).length;
  const activeConflict = activeSprintConflict(M.active_sprints, M.active_sprint_id);
  const detailSprint = detailSprintId ? allSprints.find(candidate => candidate.id === detailSprintId) : null;
  const projectPlans = M.inventory.filter(plan => (plan.type || "plan") === "plan");
  const projectHours = sprintHoursSummary(projectPlans);
  const chainHours = derivedFlowChainHours(projectPlans, M.runs || M.crew_runs || [], project);
  const heldPlans = projectPlans.filter(plan => plan.status === "blocked").length;

  if (detailSprint) {
    return (
      <div className="r-page wide r-sprint-surface">
        <SprintDetail
          sprint={detailSprint}
          inventory={M.inventory}
          onBack={() => setDetailSprintId(null)}
          onNav={onNav}
        />
      </div>
    );
  }

  return (
    <div className="r-page wide r-sprint-surface">
      <header className="r-sp-head">
        <div><div className="r-eyebrow">Sprints</div><h1>All sprints</h1></div>
        <div className="r-sprint-state-summary r-sprint-flow-summary" aria-label="Derived project figures">
          <span>left <strong>{Math.round(projectHours.left)}h</strong></span>
          <span>chain <strong>{Math.round(chainHours)}h</strong></span>
          <span>held <strong>{heldPlans}</strong></span>
        </div>
        <div className="r-sprint-tabs" role="tablist" aria-label="Sprint views">
          <button role="tab" aria-selected={surface === "overview"} onClick={() => setSurface("overview")}>Overview</button>
          <button role="tab" aria-selected={surface === "ready"} onClick={() => setSurface("ready")}>Ready lanes</button>
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
                <thead><tr><th scope="col">Sprint</th><th scope="col">State</th><th scope="col">Implementation</th><th scope="col">Hours</th><th scope="col">Flags</th></tr></thead>
                <tbody>{stateRows.map(row => {
                  const { sprint: listedSprint, hours } = row;
                  const percent = Math.round(row.meanImpl * 100);
                  const findings = subjectFindings(reviewFindings, "sprint", listedSprint.id);
                  return <tr key={listedSprint.id} hidden={foldClosed && row.closed} className={row.closed ? "closed" : ""}>
                    <th scope="row"><a href={`#sprint/${listedSprint.id}`} onClick={event => { event.preventDefault(); setDetailSprintId(listedSprint.id); }}><strong>{listedSprint.id}</strong><span>{listedSprint.theme || listedSprint.summary || "Untitled sprint"}</span></a></th>
                    <td><span className={`r-sprint-status ${row.state}`}>{row.state}</span></td>
                    <td><div className="r-sprint-implementation" aria-label={`${listedSprint.id}: ${percent}% implemented`}><span><i style={{ width: `${percent}%` }}></i></span><strong>{percent}%</strong></div></td>
                    <td className="r-sprint-hours">{Math.round(hours.total)}h · {Math.round(hours.left)}h left</td>
                    <td><div className="r-sprint-flags">{row.flag && <span className={row.flag.startsWith("was ") ? "drift" : "held"}>{row.flag}</span>}{listedSprint.id === M.active_sprint_id && <span className="focus">focus</span>}<FindingBadges findings={findings} /></div></td>
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
