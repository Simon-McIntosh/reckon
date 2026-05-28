// Reckon shell — top bar + three-column body.
// Top bar: brand · sp · screen tabs (Cockpit / Sprint).
// Body: filters · plans list · content (with two-row title bar).

function parseHash() {
  const h = (window.location.hash || "").replace(/^#/, "");
  if (!h || h === "cockpit") return { view: "cockpit" };
  if (h.startsWith("plan/")) return { view: "plan", slug: decodeURIComponent(h.slice(5)) };
  if (h.startsWith("sprint/")) return { view: "sprint", sprint: decodeURIComponent(h.slice(7)) };
  if (h === "graph") return { view: "graph" };
  if (h === "plans") return { view: "plan", slug: null };
  if (h === "sprints") return { view: "sprint", sprint: null };
  return { view: "cockpit" };
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
  }, []);
  return [route, nav];
}

// ─── Top bar ────────────────────────────────────────────────────────────

function TopBar({ route, onNav, navProject, onOpenCmdK, filtersHidden, onToggleFilters, theme, setTheme, density, setDensity, projects }) {
  const M = window.STATE;
  const view = route.view;
  const currentProject = M?.project
    || (typeof document !== "undefined" && document.querySelector('meta[name="docs-project"]')?.content)
    || null;

  // Assign window globals to local vars so JSX can use them as components
  const PP = window.ProjectPicker;
  const SM = window.SettingsMenu;

  const goPlans = () => {
    const target = M?.inventory?.find(p => p.status === "active") || M?.inventory?.[0];
    if (target) onNav({ view: "plan", slug: target.slug });
  };
  const goSprints = () => {
    const id = M?.active_sprint_id || M?.sprint?.id || M?.sprints?.[0]?.id;
    if (id) onNav({ view: "sprint", sprint: id });
  };

  return (
    <div className="r-topbar">
      {PP ? (
        <PP current={currentProject} projects={projects} onNav={navProject} />
      ) : (
        <div className="brand" onClick={() => navProject(null)} style={{ cursor: "pointer" }}>
          <span className="name">{currentProject || "fleet"}</span>
        </div>
      )}
      <button className="r-cmdk-trigger" onClick={onOpenCmdK} title="Search plans · ⌘K">
        <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
          <circle cx="7" cy="7" r="4.5"/>
          <path d="M13 13l-2.5-2.5"/>
        </svg>
        <span>Search</span>
        <span className="kbd">⌘K</span>
      </button>
      <span className="sp"></span>
      <div className="r-glyph-tabs">
        <button className={`r-glyph ${view === "cockpit" ? "active" : ""}`} onClick={() => onNav({ view: "cockpit" })} title="Overview">
          <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
            <path d="M2 12 V8"/><path d="M5.5 12 V5"/><path d="M9 12 V9"/><path d="M12.5 12 V3"/>
            <circle cx="12.5" cy="3" r="1.2" fill="currentColor" stroke="none"/>
          </svg>
          Overview
        </button>
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
      </div>
      <div className="top-r">
        {SM ? (
          <SM theme={theme} setTheme={setTheme} density={density} setDensity={setDensity} />
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
    </div>
  );
}

// ─── Filters column ─────────────────────────────────────────────────────

function FiltersCol({ filters, setFilters, showShipped, setShowShipped }) {
  const M = window.STATE;
  const milestones = M.projects?.[0]?.milestones || M.milestones || [];
  const sprints = M.sprints || [];

  const toggle = (group, value) => {
    setFilters(f => {
      // Single-select per group: clicking the same value clears it; another value replaces.
      const cur = (f[group] || []);
      if (cur.includes(value)) return { ...f, [group]: [] };
      return { ...f, [group]: [value] };
    });
  };

  const anyActive = (filters.status?.length || 0) + (filters.ms?.length || 0) + (filters.sprint?.length || 0) + (filters.type?.length || 0) > 0;

  // Sprints that have plans in inventory
  const sprintsWithPlans = sprints.filter(s => M.inventory.some(p => p.sprint === s.id));

  // Dynamic type counts — shows all types that appear in inventory
  const typeCounts = {};
  for (const p of M.inventory) {
    const t = p.type || "plan";
    typeCounts[t] = (typeCounts[t] || 0) + 1;
  }
  const allTypes = Object.entries(typeCounts).sort((a, b) => a[0].localeCompare(b[0]));
  const showTypeFilter = allTypes.length > 1;

  return (
    <aside className="r-filters">
      {anyActive && (
        <button className="r-clear-top" onClick={() => setFilters({})}>
          × clear filters
        </button>
      )}

      <div className="r-filter-group">
        <div className="r-filter-h">Status</div>
        {["active", "blocked", "pending", "shipped"].map(s => {
          const n = M.inventory.filter(p => p.status === s).length;
          const on = (filters.status || []).includes(s);
          if (n === 0) return null;
          return (
            <div key={s} className={`r-chip ${on ? "on" : ""}`} onClick={() => toggle("status", s)}>
              <span className={`dot ${s}`}></span>
              <span style={{ textTransform: "capitalize" }}>{s}</span>
              <span className="n">{n}</span>
            </div>
          );
        })}
      </div>

      {sprintsWithPlans.length > 0 && (
        <div className="r-filter-group">
          <div className="r-filter-h">Sprint</div>
          {sprintsWithPlans.map(s => {
            const n = M.inventory.filter(p => p.sprint === s.id).length;
            const on = (filters.sprint || []).includes(s.id);
            return (
              <div key={s.id} className={`r-chip r-chip-sprint ${on ? "on" : ""}`} onClick={() => toggle("sprint", s.id)}>
                <span className="r-chip-sprint-id">{s.id}</span>
                <span className="r-chip-sprint-theme">{s.theme}</span>
                <span className="n">{n}</span>
              </div>
            );
          })}
        </div>
      )}

      <div className="r-filter-group">
        <div className="r-filter-h">Milestone</div>
        {milestones.map(m => {
          const n = M.inventory.filter(p => p.ms === m.id).length;
          const on = (filters.ms || []).includes(m.id);
          if (n === 0) return null;
          return (
            <div key={m.id} className={`r-chip ${on ? "on" : ""}`} onClick={() => toggle("ms", m.id)}>
              <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: m.status === "active" ? "var(--accent)" : m.status === "shipped" ? "var(--good)" : "var(--muted)", minWidth: 22 }}>{m.id}</span>
              <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flex: 1, minWidth: 0 }}>{m.name}</span>
              <span className="n">{n}</span>
            </div>
          );
        })}
      </div>

      {showTypeFilter && (
        <div className="r-filter-group">
          <div className="r-filter-h">Type</div>
          {allTypes.map(([key, count]) => {
            const on = (filters.type || []).includes(key);
            return (
              <div key={key} className={`r-chip r-chip-type r-chip-type-${key} ${on ? "on" : ""}`} onClick={() => toggle("type", key)}>
                <span className="r-chip-type-label">{key}</span>
                <span className="n">{count}</span>
              </div>
            );
          })}
        </div>
      )}
    </aside>
  );
}

