// state-loader.js — runtime state fetcher for reckon plan pages.
//
// The page paints from the index. /_index/<project> carries one row per
// document and figure — slug, href, type, title, status, sprint, archive
// marker, stamps and figure dimensions — and nothing derived, so it is the
// first request and window.STATE_READY resolves on it.
//
// Derived state follows in the background. /_discover/<project> carries the
// same rows plus the derived fields (effective status, blockers, readiness)
// and the ready set; when it arrives it is merged into the rows already on
// screen, in place and in the order the reader is looking at, and then
// window.STATE_DERIVED_READY resolves. Every path settles that promise — the
// fallback below carries its derived state already, so it resolves with the
// state it assembled. A plan's body is fetched only when that plan is opened.
// The derived values are computed in Python on the server; the loader merges
// them and never derives them itself.
//
// A change the server reports over /_changes/<project> arrives as the index
// rows that moved — changed, added or removed — and is patched into the rows
// on screen with no request of its own; the derived state that follows is
// refetched at most once per settle window, in the background. An event that
// carries only a digest, as a server that does not push rows sends, is
// patched by nothing and answered by that same shared refetch.
//
// An index that is unavailable — a 404, or a request that fails at the
// network — costs the page nothing: the loader falls back to the sources that
// stood in for it before, in this order:
//   1. /_discover/<project>            — served discovery (live server)
//   2. state/<project>/projection.json — distributed static view (static build)
//   3. state/<project>/index.json      — legacy central index
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

// ── Row identity ─────────────────────────────────────────────────────────
// One inventory row per listed entry, whichever source named it. The index
// and the discovery payload name the same entry, so both pass through here
// and the merge can key on nav_key without a second mapping.
const canonicalType = (value) => {
  const raw = String(value || "plan").trim().toLowerCase();
  return raw === "doc" ? "research" : raw;
};
const isArchivedArtifact = (inv) =>
  inv.archived === true || inv.archived === "1" || inv.archived === "true";
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
const mapInventoryRow = (inv) => {
  const workflowStatus = inv.workflow_status || inv.status || "draft";
  const type = canonicalType(inv.type);
  const archived = isArchivedArtifact(inv);
  return {
    ...mapLegacyCapability(inv),
    workflow_status: workflowStatus,
    effective_status: inv.effective_status || workflowStatus,
    status: workflowStatus,
    type,
    nav_key: type === "plan" && !archived
      ? inv.slug
      : `${type}:${archived ? "archive:" : ""}${inv.slug}`,
  };
};
const attachmentRelationsOf = (mergedInventory) => mergedInventory.flatMap(source =>
  ["informs", "evidence_for", "verifies"].flatMap(relation =>
    (Array.isArray(source[relation]) ? source[relation] : []).map(target => ({
      relation,
      source: source.nav_key,
      target,
    }))
  )
);

// ── Pushed rows ──────────────────────────────────────────────────────────
// A change event may carry the index rows that moved — changed, added or
// removed — and the state on screen is patched from them: a changed row
// keeps its object and its place, a row the page never carried is held as an
// arrival, and a removed row leaves the inventory. Applying the push costs
// no request; the derived state that follows it is fetched once per settle
// window, in the background.
const CHANGE_SETTLE_MS = 500;

