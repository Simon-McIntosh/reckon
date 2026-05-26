// Shell — top bar + three-column body.
// Brand: reckon (the system). Project name comes from state.
// Changes from earlier versions:
//   A. Brand shows "reckon · <projectName>" (dynamic from meta tag / STATE)
//   B. Fleet prompts use dynamic project name
//   C. "Decisions N" button cycles through blocked plans (sprint titlebar)
//   D. Sidebar collapse (⌘B handle) collapses BOTH filter + plan list → full-screen
//   E. Sprint nav (‹ ›) also appears in the cockpit/overview view
//   F. Graph route (#graph) added to hash router
//   G. Cmd-K / ⌘K palette for fast plan search
//   H. showShipped toggle + filters/groupBy/filtersCollapsed persisted to localStorage

const { useState, useEffect, useMemo, useRef, useCallback } = React;

function parseHash() {
  const h = (window.location.hash || "").replace(/^#/, "");
  if (!h || h === "cockpit") return { view: "cockpit" };
  if (h.startsWith("plan/")) return { view: "plan", slug: decodeURIComponent(h.slice(5)) };
  if (h.startsWith("sprint/")) return { view: "sprint", sprint: decodeURIComponent(h.slice(7)) };
  if (h === "graph") return { view: "graph" };
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

function AppTopBar({ route, onNav, sidebarCollapsed, onToggleSidebar, onOpenCmdK }) {
  const M = window.STATE;
  const view = route.view;
  const projectName = M?.projects?.[0]?.project ||
    document.querySelector('meta[name="docs-project"]')?.content || "";

  const goPlans = () => {
    const target = M?.inventory?.find(p => p.status === "active") || M?.inventory?.[0];
    if (target) onNav({ view: "plan", slug: target.slug });
  };
  const goSprints = () => {
    const id = M?.active_sprint_id || M?.sprint?.id || M?.sprints?.[0]?.id;
    if (id) onNav({ view: "sprint", sprint: id });
  };

  return (
    <div className="plan-topbar">
      <button
        className="sidebar-toggle-btn"
        onClick={onToggleSidebar}
        title={`${sidebarCollapsed ? "Show" : "Hide"} sidebar · ⌘B`}
        aria-pressed={!sidebarCollapsed}
      >
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
          <rect x="2" y="3" width="12" height="10" rx="1.5"/>
          <path d="M6 3v10"/>
        </svg>
      </button>
      <div className="brand">
        <a href="/" style={{ display: "flex", alignItems: "center", gap: 9, textDecoration: "none", color: "inherit" }}>
          <span className="mark">R</span>
          <span className="name">reckon</span>
        </a>
        {projectName && (
          <>
            <span style={{ color: "var(--faint)", fontSize: 13, fontWeight: 400, margin: "0 1px" }}>·</span>
            <span className="proj">{projectName}</span>
          </>
        )}
      </div>
      <button className="cmdk-trigger" onClick={onOpenCmdK} title="Search plans · ⌘K">
        <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
          <circle cx="7" cy="7" r="4.5"/>
          <path d="M13 13l-2.5-2.5"/>
        </svg>
        <span>Search</span>
        <span className="kbd">⌘K</span>
      </button>
      <span className="sp"></span>
      <div className="view-tabs">
        <button
          className={`view-tab ${view === "cockpit" ? "active" : ""}`}
          onClick={() => onNav({ view: "cockpit" })}
          title="Overview"
        >
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
            <rect x="2.5" y="2.5" width="4.5" height="4.5" rx="0.7"/>
            <rect x="9" y="2.5" width="4.5" height="4.5" rx="0.7"/>
            <rect x="2.5" y="9" width="4.5" height="4.5" rx="0.7"/>
            <rect x="9" y="9" width="4.5" height="4.5" rx="0.7"/>
          </svg>
          Overview
        </button>
        <button
          className={`view-tab ${view === "plan" ? "active" : ""}`}
          onClick={goPlans}
          title="Plans"
        >
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
            <path d="M3 4h10M3 8h10M3 12h7"/>
          </svg>
          Plans
        </button>
        <button
          className={`view-tab ${view === "sprint" ? "active" : ""}`}
          onClick={goSprints}
          title="Sprints"
        >
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
            <rect x="2.5" y="3" width="3" height="10" rx="0.6"/>
            <rect x="6.5" y="3" width="3" height="10" rx="0.6"/>
            <rect x="10.5" y="3" width="3" height="10" rx="0.6"/>
          </svg>
          Sprints
        </button>
        <button
          className={`view-tab ${view === "graph" ? "active" : ""}`}
          onClick={() => onNav({ view: "graph" })}
          title="Graph — dependencies + critical path"
        >
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
            <circle cx="3.5" cy="4" r="1.5"/>
            <circle cx="3.5" cy="12" r="1.5"/>
            <circle cx="12.5" cy="8" r="1.5"/>
            <path d="M5 4l6 3.5M5 12l6-3.5"/>
          </svg>
          Graph
        </button>
      </div>
    </div>
  );
}

// ─── Filters column ─────────────────────────────────────────────────────

function FiltersCol({ filters, setFilters, showShipped, setShowShipped }) {
  const M = window.STATE;
  const statuses = ["active", "blocked", "pending", "shipped"];
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

  const anyActive = (filters.status?.length || 0) + (filters.ms?.length || 0) + (filters.sprint?.length || 0) > 0;

  return (
    <aside className="filters-col">
      <div className="filter-group">
        <div className="filter-heading">Status</div>
        {statuses.map(s => {
          const n = M.inventory.filter(p => p.status === s).length;
          const on = (filters.status || []).includes(s);
          if (n === 0) return null;
          return (
            <div key={s} className={`filter-chip ${on ? "on" : ""}`} onClick={() => toggle("status", s)}>
              <span className={`dot ${s}`}></span>
              <span style={{ textTransform: "capitalize" }}>{s}</span>
              <span className="n">{n}</span>
            </div>
          );
        })}
      </div>

      <div className="filter-group">
        <div className="filter-heading">Milestone</div>
        {milestones.map(m => {
          const n = M.inventory.filter(p => p.ms === m.id).length;
          const on = (filters.ms || []).includes(m.id);
          if (n === 0) return null;
          return (
            <div key={m.id} className={`filter-chip ${on ? "on" : ""}`} onClick={() => toggle("ms", m.id)}>
              <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: m.status === "active" ? "var(--accent)" : m.status === "shipped" ? "var(--good)" : "var(--muted)", minWidth: 22 }}>{m.id}</span>
              <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flex: 1, minWidth: 0 }}>{m.name}</span>
              <span className="n">{n}</span>
            </div>
          );
        })}
      </div>

      <div className="filter-group">
        <div className="filter-heading">Sprint</div>
        {sprints.map(s => {
          const slugs = (s.items || []).map(it => typeof it === "string" ? it : it.slug);
          const n = slugs.length;
          const on = (filters.sprint || []).includes(s.id);
          return (
            <div key={s.id} className={`filter-chip ${on ? "on" : ""}`} onClick={() => toggle("sprint", s.id)}>
              <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: s.status === "active" ? "var(--accent)" : s.status === "shipped" ? "var(--good)" : "var(--muted)", minWidth: 22 }}>{s.id}</span>
              <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flex: 1, minWidth: 0 }}>{s.theme?.slice(0, 22) + (s.theme?.length > 22 ? "…" : "")}</span>
              <span className="n">{n}</span>
            </div>
          );
        })}
      </div>

      <button className="clear-filters" disabled={!anyActive} onClick={() => setFilters({})}>
        {anyActive ? "× clear filters" : "no filters set"}
      </button>
    </aside>
  );
}