// ─── Plans list column ──────────────────────────────────────────────────

const STATUS_ORDER = { blocked: 0, active: 1, "in-progress": 1, pending: 2, shipped: 3, done: 4, draft: 5 };

// Default direction per sort key: "asc" = smallest/earliest/A first.
const SORT_DIR_DEFAULTS = { edited: "desc", created: "desc", status: "asc", progress: "desc", title: "asc" };

function sortItems(items, sortBy, dir) {
  const arr = [...items];
  const m = dir === "asc" ? 1 : -1;
  if (sortBy === "status") {
    arr.sort((a, b) => m * ((STATUS_ORDER[a.status] ?? 9) - (STATUS_ORDER[b.status] ?? 9)));
  } else if (sortBy === "progress") {
    arr.sort((a, b) => m * ((a.impl || 0) - (b.impl || 0)));
  } else if (sortBy === "title") {
    arr.sort((a, b) => m * (a.title || "").localeCompare(b.title || ""));
  } else if (sortBy === "created") {
    arr.sort((a, b) => {
      if (!a.created && !b.created) return 0;
      if (!a.created) return 1;
      if (!b.created) return -1;
      return m * a.created.localeCompare(b.created);
    });
  } else {
    // edited / recent (default): sort by last modified; undated items go to end
    arr.sort((a, b) => {
      if (!a.last && !b.last) return 0;
      if (!a.last) return 1;
      if (!b.last) return -1;
      return m * a.last.localeCompare(b.last);
    });
  }
  return arr;
}

