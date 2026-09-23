// state-loader.js — runtime state fetcher for reckon plan pages.
//
// Builds window.STATE from one authoritative source, chosen in this order:
//   1. /_discover/<project>            — auto-discovery from HTML meta tags
//   2. state/<project>/projection.json — distributed static view (static build)
//   3. state/<project>/index.json      — legacy central index
//
// Discovery answers on the served path and already carries the inventory,
// sprints, milestones, blockers, timeline, active sprint, north stars, source
// format and resource versions for both layouts, so the served loader never
// transfers the superseded aggregate beside it. Each inventory entry carries
// the plan's own state; the plan HTML is its only store, so there is no
// per-plan JSON to fetch.
//
// window.STATE_READY is a Promise. Templates wait on it before rendering.
// The same assembly remains callable so an open page can revalidate its state.

// ── Arrival tracking ─────────────────────────────────────────────────────
// Rows that arrive after a page has rendered are held as pending instead of
// being inserted, so an open list is never re-sorted underneath the reader.
// The rendered snapshot lives at module scope so it survives revalidation:
// the first load adopts every row, a later change event holds new keys as
// pending, and only an explicit reveal moves keys into the snapshot.
let arrivalRendered = null; // { keys:Set<string>, versions:Map<key,version> }

const arrivalVersionOf = (inv) =>
  String((inv && (inv.edited || inv.last || inv.version)) || "");
const arrivalKeyOf = (inv) => (inv && (inv.nav_key || inv.slug)) || "";
const arrivalCountsOf = (rows) => {
  const counts = {};
  for (const inv of rows) {
    const kind = (inv && inv.type) || "plan";
    counts[kind] = (counts[kind] || 0) + 1;
  }
  return counts;
};

