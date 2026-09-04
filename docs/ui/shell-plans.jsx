// Reckon shell plans module.
const { useCallback, useEffect, useMemo, useRef, useState } = React;

function readableFilterLabel(value) {
  return String(value || "")
    .replaceAll("-", " ")
    .replace(/\b\w/g, character => character.toUpperCase());
}

function ListFilterControls({ filters, setFilters, onClearFilters }) {
  const M = window.STATE;
  const sprints = M.sprints || [];
  const northStars = M.north_stars || [];

  const select = (group, value) => {
    setFilters(f => {
      return { ...f, [group]: value ? [value] : [] };
    });
  };

  const anyActive = (filters.status?.length || 0) + (filters.sprint?.length || 0) + (northStars.length ? (filters.north_star?.length || 0) : 0) > 0;

  // Sprints that have plans in inventory
  const sprintsWithPlans = sprints.filter(s => M.inventory.some(p => p.type === "plan" && p.sprint === s.id));

  const ALWAYS = ["active", "blocked", "pending", "shipped"];
  const actionable = M.inventory.filter(p => (p.type || "plan") === "plan");
  const allStatuses = new Set([...ALWAYS, ...actionable.map(p => p.effective_status || p.status).filter(Boolean)]);
  const statusOrder = ["active", "blocked", "pending", "in-progress", "on-hold", "shipped", "done", "superseded", "abandoned", "draft", "historical"];
  const statusList = [...allStatuses].sort((a, b) => (statusOrder.indexOf(a) + 99) - (statusOrder.indexOf(b) + 99));

  return (
    <div className="r-list-filter-controls" aria-label="Plan filters">
      <label className="r-list-filter">
        <span>Status · total plans</span>
        <select aria-label="Filter plans by status" value={(filters.status || [])[0] || ""} onChange={event => select("status", event.target.value)}>
          <option value="">All · {actionable.length}</option>
          {statusList.map(status => {
            const count = actionable.filter(plan => (plan.effective_status || plan.status) === status).length;
            if (count === 0) return null;
            return <option key={status} value={status} data-count={count}>{readableFilterLabel(status)} · {count}</option>;
          })}
        </select>
      </label>
      {sprintsWithPlans.length > 0 && (
        <label className="r-list-filter">
          <span>Sprint · assigned plans</span>
          <select aria-label="Filter plans by sprint" value={(filters.sprint || [])[0] || ""} onChange={event => select("sprint", event.target.value)}>
            <option value="">All · {actionable.filter(plan => plan.sprint).length}</option>
            {sprintsWithPlans.map(sprint => {
              const count = actionable.filter(plan => plan.sprint === sprint.id).length;
              return <option key={sprint.id} value={sprint.id} data-count={count}>{sprint.id}{sprint.theme ? ` · ${sprint.theme}` : ""} · {count}</option>;
            })}
          </select>
        </label>
      )}
      {anyActive && <button type="button" className="r-list-filter-clear" onClick={onClearFilters} aria-label="Clear plan filters">×</button>}
    </div>
  );
}

// ─── Plans list column ──────────────────────────────────────────────────

// Default direction per sort key: "asc" = smallest/earliest/A/high-priority first.
const SORT_DIR_DEFAULTS = { edited: "desc", created: "desc" };

function sortItems(items, sortBy, dir, kind = "plan") {
  const arr = items.filter(item => (item.type || "plan") === kind);
  const m = dir === "asc" ? 1 : -1;
  if (sortBy === "created") {
    // created is a Unix timestamp (integer seconds) — numeric comparison for second precision
    arr.sort((a, b) => {
      if (!a.created && !b.created) return 0;
      if (!a.created) return 1;
      if (!b.created) return -1;
      return m * ((a.created || 0) - (b.created || 0));
    });
  } else {
    // Edited is the canonical cross-artifact stamp; last is retained for
    // older discovery payloads that predate it.
    arr.sort((a, b) => {
      const aEdited = a.edited || a.last;
      const bEdited = b.edited || b.last;
      if (!aEdited && !bEdited) return 0;
      if (!aEdited) return 1;
      if (!bEdited) return -1;
      return m * String(aEdited).localeCompare(String(bEdited));
    });
  }
  return arr;
}

