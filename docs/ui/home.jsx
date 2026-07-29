// Fleet home page — renders at http://localhost:8765/
// Fetches /_projects/index.json for the project list.

const {useState, useEffect, useRef, useMemo, useCallback} = React;

function normalizeProject(raw) {
  const d = (raw && raw.data) || {};
  // Shape A: data.projects[0] has per-project summary
  if (Array.isArray(d.projects) && d.projects.length) {
    const s = d.projects[0];
    return {
      project:       raw.project,
      path:          raw.path || "",
      accent:        s.accent || window.ACCENTS?.[raw.project] || window.ACCENTS?._default || "var(--accent)",
      plans_count:   (s.plans_count | 0),
      active:        (s.active  | 0),
      blocked:       (s.blocked | 0),
      pending:       (s.pending | 0),
      shipped:       (s.shipped | 0),
      last_modified: s.last_modified || raw.updated || "",
      activity30:    Array.isArray(s.activity30) ? s.activity30 : [],
    };
  }
  // Shape B: data.counts.status
  const sb    = (d.counts && d.counts.status) || {};
  const plans = Array.isArray(d.plans) ? d.plans : (Array.isArray(d.inventory) ? d.inventory : []);
  const totalPlans = (d.counts && d.counts.total) || plans.length || 0;
  return {
    project:       raw.project,
    path:          raw.path || "",
    accent:        d.accent || window.ACCENTS?.[raw.project] || window.ACCENTS?._default || "var(--accent)",
    plans_count:   totalPlans,
    active:        (sb.active  | 0),
    blocked:       (sb.blocked | 0),
    pending:       (sb.pending | 0),
    shipped:       (sb.shipped | 0),
    last_modified: d.audit_date || raw.updated || "",
    activity30:    Array.isArray(d.activity30) ? d.activity30 : [],
  };
}

function FleetPage({projects, refreshedAt, onOpen}) {
  const tot     = projects.reduce((a, p) => a + p.plans_count, 0);
  const active  = projects.reduce((a, p) => a + p.active, 0);
  const blocked = projects.reduce((a, p) => a + p.blocked, 0);
  const shipped = projects.reduce((a, p) => a + p.shipped, 0);
  return (
    <main className="page">
      <div className="page-head">
        <h1>Across projects.</h1>
        <div className="subtitle">all reckon-managed projects · click to open</div>
      </div>
      <div className="fstrip">
        <div className="fcell"><div className="k">projects</div><div className="v">{projects.length}</div></div>
        <div className="fcell"><div className="k">plans</div><div className="v">{tot}</div></div>
        <div className="fcell"><div className="k">active</div><div className="v">{active}</div></div>
        <div className="fcell"><div className="k">blocked</div><div className={`v ${blocked ? "bad" : ""}`}>{blocked}</div></div>
        <div className="sp"/>
        <div className="fcell"><div className="k">shipped</div><div className="v">{shipped}</div></div>
      </div>
      {window.ProjectCard && projects.map(p => <window.ProjectCard key={p.project} p={p} onOpen={onOpen}/>)}
      <div className="add-hint">
        Add a project: edit <code>~/docs-server/mounts.json</code>. No restart needed.
      </div>
    </main>
  );
}

