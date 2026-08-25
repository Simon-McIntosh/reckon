// Reckon shell — top bar + plan workspace.
// Top bar: brand, search, view tabs and settings.
// Body: filters · plans list · reader · attachments.

function parseHash() {
  const h = (window.location.hash || "").replace(/^#/, "");
  if (!h || h === "cockpit") return { view: "cockpit" };
  if (h.startsWith("plan/")) return { view: "plan", slug: decodeURIComponent(h.slice(5)) };
  if (h.startsWith("sprint/")) return { view: "sprint", sprint: decodeURIComponent(h.slice(7)) };
  if (h === "graph") return { view: "graph" };
  if (h === "crew") return { view: "crew" };
  if (h === "plans") return { view: "plan", slug: null };
  if (h === "sprints") return { view: "sprint", sprint: null };
  return { view: "cockpit" };
}

function canvasViewForRoute(route) {
  const view = route?.view;
  return ["cockpit", "plan", "sprint", "graph", "crew"].includes(view)
    ? view
    : "cockpit";
}

function useHashRoute() {
  const [route, setRoute] = useState(parseHash());
  useEffect(() => {
    const onHash = () => setRoute(parseHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  const nav = useCallback((to) => {
    if (to.view === "cockpit") window.location.hash = "#cockpit";
    else if (to.view === "plan") window.location.hash = `#plan/${encodeURIComponent(to.slug)}`;
    else if (to.view === "sprint") window.location.hash = `#sprint/${encodeURIComponent(to.sprint)}`;
    else if (to.view === "graph") window.location.hash = "#graph";
    else if (to.view === "crew") window.location.hash = "#crew";
  }, []);
  return [route, nav];
}

// ─── Top bar ────────────────────────────────────────────────────────────

const PROJECT_VISIBILITY_STORAGE = "reckon:hidden-projects";

function mountedProjectRows(projects) {
  return (projects || []).filter(project => Number(project.plans_count) > 0);
}

function manageableProjectRows(projects) {
  return projects || [];
}

function effectiveHiddenProjects(projects, hiddenProjects) {
  if (Array.isArray(hiddenProjects)) return hiddenProjects;
  return [];
}

function visibleProjectRows(projects, hiddenProjects) {
  const hidden = new Set(effectiveHiddenProjects(projects, hiddenProjects));
  const mounted = mountedProjectRows(projects);
  if (mounted.length && mounted.every(project => hidden.has(project.project))) {
    hidden.delete(mounted[0].project);
  }
  return mounted.filter(project => !hidden.has(project.project));
}

function projectVisibilityChange(projects, hiddenProjects, focusedProject, targetProject) {
  const hidden = new Set(effectiveHiddenProjects(projects, hiddenProjects));
  if (hidden.has(targetProject)) {
    hidden.delete(targetProject);
    return { changed: true, hidden: [...hidden], focus: focusedProject };
  }
  const survivors = visibleProjectRows(projects, hiddenProjects)
    .filter(project => project.project !== targetProject);
  if (!survivors.length) {
    return { changed: false, hidden: [...hidden], focus: focusedProject };
  }
  hidden.add(targetProject);
  return {
    changed: true,
    hidden: [...hidden],
    focus: focusedProject === targetProject ? survivors[0].project : focusedProject,
  };
}

function snapshotTime(loadedAt) {
  if (!loadedAt) return "unknown time";
  const loaded = new Date(loadedAt);
  if (Number.isNaN(loaded.getTime())) return "unknown time";
  return loaded.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function snapshotReceipt(state) {
  return {
    sourceFormat: state?.source_format || "unknown source",
    resourceCount: Object.keys(state?.resource_versions || {}).length,
    loadedAt: snapshotTime(state?.loaded_at),
  };
}

function TopBar({ route, onNav, navProject, onOpenCmdK, filtersHidden, onToggleFilters, theme, setTheme, density, setDensity, projects, hiddenProjects, onToggleProject }) {
  const M = window.STATE;
  const view = route.view;
  const currentProject = M?.project
    || (typeof document !== "undefined" && document.querySelector('meta[name="docs-project"]')?.content)
    || null;

  const [requestedSettingsPanel, setRequestedSettingsPanel] = useState(null);
  const mountedProjects = mountedProjectRows(projects);
  const manageableProjects = manageableProjectRows(projects);
  const visibleProjects = visibleProjectRows(projects, hiddenProjects);
  const visibleProjectNames = new Set(visibleProjects.map(project => project.project));
  const current = manageableProjects.find(project => project.project === currentProject);
  const snapshot = snapshotReceipt(M);

  // Assign the window global to a local var so JSX can use it as a component.
  const SM = window.SettingsMenu;

  const goPlans = () => {
    const target = M?.inventory?.find(p => p.status === "active") || M?.inventory?.[0];
    if (target) onNav({ view: "plan", slug: target.nav_key || target.slug });
  };
  const goSprints = () => {
    const id = M?.active_sprint_id || M?.sprint?.id || M?.sprints?.[0]?.id;
    if (id) onNav({ view: "sprint", sprint: id });
  };

  return (
    <div className="r-topbar">
      <button className="r-topbar-brand" onClick={() => navProject(null)} title="All projects">
        <span className="r-topbar-mark">r</span>
        <span>reckon</span>
      </button>
      <details className="r-project-manage">
        <summary>
          <span className={`r-live-dot ${current?.live ? "is-live" : ""}`} aria-hidden="true"></span>
          <span>{currentProject || "Fleet"}</span>
          <span className="r-project-caret" aria-hidden="true">▾</span>
        </summary>
        <div className="r-project-menu">
          <div className="r-project-menu-count">{visibleProjects.length} shown · {manageableProjects.length} mounted</div>
          {manageableProjects.map(project => (
            <button
              type="button"
              key={project.project}
              className={`${project.project === currentProject ? "active " : ""}${visibleProjectNames.has(project.project) ? "" : "is-hidden"}`.trim()}
              onClick={() => navProject(project.project)}
              aria-current={project.project === currentProject ? "page" : undefined}
            >
              <span className={`r-live-dot ${project.live ? "is-live" : ""}`} aria-hidden="true"></span>
              <strong>{project.project}</strong>
              <span>{project.plans_count} plans</span>
              <span>{project.live_count || 0} live</span>
              {!visibleProjectNames.has(project.project) && <span className="r-project-visibility-state">hidden</span>}
            </button>
          ))}
          <button
            type="button"
            className="r-project-configure"
            onClick={() => {
              document.querySelector(".r-project-manage")?.removeAttribute("open");
              setRequestedSettingsPanel("visibility");
            }}
          >Configure visibility…</button>
        </div>
      </details>
      <div className="r-glyph-tabs">
        <button className={`r-glyph ${view === "plan" ? "active" : ""}`} onClick={goPlans} title="Plans">
          <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
            <path d="M3 4h10M3 8h10M3 12h7"/>
          </svg>
          Plans
        </button>
        <button className={`r-glyph ${view === "sprint" ? "active" : ""}`} onClick={goSprints} title="Sprints">
          <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
            <rect x="2.5" y="3" width="3" height="10" rx="0.6"/>
            <rect x="6.5" y="3" width="3" height="10" rx="0.6"/>
            <rect x="10.5" y="3" width="3" height="10" rx="0.6"/>
          </svg>
          Sprints
        </button>
        <button className={`r-glyph ${view === "graph" ? "active" : ""}`} onClick={() => onNav({ view: "graph" })} title="Graph">
          <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
            <circle cx="3.5" cy="4" r="1.5"/>
            <circle cx="3.5" cy="12" r="1.5"/>
            <circle cx="12.5" cy="8" r="1.5"/>
            <path d="M5 4l6 3.5M5 12l6-3.5"/>
          </svg>
          Graph
        </button>
        <button className={`r-glyph ${view === "crew" ? "active" : ""}`} onClick={() => onNav({ view: "crew" })} title="Crew">
          {window.GLYPHS?.crew}
          Crew
        </button>
      </div>
      <button className="r-cmdk-trigger" onClick={onOpenCmdK} title="Search plans · ⌘K">
        <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
          <circle cx="7" cy="7" r="4.5"/>
          <path d="M13 13l-2.5-2.5"/>
        </svg>
        <span>Search</span>
        <span className="kbd">⌘K</span>
      </button>
      <div className="top-r">
        {SM ? (
          <SM
            theme={theme}
            setTheme={setTheme}
            density={density}
            setDensity={setDensity}
            projects={manageableProjects.map(project => project)}
            visibleProjects={visibleProjects}
            onToggleProject={onToggleProject}
            requestedPanel={requestedSettingsPanel}
            onPanelOpened={() => setRequestedSettingsPanel(null)}
          />
        ) : null}
        {view === "plan" && (
          <button
            className="icon-btn"
            onClick={onToggleFilters}
            title={`${filtersHidden ? "Show" : "Hide"} filters + list · ⌘B`}
            aria-pressed={!filtersHidden}
          >
            <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
              <rect x="2" y="3" width="12" height="10" rx="1.5"/>
              <path d="M6 3v10"/>
            </svg>
          </button>
        )}
      </div>
      <div className="r-snapshot-receipt" role="status">
        <span>{snapshot.sourceFormat}</span>
        <span>{snapshot.resourceCount} resources</span>
        <span>loaded {snapshot.loadedAt}</span>
        <button type="button" onClick={() => window.location.reload()}>Refresh</button>
      </div>
    </div>
  );
}

// ─── Filters column ─────────────────────────────────────────────────────

function readableFilterLabel(value) {
  return String(value || "")
    .replaceAll("-", " ")
    .replace(/\b\w/g, character => character.toUpperCase());
}

function FiltersCol({ filters, setFilters }) {
  const M = window.STATE;
  const sprints = M.sprints || [];
  const northStars = M.north_stars || [];

  const toggle = (group, value) => {
    setFilters(f => {
      // Single-select per group: clicking the same value clears it; another value replaces.
      const cur = (f[group] || []);
      if (cur.includes(value)) return { ...f, [group]: [] };
      return { ...f, [group]: [value] };
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
    <aside className="r-filters" aria-label="Plan filters">
      <div className="r-filter-group" aria-label="Status filters">
        {statusList.map(s => {
          const n = actionable.filter(p => (p.effective_status || p.status) === s).length;
          const on = (filters.status || []).includes(s);
          if (n === 0) return null;
          return (
            <button type="button" key={s} className={`r-chip ${on ? "on" : ""}`} onClick={() => toggle("status", s)} aria-pressed={on} title={s}>
              <span className={`dot ${s}`} aria-hidden="true"></span>
              <span className="r-chip-label">{readableFilterLabel(s)}</span>
              <span className="n">{n}</span>
            </button>
          );
        })}
      </div>

      <div className="r-filter-divider" aria-hidden="true"></div>

      {sprintsWithPlans.length > 0 && (
        <div className="r-filter-group r-sprint-filters" aria-label="Sprint filters">
          {sprintsWithPlans.map(s => {
            const n = actionable.filter(p => p.sprint === s.id).length;
            const on = (filters.sprint || []).includes(s.id);
            return (
              <button type="button" key={s.id} className={`r-chip r-chip-sprint ${on ? "on" : ""}`} onClick={() => toggle("sprint", s.id)} aria-pressed={on} title={`${s.id}${s.theme ? ` · ${s.theme}` : ""}`}>
                <span className="dot sprint" aria-hidden="true"></span>
                <span className="r-chip-label">{s.id}{s.theme ? ` · ${s.theme}` : ""}</span>
                <span className="n">{n}</span>
              </button>
            );
          })}
        </div>
      )}

      {northStars.length > 0 && (
        <div className="r-filter-group" aria-label="North stars">
          {northStars.map(direction => {
            const n = actionable.filter(p => p.north_star === direction.id).length;
            const on = (filters.north_star || []).includes(direction.id);
            return (
              <button type="button" key={direction.id} className={`r-chip ${on ? "on" : ""}`} onClick={() => toggle("north_star", direction.id)} aria-pressed={on} title={`${direction.name} · ${direction.statement}`}>
                <span className="dot north-star" aria-hidden="true"></span>
                <span className="n">{n}</span>
              </button>
            );
          })}
        </div>
      )}

      {anyActive && (
        <button className="r-clear-top" onClick={() => setFilters({})}>
          × clear filters
        </button>
      )}

    </aside>
  );
}

// ─── Plans list column ──────────────────────────────────────────────────

// Default direction per sort key: "asc" = smallest/earliest/A/high-priority first.
const SORT_DIR_DEFAULTS = { edited: "desc", created: "desc" };

function sortItems(items, sortBy, dir) {
  const arr = items.filter(item => (item.type || "plan") === "plan");
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
    // edited / recent (default): sort by plan-modified date string; undated items go to end
    arr.sort((a, b) => {
      if (!a.last && !b.last) return 0;
      if (!a.last) return 1;
      if (!b.last) return -1;
      return m * a.last.localeCompare(b.last);
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

function selectPlanSection(event, onSelectPlan, slug, sectionId) {
  event.stopPropagation();
  onSelectPlan(slug);
  window.setTimeout(() => document.getElementById(sectionId)?.scrollIntoView({ block: "start" }), 0);
}

function ListCol({ route, onNav, onSelectPlan, items, sortBy, setSortBy, sortDir, toggleSortDir, filters, onClearFilters, onClearContext, onSetContext }) {
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
        <span className="r-sort-n">{sorted.length} plans</span>
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
                {identity.map((value, index) => <React.Fragment key={`${value}-${index}`}><span>{value}</span><span className="sp">·</span></React.Fragment>)}
                <button className="r-compact-signal pct" title={`${Math.round((p.impl || 0) * 100)} percent complete; open implementation`} aria-label={`${p.title}: ${Math.round((p.impl || 0) * 100)} percent complete`} onClick={(event) => selectPlanSection(event, onSelectPlan, navKey, "implementation")}>{Math.round((p.impl || 0) * 100)}%</button>
                {edited && <><span className="sp">·</span><span className="date" title={`Edited ${edited}`}>edited {edited}</span></>}
                {authored !== effective && <span className="sp">·</span>}
                {authored !== effective ? (
                  <button className="r-status-transition" title={`Authored ${authored}; effective ${effective}; ${gates} open gates`} aria-label={`${p.title}: ${authored} to ${effective}, ${gates} open gates`} onClick={(event) => selectPlanSection(event, onSelectPlan, navKey, "gate-state-heading")}>
                  <span>{authored}</span><span aria-hidden="true">→</span><span>{effective}</span><span>{gates} open {gates === 1 ? "gate" : "gates"}</span>
                  </button>
                ) : null}
                {(p.blockers || 0) > 0 && <span className="sp">·</span>}
                {(p.blockers || 0) > 0 && <button className="sig blk" title={`${p.blockers} blockers; open blockers`} aria-label={`${p.title}: ${p.blockers} blockers`} onClick={(event) => selectPlanSection(event, onSelectPlan, navKey, "blockers")}>Blockers {p.blockers}</button>}
              </div>
            </div>
          </div>
        );
        })}
      </div>
    </div>
  );
}

function AttachmentRail({ selectedKey, onSelect }) {
  const groups = attachmentGroups(window.STATE, selectedKey);
  const rows = [
    ["research", "Research", groups.research],
    ["evidence", "Evidence", groups.evidence],
  ];
  const empty = !groups.planKey || rows.every(([, , items]) => items.length === 0);
  return (
    <aside className="r-attachment-rail" aria-label="Plan attachments">
      <div className="r-attachment-heading">Attached</div>
      {empty && <p className="r-attachment-empty">No attachments</p>}
      {rows.map(([type, label, items]) => items.length > 0 && (
        <section key={type} className="r-attachment-group" aria-labelledby={`attachment-${type}`}>
          <h2 id={`attachment-${type}`}>{label}<span>{items.length}</span></h2>
          {items.map(item => {
            const key = item.nav_key || item.slug;
            return (
              <button type="button" key={key} className={selectedKey === key ? "active" : ""} aria-pressed={selectedKey === key} onClick={() => onSelect(key)}>
                <span className={`dot ${type}`} aria-hidden="true"></span>
                <span>{item.title || item.slug}</span>
              </button>
            );
          })}
        </section>
      ))}
    </aside>
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
const LIFECYCLE_STATUSES = [
  { value: "active",     label: "Active",     group: "workflow" },
  { value: "blocked",    label: "Blocked",    group: "workflow" },
  { value: "pending",    label: "Pending",    group: "workflow" },
  { value: "on-hold",    label: "On hold",    group: "paused"   },
  { value: "shipped",    label: "Shipped",    group: "done"     },
  { value: "superseded", label: "Superseded", group: "done"     },
  { value: "abandoned",  label: "Abandoned",  group: "done"     },
];

function statusWriteNotice(slug, result) {
  if (result?.persistence === "canonical" && result.ok) {
    return { state: "saved", text: `${slug} · saved to plan HTML`, version: result.version };
  }
  if (result?.persistence === "conflict") {
    return { state: "conflict", text: `${slug} · conflict; not saved · refresh and retry` };
  }
  if (result?.persistence === "failed") {
    return { state: "failed", text: `${slug} · ${result.where || "not saved"}` };
  }
  return {
    state: "local-only",
    text: `${slug} · local only · ${result?.where || "canonical save unavailable"}`,
  };
}

async function persistStatusPatch({ slug, plan, patch, onAfterChange, save, notify }) {
  const previous = Object.fromEntries(Object.keys(patch).map(key => [key, plan[key]]));
  Object.assign(plan, patch);
  if (onAfterChange) onAfterChange();

  let result;
  try {
    result = save
      ? await save(slug, patch)
      : { ok: false, persistence: "failed", local_ok: false, where: "not saved (persistence unavailable)", version: null };
  } catch (error) {
    console.warn("StatusMenu: persistence failed", error);
    result = { ok: false, persistence: "failed", local_ok: false, where: "not saved (persistence failed)", version: null };
  }

  if (result?.persistence === "conflict" || result?.persistence === "failed") {
    Object.assign(plan, previous);
    if (onAfterChange) onAfterChange();
  }

  if (notify) notify(statusWriteNotice(slug, result));
  return result;
}

function StatusMenu({ slug, plan, onAfterChange }) {
  const [open, setOpen] = React.useState(false);
  const ref = React.useRef(null);
  React.useEffect(() => {
    if (!open) return;
    const onDown = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    const onKey = (e) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", onDown); document.removeEventListener("keydown", onKey); };
  }, [open]);

  const isResearch = plan.type === "research" || plan.type === "doc";
  const isArchived = plan.archived === "1" || plan.archived === true || plan.archived === "true";
  const isRead = plan.read === "1" || plan.read === true || plan.read === "true";

  const apply = async (patch) => {
    // Update local inventory immediately so the UI reflects the change
    // before the server round-trips.
    const save = window.planSave || window.reckon?.planSave;
    return persistStatusPatch({
      slug,
      plan,
      patch,
      onAfterChange,
      save,
      notify: window.flashSaved,
    });
  };

  const setStatus = async (s) => { setOpen(false); await apply({ status: s }); };
  const toggleArchive = async () => { setOpen(false); await apply({ archived: isArchived ? "" : "1" }); };
  const toggleRead = async () => { setOpen(false); await apply({ read: isRead ? "" : "1" }); };

  return (
    <div className="r-status-menu-wrap" ref={ref}>
      <button
        type="button"
        className={`status-pill clickable ${plan.status} ${isArchived ? "archived" : ""}`}
        onClick={() => setOpen(o => !o)}
        title="Change status"
      >
        <span className="dot"></span>
        <span>{plan.status}</span>
        {isArchived && <span className="r-status-tag">archived</span>}
        {isResearch && isRead && <span className="r-status-tag read">read</span>}
        <svg className="r-status-caret" width="8" height="6" viewBox="0 0 8 6" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
          <path d="M1 1.5l3 3 3-3"/>
        </svg>
      </button>
      {open && (
        <div className="r-status-popover" role="menu">
          <div className="r-status-section">
            <div className="r-status-section-h">Workflow</div>
            {LIFECYCLE_STATUSES.filter(s => s.group === "workflow").map(s => (
              <button key={s.value} type="button" className={`r-status-item ${plan.status === s.value ? "current" : ""}`} onClick={() => setStatus(s.value)}>
                <span className={`r-status-dot ${s.value}`}></span>{s.label}
              </button>
            ))}
          </div>
          <div className="r-status-section">
            <div className="r-status-section-h">Paused</div>
            {LIFECYCLE_STATUSES.filter(s => s.group === "paused").map(s => (
              <button key={s.value} type="button" className={`r-status-item ${plan.status === s.value ? "current" : ""}`} onClick={() => setStatus(s.value)}>
                <span className={`r-status-dot ${s.value}`}></span>{s.label}
              </button>
            ))}
          </div>
          <div className="r-status-section">
            <div className="r-status-section-h">Closed</div>
            {LIFECYCLE_STATUSES.filter(s => s.group === "done").map(s => (
              <button key={s.value} type="button" className={`r-status-item ${plan.status === s.value ? "current" : ""}`} onClick={() => setStatus(s.value)}>
                <span className={`r-status-dot ${s.value}`}></span>{s.label}
              </button>
            ))}
          </div>
          <div className="r-status-section r-status-actions">
            <button type="button" className={`r-status-item r-status-action ${isArchived ? "on" : ""}`} onClick={toggleArchive} title="Archive removes the plan from the default list — it still exists, just out of the way.">
              <span className="r-status-action-glyph">{isArchived ? "↺" : "▦"}</span>
              {isArchived ? "Unarchive" : "Archive"}
            </button>
            {isResearch && (
              <button type="button" className={`r-status-item r-status-action ${isRead ? "on" : ""}`} onClick={toggleRead} title="Mark this research/doc as reviewed.">
                <span className="r-status-action-glyph">{isRead ? "↺" : "✓"}</span>
                {isRead ? "Mark unread" : "Mark read"}
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function TitleBar({ route, onNav, onOpenPrompt, onPlanMutated }) {
  const M = window.STATE;
  if (route.view === "cockpit") {
    return null;
  }
  if (route.view === "plan") {
    const p = M.inventory.find(x => (x.nav_key || x.slug) === route.slug);
    if (!p) return null;
    const isPlan = (p.type || "plan") === "plan";
    const direction = (M.north_stars || []).find(item => item.id === p.north_star);
    const openDecs = p.dec_open || 0;
    const blockedByDecisions = isPlan && openDecs > 0;
    const hasMetadataValue = value => {
      const text = value === null || value === undefined ? "" : String(value).trim();
      return text !== "" && text !== "-" && text !== "—";
    };
    const hasImplementation = hasMetadataValue(p.impl) && Number.isFinite(Number(p.impl));
    return (
      <div className="r-titlebar">
        <div className="row1">
          <span className="crumbs"><code>/{route.slug}</code></span>
          <span className="title">{p.title}</span>
          <div className="actions">
            {blockedByDecisions && (
              <button className="sig dec" data-target="decisions" title="Take the next open decision" aria-label={`${p.title}: ${openDecs} open decisions`} onClick={() => {
                  const section = document.getElementById("decisions");
                  if (section) section.scrollIntoView({ behavior: "smooth", block: "start" });
                }}
              >
                Resolve <span className="resolve-badge">{openDecs}</span>
              </button>
            )}
            {isPlan && <button
              className="gen-prompt"
              onClick={onOpenPrompt}
              title="Generate handoff prompt"
            >
              <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                <path d="M4 3h6l2 2v8H4z"/>
                <path d="M10 3v2h2"/>
                <path d="M6 7h4M6 9h4M6 11h2"/>
              </svg>
              Generate prompt
            </button>}
          </div>
        </div>
        <div className="row2">
          {isPlan ? <>
            <StatusMenu slug={route.slug} plan={p} onAfterChange={onPlanMutated} />
            {hasMetadataValue(p.ms) && <>
              <span className="dot-sep">·</span>
              <span className="meta-item"><span className="k">ms</span><span className="v">{p.ms}</span></span>
            </>}
            {hasMetadataValue(p.sprint) && <>
              <span className="dot-sep">·</span>
              <span className="meta-item"><span className="k">sprint</span><a className="v" href={`#sprint/${p.sprint}`} style={{ borderBottom: "1px dotted var(--line)" }}>{p.sprint}</a></span>
            </>}
            {hasImplementation && <>
              <span className="dot-sep">·</span>
              <span className="meta-item"><span className="k">progress</span><span className="v">{Math.round(Number(p.impl) * 100)}%</span></span>
            </>}
            {hasMetadataValue(p.north_star) && <>
              <span className="dot-sep">·</span>
              <span className="meta-item r-north-star-badge" title={direction?.statement || p.north_star}><span className="k">north star</span><span className="v">{direction?.name || p.north_star}</span></span>
            </>}
            {hasMetadataValue(p.capability?.class) && <>
              <span className="dot-sep">·</span>
              <span className="meta-item"><span className="k">capability</span><span className="v">{p.capability.class}</span></span>
            </>}
          </> : <>
            <span className={`r-type-pill ${p.type}`}>{p.type}</span>
            {p.type === "research" && <>
              <span className="dot-sep">·</span>
              <span className="meta-item"><span className="k">informs</span><span className="v">{(p.informs || []).join(", ") || "unlinked"}</span></span>
              {p.reviewed_at && <><span className="dot-sep">·</span><span className="meta-item"><span className="k">reviewed</span><span className="v">{p.reviewed_at}</span></span></>}
            </>}
            {p.type === "evidence" && <>
              <span className="dot-sep">·</span>
              <span className="meta-item"><span className="k">verdict</span><span className="v">{p.verdict || "unreviewed"}</span></span>
              <span className="dot-sep">·</span>
              <span className="meta-item"><span className="k">evidence for</span><span className="v">{(p.evidence_for || []).join(", ") || "unlinked"}</span></span>
            </>}
          </>}
          {hasMetadataValue(p.last) && <>
            <span className="dot-sep">·</span>
            <span className="meta-item"><span className="k">last</span><span className="v">{p.last}</span></span>
          </>}
          {hasMetadataValue(p.owner) && <>
            <span className="dot-sep">·</span>
            <span className="meta-item"><span className="k">owner</span><span className="v">{p.owner}</span></span>
          </>}
        </div>
      </div>
    );
  }
  if (route.view === "sprint") {
    const sprints = M.sprints || [];
    const idx = sprints.findIndex(s => s.id === route.sprint);
    const s = sprints[idx];
    const slugSet = new Set((s?.items || []).map(it => typeof it === "string" ? it : it.slug));
    const inv = [...slugSet].map(slug => M.inventory.find(x => x.slug === slug)).filter(Boolean);
    const totalOpen = inv.reduce((n, p) => n + (p.dec_open || 0), 0);
    const blocked = totalOpen > 0;
    const blockedPlans = inv.filter(p => (p.dec_open || 0) > 0);
    const handleResolve = () => {
      if (blockedPlans.length === 0) return;
      // Rotate: if currently on a plan in the list, go to next; otherwise first.
      onNav({ view: "plan", slug: blockedPlans[0].slug });
    };
    const handleGen = () => {
      window.dispatchEvent(new CustomEvent("r-open-fleet-prompt"));
    };
    const projectName = M.projects?.[0]?.project || M.project || "project";
    return (
      <div className="r-titlebar">
        <div className="row1">
          <span className="crumbs">sprint</span>
          <span className="title">{s ? `${s.id} · ${s.theme}` : route.sprint}</span>
          <div className="actions">
            <button className="r-nav-btn" disabled={idx <= 0} onClick={() => onNav({ view: "sprint", sprint: sprints[idx - 1].id })}>‹</button>
            <button className="r-nav-btn" disabled={idx >= sprints.length - 1} onClick={() => onNav({ view: "sprint", sprint: sprints[idx + 1].id })}>›</button>
            <button
              className="gen-prompt"
              onClick={handleGen}
              title="Generate fleet prompt"
            >
              <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                <path d="M4 3h6l2 2v8H4z"/><path d="M10 3v2h2"/><path d="M6 7h4M6 9h4M6 11h2"/>
              </svg>
              Generate prompt
            </button>
          </div>
        </div>
        {s && (
          <div className="row2">
            <span className={`status-pill ${s.status}`}><span className="dot"></span><span>{s.status}</span></span>
            <span className="dot-sep">·</span>
            <span className="meta-item"><span className="k">starts</span><span className="v">{s.starts}</span></span>
            <span className="dot-sep">·</span>
            <span className="meta-item"><span className="k">ends</span><span className="v">{s.ends}</span></span>
            <span className="dot-sep">·</span>
            <span className="meta-item"><span className="k">items</span><span className="v">{s.items.length}</span></span>
            <span style={{ flex: 1 }}></span>
            {totalOpen > 0 && (
              <button className="resolve-btn" onClick={handleResolve} title="Take the next open decision">
                Resolve <span className="resolve-badge">{totalOpen}</span>
              </button>
            )}
          </div>
        )}
      </div>
    );
  }
  return null;
}

// ─── App ────────────────────────────────────────────────────────────────

function ProjectStateLoadPanel({ load }) {
  const frame = {
    maxWidth: 640,
    margin: "64px auto",
    padding: "28px 32px",
    border: "1px solid var(--line)",
    borderRadius: "var(--radius-lg)",
    background: "var(--bg)",
    boxShadow: "var(--shadow)",
  };
  const label = {
    margin: 0,
    color: load.phase === "error" ? "var(--bad)" : "var(--muted)",
    fontFamily: "var(--mono)",
    fontSize: 11,
    fontWeight: 700,
    letterSpacing: ".08em",
    textTransform: "uppercase",
  };

  if (load.phase === "error") {
    const response = load.httpStatus === null
      ? (load.message || "Request failed before an HTTP response arrived")
      : `HTTP ${load.httpStatus}`;
    return React.createElement(
      "main",
      { role: "alert", style: frame },
      React.createElement("p", { style: label }, "Discovery failed"),
      React.createElement(
        "h1",
        { style: { margin: "8px 0 10px", fontSize: 22, color: "var(--ink)" } },
        "Project state unavailable",
      ),
      React.createElement(
        "p",
        { style: { margin: "0 0 20px", color: "var(--ink-2)", lineHeight: 1.55 } },
        "The project shell cannot render trusted state because discovery did not complete.",
      ),
      React.createElement(
        "dl",
        { style: { display: "grid", gridTemplateColumns: "max-content 1fr", gap: "8px 16px", margin: 0, fontSize: 13 } },
        React.createElement("dt", { style: { color: "var(--muted)" } }, "Endpoint"),
        React.createElement("dd", { style: { margin: 0 } }, React.createElement("code", null, load.endpoint)),
        React.createElement("dt", { style: { color: "var(--muted)" } }, "Response"),
        React.createElement("dd", { style: { margin: 0, fontFamily: "var(--mono)" } }, response),
      ),
    );
  }

  return React.createElement(
    "main",
    { role: "status", style: { ...frame, textAlign: "center" } },
    React.createElement("p", { style: label }, "Discovery in progress"),
    React.createElement(
      "h1",
      { style: { margin: "8px 0 10px", fontSize: 20, color: "var(--ink)" } },
      "Loading plan state…",
    ),
    React.createElement(
      "p",
      { style: { margin: 0, color: "var(--muted)", fontFamily: "var(--mono)", fontSize: 12 } },
      `${load.endpoint} · ${load.elapsedSeconds}s elapsed`,
    ),
  );
}

function ReadyGate({ children }) {
  const [ready, setReady] = useState(!window.STATE_READY);
  const [error, setError] = useState(window.STATE_ERROR || null);
  const [elapsedAt, setElapsedAt] = useState(Date.now());
  useEffect(() => {
    if (!window.STATE_READY) return undefined;
    let active = true;
    const updateElapsed = () => {
      if (active) setElapsedAt(Date.now());
    };
    const timer = window.setInterval(updateElapsed, 1000);
    window.STATE_READY.then(
      () => {
        window.clearInterval(timer);
        if (active) setReady(true);
      },
      cause => {
        window.clearInterval(timer);
        if (active) setError(cause);
      },
    );
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, []);
  const load = window.projectStateLoadView
    ? window.projectStateLoadView(error, elapsedAt)
    : {
        phase: error ? "error" : "pending",
        endpoint: "project state",
        httpStatus: null,
        message: error?.message || "",
        elapsedSeconds: 0,
      };
  if (!ready) {
    return <ProjectStateLoadPanel load={load} />;
  }
  return children;
}

function App() {
  const [route, nav] = useHashRoute();
  const canvasView = canvasViewForRoute(route);
  // Storage keys are project-scoped to prevent cross-project filter contamination.
  const PROJECT = window.STATE?.project || "default";
  const SK = {
    filters:   `reckon:${PROJECT}:filters`,
    shipped:   `reckon:${PROJECT}:showShipped`,
    collapsed: `reckon:${PROJECT}:filtersCollapsed`,
    groupBy:   `reckon:${PROJECT}:groupBy`,
    sortDirs:  `reckon:${PROJECT}:sortDirs`,
    archived:  `reckon:${PROJECT}:showArchived`,
    graphHidden: `reckon:${PROJECT}:planGraphHidden`,
  };
  const [filters, setFilters] = useState(() => {
    try { return JSON.parse(localStorage.getItem(SK.filters) || "{}"); } catch { return {}; }
  });
  const [showShipped, setShowShipped] = useState(() => {
    try { return localStorage.getItem(SK.shipped) === "1"; } catch { return false; }
  });
  useEffect(() => {
    try { localStorage.setItem(SK.filters, JSON.stringify(filters)); } catch {}
  }, [filters]);
  useEffect(() => {
    try { localStorage.setItem(SK.shipped, showShipped ? "1" : "0"); } catch {}
  }, [showShipped]);

  // Allow other components (e.g. cockpit milestone tiles) to set filters.
  useEffect(() => {
    const onSet = (e) => setFilters(e.detail || {});
    window.addEventListener("reckon:set-filters", onSet);
    return () => window.removeEventListener("reckon:set-filters", onSet);
  }, []);
  const [promptOpen, setPromptOpen] = useState(false);
  const [filtersHidden, setFiltersHidden] = useState(() => {
    try { return localStorage.getItem(SK.collapsed) === "1"; } catch { return false; }
  });
  const [graphFocal, setGraphFocal] = useState(null);
  // When viewing graph and a plan is clicked in the sidebar, also set graphFocal
  useEffect(() => {
    if (route.view === "plan" && route.slug) setGraphFocal(route.slug);
  }, [route.view, route.slug]);
  // Inline plan-graph: persisted hidden/shown state. Replaces the older
  // Reading/Graph tab toggle — the graph now lives above the report.
  const [graphHidden, setGraphHidden] = useState(() => {
    try { return localStorage.getItem(SK.graphHidden) === "1"; } catch { return false; }
  });
  useEffect(() => {
    try { localStorage.setItem(SK.graphHidden, graphHidden ? "1" : "0"); } catch {}
  }, [graphHidden]);
  const [showArchived, setShowArchived] = useState(() => {
    try { return localStorage.getItem(SK.archived) === "1"; } catch { return false; }
  });
  useEffect(() => {
    try { localStorage.setItem(SK.archived, showArchived ? "1" : "0"); } catch {}
  }, [showArchived]);
  // Inventory revision — bumped whenever a plan is mutated locally so memoised
  // views (filter list, graph) recompute against the updated inventory record.
  const [invRev, setInvRev] = useState(0);
  const bumpInv = useCallback(() => setInvRev(r => r + 1), []);
  useEffect(() => {
    try { localStorage.setItem(SK.collapsed, filtersHidden ? "1" : "0"); } catch {}
  }, [filtersHidden]);
  const [groupBy, setGroupBy] = useState(() => {
    try {
      const stored = localStorage.getItem(SK.groupBy);
      if (!SORT_OPTIONS.some(option => option.value === stored)) return "edited";
      return stored;
    } catch { return "edited"; }
  });
  useEffect(() => {
    try { localStorage.setItem(SK.groupBy, groupBy); } catch {}
  }, [groupBy]);
  const [sortDirs, setSortDirs] = useState(() => {
    try { return JSON.parse(localStorage.getItem(SK.sortDirs) || "{}"); } catch { return {}; }
  });
  useEffect(() => {
    try { localStorage.setItem(SK.sortDirs, JSON.stringify(sortDirs)); } catch {}
  }, [sortDirs]);
  const sortDir = sortDirs[groupBy] ?? SORT_DIR_DEFAULTS[groupBy] ?? "asc";
  const toggleSortDir = () => {
    const next = sortDir === "asc" ? "desc" : "asc";
    setSortDirs(prev => ({ ...prev, [groupBy]: next }));
  };
  const [cmdKOpen, setCmdKOpen] = useState(false);
  const [readingMode, setReadingMode] = useState(false);

  const [projects, setProjects] = useState([]);
  const [fleetRuns, setFleetRuns] = useState([]);
  const [hiddenProjects, setHiddenProjects] = useState(() => {
    try {
      const raw = localStorage.getItem(PROJECT_VISIBILITY_STORAGE);
      if (raw === null) return null;
      const stored = JSON.parse(raw);
      return Array.isArray(stored) ? stored : null;
    } catch { return null; }
  });
  useEffect(() => {
    Promise.all([
      fetch("/_projects/index.json").then(response => response.ok ? response.json() : null),
      fetch("/crew")
        .then(response => response.ok ? response.json() : null)
        .catch(() => ({ runs: [] })),
    ])
      .then(([data, crew]) => {
        setFleetRuns(Array.isArray(crew?.runs) ? crew.runs : []);
        if (!data?.projects) return;
        const liveCounts = (crew?.runs || []).reduce((counts, run) => {
          counts.set(run.project, (counts.get(run.project) || 0) + 1);
          return counts;
        }, new Map());
        setProjects(data.projects.map(project => {
          const state = project.data || {};
          const summary = Array.isArray(state.projects) ? state.projects[0] : null;
          const inventory = Array.isArray(state.inventory) ? state.inventory : [];
          const plans = Array.isArray(state.plans) ? state.plans : inventory;
          return {
            project: project.project,
            accent: summary?.accent || state.accent || window.ACCENTS?.[project.project] || "var(--accent)",
            plans_count: Number(summary?.plans_count ?? state.counts?.total ?? plans.length ?? 0),
            live: liveCounts.has(project.project),
            live_count: liveCounts.get(project.project) || 0,
            state,
          };
        }));
      })
      .catch(() => {});
  }, []);

  const [theme, setTheme] = useState(() => {
    try { return localStorage.getItem("reckon:theme") || "light"; } catch { return "light"; }
  });
  const [density, setDensity] = useState(() => {
    try { return localStorage.getItem("reckon:density") || "comfortable"; } catch { return "comfortable"; }
  });
  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    try { localStorage.setItem("reckon:theme", theme); } catch {}
  }, [theme]);
  useEffect(() => {
    document.documentElement.setAttribute("data-density", density);
    try { localStorage.setItem("reckon:density", density); } catch {}
  }, [density]);

  const navProject = useCallback((destProject) => {
    if (!destProject) {
      window.location.href = "/";
      return;
    }
    const M = window.STATE;
    const currentProject = M?.project || null;
    const isFromProject = !!currentProject;
    let hash = "#cockpit";
    if (isFromProject) {
      if (route.view === "graph") hash = "#graph";
      else if (route.view === "crew") hash = "#crew";
      else if (route.view === "plan") hash = "#plans";
      else if (route.view === "sprint") hash = "#sprints";
      else hash = "#cockpit";
    }
    window.location.href = `/${destProject}/${hash}`;
  }, [route]);

  const toggleProject = useCallback((targetProject) => {
    const currentProject = window.STATE?.project || null;
    const change = projectVisibilityChange(projects, hiddenProjects, currentProject, targetProject);
    if (!change.changed) return;
    setHiddenProjects(change.hidden);
    try { localStorage.setItem(PROJECT_VISIBILITY_STORAGE, JSON.stringify(change.hidden)); } catch {}
    if (change.focus && change.focus !== currentProject) navProject(change.focus);
  }, [projects, hiddenProjects, navProject]);

  const M = window.STATE;
  const items = useMemo(() => {
    if (!M) return [];
    let list = M.inventory.filter(item => (item.type || "plan") === "plan");
    // Archived axis is orthogonal to status. Hide by default unless the user
    // toggled it on OR is explicitly filtering for a specific status.
    if (!showArchived) {
      list = list.filter(p => !(p.archived === "1" || p.archived === true || p.archived === "true"));
    }
    if (filters.status?.length) list = list.filter(p => filters.status.includes(p.effective_status || p.status));
    if (filters.ms?.length) list = list.filter(p => filters.ms.includes(p.ms));
    if (filters.sprint?.length) list = list.filter(p => filters.sprint.includes(p.sprint));
    if ((M.north_stars || []).length && filters.north_star?.length) list = list.filter(p => filters.north_star.includes(p.north_star));
    if (filters.context) {
      const ctx = M.inventory.find(p => p.slug === filters.context);
      if (ctx) {
        const related = new Set([ctx.slug, ...(ctx.depends_on || []), ...(ctx.blocks || [])]);
        list = list.filter(p => related.has(p.slug));
      }
    }
    return list;
    // invRev is included to recompute when a plan's archived/status flips
    // without requiring a full inventory reload.
  }, [M, filters, showArchived, invRev]);

  const readQueue = useMemo(
    () => readingQueue(M, items, groupBy, sortDir),
    [M, items, groupBy, sortDir]
  );
  const searchItems = useMemo(() => paletteItems(M, projects), [M, projects]);
  const shownProjects = useMemo(() => visibleProjectRows(projects, hiddenProjects), [projects, hiddenProjects]);
  const shownProjectNames = useMemo(() => shownProjects.map(project => project.project), [shownProjects]);
  const readPosition = Math.max(0, readQueue.indexOf(route.slug));

  const onSelectPlan = useCallback((slug) => {
    nav({ view: "plan", slug });
  }, [nav]);

  const onSetContext = useCallback((slug) => {
    setFilters(f => ({ ...f, context: slug }));
  }, []);

  useEffect(() => {
    if (!promptOpen) return;
    setPromptOpen(false);
    window.dispatchEvent(new CustomEvent("r-open-prompt"));
  }, [promptOpen]);

  // Cmd/Ctrl+B hides both list columns. Focus keys share this lifecycle so
  // selection and palette state stay owned by App.
  useEffect(() => {
    const onKey = (e) => {
      const editable = e.target?.matches?.("input, textarea, select, [contenteditable='true']");
      if (e.key === "k" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        setCmdKOpen(true);
        return;
      }
      if (e.key === "b" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        if (route.view === "plan") setFiltersHidden(c => !c); // Plans only — hides filter + list cols
        return;
      }
      const canRead = route.view === "plan" && !!route.slug;
      if (e.key === "Escape" && readingMode) {
        e.preventDefault();
        setReadingMode(current => nextReadingMode(current, e.key, canRead));
        return;
      }
      if (editable || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key.toLowerCase() === "f" && canRead) {
        e.preventDefault();
        setReadingMode(current => nextReadingMode(current, e.key, canRead));
        return;
      }
      if (readingMode && (e.key === "ArrowRight" || e.key === "ArrowLeft")) {
        e.preventDefault();
        const next = readingQueueStep(readQueue, route.slug, e.key === "ArrowRight" ? 1 : -1);
        if (next) nav({ view: "plan", slug: next });
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [nav, readQueue, readingMode, route.slug, route.view]);

  useEffect(() => {
    if (route.view !== "plan" || !route.slug) setReadingMode(false);
  }, [route.view, route.slug]);

  return (
    <div className={`r-app ${readingMode ? "r-focus-mode" : ""}`}>
      {!readingMode && <TopBar
          route={route}
          onNav={nav}
          navProject={navProject}
          onOpenCmdK={() => setCmdKOpen(true)}
          filtersHidden={filtersHidden}
          onToggleFilters={() => setFiltersHidden(c => !c)}
          theme={theme}
          setTheme={setTheme}
          density={density}
          setDensity={setDensity}
          projects={projects}
          hiddenProjects={hiddenProjects}
          onToggleProject={toggleProject}
        />}
      {canvasView === "plan" ? (
        <div className={`r-canvas-view r-plans-view ${filtersHidden || readingMode ? "filters-collapsed" : ""} ${readingMode ? "reading-mode" : ""}`}>
          {!readingMode && <FiltersCol filters={filters} setFilters={setFilters} />}
          {!readingMode && <ListCol route={route} onNav={nav} onSelectPlan={onSelectPlan} items={items} sortBy={groupBy} setSortBy={setGroupBy} sortDir={sortDir} toggleSortDir={toggleSortDir} filters={filters} onClearFilters={() => setFilters({})} onClearContext={() => setFilters(f => { const next = {...f}; delete next.context; return next; })} onSetContext={onSetContext} />}
          <div className="r-content" style={readingMode ? { height: "100vh", overflow: "auto" } : undefined}>
            {!readingMode && <TitleBar route={route} onNav={nav} onOpenPrompt={() => setPromptOpen(true)} onPlanMutated={bumpInv} />}
            <div className="r-reader-with-attachments" style={readingMode ? { display: "block" } : undefined}>
              <div className="r-body">
                {!readingMode && <PlanGraphStrip slug={route.slug} onNav={nav} hidden={graphHidden} setHidden={setGraphHidden} />}
                <Plan
                slug={route.slug}
                onNav={nav}
                focusMode={readingMode}
                onToggleFocus={() => setReadingMode(current => !current)}
                focusPosition={{ current: readPosition + 1, total: readQueue.length }}
                onPage={(direction) => {
                  const next = readingQueueStep(readQueue, route.slug, direction);
                  if (next) nav({ view: "plan", slug: next });
                }}
                />
              </div>
              {!readingMode && <AttachmentRail selectedKey={route.slug} onSelect={onSelectPlan} />}
            </div>
          </div>
        </div>
      ) : (
        <div className={`r-canvas-view r-${canvasView}-view`}>
          <div className="r-content">
            <TitleBar route={route} onNav={nav} onOpenPrompt={() => setPromptOpen(true)} onPlanMutated={bumpInv} />
            <div className={`r-reader-with-attachments ${route.view === "cockpit" ? "r-overview-container" : ""}`}>
              <div className={`r-body ${route.view === "cockpit" ? "r-overview-view" : ""}`}>
                {canvasView === "cockpit" && <CockpitBody onNav={nav} projects={shownProjects} fleetRuns={fleetRuns} mountedProjectCount={projects.length} />}
                {canvasView === "sprint" && <><FleetPrompt sprintId={route.sprint} /><Sprint sprintId={route.sprint} onNav={nav} /></>}
                {canvasView === "graph" && <GraphView onNav={nav} items={items} focal={graphFocal} setFocal={setGraphFocal} />}
                {canvasView === "crew" && <CrewView visibleProjects={shownProjectNames} mountedProjectCount={projects.length} />}
              </div>
            </div>
          </div>
        </div>
      )}
      {cmdKOpen && <CmdKPalette items={searchItems} onClose={() => setCmdKOpen(false)} onPick={(result) => {
        setCmdKOpen(false);
        if (result.repository && result.repository !== M?.project) {
          window.location.href = `/${result.repository}/#plan/${encodeURIComponent(result.nav_key)}`;
        } else {
          nav({ view: "plan", slug: result.nav_key });
        }
      }} />}
    </div>
  );
}

function CmdKPalette({ items, onClose, onPick }) {
  const [q, setQ] = useState("");
  const [idx, setIdx] = useState(0);
  const inputRef = useRef(null);
  useEffect(() => { inputRef.current?.focus(); }, []);
  const filtered = useMemo(() => {
    if (!q.trim()) return items.slice(0, 30);
    const needle = q.toLowerCase();
    return items.filter(p =>
      p.label?.toLowerCase().includes(needle) ||
      p.slug?.toLowerCase().includes(needle) ||
      p.kind?.toLowerCase().includes(needle) ||
      p.repository?.toLowerCase().includes(needle) ||
      p.status?.toLowerCase().includes(needle) ||
      (p.ms || "").toLowerCase().includes(needle) ||
      (p.summary || "").toLowerCase().includes(needle)
    ).slice(0, 30);
  }, [q, items]);
  useEffect(() => { setIdx(0); }, [q]);
  useEffect(() => {
    const onKey = (e) => {
      if (e.key === "Escape") onClose();
      if (e.key === "ArrowDown") { e.preventDefault(); setIdx(i => Math.min(filtered.length - 1, i + 1)); }
      if (e.key === "ArrowUp")   { e.preventDefault(); setIdx(i => Math.max(0, i - 1)); }
      if (e.key === "Enter" && filtered[idx]) onPick(filtered[idx]);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [filtered, idx, onClose, onPick]);
  return (
    <div className="r-cmdk-scrim" onMouseDown={onClose}>
      <div className="r-cmdk" onMouseDown={(e) => e.stopPropagation()}>
        <input ref={inputRef} placeholder="Search plans, research and evidence across projects…" value={q} onChange={(e) => setQ(e.target.value)} />
        <div className="list">
          {filtered.map((p, i) => (
            <button key={`${p.repository}:${p.nav_key}`} className={`item ${i === idx ? "on" : ""}`} onMouseEnter={() => setIdx(i)} onClick={() => onPick(p)}>
              <span className={`dot ${p.status}`}></span>
              <span><strong>{p.label}</strong> <span className="meta" style={{ marginLeft: 6 }}>/{p.nav_key}</span></span>
              <span className="meta">{p.kind} · {p.repository} · {p.status}</span>
            </button>
          ))}
          {filtered.length === 0 && <div style={{ padding: 24, textAlign: "center", color: "var(--muted)", fontSize: 13 }}>No resources match.</div>}
        </div>
        <div className="r-cmdk-foot">
          <span>↑↓ navigate</span><span>↵ open</span><span>esc close</span>
        </div>
      </div>
    </div>
  );
}

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
  const project = M.projects?.[0] || { project: M.project || "", milestones: M.milestones || [] };
  const northStars = M.north_stars || [];

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

      <div className="r-ck-h">
        <span className="r-eyebrow">Milestones</span>
      </div>
      <div className="r-ms" style={{ marginBottom: 4 }}>
        {project.milestones.map(m => (
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

function FleetPrompt({ sprintId }) {
  const M = window.STATE;
  const sprint = M.sprints.find(s => s.id === sprintId);
  const [open, setOpen] = useState(false);
  const [text, setText] = useState(null);

  useEffect(() => {
    const h = () => { setText(null); setOpen(true); };
    window.addEventListener("r-open-fleet-prompt", h);
    return () => window.removeEventListener("r-open-fleet-prompt", h);
  }, []);

  // Build via the shared builder (same format as the single-plan button).
  // Hydrate first — the /_discover inventory has no decisions/followups, so
  // without this every section would show "(none)" decisions + no handoff brief.
  useEffect(() => {
    if (!open || !sprint) return;
    let alive = true;
    const slugSet = new Set(sprint.items.map(it => typeof it === "string" ? it : it.slug));
    const items = [...slugSet].map(slug => {
      const p = M.inventory.find(x => x.slug === slug);
      const meta = sprint.items.find(it => (typeof it === "string" ? it : it.slug) === slug);
      const just = typeof meta === "object" ? meta.justification : null;
      return p ? { ...p, justification: just } : null;
    }).filter(Boolean);
    const win = (sprint.starts || "") + (sprint.ends ? " → " + sprint.ends : "");
    const opts = { sprint: { id: sprint.id, window: win } };
    Promise.resolve(
      window.buildFleetPromptAsync
        ? window.buildFleetPromptAsync(items, M, sprint.theme, opts)
        : window.buildFleetPrompt(items, M, sprint.theme, opts)
    ).then(t => { if (alive) setText(t); });
    return () => { alive = false; };
  }, [open, sprintId]);

  if (!open || !sprint) return null;
  if (text == null) {
    return (
      <div className="r-modal-scrim" onClick={() => setOpen(false)}>
        <div className="r-modal" onClick={(e) => e.stopPropagation()}>
          <div className="head">
            <h3 style={{ margin: 0, fontSize: 16 }}>Generating fleet prompt…</h3>
            <button className="btn ghost" onClick={() => setOpen(false)}>Close · Esc</button>
          </div>
        </div>
      </div>
    );
  }
  return (
    <PromptModalAdHoc
      title={`Fleet · ${sprint.id}`}
      subtitle={`Orchestrate ${sprint.items.length} plan(s) — sequence honours depends_on`}
      buildText={() => text}
      onClose={() => setOpen(false)}
    />
  );
}

function PromptModalAdHoc({ title, subtitle, buildText, onClose }) {
  const [text, setText] = useState(() => buildText());
  useEffect(() => {
    const k = (e) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", k);
    return () => document.removeEventListener("keydown", k);
  }, [onClose]);
  const copy = () => {
    navigator.clipboard?.writeText(text);
    onClose();
    if (window.flashSaved) window.flashSaved("prompt copied");
  };
  return (
    <div className="r-modal-scrim" onClick={onClose}>
      <div className="r-modal" onClick={(e) => e.stopPropagation()}>
        <div className="head">
          <div>
            <div style={{ fontFamily: "var(--mono)", fontSize: 10.5, letterSpacing: "0.10em", textTransform: "uppercase", color: "var(--accent)", fontWeight: 600 }}>{title}</div>
            <h3 style={{ margin: "4px 0", fontSize: 17, fontWeight: 600 }}>{subtitle}</h3>
          </div>
          <button className="btn ghost" onClick={onClose}>Close · Esc</button>
        </div>
        <textarea value={text} onChange={(e) => setText(e.target.value)} spellCheck={false} />
        <div className="foot">
          <span style={{ color: "var(--muted)", fontFamily: "var(--mono)", fontSize: 11 }}>{text.length} chars</span>
          <span style={{ flex: 1 }}></span>
          <button className="btn primary" onClick={copy}>Copy to clipboard</button>
        </div>
      </div>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(
  <ReadyGate><App /></ReadyGate>
);