const SORT_OPTIONS = [
  { value: "edited",   label: "Edited"   },
  { value: "created",  label: "Created"  },
];

function openGateCount(plan) {
  if (Number.isFinite(plan.open_gate_count)) return plan.open_gate_count;
  return (plan.gates || []).filter(gate =>
    String(gate.verdict || "").trim().toLowerCase() !== "passed"
  ).length;
}

function attachmentGroups(state, selectedKey) {
  const relations = state?.attachment_relations || [];
  const selected = state?.plans?.[selectedKey];
  let planKey = selected?.type === "plan" ? selected.nav_key || selected.slug : null;
  if (!planKey) {
    const relation = relations.find(row => row.source === selectedKey);
    planKey = relation ? String(relation.target || "").split("#", 1)[0] : null;
  }
  const sources = new Set(relations
    .filter(row => String(row.target || "").split("#", 1)[0] === planKey)
    .map(row => row.source));
  const attachments = [...sources].map(key => state?.plans?.[key]).filter(Boolean);
  return {
    planKey,
    research: attachments.filter(item => item.type === "research"),
    evidence: attachments.filter(item => item.type === "evidence"),
  };
}

function readingQueue(state, filteredPlans, sortBy, sortDir) {
  const queue = [];
  const seen = new Set();
  const append = (item) => {
    const key = item?.nav_key || item?.slug;
    if (!key || seen.has(key)) return;
    seen.add(key);
    queue.push(key);
  };
  sortItems(filteredPlans, sortBy, sortDir).forEach(plan => {
    append(plan);
    const groups = attachmentGroups(state, plan.nav_key || plan.slug);
    groups.research.forEach(append);
    groups.evidence.forEach(append);
  });
  return queue;
}

function readingQueueStep(queue, selectedKey, direction) {
  const index = queue.indexOf(selectedKey);
  if (index < 0) return null;
  return queue[index + direction] || null;
}

function nextReadingMode(current, key, canRead) {
  if (!canRead) return false;
  if (key.toLowerCase() === "f") return !current;
  if (key === "Escape") return false;
  return current;
}

function paletteItems(currentState, projects) {
  const rows = [];
  const seen = new Set();
  const appendState = (repository, state) => {
    const inventory = Array.isArray(state?.inventory)
      ? state.inventory
      : Array.isArray(state?.plans)
        ? state.plans
        : Object.values(state?.plans || {});
    inventory.forEach(item => {
      const navKey = item.nav_key || item.slug;
      if (!navKey) return;
      const key = `${repository}:${navKey}`;
      if (seen.has(key)) return;
      seen.add(key);
      rows.push({
        ...item,
        nav_key: navKey,
        kind: item.type || "plan",
        label: item.title || item.slug,
        repository,
        status: item.effective_status || item.status || "unknown",
      });
    });
  };
  appendState(currentState?.project || "current", currentState);
  (projects || []).forEach(project => appendState(project.project, project.state));
  return rows;
}

function paletteKindLabel(kind) {
  return ({ plan: "Plans", research: "Research", evidence: "Evidence", figure: "Figures", archive: "Archive" })[kind] || kind;
}

function artifactKindLabel(kind) {
  return ({ plan: "Plans", research: "Research", evidence: "Evidence", figure: "Figures" })[kind] || readableFilterLabel(kind);
}

function artifactStamp(value, numeric = false) {
  if (value === null || value === undefined || value === "") return "unknown";
  if (numeric || typeof value === "number") {
    const stamp = new Date(Number(value) * 1000);
    return Number.isNaN(stamp.getTime()) ? String(value) : stamp.toISOString().slice(0, 10);
  }
  return String(value).replace("T", " ").replace(/Z$/, "");
}

function artifactState(item, kind) {
  if (kind === "plan") return item.effective_status || item.status || "pending";
  if (kind === "research") return item.verdict || "neutral";
  if (kind === "evidence") return item.gate || item.verdict || "running";
  return "neutral";
}