// ─── Plans list column ──────────────────────────────────────────────────

function ListCol({ search, setSearch, route, onNav, items }) {
  const M = window.STATE;
  const sprints = M?.sprints || [];

  const groups = React.useMemo(() => {
    const bySprint = {};
    for (const s of sprints) bySprint[s.id] = { sprint: s, plans: [] };
    bySprint._none = { sprint: null, plans: [] };
    for (const p of items) {
      const k = p.sprint || "_none";
      (bySprint[k] || bySprint._none).plans.push(p);
    }
    const order = { active: 0, planned: 1, shipped: 2 };
    return Object.values(bySprint)
      .filter(g => g.plans.length > 0)
      .sort((a, b) => {
        if (!a.sprint) return 1;
        if (!b.sprint) return -1;
        return (order[a.sprint.status] ?? 9) - (order[b.sprint.status] ?? 9);
      });
  }, [items, sprints]);

  const [collapsed, setCollapsed] = useState(() => {
    const c = {};
    for (const s of sprints) if (s.status === "shipped") c[s.id] = true;
    return c;
  });
  const toggle = (id) => setCollapsed(c => ({ ...c, [id]: !c[id] }));

  return (
    <div className="plans-list">
      <div className="plan-search">
        <input
          placeholder="Search plans…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <div className="count">{items.length} plan{items.length === 1 ? "" : "s"}</div>
      </div>
      {items.length === 0 ? (
        <div className="plans-empty">No plans match.</div>
      ) : groups.map(g => {
        const id = g.sprint?.id || "_none";
        const isOpen = !collapsed[id];
        const isActiveSprintRoute = route.view === "sprint" && route.sprint === id;
        return (
          <div key={id} className="sprint-group">
            <div className={`sprint-group-header ${isActiveSprintRoute ? "route-active" : ""}`} onClick={() => toggle(id)}>
              <span className="car">{isOpen ? "▾" : "▸"}</span>
              <span className="id">{g.sprint ? g.sprint.id : "—"}</span>
              <span className="theme">
                {g.sprint ? g.sprint.theme : "Unscheduled"}
                {g.sprint && (
                  <span className={`st ${g.sprint.status}`}>{g.sprint.status}</span>
                )}
              </span>
              <span className="n">{g.plans.length}</span>
              {g.sprint && (
                <a
                  className="board-link"
                  href={`#sprint/${g.sprint.id}`}
                  onClick={(e) => e.stopPropagation()}
                  title="Open sprint board"
                >▦</a>
              )}
            </div>
            {isOpen && g.plans.map(p => {
              const active = route.view === "plan" && route.slug === p.slug;
              return (
                <div
                  key={p.slug}
                  className={`plan-row ${active ? "active" : ""}`}
                  onClick={() => onNav({ view: "plan", slug: p.slug })}
                >
                  <span className={`dot ${p.status}`}></span>
                  <div>
                    <div className="t">{p.title}</div>
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
      })}
    </div>
  );
}

// ─── Title bar ──────────────────────────────────────────────────────────

function TitleBar({ route, onNav, onOpenPrompt }) {
  const M = window.STATE;
  if (route.view === "cockpit") {
    return (
      <div className="plan-titlebar">
        <div className="row1">
          <span className="title">Overview</span>
        </div>
      </div>
    );
  }
  if (route.view === "plan") {
    const p = M.inventory.find(x => x.slug === route.slug);
    if (!p) return null;
    const openDecs = (p.decisions || []).filter(d => !d.chosen).length;
    const blockedByDecisions = openDecs > 0;
    return (
      <div className="plan-titlebar">
        <div className="row1">
          <span className="crumbs"><code>/{route.slug}</code></span>
          <span className="title">{p.title}</span>
          <div className="actions">
            {openDecs > 0 && (
              <button
                className="decisions-btn"
                onClick={() => {
                  const el = document.querySelector(".decision:not(.taken)") || document.querySelector(".decision");
                  if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
                }}
                title={`${openDecs} pending decision${openDecs === 1 ? "" : "s"}`}
              >
                Decisions <span className="dec-badge">{openDecs}</span>
              </button>
            )}
            <button
              className="gen-prompt"
              onClick={onOpenPrompt}
              disabled={blockedByDecisions}
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
    const inv = [...slugSet].map(slug => {
      const p = M.inventory.find(x => x.slug === slug);
      if (!p) return null;
      const stored = (window.planUtils?.planLoad?.(slug)) || {};
      const overlay = stored.decisions || {};
      const liveDecs = (p.decisions || []).map(d => {
        const ov = overlay[d.key];
        if (ov?.choice) return { ...d, chosen: ov.choice, rationale: ov.rationale, when: ov.when, by: ov.by };
        return d;
      });
      return { ...p, decisions: liveDecs };
    }).filter(Boolean);
    const totalOpen = inv.reduce((n, p) => n + (p.decisions || []).filter(d => !d.chosen).length, 0);
    const blocked = totalOpen > 0;
    const blockedPlans = inv.filter(p => (p.decisions || []).some(d => !d.chosen));

    const handleDecisions = () => {
      if (blockedPlans.length === 0) return;
      const currentSlug = route.view === "plan" ? route.slug : null;
      const currentIdx = blockedPlans.findIndex(p => p.slug === currentSlug);
      const nextIdx = (currentIdx >= 0 && currentIdx < blockedPlans.length - 1)
        ? currentIdx + 1
        : 0;
      onNav({ view: "plan", slug: blockedPlans[nextIdx].slug });
    };

    const handleGen = () => {
      window.dispatchEvent(new CustomEvent("open-fleet-prompt"));
    };
    return (
      <div className="plan-titlebar">
        <div className="row1">
          <span className="crumbs">sprint</span>
          <span className="title">{s ? `${s.id} · ${s.theme}` : route.sprint}</span>
          <div className="actions">
            <button className="sprint-nav-btn" disabled={idx <= 0} onClick={() => onNav({ view: "sprint", sprint: sprints[idx - 1].id })}>‹</button>
            <button className="sprint-nav-btn" disabled={idx >= sprints.length - 1} onClick={() => onNav({ view: "sprint", sprint: sprints[idx + 1].id })}>›</button>
            {blocked && (
              <button className="decisions-btn" onClick={handleDecisions} title="Navigate to next plan with pending decisions">
                Decisions <span className="dec-badge">{totalOpen}</span>
              </button>
            )}
            <button
              className="gen-prompt"
              onClick={handleGen}
              disabled={blocked}
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
    <div className="plan-deps">
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

// ─── Cmd-K palette ──────────────────────────────────────────────────────

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
    <div className="cmdk-scrim" onMouseDown={onClose}>
      <div className="cmdk" onMouseDown={(e) => e.stopPropagation()}>
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
        <div className="cmdk-foot">
          <span>↑↓ navigate</span><span>↵ open</span><span>esc close</span>
        </div>
      </div>
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

function PlanApp() {
  const [route, nav] = useHashRoute();
  const [filters, setFilters] = useState(() => {
    try { return JSON.parse(localStorage.getItem("reckon:filters") || "{}"); } catch { return {}; }
  });
  const [showShipped, setShowShipped] = useState(() => {
    try { return localStorage.getItem("reckon:showShipped") === "1"; } catch { return false; }
  });
  useEffect(() => {
    try { localStorage.setItem("reckon:filters", JSON.stringify(filters)); } catch {}
  }, [filters]);
  useEffect(() => {
    try { localStorage.setItem("reckon:showShipped", showShipped ? "1" : "0"); } catch {}
  }, [showShipped]);

  // Allow other components (e.g. cockpit milestone tiles) to set filters.
  useEffect(() => {
    const onSet = (e) => setFilters(e.detail || {});
    window.addEventListener("reckon:set-filters", onSet);
    return () => window.removeEventListener("reckon:set-filters", onSet);
  }, []);

  const [search, setSearch] = useState("");
  const [promptOpen, setPromptOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => {
    try { return localStorage.getItem("reckon:sidebarCollapsed") === "1"; } catch { return false; }
  });
  const [cockpitSprintIdx, setCockpitSprintIdx] = useState(null);
  const [graphFocal, setGraphFocal] = useState(null);
  const [cmdKOpen, setCmdKOpen] = useState(false);

  useEffect(() => {
    try { localStorage.setItem("reckon:sidebarCollapsed", sidebarCollapsed ? "1" : "0"); } catch {}
  }, [sidebarCollapsed]);

  // When viewing graph and a plan is clicked in the sidebar, also set graphFocal.
  useEffect(() => {
    if (route.view === "plan" && route.slug) setGraphFocal(route.slug);
  }, [route.view, route.slug]);

  const M = window.STATE;
  const items = useMemo(() => {
    if (!M) return [];
    let list = M.inventory;
    if (!showShipped) list = list.filter(p => p.status !== "shipped");
    if (filters.status?.length) list = list.filter(p => filters.status.includes(p.status));
    if (filters.ms?.length) list = list.filter(p => filters.ms.includes(p.ms));
    if (filters.sprint?.length) list = list.filter(p => filters.sprint.includes(p.sprint));
    if (search.trim()) {
      const q = search.toLowerCase();
      list = list.filter(p =>
        p.title?.toLowerCase().includes(q) ||
        p.slug?.toLowerCase().includes(q) ||
        p.summary?.toLowerCase().includes(q)
      );
    }
    return list;
  }, [M, filters, showShipped, search]);

  useEffect(() => {
    if (!promptOpen) return;
    setPromptOpen(false);
    window.dispatchEvent(new CustomEvent("open-prompt"));
  }, [promptOpen]);

  // ⌘B / Ctrl+B toggles the sidebar; ⌘K / Ctrl+K opens Cmd-K palette.
  useEffect(() => {
    const onKey = (e) => {
      if (e.key === "k" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        setCmdKOpen(true);
      }
      if (e.key === "b" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        setSidebarCollapsed(c => !c);
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  return (
    <div className="plan-app">
      <AppTopBar
        route={route}
        onNav={nav}
        sidebarCollapsed={sidebarCollapsed}
        onToggleSidebar={() => setSidebarCollapsed(c => !c)}
        onOpenCmdK={() => setCmdKOpen(true)}
      />
      <div className={`plan-layout ${sidebarCollapsed ? "sidebar-collapsed" : ""} ${(route.view === "cockpit" || route.view === "sprint") ? "overview-mode" : ""}`}>
        <button
          className="sidebar-handle"
          onClick={() => setSidebarCollapsed(c => !c)}
          title={sidebarCollapsed ? "Show sidebar · ⌘B" : "Hide sidebar · ⌘B"}
          aria-label="Toggle sidebar"
        >
          <span></span><span></span>
        </button>
        <FiltersCol filters={filters} setFilters={setFilters} showShipped={showShipped} setShowShipped={setShowShipped} />
        <ListCol search={search} setSearch={setSearch} route={route} onNav={nav} items={items} />
        <div className="content-col">
          <TitleBar route={route} onNav={nav} onOpenPrompt={() => setPromptOpen(true)} />
          <div className="plan-content-body">
            {route.view === "sprint" && <FleetPrompt sprintId={route.sprint} />}
            {route.view === "plan" && <PlanDeps slug={route.slug} />}
            {route.view === "cockpit" && <CockpitBody onNav={nav} cockpitSprintIdx={cockpitSprintIdx} setCockpitSprintIdx={setCockpitSprintIdx} />}
            {route.view === "plan" && <PlanView slug={route.slug} onNav={nav} />}
            {route.view === "sprint" && <SprintView sprintId={route.sprint} onNav={nav} />}
            {route.view === "graph" && typeof GraphView !== "undefined" && <GraphView onNav={nav} items={items} focal={graphFocal} setFocal={setGraphFocal} />}
          </div>
        </div>
      </div>
      {cmdKOpen && (
        <CmdKPalette
          items={M?.inventory || []}
          onClose={() => setCmdKOpen(false)}
          onPick={(slug) => { setCmdKOpen(false); nav({ view: "plan", slug }); }}
        />
      )}
    </div>
  );
}

// ─── Cockpit body ────────────────────────────────────────────────────────

function CockpitBody({ onNav, cockpitSprintIdx, setCockpitSprintIdx }) {
  const M = window.STATE;
  if (!M) return null;
  const project = M.projects[0] || {};
  const allSprints = M.sprints || [];
  const defaultIdx = allSprints.findIndex(s => s.id === M.active_sprint_id);
  const displayIdx = cockpitSprintIdx !== null ? cockpitSprintIdx : (defaultIdx >= 0 ? defaultIdx : 0);
  const sprint = allSprints[displayIdx] || M.sprint;

  const decisionPlans = M.inventory
    .filter(i => (i.dec_open || 0) > 0)
    .sort((a, b) => (b.dec_open || 0) - (a.dec_open || 0));
  const decisionTotal = decisionPlans.reduce((n, p) => n + (p.dec_open || 0), 0);

  return (
    <>
      {project.plans_count != null && (
        <div className="ck-sub">
          {project.plans_count} plans · owner {project.owner}
        </div>
      )}

      <div className="ck-heading">
        <span className="eyebrow">Milestones</span>
      </div>
      <div className="ms-grid" style={{ marginBottom: 4 }}>
        {(project.milestones || []).map(m => (
          <button key={m.id} className={`ms-tile ${m.status}`}
            onClick={() => {
              const target = M.inventory.find(i => i.ms === m.id && i.status === "active")
                || M.inventory.find(i => i.ms === m.id);
              if (target) {
                try { localStorage.setItem("reckon:filters", JSON.stringify({ ms: [m.id] })); } catch {}
                window.dispatchEvent(new CustomEvent("reckon:set-filters", { detail: { ms: [m.id] } }));
                onNav({ view: "plan", slug: target.slug });
              }
            }}>
            <div className="fill" style={{ "--w": `${m.pct}%` }}></div>
            <div className="lbl">{m.id} · <span className={`stat-${m.status}`}>{m.status}</span></div>
            <div className="nm">{m.name}</div>
            <div className="pct">{m.pct}%</div>
          </button>
        ))}
      </div>

      <div className="ck-heading">
        <span className="eyebrow">Sprint {sprint?.id} · {sprint?.theme}</span>
        <div className="sprint-nav">
          <button className="sprint-nav-btn" disabled={displayIdx <= 0} onClick={() => setCockpitSprintIdx(displayIdx - 1)}>‹</button>
          <button className="sprint-nav-btn" disabled={displayIdx >= allSprints.length - 1} onClick={() => setCockpitSprintIdx(displayIdx + 1)}>›</button>
        </div>
        <a className="board-icon" href={`#sprint/${sprint?.id}`} title="Open sprint board">▦</a>
      </div>
      <div className="ck-list" style={{ marginBottom: 22 }}>
        {(sprint?.items || []).map(it => {
          const slug = typeof it === "string" ? it : it.slug;
          const justification = typeof it === "object" ? it.justification : null;
          const p = M.inventory.find(x => x.slug === slug);
          if (!p) return null;
          const pct = Math.round((p.impl || 0) * 100);
          return (
            <a key={slug} className="ck-row" href={`#plan/${slug}`}>
              <span className={`ck-dot ${p.status}`}></span>
              <div className="ck-body">
                <div className="ck-title">{p.title}</div>
                {justification && <div className="ck-just">{justification}</div>}
              </div>
              <div className="ck-progress">
                <span className="ck-bar"><i style={{ width: `${pct}%` }} className={p.status === "shipped" ? "shipped" : p.status === "blocked" ? "blocked" : ""}></i></span>
                <span className="ck-pct">{pct}%</span>
              </div>
              <span className="ck-arrow">›</span>
            </a>
          );
        })}
      </div>

      <div className="ck-heading">
        <span className="eyebrow">Decisions · {decisionTotal} open across {decisionPlans.length} plan{decisionPlans.length === 1 ? "" : "s"}</span>
      </div>
      {decisionPlans.length === 0 ? (
        <div className="ck-empty">No open decisions.</div>
      ) : (
        <div className="ck-list">
          {decisionPlans.map(p => (
            <a key={p.slug} className="ck-row" href={`#plan/${p.slug}`}>
              <span className="ck-num">{p.dec_open}</span>
              <div className="ck-body">
                <div className="ck-title">{p.title}</div>
                <div className="ck-slug">/{p.slug}</div>
              </div>
              <span className="ck-arrow">›</span>
            </a>
          ))}
        </div>
      )}

      <div className="ck-heading"><span className="eyebrow">Recent activity</span></div>
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

// ─── Fleet prompt modal ─────────────────────────────────────────────────

function FleetPrompt({ sprintId }) {
  const M = window.STATE;
  const sprint = M.sprints.find(s => s.id === sprintId);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    const h = () => setOpen(true);
    window.addEventListener("open-fleet-prompt", h);
    return () => window.removeEventListener("open-fleet-prompt", h);
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
    const projectName = M.projects[0]?.project ||
      document.querySelector('meta[name="docs-project"]')?.content ||
      "project";

    let txt = `Orchestration\n  You are coordinating a fleet of workers across ${order.length} plans in a single\n  sprint. Dispatch in the order below; honour the dependency edges. Workers\n  whose dependencies are satisfied may run in parallel. Each worker must read\n  every plan it depends on in full, develop the plan further as it works,\n  inspect code under ${projectName}/ when ambiguous, honour locked decisions,\n  resolve open decisions as part of the work — document rationale in the state JSON.\n\nProject: ${projectName}\nSprint:  ${sprint.id}\nGoal:    ${sprint.theme}\nWindow:  ${sprint.starts} → ${sprint.ends}\n\nExecution sequence (resolved from depends_on within the sprint):\n`;
    order.forEach((p, i) => {
      txt += `  ${i + 1}. ${p.slug}${(p.depends_on || []).length ? "  (← " + p.depends_on.join(", ") + ")" : ""}\n`;
    });
    txt += `\nEach plan's individual prompt follows below as a numbered section.\n\n`;
    order.forEach((p, i) => {
      txt += `\n─── ${i + 1}/${order.length} · ${p.slug} ───\n`;
      const decisions = (p.decisions || []);
      const locked = decisions.filter(d => d.chosen);
      const openD = decisions.filter(d => !d.chosen);
      const lockedBlock = locked.length === 0 ? "  (none)" : locked.map(d => `  ${d.key} → ${d.chosen}`).join("\n");
      const openBlock = openD.length === 0 ? "  (none)" : openD.map(d => `  ${d.key} — ${d.title}`).join("\n");
      const next = (p.followups || [])[0];
      const comments = (p.comments) || (window.planUtils?.planLoad?.(p.slug)?.comments) || {};
      const commentEntries = Object.entries(comments).filter(([_, arr]) => (arr || []).length > 0);
      const commentsBlock = commentEntries.length === 0 ? "  (none)" :
        commentEntries.map(([sid, arr]) =>
          arr.map(c =>
            `  §${sid} · ${c.who} · ${c.when}\n` +
            (c.quote ? `      quote: "${c.quote.length > 200 ? c.quote.slice(0, 200) + "…" : c.quote}"\n` : "") +
            `      body: ${c.body}`
          ).join("\n")
        ).join("\n");
      txt += `Plan: ${p.slug}\nStatus: ${p.status} · ${p.phase || ""}\nJustification (sprint): ${p.justification || "—"}\n\nState to read\n  state/${projectName}/${p.slug}.json\n\nLocked decisions to honour\n${lockedBlock}\n\nOpen decisions (resolve as part of the work — document rationale)\n${openBlock}\n\nComments\n${commentsBlock}\n\nNext-up\n  ${next?.title || "—"}\n  ${next?.body || ""}\n\nDone-when\n  1. Land the work this prompt describes.\n  2. POST a followup to ${p.slug}.json#followups.\n  3. Mark the current followup resolved.\n`;
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
    <div className="modal-scrim" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
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
  <ReadyGate><PlanApp /></ReadyGate>
);