function ListCol({ route, onNav, onSelectPlan, items, sortBy, setSortBy, sortDir, toggleSortDir, filters, onClearFilters, onClearContext }) {
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

  return (
    <div className="r-list">
      {contextSlug && (
        <div className="r-context-chip">
          <span className="r-context-label">⟳ Related to: <code>{contextSlug}</code></span>
          <button className="r-context-x" onClick={onClearContext} title="Clear context filter">×</button>
        </div>
      )}

      <div className="r-sort-bar">
        <span className="r-sort-n">{items.length}</span>
        <select className="r-sort-select" value={sortBy} onChange={e => setSortBy(e.target.value)}>
          <option value="edited">Edited</option>
          <option value="created">Created</option>
          <option value="status">Status</option>
          <option value="progress">Progress</option>
          <option value="title">Title</option>
        </select>
        <button
          className={`r-sort-dir ${sortDir !== (SORT_DIR_DEFAULTS[sortBy] || "asc") ? "active" : ""}`}
          onClick={toggleSortDir}
          title={sortDir === "asc" ? "Ascending" : "Descending"}
        >
          {sortDir === "asc" ? <SortAscIcon /> : <SortDescIcon />}
        </button>
      </div>

      {items.length === 0 ? (
        <div className="r-list-empty">
          No plans match.
          {(filters?.status?.length || filters?.ms?.length || filters?.sprint?.length || filters?.type?.length) && (
            <button className="r-clear-btn" onClick={onClearFilters}>Clear filters</button>
          )}
        </div>
      ) : sorted.map(p => {
        const active = route.view === "plan" && route.slug === p.slug;
        return (
          <div
            key={p.slug}
            className={`r-row ${active ? "active" : ""}`}
            onClick={() => onSelectPlan(p.slug)}
          >
            <span className={`dot ${p.status}`}></span>
            <div>
              <div className="t">{p.title}{(p.type && p.type !== "plan") && <span className={`r-type-pill ${p.type}`}>{p.type}</span>}</div>
              <div className="meta">
                <span className="ms">{p.ms}</span>
                <span className="sp">·</span>
                <span className="pct">{Math.round((p.impl || 0) * 100)}%</span>
                <span className="sp">·</span>
                <span>{p.last}</span>
              </div>
              {((p.dec_open || 0) > 0 || (p.blockers || 0) > 0) && (
                <div className="sigs">
                  {(p.dec_open || 0) > 0 && <span className="sig dec">D {p.dec_open}</span>}
                  {(p.blockers || 0) > 0 && <span className="sig blk">! {p.blockers}</span>}
                </div>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ─── Title bar ──────────────────────────────────────────────────────────

function TitleBar({ route, onNav, onOpenPrompt }) {
  const M = window.STATE;
  if (route.view === "cockpit") {
    return null;
  }
  if (route.view === "plan") {
    const p = M.inventory.find(x => x.slug === route.slug);
    if (!p) return null;
    const openDecs = p.dec_open || 0;
    const blockedByDecisions = openDecs > 0;
    return (
      <div className="r-titlebar">
        <div className="row1">
          <span className="crumbs"><code>/{route.slug}</code></span>
          <span className="title">{p.title}</span>
          <div className="actions">
            {blockedByDecisions && (
              <button
                className="resolve-btn"
                onClick={() => {
                  const el = document.querySelector(".r-dec:not(.taken)") || document.querySelector(".r-dec");
                  if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
                }}
                title="Take the next open decision"
              >
                Resolve <span className="resolve-badge">{openDecs}</span>
              </button>
            )}
            <button
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
            </button>
          </div>
        </div>
        <div className="row2">
          <span className={`status-pill ${p.status}`}><span className="dot"></span><span>{p.status}</span></span>
          <span className="dot-sep">·</span>
          <span className="meta-item"><span className="k">ms</span><span className="v">{p.ms}</span></span>
          {p.sprint && <>
            <span className="dot-sep">·</span>
            <span className="meta-item"><span className="k">sprint</span><a className="v" href={`#sprint/${p.sprint}`} style={{ borderBottom: "1px dotted var(--line)" }}>{p.sprint}</a></span>
          </>}
          <span className="dot-sep">·</span>
          <span className="meta-item"><span className="k">progress</span><span className="v">{Math.round((p.impl || 0) * 100)}%</span></span>
          <span className="dot-sep">·</span>
          <span className="meta-item"><span className="k">last</span><span className="v">{p.last}</span></span>
          {p.owner && <>
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
            <button className={`resolve-btn ${blocked ? "" : "done"}`} onClick={handleResolve} title={blocked ? "Take the next open decision" : "All decisions taken"}>
              Resolve <span className="resolve-badge">{totalOpen}</span>
            </button>
          </div>
        )}
      </div>
    );
  }
  return null;
}

// ─── Dependency strip (top of plan body) ────────────────────────────────

function PlanDeps({ slug }) {
  const M = window.STATE;
  const p = M.inventory.find(x => x.slug === slug);
  if (!p) return null;
  const deps = p.depends_on || [];
  const blocks = p.blocks || [];
  if (deps.length === 0 && blocks.length === 0) return null;
  return (
    <div className="r-deps">
      {deps.length > 0 && <>
        <span className="lbl">depends on</span>
        {deps.map(s => {
          const target = M.inventory.find(i => i.slug === s);
          const blocked = target?.status === "blocked";
          return <a key={s} className={`pill ${blocked ? "blocked" : ""}`} href={`#plan/${s}`}>{s}</a>;
        })}
      </>}
      {blocks.length > 0 && <>
        {deps.length > 0 && <span className="lbl" style={{ marginLeft: 14 }}>blocks</span>}
        {deps.length === 0 && <span className="lbl">blocks</span>}
        {blocks.map(s => <a key={s} className="pill" href={`#plan/${s}`}>{s}</a>)}
      </>}
    </div>
  );
}

// ─── App ────────────────────────────────────────────────────────────────

function ReadyGate({ children }) {
  const [ready, setReady] = useState(false);
  useEffect(() => {
    if (window.STATE_READY) window.STATE_READY.then(() => setReady(true));
    else setReady(true);
  }, []);
  if (!ready) {
    return <div style={{ padding: 48, textAlign: "center", fontFamily: "var(--mono)", fontSize: 13, color: "var(--muted)" }}>Loading plan state…</div>;
  }
  return children;
}

function App() {
  const [route, nav] = useHashRoute();
  // Storage keys are project-scoped to prevent cross-project filter contamination.
  const PROJECT = window.STATE?.project || "default";
  const SK = {
    filters:   `reckon:${PROJECT}:filters`,
    shipped:   `reckon:${PROJECT}:showShipped`,
    collapsed: `reckon:${PROJECT}:filtersCollapsed`,
    groupBy:   `reckon:${PROJECT}:groupBy`,
    sortDirs:  `reckon:${PROJECT}:sortDirs`,
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
  useEffect(() => {
    try { localStorage.setItem(SK.collapsed, filtersHidden ? "1" : "0"); } catch {}
  }, [filtersHidden]);
  const [groupBy, setGroupBy] = useState(() => {
    try {
      const stored = localStorage.getItem(SK.groupBy);
      if (!stored || stored === "sprint" || stored === "recent") return "edited";
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

  const [projects, setProjects] = useState([]);
  useEffect(() => {
    fetch("/_projects/index.json")
      .then(r => r.ok ? r.json() : null)
      .then(data => {
        if (!data?.projects) return;
        setProjects(data.projects.map(p => ({
          project: p.project,
          accent: p.data?.accent || window.ACCENTS?.[p.project] || "var(--accent)",
          plans_count: p.data?.counts?.total || 0,
        })));
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
      else if (route.view === "plan") hash = "#plans";
      else if (route.view === "sprint") hash = "#sprints";
      else hash = "#cockpit";
    }
    window.location.href = `/${destProject}/${hash}`;
  }, [route]);

  const M = window.STATE;
  const items = useMemo(() => {
    if (!M) return [];
    let list = M.inventory;
    if (filters.status?.length) list = list.filter(p => filters.status.includes(p.status));
    if (filters.ms?.length) list = list.filter(p => filters.ms.includes(p.ms));
    if (filters.sprint?.length) list = list.filter(p => filters.sprint.includes(p.sprint));
    if (filters.type?.length) {
      list = list.filter(p => {
        const t = p.type || "plan";
        return filters.type.includes(t);
      });
    }
    if (filters.context) {
      const ctx = M.inventory.find(p => p.slug === filters.context);
      if (ctx) {
        const related = new Set([ctx.slug, ...(ctx.depends_on || []), ...(ctx.blocks || [])]);
        list = list.filter(p => related.has(p.slug));
      }
    }
    return list;
  }, [M, filters]);

  const onSelectPlan = useCallback((slug) => {
    nav({ view: "plan", slug });
    setFilters(f => ({ ...f, context: slug }));
  }, [nav]);

  useEffect(() => {
    if (!promptOpen) return;
    setPromptOpen(false);
    window.dispatchEvent(new CustomEvent("r-open-prompt"));
  }, [promptOpen]);

  // Cmd/Ctrl+B — hides both filter + list columns; Plans view only
  useEffect(() => {
    const onKey = (e) => {
      if (e.key === "k" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        setCmdKOpen(true);
      }
      if (e.key === "b" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        if (route.view === "plan") setFiltersHidden(c => !c); // Plans only — hides filter + list cols
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [route.view]);

  return (
    <div className="r-app">
      <TopBar
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
        />
      <div className={`r-3col ${filtersHidden ? "filters-collapsed" : ""} ${(route.view === "cockpit" || route.view === "sprint") ? "overview-mode" : ""}`}>
        <button
          className="r-filter-handle"
          onClick={() => setFiltersHidden(c => !c)}
          title={filtersHidden ? "Show filters · ⌘B" : "Hide filters · ⌘B"}
          aria-label="Toggle filters"
        >
          <span></span><span></span>
        </button>
        <FiltersCol filters={filters} setFilters={setFilters} showShipped={showShipped} setShowShipped={setShowShipped} />
        <ListCol route={route} onNav={nav} onSelectPlan={onSelectPlan} items={items} sortBy={groupBy} setSortBy={setGroupBy} sortDir={sortDir} toggleSortDir={toggleSortDir} filters={filters} onClearFilters={() => setFilters({})} onClearContext={() => setFilters(f => { const next = {...f}; delete next.context; return next; })} />
        <div className="r-content">
          <TitleBar route={route} onNav={nav} onOpenPrompt={() => setPromptOpen(true)} />
          <div className="r-body">
            {route.view === "sprint" && <FleetPrompt sprintId={route.sprint} />}
            {route.view === "plan" && <PlanDeps slug={route.slug} />}
            {route.view === "cockpit" && <CockpitBody onNav={nav} />}
            {route.view === "plan" && <Plan slug={route.slug} onNav={nav} />}
            {route.view === "sprint" && <Sprint sprintId={route.sprint} onNav={nav} />}
            {route.view === "graph" && <GraphView onNav={nav} items={items} focal={graphFocal} setFocal={setGraphFocal} />}
          </div>
        </div>
      </div>
      {cmdKOpen && <CmdKPalette items={M?.inventory || []} onClose={() => setCmdKOpen(false)} onPick={(slug) => { setCmdKOpen(false); nav({ view: "plan", slug }); }} />}
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
      p.title?.toLowerCase().includes(needle) ||
      p.slug?.toLowerCase().includes(needle) ||
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
      if (e.key === "Enter" && filtered[idx]) onPick(filtered[idx].slug);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [filtered, idx, onClose, onPick]);
  return (
    <div className="r-cmdk-scrim" onMouseDown={onClose}>
      <div className="r-cmdk" onMouseDown={(e) => e.stopPropagation()}>
        <input ref={inputRef} placeholder="Search plans by title, slug, milestone…" value={q} onChange={(e) => setQ(e.target.value)} />
        <div className="list">
          {filtered.map((p, i) => (
            <button key={p.slug} className={`item ${i === idx ? "on" : ""}`} onMouseEnter={() => setIdx(i)} onClick={() => onPick(p.slug)}>
              <span className={`dot ${p.status}`}></span>
              <span><strong>{p.title}</strong> <span className="meta" style={{ marginLeft: 6 }}>/{p.slug}</span></span>
              <span className="meta">{p.ms || "—"} · {Math.round((p.impl || 0) * 100)}%</span>
            </button>
          ))}
          {filtered.length === 0 && <div style={{ padding: 24, textAlign: "center", color: "var(--muted)", fontSize: 13 }}>No plans match.</div>}
        </div>
        <div className="r-cmdk-foot">
          <span>↑↓ navigate</span><span>↵ open</span><span>esc close</span>
        </div>
      </div>
    </div>
  );
}

// Leaner cockpit body — plan list is in column 2 so cockpit is project-only.
function CockpitBody({ onNav }) {
  const M = window.STATE;
  if (!M) return null;
  const project = M.projects?.[0] || { project: M.project || "", milestones: M.milestones || [] };
  const allSprints = M.sprints || [];
  const [ckSprintIdx, setCkSprintIdx] = useState(() => {
    const i = allSprints.findIndex(s => s.id === M.active_sprint_id);
    return i >= 0 ? i : 0;
  });
  const sprint = allSprints[ckSprintIdx] || M.sprint;

  const decisionPlans = M.inventory
    .filter(i => (i.dec_open || 0) > 0)
    .sort((a, b) => (b.dec_open || 0) - (a.dec_open || 0));
  const decisionTotal = decisionPlans.reduce((n, p) => n + (p.dec_open || 0), 0);
  const planSlugsWithDecs = new Set(decisionPlans.map(p => p.slug));
  const decsByMs = {};
  for (const p of decisionPlans) decsByMs[p.ms] = (decsByMs[p.ms] || 0) + (p.dec_open || 0);
  const decsByRoi = { high: 0, mid: 0, low: 0 };
  for (const p of decisionPlans) decsByRoi[p.roi || "mid"] = (decsByRoi[p.roi || "mid"] || 0) + (p.dec_open || 0);

  return (
    <>
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
        <span className="r-eyebrow">Sprint {sprint?.id} · {sprint?.theme}</span>
        <div className="r-ck-h-actions">
          <button className="r-nav-btn" disabled={ckSprintIdx <= 0} onClick={() => setCkSprintIdx(i => i - 1)}>‹</button>
          <button className="r-nav-btn" disabled={ckSprintIdx >= allSprints.length - 1} onClick={() => setCkSprintIdx(i => i + 1)}>›</button>
          <a className="r-board-icon" href={`#sprint/${sprint?.id}`} title="Sprints">
            <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
              <rect x="2.5" y="3" width="3" height="10" rx="0.6"/>
              <rect x="6.5" y="3" width="3" height="10" rx="0.6"/>
              <rect x="10.5" y="3" width="3" height="10" rx="0.6"/>
            </svg>
          </a>
        </div>
      </div>
      <div className="r-ck-list" style={{ marginBottom: 22 }}>
        {(sprint?.items || []).map(it => {
          const slug = typeof it === "string" ? it : it.slug;
          const justification = typeof it === "object" ? it.justification : null;
          const p = M.inventory.find(x => x.slug === slug);
          if (!p) return null;
          const pct = Math.round((p.impl || 0) * 100);
          return (
            <a key={slug} className="r-ck-row" href={`#plan/${slug}`}>
              <span className={`r-ck-dot ${p.status}`}></span>
              <div className="r-ck-body">
                <div className="r-ck-title">{p.title}</div>
                {justification && <div className="r-ck-just">{justification}</div>}
              </div>
              <div className="r-ck-prog">
                <span className="r-ck-bar"><i style={{ width: `${pct}%` }} className={p.status === "shipped" ? "shipped" : p.status === "blocked" ? "blocked" : ""}></i></span>
                <span className="r-ck-pct">{pct}%</span>
              </div>
              <span className="r-ck-arr">›</span>
            </a>
          );
        })}
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

  useEffect(() => {
    const h = () => setOpen(true);
    window.addEventListener("r-open-fleet-prompt", h);
    return () => window.removeEventListener("r-open-fleet-prompt", h);
  }, []);

  if (!sprint) return null;

  const slugSet = new Set(sprint.items.map(it => typeof it === "string" ? it : it.slug));
  const itemsArr = [...slugSet].map(slug => {
    const p = M.inventory.find(x => x.slug === slug);
    const meta = sprint.items.find(it => (typeof it === "string" ? it : it.slug) === slug);
    const just = typeof meta === "object" ? meta.justification : null;
    return p ? { ...p, justification: just } : null;
  }).filter(Boolean);

  const order = [];
  const visited = new Set();
  const visit = (p) => {
    if (visited.has(p.slug)) return;
    visited.add(p.slug);
    for (const dep of (p.depends_on || [])) {
      const depPlan = itemsArr.find(x => x.slug === dep);
      if (depPlan) visit(depPlan);
    }
    order.push(p);
  };
  for (const p of itemsArr) visit(p);

  const buildPrompt = () => {
    const proj = M.projects?.[0]?.project || M.project || "project";
    let txt = `Orchestration\n  You are coordinating a fleet of workers across ${order.length} plans in a single\n  sprint. Dispatch in the order below; honour the dependency edges. Workers\n  whose dependencies are satisfied may run in parallel. Each worker must read\n  every plan it depends on in full, develop the plan further as it works,\n  inspect code under the project repo when ambiguous, honour locked decisions,\n  and never resolve open decisions unilaterally.\n\nProject: ${proj}\nSprint:  ${sprint.id}\nGoal:    ${sprint.theme}\nWindow:  ${sprint.starts} → ${sprint.ends}\n\nExecution sequence (resolved from depends_on within the sprint):\n`;
    order.forEach((p, i) => {
      txt += `  ${i + 1}. ${p.slug}${(p.depends_on || []).length ? "  (← " + p.depends_on.join(", ") + ")" : ""}\n`;
    });
    txt += `\nEach plan's individual prompt follows below as a numbered section.\n\n`;
    order.forEach((p, i) => {
      txt += `\n─── ${i + 1}/${order.length} · ${p.slug} ───\n`;
      const decisions = (p.decisions || []);
      const locked = decisions.filter(d => (d.chosen || d.choice));
      const openD = decisions.filter(d => !(d.chosen || d.choice));
      const lockedBlock = locked.length === 0 ? "  (none)" : locked.map(d => `  ${d.key} → ${d.chosen || d.choice}`).join("\n");
      const openBlock = openD.length === 0 ? "  (none)" : openD.map(d => `  ${d.key} — ${d.title}`).join("\n");
      const next = (p.followups || [])[0];
      const comments = (p.comments) || (window.reckon.planLoad?.(p.slug)?.comments) || {};
      const commentEntries = Object.entries(comments).filter(([_, arr]) => (arr || []).length > 0);
      const commentsBlock = commentEntries.length === 0 ? "  (none)" :
        commentEntries.map(([sid, arr]) =>
          arr.map(c =>
            `  §${sid} · ${c.who} · ${c.when}\n` +
            (c.quote ? `      quote: "${c.quote.length > 200 ? c.quote.slice(0, 200) + "…" : c.quote}"\n` : "") +
            `      body: ${c.body}`
          ).join("\n")
        ).join("\n");
      txt += `Plan: ${p.slug}\nStatus: ${p.status} · ${p.phase || ""}\nJustification (sprint): ${p.justification || "—"}\n\nPlan page to read\n  ${proj}/${p.slug}.html\n\nLocked decisions to honour\n${lockedBlock}\n\nOpen decisions to surface\n${openBlock}\n\nComments (anchored to sections)\n${commentsBlock}\n\nNext-up\n  ${next?.title || "—"}\n  ${next?.body || ""}\n\nDone-when\n  1. Land the work this prompt describes.\n  2. POST a followup to ${p.slug}.json#followups.\n  3. Mark the current followup resolved.\n`;
    });
    return txt;
  };

  if (!open) return null;
  return (
    <PromptModalAdHoc
      title={`Fleet · ${sprint.id}`}
      subtitle={`Orchestrate ${order.length} workers — sequence honours depends_on`}
      buildText={buildPrompt}
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
