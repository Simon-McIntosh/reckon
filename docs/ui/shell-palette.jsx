// Reckon shell palette module.
const { useCallback, useEffect, useMemo, useRef, useState } = React;

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
              <span className="meta">{window.ReckonShell.plans.paletteKindLabel(p.kind)} · {p.repository} · {p.status}</span>
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


window.ReckonShell = window.ReckonShell || {};
window.ReckonShell.palette = { CmdKPalette };