function artifactIsDone(item, kind) {
  if (kind === "plan") return Number(item.impl || 0) >= 1;
  if (kind === "evidence") return ["pass", "passed"].includes(String(artifactState(item, kind)).toLowerCase());
  return false;
}

function artifactIndexRows(items, kind, sortBy, sortDir, status, hideDone) {
  let rows = (items || []).filter(item => (item.type || "plan") === kind);
  if (kind === "plan" && status) {
    rows = rows.filter(item => artifactState(item, kind) === status);
  }
  const shippedSelected = kind === "plan" && status === "shipped";
  if (hideDone && !shippedSelected) {
    rows = rows.filter(item => !artifactIsDone(item, kind));
  }
  return sortItems(rows, sortBy, sortDir, kind);
}

function ArtifactIndex({ kind, onSelect, filters, setFilters, sortBy, setSortBy, sortDir, toggleSortDir }) {
  const state = window.STATE || {};
  const inventory = state.inventory || [];
  const project = state.project || "project";
  const label = artifactKindLabel(kind);
  const status = kind === "plan" ? (filters?.status || [])[0] || "" : "";
  const hideStorage = `reckon:${project}:showShipped`;
  const [hideDone, setHideDone] = React.useState(() => {
    try { return localStorage.getItem(hideStorage) !== "1"; } catch { return true; }
  });
  React.useEffect(() => {
    try { localStorage.setItem(hideStorage, hideDone ? "0" : "1"); } catch {}
  }, [hideDone, hideStorage]);

  const allKindRows = React.useMemo(
    () => inventory.filter(item => (item.type || "plan") === kind),
    [inventory, kind]
  );
  const rows = React.useMemo(
    () => artifactIndexRows(inventory, kind, sortBy, sortDir, status, hideDone),
    [inventory, kind, sortBy, sortDir, status, hideDone]
  );
  const statusChoices = ["active", "blocked", "pending", "shipped"];
  const left = allKindRows.filter(item => !artifactIsDone(item, kind)).length;
  const held = allKindRows.filter(item => artifactState(item, kind) === "blocked").length;
  const today = new Date().toISOString().slice(0, 10);
  const todayCount = allKindRows.filter(item => String(item.edited || item.last || "").startsWith(today)).length;
  const rowsWithoutHide = artifactIndexRows(inventory, kind, sortBy, sortDir, status, false);
  const hiddenEverything = hideDone && rowsWithoutHide.length > 0 && rows.length === 0;

  const setStatus = next => {
    if (kind !== "plan") return;
    setFilters(current => ({ ...current, status: next ? [next] : [] }));
  };

  return (
    <section className="r-artifact-index" aria-label={`${label} index`}>
      <header className="r-artifact-index-head">
        <div className="r-artifact-index-title">
          <p>{label}</p>
          <h1>{allKindRows.length} in {project}</h1>
          <span className="r-artifact-index-figures">
            {kind === "plan" ? <>{left} left <i>·</i> {held} held</> : <>{todayCount} today</>}
          </span>
        </div>
        <div className="r-artifact-index-actions">
          <div className="r-sort-segments" aria-label={`Sort ${label.toLowerCase()} by`}>
            {SORT_OPTIONS.map(option => (
              <button type="button" key={option.value} className={sortBy === option.value ? "active" : ""} aria-pressed={sortBy === option.value} onClick={() => setSortBy(option.value)}>
                {option.label}
              </button>
            ))}
          </div>
          <button type="button" className="r-sort-dir" onClick={toggleSortDir} aria-label={sortDir === "asc" ? "Sort ascending" : "Sort descending"}>
            {sortDir === "asc" ? "↑" : "↓"}
          </button>
          <label className="r-hide-done">
            <input type="checkbox" checked={hideDone} onChange={event => setHideDone(event.target.checked)} />
            hide done
          </label>
        </div>
        {kind === "plan" && (
          <div className="r-feed-status-filters" aria-label="Filter plans by status">
            <button type="button" className={!status ? "active" : ""} aria-pressed={!status} data-count={allKindRows.length} onClick={() => setStatus("")}>All <span>{allKindRows.length}</span></button>
            {statusChoices.map(choice => {
              const count = allKindRows.filter(item => artifactState(item, kind) === choice).length;
              return <button type="button" key={choice} className={status === choice ? "active" : ""} aria-pressed={status === choice} data-count={count} onClick={() => setStatus(choice)}>{choice} <span>{count}</span></button>;
            })}
          </div>
        )}
      </header>

      <div className="r-artifact-feed" data-artifact-feed={kind}>
        {rows.length === 0 ? (
          <div className="r-artifact-empty" role="status">
            {hiddenEverything
              ? `Everything in ${label.toLowerCase()} is done for ${project}.`
              : `No ${label.toLowerCase()} in ${project}.`}
          </div>
        ) : rows.map(item => {
          const navKey = item.nav_key || item.slug;
          const itemState = artifactState(item, kind);
          const percent = Math.round(Number(item.impl || 0) * 100);
          const hours = Number(item.effort_hours);
          return (
            <button type="button" key={navKey} className={`r-artifact-row r-artifact-row-${kind}`} data-artifact-slug={navKey} onClick={() => onSelect(navKey)}>
              <span className={`r-artifact-dot ${kind}-${itemState}`} aria-hidden="true"></span>
              {kind === "figure" && <img className="r-artifact-thumb" src={item.href} alt="" width="50" height="34" />}
              <span className="r-artifact-row-main">
                <span className="r-artifact-row-title">{item.title || item.slug}</span>
                <code>{item.slug}</code>
                {kind === "plan" && (
                  <span className="r-artifact-plan-progress">
                    <span className="r-artifact-progress" aria-label={`${percent} percent complete`}><i style={{ width: `${percent}%` }}></i></span>
                    <span>{Number.isFinite(hours) ? `${hours}h` : (item.effort || "—")} · {item.sprint || "unassigned"}</span>
                  </span>
                )}
                {kind === "figure" && <span className="r-artifact-dimensions">{item.dims || "dimensions unknown"}</span>}
              </span>
              <span className="r-artifact-row-trailing">
                <span className="r-artifact-stamps">
                  <span>created {artifactStamp(item.created, true)}</span>
                  <i aria-hidden="true">·</i>
                  <span>edited {artifactStamp(item.edited || item.last)}</span>
                </span>
                {kind === "plan"
                  ? <span className={`r-artifact-status-chip ${itemState}`}>{itemState}</span>
                  : <span className={`r-artifact-verdict r-artifact-verdict-${itemState}`}>{itemState}</span>}
              </span>
            </button>
          );
        })}
      </div>
    </section>
  );
}