window.revalidateProjectState = async function () {
  const PROJECT = (document.querySelector('meta[name="docs-project"]')?.content) ||
                  window.location.pathname.replace(/^\/+/, "").split("/")[0] ||
                  "unknown";
  const discoveryEndpoint = `/_discover/${PROJECT}`;

  window.STATE_LOAD = {
    endpoint: discoveryEndpoint,
    startedAt: Date.now(),
  };
  window.projectStateLoadView = (error = null, now = Date.now()) => ({
    phase: error ? "error" : "pending",
    endpoint: error?.endpoint || window.STATE_LOAD.endpoint,
    httpStatus: Number.isFinite(error?.status) ? error.status : null,
    message: error?.message || "",
    elapsedSeconds: Math.max(
      0,
      Math.floor((now - window.STATE_LOAD.startedAt) / 1000),
    ),
  });

  async function getJson(url, { required = false } = {}) {
    try {
      const r = await fetch(url, { cache: "no-store" });
      if (!r.ok) {
        if (required) throw new Error(`${url} returned HTTP ${r.status}`);
        return null;
      }
      return await r.json();
    } catch (error) {
      if (required) throw error;
      return null;
    }
  }

  const stateBase = `state/${PROJECT}`;
  const canonicalType = (value) => {
    const raw = String(value || "plan").trim().toLowerCase();
    return raw === "doc" ? "research" : raw;
  };
  const mapLegacyCapability = (record) => {
    if (!record || typeof record !== "object" || record.capability || !record.tier) {
      return record;
    }
    const classes = {
      haiku: "routine",
      sonnet: "general",
      opus: "orchestrator",
    };
    const capabilityClass = classes[String(record.tier).toLowerCase()];
    if (!capabilityClass) return record;
    return {
      ...record,
      capability: {
        version: "1.0",
        class: capabilityClass,
        requirements: {},
      },
      compatibility_warning: "legacy tier mapped on read",
    };
  };

  // ── 1. Sources, in priority order: discovery, then the static views ──────
  // Discovery is asked first and, when it answers, nothing else is fetched.
  // It is the served authority; the aggregate is only the static build's
  // stand-in and is never requested for a live page.
  let disc = null;
  let discoveryError = null;
  let discoveryStatus = null;
  let discoveryRefused = false;
  let discoveryResponse;
  try {
    discoveryResponse = await fetch(discoveryEndpoint, { cache: "no-store" });
  } catch (cause) {
    discoveryRefused = true;
    discoveryError = new Error(
      `${discoveryEndpoint} failed: ${cause?.message || "network error"}`
    );
    discoveryError.endpoint = discoveryEndpoint;
    discoveryError.cause = cause;
  }
  if (!discoveryRefused) {
    if (discoveryResponse.ok) {
      disc = await discoveryResponse.json();
    } else {
      discoveryError = new Error(
        `${discoveryEndpoint} returned HTTP ${discoveryResponse.status}`
      );
      discoveryError.endpoint = discoveryEndpoint;
      discoveryError.status = discoveryResponse.status;
      discoveryStatus = discoveryResponse.status;
    }
  }

  let projectionBlob = null;
  let idxBlob = null;
  let idx = {};
  if (!disc) {
    // Only an unreachable discovery — a network failure or an explicit 404 —
    // is the static-build case. Any other HTTP failure is a live server
    // refusing its own inventory and is raised below when discovery is
    // unreachable and no projection stands in for it.
    if (!(discoveryRefused || discoveryStatus === 404)) throw discoveryError;
    projectionBlob = await getJson(`${stateBase}/projection.json`);
    idxBlob = projectionBlob ||
              (await getJson(`${stateBase}/index.json`, { required: true }));
    idx = (idxBlob && idxBlob.data) || {};
  }

  let sprints    = Array.isArray(idx.sprints)
    ? idx.sprints.map(sprint => ({
        ...sprint,
        items: (sprint.items || []).map(item =>
          typeof item === "object" ? mapLegacyCapability(item) : item
        ),
      }))
    : [];
  let milestones = Array.isArray(idx.milestones) ? idx.milestones : [];
  let inventory  = Array.isArray(idx.inventory)  ? idx.inventory  : [];
  let northStars = Array.isArray(idx.north_stars) ? idx.north_stars : [];

  // ── 2. Central-index layout: data.plans[] (no data.inventory) ─────────
  // Handles repos that store plan metadata in a central index.json using
  // data.plans[] (with path fields) instead of data.inventory[] with slugs.
  if (inventory.length === 0 && Array.isArray(idx.plans) && idx.plans.length > 0) {
    const pathToSprint = {};
    for (const s of sprints) {
      for (const it of (s.items || [])) {
        const raw = typeof it === "string" ? it : (it.path || it.slug || "");
        const key = raw.replace(/^.*\//, "").replace(/\.[^.]+$/, "");
        if (key) pathToSprint[key] = s.id;
      }
    }
    inventory = idx.plans.map(pl => {
      const rawPath = pl.path || pl.slug || "";
      const slug = rawPath.replace(/^.*\//, "").replace(/\.[^.]+$/, "")
                   || (pl.title || "plan").toLowerCase().replace(/[^a-z0-9]+/g, "-").slice(0, 48);
      return {
        slug,
        title:    pl.title || slug,
        type:     canonicalType(pl.type || pl.reckon_type),
        status:   pl.status || "pending",
        ms:       pl.milestone || "—",
        roi:      pl.roi    || "mid",
        effort:   pl.effort || "M",
        effort_hours: pl.effort_hours,
        impl:     pl.implementation_fraction || 0,
        dec_open: pl.dec_open || 0,
        blockers: Array.isArray(pl.blocked_by) ? pl.blocked_by.length
                  : (typeof pl.blockers === "number" ? pl.blockers : 0),
        sprint:   pathToSprint[slug] || null,
        last:     pl.last_modified || "",
        summary:  pl.summary || "",
        category: pl.category || "",
        informs:      pl.informs || [],
        evidence_for: pl.evidence_for || [],
        verifies:     pl.verifies || [],
        reviewed_at:  pl.reviewed_at || "",
        recorded_at:  pl.recorded_at || "",
        verdict:      pl.verdict || "",
        environment:  pl.environment || "",
        source:       pl.source || "",
        source_quality: pl.source_quality || "",
        commits:      pl.commits || [],
        artifacts:    pl.artifacts || [],
        _central: true,
      };
    });
  }

  // ── 3. Discovery, when it answered, is the authority ─────────────────────
  // Its inventory, sprints, milestones and north stars are taken for both the
  // distributed and the legacy layout. No persisted aggregate was read to
  // compete with them, so the legacy "index wins" branch of the old ordering
  // has nothing to win with.
  if (disc) {
    if (Array.isArray(disc.inventory))   inventory  = disc.inventory;
    if (Array.isArray(disc.sprints))     sprints    = disc.sprints;
    if (Array.isArray(disc.milestones))  milestones = disc.milestones;
    if (Array.isArray(disc.north_stars)) northStars = disc.north_stars;
  }
  // disc unavailable (static build) → fall through with idx / idx.plans in hand

  // ── 4. Per-plan state travels inside the inventory ─────────────────────
  // Each inventory entry was parsed from its plan page's embedded
  // <script id="reckon-owned sections in (status, decisions, followups,
  // comments, questions). The plan HTML is the sole store — there is no
  // per-plan state JSON to fetch.
  const isArchivedArtifact = (inv) =>
    inv.archived === true || inv.archived === "1" || inv.archived === "true";
  const mergedInventory = inventory.map(inv => {
    const workflowStatus = inv.workflow_status || inv.status || "draft";
    const effectiveStatus = inv.effective_status || workflowStatus;
    return {
      ...mapLegacyCapability(inv),
      workflow_status: workflowStatus,
      effective_status: effectiveStatus,
      status: workflowStatus,
      type: canonicalType(inv.type),
      nav_key: canonicalType(inv.type) === "plan" && !isArchivedArtifact(inv)
        ? inv.slug
        : `${canonicalType(inv.type)}:${isArchivedArtifact(inv) ? "archive:" : ""}${inv.slug}`,
    };
  });
  const plans = Object.fromEntries(mergedInventory.map(inv => [inv.nav_key, inv]));
  const attachmentRelations = mergedInventory.flatMap(source =>
    ["informs", "evidence_for", "verifies"].flatMap(relation =>
      (Array.isArray(source[relation]) ? source[relation] : []).map(target => ({
        relation,
        source: source.nav_key,
        target,
      }))
    )
  );

  // ── Arrival diff ───────────────────────────────────────────────────────
  // Rows new to the rendered snapshot are held as pending rather than being
  // inserted; rows whose version moved update in place. The snapshot only
  // advances when the reader reveals a held row, so an arriving payload can
  // never re-sort or re-scroll a list that is already open.
  if (arrivalRendered === null) {
    arrivalRendered = {
      keys: new Set(mergedInventory.map(arrivalKeyOf)),
      versions: new Map(mergedInventory.map(inv => [arrivalKeyOf(inv), arrivalVersionOf(inv)])),
    };
  }
  const pendingRows = [];
  const updateRows = [];
  const seenKeys = new Set();
  for (const inv of mergedInventory) {
    const key = arrivalKeyOf(inv);
    if (!key) continue;
    seenKeys.add(key);
    if (arrivalRendered.keys.has(key)) {
      if (arrivalRendered.versions.get(key) !== arrivalVersionOf(inv)) {
        arrivalRendered.versions.set(key, arrivalVersionOf(inv));
        updateRows.push(inv);
      }
    } else {
      pendingRows.push(inv);
    }
  }
  for (const key of arrivalRendered.keys) {
    if (!seenKeys.has(key)) {
      arrivalRendered.keys.delete(key);
      arrivalRendered.versions.delete(key);
    }
  }
  const arrival = {
    pending: pendingRows,
    updates: updateRows,
    byKind: arrivalCountsOf(pendingRows),
    total: pendingRows.length,
    receipt: pendingRows.length ? `${pendingRows.length} new` : "live",
  };

  // ── 5b. Auto-augment sprint items from inventory.sprint membership ──────
  // Plans with sprint:"X" in their inventory entry appear in that sprint
  // automatically — no explicit sprint.items[] wiring needed.
  const augmentedSprints = sprints.map(s => {
    const explicit = new Set(
      (s.items || []).map(it => typeof it === "string" ? it : it.slug)
    );
    const auto = mergedInventory
      .filter(p => p.type === "plan" && p.sprint === s.id && !explicit.has(p.slug))
      .map(p => p.slug);
    return auto.length ? { ...s, items: [...(s.items || []), ...auto] } : s;
  });
  const activeSprintId = disc?.active_sprint_id ?? idx.active_sprint_id ?? null;
  const activeSprints = augmentedSprints.filter(s => s.status === "active");
  const activeSprintConflict = activeSprints.length === 0
    ? activeSprintId !== null
    : activeSprints.length !== 1 || activeSprints[0].id !== activeSprintId;
  const activeSprint = augmentedSprints.find(s => s.id === activeSprintId)
                    || augmentedSprints.find(s => s.status === "active");

  // ── 6. Assemble window.STATE ───────────────────────────────────────────

  // Ensure projects[0] is populated. Some central-index repos (e.g. imas-efit)
  // have data.plans[] + data.counts + data.milestones at the top level and no
  // data.projects[]. Synthesise one so the SPA components can read uniformly.
  // idx.projects is non-empty only on the fallback path, where the aggregate
  // was actually read; a page whose discovery answered takes the synthesised
  // row, so no field of the superseded block reaches window.STATE.
  let projects = Array.isArray(idx.projects) ? idx.projects.slice() : [];

  // Live counts derived from the recovered inventory. /_discover is the
  // authoritative plan list; the persisted projects[] counts in index.json go
  // stale (the audit recomputes rollups in its response but never writes them).
  // So whenever a page has an inventory, the counts shown come from it — never
  // from the persisted block. When nothing answered, liveCounts is null and the
  // synthesised row falls back to the persisted counts as a last resort.
  const liveCounts = mergedInventory.length > 0 ? (() => {
    const actionable = mergedInventory.filter(p => p.type === "plan");
    const count = (s) => actionable.filter(p => p.effective_status === s).length;
    const lastMods = actionable.map(p => p.last || "").filter(Boolean).sort();
    return {
      plans_count:   actionable.length,
      active:        count("active"),
      blocked:       count("blocked"),
      pending:       count("pending"),
      shipped:       count("shipped"),
      last_modified: lastMods.length ? lastMods[lastMods.length - 1] : (idx.audit_date || ""),
    };
  })() : null;

  if (projects.length === 0) {
    projects = [{
      project:       PROJECT,
      path:          window.location.pathname.replace(/\/$/, "").split("/").pop() || PROJECT,
      published:     "",
      owner:         "",
      ...(liveCounts || {
        plans_count:   (idx.counts && idx.counts.total) || mergedInventory.length,
        active: 0, blocked: 0, pending: 0, shipped: 0,
        last_modified: idx.audit_date || "",
      }),
      milestones,
      top:           [],
      activity30:    [],
      tests_30d:     { pass: 0, runs: 0 },
    }];
  } else {
    // Persisted projects[0] exists: overlay live counts (when available) so the
    // cockpit never shows a stale plan count, and backfill milestones if absent.
    projects = projects.map((p, i) => i === 0
      ? {
          ...p,
          ...(liveCounts || {}),
          milestones: (Array.isArray(p.milestones) && p.milestones.length) ? p.milestones : milestones,
        }
      : p);
  }

  const surfaceState = disc ?? idx;
  const readySet = (
    surfaceState.ready_set &&
    typeof surfaceState.ready_set === "object" &&
    !Array.isArray(surfaceState.ready_set)
  ) ? surfaceState.ready_set : {};
  const endpoints = Array.isArray(surfaceState.endpoints)
    ? surfaceState.endpoints
    : (Array.isArray(readySet.endpoints) ? readySet.endpoints : []);

  window.STATE = {
    today:            new Date().toISOString().slice(0, 10),
    project:          PROJECT,
    projects,
    milestones,
    north_stars:       northStars,
    inventory:        mergedInventory,
    source_format:    disc?.source_format ?? idx.source_format ?? "legacy-index",
    resource_versions: disc?.resource_versions ?? idx.resource_versions ?? {},
    loaded_at:        new Date().toISOString(),
    active_sprint_id: activeSprintId,
    active_sprints:   activeSprints,
    active_sprint_conflict: activeSprintConflict,
    sprints:          augmentedSprints,
    sprint:           activeSprint,
    blockers:         Array.isArray(disc?.blockers) ? disc.blockers
                      : (Array.isArray(idx.blockers) ? idx.blockers : []),
    timeline:         Array.isArray(disc?.timeline) ? disc.timeline
                      : (Array.isArray(idx.timeline) ? idx.timeline : []),
    ready_set:        readySet,
    endpoints,
    schedule: (surfaceState && typeof surfaceState.schedule === "object" && surfaceState.schedule !== null)
      ? surfaceState.schedule
      : null,
    attachment_relations: attachmentRelations,
    plans,
    arrival,
  };
  window.STATE_ERROR = null;
  return window.STATE;
};

window.STATE_READY = window.revalidateProjectState().catch(error => {
  window.STATE_ERROR = error;
  throw error;
});

window.watchProjectStateChanges = function (onChange) {
  const project = (document.querySelector('meta[name="docs-project"]')?.content) ||
                  window.location.pathname.replace(/^\/+/, "").split("/")[0] ||
                  "unknown";
  const changes = new EventSource(`/_changes/${project}`);
  changes.addEventListener("change", () => onChange());
  return changes;
};

// Reveal the held rows for one kind (or all when kind is null/undefined) and
// adopt them into the rendered snapshot so a later change event does not
// re-flag the same rows as new again.
window.revealArrivals = function (kind) {
  const current = window.STATE && window.STATE.arrival;
  if (!current || !Array.isArray(current.pending)) return [];
  const matches = inv => kind === null || kind === undefined
    || (inv && (inv.type || "plan")) === kind;
  const revealed = current.pending.filter(matches);
  const kept = current.pending.filter(inv => !matches(inv));
  if (arrivalRendered) {
    for (const inv of revealed) {
      const key = arrivalKeyOf(inv);
      if (!key) continue;
      arrivalRendered.keys.add(key);
      arrivalRendered.versions.set(key, arrivalVersionOf(inv));
    }
  }
  window.STATE.arrival = {
    ...current,
    pending: kept,
    byKind: arrivalCountsOf(kept),
    total: kept.length,
    receipt: kept.length ? `${kept.length} new` : "live",
  };
  return revealed;
};
