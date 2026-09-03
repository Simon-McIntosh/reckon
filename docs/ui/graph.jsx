// Two graph components:
//   DependencyChainView — top-level Graph tab. Lead with graph handles, then
//     dependency chains with ‹ › nav across current endpoints; status-grouped
//     list below. Generate-prompt button opens PathPromptModal.
//   RadialFan — plan-view sub-mode. Focal plan centred, deps left, blocks
//     right. Single-hop only. Click satellite to navigate.
//
// Both compute structural dependency depth, never execution ordering.

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

function _dependencyChainMeasure(plans) {
  plans = plans.filter(p => (p.type || "plan") === "plan");
  const bySlug = Object.fromEntries(plans.map(p => [p.slug, p]));
  const pathLen = {}, pathPrev = {};
  function lp(slug, seen = new Set()) {
    if (pathLen[slug] !== undefined) return pathLen[slug];
    if (seen.has(slug)) return 0;
    seen.add(slug);
    const deps = (bySlug[slug]?.depends_on || [])
      .map(_refSlug)
      .filter(d => bySlug[d]);
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
function _allDependencyChains(plans) {
  plans = plans.filter(p => (p.type || "plan") === "plan");
  const bySlug = Object.fromEntries(plans.map(p => [p.slug, p]));
  const pathLen = {}, pathPrev = {};
  function lp(slug, seen = new Set()) {
    if (pathLen[slug] !== undefined) return pathLen[slug];
    if (seen.has(slug)) return 0;
    seen.add(slug);
    const deps = (bySlug[slug]?.depends_on || [])
      .map(_refSlug)
      .filter(d => bySlug[d]);
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

  const handled = plans.filter(p => String(p.graph_handle || "").trim());
  const live = plans.filter(p =>
    (p.status === "active" || p.status === "blocked") &&
    !String(p.graph_handle || "").trim()
  );
  const endpoints = [...handled, ...live];
  if (endpoints.length === 0) return [];

  // Named graph endpoints lead; remaining live trajectories retain the
  // structural depth ordering this view already used.
  const allEndpoints = endpoints.sort((a, b) => {
    const handleOrder = Number(Boolean(b.graph_handle)) - Number(Boolean(a.graph_handle));
    if (handleOrder) return handleOrder;
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

function _dependencyClosure(endpointSlug, bySlug) {
  if (!endpointSlug || !bySlug[endpointSlug]) return new Set();
  const visited = new Set();
  function visit(slug) {
    if (visited.has(slug) || !bySlug[slug]) return;
    visited.add(slug);
    for (const dependency of (bySlug[slug].depends_on || [])) {
      visit(_refSlug(dependency));
    }
  }
  visit(endpointSlug);
  return visited;
}

function _openDecisionCount(plan) {
  const decisions = Array.isArray(plan?.decisions) ? plan.decisions : [];
  if (decisions.length) {
    return decisions.filter(decision => !(decision.chosen || decision.choice)).length;
  }
  return Number(plan?.dec_open || 0);
}

function _graphHandleView(endpoint, members, hopCount, fallbackProject) {
  const handle = String(endpoint?.graph_handle || "").trim();
  if (!handle) return null;
  const repositoryCounts = {};
  for (const member of members) {
    const repository = String(
      member.project || member.repo || fallbackProject || "unknown"
    );
    repositoryCounts[repository] = (repositoryCounts[repository] || 0) + 1;
  }
  const total = members.length;
  const shipped = members.filter(member => member.status === "shipped").length;
  const openDecisions = members.reduce(
    (count, member) => count + _openDecisionCount(member),
    0,
  );
  const structuralDepth = Math.max(1, Number(hopCount || 0));
  return {
    handle,
    shipLine: `/reckon-ship ${handle}`,
    members,
    repositories: Object.entries(repositoryCounts)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([repository, count]) => ({ repository, count })),
    shipped,
    total,
    openDecisions,
    structuralDepth,
    averageWidth: total ? (total / structuralDepth).toFixed(2) : "0.00",
  };
}

// ─── One DAG layout, shared by every dependency surface ───────────────────
//
// Positions derive from structural dependency depth and row order, never from
// execution ordering or wall-clock time. Geometry is published so a stylesheet
// and a stage can agree on card size without either restating the numbers.
const DAG_GEOMETRY = {
  cardWidth: 216,
  cardHeight: 82,
  columnGap: 92,
  rowGap: 22,
  // Room above the first row for the column label.
  topInset: 34,
  stageMargin: 20,
  // A routed detour runs this far below the bottom of every column it crosses.
  detourClearance: 24,
  // Co-terminating arrivals are offset by this much around the card centre so
  // two arrowheads into one card stay distinguishable.
  arrivalFan: 8,
  // Horizontal room reserved for the arrowhead in front of the target card.
  arrowLength: 9,
  // A detour is the lowest ink on the stage, so the stage must clear it.
  detourBottomMargin: 30,
  // Quarter-turn control offsets: a short one for the in-column cubic, a
  // longer pair for the shoulders of a routed detour.
  cubicHandle: 38,
  turnHandle: 34,
  turnRun: 68,
};

const DAG_STROKE = "#c9ccd4";
const DAG_HELD_STROKE = "oklch(0.58 0.20 25)";

// Prerequisites that exist in the drawn set. A dependency on a slug outside it
// is not a missing node, it is context the caller chose not to draw, so it
// contributes neither depth nor an edge.
function _dagPrerequisites(plan, bySlug) {
  return (plan?.depends_on || [])
    .map(_refSlug)
    .filter(slug => bySlug[slug] && slug !== plan.slug);
}

// depth = 1 + max(depth of known prerequisites), zero without any. A cycle is
// terminated by the seen set rather than detected: re-entering a slug already
// on the current walk contributes zero, so every depth stays finite.
function _dagDepths(plans, bySlug) {
  const depth = {};
  const resolve = (slug, seen) => {
    if (depth[slug] != null) return depth[slug];
    const plan = bySlug[slug];
    if (!plan || seen.has(slug)) return 0;
    seen.add(slug);
    const prerequisites = _dagPrerequisites(plan, bySlug);
    depth[slug] = prerequisites.length
      ? 1 + Math.max(...prerequisites.map(dep => resolve(dep, new Set(seen))))
      : 0;
    return depth[slug];
  };
  plans.forEach(plan => resolve(plan.slug, new Set()));
  return depth;
}

function _dagLayout(plans, prefix) {
  const G = DAG_GEOMETRY;
  const drawn = (plans || []).filter(plan => plan && plan.slug);
  const bySlug = {};
  drawn.forEach(plan => { bySlug[plan.slug] = plan; });
  const depth = _dagDepths(drawn, bySlug);

  const columnMembers = {};
  drawn.forEach(plan => {
    (columnMembers[depth[plan.slug]] ||= []).push(plan);
  });
  Object.values(columnMembers).forEach(column => column.sort((left, right) =>
    String(left.slug).localeCompare(String(right.slug))
  ));

  const position = {};
  Object.keys(columnMembers).forEach(key => {
    columnMembers[key].forEach((plan, row) => {
      position[plan.slug] = {
        x: Number(key) * (G.cardWidth + G.columnGap),
        y: G.topInset + row * (G.cardHeight + G.rowGap),
      };
    });
  });

  // The bottom edge of a column, which is what a skip-level edge must clear.
  const columnBottom = key => G.topInset
    + Math.max(0, (columnMembers[key] || []).length - 1) * (G.cardHeight + G.rowGap)
    + G.cardHeight;

  const dependedOn = new Set();
  drawn.forEach(plan => _dagPrerequisites(plan, bySlug).forEach(dep => dependedOn.add(dep)));

  const nodes = drawn.map(plan => {
    const at = position[plan.slug];
    const ghost = Boolean(plan.ghost);
    return {
      key: `${prefix}-${plan.slug}`,
      slug: plan.slug,
      project: plan.project || plan.repo || null,
      title: plan.title || plan.slug,
      status: plan.status || "pending",
      // A context node names the sprint it actually belongs to, so a dimmed
      // card explains itself against the sprint in the header.
      statusText: ghost
        ? `${plan.status || "pending"} · ${plan.sprint || "unscheduled"}`
        : (plan.status || "pending"),
      hours: `${Number(plan.effort_hours || 0)}h`,
      percent: Math.round(Number(plan.impl || 0) * 100),
      depth: depth[plan.slug],
      ghost,
      blocked: plan.status === "blocked",
      // A card with neither a prerequisite nor a dependent is an isolate, and
      // reads as one: the connected cards carry the stronger border.
      connected: _dagPrerequisites(plan, bySlug).length > 0 || dependedOn.has(plan.slug),
      borderStyle: ghost ? "dashed" : "solid",
      background: ghost ? "transparent" : "var(--bg)",
      opacity: ghost ? 0.62 : 1,
      x: at.x,
      y: at.y,
      width: G.cardWidth,
      height: G.cardHeight,
    };
  });

  const arrivals = {};
  drawn.forEach(plan => {
    const prerequisites = _dagPrerequisites(plan, bySlug);
    if (prerequisites.length) arrivals[plan.slug] = prerequisites;
  });

  const edges = [];
  let deepestDetour = 0;
  drawn.forEach(plan => {
    (arrivals[plan.slug] || []).forEach((dep, index, all) => {
      const from = position[dep], to = position[plan.slug];
      const x1 = from.x + G.cardWidth;
      const y1 = from.y + G.cardHeight / 2;
      const x2 = to.x - G.arrowLength;
      const y2 = to.y + G.cardHeight / 2
        + (index - (all.length - 1) / 2) * G.arrivalFan;
      const span = depth[plan.slug] - depth[dep];
      let d = null;
      let detourY = null;
      if (span > 1) {
        // Explicit routed path: quarter-turn down, a flat run AT the clearance
        // depth, quarter-turn up. A cubic with both control points at that
        // depth reaches only three quarters of it and passes beneath the
        // intervening cards, which is why this is not a curve.
        let clear = Math.max(y1, y2);
        for (let k = depth[dep] + 1; k < depth[plan.slug]; k += 1) {
          clear = Math.max(clear, columnBottom(k));
        }
        detourY = clear + G.detourClearance;
        deepestDetour = Math.max(deepestDetour, detourY);
        d = `M ${x1} ${y1} C ${x1 + G.turnHandle} ${y1}, ${x1 + G.turnHandle} ${detourY}, ${x1 + G.turnRun} ${detourY}`
          + ` L ${x2 - G.turnRun} ${detourY}`
          + ` C ${x2 - G.turnHandle} ${detourY}, ${x2 - G.turnHandle} ${y2}, ${x2} ${y2}`;
      } else {
        d = `M ${x1} ${y1} C ${x1 + G.cubicHandle} ${y1}, ${x2 - G.cubicHandle} ${y2}, ${x2} ${y2}`;
      }
      const shipped = bySlug[dep].status === "shipped";
      const held = !shipped && plan.status === "blocked";
      edges.push({
        key: `${prefix}-${dep}-${plan.slug}`,
        from: dep,
        to: plan.slug,
        span,
        skip: span > 1,
        d,
        detourY,
        endY: y2,
        head: `${x2},${y2 - 4.5} ${x2 + G.arrowLength},${y2} ${x2},${y2 + 4.5}`,
        held,
        dashed: !shipped,
        stroke: held ? DAG_HELD_STROKE : DAG_STROKE,
        strokeWidth: held ? 1.8 : 1.4,
        dash: shipped ? "0" : "4 3",
      });
    });
  });

  const depths = Object.keys(columnMembers).map(Number);
  const columnCount = Math.max(1, ...depths.map(value => value + 1));
  const rowCount = Math.max(1, ...Object.values(columnMembers).map(column => column.length));
  const columns = [...depths].sort((left, right) => left - right).map(value => ({
    depth: value,
    label: value === 0 ? "no prerequisites" : `depth ${value}`,
    x: value * (G.cardWidth + G.columnGap),
    width: G.cardWidth,
  }));

  return {
    nodes,
    edges,
    columns,
    depth,
    geometry: G,
    width: columnCount * (G.cardWidth + G.columnGap) - G.columnGap + G.stageMargin,
    // The stage clears the row extent and the deepest detour actually emitted,
    // so a routed edge is never drawn outside it.
    height: Math.max(
      G.topInset + rowCount * (G.cardHeight + G.rowGap) + G.stageMargin,
      deepestDetour ? deepestDetour + G.detourBottomMargin : 0,
    ),
  };
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

// ─── Dependency-chain view (top-level Graph tab) ─────────────────────────

function DependencyChainView({ onNav }) {
  const M = window.STATE;
  if (!M) return null;

  const allPaths = React.useMemo(() => _allDependencyChains(M.inventory), [M.inventory]);
  const { bySlug, pathLen } = React.useMemo(
    () => _dependencyChainMeasure(M.inventory),
    [M.inventory],
  );

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
  const fullPrereqSet = React.useMemo(
    () => _dependencyClosure(chain.at(-1), bySlug),
    [chain, bySlug],
  );

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
  const openDecCount = fullPrereqItems.reduce(
    (count, plan) => count + _openDecisionCount(plan),
    0,
  );
  const graphHandle = _graphHandleView(
    endPlan,
    fullPrereqItems,
    pathLen[endSlug],
    M.project,
  );
  const canvas = React.useMemo(() => {
    const members = fullPrereqItems;
    const memberBySlug = Object.fromEntries(members.map(member => [member.slug, member]));
    const depth = {};
    function memberDepth(slug, seen = new Set()) {
      if (depth[slug] !== undefined) return depth[slug];
      if (seen.has(slug)) return 0;
      seen.add(slug);
      const dependencies = (memberBySlug[slug]?.depends_on || [])
        .map(_refSlug)
        .filter(dependency => memberBySlug[dependency]);
      depth[slug] = dependencies.length
        ? 1 + Math.max(...dependencies.map(dependency => memberDepth(dependency, new Set(seen))))
        : 0;
      return depth[slug];
    }
    members.forEach(member => memberDepth(member.slug));
    const columns = {};
    members.forEach(member => {
      (columns[depth[member.slug]] ||= []).push(member);
    });
    Object.values(columns).forEach(column => column.sort((left, right) =>
      left.slug.localeCompare(right.slug)
    ));
    const rows = {};
    Object.values(columns).forEach(column => column.forEach((member, row) => {
      rows[member.slug] = row;
    }));
    const cardWidth = 178;
    const columnGap = 62;
    const cardHeight = 54;
    const rowGap = 16;
    const position = member => ({
      x: depth[member.slug] * (cardWidth + columnGap),
      y: rows[member.slug] * (cardHeight + rowGap),
    });
    const edges = [];
    for (const member of members) {
      for (const dependencyRef of (member.depends_on || [])) {
        const dependency = memberBySlug[_refSlug(dependencyRef)];
        if (!dependency) continue;
        const source = position(dependency);
        const target = position(member);
        const x1 = source.x + cardWidth;
        const y1 = source.y + cardHeight / 2;
        const x2 = target.x - 9;
        const y2 = target.y + cardHeight / 2;
        edges.push({
          key: `${dependency.slug}-${member.slug}`,
          d: `M ${x1} ${y1} C ${x1 + 30} ${y1}, ${x2 - 30} ${y2}, ${x2} ${y2}`,
          head: `${x2},${y2 - 4} ${x2 + 8},${y2} ${x2},${y2 + 4}`,
          blocked: dependency.status === "blocked" || member.status === "blocked",
        });
      }
    }
    const columnCount = Math.max(1, ...Object.values(depth).map(value => value + 1));
    const rowCount = Math.max(1, ...Object.values(rows).map(value => value + 1));
    return {
      members,
      edges,
      position,
      width: columnCount * (cardWidth + columnGap) - columnGap,
      height: rowCount * (cardHeight + rowGap) - rowGap,
    };
  }, [fullPrereqItems]);

  const copyGraphShipLine = async () => {
    if (!graphHandle || graphHandle.openDecisions) return;
    await navigator.clipboard?.writeText(graphHandle.shipLine);
    if (window.flashSaved) window.flashSaved("graph ship line copied");
  };

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
      {graphHandle ? (
        <>
          <header className="r-graph-header">
            <span className="r-graph-handle-token">{graphHandle.handle}</span>
            <strong>{endPlan?.title || endSlug}</strong>
            <span className="r-graph-subtitle">
              endpoint /{endSlug} · closure {graphHandle.total} derived
            </span>
            <div className="r-graph-path-nav" aria-label="Graph trajectory navigation">
              <button type="button" disabled={safeIdx <= 0}
                onClick={() => setPathIdx(index => Math.max(0, index - 1))}>‹</button>
              <span>{safeIdx + 1}/{allPaths.length}</span>
              <button type="button" disabled={safeIdx >= allPaths.length - 1}
                onClick={() => setPathIdx(index => Math.min(allPaths.length - 1, index + 1))}>›</button>
            </div>
            <button
              type="button"
              className="r-graph-ship"
              onClick={copyGraphShipLine}
              disabled={graphHandle.openDecisions > 0}
              title={graphHandle.openDecisions
                ? `${graphHandle.openDecisions} open decision${graphHandle.openDecisions === 1 ? "" : "s"} in the closure — shipping is held`
                : `Copy ${graphHandle.shipLine}`}
            >
              {graphHandle.shipLine}
            </button>
          </header>

          <div className="r-graph-authority">
            <span className="r-graph-label">Derived authority</span>
            {graphHandle.repositories.map(scope => (
              <span className="r-graph-scope" key={scope.repository}>
                {scope.repository}<b>{scope.count}</b>
              </span>
            ))}
            <span className="r-graph-authority-note">
              repositories enter scope only through closure membership · writes outside scope refused
            </span>
            <span className={`r-graph-ship-state ${graphHandle.openDecisions ? "held" : "ready"}`}>
              {graphHandle.openDecisions
                ? `held · ${graphHandle.openDecisions} open`
                : "ready"}
            </span>
          </div>

          <div className="r-graph-layout">
            <nav className="r-graph-members" aria-label="Derived closure membership">
              <span className="r-graph-label">Derived closure</span>
              {graphHandle.members.map(member => (
                <a key={member.slug} href={`#plan/${member.slug}`}>
                  <span className="r-graph-member-repo">
                    {member.project || member.repo || M.project}
                  </span>
                  <span className="r-graph-member-title">{member.title || member.slug}</span>
                  <span className="r-graph-member-status">{member.status}</span>
                </a>
              ))}
            </nav>

            <section className="r-graph-canvas-panel" aria-label="Derived dependency closure">
              <div className="r-graph-metrics">
                <div>
                  <span>closure members</span>
                  <strong>{graphHandle.total}</strong>
                  <small>derived from the endpoint</small>
                </div>
                <div>
                  <span>average width</span>
                  <strong>{graphHandle.averageWidth}</strong>
                  <small>{graphHandle.total} members ÷ {graphHandle.structuralDepth} hops</small>
                </div>
                <div>
                  <span>longest dependency chain by hop count</span>
                  <strong>{graphHandle.structuralDepth}</strong>
                  <small>structural depth only; not execution ordering</small>
                </div>
              </div>

              <div className="r-graph-canvas-scroll">
                <div className="r-graph-canvas-stage" style={{ width: canvas.width, height: canvas.height }}>
                  <svg width={canvas.width} height={canvas.height} aria-hidden="true">
                    {canvas.edges.map(edge => (
                      <React.Fragment key={edge.key}>
                        <path d={edge.d} className={edge.blocked ? "blocked" : ""}/>
                        <polygon points={edge.head} className={edge.blocked ? "blocked" : ""}/>
                      </React.Fragment>
                    ))}
                  </svg>
                  {canvas.members.map(member => {
                    const position = canvas.position(member);
                    const navKey = _artifactKey(member);
                    return (
                      <a
                        key={member.slug}
                        className={`r-graph-node-card ${member.status}`}
                        href={`#plan/${member.slug}`}
                        style={{ left: position.x, top: position.y }}
                        onClick={event => {
                          event.preventDefault();
                          onNav({ view: "plan", slug: navKey });
                        }}
                      >
                        <strong>{member.title || member.slug}</strong>
                        <span>{member.status}</span>
                      </a>
                    );
                  })}
                </div>
              </div>

              <footer className="r-graph-legend">
                <span><i></i>▶ depends on</span>
                <span><i className="blocked"></i>blocking</span>
                <button type="button" className="gen-prompt" onClick={handleGenPrompt}
                  disabled={chain.length === 0}
                  title={openDecCount > 0 ? `${openDecCount} open decisions — resolve first` : "Generate fleet prompt"}>
                  Generate prompt
                </button>
              </footer>
            </section>
          </div>
        </>
      ) : (
        <p className="r-graph-empty">No shippable graph handle is available for this trajectory.</p>
      )}

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

window.GraphView = DependencyChainView;
window.DependencyChainView = DependencyChainView;
window.CriticalPathView = DependencyChainView;
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

window.ReckonGraph = { layout: _dagLayout, geometry: DAG_GEOMETRY };