function selectPlanSection(event, onSelectPlan, slug, sectionId) {
  event.stopPropagation();
  onSelectPlan(slug);
  window.setTimeout(() => document.getElementById(sectionId)?.scrollIntoView({ block: "start" }), 0);
}

function ListCol({ route, onNav, onSelectPlan, items, sortBy, setSortBy, sortDir, toggleSortDir, filters, setFilters, onClearFilters, onClearContext, onSetContext }) {
  const sorted = React.useMemo(() => sortItems(items, sortBy, sortDir), [items, sortBy, sortDir]);
  const contextSlug = filters.context || null;

  const SortAscIcon = () => (
    <svg width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
      <path d="M5 9V1M2 4l3-3 3 3"/>
    </svg>
  );
  const SortDescIcon = () => (
    <svg width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
      <path d="M5 1v8M2 6l3 3 3-3"/>
    </svg>
  );
  const RelatedIcon = () => (
    <svg width="11" height="11" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
      <circle cx="2.5" cy="6" r="1.4"/>
      <circle cx="9.5" cy="2.5" r="1.4"/>
      <circle cx="9.5" cy="9.5" r="1.4"/>
      <path d="M3.9 5.5L8.1 3M3.9 6.5L8.1 9"/>
    </svg>
  );

  return (
    <div className="r-list">
      <div className="r-sort-bar">
        <span className="r-sort-n">{sorted.length} shown</span>
        <div className="r-sort-segments" aria-label="Sort plans by">
          {SORT_OPTIONS.map(option => (
            <button type="button" key={option.value} className={sortBy === option.value ? "active" : ""} aria-pressed={sortBy === option.value} onClick={() => setSortBy(option.value)}>
              {option.label}
            </button>
          ))}
        </div>
        <button
          className={`r-sort-dir ${sortDir !== (SORT_DIR_DEFAULTS[sortBy] || "asc") ? "active" : ""}`}
          onClick={toggleSortDir}
          title={sortDir === "asc" ? "Ascending order" : "Descending order"}
          aria-label={sortDir === "asc" ? "Sort ascending" : "Sort descending"}
        >
          {sortDir === "asc" ? <SortAscIcon /> : <SortDescIcon />}
        </button>
        <button type="button" className="r-sort-more" aria-label="More plan list options" disabled>⋯</button>
        <ListFilterControls filters={filters} setFilters={setFilters} onClearFilters={onClearFilters} />
      </div>

      {/* Context / Related filter — explicit opt-in, not auto-applied */}
      {!contextSlug && route.view === "plan" && route.slug && (
        <button className="r-related-btn" onClick={() => onSetContext(route.slug)} title="Show only plans related to the current one">
          <RelatedIcon /> Related
        </button>
      )}
      {contextSlug && (
        <div className="r-context-chip">
          <RelatedIcon />
          <span className="r-context-label">Related to <code>{contextSlug}</code></span>
          <button className="r-context-x" onClick={onClearContext} title="Clear">×</button>
        </div>
      )}

      <div className="r-list-body">
        {items.length === 0 ? (
          <div className="r-list-empty">
            No plans match.
            {(filters?.status?.length || filters?.sprint?.length) && (
              <button className="r-clear-btn" onClick={onClearFilters}>Clear filters</button>
            )}
          </div>
        ) : sorted.map(p => {
        const navKey = p.nav_key || p.slug;
        const active = route.view === "plan" && route.slug === navKey;
        const isArchived = p.archived === "1" || p.archived === true || p.archived === "true";
        const isRead = p.read === "1" || p.read === true || p.read === "true";
        const authored = p.workflow_status || p.status || "pending";
        const effective = p.effective_status || authored;
        const gates = openGateCount(p);
        const edited = p.last || null;
        const identity = [p.roi, p.effort].filter(value => value && value !== "—");
        const editedValue = <>{edited && <span className="date" title={`Edited ${edited}`}>edited {edited}</span>}</>;
        const metadata = [
          ...identity.map((value, index) => <span key={`identity-${value}-${index}`}>{value}</span>),
          <button key="implementation" className="r-compact-signal pct" title={`${Math.round((p.impl || 0) * 100)} percent complete; open implementation`} aria-label={`${p.title}: ${Math.round((p.impl || 0) * 100)} percent complete`} onClick={(event) => selectPlanSection(event, onSelectPlan, navKey, "implementation")}>{Math.round((p.impl || 0) * 100)}%</button>,
          edited ? editedValue : null,
          authored !== effective ? (
            <button key="transition" className="r-status-transition" title={`Authored ${authored}; effective ${effective}; ${gates} open gates`} aria-label={`${p.title}: ${authored} to ${effective}, ${gates} open gates`} onClick={(event) => selectPlanSection(event, onSelectPlan, navKey, "gate-state-heading")}>
              <span>{authored}</span><span aria-hidden="true">→</span><span>{effective}</span><span>{gates} open {gates === 1 ? "gate" : "gates"}</span>
            </button>
          ) : null,
          (p.blockers || 0) > 0 ? <button key="blockers" className="sig blk" title={`${p.blockers} blockers; open blockers`} aria-label={`${p.title}: ${p.blockers} blockers`} onClick={(event) => selectPlanSection(event, onSelectPlan, navKey, "blockers")}>Blockers {p.blockers}</button> : null,
        ].filter(Boolean);
        return (
          <div
            key={navKey}
            className={`r-row ${active ? "active" : ""} ${isArchived ? "archived" : ""} ${isRead ? "read" : ""}`}
            onClick={() => onSelectPlan(navKey)}
          >
            <span className={`dot ${authored === effective ? effective : "transition"}`} title={authored === effective ? effective : `${authored} to ${effective}`}></span>
            <div>
              <div className="t" title={p.title}>{p.title}</div>
              <div className="meta">
                {metadata.map((item, index) => (
                  <React.Fragment key={`metadata-${index}`}>
                    {index > 0 && <span className="sp" aria-hidden="true">·</span>}
                    {item}
                  </React.Fragment>
                ))}
              </div>
            </div>
          </div>
        );
        })}
      </div>
    </div>
  );
}

