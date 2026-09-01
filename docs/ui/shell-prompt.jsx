// Reckon shell prompt module.
const { useCallback, useEffect, useMemo, useRef, useState } = React;

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


window.ReckonShell = window.ReckonShell || {};
window.ReckonShell.prompt = { FleetPrompt, PromptModalAdHoc };
