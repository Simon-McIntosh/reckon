// Two graph components:
//   CriticalPathView — top-level Graph tab. Lead with mini-map; then critical
//     path chain with ‹ › nav across all maximal-length paths; status-grouped
//     list below. Generate-prompt button opens PathPromptModal.
//   RadialFan — plan-view sub-mode. Focal plan centred, deps left, blocks
//     right. Single-hop only. Click satellite to navigate.
//
// Both compute critical path (longest dep chain ending at active/blocked).

// Provenance direction is always research → plan → evidence. References may be
// same-project slugs or project-qualified `project:slug#stage` keys.
function _refSlug(ref) {
  return String(ref || "").split("#", 1)[0].split(":").pop();
}

function _artifactKey(artifact) {
  if (artifact?.nav_key) return artifact.nav_key;
  const artifactType = artifact?.type || "plan";
  const archivePart = artifact?.archived ? "archive:" : "";
  return artifactType === "plan" && !archivePart
    ? artifact.slug
    : `${artifactType}:${archivePart}${artifact.slug}`;
}

function _criticalPath(plans) {
  plans = plans.filter(p => (p.type || "plan") === "plan");
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
  plans = plans.filter(p => (p.type || "plan") === "plan");
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
    .filter(p => (p.type || "plan") === "plan" && !fullPrereqSet.has(p.slug) && (p.status === "active" || p.status === "blocked"))
    .sort((a, b) => (b.impl || 0) - (a.impl || 0));

  // Mini-map data
  const mini = React.useMemo(() => {
    const plans = M.inventory;
    const depth = {};
    const bs = Object.fromEntries(plans.map(p => [_artifactKey(p), p]));
    const researchInputs = {};
    for (const artifact of plans) {
      if (artifact.type !== "research") continue;
      for (const ref of (artifact.informs || [])) {
        const target = _refSlug(ref);
        (researchInputs[target] = researchInputs[target] || []).push(_artifactKey(artifact));
      }
    }
    function d(s, seen = new Set()) {
      if (depth[s] !== undefined) return depth[s];
      if (seen.has(s)) return 0;
      seen.add(s);
      const artifact = bs[s] || {};
      const provenanceDeps = artifact.type === "evidence"
        ? [...(artifact.evidence_for || []), ...(artifact.verifies || [])].map(_refSlug)
        : artifact.type === "plan" ? (researchInputs[artifact.slug] || []) : [];
      const authoredDeps = artifact.type === "plan"
        ? (artifact.depends_on || []).map(_refSlug)
        : [];
      const dd = [...authoredDeps, ...provenanceDeps].filter(x => bs[x]);
      if (dd.length === 0) { depth[s] = 0; return 0; }
      depth[s] = 1 + Math.max(...dd.map(x => d(x, seen)));
      return depth[s];
    }
    plans.forEach(p => d(_artifactKey(p)));
    const maxD = Math.max(0, ...Object.values(depth));
    const byDepth = {};
    for (const p of plans) {
      const key = _artifactKey(p);
      (byDepth[depth[key]] = byDepth[depth[key]] || []).push(p);
    }
    const W = 280, H = 130;
    const colW = W / (maxD + 2);
    const pos = {};
    for (let i = 0; i <= maxD; i++) {
      const col = byDepth[i] || [];
      const rowH = (H - 20) / Math.max(1, col.length);
      col.forEach((p, j) => {
        pos[_artifactKey(p)] = { x: 14 + i * colW, y: 10 + (j + 0.5) * rowH };
      });
    }
    const edges = [];
    for (const p of plans) {
      for (const dep of (p.depends_on || [])) {
        if (!bs[dep]) continue;
        const a = pos[_refSlug(dep)], b = pos[_artifactKey(p)];
        if (!a || !b) continue;
        edges.push({ a, b, crit: onPath.has(dep) && onPath.has(p.slug) });
      }
      if (p.type === "research") {
        for (const ref of (p.informs || [])) {
          const target = _refSlug(ref);
          const a = pos[_artifactKey(p)], b = pos[target];
          if (a && b) edges.push({ a, b, crit: false, provenance: true });
        }
      }
      if (p.type === "evidence") {
        for (const ref of [...(p.evidence_for || []), ...(p.verifies || [])]) {
          const source = _refSlug(ref);
          const a = pos[source], b = pos[_artifactKey(p)];
          if (a && b) edges.push({ a, b, crit: false, provenance: true });
        }
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
              const navKey = _artifactKey(p);
              const xy = mini.pos[navKey];
              if (!xy) return null;
              const c = statusColor(p.status);
              const isPlanArtifact = (p.type || "plan") === "plan" && !p.archived;
              const isCrit = isPlanArtifact && onPath.has(p.slug);
              const isEndpoint = isPlanArtifact && endpointToChain[p.slug] !== undefined;
              const isOffChainPrereq = isPlanArtifact && fullPrereqSet.has(p.slug) && !onPath.has(p.slug);
              return (
                <g key={navKey}
                   transform={`translate(${xy.x},${xy.y})`}
                   style={{ cursor: isEndpoint ? "pointer" : "default" }}
                   onClick={() => {
                     if (isEndpoint) setPathIdx(endpointToChain[p.slug]);
                     else onNav({ view: "plan", slug: navKey });
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

// ─── Plan-view focal graph (card-based, three-column) ────────────────────
// Layout:
//   ┌───────────────┬──────────────────────┬───────────────┐
//   │  DEPENDS ON   │       FOCAL          │    BLOCKS     │
//   │   cards       │       card           │     cards     │
//   └───────────────┴──────────────────────┴───────────────┘
// Edges are drawn as an SVG overlay using getBoundingClientRect once after
// layout settles. Clicking any satellite card navigates to that plan — the
// App-level viewMode keeps us in the Graph tab across the slug change, so the
// new focal slides in with its own dependency cone.

function _PlanCard({ plan, role, focal, onClick }) {
  if (!plan) return null;
  const artifactType = plan.type || "plan";
  const pct = Math.round((plan.impl || 0) * 100);
  const isArchived = plan.archived === "1" || plan.archived === true || plan.archived === "true";
  return (
    <button
      type="button"
      className={`r-gcard r-gcard-${role} ${focal ? "focal" : ""} ${artifactType === "plan" ? plan.status : artifactType} ${isArchived ? "archived" : ""}`}
      data-graph-slug={_artifactKey(plan)}
      onClick={onClick}
      title={plan.title}
    >
      <div className="r-gcard-top">
        <span className={`r-gcard-dot ${artifactType === "plan" ? plan.status : artifactType}`}></span>
        <span className="r-gcard-title">{plan.title}</span>
      </div>
      <div className="r-gcard-meta">
        <code>/{plan.slug}</code>
        <span className="sep">·</span>
        <span>{artifactType === "plan" ? (plan.ms || "—") : artifactType}</span>
        {plan.sprint && (<><span className="sep">·</span><span>{plan.sprint}</span></>)}
      </div>
      {artifactType === "plan" && <div className="r-gcard-bar"><i style={{ width: `${pct}%` }}></i></div>}
      <div className="r-gcard-foot">
        <span className="r-gcard-status">{artifactType === "plan" ? plan.status : (plan.verdict || artifactType)}</span>
        {artifactType === "plan" && <span className="r-gcard-pct">{pct}%</span>}
      </div>
    </button>
  );
}

// Reverse-edge index: makes graph relationships symmetric without requiring
// both sides to author them. If plan A's depends_on lists B, B's effective
// blocks should include A — even if B.blocks doesn't mention A explicitly.
// Memoised on inventory identity so it's recomputed only when the list rebuilds.
function _buildReverseIndex(inventory) {
  const revDeps = {};    // slug → [slugs that depend on this slug] → effective blocks
  const revBlocks = {};  // slug → [slugs that say they block this slug] → effective depends_on
  const researchInputs = {};
  const evidenceOutputs = {};
  for (const p of inventory) {
    const key = _artifactKey(p);
    for (const d of (p.depends_on || [])) {
      (revDeps[_refSlug(d)] = revDeps[_refSlug(d)] || []).push(key);
    }
    for (const b of (p.blocks || [])) {
      (revBlocks[_refSlug(b)] = revBlocks[_refSlug(b)] || []).push(key);
    }
    if (p.type === "research") {
      for (const ref of (p.informs || [])) {
        const target = _refSlug(ref);
        (researchInputs[target] = researchInputs[target] || []).push(key);
      }
    }
    if (p.type === "evidence") {
      for (const ref of [...(p.evidence_for || []), ...(p.verifies || [])]) {
        const source = _refSlug(ref);
        (evidenceOutputs[source] = evidenceOutputs[source] || []).push(key);
      }
    }
  }
  return { revDeps, revBlocks, researchInputs, evidenceOutputs };
}

function _effectiveNeighbours(focal, bySlug, revIdx) {
  if (!focal) return { deps: [], blocks: [] };
  const seen = new Set();
  const focalKey = _artifactKey(focal);
  const collect = (slugs) => {
    const out = [];
    for (const s of (slugs || [])) {
      if (!s || seen.has(s) || s === focalKey) continue;
      seen.add(s);
      const p = bySlug[s];
      if (p) out.push(p);
    }
    return out;
  };
  const depsSeen = new Set();
  const blocksSeen = new Set();
  const pushUnique = (set, out, plans) => {
    for (const p of plans) {
      const key = _artifactKey(p);
      if (!set.has(key)) { set.add(key); out.push(p); }
    }
  };
  const deps = [];
  if (focal.type === "plan" || !focal.type) {
    pushUnique(depsSeen, deps, ((focal.depends_on || []).map(_refSlug).map(s => bySlug[s]).filter(Boolean)));
    pushUnique(depsSeen, deps, ((revIdx.revBlocks[focal.slug] || []).map(s => bySlug[s]).filter(Boolean)));
    pushUnique(depsSeen, deps, ((revIdx.researchInputs[focal.slug] || []).map(s => bySlug[s]).filter(Boolean)));
  } else if (focal.type === "evidence") {
    const refs = [...(focal.evidence_for || []), ...(focal.verifies || [])].map(_refSlug);
    pushUnique(depsSeen, deps, refs.map(s => bySlug[s]).filter(Boolean));
  }
  const blocks = [];
  if (focal.type === "plan" || !focal.type) {
    pushUnique(blocksSeen, blocks, ((focal.blocks || []).map(_refSlug).map(s => bySlug[s]).filter(Boolean)));
    pushUnique(blocksSeen, blocks, ((revIdx.revDeps[focal.slug] || []).map(s => bySlug[s]).filter(Boolean)));
    pushUnique(blocksSeen, blocks, ((revIdx.evidenceOutputs[focal.slug] || []).map(s => bySlug[s]).filter(Boolean)));
  } else if (focal.type === "research") {
    pushUnique(blocksSeen, blocks, (focal.informs || []).map(_refSlug).map(s => bySlug[s]).filter(Boolean));
  }
  // Guard against self-loops and dep⇄block overlap (a plan shouldn't appear in
  // both columns — when it does, prefer the explicitly-authored side).
  const blockKeys = new Set(blocks.map(_artifactKey));
  const cleanDeps = deps.filter(p => !blockKeys.has(_artifactKey(p)) || (focal.depends_on || []).map(_refSlug).includes(p.slug));
  return { deps: cleanDeps, blocks };
}

function RadialFan({ focalSlug, onNav, compact = false }) {
  const M = window.STATE;
  if (!M) return null;
  const bySlug = React.useMemo(() => Object.fromEntries(M.inventory.map(p => [_artifactKey(p), p])), [M.inventory]);
  const revIdx = React.useMemo(() => _buildReverseIndex(M.inventory), [M.inventory]);
  const focal = bySlug[focalSlug];

  const containerRef = React.useRef(null);
  const [edges, setEdges] = React.useState([]);
  const [boxSize, setBoxSize] = React.useState({ w: 0, h: 0 });

  // Symmetric: blocks ∪ reverse-deps; depends_on ∪ reverse-blocks.
  const { deps, blocks } = React.useMemo(() => _effectiveNeighbours(focal, bySlug, revIdx), [focal, bySlug, revIdx]);

  // Recompute edge geometry whenever the layout could shift.
  React.useLayoutEffect(() => {
    if (!containerRef.current) return;
    const measure = () => {
      const root = containerRef.current;
      if (!root) return;
      const rect = root.getBoundingClientRect();
      setBoxSize({ w: rect.width, h: rect.height });
      const focalCard = root.querySelector('.r-gcard.focal');
      if (!focalCard) { setEdges([]); return; }
      const fr = focalCard.getBoundingClientRect();
      // Inset endpoints by GAP px so the arrow-tip marker is fully visible
      // (cards have z-index:1 so anything underneath them is clipped).
      const GAP = 6;
      const next = [];
      root.querySelectorAll('.r-gcard-dep').forEach(el => {
        const r = el.getBoundingClientRect();
        next.push({
          id: el.dataset.graphSlug,
          x1: r.right - rect.left + GAP, y1: r.top - rect.top + r.height / 2,
          x2: fr.left - rect.left - GAP, y2: fr.top - rect.top + fr.height / 2,
          dir: "dep",
        });
      });
      root.querySelectorAll('.r-gcard-blk').forEach(el => {
        const r = el.getBoundingClientRect();
        next.push({
          id: el.dataset.graphSlug,
          x1: fr.right - rect.left + GAP, y1: fr.top - rect.top + fr.height / 2,
          x2: r.left   - rect.left - GAP, y2: r.top - rect.top + r.height / 2,
          dir: "blk",
        });
      });
      setEdges(next);
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(containerRef.current);
    window.addEventListener("resize", measure);
    return () => { ro.disconnect(); window.removeEventListener("resize", measure); };
  }, [focalSlug, deps.length, blocks.length]);

  if (!focal) return <div style={{ padding: 24, color: "var(--muted)" }}>No plan selected.</div>;

  const goTo = (key) => onNav({ view: "plan", slug: key });
  const empty = deps.length === 0 && blocks.length === 0;

  return (
    <div className={`r-fan-wrap ${compact ? "compact" : ""}`}>
      <div className="r-fan-grid" ref={containerRef}>
        {/* Edges overlay — absolute-positioned SVG behind the cards */}
        <svg className="r-fan-edges" width={boxSize.w} height={boxSize.h} aria-hidden="true">
          <defs>
            <marker id="fanArrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
              <path d="M0 0 L10 5 L0 10 Z" fill="var(--line-2, #c4c4b8)" />
            </marker>
          </defs>
          {edges.map((e, i) => {
            // Smooth horizontal cubic bezier (left/right columns)
            const mx = (e.x1 + e.x2) / 2;
            const d = `M ${e.x1} ${e.y1} C ${mx} ${e.y1}, ${mx} ${e.y2}, ${e.x2} ${e.y2}`;
            return (
              <path
                key={`${e.dir}-${e.id}-${i}`}
                d={d}
                fill="none"
                stroke="var(--line-2, #c4c4b8)"
                strokeWidth="1.5"
                markerEnd="url(#fanArrow)"
              />
            );
          })}
        </svg>

        {/* Column: depends on */}
        <div className="r-fan-col r-fan-col-left">
          <div className="r-fan-col-h">
            <span className="r-fan-col-lbl">Depends on</span>
            <span className="r-fan-col-n">{deps.length}</span>
          </div>
          <div className="r-fan-col-body">
            {deps.length === 0 && <div className="r-fan-col-empty">No upstream dependencies</div>}
            {deps.map(p => (
              <_PlanCard key={_artifactKey(p)} plan={p} role="dep" focal={false} onClick={() => goTo(_artifactKey(p))} />
            ))}
          </div>
        </div>

        {/* Column: focal */}
        <div className="r-fan-col r-fan-col-focal">
          <div className="r-fan-col-h centred">
            <span className="r-fan-col-lbl">Focal</span>
          </div>
          <div className="r-fan-col-body centred">
            <_PlanCard plan={focal} role="focal" focal={true} onClick={() => {}} />
          </div>
        </div>

        {/* Column: blocks */}
        <div className="r-fan-col r-fan-col-right">
          <div className="r-fan-col-h">
            <span className="r-fan-col-lbl">Blocks</span>
            <span className="r-fan-col-n">{blocks.length}</span>
          </div>
          <div className="r-fan-col-body">
            {blocks.length === 0 && <div className="r-fan-col-empty">Nothing downstream</div>}
            {blocks.map(p => (
              <_PlanCard key={_artifactKey(p)} plan={p} role="blk" focal={false} onClick={() => goTo(_artifactKey(p))} />
            ))}
          </div>
        </div>
      </div>

      {empty && (
        <div className="r-fan-empty">This plan stands alone — no direct dependencies or downstream items.</div>
      )}
    </div>
  );
}
window.RadialFan = RadialFan;
