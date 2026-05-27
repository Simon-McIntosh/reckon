// Two graph components:
//   CriticalPathView — top-level Graph tab. Lead with mini-map; then critical
//     path chain with ‹ › nav across all maximal-length paths; status-grouped
//     list below. Generate-prompt button opens PathPromptModal.
//   RadialFan — plan-view sub-mode. Focal plan centred, deps left, blocks
//     right. Single-hop only. Click satellite to navigate.
//
// Both compute critical path (longest dep chain ending at active/blocked).

function _criticalPath(plans) {
  const bySlug = Object.fromEntries(plans.map(p => [p.slug, p]));
  const pathLen = {}, pathPrev = {};
  function lp(slug, seen = new Set()) {
    if (pathLen[slug] !== undefined) return pathLen[slug];
    if (seen.has(slug)) return 0;
    seen.add(slug);
    const deps = (bySlug[slug]?.depends_on || []).filter(d => bySlug[d]);
    if (deps.length === 0) { pathLen[slug] = 1; return 1; }
    let best = 0, bestPrev = null;
    for (const d of deps) {
      const v = lp(d, new Set(seen));
      if (v > best) { best = v; bestPrev = d; }
    }
    pathLen[slug] = best + 1;
    pathPrev[slug] = bestPrev;
    return pathLen[slug];
  }
  plans.forEach(p => lp(p.slug));
  const live = plans.filter(p => p.status === "active" || p.status === "blocked");
  let critEnd = null, critLen = 0;
  for (const p of live) {
    if (pathLen[p.slug] > critLen) { critLen = pathLen[p.slug]; critEnd = p.slug; }
  }
  const chain = [];
  let cur = critEnd;
  while (cur) { chain.unshift(cur); cur = pathPrev[cur]; }
  return { chain, critLen, critEnd, pathLen, pathPrev, bySlug };
}

// Returns an array of all maximal-length chains (each is a slug array).
// Sorted by chain length descending. Multiple endpoints may share the max length.
function _allCriticalPaths(plans) {
  const bySlug = Object.fromEntries(plans.map(p => [p.slug, p]));
  const pathLen = {}, pathPrev = {};
  function lp(slug, seen = new Set()) {
    if (pathLen[slug] !== undefined) return pathLen[slug];
    if (seen.has(slug)) return 0;
    seen.add(slug);
    const deps = (bySlug[slug]?.depends_on || []).filter(d => bySlug[d]);
    if (deps.length === 0) { pathLen[slug] = 1; return 1; }
    let best = 0, bestPrev = null;
    for (const d of deps) {
      const v = lp(d, new Set(seen));
      if (v > best) { best = v; bestPrev = d; }
    }
    pathLen[slug] = best + 1;
    pathPrev[slug] = bestPrev;
    return pathLen[slug];
  }
  plans.forEach(p => lp(p.slug));

  const live = plans.filter(p => p.status === "active" || p.status === "blocked");
  if (live.length === 0) return [];

  // Sort all live plans by pathLen desc, ties broken by slug alpha
  const allEndpoints = [...live].sort((a, b) => {
    const diff = (pathLen[b.slug] || 0) - (pathLen[a.slug] || 0);
    return diff !== 0 ? diff : a.slug.localeCompare(b.slug);
  });
  if (allEndpoints.length === 0) return [];
  return allEndpoints.map(ep => {
    const chain = [];
    let cur = ep.slug;
    while (cur) { chain.unshift(cur); cur = pathPrev[cur]; }
    return chain;
  });
}

// ─── Path prompt modal ────────────────────────────────────────────────────