// ─── Title bar ──────────────────────────────────────────────────────────

// ─── Inline plan dependency graph (replaces both the Reading/Graph tabs
//     and the legacy depends-on / blocks pill strip).
// Shown above the report body. Collapsible — when hidden, only a thin header
// strip remains so the report slides up. State persists per project.

function PlanGraphStrip({ slug, onNav, hidden, setHidden }) {
  const M = window.STATE;
  const p = M?.inventory?.find(x => x.slug === slug);
  if (!p) return null;
  // Probe symmetric counts via the same reverse index the graph uses.
  let counts = { dep: 0, blk: 0 };
  if (window.RadialFan && window.STATE) {
    // Cheap recount — avoids touching graph internals.
    const inv = window.STATE.inventory || [];
    const directDeps = new Set(p.depends_on || []);
    const directBlocks = new Set(p.blocks || []);
    const revBlk = new Set();  // who depends on me → I block them
    const revDep = new Set();  // who blocks me   → I depend on them
    for (const q of inv) {
      if ((q.depends_on || []).includes(p.slug)) revBlk.add(q.slug);
      if ((q.blocks     || []).includes(p.slug)) revDep.add(q.slug);
    }
    const depSet = new Set([...directDeps, ...revDep].filter(s => s !== p.slug));
    const blkSet = new Set([...directBlocks, ...revBlk].filter(s => s !== p.slug));
    counts = { dep: depSet.size, blk: blkSet.size };
  }
  const empty = counts.dep === 0 && counts.blk === 0;
  return (
    <div className={`r-plan-graph ${hidden ? "is-hidden" : ""}`}>
      <div className="r-plan-graph-h">
        <button
          type="button"
          className="r-plan-graph-toggle"
          onClick={() => setHidden(h => !h)}
          aria-expanded={!hidden}
          title={hidden ? "Show dependency graph" : "Hide dependency graph"}
        >
          <svg className="caret" width="9" height="9" viewBox="0 0 9 9" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M2 3l2.5 3L7 3"/>
          </svg>
          <span className="lbl">Dependencies</span>
          {!empty && (
            <span className="counts">
              <span className="c-pair"><span className="n">{counts.dep}</span><span className="k">in</span></span>
              <span className="c-pair"><span className="n">{counts.blk}</span><span className="k">out</span></span>
            </span>
          )}
          {empty && <span className="counts muted">no edges</span>}
        </button>
      </div>
      {!hidden && (
        window.RadialFan
          ? <window.RadialFan focalSlug={slug} onNav={onNav} compact={true} />
          : <div style={{ padding: 16, color: "var(--muted)", fontSize: 12 }}>Graph view loading…</div>
      )}
    </div>
  );
}

// Statuses surfaced in the lifecycle menu. Workflow group is the day-to-day
// active set; the second group covers paused / completed / abandoned states.

window.ReckonShell = window.ReckonShell || {};
window.ReckonShell.plans = { readableFilterLabel, ListFilterControls, SORT_DIR_DEFAULTS, sortItems, SORT_OPTIONS, openGateCount, attachmentGroups, readingQueue, readingQueueStep, nextReadingMode, paletteItems, paletteKindLabel, artifactKindLabel, artifactStamp, artifactState, artifactIsDone, artifactIndexRows, ArtifactIndex, selectPlanSection, ListCol, PlanGraphStrip };