const applyIndexRowChanges = (delta) => {
  const state = window.STATE;
  if (!state || !Array.isArray(state.inventory)) return false;
  const list = (value) => (Array.isArray(value) ? value : []);
  const changed = list(delta?.changed);
  const added = list(delta?.added);
  const removed = list(delta?.removed);
  if (!changed.length && !added.length && !removed.length) return false;

  const onScreen = new Map(state.inventory.map(row => [arrivalKeyOf(row), row]));
  const pending = [];
  for (const row of [...changed, ...added]) {
    const mapped = mapInventoryRow(row);
    const key = arrivalKeyOf(mapped);
    if (!key) continue;
    const existing = onScreen.get(key);
    if (!existing) {
      // A row the page never carried is held as an arrival, like any other
      // row that appears after the list opened.
      pending.push(mapped);
      continue;
    }
    // The index carries no derived effective status, so a pushed row that
    // does not speak about it keeps the one a discovery merge delivered
    // rather than a locally defaulted one.
    if (!("effective_status" in row)) {
      mapped.effective_status = existing.effective_status || mapped.effective_status;
    }
    Object.assign(existing, mapped);
  }

  const removedKeys = new Set(
    removed.map(row => arrivalKeyOf(mapInventoryRow(row))).filter(Boolean)
  );
  if (removedKeys.size) {
    state.inventory = state.inventory.filter(row => !removedKeys.has(arrivalKeyOf(row)));
    if (arrivalRendered) {
      for (const key of removedKeys) {
        arrivalRendered.keys.delete(key);
        arrivalRendered.versions.delete(key);
      }
    }
    // A row removed before its arrival was revealed must leave the arrival
    // count too: the page must not keep offering a document that is gone.
    const pendingBefore =
      state.arrival && Array.isArray(state.arrival.pending) ? state.arrival.pending : [];
    const pendingKept = pendingBefore.filter(row => !removedKeys.has(arrivalKeyOf(row)));
    if (pendingKept.length !== pendingBefore.length) {
      state.arrival = {
        ...(state.arrival || {}),
        pending: pendingKept,
        byKind: arrivalCountsOf(pendingKept),
        total: pendingKept.length,
        receipt: pendingKept.length ? `${pendingKept.length} new` : "live",
      };
    }
  }

  state.plans = Object.fromEntries(state.inventory.map(row => [arrivalKeyOf(row), row]));
  state.attachment_relations = attachmentRelationsOf(state.inventory);

  const heldKeys = new Set(
    (state.arrival && Array.isArray(state.arrival.pending) ? state.arrival.pending : [])
      .map(arrivalKeyOf)
  );
  const arrivals = pending.filter(row => !heldKeys.has(arrivalKeyOf(row)));
  if (arrivals.length) {
    const held = [
      ...((state.arrival && Array.isArray(state.arrival.pending)) ? state.arrival.pending : []),
      ...arrivals,
    ];
    state.arrival = {
      ...(state.arrival || {}),
      pending: held,
      byKind: arrivalCountsOf(held),
      total: held.length,
      receipt: `${held.length} new`,
    };
  }
  return true;
};

// ── Document checks ──────────────────────────────────────────────────────
// /_checks/<project> lists the documents that fail the render contract and how
// many verdicts are still pending; the server computes and keeps the verdicts.
// Each listed verdict lands on its row as `check`, and every other row's is
// cleared. While verdicts are pending the server is computing them, so the
// loader asks again after a pause, a bounded number of times.
//
// Nothing in the load sequence calls this: the verdicts are an overlay, and the
// shell asks for them once derived state has settled, so they never compete
// with the first paint or with discovery.
const CHECKS_RETRY_MS = 20000;
const CHECKS_MAX_RETRIES = 30;
let checksTimer = null;

window.loadDocumentChecks = async function (project, attempt = 0) {
  project = project ||
            (document.querySelector('meta[name="docs-project"]')?.content) ||
            window.location.pathname.replace(/^\/+/, "").split("/")[0];
  if (!project) return null;
  if (checksTimer !== null) {
    clearTimeout(checksTimer);
    checksTimer = null;
  }
  let payload = null;
  try {
    const response = await fetch(`/_checks/${project}`, { cache: "no-store" });
    if (response.ok) payload = await response.json();
  } catch (cause) {
    payload = null; // no checks route: the rows render without verdicts
  }
  if (!payload || !Array.isArray(payload.documents)) return null;
  const byKey = new Map(
    payload.documents.map(doc => [arrivalKeyOf(mapInventoryRow(doc)), doc])
  );
  const state = window.STATE;
  if (state && Array.isArray(state.inventory)) {
    for (const row of state.inventory) {
      const doc = byKey.get(arrivalKeyOf(row));
      row.check = doc
        ? { errors: doc.errors, warnings: doc.warnings, findings: doc.findings, path: doc.path }
        : null;
    }
    state.checks = {
      checked: payload.checked,
      pending: payload.pending,
      failing: payload.failing,
    };
  }
  window.dispatchEvent(new CustomEvent("reckon:checks", { detail: state?.checks || null }));
  if (payload.pending > 0 && payload.refreshing && attempt < CHECKS_MAX_RETRIES) {
    checksTimer = setTimeout(
      () => window.loadDocumentChecks(project, attempt + 1),
      CHECKS_RETRY_MS,
    );
  }
  return payload;
};