function PathPromptModal({ chain, fullPrereqItems, bySlug, onClose }) {
  const M = window.STATE;
  const items = fullPrereqItems || (chain || []).map(s => bySlug?.[s]).filter(Boolean);
  const blocked = items.some(p => (p.decisions || []).some(d => !(d.chosen || d.choice)));
  const openDecCount = items.reduce((n, p) =>
    n + (p.decisions || []).filter(d => !(d.chosen || d.choice)).length, 0);

  const prompt = (!blocked && window.buildFleetPrompt)
    ? window.buildFleetPrompt(items, M)
    : "";

  React.useEffect(() => {
    const k = (e) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", k);
    return () => document.removeEventListener("keydown", k);
  }, [onClose]);

  const copy = () => {
    navigator.clipboard?.writeText(prompt);
    onClose();
    if (window.flashSaved) window.flashSaved("path prompt copied");
  };

  return (
    <div className="r-modal-scrim" onClick={onClose}>
      <div className="r-modal" onClick={(e) => e.stopPropagation()}>
        <div className="head">
          <div>
            <div className="r-eyebrow">Graph · fleet prompt</div>
            <h3>{chain?.[chain.length - 1] || "—"} · {items.length} plans</h3>
          </div>
          <button className="btn ghost" onClick={onClose}>Close · Esc</button>
        </div>
        {blocked ? (
          <div style={{ padding: "24px 20px", background: "var(--bad-2)", borderRadius: 6, margin: "12px 20px" }}>
            <span style={{ color: "var(--bad)", fontWeight: 600 }}>{openDecCount} open decision{openDecCount === 1 ? "" : "s"} — resolve before generating prompt.</span>
          </div>
        ) : (
          <textarea value={prompt} readOnly spellCheck={false} />
        )}
        <div className="foot">
          <span style={{ color: "var(--muted)", fontFamily: "var(--mono)", fontSize: 11 }}>
            {items.length} plans · {blocked ? "blocked" : `${prompt.length} chars`}
          </span>
          <span style={{ flex: 1 }}></span>
          <button className="btn primary" onClick={copy} disabled={blocked}>Copy to clipboard</button>
        </div>
      </div>
    </div>
  );
}

// ─── Critical-path-first view (top-level Graph tab) ──────────────────────