function FleetCmdKPalette({onClose, projects}) {
  const [q, setQ] = useState("");
  const [idx, setIdx] = useState(0);
  const [inventory, setInventory] = useState([]);
  const inp = useRef(null);
  useEffect(() => { inp.current?.focus(); }, []);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      const items = [];
      for (const proj of projects) {
        try {
          const r = await fetch(`/_discover/${proj.project}`, {cache: "no-store"});
          if (!r.ok) continue;
          const d = await r.json();
          if (Array.isArray(d.inventory)) {
            for (const p of d.inventory) items.push({...p, project: proj.project, accent: proj.accent});
          }
        } catch {}
      }
      if (!cancelled) setInventory(items);
    }
    load();
    return () => { cancelled = true; };
  }, [projects.map(p => p.project).join(",")]);

  const filtered = useMemo(() => {
    const n = q.trim().toLowerCase();
    if (!n) return inventory.slice(0, 40);
    return inventory.filter(p =>
      p.title?.toLowerCase().includes(n) ||
      p.slug?.toLowerCase().includes(n) ||
      p.project?.toLowerCase().includes(n) ||
      p.type?.toLowerCase().includes(n) ||
      p.verdict?.toLowerCase().includes(n) ||
      p.source?.toLowerCase().includes(n) ||
      (p.informs || []).some(ref => ref.toLowerCase().includes(n)) ||
      (p.evidence_for || []).some(ref => ref.toLowerCase().includes(n))
    ).slice(0, 40);
  }, [q, inventory]);

  useEffect(() => { setIdx(0); }, [q]);
  useEffect(() => {
    const k = (e) => {
      if (e.key === "Escape") onClose();
      if (e.key === "ArrowDown") { e.preventDefault(); setIdx(i => Math.min(filtered.length - 1, i + 1)); }
      if (e.key === "ArrowUp")   { e.preventDefault(); setIdx(i => Math.max(0, i - 1)); }
      if (e.key === "Enter" && filtered[idx]) {
        window.location.href = `/${filtered[idx].project}/#plan/${filtered[idx].slug}`;
        onClose();
      }
    };
    document.addEventListener("keydown", k);
    return () => document.removeEventListener("keydown", k);
  }, [filtered, idx, onClose]);

  function statusColor(s) {
    return s === "active" ? "var(--accent)" : s === "blocked" ? "var(--bad)" :
           s === "shipped" ? "var(--ok)" : "var(--line-2)";
  }

  return (
    <div className="cmdk-scrim" onMouseDown={onClose}>
      <div className="cmdk" onMouseDown={e => e.stopPropagation()}>
        <input ref={inp} placeholder="Search plans, research and evidence…" value={q} onChange={e => setQ(e.target.value)}/>
        <div className="list">
          {filtered.map((p, i) => (
            <button key={p.project + "/" + p.slug}
                    className={`cmdk-item ${i === idx ? "on" : ""}`}
                    onMouseEnter={() => setIdx(i)}
                    onClick={() => { window.location.href = `/${p.project}/#plan/${p.slug}`; onClose(); }}>
              <span className="cdot" style={{width:8,height:8,borderRadius:"50%",background:statusColor(p.status)}}/>
              <span className="cgl" style={{color: p.accent || "var(--accent)"}}>
                {window.GLYPHS?.[p.project] || window.GLYPHS?._default}
              </span>
              <span><span className="ct">{p.title}</span> <span className="cs">/{p.slug}</span></span>
              <span className="cmeta">
                {p.project} · {p.type || "plan"}
                {(p.type || "plan") === "plan" ? ` · ${Math.round((p.impl || 0) * 100)}%` : ""}
                {p.type === "evidence" && p.verdict ? ` · ${p.verdict}` : ""}
              </span>
            </button>
          ))}
          {filtered.length === 0 && (
            <div style={{padding: 24, textAlign: "center", color: "var(--muted)", fontSize: 13}}>
              {inventory.length === 0 ? "Loading artifacts…" : "No artifacts match."}
            </div>
          )}
        </div>
        <div className="cmdk-foot"><span>↑↓ navigate</span><span>↵ open</span><span>esc close</span></div>
      </div>
    </div>
  );
}

function HomeApp() {
  const [projects, setProjects] = useState([]);
  const [refreshedAt, setRefreshedAt] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [cmdK, setCmdK] = useState(false);
  const [theme, setTheme] = useState("light");
  const [density, setDensity] = useState("comfortable");

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);

  useEffect(() => {
    const k = (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") { e.preventDefault(); setCmdK(true); }
    };
    document.addEventListener("keydown", k);
    return () => document.removeEventListener("keydown", k);
  }, []);

  useEffect(() => {
    fetch("/_projects/index.json", {cache: "no-store"})
      .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
      .then(data => {
        if (data.updated) setRefreshedAt(new Date(data.updated).toLocaleTimeString());
        setProjects((data.projects || []).map(normalizeProject));
        setLoading(false);
      })
      .catch(e => { setError(e.message); setLoading(false); });
  }, []);

  const onOpen = (projectKey) => {
    window.location.href = `/${projectKey}/`;
  };

  return (
    <>
      <div className="topbar">
        {window.ProjectPicker
          ? <window.ProjectPicker current={null} projects={projects} onNav={p => p ? onOpen(p) : null}/>
          : <span className="label fleet">fleet</span>}
        <button className="r-cmdk-trigger" onClick={() => setCmdK(true)}>
          <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
            <circle cx="7" cy="7" r="4.5"/><path d="M13 13l-2.5-2.5"/>
          </svg>
          <span>Search</span>
          <span className="kbd">⌘K</span>
        </button>
        <span className="sp"/>
        <div className="top-r">
          {window.SettingsMenu && <window.SettingsMenu theme={theme} setTheme={setTheme} density={density} setDensity={setDensity}/>}
          {refreshedAt && <span className="meta-time">refreshed {refreshedAt}</span>}
        </div>
      </div>
      {loading && (
        <main className="page">
          <div style={{padding: "48px 0", textAlign: "center", color: "var(--muted)"}}>Loading fleet data…</div>
        </main>
      )}
      {error && (
        <main className="page">
          <div style={{padding: 32, textAlign: "center"}}>
            <div style={{color: "var(--bad)", fontFamily: "var(--mono)", fontSize: 12}}>{error}</div>
            <p style={{color: "var(--muted)", marginTop: 8}}>Is the reckon server running? <code>uv run reckon serve</code></p>
          </div>
        </main>
      )}
      {!loading && !error && projects.length === 0 && (
        <main className="page">
          <div style={{padding: "48px 24px", textAlign: "center", color: "var(--muted)"}}>
            <div style={{fontSize: 28, marginBottom: 12}}>🗂</div>
            <h2 style={{fontSize: 15, fontWeight: 600}}>No projects mounted yet</h2>
            <p>Edit <code>~/docs-server/mounts.json</code> to register a project, then run <code>uv run reckon serve</code>.</p>
          </div>
        </main>
      )}
      {!loading && !error && projects.length > 0 && (
        <FleetPage projects={projects} refreshedAt={refreshedAt} onOpen={onOpen}/>
      )}
      {cmdK && <FleetCmdKPalette projects={projects} onClose={() => setCmdK(false)}/>}
    </>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<HomeApp/>);
