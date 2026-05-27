// Shared UI primitives — project picker, sparkline, chips, project card.
// Depends on window.GLYPHS and window.ACCENTS from glyphs.jsx.
// Exposes: window.ProjectPicker, window.Sparkline, window.Chip, window.ProjectCard, window.SettingsMenu

const {useState, useEffect, useRef, useMemo} = React;

function ProjectPicker({current, projects, onNav}) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  useEffect(() => {
    const close = (e) => { if (!ref.current?.contains(e.target)) setOpen(false); };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);
  const cur = projects.find(p => p.project === current);
  const accent = cur ? (cur.accent || window.ACCENTS?.[current] || window.ACCENTS?._default || "var(--accent)") : "var(--ink)";
  return (
    <div className={`pick ${open ? "open" : ""}`} ref={ref} onClick={() => setOpen(v => !v)}>
      <span className="glyph" style={{color: accent}}>
        {current ? (window.GLYPHS?.[current] || window.GLYPHS?._default) : window.GLYPHS?.fleet}
      </span>
      <span className={`label ${current ? "" : "fleet"}`}>{current || "fleet"}</span>
      <span className="caret">▾</span>
      {open && (
        <div className="pick-menu" onClick={e => e.stopPropagation()}>
          <a className={`pick-item ${!current ? "active" : ""}`} onClick={() => { setOpen(false); onNav(null); }}>
            <span className="gl" style={{color: "var(--ink-2)"}}>{window.GLYPHS?.fleet}</span>
            <span className="lbl">fleet</span>
            <span className="meta">{projects.length} projects</span>
          </a>
          <div className="pick-divider"/>
          <div className="pick-section">Mounted</div>
          {projects.map(p => {
            const pacc = p.accent || window.ACCENTS?.[p.project] || window.ACCENTS?._default || "var(--accent)";
            return (
              <a key={p.project} className={`pick-item ${p.project === current ? "active" : ""}`}
                 onClick={() => { setOpen(false); onNav(p.project); }}>
                <span className="gl" style={{color: pacc}}>
                  {window.GLYPHS?.[p.project] || window.GLYPHS?._default}
                </span>
                <span className="lbl">{p.project}</span>
                <span className="meta">{p.plans_count || 0} plans</span>
              </a>
            );
          })}
        </div>
      )}
    </div>
  );
}

function SettingsMenu({theme, setTheme, density, setDensity}) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  useEffect(() => {
    const close = (e) => { if (!ref.current?.contains(e.target)) setOpen(false); };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);
  return (
    <div className="settings" ref={ref}>
      <button className="icon-btn" onClick={() => setOpen(v => !v)} title="Settings">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
          <circle cx="12" cy="12" r="3"/>
          <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h0a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v0a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>
        </svg>
      </button>
      {open && (
        <div className="settings-menu">
          <div className="settings-title">Theme</div>
          <button className={`settings-item ${theme === "light" ? "on" : ""}`} onClick={() => setTheme("light")}>Light</button>
          <button className={`settings-item ${theme === "dark" ? "on" : ""}`} onClick={() => setTheme("dark")}>Dark</button>
          <div className="settings-title" style={{marginTop: 6}}>Density</div>
          {["comfortable", "compact", "dense"].map(d => (
            <button key={d} className={`settings-item ${density === d ? "on" : ""}`}
                    onClick={() => setDensity(d)} style={{textTransform: "capitalize"}}>{d}</button>
          ))}
        </div>
      )}
    </div>
  );
}

function Sparkline({data, accent}) {
  if (!data || data.length < 2) return <svg className="spark" viewBox="0 0 240 32"/>;
  const W = 240, H = 32;
  const max = Math.max(1, ...data);
  const sx = (i) => (i / (data.length - 1)) * W;
  const sy = (v) => H - 2 - (v / max) * (H - 6);
  const path = data.map((v, i) => `${i === 0 ? "M" : "L"}${sx(i)},${sy(v)}`).join(" ");
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="spark" preserveAspectRatio="none">
      <path d={path} stroke={accent} strokeWidth="1.4" fill="none" strokeLinecap="round" strokeLinejoin="round"/>
    </svg>
  );
}

function Chip({count, kind, label}) {
  return (
    <span className={`chip ${kind} ${count === 0 ? "zero" : ""}`}>
      <span className="dot"/>
      <span>{count} {label}</span>
    </span>
  );
}

function ProjectCard({p, onOpen}) {
  const accent = p.accent || window.ACCENTS?.[p.project] || window.ACCENTS?._default || "var(--accent)";
  const implPct = p.plans_count ? Math.round(p.shipped / p.plans_count * 100) : 0;
  const tot = (p.activity30 || []).reduce((a, b) => a + b, 0);
  const last3 = (p.activity30 || []).slice(-3).reduce((a, b) => a + b, 0);
  return (
    <a className="pcard" onClick={() => onOpen(p.project)}>
      <span className="pgl" style={{color: accent, background: `color-mix(in oklch,${accent} 10%,white)`}}>
        {window.GLYPHS?.[p.project] || window.GLYPHS?._default}
      </span>
      <div>
        <div className="pname">{p.project}</div>
        <div className="ptag">
          {p.plans_count} plans · {implPct}% shipped
          {p.last_modified ? ` · last edited ${new Date(p.last_modified).toLocaleDateString("en-US", {month: "short", day: "numeric"})}` : ""}
        </div>
        <div className="chips">
          <Chip count={p.active} kind="active" label="active"/>
          <Chip count={p.blocked} kind="blocked" label="blocked"/>
          <Chip count={p.pending} kind="pending" label="pending"/>
          <Chip count={p.shipped} kind="shipped" label="shipped"/>
        </div>
      </div>
      <div className="spark-wrap">
        <Sparkline data={p.activity30 || []} accent={accent}/>
        <div className="spark-foot">
          <span>30-day activity</span>
          <span>{tot} updates · {last3} last 3d</span>
        </div>
      </div>
      <span className="arr">→</span>
    </a>
  );
}

window.ProjectPicker = ProjectPicker;
window.SettingsMenu = SettingsMenu;
window.Sparkline = Sparkline;
window.Chip = Chip;
window.ProjectCard = ProjectCard;