function CriticalPathView({ onNav }) {
  const M = window.STATE;
  if (!M) return null;

  const allPaths = React.useMemo(() => _allCriticalPaths(M.inventory), [M.inventory]);
  const { bySlug, pathLen } = React.useMemo(() => _criticalPath(M.inventory), [M.inventory]);

  const [pathIdx, setPathIdx] = React.useState(0);
  const safeIdx = Math.max(0, Math.min(pathIdx, allPaths.length - 1));
  const chain = allPaths[safeIdx] || [];
  const onPath = new Set(chain);

  const [showPrompt, setShowPrompt] = React.useState(false);

  // Map: endpoint slug → chain index (for mini-map endpoint-dot clicks)
  const endpointToChain = React.useMemo(() => {
    const m = {};
    allPaths.forEach((c, i) => { if (c.length > 0) m[c[c.length - 1]] = i; });
    return m;
  }, [allPaths]);

  // Full transitive prereq set of the current endpoint (DFS through ALL deps)
  const fullPrereqSet = React.useMemo(() => {
    const end = chain.length > 0 ? chain[chain.length - 1] : null;
    if (!end) return new Set();
    const visited = new Set();
    function visit(slug) {
      if (visited.has(slug)) return;
      visited.add(slug);
      for (const dep of (bySlug[slug]?.depends_on || [])) {
        if (bySlug[dep]) visit(dep);
      }
    }
    visit(end);
    return visited;
  }, [chain, bySlug]);

  // Plans in fullPrereqSet but NOT on the linear chain → DAG branches
  const alsoRequired = React.useMemo(() =>
    [...fullPrereqSet].filter(s => !onPath.has(s)).map(s => bySlug[s]).filter(Boolean),
    [fullPrereqSet, onPath, bySlug]
  );

  // Plans not in fullPrereqSet and active/blocked (other active plans)
  const otherActive = M.inventory
    .filter(p => !fullPrereqSet.has(p.slug) && (p.status === "active" || p.status === "blocked"))
    .sort((a, b) => (b.impl || 0) - (a.impl || 0));

  // Mini-map data
  const mini = React.useMemo(() => {
    const plans = M.inventory;
    const depth = {};
    const bs = bySlug;
    function d(s, seen = new Set()) {
      if (depth[s] !== undefined) return depth[s];
      if (seen.has(s)) return 0;
      seen.add(s);
      const dd = (bs[s]?.depends_on || []).filter(x => bs[x]);
      if (dd.length === 0) { depth[s] = 0; return 0; }
      depth[s] = 1 + Math.max(...dd.map(x => d(x, seen)));
      return depth[s];
    }
    plans.forEach(p => d(p.slug));
    const maxD = Math.max(0, ...Object.values(depth));
    const byDepth = {};
    for (const p of plans) (byDepth[depth[p.slug]] = byDepth[depth[p.slug]] || []).push(p);
    const W = 280, H = 130;
    const colW = W / (maxD + 2);
    const pos = {};
    for (let i = 0; i <= maxD; i++) {
      const col = byDepth[i] || [];
      const rowH = (H - 20) / Math.max(1, col.length);
      col.forEach((p, j) => {
        pos[p.slug] = { x: 14 + i * colW, y: 10 + (j + 0.5) * rowH };
      });
    }
    const edges = [];
    for (const p of plans) {
      for (const dep of (p.depends_on || [])) {
        if (!bs[dep]) continue;
        const a = pos[dep], b = pos[p.slug];
        if (!a || !b) continue;
        edges.push({ a, b, crit: onPath.has(dep) && onPath.has(p.slug) });
      }
    }
    return { plans, pos, edges, W, H };
  }, [M.inventory, bySlug, chain]);

  function statusColor(s) {
    return s === "active" ? "var(--accent)" :
      s === "blocked" ? "var(--bad)" :
      s === "pending" ? "var(--warn)" :
      s === "shipped" ? "var(--good)" : "var(--muted)";
  }

  // Full prereq items for the prompt (chain + DAG branches, in topological order via buildFleetPrompt)
  const fullPrereqItems = [...fullPrereqSet].map(s => bySlug[s]).filter(Boolean);
  const endSlug = chain.length > 0 ? chain[chain.length - 1] : null;
  const endPlan = endSlug ? bySlug[endSlug] : null;

  // Open decisions in the full prereq set
  const openDecCount = fullPrereqItems.reduce((n, p) =>
    n + (p.decisions || []).filter(d => !(d.chosen || d.choice)).length, 0);

  const handleGenPrompt = () => {
    if (openDecCount > 0) {
      const firstBlocking = fullPrereqItems.find(p =>
        (p.decisions || []).some(d => !(d.chosen || d.choice))
      );
      if (firstBlocking) { window.location.hash = `#plan/${firstBlocking.slug}`; return; }
    }
    setShowPrompt(true);
  };

  return (
    <div className="r-graph">

      {/* 1. Trajectory header */}
      <div className="r-graph-head">
        <div className="r-crit-chain-nav">
          <button className="r-nav-btn" disabled={safeIdx <= 0}
            onClick={() => setPathIdx(i => Math.max(0, i - 1))} title="Previous trajectory">‹</button>
          <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--muted)", minWidth: 36, textAlign: "center" }}>
            {allPaths.length > 0 ? `${safeIdx + 1}/${allPaths.length}` : "—"}
          </span>
          <button className="r-nav-btn" disabled={safeIdx >= allPaths.length - 1}
            onClick={() => setPathIdx(i => Math.min(allPaths.length - 1, i + 1))} title="Next trajectory">›</button>
        </div>
        {endPlan && (
          <span style={{ fontFamily: "var(--mono)", fontSize: 12, color: "var(--ink-2)", marginLeft: 8, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: 180 }}>
            → <strong>{endSlug}</strong>
          </span>
        )}
        {fullPrereqSet.size > 0 && (
          <span style={{ fontFamily: "var(--mono)", fontSize: 11, color: "var(--muted)", marginLeft: 8, whiteSpace: "nowrap" }}>
            {fullPrereqSet.size} prereq{fullPrereqSet.size !== 1 ? "s" : ""}
          </span>
        )}
        <span style={{ flex: 1 }}></span>
        <button
          className="gen-prompt"
          onClick={handleGenPrompt}
          disabled={chain.length === 0}
          title={openDecCount > 0 ? `${openDecCount} open decisions — resolve first` : "Generate fleet prompt"}
          style={{ position: "relative" }}
        >
          <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
            <path d="M4 3h6l2 2v8H4z"/>
            <path d="M10 3v2h2"/>
            <path d="M6 7h4M6 9h4M6 11h2"/>
          </svg>
          Generate prompt
          {openDecCount > 0 && <span className="resolve-badge" style={{ marginLeft: 4 }}>{openDecCount}</span>}
        </button>
      </div>

      {/* 2. Chain cards */}
      <div className="r-crit-chain">
        {chain.length === 0 && (
          <div style={{ color: "var(--muted)", padding: "12px 0", fontSize: 13 }}>
            No active or blocked plans with dependencies found.
          </div>
        )}
        {chain.map((slug, i) => {
          const p = bySlug[slug];
          if (!p) return null;
          return (
            <React.Fragment key={slug}>
              <a className="r-crit-card" href={`#plan/${slug}`}>
                <div className="r-crit-card-h">
                  <span className={`r-crit-dot ${p.status}`}></span>
                  <span className="r-crit-card-t">{p.title}</span>
                </div>
                <div className="r-crit-card-meta">
                  /{slug} · {p.ms || "—"}{p.sprint ? " · " + p.sprint : ""}
                </div>
                <div className="r-crit-card-bar">
                  <i style={{ width: `${Math.round((p.impl || 0) * 100)}%`, background: statusColor(p.status) }}></i>
                </div>
                <div className="r-crit-card-foot">
                  <span style={{ color: statusColor(p.status) }}>{p.status}</span>
                  <span style={{ flex: 1 }}></span>
                  <span>{Math.round((p.impl || 0) * 100)}%</span>
                </div>
              </a>
              {i < chain.length - 1 && <span className="r-crit-arr">→</span>}
            </React.Fragment>
          );
        })}
      </div>

      {/* 3. "Also required" strip — DAG branches not on the linear chain */}
      {alsoRequired.length > 0 && (
        <div className="r-graph-section" style={{ marginTop: 16 }}>
          <div style={{ fontFamily: "var(--mono)", fontSize: 10.5, letterSpacing: "0.08em", textTransform: "uppercase", color: "var(--muted)", marginBottom: 8 }}>
            Also required · {alsoRequired.length}
          </div>
          <div className="r-ck-list">
            {alsoRequired.map(p => (
              <a key={p.slug} className="r-ck-row" href={`#plan/${p.slug}`}>
                <span className={`r-ck-dot ${p.status}`}></span>
                <div className="r-ck-body">
                  <div className="r-ck-title">{p.title}</div>
                  <div className="r-ck-slug">/{p.slug} · {p.ms || "—"}</div>
                </div>
                <div className="r-ck-prog">
                  <span className="r-ck-bar"><i style={{ width: `${Math.round((p.impl || 0) * 100)}%`, background: statusColor(p.status) }}></i></span>
                  <span className="r-ck-pct">{Math.round((p.impl || 0) * 100)}%</span>
                </div>
                <span className="r-ck-arr">›</span>
              </a>
            ))}
          </div>
        </div>
      )}

      {/* 4. Mini-map — last, with endpoint dots clickable */}
      <div className="r-graph-section" style={{ marginTop: 20 }}>
        <div style={{ fontFamily: "var(--mono)", fontSize: 10.5, letterSpacing: "0.08em", textTransform: "uppercase", color: "var(--muted)", marginBottom: 8 }}>
          Map · {M.inventory.length} plans
        </div>
        <div className="r-mini-map">
          <svg viewBox={`0 0 ${mini.W} ${mini.H}`} width="100%" height={mini.H}>
            {mini.edges.map((e, i) => (
              <line key={i} x1={e.a.x} y1={e.a.y} x2={e.b.x} y2={e.b.y}
                stroke={e.crit ? "var(--accent)" : "var(--hair)"}
                strokeWidth={e.crit ? 1.8 : 1}/>
            ))}
            {mini.plans.map(p => {
              const xy = mini.pos[p.slug];
              if (!xy) return null;
              const c = statusColor(p.status);
              const isCrit = onPath.has(p.slug);
              const isEndpoint = endpointToChain[p.slug] !== undefined;
              const isOffChainPrereq = fullPrereqSet.has(p.slug) && !onPath.has(p.slug);
              return (
                <g key={p.slug}
                   transform={`translate(${xy.x},${xy.y})`}
                   style={{ cursor: isEndpoint ? "pointer" : "default" }}
                   onClick={() => {
                     if (isEndpoint) setPathIdx(endpointToChain[p.slug]);
                     else onNav({ view: "plan", slug: p.slug });
                   }}>
                  <title>{p.title} · {p.status}{isEndpoint ? " (endpoint — click to select)" : ""}</title>
                  {isCrit && <circle r="6" fill="none" stroke="var(--accent)" strokeWidth="1.5"/>}
                  {isOffChainPrereq && !isCrit && <circle r="5" fill="none" stroke="var(--muted)" strokeWidth="1" strokeDasharray="2,2"/>}
                  <circle r="3" fill={c}/>
                </g>
              );
            })}
          </svg>
        </div>
      </div>

      {/* 5. Other active & blocked (not in the prereq set) */}
      {otherActive.length > 0 && (
        <div className="r-graph-grid">
          <div>
            <div style={{ fontFamily: "var(--mono)", fontSize: 10.5, letterSpacing: "0.08em", textTransform: "uppercase", color: "var(--muted)", margin: "16px 0 8px" }}>
              Other active &amp; blocked · {otherActive.length}
            </div>
            <div className="r-ck-list">
              {otherActive.map(p => (
                <a key={p.slug} className="r-ck-row" href={`#plan/${p.slug}`}>
                  <span className={`r-ck-dot ${p.status}`}></span>
                  <div className="r-ck-body">
                    <div className="r-ck-title">{p.title}</div>
                    <div className="r-ck-slug">/{p.slug} · {p.ms || "—"}</div>
                  </div>
                  <div className="r-ck-prog">
                    <span className="r-ck-bar"><i style={{ width: `${Math.round((p.impl || 0) * 100)}%`, background: statusColor(p.status) }}></i></span>
                    <span className="r-ck-pct">{Math.round((p.impl || 0) * 100)}%</span>
                  </div>
                  <span className="r-ck-arr">›</span>
                </a>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* Prompt modal */}
      {showPrompt && window.reckon?.PromptModal && (
        <window.reckon.PromptModal
          planSlug={endSlug || "graph"}
          initialPrompt={
            window.buildFleetPrompt
              ? window.buildFleetPrompt(fullPrereqItems, window.STATE, endPlan?.title)
              : "(prompts.js not loaded)"
          }
          onClose={() => setShowPrompt(false)}
        />
      )}
    </div>
  );
}

window.GraphView = CriticalPathView;
window.CriticalPathView = CriticalPathView;
window.PathPromptModal = PathPromptModal;

// ─── Radial fan (plan view sub-mode) ─────────────────────────────────────

function RadialFan({ focalSlug, onNav }) {
  const M = window.STATE;
  if (!M) return null;
  const bySlug = Object.fromEntries(M.inventory.map(p => [p.slug, p]));
  const focal = bySlug[focalSlug];
  if (!focal) return <div style={{ padding: 24, color: "var(--muted)" }}>No plan selected.</div>;

  const MAX_PER_SIDE = 6;
  const deps = (focal.depends_on || []).map(s => bySlug[s]).filter(Boolean);
  const blocks = (focal.blocks || []).map(s => bySlug[s]).filter(Boolean);
  const depsOverflow = Math.max(0, deps.length - MAX_PER_SIDE);
  const blocksOverflow = Math.max(0, blocks.length - MAX_PER_SIDE);
  const depsShown = deps.slice(0, MAX_PER_SIDE);
  const blocksShown = blocks.slice(0, MAX_PER_SIDE);

  const { chain } = _criticalPath(M.inventory);
  const onPath = new Set(chain);

  function statusColor(s) {
    return s === "active" ? "var(--accent)" :
      s === "blocked" ? "var(--bad)" :
      s === "pending" ? "var(--warn)" :
      s === "shipped" ? "var(--good)" : "var(--muted)";
  }

  // Layout
  const W = 880, H = 420;
  const CX = W / 2, CY = H / 2;
  const R = 170;
  const focalR = 70;
  const satR = 44;

  function arcPos(items, side) {
    const n = items.length;
    if (n === 0) return [];
    const base = side === "left" ? Math.PI : 0;
    const span = Math.min(Math.PI * 0.85, Math.max(0.5, n * 0.28));
    return items.map((p, i) => {
      const t = n === 1 ? 0 : (i / (n - 1) - 0.5) * 2;
      const angle = base + t * (span / 2);
      return { plan: p, x: CX + Math.cos(angle) * R, y: CY + Math.sin(angle) * R };
    });
  }
  const left = arcPos(depsShown, "left");
  const right = arcPos(blocksShown, "right");

  return (
    <div className="r-fan">
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" preserveAspectRatio="xMidYMid meet">
        <defs>
          <marker id="fanArr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto">
            <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--muted)"/>
          </marker>
          <marker id="fanArrC" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">
            <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--accent)"/>
          </marker>
        </defs>

        {/* zone labels */}
        <text x={50} y={CY - 4} fontSize="10" fontFamily="var(--mono)" fill="var(--muted)" letterSpacing="0.10em">DEPENDS ON</text>
        <text x={50} y={CY + 14} fontSize="9" fontFamily="var(--mono)" fill="var(--faint)">{deps.length} {deps.length === 1 ? "plan" : "plans"}</text>
        <text x={W - 120} y={CY - 4} fontSize="10" fontFamily="var(--mono)" fill="var(--muted)" letterSpacing="0.10em">BLOCKS</text>
        <text x={W - 120} y={CY + 14} fontSize="9" fontFamily="var(--mono)" fill="var(--faint)">{blocks.length} {blocks.length === 1 ? "plan" : "plans"}</text>

        {/* edges */}
        {left.map((n, i) => {
          const isCrit = onPath.has(n.plan.slug) && onPath.has(focalSlug);
          const dx = CX - n.x, dy = CY - n.y;
          const d = Math.hypot(dx, dy);
          const ux = dx / d, uy = dy / d;
          return (
            <line key={"el" + i}
              x1={n.x + ux * satR} y1={n.y + uy * satR}
              x2={CX - ux * focalR} y2={CY - uy * focalR}
              stroke={isCrit ? "var(--accent)" : "var(--hair)"}
              strokeWidth={isCrit ? 2.5 : 1.5}
              markerEnd={isCrit ? "url(#fanArrC)" : "url(#fanArr)"}/>
          );
        })}
        {right.map((n, i) => {
          const isCrit = onPath.has(n.plan.slug) && onPath.has(focalSlug);
          const dx = n.x - CX, dy = n.y - CY;
          const d = Math.hypot(dx, dy);
          const ux = dx / d, uy = dy / d;
          return (
            <line key={"er" + i}
              x1={CX + ux * focalR} y1={CY + uy * focalR}
              x2={n.x - ux * satR} y2={n.y - uy * satR}
              stroke={isCrit ? "var(--accent)" : "var(--hair)"}
              strokeWidth={isCrit ? 2.5 : 1.5}
              markerEnd={isCrit ? "url(#fanArrC)" : "url(#fanArr)"}/>
          );
        })}

        {/* satellites */}
        {[...left, ...right].map((n, i) => {
          const onP = onPath.has(n.plan.slug);
          return (
            <g key={"sat" + n.plan.slug} transform={`translate(${n.x},${n.y})`}
               style={{ cursor: "pointer" }}
               onClick={() => onNav({ view: "plan", slug: n.plan.slug })}>
              {onP && <circle r={satR + 4} fill="var(--accent)" opacity="0.08"/>}
              <circle r={satR} fill="#fff" stroke={onP ? "var(--accent)" : "var(--line)"} strokeWidth={onP ? 2 : 1}/>
              <circle r={satR} fill="none" stroke={statusColor(n.plan.status)} strokeWidth="3"
                      strokeDasharray={`${(n.plan.impl || 0) * 2 * Math.PI * satR} ${2 * Math.PI * satR}`}
                      transform="rotate(-90)"/>
              <text y={-4} textAnchor="middle" fontSize="11" fontWeight="600" fill="var(--ink)">
                {n.plan.title.length > 14 ? n.plan.title.slice(0, 14) + "…" : n.plan.title}
              </text>
              <text y={10} textAnchor="middle" fontSize="9" fontFamily="var(--mono)" fill="var(--muted)">
                {n.plan.ms || "—"} · {Math.round((n.plan.impl || 0) * 100)}%
              </text>
            </g>
          );
        })}

        {/* overflow pills */}
        {depsOverflow > 0 && (
          <g transform={`translate(${CX - R - 100},${CY + 90})`}>
            <rect x="0" y="0" width="80" height="22" rx="11" fill="var(--bg-2)" stroke="var(--hair)"/>
            <text x="40" y="15" textAnchor="middle" fontSize="11" fontFamily="var(--mono)" fill="var(--muted)">+{depsOverflow} more</text>
          </g>
        )}
        {blocksOverflow > 0 && (
          <g transform={`translate(${CX + R + 20},${CY + 90})`}>
            <rect x="0" y="0" width="80" height="22" rx="11" fill="var(--bg-2)" stroke="var(--hair)"/>
            <text x="40" y="15" textAnchor="middle" fontSize="11" fontFamily="var(--mono)" fill="var(--muted)">+{blocksOverflow} more</text>
          </g>
        )}

        {/* focal */}
        <g transform={`translate(${CX},${CY})`}>
          {onPath.has(focalSlug) && <circle r={focalR + 6} fill="var(--accent)" opacity="0.10"/>}
          <circle r={focalR} fill="#fff" stroke="var(--ink)" strokeWidth="1.5"/>
          <circle r={focalR} fill="none" stroke={statusColor(focal.status)} strokeWidth="4"
                  strokeDasharray={`${(focal.impl || 0) * 2 * Math.PI * focalR} ${2 * Math.PI * focalR}`}
                  transform="rotate(-90)"/>
          <text y={-12} textAnchor="middle" fontSize="14" fontWeight="600" fill="var(--ink)">
            {focal.title.length > 24 ? focal.title.slice(0, 24) + "…" : focal.title}
          </text>
          <text y={6} textAnchor="middle" fontSize="10" fontFamily="var(--mono)" fill="var(--muted)">
            /{focal.slug}
          </text>
          <text y={22} textAnchor="middle" fontSize="11" fontFamily="var(--mono)" fill={statusColor(focal.status)}>
            {focal.status} · {Math.round((focal.impl || 0) * 100)}%
          </text>
        </g>
      </svg>

      {(deps.length === 0 && blocks.length === 0) && (
        <div className="r-fan-empty">No direct dependencies or blocks. This plan stands alone.</div>
      )}
    </div>
  );
}
window.RadialFan = RadialFan;