window.revalidateProjectState = async function () {
  const PROJECT = (document.querySelector('meta[name="docs-project"]')?.content) ||
                  window.location.pathname.replace(/^\/+/, "").split("/")[0] ||
                  "unknown";
  const indexEndpoint = `/_index/${PROJECT}`;
  const discoveryEndpoint = `/_discover/${PROJECT}`;

  window.STATE_LOAD = {
    endpoint: indexEndpoint,
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

  // ── Derived blocks ───────────────────────────────────────────────────────
  // What a payload carries beyond its rows. Both the first paint and the
  // background merge read them from the rows on screen, so the sprint
  // augmentation and the counts always describe the same inventory the list
  // renders.
  const augmentSprints = (sprints, inventory) => sprints.map(s => {
    const explicit = new Set(
      (s.items || []).map(it => typeof it === "string" ? it : it.slug)
    );
    const auto = inventory
      .filter(p => p.type === "plan" && p.sprint === s.id && !explicit.has(p.slug))
      .map(p => p.slug);
    return auto.length ? { ...s, items: [...(s.items || []), ...auto] } : s;
  });

  const derivedBlocks = (mergedInventory, { sprints, disc = null, idx = {} }) => {
    const augmentedSprints = augmentSprints(sprints, mergedInventory);
    const activeSprintId = disc?.active_sprint_id ?? idx.active_sprint_id ?? null;
    const activeSprints = augmentedSprints.filter(s => s.status === "active");
    const activeSprintConflict = activeSprints.length === 0
      ? activeSprintId !== null
      : activeSprints.length !== 1 || activeSprints[0].id !== activeSprintId;
    const activeSprint = augmentedSprints.find(s => s.id === activeSprintId)
                      || augmentedSprints.find(s => s.status === "active");
    const surfaceState = disc ?? idx;
    const readySet = (
      surfaceState.ready_set &&
      typeof surfaceState.ready_set === "object" &&
      !Array.isArray(surfaceState.ready_set)
    ) ? surfaceState.ready_set : {};
    const endpoints = Array.isArray(surfaceState.endpoints)
      ? surfaceState.endpoints
      : (Array.isArray(readySet.endpoints) ? readySet.endpoints : []);
    return {
      sprints: augmentedSprints,
      active_sprint_id: activeSprintId,
      active_sprints: activeSprints,
      active_sprint_conflict: activeSprintConflict,
      sprint: activeSprint,
      ready_set: readySet,
      endpoints,
      source_format: disc?.source_format ?? idx.source_format ?? "legacy-index",
      resource_versions: disc?.resource_versions ?? idx.resource_versions ?? {},
      blockers: Array.isArray(disc?.blockers) ? disc.blockers
                : (Array.isArray(idx.blockers) ? idx.blockers : []),
      timeline: Array.isArray(disc?.timeline) ? disc.timeline
                : (Array.isArray(idx.timeline) ? idx.timeline : []),
      schedule: (surfaceState && typeof surfaceState.schedule === "object" && surfaceState.schedule !== null)
        ? surfaceState.schedule
        : null,
    };
  };

  // Ensure projects[0] is populated. Some central-index repos (e.g. imas-efit)
  // have data.plans[] + data.counts + data.milestones at the top level and no
  // data.projects[]. Synthesise one so the SPA components can read uniformly.
  // idx.projects is non-empty only on the fallback path, where the aggregate
  // was actually read; a page whose index answered takes the synthesised row.
  const projectRows = (mergedInventory, milestones, idx) => {
    let projects = Array.isArray(idx.projects) ? idx.projects.slice() : [];

    // Live counts derived from the recovered inventory. The served rows are
    // the authoritative plan list; the persisted projects[] counts in
    // index.json go stale (the audit recomputes rollups in its response but
    // never writes them). So whenever a page has an inventory, the counts
    // shown come from it — never from the persisted block. When nothing
    // answered, liveCounts is null and the synthesised row falls back to the
    // persisted counts as a last resort.
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
      // Persisted projects[0] exists: overlay live counts (when available) so
      // the cockpit never shows a stale plan count, and backfill milestones if
      // absent.
      projects = projects.map((p, i) => i === 0
        ? {
            ...p,
            ...(liveCounts || {}),
            milestones: (Array.isArray(p.milestones) && p.milestones.length) ? p.milestones : milestones,
          }
        : p);
    }
    return projects;
  };

  // ── Assembly ─────────────────────────────────────────────────────────────
  // One source's rows and derived blocks become the state a page renders. The
  // index passes rows and nothing derived; the fallback passes whichever
  // payload answered.
  const assemble = ({ rows, sprints = [], milestones = [], northStars = [], disc = null, idx = {} }) => {
    const mergedInventory = rows.map(mapInventoryRow);
    const plans = Object.fromEntries(mergedInventory.map(inv => [inv.nav_key, inv]));
    const attachmentRelations = attachmentRelationsOf(mergedInventory);

    // ── Arrival diff ─────────────────────────────────────────────────────
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

    return {
      today:            new Date().toISOString().slice(0, 10),
      project:          PROJECT,
      projects:         projectRows(mergedInventory, milestones, idx),
      milestones,
      north_stars:      northStars,
      inventory:        mergedInventory,
      loaded_at:        new Date().toISOString(),
      ...derivedBlocks(mergedInventory, { sprints, disc, idx }),
      attachment_relations: attachmentRelations,
      plans,
      arrival,
    };
  };

  // ── Derived state, when it arrives ───────────────────────────────────────
  // The discovery payload is the same rows plus the derived fields and the
  // ready set. It lands on the rows already on screen: each row keeps its
  // object and its place, a row the index did not carry is held as an arrival
  // rather than inserted, and the derived blocks are refreshed around them.
  const mergeDerived = (disc) => {
    const state = window.STATE;
    if (!state || !Array.isArray(state.inventory)) return state;
    const byKey = new Map(state.inventory.map(row => [arrivalKeyOf(row), row]));
    const pending = [];
    for (const row of (Array.isArray(disc.inventory) ? disc.inventory : []).map(mapInventoryRow)) {
      const key = arrivalKeyOf(row);
      if (!key) continue;
      const onScreen = byKey.get(key);
      if (onScreen) {
        Object.assign(onScreen, row);
      } else {
        // A row the index did not carry is held as an arrival: the open list
        // keeps the rows the reader is already looking at.
        pending.push(row);
      }
    }
    state.plans = Object.fromEntries(state.inventory.map(row => [arrivalKeyOf(row), row]));
    state.attachment_relations = attachmentRelationsOf(state.inventory);
    Object.assign(
      state,
      derivedBlocks(state.inventory, {
        sprints: Array.isArray(disc.sprints) ? disc.sprints : state.sprints,
        disc,
        idx: {},
      }),
      {
        milestones: Array.isArray(disc.milestones) ? disc.milestones : state.milestones,
        projects: projectRows(
          state.inventory,
          Array.isArray(disc.milestones) ? disc.milestones : state.milestones,
          {},
        ),
        loaded_at: new Date().toISOString(),
      },
    );
    if (pending.length) {
      const held = [
        ...((state.arrival && Array.isArray(state.arrival.pending)) ? state.arrival.pending : []),
        ...pending,
      ];
      state.arrival = {
        ...(state.arrival || {}),
        pending: held,
        byKind: arrivalCountsOf(held),
        total: held.length,
        receipt: `${held.length} new`,
      };
    }
    return state;
  };

  // ── The index answers the first paint ────────────────────────────────────
  let indexRows = null;
  try {
    const response = await fetch(indexEndpoint, { cache: "no-store" });
    if (response.ok) {
      const payload = await response.json();
      if (Array.isArray(payload)) indexRows = payload;
    }
  } catch (cause) {
    indexRows = null; // an unreachable index is the fallback's cue
  }

  if (indexRows !== null) {
    const state = assemble({ rows: indexRows });
    window.STATE = state;
    window.STATE_ERROR = null;
    // The derived fetch starts one task later, in the background: the first
    // paint is not competing with it, and nothing the reader can already see
    // waits on it. A discovery response that never comes, or comes back
    // broken, leaves the painted rows exactly as they are.
    window.STATE_DERIVED_READY = new Promise(resolve => {
      setTimeout(async () => {
        const disc = await getJson(discoveryEndpoint);
        if (disc) mergeDerived(disc);
        resolve(window.STATE);
      }, 0);
    });
    return state;
  }

  // ── Fallback: the index is unavailable ───────────────────────────────────
  window.STATE_LOAD.endpoint = discoveryEndpoint;

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

  // ── Central-index layout: data.plans[] (no data.inventory) ─────────────
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

  // ── Discovery, when it answered, is the authority ────────────────────────
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

  const state = assemble({ rows: inventory, sprints, milestones, northStars, disc, idx });
  window.STATE = state;
  window.STATE_ERROR = null;
  // This path already carries everything a source had, derived state included,
  // so the promise a consumer awaits is settled with the state it can read.
  window.STATE_DERIVED_READY = Promise.resolve(state);
  return state;
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
  // At most one derived-state refetch per settle window, so a burst of change
  // events — a save touches several files, and a page may mix pushed rows with
  // digest-only events — costs one revalidation, and the rows the reader is
  // looking at have already been patched from the pushes.
  let settleTimer = null;
  const refetchDerived = () => {
    if (settleTimer !== null) return;
    settleTimer = setTimeout(async () => {
      settleTimer = null;
      try {
        await onChange?.();
      } catch (cause) {
        // The rows are already patched; a failed background refetch leaves
        // them on screen, and the next change event tries again.
      }
    }, CHANGE_SETTLE_MS);
  };
  changes.addEventListener("change", (event) => {
    let payload = null;
    try {
      payload = JSON.parse(event?.data ?? "null");
    } catch (cause) {
      payload = null;
    }
    // A pushed row is applied in place now; every event shape shares the one
    // refetch that follows, as a digest-only event always asked for.
    if (payload?.rows) applyIndexRowChanges(payload.rows);
    refetchDerived();
  });
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
