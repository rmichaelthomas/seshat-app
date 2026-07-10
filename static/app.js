// ── State ──────────────────────────────────────────────────────────────────

let projects     = [];
let orphans      = [];
let groups       = [];
let activeFilter = "all";
let selectedName = null;
let activeView   = "projects";   // "projects" | "agreements" | "receipts" | "invariant" | "revocations" | "vault" | "organize" (organize is a Projects sub-view, not a top-level domain-view)
let routerStatus = null;   // result of GET /api/router/status
let hostnames = [];   // [{project_name, hostname, port}] from /api/router/hostnames
let _refreshFailCount = 0;

// ── Receipts / Vault detail-panel selection state ────────────────────────────
// selectedName (above) tracks the selected Projects row; these track the
// equivalent selection for the other domains that now share #detailPanel.
let selectedReceiptHash = null;
let selectedVaultKey    = null;
let selectedAgreementRule  = null; // { source: "agreement"|"revocation", index } | null
let selectedInvariantClaim = null; // index into invariantLastRunCache.invariant.claims | null
let selectedRevocationRule = null; // index into revocationsCache.rules | null

// ── Agreements / Invariant / Revocations domain data caches ──────────────────
// Populated on domain entry (renderAgreementsView/renderInvariantView/
// renderRevocationsView) — unlike `projects`, these three aren't polled, so
// the cache is simply "whatever the last fetch on this domain returned."
let agreementCache      = null; // last GET /api/agreement response
let revocationsCache    = null; // last GET /api/revocations response
let invariantCache      = null; // last GET /api/invariant response
let invariantLastRunCache = null; // last GET /api/invariant/last-run response
let revocationDenialCountsCache = {}; // { ruleIndex: count } from the best-effort receipts join

// ── Receipts follow/tail-mode state ──────────────────────────────────────────
let receiptsCache     = [];   // currently-rendered receipts, newest first
let receiptsFollowing = true; // default-on, matches the reference
let receiptsFollowTimer = null;

// ── Vault detail-panel data cache (populated by renderVaultView()) ──────────
let vaultAuditCache   = [];
let vaultSummaryCache = null;

const uiState = {
  searchQuery: "",
  sortField: "name",
  sortDir: "asc",
  editingConfig: null,
  dirtyFields: {},
};

// ── Keyboard shortcuts ────────────────────────────────────────────────────

function initKeyboard() {
  document.addEventListener("keydown", e => {
    const tag = e.target.tagName;
    const inInput = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";

    // Escape: cascading dismiss (innermost first)
    if (e.key === "Escape") {
      if ($("confirmOverlay").classList.contains("open")) return; // handled by confirmAction itself
      if ($("discoverOverlay")?.style.display !== "none" && $("discoverOverlay")?.style.display) { closeDiscover(); return; }
      if ($("githubImportOverlay")?.style.display !== "none" && $("githubImportOverlay")?.style.display) { closeGitHubImportModal(); return; }
      if ($("githubTokenOverlay")?.style.display !== "none" && $("githubTokenOverlay")?.style.display) { closeGitHubTokenModal(); return; }
      if ($("routerModalOverlay")?.style.display !== "none" && $("routerModalOverlay")?.style.display) { closeSetupModal(); return; }
      if ($("modalOverlay").classList.contains("open"))         { closeProjectModal(); return; }
      if ($("groupModalOverlay").classList.contains("open"))    { closeGroupModal();   return; }
      if ($("vaultKeyModalOverlay")?.classList.contains("open")) { closeVaultKeyModal(); return; }
      if (uiState.editingConfig) { cancelConfigEdit(uiState.editingConfig); return; }
      if ($("detailPanel").classList.contains("open")) { closeDetail(); return; }
      return;
    }

    // Enter in config edit mode (not in textarea): save
    if (e.key === "Enter" && uiState.editingConfig && inInput && tag !== "TEXTAREA") {
      e.preventDefault();
      saveConfig(uiState.editingConfig);
      return;
    }

    // / key (not in input): focus search bar
    if (e.key === "/" && !inInput) {
      e.preventDefault();
      $("searchInput")?.focus();
      return;
    }
  });
}

// ── Boot ───────────────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
  initSearchSort();
  initProjectModal();
  initGroupModal();
  initVaultKeyModal();
  initKeyboard();
  initSearchHint();
  renderSidebar();
  // Organize is a Projects sub-view (not its own domain-view); hidden until toggled.
  $("organizeView").style.display = "none";
  refresh();
  loadSetupStatus();
  loadHostnames();
  setInterval(refresh, 5000);
});

// ── Data ───────────────────────────────────────────────────────────────────

async function refresh() {
  try {
    const [projRes, orphanRes, groupRes, hostnamesRes] = await Promise.all([
      fetch("/api/projects"),
      fetch("/api/orphans"),
      fetch("/api/groups"),
      fetch("/api/router/hostnames"),
    ]);
    projects  = await projRes.json();
    orphans   = await orphanRes.json();
    groups    = await groupRes.json();
    hostnames = await hostnamesRes.json();

    _refreshFailCount = 0;
    hideStaleBanner();

    if (activeView === "projects") {
      render();
      if (selectedName && !uiState.editingConfig) updateDetailPanel(selectedName);
    } else {
      renderGroups();
      renderCounts();
    }
  } catch (_) {
    _refreshFailCount++;
    if (_refreshFailCount >= 2) showStaleBanner();
  }
}

function showStaleBanner() {
  const b = $("staleBanner");
  if (b) b.style.display = "flex";
  const shelf = $("projectShelf");
  if (shelf) shelf.style.opacity = "0.5";
}

function hideStaleBanner() {
  const b = $("staleBanner");
  if (b) b.style.display = "none";
  const shelf = $("projectShelf");
  if (shelf) shelf.style.opacity = "";
}

// ── Domain nav (six-domain redesign) ────────────────────────────────────────
//
// The six top-level domains, each with its own <section class="domain-view"
// id="view-{id}"> in templates/index.html. showDomain() is the single source
// of truth for which one is visible; renderSidebar() builds the sidebar rows
// from the same list and keeps the active row in sync.
//
// "organize" is NOT one of these six ids — it's a sub-view nested inside
// view-projects (see showOrganizeView below), so it has no #view-organize
// section of its own and is never passed to showDomain().

const DOMAINS = [
  ["projects",    "◈", "Projects"],
  ["agreements",  "☰", "Agreements"],
  ["receipts",    "⧫", "Receipts"],
  ["invariant",   "◇", "Invariant"],
  ["revocations", "⊘", "Revocations"],
  ["vault",       "⚿", "Vault"],
];

// One subnav renderer per domain that has one. Agreements/Invariant/
// Revocations extend the same mechanism Task 3 established for Projects
// (renderProjectsSubnav) — a plain function returning subnav HTML, looked up
// by domain id rather than chained in an ever-longer ternary.
const DOMAIN_SUBNAV_RENDERERS = {
  projects:    renderProjectsSubnav,
  agreements:  renderAgreementsSubnav,
  invariant:   renderInvariantSubnav,
  revocations: renderRevocationsSubnav,
};

function renderSidebar() {
  // Organize is shown nested inside the Projects domain-view, so the
  // Projects row stays highlighted while Organize is active.
  const current = activeView === "organize" ? "projects" : activeView;
  $("sidebar").innerHTML = DOMAINS.map(([id, glyph, label]) => {
    const subnavFn = id === current ? DOMAIN_SUBNAV_RENDERERS[id] : null;
    return `
    <div class="nav-item${id === current ? " on" : ""}" onclick="showDomain('${id}')">
      <span class="ng">${glyph}</span> ${esc(label)}
    </div>${subnavFn ? subnavFn() : ""}`;
  }).join("");
  // Groups are data-driven (fetched separately from `groups`); populate/wire
  // the #groupList placeholder the subnav string just created. renderGroups()
  // itself no-ops when #groupList isn't in the DOM, so this is safe to call
  // even when the projects subnav wasn't rendered above.
  if (current === "projects") renderGroups();
}

// Projects sub-nav (replaces the Task-2-fix stopgap `.filters-groups-bar`):
// Status rows reuse the same activeFilter/renderCounts() computation the old
// bar used, Groups rows reuse renderGroups()'s existing data, and the Tools
// row is the only click-trigger for the Organize sub-view Task 2 left dangling.
function renderProjectsSubnav() {
  const running    = projects.filter(p => p.status === "running").length;
  const stopped    = projects.filter(p => p.status === "stopped").length;
  const conflict   = projects.filter(p => p.status === "conflict").length;
  const isOrganize = activeView === "organize";
  const statusItem = (filter, dotCls, label, count, countId) => `
    <div class="sub-item${(!isOrganize && activeFilter === filter) ? " on" : ""}" onclick="setProjectFilter('${filter}')">
      <span class="sdot${dotCls ? ` ${dotCls}` : ""}"></span> <span class="lbl">${label}</span> <span class="sc" id="${countId}">${count}</span>
    </div>`;
  return `
    <div class="subnav">
      <div class="subnav-h">Status</div>
      ${statusItem("all",      "",  "All",       projects.length, "count-all")}
      ${statusItem("running",  "g", "Running",   running,          "count-running")}
      ${statusItem("stopped",  "",  "Stopped",   stopped,          "count-stopped")}
      ${statusItem("conflict", "r", "Conflicts", conflict,         "count-conflict")}
      ${statusItem("orphans",  "o", "Orphans",   orphans.length,   "count-orphans")}
      <div class="subnav-h-row">
        <span class="subnav-h">Groups</span>
        <button class="sidebar-add-btn" id="addGroupBtn" title="New group" onclick="openGroupModal()">+</button>
      </div>
      <div id="groupList"><!-- rendered by JS: renderGroups() --></div>
      <div class="subnav-h">Tools</div>
      <div class="sub-item${isOrganize ? " on" : ""}" onclick="showOrganizeView()"><span class="lbl">Organize</span></div>
    </div>`;
}

// Shared "click a sub-nav row, scroll the main view to the matching anchor"
// mechanism for Agreements/Invariant/Revocations — the brief is explicit that
// this should be a simple scrollIntoView(), not a second view-mode toggle
// like Projects' activeFilter. `?.` guards the case where the anchor hasn't
// rendered yet (e.g. the domain's data fetch hasn't resolved).
function _scrollDomainAnchor(anchorId) {
  $(anchorId)?.scrollIntoView({ behavior: "smooth", block: "start" });
}

// "Sync log" has no backing endpoint (no sync-history API exists) — per the
// build brief, this surfaces that honestly via a toast rather than pretending
// to have data or silently doing nothing.
function showSyncLogUnavailable() {
  toast("Sync history isn't available yet.", "info");
}

// Agreements sub-nav: "Agreement rules" / "Platform revocations" (only when
// there are any) / "Dry run". Counts come from agreementCache/revocationsCache
// (populated by renderAgreementsView()); omitted (not "0") until that fetch
// resolves so the sub-nav never shows a fabricated count mid-load.
function renderAgreementsSubnav() {
  const ruleCount = agreementCache?.exists ? (agreementCache.rules || []).length : null;
  const revCount  = revocationsCache?.exists ? (revocationsCache.rules || []).length : 0;
  return `
    <div class="subnav">
      <div class="subnav-h">View</div>
      <div class="sub-item" onclick="_scrollDomainAnchor('agr-anchor-rules')">
        <span class="lbl">Agreement rules</span>${ruleCount !== null ? ` <span class="sc">${ruleCount}</span>` : ""}
      </div>
      ${revCount > 0 ? `
      <div class="sub-item" onclick="_scrollDomainAnchor('agr-anchor-revocations')">
        <span class="lbl">Platform revocations</span> <span class="sc">${revCount}</span>
      </div>` : ""}
      <div class="sub-item" onclick="_scrollDomainAnchor('agr-anchor-dryrun')"><span class="lbl">Dry run</span></div>
    </div>`;
}

// Invariant sub-nav: "Last run" / "Contract". Both are anchors within the
// single populated view (the reference's SUBNAV implies two switchable
// views, but per the brief's guidance for Agreements — "don't build a second
// view-mode toggle" — these just scroll to the matching section instead).
function renderInvariantSubnav() {
  return `
    <div class="subnav">
      <div class="subnav-h">View</div>
      <div class="sub-item" onclick="_scrollDomainAnchor('inv-anchor-lastrun')"><span class="lbl">Last run</span></div>
      <div class="sub-item" onclick="_scrollDomainAnchor('inv-anchor-contract')"><span class="lbl">Contract</span></div>
    </div>`;
}

// Revocations sub-nav: "Active" / "Sync log". "Sync log" has no backing data
// (see showSyncLogUnavailable()) so it's not a scroll target.
function renderRevocationsSubnav() {
  const count = revocationsCache?.exists ? (revocationsCache.rules || []).length : null;
  return `
    <div class="subnav">
      <div class="subnav-h">View</div>
      <div class="sub-item" onclick="_scrollDomainAnchor('rev-anchor-active')">
        <span class="lbl">Active</span>${count !== null ? ` <span class="sc">${count}</span>` : ""}
      </div>
      <div class="sub-item" onclick="showSyncLogUnavailable()"><span class="lbl">Sync log</span></div>
    </div>`;
}

// Sets the Projects status filter from a sidebar sub-nav click. Same
// activeFilter variable and render()-vs-switch-domain branching the old
// initFilters()-bound .filter-btn click handler used — just invoked directly
// from inline onclick since the sub-nav is re-rendered (not static markup),
// so a boot-time-only addEventListener binding would go stale after the
// first re-render.
function setProjectFilter(filter) {
  activeFilter = filter;
  if (activeView !== "projects") { showProjectView(); return; }
  renderSidebar();
  render();
}

function _activateDomainSection(id) {
  document.querySelectorAll(".domain-view").forEach(v => v.classList.remove("on"));
  $("view-" + id).classList.add("on");
}

// showDomain() owns switching to one of the six real domains: it gates on
// unsaved detail-panel edits (same protection showVaultView()/etc. always
// had), flips the visible .domain-view section, updates activeView, keeps
// the sidebar in sync, and triggers that domain's render/load.
async function showDomain(id) {
  if (id === activeView) return; // re-clicking the active nav-item is a no-op
  if (await closeDetail() === false) return;
  if (activeView === "receipts" && id !== "receipts") stopReceiptsFollow();
  _activateDomainSection(id);
  activeView = id;
  renderSidebar();
  $("addProjectBtn").style.display = (id === "projects") ? "" : "none";
  $("searchSortBar").style.display = (id === "projects") ? "" : "none";
  if (id === "projects") {
    $("organizeView").style.display = "none";
    $("projectView").style.display  = "block";
    render();
  } else if (id === "vault") {
    await renderVaultView();
  } else if (id === "receipts") {
    await renderReceiptsView();
  } else if (id === "agreements") {
    await renderAgreementsView();
  } else if (id === "invariant") {
    await renderInvariantView();
  } else if (id === "revocations") {
    await renderRevocationsView();
  }
}

// ── View switching ─────────────────────────────────────────────────────────

function toggleVaultView() {
  if (activeView === "vault") {
    showProjectView();
  } else {
    showVaultView();
  }
}

function toggleOrganizeView() {
  if (activeView === "organize") {
    showProjectView();
  } else {
    showOrganizeView();
  }
}

async function showProjectView() {
  await showDomain("projects");
}

async function installVaultDeps() {
  // Targets #vaultEncBanner/#vaultInstallDepsBtn (restyled to .banner.warn in
  // this task) rather than the old .vault-enc-badge.warn markup those ids
  // replaced — this function's install-deps flow itself is unchanged.
  const btn = $("vaultInstallDepsBtn");
  if (btn) { btn.disabled = true; btn.textContent = "Installing…"; }
  try {
    const res  = await fetch("/api/vault/install-deps", { method: "POST" });
    if (!res.ok) {
      const text = await res.text();
      throw new Error(res.status + " — " + text.slice(0, 120));
    }
    const data = await res.json();
    if (!data.ok) {
      toast("Install failed: " + (data.error || "unknown error"), "error");
      if (btn) { btn.disabled = false; btn.textContent = "Fix: Install deps"; }
      return;
    }
    const banner = $("vaultEncBanner");
    if (banner) banner.innerHTML =
      `<span class="bi" style="color:var(--yellow)">⚠</span> Installed — restart Seshat to activate encryption`;
  } catch (e) {
    toast("Install failed: " + e.message, "error");
    if (btn) { btn.disabled = false; btn.textContent = "Fix: Install deps"; }
  }
}

async function showVaultView() {
  await showDomain("vault");
}

// Organize is a Projects sub-view (no #view-organize section of its own), so
// it can't go through showDomain() — that would re-show #projectView, not
// #organizeView. It duplicates showDomain()'s gating/section-activation
// instead.
async function showOrganizeView() {
  if (activeView === "organize") return; // already showing; no-op
  if (await closeDetail() === false) return;
  _activateDomainSection("projects");
  activeView = "organize";
  renderSidebar();
  $("projectView").style.display  = "none";
  $("organizeView").style.display = "block";
  $("addProjectBtn").style.display = "none";
  $("searchSortBar").style.display = "none";
  await Promise.all([loadFolderMap(), loadRecommendations(), loadMoveHistory()]);
}

function toggleReceiptsView() {
  if (activeView === "receipts") {
    showProjectView();
  } else {
    showReceiptsView();
  }
}

async function showReceiptsView() {
  await showDomain("receipts");
}

async function renderReceiptsView() {
  stopReceiptsFollow();
  $("receiptsContent").innerHTML = `<div class="empty-state"><div class="empty-state-title">Loading receipts…</div></div>`;
  try {
    const [receiptsRes, statsRes] = await Promise.all([
      fetch("/api/receipts?limit=100"),
      fetch("/api/receipts/stats"),
    ]);
    const receipts = await receiptsRes.json();
    const stats    = await statsRes.json();

    // No existing code verifies hash-chain integrity over the API surface
    // this dashboard is allowed to call (seshat.py has no such endpoint; the
    // only verification that exists is `seshat receipts verify` in cli.py, a
    // local CLI-only command). So this view never claims "intact" — it shows
    // real, unverified facts only: count and the head receipt's own hash.
    if (stats.total === 0) {
      $("receiptsContent").innerHTML = `
        <div class="view-title"><h1>Receipts</h1></div>
        <div class="empty-wrap"><div class="empty-card">
          <div class="empty-glyph">⧫</div>
          <div class="empty-title">No receipts yet</div>
          <div class="empty-desc">Receipts are emitted as governed actions run. Start or stop a project, and its hash-chained receipt appears here.</div>
        </div></div>`;
      receiptsCache = [];
      return;
    }

    receiptsCache = receipts;

    const sessionOptions = stats.sessions.map(s =>
      `<option value="${esc(s)}">${esc(s)}</option>`
    ).join("");

    const actionOptions = Object.keys(stats.actions).sort().map(a =>
      `<option value="${esc(a)}">${esc(a)} (${stats.actions[a]})</option>`
    ).join("");

    const sessionsSummary = _receiptsSessionSummary(receipts);
    const headHash  = receipts[0] && receipts[0].receipt_hash;
    const headShort = headHash ? `${headHash.slice(0, 12)}…` : "—";

    $("receiptsContent").innerHTML = `
      <div class="view-title"><h1>Receipts</h1><span class="vsub">${stats.total} receipt${stats.total !== 1 ? "s" : ""}</span></div>
      <div class="stat-row">
        <div class="stat-card"><div class="sl">Total</div><div class="sv">${stats.total}</div><div class="sm">receipts</div></div>
        <div class="stat-card"><div class="sl">Sessions</div><div class="sv">${stats.sessions.length}</div><div class="sm">${esc(sessionsSummary)}</div></div>
        <div class="stat-card"><div class="sl">Actions</div><div class="sv">${Object.keys(stats.actions).length}</div><div class="sm">types</div></div>
        <div class="stat-card"><div class="sl">Chain</div><div class="sv" style="font-size:14px;color:var(--cyan)">${esc(headShort)}</div><div class="sm">head</div></div>
      </div>
      <div class="chain-strip">
        <span class="bar-full">████████████</span>
        <span>${stats.total} receipt${stats.total !== 1 ? "s" : ""}, hash-chained</span>
        <span class="bspacer"></span>
        <span class="num">head</span> <span class="hh">${esc(headShort)}</span>
      </div>
      <div class="follow-strip">
        <span class="follow-toggle" id="followToggle" onclick="toggleReceiptsFollow()"><span class="pulse"></span> following</span>
        <span>· new receipts prepend at the top (1s poll)</span>
        <span style="margin-left:auto;display:flex;gap:8px;align-items:center">
          <select id="receiptsFilterAction" onchange="filterReceipts()" style="font-size:12px">
            <option value="">All actions</option>
            ${actionOptions}
          </select>
          <select id="receiptsFilterSession" onchange="filterReceipts()" style="font-size:12px">
            <option value="">All sessions</option>
            ${sessionOptions}
          </select>
          <button class="btn btn-ghost btn-sm" onclick="renderReceiptsView()">↺ Refresh</button>
        </span>
      </div>
      <table class="tbl">
        <thead><tr><th style="width:90px">Time</th><th style="width:160px">Action</th><th>Target</th><th style="width:140px">Actor</th><th style="width:40px"></th></tr></thead>
        <tbody id="receiptsTbody">${renderReceiptRows(receipts)}</tbody>
      </table>`;
    wireReceiptRowClicks();
    updateFollowStripUI();
    if (receiptsFollowing) startReceiptsFollow();
  } catch (e) {
    $("receiptsContent").innerHTML = `<div class="empty-state"><div class="empty-state-title">Could not load receipts</div><div class="empty-state-sub">${esc(e.message)}</div></div>`;
  }
}

async function filterReceipts() {
  const action  = $("receiptsFilterAction").value;
  const session = $("receiptsFilterSession").value;
  let url = "/api/receipts?limit=100";
  if (action)  url += `&action=${encodeURIComponent(action)}`;
  if (session) url += `&session=${encodeURIComponent(session)}`;
  try {
    const res      = await fetch(url);
    const receipts = await res.json();
    receiptsCache = receipts;
    $("receiptsTbody").innerHTML = renderReceiptRows(receipts);
    wireReceiptRowClicks();
  } catch (e) {
    $("receiptsTbody").innerHTML = `<tr><td colspan="5" class="receipts-empty">${esc(e.message)}</td></tr>`;
  }
}

function wireReceiptRowClicks() {
  document.querySelectorAll("#receiptsTbody tr[data-hash]").forEach(row => {
    row.addEventListener("click", () => selectReceipt(row.dataset.hash));
  });
}

// Maps a receipt's action string to the reference's badge color families
// (start=green, stop=red, register=blue, revoke=orange). Real action names
// emitted by cli.py/mcp_server.py beyond the reference's four examples
// (set_project_override, set_secret) are judgment calls, folded into the
// "register" family since they're writes/declarations, not starts/stops.
function badgeClassForAction(action) {
  if (/^start/.test(action))    return "start";
  if (/^stop/.test(action))     return "stop";
  if (/^register/.test(action)) return "register";
  if (/revoke/.test(action))    return "revoke";
  if (/^set_/.test(action))     return "register";
  return "";
}

// "N mcp · N cli" style breakdown for the Sessions stat card, computed from
// the already-fetched receipts list (actor.type per distinct session_id) —
// not a new backend field, /api/receipts/stats doesn't return this shape.
function _receiptsSessionSummary(receipts) {
  const seen = new Set();
  const counts = {};
  for (const r of receipts) {
    const sid = r.actor && r.actor.session_id;
    if (!sid || seen.has(sid)) continue;
    seen.add(sid);
    const t = ((r.actor && r.actor.type) || "unknown").replace("_session", "");
    counts[t] = (counts[t] || 0) + 1;
  }
  return Object.entries(counts).map(([t, n]) => `${n} ${t}`).join(" · ");
}

function renderReceiptRows(receipts) {
  if (!receipts || receipts.length === 0) {
    return `<tr><td colspan="5" class="receipts-empty">No receipts match this filter.</td></tr>`;
  }
  return receipts.map(r => {
    const ts      = new Date(r.timestamp);
    const timeStr = ts.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    const isSuccess = r.result && r.result.status === "success";
    const actor      = r.actor || {};
    const actorTypeCls = (actor.type || "").replace("_session", "");
    const sessionShort = (actor.session_id || "").replace("mcp_session_", "").slice(0, 8);

    const target = r.target || {};
    const targetParts = [];
    if (target.project) targetParts.push(target.project);
    if (target.group)   targetParts.push(`group: ${target.group}`);
    if (target.port)    targetParts.push(`:${target.port}`);
    if (target.key)     targetParts.push(`key: ${target.key}`);
    const targetStr = targetParts.join(" · ") || "—";

    return `
      <tr data-hash="${esc(r.receipt_hash)}">
        <td class="num">${esc(timeStr)}</td>
        <td><span class="badge ${badgeClassForAction(r.action)}">${esc(r.action)}</span></td>
        <td>${esc(targetStr)}</td>
        <td class="actor ${esc(actorTypeCls)}">${esc(actorTypeCls)}${sessionShort ? ` · ${esc(sessionShort)}` : ""}</td>
        <td style="color:${isSuccess ? "var(--green)" : "var(--red)"}">${isSuccess ? "✓" : "✗"}</td>
      </tr>`;
  }).join("");
}

// ── Receipts detail panel + follow/tail mode ─────────────────────────────────

function selectReceipt(hash) {
  const r = receiptsCache.find(x => x.receipt_hash === hash);
  if (!r) return;
  selectedReceiptHash = hash;
  selectedVaultKey    = null;
  selectedName        = null;
  document.querySelectorAll(".tbl tbody tr.sel").forEach(row => row.classList.remove("sel"));
  document.querySelector(`#receiptsTbody tr[data-hash="${CSS.escape(hash)}"]`)?.classList.add("sel");
  $("detailInner").innerHTML = renderReceiptDetail(r);
  $("detailPanel").classList.add("open");
}

function renderReceiptDetail(r) {
  const ts      = new Date(r.timestamp);
  const timeStr = ts.toLocaleString("en-US", {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
  const isSuccess = r.result && r.result.status === "success";
  const actor     = r.actor || {};
  const agentHint = actor.agent_hint && actor.agent_hint !== "unknown" ? actor.agent_hint : null;
  const sessionShort = (actor.session_id || "").replace("mcp_session_", "").slice(0, 8);

  const target = r.target || {};
  const targetRows = [];
  if (target.project) targetRows.push(["project", target.project]);
  if (target.group)   targetRows.push(["group", target.group]);
  if (target.port)    targetRows.push(["port", `:${target.port}`]);
  if (target.key)     targetRows.push(["key", target.key]);

  const before = r.environment_before || {};
  const after  = r.environment_after  || {};
  const portsBefore = (before.listening_ports || []).length;
  const portsAfter  = (after.listening_ports  || []).length;
  const portDelta   = portsAfter - portsBefore;
  const portDeltaStr = portDelta > 0
    ? `+${portDelta} port${portDelta !== 1 ? "s" : ""}`
    : portDelta < 0
      ? `${portDelta} port${Math.abs(portDelta) !== 1 ? "s" : ""}`
      : "no port change";

  const resultDetail = [];
  if (r.result) {
    if (r.result.pid)         resultDetail.push(`PID ${r.result.pid}`);
    if (r.result.stopped_pid) resultDetail.push(`stopped PID ${r.result.stopped_pid}`);
    if (r.result.error)       resultDetail.push(r.result.error);
  }

  const short = h => h ? `${h.slice(0, 12)}…` : "—";

  // Most receipts on a fresh Phase-1 machine have no `invariant` key (the
  // harness is an optional external package) — omit the section entirely
  // rather than render an empty one. Only fields confirmed present on the
  // dataclass output (invariant_check.py) are shown; nothing fabricated.
  const inv = r.invariant;
  const invariantSection = inv ? `
    <div class="d-h">Invariant</div>
    <div class="d-row"><span class="k">result</span><span class="v" style="color:${inv.converged ? "var(--green)" : "var(--orange)"}">${inv.converged ? "converged" : "not converged"}</span></div>
    <div class="d-row"><span class="k">claims</span><span class="v">${(inv.claims || []).length}</span></div>
    <div class="d-row"><span class="k">cycles</span><span class="v">${inv.total_cycles ?? "—"}</span></div>
    ${inv.error ? `<div class="d-row"><span class="k">error</span><span class="v" style="color:var(--red)">${esc(inv.error)}</span></div>` : ""}` : "";

  return `
    <div class="detail-close-row"><button class="icon-btn" onclick="closeDetail()">✕</button></div>
    <div class="d-title">${esc(r.action)}</div>
    <div class="d-meta">${esc(timeStr)} · ${isSuccess ? "✓ success" : "✗ failed"}</div>
    <div class="d-h">Actor</div>
    <div class="d-row"><span class="k">type</span><span class="v" style="color:var(--purple)">${esc(actor.type || "—")}</span></div>
    <div class="d-row"><span class="k">session</span><span class="v">${esc(sessionShort || "—")}</span></div>
    ${agentHint ? `<div class="d-row"><span class="k">agent hint</span><span class="v">${esc(agentHint)}</span></div>` : ""}
    <div class="d-h">Target</div>
    ${targetRows.length
      ? targetRows.map(([k, v]) => `<div class="d-row"><span class="k">${esc(k)}</span><span class="v">${esc(v)}</span></div>`).join("")
      : `<div class="d-row"><span class="k">—</span><span class="v">—</span></div>`}
    <div class="d-h">Result</div>
    <div class="d-row"><span class="k">environment</span><span class="v">${esc(portDeltaStr)}</span></div>
    ${resultDetail.length ? `<div class="d-row"><span class="k">detail</span><span class="v">${esc(resultDetail.join(" · "))}</span></div>` : ""}
    <div class="d-h">Chain</div>
    <div class="d-row"><span class="k">receipt</span><span class="v" style="color:var(--cyan)">${short(r.receipt_hash)}</span></div>
    <div class="d-row"><span class="k">previous</span><span class="v" style="color:var(--cyan)">${short(r.previous_hash)}</span></div>
    ${invariantSection}`;
}

// Follow/tail mode: polls a small recent-receipts window every 1s and diffs
// by receipt_hash against what's already rendered. Only ever inserts the new
// row(s) — never re-fetches/redraws the whole table on a tick, which is the
// flicker failure mode the build prompt explicitly calls out.
function startReceiptsFollow() {
  stopReceiptsFollow();
  receiptsFollowTimer = setInterval(pollReceiptsFollow, 1000);
}

function stopReceiptsFollow() {
  if (receiptsFollowTimer) { clearInterval(receiptsFollowTimer); receiptsFollowTimer = null; }
}

function toggleReceiptsFollow() {
  receiptsFollowing = !receiptsFollowing;
  updateFollowStripUI();
  if (receiptsFollowing) startReceiptsFollow(); else stopReceiptsFollow();
}

function updateFollowStripUI() {
  const el = $("followToggle");
  if (!el) return;
  el.innerHTML = receiptsFollowing
    ? `<span class="pulse"></span> following`
    : `<span class="pulse" style="animation:none;opacity:.35"></span> paused`;
}

async function pollReceiptsFollow() {
  if (activeView !== "receipts") { stopReceiptsFollow(); return; }
  try {
    const action  = $("receiptsFilterAction")?.value || "";
    const session = $("receiptsFilterSession")?.value || "";
    let url = "/api/receipts?limit=5";
    if (action)  url += `&action=${encodeURIComponent(action)}`;
    if (session) url += `&session=${encodeURIComponent(session)}`;
    const res    = await fetch(url);
    const latest = await res.json();
    if (!latest.length) return;

    const known = new Set(receiptsCache.map(r => r.receipt_hash));
    const fresh = latest.filter(r => !known.has(r.receipt_hash));
    if (!fresh.length) return;

    // `fresh` is newest-first (a prefix of `latest`); insert oldest-of-batch
    // first so the final DOM order stays newest-at-top.
    for (let i = fresh.length - 1; i >= 0; i--) prependReceiptRow(fresh[i]);
    receiptsCache = [...fresh, ...receiptsCache];
  } catch (_) { /* transient network error — try again next tick */ }
}

function prependReceiptRow(r) {
  const tbody = $("receiptsTbody");
  if (!tbody) return;
  const tmp = document.createElement("tbody");
  tmp.innerHTML = renderReceiptRows([r]);
  const row = tmp.firstElementChild;
  if (!row) return;
  row.classList.add("receipt-row-new");
  row.addEventListener("click", () => selectReceipt(row.dataset.hash));
  tbody.insertBefore(row, tbody.firstChild);
  setTimeout(() => row.classList.remove("receipt-row-new"), 1200);
}

// ── Shared formatting helpers (Agreements/Invariant/Revocations) ────────────

// No existing relative-time helper in app.js — receipts render absolute
// times only (renderReceiptRows/renderReceiptDetail use toLocaleTimeString).
// Written fresh here for the Agreements "synced Xm ago" banner and the
// Revocations sync-freshness banner. Returns null for missing/unparseable
// input so callers can fall back to their own copy ("never synced" etc.)
// instead of printing something meaningless.
function _relativeTime(isoString) {
  if (!isoString) return null;
  const then = new Date(isoString).getTime();
  if (Number.isNaN(then)) return null;
  const deltaSec = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (deltaSec < 60) return "just now";
  const deltaMin = Math.floor(deltaSec / 60);
  if (deltaMin < 60) return `${deltaMin}m ago`;
  const deltaHr = Math.floor(deltaMin / 60);
  if (deltaHr < 24) return `${deltaHr}h ago`;
  const deltaDay = Math.floor(deltaHr / 24);
  return `${deltaDay}d ago`;
}

// Maps an Agreement/revocation rule's verb to the existing .verb-permit /
// .verb-forbid classes. The endpoint's documented shape allows a verb other
// than permit/forbid ("other") — that case renders as plain text with no
// color class rather than guessing.
function _agrVerbCls(verb) {
  if (verb === "permit") return "verb-permit";
  if (verb === "forbid") return "verb-forbid";
  return "";
}

// Pulls a bare `action is X` / `actor is X` value out of a canonical rule
// string. Real canonical output on this machine renders these unquoted
// (`action is start_project`), but the pattern also accepts a quoted form
// (`action is "start_project"`) since the interpreter's canonical renderer
// quotes some literals (e.g. `because "..."` reason clauses) and not others.
// This is intentionally simple substring extraction, not a parser — good
// enough for the best-effort override-status / recent-matches features the
// brief explicitly scopes down to "simple substring/field comparison."
function _extractAgrField(canonical, field) {
  if (!canonical) return null;
  const m = canonical.match(new RegExp(`\\b${field} is "?([^"\\s]+)"?`));
  return m ? m[1] : null;
}

// ── Agreements domain ─────────────────────────────────────────────────────

async function renderAgreementsView() {
  $("agreementsContent").innerHTML = `<div class="empty-state"><div class="empty-state-title">Loading agreement…</div></div>`;
  try {
    const [agrRes, revRes] = await Promise.all([
      fetch("/api/agreement"),
      fetch("/api/revocations"),
    ]);
    const agreement   = await agrRes.json();
    const revocations = await revRes.json();
    agreementCache   = agreement;
    revocationsCache = revocations;

    if (!agreement.exists) {
      $("agreementsContent").innerHTML = renderAgreementsEmptyState();
      renderSidebar();
      return;
    }

    const rules    = agreement.rules || [];
    const revRules = revocations.exists ? (revocations.rules || []) : [];

    let bannerWarn = "";
    if (revocations.exists && revRules.length > 0) {
      const syncedStr = revocations.sync && revocations.sync.last_checked
        ? `synced ${_relativeTime(revocations.sync.last_checked) || "recently"}`
        : "never synced";
      bannerWarn = `
      <div class="banner warn">
        <span class="bi" style="color:var(--red)">⊘</span> ${revRules.length} platform revocation${revRules.length !== 1 ? "s" : ""} overlay this Agreement
        <span class="bspacer"></span>
        <span class="num">${esc(syncedStr)}</span>
      </div>`;
    }

    $("agreementsContent").innerHTML = `
      <div class="view-title"><h1>Agreements</h1><span class="vsub">${rules.length} rule${rules.length !== 1 ? "s" : ""} · deny-by-default</span></div>
      <div class="banner ok"><span class="bi" style="color:var(--green)">☰</span> Agreement active at <code>~/.seshat/agreement.limn</code> <span class="bspacer"></span> <span class="num">${rules.length} rule${rules.length !== 1 ? "s" : ""}</span></div>
      ${bannerWarn}
      <table class="tbl" id="agr-anchor-rules">
        <thead><tr><th style="width:36px">#</th><th style="width:70px">Verb</th><th>Condition</th><th style="width:110px">Window</th><th style="width:90px">Source</th></tr></thead>
        <tbody>
          <tr><td colspan="5" class="tbl-section-label">Agreement rules</td></tr>
          ${renderAgreementRuleRows(rules, "agreement")}
          ${revRules.length ? `
          <tr id="agr-anchor-revocations"><td colspan="5" class="tbl-section-label">Platform revocations</td></tr>
          ${renderAgreementRuleRows(revRules, "revocation")}` : ""}
        </tbody>
      </table>
      <div class="dryrun" id="agr-anchor-dryrun">
        <div class="dryrun-h">Dry run — evaluate against the live Agreement (no side effects)</div>
        <div class="dryrun-form">
          <input class="actor-i" id="dryrunActor" placeholder="actor">
          <input class="action-i" id="dryrunAction" placeholder="action">
          <input class="scope-i" id="dryrunScope" placeholder="scope (optional)">
          <button class="btn btn-primary" onclick="runAgreementDryRun()">Check</button>
        </div>
        <div id="dryrunResultWrap"></div>
      </div>`;

    wireAgreementRowClicks();
    renderSidebar(); // refresh sub-nav counts now that data has loaded
  } catch (e) {
    $("agreementsContent").innerHTML = `<div class="empty-state"><div class="empty-state-title">Could not load Agreement</div><div class="empty-state-sub">${esc(e.message)}</div></div>`;
  }
}

function renderAgreementsEmptyState() {
  return `
    <div class="view-title"><h1>Agreements</h1></div>
    <div class="empty-wrap"><div class="empty-card">
      <div class="empty-glyph">☰</div>
      <div class="empty-title">No Agreement governs this machine</div>
      <div class="empty-desc">Agents are acting without deny-by-default enforcement. An Agreement defines what each actor may and may not do.</div>
      <div class="init-cmd"><div class="cl">initialize a starter Agreement</div><div class="cc"><span class="p">$</span> seshat agreement init</div></div>
      <div class="init-cmd"><div class="cl">or install an existing file</div><div class="cc"><span class="p">$</span> seshat agreement install &lt;path&gt;</div></div>
      <div class="platform-hint">translate a policy from English → <span class="lnk">liminate.dev/translate</span></div>
    </div></div>`;
}

// `source` is "agreement" | "revocation" — used for row numbering (1,2,3…
// vs R1,R2,R3…), the .src-tag variant, and which cache selectAgreementRule()
// re-reads the rule from on click.
function renderAgreementRuleRows(rules, source) {
  return rules.map((r, i) => {
    const rowNum = source === "revocation" ? `R${i + 1}` : `${i + 1}`;
    if (r.error) {
      return `<tr><td colspan="5" style="color:var(--red)">${esc(rowNum)}: ${esc(r.error)}</td></tr>`;
    }
    const srcTag = source === "revocation"
      ? `<span class="src-tag rev">revocation</span>`
      : `<span class="src-tag">agreement</span>`;
    const win = r.window || "unbounded";
    return `
      <tr data-source="${source}" data-index="${i}">
        <td class="num">${esc(rowNum)}</td>
        <td>${r.verb ? `<span class="${_agrVerbCls(r.verb)}">${esc(r.verb)}</span>` : "—"}</td>
        <td><span class="canon">${esc(r.canonical)}</span></td>
        <td><span class="win ${esc(win)}">${esc(win)}</span></td>
        <td>${srcTag}</td>
      </tr>`;
  }).join("");
}

function wireAgreementRowClicks() {
  document.querySelectorAll("#agr-anchor-rules tbody tr[data-index]").forEach(row => {
    row.addEventListener("click", () => selectAgreementRule(row.dataset.source, parseInt(row.dataset.index, 10)));
  });
}

function selectAgreementRule(source, index) {
  const rules = source === "revocation" ? (revocationsCache?.rules || []) : (agreementCache?.rules || []);
  const rule = rules[index];
  if (!rule || rule.error) return;
  selectedAgreementRule  = { source, index };
  selectedRevocationRule = null;
  selectedInvariantClaim = null;
  selectedReceiptHash     = null;
  selectedVaultKey        = null;
  selectedName            = null;
  document.querySelectorAll(".tbl tbody tr.sel").forEach(row => row.classList.remove("sel"));
  document.querySelector(`#agr-anchor-rules tbody tr[data-source="${source}"][data-index="${index}"]`)?.classList.add("sel");
  $("detailInner").innerHTML = `<div class="empty-state" style="padding:40px 20px"><div class="empty-state-sub">Loading…</div></div>`;
  $("detailPanel").classList.add("open");
  renderAgreementRuleDetail(source, index, rule);
}

// Async because "recent matches" needs a follow-up /api/receipts fetch.
// Renders a loading skeleton synchronously first (see selectAgreementRule),
// then patches #detailInner once the fetch resolves — guarded by a
// still-selected check so a fast second click can't land a stale response.
async function renderAgreementRuleDetail(source, index, rule) {
  const verbCls        = _agrVerbCls(rule.verb);
  const win             = rule.window || "unbounded";
  const overrideStatus  = _computeOverrideStatus(source, rule);
  const actionName      = _extractAgrField(rule.canonical, "action");

  let recentMatchesHTML = "";
  if (actionName) {
    try {
      const res = await fetch(`/api/receipts?action=${encodeURIComponent(actionName)}&limit=5`);
      if (res.ok) {
        const matches = await res.json();
        if (matches.length) {
          recentMatchesHTML = `
            <div class="d-h">Recent matches</div>
            ${matches.map(r => {
              const ts     = new Date(r.timestamp).toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
              const status = r.result && r.result.status;
              const label  = status === "success" ? "✓ permitted" : status === "denied" ? "✗ denied" : `— ${esc(status || "unknown")}`;
              const color  = status === "success" ? "var(--green)" : status === "denied" ? "var(--red)" : "var(--text-dim)";
              return `<div class="d-row"><span class="k">${esc(ts)}</span><span class="v" style="color:${color}">${label}</span></div>`;
            }).join("")}`;
        }
      }
    } catch (_) { /* best-effort only — omit the section on failure */ }
  }

  if (!selectedAgreementRule || selectedAgreementRule.source !== source || selectedAgreementRule.index !== index) return;

  $("detailInner").innerHTML = `
    <div class="detail-close-row"><button class="icon-btn" onclick="closeDetail()">✕</button></div>
    <div class="d-title">${source === "revocation" ? `Revocation R${index + 1}` : `Rule ${index + 1}`}</div>
    <div class="d-meta">${esc(rule.verb || "—")} · ${esc(win)} · ${source === "revocation" ? "revocation" : "agreement"}</div>
    <div class="d-h">Canonical form</div>
    <div class="d-canon">${esc(rule.canonical)}</div>
    <div class="d-h">Properties</div>
    <div class="d-row"><span class="k">verb</span><span class="v ${verbCls}">${esc(rule.verb || "—")}</span></div>
    <div class="d-row"><span class="k">window</span><span class="v">${esc(win)}</span></div>
    <div class="d-row"><span class="k">source</span><span class="v">${source === "revocation" ? "revocation" : "agreement"}</span></div>
    ${overrideStatus}
    ${recentMatchesHTML}`;
}

// Best-effort: does any revocation rule reference the same action or actor
// as this Agreement rule? Simple field-string comparison, not real semantic
// overlap detection (per the brief). Only meaningful for Agreement rules —
// revocations always win by construction, so they have no "override status"
// of their own. Returns "" (omit the section) when there's nothing to
// compare against; states "no overlapping revocation found" only when a real
// comparison against real revocation rules was actually performed.
function _computeOverrideStatus(source, rule) {
  if (source === "revocation") return "";
  const revRules = revocationsCache?.exists ? (revocationsCache.rules || []) : [];
  if (!revRules.length) return "";
  const myAction = _extractAgrField(rule.canonical, "action");
  const myActor  = _extractAgrField(rule.canonical, "actor");
  const overlapping = [];
  if (myAction || myActor) {
    revRules.forEach((rr, i) => {
      if (rr.error) return;
      const rAction = _extractAgrField(rr.canonical, "action");
      const rActor  = _extractAgrField(rr.canonical, "actor");
      if ((myAction && rAction && myAction === rAction) || (myActor && rActor && myActor === rActor)) {
        overlapping.push(i);
      }
    });
  }
  const rows = overlapping.length
    ? overlapping.map(i => `<div class="d-row"><span class="k">revocation R${i + 1}</span><span class="v" style="color:var(--red)">narrows this</span></div>`).join("")
    : `<div class="d-row"><span class="k">—</span><span class="v">no overlapping revocation found</span></div>`;
  return `<div class="d-h">Override status</div>${rows}`;
}

async function runAgreementDryRun() {
  const actor  = ($("dryrunActor")?.value || "").trim();
  const action = ($("dryrunAction")?.value || "").trim();
  const scope  = ($("dryrunScope")?.value || "").trim();
  const wrap = $("dryrunResultWrap");
  if (!wrap) return;
  if (!actor || !action) {
    wrap.innerHTML = `<div class="dryrun-result forbidden"><span class="drr-verdict">Enter both actor and action</span></div>`;
    return;
  }
  wrap.innerHTML = `<div class="dryrun-result" style="opacity:.6">Checking…</div>`;
  try {
    const body = { actor, action };
    if (scope) body.scope = scope; // omit scope from the body when blank, matching the endpoint's own handling
    const res  = await fetch("/api/agreement/check", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    wrap.innerHTML = renderDryRunResult(data);
  } catch (e) {
    wrap.innerHTML = `<div class="dryrun-result forbidden"><span class="drr-verdict">✗ ERROR</span><div class="drr-reason">${esc(e.message)}</div></div>`;
  }
}

// Only a true `mode === "permitted"` gets the green/permitted treatment —
// every denial mode (forbidden/default-deny/no-agreement/error) gets the
// red/forbidden treatment. The reference only defines two dryrun-result
// variants (.permitted/.forbidden); per the brief this is an explicit,
// documented judgment call rather than inventing a third CSS variant for
// edge cases (error/no-agreement) the reference doesn't cover.
function renderDryRunResult(data) {
  if (data.error && !data.mode) {
    return `<div class="dryrun-result forbidden"><span class="drr-verdict">✗ ERROR</span><div class="drr-reason">${esc(data.error)}</div></div>`;
  }
  const isPermitted = data.mode === "permitted";
  const cls          = isPermitted ? "permitted" : "forbidden";
  const verdictLabel = isPermitted ? "✓ PERMITTED" : `✗ ${(data.mode || "DENIED").toUpperCase()}`;
  const ruleBlock = data.rule
    ? `<div class="drr-rule">decided by <span class="canon">${esc(data.rule)}</span></div>` : "";
  return `
    <div class="dryrun-result ${cls}">
      <span class="drr-verdict">${verdictLabel}</span> <span class="num">· mode: ${esc(data.mode || "—")}</span>
      ${ruleBlock}
      <div class="drr-reason">${esc(data.reason || "")}</div>
    </div>`;
}

// ── Invariant domain ───────────────────────────────────────────────────────

async function renderInvariantView() {
  $("invariantContent").innerHTML = `<div class="empty-state"><div class="empty-state-title">Loading invariant…</div></div>`;
  try {
    const [invRes, lastRunRes] = await Promise.all([
      fetch("/api/invariant"),
      fetch("/api/invariant/last-run"),
    ]);
    const invariant = await invRes.json();
    const lastRun    = await lastRunRes.json();
    invariantCache        = invariant;
    invariantLastRunCache = lastRun;

    if (!invariant.exists) {
      $("invariantContent").innerHTML = renderInvariantEmptyState();
      renderSidebar();
      return;
    }

    const hasLastRun = !!lastRun.exists;
    const inv = hasLastRun ? lastRun.invariant : null;

    const harnessChip = (hasLastRun && inv && inv.harness_version)
      ? `<span class="num">harness v${esc(inv.harness_version)}</span>` : "";

    const lastRunTimeShort = (hasLastRun && lastRun.receipt_timestamp)
      ? new Date(lastRun.receipt_timestamp).toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit" })
      : null;
    const subtitle = lastRunTimeShort
      ? `semantic verification · last run ${lastRunTimeShort}`
      : "semantic verification";

    let bodyHTML;
    if (!hasLastRun) {
      // A contract can exist with zero runs so far — this is a legitimate
      // intermediate state, not an error or a domain-empty-state.
      bodyHTML = `<div style="padding:24px 4px;color:var(--text-muted);font-size:12px">No verification runs yet. Runs appear here after Invariant checks a permitted action.</div>`;
    } else {
      bodyHTML = await renderInvariantLastRunBody(inv, lastRun);
    }

    $("invariantContent").innerHTML = `
      <div class="view-title"><h1>Invariant</h1><span class="vsub">${esc(subtitle)}</span></div>
      <div class="banner ok" id="inv-anchor-contract"><span class="bi" style="color:var(--cyan)">◇</span> Verification contract at <code>~/.seshat/invariant.limn</code> <span class="bspacer"></span> ${harnessChip}</div>
      <div id="inv-anchor-lastrun">${bodyHTML}</div>`;

    if (hasLastRun) wireInvariantClaimRowClicks();
    renderSidebar();
  } catch (e) {
    $("invariantContent").innerHTML = `<div class="empty-state"><div class="empty-state-title">Could not load Invariant</div><div class="empty-state-sub">${esc(e.message)}</div></div>`;
  }
}

function renderInvariantEmptyState() {
  return `
    <div class="view-title"><h1>Invariant</h1></div>
    <div class="empty-wrap"><div class="empty-card">
      <div class="empty-glyph">◇</div>
      <div class="empty-title">No verification contract</div>
      <div class="empty-desc">Invariant runs semantic checks after each permitted action — confirming the environment actually matches what the action claimed. Without a contract, actions run unverified.</div>
      <div class="init-cmd"><div class="cl">initialize a verification contract</div><div class="cc"><span class="p">$</span> seshat invariant init</div></div>
    </div></div>`;
}

// Claim status vocabulary, confirmed against liminate_invariant.types
// (ClaimOutcome.status): "verified" | "corrected" | "escalated" | "pending"
// ("pending" is reserved for a deferred prediction capability this harness
// version never assigns — handled here as a graceful "unknown" fallback,
// not a crash, in case a future harness version does emit it).
function _claimStatusCls(status) {
  if (status === "verified")  return "claim-verified";
  if (status === "corrected") return "claim-corrected";
  if (status === "escalated") return "claim-escalated";
  return "";
}
function _claimStatusGlyph(status) {
  if (status === "verified")  return "●";
  if (status === "corrected") return "◐";
  if (status === "escalated") return "▲";
  return "○";
}

// Builds the stat-row + claim table for a last-run that exists. Async: the
// "From receipt" card's action name is a best-effort join against
// /api/receipts by receipt_hash (not fabricated — omitted if the join fails
// or the receipt can't be found, per the brief: "if that join is awkward,
// showing just the hash is fine").
async function renderInvariantLastRunBody(inv, lastRun) {
  const claims = inv.claims || [];
  const verifiedCount  = claims.filter(c => c.status === "verified").length;
  const correctedCount = claims.filter(c => c.status === "corrected").length;
  const escalatedCount = claims.filter(c => c.status === "escalated").length;
  const otherCount     = claims.length - verifiedCount - correctedCount - escalatedCount;
  const breakdownParts = [];
  if (verifiedCount)  breakdownParts.push(`${verifiedCount} verified`);
  if (correctedCount) breakdownParts.push(`${correctedCount} corrected`);
  if (escalatedCount) breakdownParts.push(`${escalatedCount} escalated`);
  if (otherCount)      breakdownParts.push(`${otherCount} other`);
  const breakdownStr = breakdownParts.join(" · ") || "—";

  const lastRunTs = lastRun.receipt_timestamp
    ? new Date(lastRun.receipt_timestamp).toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", second: "2-digit" })
    : "—";
  const receiptShort = lastRun.receipt_hash ? `${lastRun.receipt_hash.slice(0, 12)}…` : "—";

  let actionHint = "";
  if (lastRun.receipt_hash) {
    try {
      const res = await fetch("/api/receipts?limit=200");
      if (res.ok) {
        const receipts = await res.json();
        const match = receipts.find(r => r.receipt_hash === lastRun.receipt_hash);
        if (match) actionHint = match.action;
      }
    } catch (_) { /* best-effort — hash-only fallback below is fine */ }
  }

  return `
    <div class="stat-row" style="grid-template-columns:repeat(3,1fr)">
      <div class="stat-card"><div class="sl">Last run</div><div class="sv${inv.converged ? " g" : ""}">${inv.converged ? "converged" : "not converged"}</div><div class="sm">${inv.total_cycles ?? "—"} cycle${inv.total_cycles === 1 ? "" : "s"} · ${esc(lastRunTs)}</div></div>
      <div class="stat-card"><div class="sl">Claims</div><div class="sv">${claims.length}</div><div class="sm">${esc(breakdownStr)}</div></div>
      <div class="stat-card"><div class="sl">From receipt</div><div class="sv" style="font-size:14px;color:var(--cyan)">${esc(receiptShort)}</div><div class="sm">${esc(actionHint)}</div></div>
    </div>
    ${inv.error ? `<div class="banner err"><span class="bi">⚠</span> ${esc(inv.error)}</div>` : ""}
    <table class="tbl" id="inv-claims-table">
      <thead><tr><th>Claim</th><th style="width:120px">Status</th></tr></thead>
      <tbody>${renderInvariantClaimRows(claims)}</tbody>
    </table>`;
}

function renderInvariantClaimRows(claims) {
  if (!claims.length) return `<tr><td colspan="2" class="receipts-empty">No claims recorded for this run.</td></tr>`;
  return claims.map((c, i) => `
    <tr data-index="${i}">
      <td class="canon">${esc(c.name || "—")}</td>
      <td class="${_claimStatusCls(c.status)}">${_claimStatusGlyph(c.status)} ${esc(c.status || "unknown")}</td>
    </tr>`).join("");
}

function wireInvariantClaimRowClicks() {
  document.querySelectorAll("#inv-claims-table tbody tr[data-index]").forEach(row => {
    row.addEventListener("click", () => selectInvariantClaim(parseInt(row.dataset.index, 10)));
  });
}

function selectInvariantClaim(index) {
  const claims = invariantLastRunCache?.invariant?.claims || [];
  const c = claims[index];
  if (!c) return;
  selectedInvariantClaim = index;
  selectedAgreementRule   = null;
  selectedRevocationRule  = null;
  selectedReceiptHash     = null;
  selectedVaultKey        = null;
  selectedName            = null;
  document.querySelectorAll(".tbl tbody tr.sel").forEach(row => row.classList.remove("sel"));
  document.querySelector(`#inv-claims-table tbody tr[data-index="${index}"]`)?.classList.add("sel");
  $("detailInner").innerHTML = renderInvariantClaimDetail(c);
  $("detailPanel").classList.add("open");
}

// "Environment snapshot" (per the reference's DETAILS.invariant) is NOT
// rendered here — confirmed against invariant_check.py/liminate_invariant's
// ClaimOutcome dataclass that no raw snapshot is retained per-claim in the
// receipt's invariant block (the snapshot is only ever an internal input to
// the verification agent, never part of its output). Likewise per-claim
// history via receipts is omitted rather than attempted: there's no stable
// way to join a claim identity across receipts beyond matching claim-name
// text, and this machine has zero receipts carrying an invariant block to
// validate that join against — the brief explicitly allows omission here
// ("otherwise omit") over shipping an unverified best-effort feature.
function renderInvariantClaimDetail(c) {
  const lastRun      = invariantLastRunCache;
  const receiptShort = lastRun?.receipt_hash ? `${lastRun.receipt_hash.slice(0, 12)}…` : "—";
  const totalCycles  = lastRun?.invariant?.total_cycles;
  const cycleStr     = (c.cycles != null && totalCycles != null) ? `${c.cycles} of ${totalCycles}` : (c.cycles ?? "—");

  const escalationBlock = c.escalation_reason
    ? `<div class="d-row"><span class="k">escalation reason</span><span class="v" style="color:var(--red)">${esc(c.escalation_reason)}</span></div>` : "";
  const handlerBlock = c.handler
    ? `<div class="d-row"><span class="k">handler</span><span class="v">${esc(c.handler)}</span></div>` : "";
  const correctionsBlock = (c.corrections && c.corrections.length)
    ? `<div class="d-h">Corrections</div>${c.corrections.map(x => `<div class="d-row"><span class="k">—</span><span class="v">${esc(x)}</span></div>`).join("")}`
    : "";

  return `
    <div class="detail-close-row"><button class="icon-btn" onclick="closeDetail()">✕</button></div>
    <div class="d-title">${esc(c.name || "Claim")}</div>
    <div class="d-meta"><span class="${_claimStatusCls(c.status)}">${_claimStatusGlyph(c.status)} ${esc(c.status || "unknown")}</span></div>
    <div class="d-h">Checked against</div>
    <div class="d-row"><span class="k">receipt</span><span class="v" style="color:var(--cyan)">${esc(receiptShort)}</span></div>
    <div class="d-row"><span class="k">cycle</span><span class="v">${esc(String(cycleStr))}</span></div>
    ${escalationBlock}
    ${handlerBlock}
    ${correctionsBlock}`;
}

// ── Revocations domain ─────────────────────────────────────────────────────

async function renderRevocationsView() {
  $("revocationsContent").innerHTML = `<div class="empty-state"><div class="empty-state-title">Loading revocations…</div></div>`;
  try {
    const res  = await fetch("/api/revocations");
    const data = await res.json();
    revocationsCache = data;

    if (!data.exists) {
      $("revocationsContent").innerHTML = renderRevocationsEmptyState();
      renderSidebar();
      return;
    }

    const rules = data.rules || [];
    const sync  = data.sync || {};
    const freshness = _revocationsSyncFreshness(sync.last_checked);
    const headShort = sync.head_hash ? `${sync.head_hash.slice(0, 4)}…${sync.head_hash.slice(-4)}` : "—";

    // Best-effort denial-count join against /api/receipts (see
    // _computeDenialCounts). Never fabricated — "—" when the join can't be
    // performed (fetch failure) rather than 0 (0 is a real, meaningful count
    // when the join succeeds and finds nothing).
    let denialCounts = null;
    try {
      denialCounts = await _computeDenialCounts(rules);
    } catch (_) { denialCounts = null; }
    revocationDenialCountsCache = denialCounts || {};

    $("revocationsContent").innerHTML = `
      <div class="view-title"><h1>Revocations</h1><span class="vsub">platform overlay · forbid-only</span></div>
      <div class="banner ${freshness.cls}"><span class="bi" style="color:var(--${freshness.dotColor})">${freshness.glyph}</span> ${esc(freshness.label)} <span class="bspacer"></span> <span class="num">head ${esc(headShort)}</span></div>
      <table class="tbl" id="rev-anchor-active">
        <thead><tr><th style="width:40px">#</th><th style="width:70px">Verb</th><th>Condition</th><th style="width:110px">Window</th><th style="width:90px">Denials</th></tr></thead>
        <tbody>${renderRevocationRows(rules, revocationDenialCountsCache)}</tbody>
      </table>`;

    wireRevocationRowClicks();
    renderSidebar();
  } catch (e) {
    $("revocationsContent").innerHTML = `<div class="empty-state"><div class="empty-state-title">Could not load revocations</div><div class="empty-state-sub">${esc(e.message)}</div></div>`;
  }
}

function renderRevocationsEmptyState() {
  return `
    <div class="view-title"><h1>Revocations</h1></div>
    <div class="empty-wrap"><div class="empty-card">
      <div class="empty-glyph">⊘</div>
      <div class="empty-title">No platform revocations</div>
      <div class="empty-desc">Revocations are forbid-only kill orders authored on the platform and synced down. They subtract authority the Agreement grants, and always win.</div>
      <div class="init-cmd"><div class="cl">sync revocations from the platform</div><div class="cc"><span class="p">$</span> seshat revocations sync</div></div>
      <div class="platform-hint">revocations are authored on <span class="lnk">liminate.dev</span></div>
    </div></div>`;
}

// Sync-freshness thresholds (client-computed per the build plan — the
// backend deliberately only exposes raw last_checked): fresh = synced within
// the last hour (green/.banner.ok), stale = synced longer ago than that
// (amber/.banner.warn), never = last_checked is null (red/.banner.err).
// There's no single "correct" threshold specified anywhere in the brief;
// documented here and in the task report as a judgment call.
function _revocationsSyncFreshness(lastChecked) {
  if (!lastChecked) {
    return { cls: "err", glyph: "○", dotColor: "red", label: "Never synced" };
  }
  const then = new Date(lastChecked).getTime();
  if (Number.isNaN(then)) {
    return { cls: "err", glyph: "○", dotColor: "red", label: "Never synced" };
  }
  const ageMs = Date.now() - then;
  const rel = _relativeTime(lastChecked) || "recently";
  if (ageMs <= 60 * 60 * 1000) {
    return { cls: "ok", glyph: "●", dotColor: "green", label: `Synced ${rel}` };
  }
  return { cls: "warn", glyph: "●", dotColor: "orange", label: `Synced ${rel} (stale)` };
}

// Best-effort denial count per rule: joins on exact string equality between
// a rule's canonical form and a denied receipt's result.rule. This is a real
// field, not a guess — mcp_server.py's _enforce() writes result.rule =
// decision.rule (the exact canonical string of the deciding Agreement rule)
// on every denial receipt, confirmed by reading mcp_server.py directly. When
// the fetch itself fails, the caller shows "—"; when it succeeds, a rule
// with zero matches legitimately shows 0, not "—".
async function _computeDenialCounts(rules) {
  const counts = {};
  if (!rules.length) return counts;
  const res = await fetch("/api/receipts?limit=200");
  if (!res.ok) throw new Error("receipts fetch failed");
  const receipts = await res.json();
  const denied = receipts.filter(r => r.result && r.result.status === "denied" && r.result.rule);
  rules.forEach((rule, i) => {
    if (rule.error) return;
    counts[i] = denied.filter(r => r.result.rule === rule.canonical).length;
  });
  return counts;
}

function renderRevocationRows(rules, denialCounts) {
  if (!rules.length) return `<tr><td colspan="5" class="receipts-empty">No revocation rules parsed.</td></tr>`;
  return rules.map((r, i) => {
    const rowNum = `R${i + 1}`;
    if (r.error) {
      return `<tr><td colspan="5" style="color:var(--red)">${esc(rowNum)}: ${esc(r.error)}</td></tr>`;
    }
    const win   = r.window || "unbounded";
    const count = denialCounts && denialCounts[i] !== undefined ? denialCounts[i] : null;
    return `
      <tr data-index="${i}">
        <td class="num">${esc(rowNum)}</td>
        <td>${r.verb ? `<span class="${_agrVerbCls(r.verb)}">${esc(r.verb)}</span>` : "—"}</td>
        <td><span class="canon">${esc(r.canonical)}</span></td>
        <td><span class="win ${esc(win)}">${esc(win)}</span></td>
        <td class="num">${count === null ? "—" : count}</td>
      </tr>`;
  }).join("");
}

function wireRevocationRowClicks() {
  document.querySelectorAll("#rev-anchor-active tbody tr[data-index]").forEach(row => {
    row.addEventListener("click", () => selectRevocationRule(parseInt(row.dataset.index, 10)));
  });
}

function selectRevocationRule(index) {
  const rule = (revocationsCache?.rules || [])[index];
  if (!rule || rule.error) return;
  selectedRevocationRule = index;
  selectedAgreementRule   = null;
  selectedInvariantClaim  = null;
  selectedReceiptHash     = null;
  selectedVaultKey        = null;
  selectedName            = null;
  document.querySelectorAll(".tbl tbody tr.sel").forEach(row => row.classList.remove("sel"));
  document.querySelector(`#rev-anchor-active tbody tr[data-index="${index}"]`)?.classList.add("sel");
  $("detailInner").innerHTML = renderRevocationDetail(rule, index);
  $("detailPanel").classList.add("open");
}

function renderRevocationDetail(rule, index) {
  const sync         = revocationsCache?.sync || {};
  const headShort    = sync.head_hash ? `${sync.head_hash.slice(0, 12)}…` : "—";
  const lastChecked  = sync.last_checked ? (_relativeTime(sync.last_checked) || "recently") : "never synced";
  const count        = revocationDenialCountsCache[index];
  const win          = rule.window || "unbounded";
  return `
    <div class="detail-close-row"><button class="icon-btn" onclick="closeDetail()">✕</button></div>
    <div class="d-title">Revocation R${index + 1}</div>
    <div class="d-meta">${esc(rule.verb || "forbid")} · ${esc(win)} · from platform</div>
    <div class="d-h">Canonical form</div>
    <div class="d-canon">${esc(rule.canonical)}</div>
    <div class="d-h">Sync metadata</div>
    <div class="d-row"><span class="k">head hash</span><span class="v" style="color:var(--cyan)">${esc(headShort)}</span></div>
    <div class="d-row"><span class="k">last checked</span><span class="v">${esc(lastChecked)}</span></div>
    <div class="d-h">Enforcement</div>
    <div class="d-row"><span class="k">denials (recent)</span><span class="v" style="color:var(--red)">${count === undefined || count === null ? "—" : count}</span></div>`;
}

// ── Router setup ──────────────────────────────────────────────────────────

async function loadSetupStatus() {
  try {
    const res  = await fetch("/api/router/status");
    routerStatus = await res.json();
    updateRouterBanner();
  } catch (_) { /* server may be restarting */ }
}

function routerReady() {
  return routerStatus &&
    routerStatus.caddy_running &&
    routerStatus.dnsmasq_running &&
    routerStatus.resolver_configured;
}

function updateRouterBanner() {
  const banner = $("routerBanner");
  if (!routerStatus) return;
  const fullySetup = routerStatus.caddy_installed && routerStatus.dnsmasq_installed &&
                     routerStatus.caddy_running    && routerStatus.dnsmasq_running &&
                     routerStatus.resolver_configured && routerStatus.caddy_ca_trusted;
  if (fullySetup) {
    banner.style.display = "none";
    return;
  }
  banner.style.display = "flex";
  const installed  = routerStatus.caddy_installed && routerStatus.dnsmasq_installed;
  const configured = routerStatus.resolver_configured;
  $("routerRestartBtn").style.display = (installed && configured) ? "" : "none";
}

function openSetupModal() {
  $("routerModalOverlay").style.display = "flex";
  runSetupWizard();
}

function closeSetupModal() {
  $("routerModalOverlay").style.display = "none";
  if (_resolverPollTimer) { clearInterval(_resolverPollTimer); _resolverPollTimer = null; }
}

let _resolverPollTimer = null;

async function runSetupWizard() {
  if (!routerStatus) await loadSetupStatus();
  // Fetch vault status for the encryption step
  let vaultEncrypted = false;
  try { const r = await fetch("/api/vault"); vaultEncrypted = (await r.json()).encrypted; } catch (_) {}
  updateStepStatus("vault-enc",    vaultEncrypted);
  updateStepStatus("caddy",        routerStatus.caddy_installed);
  updateStepStatus("dnsmasq",      routerStatus.dnsmasq_installed);
  updateStepStatus("dnsmasq-cfg",  routerStatus.dnsmasq_running);
  updateStepStatus("resolver",     routerStatus.resolver_configured);
  updateStepStatus("caddy-trust",  routerStatus.caddy_ca_trusted);
  if (!vaultEncrypted)                     { await runVaultEncSetup();  return; }
  if (!routerStatus.caddy_installed || !routerStatus.dnsmasq_installed) return;
  if (!routerStatus.dnsmasq_running)       { await runDnsmasqConfig();  return; }
  if (!routerStatus.resolver_configured)   { await runResolverConfig(); return; }
  if (!routerStatus.caddy_ca_trusted)      { await runCaddyTrust();     return; }
  $("routerModalDoneBtn").style.display = "";
}

async function runVaultEncSetup() {
  updateStepStatus("vault-enc", false, true);
  $("step-vault-enc-body").innerHTML = `<span style="color:var(--text-muted);font-size:13px">Installing keyring + cryptography…</span>`;
  try {
    const res = await fetch("/api/vault/install-deps", { method: "POST" });
    if (!res.ok) {
      const text = await res.text();
      throw new Error(res.status + " — " + text.slice(0, 120));
    }
    const data = await res.json();
    if (!data.ok) {
      $("step-vault-enc-body").innerHTML =
        `<span style="color:var(--red);font-size:13px">Install failed: ${esc(data.error || "unknown error")}</span>
         <button class="btn btn-ghost btn-sm" style="margin-top:8px" onclick="runVaultEncSetup()">Retry</button>`;
      updateStepStatus("vault-enc", false);
      return;
    }
    updateStepStatus("vault-enc", true);
    $("step-vault-enc-body").innerHTML =
      `<span style="color:var(--text-muted);font-size:13px">Restart Seshat to activate encryption, then re-open Set Up.</span>`;
  } catch (e) {
    $("step-vault-enc-body").innerHTML =
      `<span style="color:var(--red);font-size:13px">Error: ${esc(e.message)}</span>
       <button class="btn btn-ghost btn-sm" style="margin-top:8px" onclick="runVaultEncSetup()">Retry</button>`;
    updateStepStatus("vault-enc", false);
  }
}

async function runResolverConfig() {
  updateStepStatus("resolver", false, true);
  try {
    const res  = await fetch("/api/router/setup/resolver", { method: "POST" });
    const data = await res.json();
    updateStepStatus("resolver", data.ok);
    if (data.ok) await runCaddyTrust();
    else $("routerModalError").textContent = data.error || "Resolver configuration failed";
  } catch (e) {
    updateStepStatus("resolver", false);
    $("routerModalError").textContent = e.message;
  }
}

async function runCaddyTrust() {
  updateStepStatus("caddy-trust", false, true);
  try {
    const res  = await fetch("/api/router/setup/caddy-trust", { method: "POST" });
    const data = await res.json();
    updateStepStatus("caddy-trust", data.ok);
    if (data.ok) $("routerModalDoneBtn").style.display = "";
    else $("routerModalError").textContent = data.error || "Failed to trust Caddy CA";
  } catch (e) {
    updateStepStatus("caddy-trust", false);
    $("routerModalError").textContent = e.message;
  }
}

function updateStepStatus(stepId, ok, running = false) {
  const el   = $(`step-${stepId}-status`);
  const body = $(`step-${stepId}-body`);
  if (running) {
    el.textContent = "·";
    el.style.color = "var(--text-muted)";
    if (body) body.style.display = "none";
    return;
  }
  el.textContent = ok ? "✓" : "✗";
  el.style.color = ok ? "var(--green)" : "var(--red)";
  if (body) body.style.display = ok ? "none" : "";
}

async function checkCaddyInstalled() {
  await loadSetupStatus();
  updateStepStatus("caddy", routerStatus.caddy_installed);
  if (routerStatus.caddy_installed && routerStatus.dnsmasq_installed) runDnsmasqConfig();
}

async function checkDnsmasqInstalled() {
  await loadSetupStatus();
  updateStepStatus("dnsmasq", routerStatus.dnsmasq_installed);
  if (routerStatus.caddy_installed && routerStatus.dnsmasq_installed) runDnsmasqConfig();
}

async function runDnsmasqConfig() {
  updateStepStatus("dnsmasq-cfg", false, true);
  try {
    const res  = await fetch("/api/router/setup/dnsmasq", { method: "POST" });
    const data = await res.json();
    updateStepStatus("dnsmasq-cfg", data.ok);
    if (data.ok) await runResolverConfig();
    else $("routerModalError").textContent = data.error || "dnsmasq configuration failed";
  } catch (e) {
    updateStepStatus("dnsmasq-cfg", false);
    $("routerModalError").textContent = e.message;
  }
}

async function finishSetup() {
  const res  = await fetch("/api/router/setup/caddy-start", { method: "POST" });
  const data = await res.json();
  if (!data.ok) {
    $("routerModalError").textContent = data.error || "Failed to start Caddy";
    return;
  }
  closeSetupModal();
  await loadSetupStatus();
  await loadHostnames();
  renderShelf();
}

async function restartRouterServices() {
  await fetch("/api/router/setup/caddy-start", { method: "POST" });
  await fetch("/api/router/setup/dnsmasq",     { method: "POST" });
  await loadSetupStatus();
  renderShelf();
}

function editHostname(projectName) {
  const field = $(`hostname-field-${projectName}`);
  if (!field) return;
  const h       = hostnames.find(x => x.project_name === projectName);
  const current = h ? h.hostname : _slugify(projectName);
  const safeN   = projectName.replace(/\\/g, "\\\\").replace(/'/g, "\\'");
  field.querySelector(".hostname-field-view").outerHTML = `
    <div class="hostname-field-edit">
      <input class="hostname-edit-input" id="hostname-input-${esc(projectName)}"
             value="${esc(current)}" spellcheck="false">
      <button class="btn btn-primary btn-sm" onclick="saveHostname('${safeN}')">Save</button>
      <button class="btn btn-ghost  btn-sm" onclick="resetHostname('${safeN}')">Reset to default</button>
    </div>`;
}

async function saveHostname(projectName) {
  const input = $(`hostname-input-${projectName}`);
  if (!input) return;
  const hostname = input.value.trim();
  const res  = await fetch(`/api/router/hostnames/${encodeURIComponent(projectName)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ hostname }),
  });
  const data = await res.json();
  if (data.error) { toast(data.error, "error"); return; }
  await loadHostnames();
  renderShelf();
  updateDetailPanel(projectName);
}

async function resetHostname(projectName) {
  const res  = await fetch(`/api/router/hostnames/${encodeURIComponent(projectName)}`, {
    method: "DELETE",
  });
  const data = await res.json();
  if (data.error) { toast(data.error, "error"); return; }
  await loadHostnames();
  renderShelf();
  updateDetailPanel(projectName);
}

async function useHostnameForVaultKey(key, hostnameUrl, proj) {
  const url  = proj
    ? `/api/vault/overrides/${encodeURIComponent(proj)}`
    : "/api/vault/keys";
  const res  = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ key, value: hostnameUrl }),
  });
  const data = await res.json();
  if (data.error) { toast(data.error, "error"); return; }
  toast("Updated to hostname URL", "success");
  await renderVaultView();
}

async function loadHostnames() {
  try {
    const res = await fetch("/api/router/hostnames");
    hostnames = await res.json();
  } catch (_) { /* server may be restarting */ }
}

// ── Render (projects view) ─────────────────────────────────────────────────

function render() {
  renderCounts();
  renderShelf();
  renderOrphans();
  renderGroups();
}

function renderCounts() {
  // #count-* elements only exist in the DOM while the Projects sub-nav is
  // rendered (see renderProjectsSubnav()); no-op on other domains, same as
  // renderGroups()'s `if (!list) return;` guard just above.
  if (!$("count-all")) return;
  const running  = projects.filter(p => p.status === "running").length;
  const stopped  = projects.filter(p => p.status === "stopped").length;
  const conflict = projects.filter(p => p.status === "conflict").length;
  $("count-all").textContent      = projects.length;
  $("count-running").textContent  = running;
  $("count-stopped").textContent  = stopped;
  $("count-conflict").textContent = conflict;
  $("count-orphans").textContent  = orphans.length;
}

function renderShelf() {
  const shelf = $("projectShelf");
  if (projects.length === 0) {
    shelf.innerHTML = `
      <div class="empty-state">
        <div class="empty-state-title">No projects registered yet</div>
        <div class="empty-state-sub">Click "Register Project" to add your first project.</div>
      </div>`;
    return;
  }
  let visible = (activeFilter === "all" || activeFilter === "orphans")
    ? projects
    : projects.filter(p => p.status === activeFilter);

  visible = visible.filter(p => _matchesSearch(p, uiState.searchQuery));
  visible = _sortProjects(visible);

  if (visible.length === 0) {
    const msg = uiState.searchQuery
      ? `No projects matching "${esc(uiState.searchQuery)}"`
      : `No ${({ running: "running", stopped: "stopped", conflict: "in conflict" }[activeFilter] ?? activeFilter)} projects`;
    shelf.innerHTML = `<div class="empty-state"><div class="empty-state-title">${msg}</div></div>`;
    return;
  }
  shelf.innerHTML = visible.map(projectRowHTML).join("");
  attachRowEvents(shelf);
  if (selectedName) {
    shelf.querySelector(`[data-name="${CSS.escape(selectedName)}"]`)?.classList.add("selected");
  }
}

// ── Status light helper ────────────────────────────────────────────────────

/**
 * Returns the CSS class for the status light dot.
 * Priority: conflict > error > degraded > running > stopped
 */
function getStatusLightClass(p) {
  if (p.status === "conflict") return "conflict";
  const isRunning = p.status === "running";
  if (isRunning && p.has_error && p.recent_error) return "error";
  if (p.composite_status === "degraded") return "degraded";
  return p.status;   // "running" | "stopped"
}

function _hostnameChipHTML(projectName) {
  const h = hostnames.find(x => x.project_name === projectName);
  if (!h) return "";
  const ready = routerReady();
  return `<div class="hostname-chip${ready ? "" : " muted"}"
               data-hostname="${esc(h.hostname)}">${esc(h.hostname)}</div>`;
}

function projectRowHTML(p) {
  const isRunning  = p.status === "running";
  const hasError   = isRunning && p.has_error && p.recent_error;
  const lightClass = getStatusLightClass(p);
  const tags = (p.tags || []).slice(0, 3).map(t => `<span class="tag">${esc(t)}</span>`).join("");
  const conflictLine = p.status === "conflict" && p.process_name
    ? `<div class="conflict-inline">⚠ Port in use by <code>${esc(p.process_name)}</code> (PID ${p.pid})</div>` : "";
  const errorLine = hasError && p.recent_error
    ? `<div class="error-preview">⚠ ${esc(p.recent_error.short || p.recent_error.message.slice(0, 60))}</div>` : "";
  const agentTag = (p.started_by && p.started_by.startsWith("mcp_session_"))
    ? `<span class="agent-tag" title="Started by AI agent">⚡ agent</span>` : "";
  const ssCls  = isRunning ? "stop-btn" : "start-btn";
  const ssIcon = isRunning ? "■" : "▶";
  return `
    <div class="project-row ${p.status}" data-name="${esc(p.name)}">
      <div><div class="status-light ${lightClass}"></div></div>
      <div>
        <div class="project-name">${esc(p.name)} ${agentTag}</div>
        ${tags ? `<div class="project-tags">${tags}</div>` : ""}
        ${conflictLine}${errorLine}
      </div>
      <div class="project-port">:${p.port}${(p.child_ports||[]).map(cp=>`<span class="child-port"> :${cp}</span>`).join("")}</div>
      <div>${_hostnameChipHTML(p.name)}</div>
      <div class="project-dir">${esc(shortPath(p.directory))}</div>
      <div class="project-actions">
        <button class="action-btn start-stop-btn ${ssCls}" title="${isRunning?"Stop":"Start"}">${ssIcon}</button>
        <button class="action-btn open-browser-btn" title="Open in Browser">↗</button>
        <button class="action-btn open-finder-btn"  title="Open in Finder">📁</button>
        <button class="action-btn open-term-btn"    title="Open in Terminal">⌘</button>
      </div>
    </div>`;
}

function attachRowEvents(shelf) {
  shelf.querySelectorAll(".project-row").forEach(row => {
    const name = row.dataset.name;
    const p    = projects.find(x => x.name === name);
    row.addEventListener("click", e => { if (e.target.closest(".action-btn")) return; selectProject(name); });
    row.querySelector(".start-stop-btn").addEventListener("click", e => {
      e.stopPropagation(); p.status === "running" ? stopProject(name) : startProject(name);
    });
    row.querySelector(".open-browser-btn").addEventListener("click", e => {
      e.stopPropagation(); window.open(p.url || `http://localhost:${p.port}`, "_blank");
    });
    row.querySelector(".hostname-chip:not(.muted)")?.addEventListener("click", e => {
      e.stopPropagation();
      window.open(`http://${e.currentTarget.dataset.hostname}`, "_blank");
    });
    row.querySelector(".open-finder-btn").addEventListener("click", e => {
      e.stopPropagation(); apiOpen(p.directory, "finder");
    });
    row.querySelector(".open-term-btn").addEventListener("click", e => {
      e.stopPropagation(); apiOpen(p.directory, "terminal");
    });
  });
}

function renderOrphans() {
  const section = $("orphanSection");
  const list    = $("orphanList");
  const show    = activeFilter === "all" || activeFilter === "orphans";
  if (!show || orphans.length === 0) { section.style.display = "none"; return; }
  section.style.display = "block";
  list.innerHTML = orphans.map(o => `
    <div class="orphan-row">
      <div class="orphan-port">:${o.port}</div>
      <div class="orphan-info">
        <div class="orphan-name">${esc(o.name)} <span class="orphan-pid">(PID ${o.pid})</span></div>
        <div class="orphan-cmd">${esc(o.cmdline || "—")}</div>
      </div>
      <div style="display:flex;gap:4px;flex-shrink:0">
        <button class="action-btn adopt-btn" data-port="${o.port}" data-name="${esc(o.name||"")}" data-cmdline="${esc(o.cmdline||"")}" title="Register this process">+</button>
        <button class="action-btn stop-btn" onclick="stopOrphan(${o.port})" title="Stop process">■</button>
      </div>
    </div>`).join("");
  // Wire adopt buttons via delegation (avoids inline JS string escaping issues)
  list.querySelectorAll(".adopt-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      adoptOrphan(parseInt(btn.dataset.port, 10), btn.dataset.name, btn.dataset.cmdline);
    });
  });
}

// Re-skinned into sidebar sub-nav `.sub-item` rows (was `.group-item` in the
// Task-2-fix stopgap bar) — same `groups` data and the same start/stop/delete
// action wiring, just a different wrapper markup/target element.
function renderGroups() {
  const list = $("groupList");
  if (!list) return;
  if (groups.length === 0) {
    list.innerHTML = `<div class="sub-item" style="cursor:default;color:var(--text-dim)"><span class="lbl">No groups yet</span></div>`;
    return;
  }
  list.innerHTML = groups.map(g => {
    const count = (g.projects || []).length;
    return `
      <div class="sub-item group-sub-item" title="${esc((g.projects||[]).join(', '))}">
        <span class="sdot b"></span> <span class="lbl">${esc(g.name)}</span> <span class="sc">${count}</span>
        <span class="group-actions">
          <button class="group-btn start"  data-action="start-group"  data-name="${esc(g.name)}" title="Start all">▶</button>
          <button class="group-btn stop"   data-action="stop-group"   data-name="${esc(g.name)}" title="Stop all">■</button>
          <button class="group-btn delete" data-action="delete-group" data-name="${esc(g.name)}" title="Remove group">✕</button>
        </span>
      </div>`;
  }).join("");
  // Wire group action buttons via delegation (avoids inline JS string escaping issues)
  list.querySelectorAll(".group-btn").forEach(btn => {
    btn.addEventListener("click", e => {
      e.stopPropagation();
      const name = btn.dataset.name;
      if (btn.dataset.action === "start-group")  startGroup(name);
      else if (btn.dataset.action === "stop-group")  stopGroup(name);
      else if (btn.dataset.action === "delete-group") deleteGroup(name);
    });
  });
}

// ── Detail panel ───────────────────────────────────────────────────────────

async function selectProject(name) {
  if (uiState.editingConfig && _isConfigDirty()) {
    const yes = await confirmAction({
      title: "Discard changes?",
      message: "You have unsaved configuration edits.",
      confirmText: "Discard",
      danger: false,
    });
    if (!yes) return;
    uiState.editingConfig = null;
    uiState.dirtyFields = {};
  }
  selectedName = name;
  document.querySelectorAll(".project-row").forEach(r =>
    r.classList.toggle("selected", r.dataset.name === name)
  );
  updateDetailPanel(name);
  $("detailPanel").classList.add("open");
  loadLogs(name);
  loadEnvStatus(name);
}

function _slugify(name) {
  return name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "") + ".seshat";
}

function _hostnameDetailFieldHTML(projectName) {
  const h = hostnames.find(x => x.project_name === projectName);
  const current = h ? h.hostname : _slugify(projectName);
  const safeN = projectName.replace(/\\/g, "\\\\").replace(/'/g, "\\'");
  return `
    <div class="detail-field hostname-detail-field" id="hostname-field-${esc(projectName)}">
      <div class="detail-label">Local Address</div>
      <div class="hostname-field-view">
        <span class="hostname-field-value">${esc(current)}</span>
        <button class="detail-section-action" onclick="editHostname('${safeN}')">Edit</button>
      </div>
    </div>`;
}

function updateDetailPanel(name) {
  const p = projects.find(x => x.name === name);
  if (!p) return;
  // Preserve log content across re-renders so the 5s refresh doesn't wipe it
  const prevLog = $("logViewer") ? $("logViewer").innerHTML : null;
  const isRunning  = p.status === "running";
  const isConflict = p.status === "conflict";
  const hasError   = isRunning && p.has_error && p.recent_error;
  const isDegraded = p.composite_status === "degraded";

  // Status badge: conflict → error → degraded → base status
  let statusCls, statusTxt;
  if (isConflict)      { statusCls = "conflict"; statusTxt = "⚠ Conflict"; }
  else if (hasError)   { statusCls = "error";    statusTxt = "⚠ Running with errors"; }
  else if (isDegraded) { statusCls = "degraded"; statusTxt = "◑ Degraded — dep down"; }
  else                 { statusCls = p.status;   statusTxt = statusLabel(p.status); }

  // Attribution badge
  let attrBadge = "";
  if (p.started_by) {
    if (p.started_by === "dashboard") {
      attrBadge = `<div class="attr-badge attr-dashboard">Started from dashboard</div>`;
    } else if (p.started_by === "cli") {
      attrBadge = `<div class="attr-badge attr-cli">Started from CLI</div>`;
    } else if (p.started_by.startsWith("mcp_session_")) {
      const sessionId = p.started_by.replace("mcp_session_", "");
      attrBadge = `<div class="attr-badge attr-agent">Agent session <code>${esc(sessionId)}</code></div>`;
    } else {
      attrBadge = `<div class="attr-badge attr-unknown">Started by: ${esc(p.started_by)}</div>`;
    }
  }

  const conflictBlock = isConflict ? `
    <div class="conflict-message">
      ⚠ Port ${p.port} is in use by <code>${esc(p.process_name||"unknown")}</code>
      (PID ${p.pid||"?"})${p.process_cmd ? ` — ${esc(p.process_cmd.slice(0,80))}` : ""}.
      <div style="margin-top:8px">
        <button class="detail-btn danger" onclick="stopOrphan(${p.port})">Kill Process on :${p.port}</button>
      </div>
    </div>` : "";
  const errorBlock = hasError ? renderErrorBlock(p.recent_error) : "";
  const pidBlock = (p.pid&&isRunning) ? `
    <div class="detail-field"><div class="detail-label">PID</div>
    <div class="detail-value mono">${p.pid}</div></div>` : "";

  const depsBlock = renderDependencies(p.dependencies || [], p.dep_status || [], p.name);

  const safeN = p.name.replace(/\\/g,"\\\\").replace(/'/g,"\\'");
  const safeD = (p.directory||"").replace(/\\/g,"\\\\").replace(/'/g,"\\'");
  $("detailInner").innerHTML = `
    <div class="detail-close-row"><button class="icon-btn" onclick="closeDetail()">✕</button></div>
    <div class="detail-name">${esc(p.name)}</div>
    <div class="detail-url">localhost:${p.port}</div>
    <div class="detail-status ${statusCls}">${statusTxt}</div>
    ${attrBadge}
    ${_hostnameDetailFieldHTML(p.name)}
    ${conflictBlock}${errorBlock}
    <div class="detail-actions">
      ${isRunning
        ? `<button class="detail-btn stop"  onclick="stopProject('${safeN}')">■ Stop</button>`
        : `<button class="detail-btn start" onclick="startProject('${safeN}')">▶ Start</button>`}
      <button class="detail-btn" id="openBrowserBtn" data-url="${esc(p.url||`http://localhost:${p.port}`)}">↗ Open in Browser</button>
      <button class="detail-btn" onclick="apiOpen('${safeD}','finder')">📁 Open in Finder</button>
      <button class="detail-btn" onclick="apiOpen('${safeD}','terminal')">⌘ Open in Terminal</button>
    </div>
    <div class="detail-section">
      <div class="detail-section-header">
        <div class="detail-section-title" style="margin:0;border:none;padding:0">Configuration</div>
        <button class="detail-section-action" id="configEditBtn" onclick="toggleConfigEdit('${safeN}')">Edit</button>
      </div>
      <div class="detail-field"><div class="detail-label">Directory</div>
        <div class="detail-value mono" id="cfg-directory">${esc(p.directory)}</div></div>
      <div class="detail-field"><div class="detail-label">Start Command</div>
        <div class="detail-value mono" id="cfg-start">${esc(p.start)}</div></div>
      <div class="detail-field"><div class="detail-label">Port</div>
        <div class="detail-value mono" id="cfg-port">${p.port}</div></div>
      ${(p.child_ports||[]).length ? `<div class="detail-field"><div class="detail-label">Child Ports</div>
        <div class="detail-value mono">${(p.child_ports||[]).map(cp=>`:${cp}`).join(", ")}</div></div>` : ""}
      <div class="detail-field"><div class="detail-label">Scheme</div>
        <div class="detail-value mono" id="cfg-scheme">${esc(p.scheme || "http")}${(p.scheme === "https") ? ' <span style="font-size:10px;color:var(--text-muted)">(HTTPS upstream)</span>' : ''}</div></div>
      <div class="detail-field"><div class="detail-label">Stop Command</div>
        <div class="detail-value mono" id="cfg-stop">${esc(p.stop || "")}</div></div>
      <div class="detail-field"><div class="detail-label">URL</div>
        <div class="detail-value mono" id="cfg-url">${esc(p.url || "")}</div></div>
      <div class="detail-field"><div class="detail-label">Tags</div>
        <div class="detail-value" id="cfg-tags">${(p.tags||[]).map(t=>`<span class="tag">${esc(t)}</span>`).join(" ") || '<span style="color:var(--text-muted)">—</span>'}</div></div>
      <div class="detail-field"><div class="detail-label">Notes</div>
        <div class="detail-value" id="cfg-notes">${esc(p.notes || "") || '<span style="color:var(--text-muted)">—</span>'}</div></div>
      ${pidBlock}
    </div>
    ${depsBlock}
    ${(p.env&&p.env.length) ? `
    <div class="detail-section">
      <div class="detail-section-header">
        <div class="detail-section-title" style="margin:0;border:none;padding:0">Environment</div>
        <button class="detail-section-action" onclick="showVaultView()">Manage in Vault →</button>
      </div>
      <div class="env-list" id="envList"><div style="color:var(--text-muted);font-size:12px">Loading…</div></div>
    </div>` : ""}
    <div class="detail-section" id="logSection">
      <div class="detail-section-header">
        <div class="detail-section-title" style="margin:0;border:none;padding:0">Output Log</div>
        <button class="detail-section-action" onclick="loadLogs('${safeN}')">↺ Refresh</button>
      </div>
      <div class="log-viewer" id="logViewer"><div class="log-empty">Loading…</div></div>
    </div>
    ${renderSourceSection(p, safeN)}
    <div class="detail-section">
      <div class="detail-section-title">Danger Zone</div>
      <button class="detail-btn danger" onclick="removeProject('${safeN}')">Remove from Registry</button>
    </div>`;
  $("openBrowserBtn").addEventListener("click", () => window.open($("openBrowserBtn").dataset.url, "_blank"));
  if (prevLog && prevLog !== '<div class="log-empty">Loading\u2026</div>') $("logViewer").innerHTML = prevLog;
}

function toggleConfigEdit(projectName) {
  const p = projects.find(x => x.name === projectName);
  if (!p) return;
  const btn = $("configEditBtn");

  if (btn.textContent === "Save") {
    saveConfig(projectName);
    return;
  }

  uiState.editingConfig = projectName;
  uiState.dirtyFields = {
    directory: p.directory,
    start: p.start,
    port: p.port,
    scheme: p.scheme || "http",
    stop: p.stop || "",
    url: p.url || "",
    tags: (p.tags || []).join(", "),
    notes: p.notes || "",
  };

  btn.textContent = "Save";

  $("cfg-directory").innerHTML = `<input class="config-edit-input" id="cfg-input-directory" value="${esc(p.directory)}" spellcheck="false">`;
  $("cfg-start").innerHTML     = `<input class="config-edit-input" id="cfg-input-start" value="${esc(p.start)}" spellcheck="false">`;
  $("cfg-port").innerHTML      = `<input class="config-edit-input config-edit-port" id="cfg-input-port" type="number" value="${p.port}" min="1" max="65535">`;
  const curScheme = p.scheme || "http";
  $("cfg-scheme").innerHTML    = `<select class="config-edit-input" id="cfg-input-scheme" style="width:auto"><option value="http"${curScheme==="http"?" selected":""}>http</option><option value="https"${curScheme==="https"?" selected":""}>https</option></select>`;
  $("cfg-stop").innerHTML      = `<input class="config-edit-input" id="cfg-input-stop" value="${esc(p.stop || "")}" spellcheck="false" placeholder="Optional stop command">`;
  $("cfg-url").innerHTML       = `<input class="config-edit-input" id="cfg-input-url" value="${esc(p.url || "")}" spellcheck="false" placeholder="http://localhost:${p.port}">`;
  $("cfg-tags").innerHTML      = `<input class="config-edit-input" id="cfg-input-tags" value="${esc((p.tags||[]).join(", "))}" spellcheck="false" placeholder="Comma-separated tags">`;
  $("cfg-notes").innerHTML     = `<textarea class="config-edit-input config-edit-notes" id="cfg-input-notes" rows="2" spellcheck="false" placeholder="Optional notes">${esc(p.notes || "")}</textarea>`;

  // Add Cancel button after Save if not already present
  if (!$("configCancelBtn")) {
    btn.insertAdjacentHTML("afterend",
      ` <button class="detail-section-action" id="configCancelBtn" onclick="cancelConfigEdit('${projectName.replace(/\\/g,"\\\\").replace(/'/g,"\\'")}')">Cancel</button>`);
  }

  // Toggle dirty indicator on Save button when inputs change
  const cfgInputs = [
    $("cfg-input-directory"), $("cfg-input-start"), $("cfg-input-port"), $("cfg-input-scheme"),
    $("cfg-input-stop"), $("cfg-input-url"), $("cfg-input-tags"), $("cfg-input-notes"),
  ];
  cfgInputs.forEach(el => {
    if (el) el.addEventListener("input", () => {
      const dirty = _isConfigDirty();
      $("configEditBtn")?.classList.toggle("dirty", dirty);
    });
  });
}

async function saveConfig(projectName) {
  const p = projects.find(x => x.name === projectName);
  if (!p) return;

  const updates = {};
  const fields = {
    directory: $("cfg-input-directory")?.value.trim(),
    start:     $("cfg-input-start")?.value.trim(),
    port:      parseInt($("cfg-input-port")?.value, 10) || p.port,
    scheme:    $("cfg-input-scheme")?.value || "http",
    stop:      $("cfg-input-stop")?.value.trim() || "",
    url:       $("cfg-input-url")?.value.trim() || "",
    tags:      ($("cfg-input-tags")?.value || "").split(",").map(t => t.trim()).filter(Boolean),
    notes:     $("cfg-input-notes")?.value.trim() || "",
  };

  const origScheme = p.scheme || "http";
  const schemeChanged = fields.scheme !== origScheme;
  const defaultUrl = (s, port) => `${s}://localhost:${port}`;
  // If URL still matches the default derived from the old scheme, auto-update to new scheme
  if (schemeChanged && (fields.url === "" || fields.url === defaultUrl(origScheme, p.port))) {
    fields.url = defaultUrl(fields.scheme, fields.port);
  }

  if (fields.directory !== p.directory)              updates.directory = fields.directory;
  if (fields.start     !== p.start)                  updates.start     = fields.start;
  if (fields.port      !== p.port)                   updates.port      = fields.port;
  if (schemeChanged)                                 updates.scheme    = fields.scheme;
  if (fields.stop      !== (p.stop || ""))           updates.stop      = fields.stop;
  if (fields.url       !== (p.url || ""))            updates.url       = fields.url;
  if (JSON.stringify(fields.tags) !== JSON.stringify(p.tags || [])) updates.tags = fields.tags;
  if (fields.notes     !== (p.notes || ""))          updates.notes     = fields.notes;

  if (Object.keys(updates).length === 0) {
    cancelConfigEdit(projectName);
    return;
  }

  const res = await fetch(`/api/projects/${encodeURIComponent(projectName)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(updates),
  });
  const data = await res.json();
  if (data.error) { toast(data.error, "error"); return; }

  uiState.editingConfig = null;
  uiState.dirtyFields = {};
  toast("Configuration saved", "success");
  await refresh();
}

function cancelConfigEdit(projectName) {
  uiState.editingConfig = null;
  uiState.dirtyFields = {};
  updateDetailPanel(projectName);
}

function _isConfigDirty() {
  if (!uiState.editingConfig) return false;
  const orig = uiState.dirtyFields;
  const cur = {
    directory: $("cfg-input-directory")?.value.trim() ?? "",
    start:     $("cfg-input-start")?.value.trim() ?? "",
    port:      parseInt($("cfg-input-port")?.value, 10) || 0,
    scheme:    $("cfg-input-scheme")?.value ?? "http",
    stop:      $("cfg-input-stop")?.value.trim() ?? "",
    url:       $("cfg-input-url")?.value.trim() ?? "",
    tags:      $("cfg-input-tags")?.value.trim() ?? "",
    notes:     $("cfg-input-notes")?.value.trim() ?? "",
  };
  return orig.directory !== cur.directory
      || orig.start     !== cur.start
      || String(orig.port) !== String(cur.port)
      || orig.scheme    !== cur.scheme
      || orig.stop      !== cur.stop
      || orig.url       !== cur.url
      || orig.tags      !== cur.tags
      || orig.notes     !== cur.notes;
}

function renderErrorBlock(err) {
  if (!err) return "";
  const safePath = (err.file_ref?.path||"").replace(/\\/g,"\\\\").replace(/'/g,"\\'");
  const openBtn  = err.file_ref ? `<button class="error-action-btn" onclick="apiOpen('${esc(safePath)}','editor')">Open File</button>` : "";
  return `
    <div class="error-block">
      <div class="error-block-message">${esc(err.message)}</div>
      ${err.short ? `<div class="error-block-location">📍 ${esc(err.short)}</div>` : ""}
      <div class="error-block-actions">
        <button class="error-action-btn" onclick="copyError(this)">Copy</button>
        ${openBtn}
      </div>
    </div>`;
}

/**
 * Render the Dependencies section of the detail panel.
 *
 * @param {Array}  deps      - Raw dep config objects from registry
 * @param {Array}  depStatus - Live status results from /api/projects/<name>/deps (or cache)
 * @param {string} name      - Project name (for the Refresh button callback)
 */
function renderDependencies(deps, depStatus, name) {
  if (!deps || !deps.length) return "";

  const icons = { tunnel: "🔗", database: "🗄️", api: "⚡", hosting: "🌐" };

  // Build a lookup: (label || provider) → result
  const statusMap = {};
  (depStatus || []).forEach(d => {
    const key = d.label || d.provider;
    if (key) statusMap[key] = d;
  });

  const safeN = (name || "").replace(/\\/g, "\\\\").replace(/'/g, "\\'");
  const cacheEmpty = !depStatus || depStatus.length === 0;

  const rows = deps.map(d => {
    const key    = d.label || d.provider;
    const result = statusMap[key] || {};
    const status = result.status || "unknown";
    const detail = result.detail || "";
    const pubUrl = result.public_url || "";

    const statusLabels = { connected: "connected", disconnected: "disconnected", unknown: "…" };
    const statusTxt = statusLabels[status] ?? status;

    return `
      <div class="dep-item">
        <span class="dep-icon">${icons[d.type] || "○"}</span>
        <div class="dep-info">
          <div class="dep-label">${esc(d.label || d.provider)}</div>
          <div class="dep-provider">${esc(d.provider)} · ${esc(d.type)}</div>
          ${detail ? `<div class="dep-detail">${esc(detail)}</div>` : ""}
          ${pubUrl  ? `<a href="${esc(pubUrl)}" target="_blank" class="dep-url">${esc(pubUrl)}</a>` : ""}
        </div>
        <span class="dep-status ${status}">${statusTxt}</span>
      </div>`;
  }).join("");

  const hint = cacheEmpty
    ? `<div style="font-size:11px;color:var(--text-muted);margin-bottom:8px">Checking… results appear within 30s or tap Refresh.</div>`
    : "";

  return `
    <div class="detail-section">
      <div class="detail-section-header">
        <div class="detail-section-title" style="margin:0;border:none;padding:0">Dependencies</div>
        <button class="detail-section-action" onclick="loadDeps('${safeN}')">↺ Refresh</button>
      </div>
      ${hint}
      <div class="dep-list">${rows}</div>
    </div>`;
}

function renderSourceSection(p, safeN) {
  const src = p.source;
  if (src && src.type === "github" && src.full_name) {
    const ghUrl = `https://github.com/${src.full_name}`;
    return `
      <div class="detail-section">
        <div class="detail-section-header">
          <div class="detail-section-title" style="margin:0;border:none;padding:0">GitHub Source</div>
          <button class="detail-section-action" onclick="refreshProjectSource('${safeN}')">↻ Refresh</button>
        </div>
        <div class="detail-field">
          <div class="detail-label">Repository</div>
          <div class="detail-value mono"><a href="${esc(ghUrl)}" target="_blank">${esc(src.full_name)}</a></div>
        </div>
      </div>`;
  }
  return `
    <div class="detail-section">
      <div class="detail-section-title">GitHub Source</div>
      <div style="font-size:12px;color:var(--text-muted);margin-bottom:8px">Not linked. Link this project to a GitHub repo to enable metadata refresh.</div>
      <button class="detail-btn" onclick="linkProjectSource('${safeN}')">Link to GitHub…</button>
    </div>`;
}

async function refreshProjectSource(name) {
  const res  = await fetch(`/api/projects/${encodeURIComponent(name)}/refresh`, { method: "POST" });
  const data = await res.json();
  if (!res.ok) { toast(data.error || "Refresh failed", "error"); return; }
  const changed = data.changed || [];
  if (!changed.length) {
    toast("No changes — README metadata matches current values", "success");
  } else {
    toast(`Updated: ${changed.join(", ")}`, "success");
  }
  await refresh();
}

async function linkProjectSource(name) {
  const full_name = await promptAction({
    title: "Link to GitHub",
    hint: "Format: owner/repo",
    placeholder: "e.g. acme/my-project",
  });
  if (!full_name) return;
  if (!full_name.includes("/")) { toast("Expected format: owner/repo", "error"); return; }
  const res  = await fetch(`/api/projects/${encodeURIComponent(name)}/link`, {
    method:  "POST",
    headers: { "Content-Type": "application/json" },
    body:    JSON.stringify({ full_name }),
  });
  const data = await res.json();
  if (!res.ok) { toast(data.error || "Link failed", "error"); return; }
  toast(`Linked to ${full_name}`, "success");
  await refresh();
}

async function closeDetail() {
  if (uiState.editingConfig && _isConfigDirty()) {
    const yes = await confirmAction({
      title: "Discard changes?",
      message: "You have unsaved configuration edits.",
      confirmText: "Discard",
      danger: false,
    });
    if (!yes) return false;
    uiState.editingConfig = null;
    uiState.dirtyFields = {};
  }
  selectedName        = null;
  selectedReceiptHash = null;
  selectedVaultKey    = null;
  selectedAgreementRule  = null;
  selectedInvariantClaim = null;
  selectedRevocationRule = null;
  document.querySelectorAll(".project-row").forEach(r => r.classList.remove("selected"));
  document.querySelectorAll(".tbl tbody tr.sel").forEach(r => r.classList.remove("sel"));
  $("detailPanel").classList.remove("open");
  $("detailInner").innerHTML = `<div class="empty-state" style="padding:60px 20px"><div class="empty-state-sub">Select a row to view details</div></div>`;
}

// ── Log viewer ─────────────────────────────────────────────────────────────

const ERROR_LINE_RE   = /traceback|error:|exception:|fatal|critical|enoent|eaddrinuse|econnrefused|panic:/i;
const WARNING_LINE_RE = /warning|warn:/i;
const SEP_LINE_RE     = /^--- (Started|cmd:)/;

async function loadLogs(name) {
  const viewer = $("logViewer");
  if (!viewer) return;
  try {
    const res  = await fetch(`/api/projects/${encodeURIComponent(name)}/logs`);
    const data = await res.json();
    if (!data.lines||data.lines.length===0) {
      viewer.innerHTML = `<div class="log-empty">No log output yet. Start the project to see output here.</div>`;
      return;
    }
    viewer.innerHTML = data.lines.map(line => {
      let cls = "";
      if (SEP_LINE_RE.test(line))          cls = "is-sep";
      else if (ERROR_LINE_RE.test(line))   cls = "is-error";
      else if (WARNING_LINE_RE.test(line)) cls = "is-warning";
      return `<div class="log-line ${cls}">${esc(line)}</div>`;
    }).join("");
    viewer.scrollTop = viewer.scrollHeight;
  } catch (_) {
    viewer.innerHTML = `<div class="log-empty">Could not load logs.</div>`;
  }
}

// ── Dep force-refresh ──────────────────────────────────────────────────────

/**
 * Synchronous dep check for the selected project.
 * Calls GET /api/projects/<name>/deps, updates the local project's dep_status,
 * and re-renders the detail panel so status lights update immediately.
 */
async function loadDeps(name) {
  try {
    const res  = await fetch(`/api/projects/${encodeURIComponent(name)}/deps`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Dep check failed");

    // Patch dep_status into our local copy so the panel re-renders correctly
    const p = projects.find(x => x.name === name);
    if (p) {
      p.dep_status = data;
      // Recompute composite_status locally to avoid waiting for next poll
      const anyDown = data.some(d => d.status === "disconnected");
      p.composite_status = (p.status === "running" && anyDown) ? "degraded" : p.status;
    }

    if (selectedName === name) updateDetailPanel(name);
    // Also re-render the shelf row so the status light updates
    renderShelf();
  } catch (e) {
    toast(`Dep check: ${e.message}`, "error");
  }
}

// ── Env status in detail panel ─────────────────────────────────────────────

async function loadEnvStatus(name) {
  const list = $("envList");
  if (!list) return;
  const p = projects.find(x => x.name === name);
  if (!p || !p.env || !p.env.length) return;

  try {
    const [overrideRes, summaryRes] = await Promise.all([
      fetch(`/api/vault/overrides/${encodeURIComponent(name)}`),
      fetch("/api/vault"),
    ]);
    const { keys: overrideKeys } = await overrideRes.json();
    const summary = await summaryRes.json();
    const sharedKeys = summary.keys || [];

    list.innerHTML = p.env.map(key => {
      let cls, label;
      if (overrideKeys.includes(key)) {
        cls = "override"; label = "override ✓";
      } else if (sharedKeys.includes(key)) {
        cls = "shared"; label = "shared ✓";
      } else {
        cls = "missing"; label = "not in vault ⚠";
      }
      return `<div class="env-item">
        <span class="env-key">${esc(key)}</span>
        <span class="env-status ${cls}">${label}</span>
      </div>`;
    }).join("");
  } catch (_) {
    list.innerHTML = `<div style="color:var(--text-muted);font-size:12px">Could not load vault status.</div>`;
  }
}

// ── Vault view ─────────────────────────────────────────────────────────────

async function renderVaultView() {
  $("vaultContent").innerHTML = `<div class="empty-state"><div class="empty-state-title">Loading vault…</div></div>`;
  try {
    const [summaryRes, auditRes] = await Promise.all([
      fetch("/api/vault"),
      fetch("/api/vault/audit"),
    ]);
    const summary = await summaryRes.json();
    const audit   = await auditRes.json();
    vaultSummaryCache = summary;
    vaultAuditCache   = audit;

    const overrideProjCount = Object.keys(summary.project_overrides || {}).length;

    $("vaultContent").innerHTML = `
      <div class="vault-view">

        <div class="vault-view-header">
          <div class="view-title" style="margin-bottom:0"><h1>Vault</h1><span class="vsub">${summary.key_count} key${summary.key_count !== 1 ? "s" : ""} · ${summary.encrypted ? "encrypted" : "unencrypted"}</span></div>
          <button class="btn btn-ghost btn-sm" onclick="openVaultKeyModal('shared')">+ Add Key</button>
        </div>

        <!-- Status banner: same encrypted/unencrypted detection logic as
             before (summary.encrypted), restyled to .banner.ok/.banner.warn.
             installVaultDeps() targets #vaultEncBanner/#vaultInstallDepsBtn. -->
        <div class="banner ${summary.encrypted ? "ok" : "warn"}" id="vaultEncBanner">
          <span class="bi" style="color:var(--blue)">⚿</span>
          ${summary.encrypted ? "Vault encrypted (keyring + cryptography)" : "Vault unencrypted"}
          <span class="bspacer"></span>
          ${!summary.encrypted ? `<button class="btn btn-sm" id="vaultInstallDepsBtn" onclick="installVaultDeps()">Fix: Install deps</button>` : ""}
          <span class="num">${summary.key_count} key${summary.key_count !== 1 ? "s" : ""} · ${overrideProjCount} project override${overrideProjCount !== 1 ? "s" : ""}</span>
        </div>

        <!-- Unified key table: reuses vault.audit()'s existing response shape
             for aud-ok/aud-unused/aud-missing coloring, replacing the old
             separate Shared-Keys-list + Audit-findings-list. -->
        <table class="tbl">
          <thead><tr><th>Key</th><th style="width:120px">Audit</th><th>Used by</th></tr></thead>
          <tbody id="vaultKeyTbody">${renderVaultKeyRows(audit)}</tbody>
        </table>

        <!-- Per-project overrides (existing capability, untouched) -->
        <div class="vault-section">
          <div class="vault-section-header">
            <div class="vault-section-title">Per-Project Overrides</div>
          </div>
          <div id="overridesList">${renderOverrideGroups(summary.project_overrides, audit)}</div>
        </div>

        <!-- Import from .env (existing capability, untouched) -->
        <div class="vault-section">
          <div class="vault-section-header">
            <div class="vault-section-title">Import from .env</div>
          </div>
          ${renderImportSection()}
        </div>

      </div>`;

    initVaultViewEvents();
  } catch (e) {
    $("vaultContent").innerHTML = `<div class="empty-state"><div class="empty-state-title">Could not load vault</div><div class="empty-state-sub">${esc(e.message)}</div></div>`;
  }
}

// One row per vault.audit() entry — shared keys AND keys some project
// declares but that don't resolve anywhere (in_shared is always false for
// those, per vault.py's audit(); missing_from is only ever non-empty then).
// aud-missing rows aren't real vault keys (nothing to reveal), so clicking
// one pre-fills the add-key modal instead of opening the detail panel — see
// the click wiring in initVaultViewEvents(), same behavior the old
// .audit-row-missing handler had.
function renderVaultKeyRows(audit) {
  if (!audit || audit.length === 0) {
    return `<tr><td colspan="3"><div class="vault-empty">No shared keys yet. Add your first key above.</div></td></tr>`;
  }
  return audit.map(a => {
    const isMissing = a.missing_from.length > 0;
    const isUnused  = !isMissing && a.unused;
    const statusCls = isMissing ? "aud-missing" : (isUnused ? "aud-unused" : "aud-ok");
    const statusTxt = isMissing ? "✗ missing" : (isUnused ? "○ unused" : "● ok");
    const usedBy = isMissing
      ? `referenced by ${esc(a.missing_from.join(", "))}`
      : (a.declared_by.length ? esc(a.declared_by.join(", ")) : "—");
    return `
      <tr data-key="${esc(a.key)}" data-missing="${isMissing}">
        <td class="canon">${esc(a.key)}</td>
        <td class="${statusCls}">${statusTxt}</td>
        <td class="num">${usedBy}</td>
      </tr>`;
  }).join("");
}

function renderOverrideGroups(overrides, audit) {
  const projects = Object.keys(overrides || {});
  if (projects.length === 0) {
    return `<div class="vault-empty">No per-project overrides. Use overrides when a project needs a different value for a shared key (e.g. a dev database URL).</div>`;
  }
  return projects.map(proj => {
    const keys = overrides[proj] || [];
    const rows = keys.map(key => {
      return `
        <div class="vault-key-row" data-proj="${esc(proj)}" data-key="${esc(key)}">
          <div><div class="vault-key-name">${esc(key)}</div></div>
          <div class="vault-key-value" id="ovval-${esc(proj)}-${esc(key)}">••••••••</div>
          <div class="vault-row-actions">
            <button class="vault-row-btn reveal-ov-btn" data-proj="${esc(proj)}" data-key="${esc(key)}" title="Reveal">👁</button>
            <button class="vault-row-btn edit-ov-btn"   data-proj="${esc(proj)}" data-key="${esc(key)}" title="Edit">✎</button>
            <button class="vault-row-btn delete delete-ov-btn" data-proj="${esc(proj)}" data-key="${esc(key)}" title="Delete">✕</button>
          </div>
        </div>`;
    }).join("");
    return `
      <div class="vault-override-project">
        <div class="vault-override-project-header">
          <span>${esc(proj)}</span>
          <button class="vault-row-btn add-override-btn" data-proj="${esc(proj)}" title="Add override for this project" style="font-size:12px">+ Add</button>
        </div>
        ${rows}
      </div>`;
  }).join("");
}

function renderImportSection() {
  const projectOptions = projects.map(p =>
    `<option value="${esc(p.name)}">${esc(p.name)}</option>`
  ).join("");
  return `
    <div class="vault-import-form">
      <div>
        <label style="margin-bottom:6px;display:block">Paste .env contents</label>
        <textarea id="importContent" placeholder="KEY=value&#10;ANOTHER_KEY=value"></textarea>
      </div>
      <div class="vault-import-controls">
        <select id="importTarget">
          <option value="">Import to Shared Vault</option>
          ${projectOptions ? `<optgroup label="Import as override for:">${projectOptions}</optgroup>` : ""}
        </select>
        <button class="btn btn-ghost btn-sm" onclick="runImport()">Import</button>
        <label class="btn btn-ghost btn-sm" style="cursor:pointer">
          Choose file
          <input type="file" accept=".env,text/plain" style="display:none" onchange="loadEnvFile(this)">
        </label>
      </div>
      <div id="importResult" style="font-size:12px;color:var(--text-muted)"></div>
    </div>`;
}

function buildLocalhostHint(value, key, proj) {
  // value must match http://localhost:PORT or https://localhost:PORT
  const m = /^https?:\/\/localhost:(\d+)/.exec(value);
  if (!m) return null;
  const port = parseInt(m[1], 10);
  const match = hostnames.find(h => h.port === port);
  if (!match) return null;
  const hostnameUrl = `http://${match.hostname}`;
  const safeKey  = key.replace(/\\/g, "\\\\").replace(/'/g, "\\'");
  const safeProj = (proj || "").replace(/\\/g, "\\\\").replace(/'/g, "\\'");
  return `
    <div class="vault-hostname-hint">
      You can also use <code>${esc(hostnameUrl)}</code>
      <button class="vault-hostname-hint-btn"
              onclick="useHostnameForVaultKey('${safeKey}','${esc(hostnameUrl)}','${safeProj}')">
        Use hostname
      </button>
    </div>`;
}

function initVaultViewEvents() {
  // Unified key table rows: reuses the existing missing/unused audit fields
  // (see renderVaultKeyRows()) to decide the click behavior — a "missing" row
  // isn't an actual vault key (nothing to reveal), so it pre-fills the
  // add-key modal instead, matching the old .audit-row-missing click.
  document.querySelectorAll("#vaultKeyTbody tr[data-key]").forEach(row => {
    row.addEventListener("click", () => {
      const key = row.dataset.key;
      if (row.dataset.missing === "true") {
        openVaultKeyModal("shared", null, null);
        setTimeout(() => {
          const input = document.querySelector("#vaultKeyForm [name=key]");
          if (input) { input.value = key; input.readOnly = false; }
        }, 100);
        return;
      }
      selectVaultKey(key);
    });
  });

  // Reveal override
  document.querySelectorAll(".reveal-ov-btn").forEach(btn => {
    btn.addEventListener("click", async () => {
      const { proj, key } = btn.dataset;
      const el = $(`ovval-${proj}-${key}`);
      if (el.classList.contains("revealed")) {
        el.textContent = "••••••••"; el.classList.remove("revealed");
        el.closest(".vault-key-row").querySelector(".vault-hostname-hint")?.remove();
        return;
      }
      try {
        const res = await fetch(`/api/vault/overrides/${encodeURIComponent(proj)}/${encodeURIComponent(key)}`);
        const d   = await res.json();
        el.textContent = d.value; el.classList.add("revealed");
        const hint = buildLocalhostHint(d.value, key, proj);
        if (hint) {
          el.closest(".vault-key-row")
            .querySelector(".vault-row-actions")
            .insertAdjacentHTML("beforebegin", hint);
        }
      } catch (_) { toast("Could not reveal value", "error"); }
    });
  });

  // Add override
  document.querySelectorAll(".add-override-btn").forEach(btn => {
    btn.addEventListener("click", () => openVaultKeyModal("override", btn.dataset.proj));
  });

  // Edit override
  document.querySelectorAll(".edit-ov-btn").forEach(btn => {
    btn.addEventListener("click", () => openVaultKeyModal("override", btn.dataset.proj, btn.dataset.key));
  });

  // Delete override
  document.querySelectorAll(".delete-ov-btn").forEach(btn => {
    btn.addEventListener("click", async () => {
      const { proj, key } = btn.dataset;
      const yes = await confirmAction({
        title: "Delete override?",
        message: `Remove override for "${key}" from "${proj}".`,
        confirmText: "Delete",
        danger: true,
      });
      if (!yes) return;
      await fetch(`/api/vault/overrides/${encodeURIComponent(proj)}/${encodeURIComponent(key)}`, { method: "DELETE" });
      toast(`Override removed`, "success");
      await renderVaultView();
    });
  });
}

// ── Vault detail panel (key-row selection) ───────────────────────────────────

function selectVaultKey(key) {
  selectedVaultKey    = key;
  selectedReceiptHash = null;
  selectedName        = null;
  document.querySelectorAll(".tbl tbody tr.sel").forEach(row => row.classList.remove("sel"));
  document.querySelector(`#vaultKeyTbody tr[data-key="${CSS.escape(key)}"]`)?.classList.add("sel");
  $("detailInner").innerHTML = renderVaultDetail(key);
  wireVaultDetailEvents(key);
  $("detailPanel").classList.add("open");
}

function renderVaultDetail(key) {
  const a = vaultAuditCache.find(x => x.key === key) || { declared_by: [], overridden_by: [], unused: true };
  const usedByRows = a.declared_by.length
    ? a.declared_by.map(proj => {
        const isOverride = a.overridden_by.includes(proj);
        return `<div class="d-row"><span class="k">${esc(proj)}</span><span class="v"${isOverride ? ' style="color:var(--amber)"' : ""}>${isOverride ? "override" : "shared"}</span></div>`;
      }).join("")
    : `<div class="d-row"><span class="k">—</span><span class="v">unused</span></div>`;

  // vault.summary() only exposes an `encrypted` boolean, no store-name field
  // — this shows exactly that fact rather than naming a specific backend
  // (e.g. "keyring") the API response doesn't actually confirm.
  const storeLabel = vaultSummaryCache && vaultSummaryCache.encrypted ? "encrypted" : "unencrypted";

  return `
    <div class="detail-close-row"><button class="icon-btn" onclick="closeDetail()">✕</button></div>
    <div class="d-title">${esc(key)}</div>
    <div class="d-meta">${a.unused ? "○ unused" : "● ok"} · shared</div>
    <div class="d-h">Value</div>
    <div class="d-canon" id="vaultDetailValue" data-revealed="false">••••••••••••••••••••••••</div>
    <div class="d-actions">
      <button class="reveal-btn" id="vaultDetailReveal">Reveal</button>
      <button class="reveal-btn" id="vaultDetailCopy">Copy</button>
      <button class="reveal-btn" id="vaultDetailEdit">Edit</button>
      <button class="reveal-btn" id="vaultDetailDelete">Delete</button>
    </div>
    <div class="d-h">Used by</div>
    ${usedByRows}
    <div class="d-h">Backend</div>
    <div class="d-row"><span class="k">store</span><span class="v">${esc(storeLabel)}</span></div>`;
}

// Attached via addEventListener (not inline onclick) so key names containing
// quotes/backslashes never need string-escaping into an HTML attribute.
function wireVaultDetailEvents(key) {
  $("vaultDetailReveal")?.addEventListener("click", () => toggleVaultDetailReveal(key));
  $("vaultDetailCopy")?.addEventListener("click", copyVaultDetailValue);
  $("vaultDetailEdit")?.addEventListener("click", () => openVaultKeyModal("shared", null, key));
  $("vaultDetailDelete")?.addEventListener("click", () => deleteVaultKey(key));
}

// Reuses the existing GET /api/vault/keys/<key> endpoint — same fetch the old
// inline reveal-key-btn used, no new backend call.
async function toggleVaultDetailReveal(key) {
  const el  = $("vaultDetailValue");
  const btn = $("vaultDetailReveal");
  if (!el || !btn) return;
  if (el.dataset.revealed === "true") {
    el.textContent = "••••••••••••••••••••••••";
    el.dataset.revealed = "false";
    btn.textContent = "Reveal";
    return;
  }
  try {
    const res = await fetch(`/api/vault/keys/${encodeURIComponent(key)}`);
    const d   = await res.json();
    el.textContent = d.value;
    el.dataset.revealed = "true";
    btn.textContent = "Hide";
  } catch (_) { toast("Could not reveal key", "error"); }
}

function copyVaultDetailValue() {
  const el = $("vaultDetailValue");
  if (!el || el.dataset.revealed !== "true") { toast("Reveal the value first", "info"); return; }
  navigator.clipboard.writeText(el.textContent)
    .then(() => toast("Copied", "success"))
    .catch(() => toast("Could not copy", "error"));
}

// Same confirm copy + endpoint the old inline delete-key-btn used, relocated
// here since that markup no longer exists (folded into the unified table).
async function deleteVaultKey(key) {
  const yes = await confirmAction({
    title: "Delete key?",
    message: `Remove "${key}" from the vault. Projects using it will lose access.`,
    confirmText: "Delete",
    danger: true,
  });
  if (!yes) return;
  await fetch(`/api/vault/keys/${encodeURIComponent(key)}`, { method: "DELETE" });
  toast(`${key} deleted`, "success");
  closeDetail();
  await renderVaultView();
}

async function runImport() {
  const content = $("importContent").value.trim();
  const target  = $("importTarget").value;
  const result  = $("importResult");
  if (!content) { result.textContent = "Paste some .env content first."; return; }
  try {
    const res  = await fetch("/api/vault/import", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content, project: target || null }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error);
    const dest = target ? `as override for ${target}` : "to shared vault";
    result.style.color = "var(--green)";
    result.textContent = `✓ Imported ${data.count} key${data.count!==1?"s":""} ${dest}: ${data.keys.join(", ")}`;
    toast(`Imported ${data.count} key${data.count!==1?"s":""}`, "success");
    $("importContent").value = "";
    await renderVaultView();
  } catch (e) {
    result.style.color = "var(--red)";
    result.textContent = e.message;
  }
}

function loadEnvFile(input) {
  const file = input.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = e => { $("importContent").value = e.target.result; };
  reader.readAsText(file);
}

// ── Vault key modal ────────────────────────────────────────────────────────

function initVaultKeyModal() {
  $("vaultKeyModalClose").addEventListener("click", closeVaultKeyModal);
  $("vaultKeyCancelBtn").addEventListener("click", closeVaultKeyModal);
  $("vaultKeyModalOverlay").addEventListener("click", e => {
    if (e.target === $("vaultKeyModalOverlay")) closeVaultKeyModal();
  });
  $("vaultKeyReveal").addEventListener("click", () => {
    const inp = $("vaultKeyForm").querySelector("[name='value']");
    inp.type  = inp.type === "password" ? "text" : "password";
    $("vaultKeyReveal").textContent = inp.type === "password" ? "Show" : "Hide";
  });
  $("vaultKeyForm").addEventListener("submit", async e => {
    e.preventDefault();
    const fd      = new FormData(e.target);
    const mode    = fd.get("mode");
    const project = fd.get("project");
    const key     = fd.get("key").trim().toUpperCase();
    const value   = fd.get("value");
    $("vaultKeyError").textContent = "";
    try {
      let res;
      if (mode === "override" && project) {
        res = await fetch(`/api/vault/overrides/${encodeURIComponent(project)}`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ key, value }),
        });
      } else {
        res = await fetch("/api/vault/keys", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ key, value }),
        });
      }
      const data = await res.json();
      if (!res.ok) throw new Error(data.error);
      toast(`${key} saved`, "success");
      closeVaultKeyModal();
      if (activeView === "vault") await renderVaultView();
    } catch (err) {
      $("vaultKeyError").textContent = err.message;
    }
  });
}

function openVaultKeyModal(mode, project = null, existingKey = null) {
  const form = $("vaultKeyForm");
  form.querySelector("[name='mode']").value    = mode;
  form.querySelector("[name='project']").value = project || "";
  form.querySelector("[name='key']").value     = existingKey || "";
  form.querySelector("[name='value']").value   = "";
  form.querySelector("[name='value']").type    = "password";
  $("vaultKeyReveal").textContent = "Show";
  $("vaultKeyError").textContent  = "";

  const proj = project ? ` for ${project}` : "";
  $("vaultKeyModalTitle").textContent = existingKey
    ? `Edit Key${proj}`
    : (mode === "override" ? `Add Override${proj}` : "Add Shared Key");

  $("vaultKeyModalOverlay").classList.add("open");
  setTimeout(() => {
    const keyInput = form.querySelector("[name='key']");
    if (existingKey) form.querySelector("[name='value']").focus();
    else keyInput.focus();
  }, 60);
}

function closeVaultKeyModal() {
  $("vaultKeyModalOverlay").classList.remove("open");
  $("vaultKeyForm").reset();
  $("vaultKeyError").textContent = "";
}

// ── Project actions ────────────────────────────────────────────────────────

async function startProject(name) {
  const btn = document.querySelector(`.project-row[data-name="${CSS.escape(name)}"] .start-btn`);
  if (btn) { btn.disabled = true; btn.textContent = "…"; }
  try {
    const res  = await fetch(`/api/projects/${encodeURIComponent(name)}/start`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error);
    toast(`${name} started`, "success");
    await refresh();
    if (selectedName === name) loadLogs(name);
  } catch (e) { toast(e.message, "error"); }
  if (btn) { btn.disabled = false; btn.textContent = "▶"; }
}

async function stopProject(name) {
  const btn = document.querySelector(`.project-row[data-name="${CSS.escape(name)}"] .stop-btn`);
  if (btn) { btn.disabled = true; btn.textContent = "…"; }
  try {
    const res  = await fetch(`/api/projects/${encodeURIComponent(name)}/stop`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error);
    toast(`${name} stopped`, "success");
    await refresh();
  } catch (e) { toast(e.message, "error"); }
  if (btn) { btn.disabled = false; btn.textContent = "■"; }
}

async function removeProject(name) {
  const yes = await confirmAction({
    title: "Remove project?",
    message: `This removes "${name}" from the registry. The project files are not deleted.`,
    confirmText: "Remove",
    danger: true,
  });
  if (!yes) return;
  try {
    const res  = await fetch(`/api/projects/${encodeURIComponent(name)}`, { method: "DELETE" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error);
    toast(`${name} removed`, "success");
    closeDetail();
    await refresh();
  } catch (e) { toast(e.message, "error"); }
}

// ── Ports modal ────────────────────────────────────────────────────────────

let _listeners = [];

async function openPortsModal() {
  $("portsOverlay").style.display = "flex";
  await loadPorts();
}

function closePortsModal() {
  $("portsOverlay").style.display = "none";
}

async function loadPorts() {
  $("portsBanner").style.display = "none";
  try {
    const res  = await fetch("/api/listeners");
    const data = await res.json();
    if (!res.ok) {
      _listeners = [];
      $("portsBanner").textContent   = data.error || "Could not load listeners.";
      $("portsBanner").style.display = "block";
    } else {
      _listeners = data;
    }
    renderPortsTable();
  } catch (e) {
    $("portsBanner").textContent   = "Network error: " + e.message;
    $("portsBanner").style.display = "block";
  }
}

function renderPortsTable() {
  const q = ($("portsSearch").value || "").trim().toLowerCase();
  const filtered = _listeners.filter(l => {
    if (!q) return true;
    return String(l.port).includes(q)
        || (l.name || "").toLowerCase().includes(q)
        || (l.cmdline || "").toLowerCase().includes(q)
        || (l.project_name || "").toLowerCase().includes(q);
  });
  const body = $("portsRows");
  if (!filtered.length) {
    body.innerHTML = `<tr><td colspan="6" style="padding:20px;text-align:center;color:var(--text-muted)">No listeners${q ? " match filter" : ""}</td></tr>`;
    return;
  }
  const kindStyle = {
    seshat:   "color:#7aa9ff",
    project:  "color:#4caf50",
    conflict: "color:#e53935",
    orphan:   "color:#ff9800",
  };
  const kindLabel = {
    seshat:   "seshat",
    project:  "project",
    conflict: "conflict",
    orphan:   "orphan",
  };
  body.innerHTML = filtered.map(l => {
    const nameCell = l.project_name
      ? `${esc(l.project_name)}<div style="font-size:11px;color:var(--text-muted)">${esc(l.name||"")}</div>`
      : esc(l.name || "—");
    const disableKill = l.kind === "seshat";
    return `<tr style="border-bottom:1px solid var(--border)">
      <td style="padding:8px 4px;font-family:var(--mono,monospace)"><strong>:${l.port}</strong></td>
      <td style="padding:8px 4px;font-size:11px;${kindStyle[l.kind]||""}">${kindLabel[l.kind]||l.kind}</td>
      <td style="padding:8px 4px">${nameCell}</td>
      <td style="padding:8px 4px;font-family:var(--mono,monospace);font-size:12px">${l.pid}</td>
      <td style="padding:8px 4px;font-size:12px;color:var(--text-muted);max-width:360px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(l.cmdline||"")}">${esc((l.cmdline||"").slice(0,120))}</td>
      <td style="padding:8px 4px;text-align:right">
        <button class="detail-btn danger" style="padding:3px 10px;font-size:12px" ${disableKill ? "disabled title=\"Refusing to kill Seshat itself\"" : ""} onclick="killListener(${l.port})">Kill</button>
      </td>
    </tr>`;
  }).join("");
}

async function killListener(port) {
  const entry = _listeners.find(l => l.port === port);
  const label = entry?.project_name ? `${entry.project_name} (:${port})` : `port :${port}`;
  const yes = await confirmAction({
    title:       "Kill process?",
    message:     `This will terminate the process on ${label}.`,
    confirmText: "Kill",
    danger:      true,
  });
  if (!yes) return;
  const res  = await fetch(`/api/listeners/${port}/stop`, { method: "POST" });
  const data = await res.json();
  if (!res.ok) { toast(data.error || "Kill failed", "error"); return; }
  toast(`Killed process on :${port}`, "success");
  await loadPorts();
  await refresh();
}

async function stopOrphan(port) {
  const yes = await confirmAction({
    title: "Stop process?",
    message: `This will kill the process on port ${port}.`,
    confirmText: "Stop",
    danger: true,
  });
  if (!yes) return;
  try {
    const res  = await fetch(`/api/orphans/${port}/stop`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error);
    toast(`Process on :${port} stopped`, "success");
    await refresh();
  } catch (e) { toast(e.message, "error"); }
}

function adoptOrphan(port, name, cmdline) {
  openProjectModal({
    name: name || "",
    port: port,
    start: cmdline || "",
    directory: "",
  });
}

function copyError(btn) {
  const block   = btn.closest(".error-block");
  const message = block?.querySelector(".error-block-message")?.textContent || "";
  const loc     = block?.querySelector(".error-block-location")?.textContent || "";
  navigator.clipboard.writeText([message, loc].filter(Boolean).join("\n"))
    .then(() => toast("Error copied", "success"))
    .catch(() => toast("Could not copy", "error"));
}

async function apiOpen(path, mode) {
  try {
    const res = await fetch("/api/open", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, mode }),
    });
    if (!res.ok) { const d = await res.json(); throw new Error(d.error); }
  } catch (e) { toast(`Could not open: ${e.message}`, "error"); }
}

// ── Group actions ──────────────────────────────────────────────────────────

async function startGroup(name) {
  try {
    const res  = await fetch(`/api/groups/${encodeURIComponent(name)}/start`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error);
    const started = (data.results||[]).filter(r=>r.status==="started").length;
    const skipped = (data.results||[]).filter(r=>r.status==="already_running").length;
    const failed  = (data.results||[]).filter(r=>r.error).length;
    let msg = `${name}: ${started} started`;
    if (skipped) msg += `, ${skipped} already running`;
    if (failed)  msg += `, ${failed} failed`;
    toast(msg, failed?"error":"success");
    await refresh();
  } catch (e) { toast(e.message, "error"); }
}

async function stopGroup(name) {
  try {
    const res  = await fetch(`/api/groups/${encodeURIComponent(name)}/stop`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error);
    const stopped = (data.results||[]).filter(r=>r.status==="stopped").length;
    toast(`${name}: ${stopped} stopped`, "success");
    await refresh();
  } catch (e) { toast(e.message, "error"); }
}

async function deleteGroup(name) {
  const yes = await confirmAction({
    title: "Delete group?",
    message: `Remove the group "${name}". Projects in it are not affected.`,
    confirmText: "Delete",
    danger: true,
  });
  if (!yes) return;
  try {
    const res  = await fetch(`/api/groups/${encodeURIComponent(name)}`, { method: "DELETE" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error);
    toast(`Group "${name}" removed`, "success");
    await refresh();
  } catch (e) { toast(e.message, "error"); }
}

// ── Search & Sort ─────────────────────────────────────────────────────────

function initSearchSort() {
  const input = $("searchInput");
  const clear = $("searchClear");

  input.addEventListener("input", () => {
    uiState.searchQuery = input.value;
    clear.style.display = input.value ? "block" : "none";
    renderShelf();
    renderOrphans();
  });

  clear.addEventListener("click", () => {
    input.value = "";
    uiState.searchQuery = "";
    clear.style.display = "none";
    renderShelf();
    renderOrphans();
  });
}

function cycleSortField() {
  const fields = ["name", "status", "port"];
  const i = fields.indexOf(uiState.sortField);
  uiState.sortField = fields[(i + 1) % fields.length];
  $("sortFieldBtn").textContent = uiState.sortField.charAt(0).toUpperCase() + uiState.sortField.slice(1);
  renderShelf();
}

function toggleSortDir() {
  uiState.sortDir = uiState.sortDir === "asc" ? "desc" : "asc";
  $("sortDirBtn").textContent = uiState.sortDir === "asc" ? "↑" : "↓";
  renderShelf();
}

function _matchesSearch(p, query) {
  if (!query) return true;
  const q = query.toLowerCase();
  return p.name.toLowerCase().includes(q)
      || (p.directory || "").toLowerCase().includes(q)
      || (p.notes || "").toLowerCase().includes(q)
      || (p.tags || []).some(t => t.toLowerCase().includes(q));
}

function _sortProjects(list) {
  const statusOrder = { running: 0, conflict: 1, stopped: 2 };
  const dir = uiState.sortDir === "asc" ? 1 : -1;
  return [...list].sort((a, b) => {
    switch (uiState.sortField) {
      case "status": return dir * ((statusOrder[a.status] ?? 3) - (statusOrder[b.status] ?? 3));
      case "port":   return dir * (a.port - b.port);
      default:       return dir * a.name.localeCompare(b.name);
    }
  });
}

// ── Project modal ──────────────────────────────────────────────────────────

function initProjectModal() {
  $("addProjectBtn").addEventListener("click", () => openProjectModal());
  $("modalClose").addEventListener("click",   closeProjectModal);
  $("cancelBtn").addEventListener("click",    closeProjectModal);
  $("modalOverlay").addEventListener("click", e => { if (e.target===$("modalOverlay")) closeProjectModal(); });
  $("addProjectForm").querySelector("[name='port']").addEventListener("input", e => {
    const f = $("addProjectForm").querySelector("[name='url']");
    if (!f.value) f.placeholder = `http://localhost:${e.target.value}`;
  });
  $("addProjectForm").addEventListener("submit", async e => {
    e.preventDefault();
    const fd   = new FormData(e.target);
    const port = parseInt(fd.get("port"), 10);
    const tags = fd.get("tags").split(",").map(t=>t.trim()).filter(Boolean);
    const payload = {
      name: fd.get("name").trim(), port,
      directory: fd.get("directory").trim(), start: fd.get("start").trim(),
      url: fd.get("url").trim()||`http://localhost:${port}`,
      tags, notes: fd.get("notes").trim(),
    };
    $("formError").textContent = "";
    try {
      const res  = await fetch("/api/projects", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error);
      toast(`${payload.name} registered`, "success");
      closeProjectModal(); await refresh(); selectProject(payload.name);
    } catch (err) { $("formError").textContent = err.message; }
  });
}

function openProjectModal(prefill) {
  const form = $("addProjectForm");
  if (prefill) {
    if (prefill.name)  form.querySelector("[name='name']").value  = prefill.name;
    if (prefill.port)  form.querySelector("[name='port']").value  = prefill.port;
    if (prefill.start) form.querySelector("[name='start']").value = prefill.start;
    if (prefill.directory !== undefined) form.querySelector("[name='directory']").value = prefill.directory;
  }
  $("modalOverlay").classList.add("open");
  const focusField = (prefill && prefill.name) ? "directory" : "name";
  setTimeout(() => form.querySelector(`[name='${focusField}']`).focus(), 60);
}
function closeProjectModal() {
  $("modalOverlay").classList.remove("open");
  $("addProjectForm").reset();
  $("formError").textContent = "";
}

// ── Group modal ────────────────────────────────────────────────────────────

function initGroupModal() {
  // #addGroupBtn now lives inside the dynamically-rendered Projects sub-nav
  // (renderProjectsSubnav()), not static boot-time markup — it wires
  // openGroupModal() via inline onclick instead, since it doesn't exist yet
  // when initGroupModal() runs (would throw on a null element otherwise).
  $("groupModalClose").addEventListener("click", closeGroupModal);
  $("groupCancelBtn").addEventListener("click",  closeGroupModal);
  $("groupModalOverlay").addEventListener("click", e => { if (e.target===$("groupModalOverlay")) closeGroupModal(); });
  $("addGroupForm").addEventListener("submit", async e => {
    e.preventDefault();
    const fd       = new FormData(e.target);
    const name     = fd.get("groupName").trim();
    const selected = [...document.querySelectorAll("#groupProjectCheckboxes input:checked")].map(cb=>cb.value);
    $("groupFormError").textContent = "";
    try {
      const res  = await fetch("/api/groups", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, projects: selected }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error);
      toast(`Group "${name}" created`, "success");
      closeGroupModal(); await refresh();
    } catch (err) { $("groupFormError").textContent = err.message; }
  });
}

function openGroupModal() {
  const box = $("groupProjectCheckboxes");
  box.innerHTML = projects.length === 0
    ? `<div style="color:var(--text-muted);font-size:12px;padding:4px">No projects registered yet.</div>`
    : projects.map(p => `
        <label class="checkbox-item">
          <input type="checkbox" value="${esc(p.name)}">
          <span>${esc(p.name)}</span>
          <span class="check-port">:${p.port}</span>
        </label>`).join("");
  $("groupModalOverlay").classList.add("open");
  setTimeout(() => $("addGroupForm").querySelector("[name='groupName']").focus(), 60);
}

function closeGroupModal() {
  $("groupModalOverlay").classList.remove("open");
  $("addGroupForm").reset();
  $("groupFormError").textContent = "";
}

// ── Organize view ──────────────────────────────────────────────────────────

async function loadFolderMap() {
  const el = $("folderMapContent");
  if (!el) return;
  try {
    const res  = await fetch("/api/organize/map");
    const data = await res.json();
    el.innerHTML = renderFolderMap(data);
  } catch (e) {
    el.innerHTML = `<div class="empty-state"><div class="empty-state-sub">Could not load folder map.</div></div>`;
  }
}

function renderFolderMap(groups) {
  if (!groups || groups.length === 0) {
    return `<div class="empty-state"><div class="empty-state-sub">No projects registered yet.</div></div>`;
  }
  return groups.map(g => `
    <div class="folder-group">
      <div class="folder-group-header">${esc(shortPath(g.parent))}</div>
      ${g.projects.map(p => `
        <div class="folder-group-row">
          <span class="folder-project-name">${esc(p.name)}</span>
          <span class="folder-project-port">:${p.port}</span>
          <span class="folder-project-dir">${esc(shortPath(p.directory))}</span>
          ${(p.tags||[]).slice(0,3).map(t=>`<span class="tag">${esc(t)}</span>`).join("")}
        </div>`).join("")}
    </div>`).join("");
}

async function loadRecommendations() {
  const el   = $("recommendationsContent");
  const root = ($("structureRoot") || {}).value || "~/Projects";
  if (!el) return;
  try {
    const res  = await fetch(`/api/organize/recommendations?root=${encodeURIComponent(root)}`);
    const data = await res.json();
    el.innerHTML = renderRecommendations(data);
  } catch (e) {
    el.innerHTML = `<div class="empty-state"><div class="empty-state-sub">Could not load recommendations.</div></div>`;
  }
}

function renderRecommendations(recs) {
  if (!recs || recs.length === 0) {
    return `<div class="empty-state"><div class="empty-state-sub">No projects to organize.</div></div>`;
  }
  return `
    <table class="organize-table">
      <thead>
        <tr>
          <th>Project</th>
          <th>Current Location</th>
          <th>Suggested Location</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        ${recs.map(r => {
          const already = r.current === r.suggested;
          return `
            <tr class="rec-row ${already ? 'rec-row--done' : ''}" data-project="${esc(r.project_name)}">
              <td class="rec-name">${esc(r.project_name)}</td>
              <td class="rec-current mono">${esc(shortPath(r.current))}</td>
              <td class="rec-dest">
                <input class="rec-dest-input mono" type="text"
                  value="${esc(r.suggested)}"
                  ${already ? 'disabled' : ''}
                  data-original="${esc(r.suggested)}">
              </td>
              <td class="rec-action">
                ${already
                  ? `<span class="rec-done-badge">✓ moved</span>`
                  : `<button class="btn btn-ghost btn-sm move-btn"
                       onclick="moveSingle('${esc(r.project_name.replace(/'/g, "\\'"))}', this)">
                       Move
                     </button>`}
              </td>
            </tr>`;
        }).join("")}
      </tbody>
    </table>`;
}

async function moveSingle(projectName, btn) {
  const row  = btn.closest(".rec-row");
  const dest = row.querySelector(".rec-dest-input").value.trim();
  if (!dest) { toast("Destination cannot be empty", "error"); return; }

  // Check if project is running
  const p = projects.find(x => x.name === projectName);
  if (p && p.status === "running") {
    const yes = await confirmAction({
      title: `"${projectName}" is running`,
      message: "Moving it won't affect the running process, but the next start will use the new location.",
      confirmText: "Continue",
    });
    if (!yes) return;
  }

  btn.disabled = true;
  btn.textContent = "Moving…";

  const result = await _doMigrate(projectName, dest, true);
  if (!result) {
    btn.disabled = false;
    btn.textContent = "Move";
    return;
  }

  toast(`${projectName} moved`, "success");
  await Promise.all([loadFolderMap(), loadRecommendations()]);
}

async function _doMigrate(projectName, destination, force) {
  try {
    const res  = await fetch("/api/organize/migrate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project: projectName, destination, force }),
    });
    const data = await res.json();
    if (!res.ok) { toast(data.error || "Migration failed", "error"); return null; }
    if (data.warning === "project_running") {
      toast("Project is currently running — stop it first or use the individual Move button to confirm.", "error");
      return null;
    }
    return data;
  } catch (e) {
    toast(`Migration error: ${e.message}`, "error");
    return null;
  }
}

async function moveAll() {
  const rows = document.querySelectorAll(".rec-row:not(.rec-row--done)");
  if (rows.length === 0) { toast("Nothing to move", "info"); return; }

  // Collect destinations from the editable inputs
  const moves = [...rows].map(row => ({
    project:     row.dataset.project,
    destination: row.querySelector(".rec-dest-input").value.trim(),
  })).filter(m => m.destination);

  // Identify running projects
  const runningNames = moves
    .filter(m => projects.find(p => p.name === m.project && p.status === "running"))
    .map(m => m.project);

  if (runningNames.length > 0) {
    const yes = await confirmAction({
      title: `${runningNames.length} project${runningNames.length > 1 ? "s" : ""} running`,
      message: `${runningNames.join(", ")} — moving won't affect running processes, but the next start will use the new locations.`,
      confirmText: "Continue",
    });
    if (!yes) return;
  }

  const btn = $("moveAllBtn");
  btn.disabled = true;
  btn.textContent = "Moving…";

  let succeeded = 0;
  for (const { project, destination } of moves) {
    const result = await _doMigrate(project, destination, true);
    if (!result) {
      // _doMigrate already toasted the error; stop on hard failure
      break;
    }
    succeeded++;
  }

  btn.disabled = false;
  btn.textContent = "Move All";

  if (succeeded > 0) {
    toast(`${succeeded} project${succeeded > 1 ? "s" : ""} moved`, "success");
    await Promise.all([loadFolderMap(), loadRecommendations(), loadMoveHistory()]);
  }
}

async function loadMoveHistory() {
  const el = $("moveHistoryContent");
  if (!el) return;
  try {
    const res  = await fetch("/api/organize/history");
    const data = await res.json();
    el.innerHTML = renderMoveHistory(data);
  } catch (e) {
    el.innerHTML = `<div class="empty-state"><div class="empty-state-sub">Could not load history.</div></div>`;
  }
}

function renderMoveHistory(moves) {
  if (!moves || moves.length === 0) {
    return `<div class="empty-state"><div class="empty-state-sub">No moves recorded yet.</div></div>`;
  }
  return `
    <table class="organize-table">
      <thead>
        <tr>
          <th>Project</th>
          <th>From</th>
          <th>To</th>
          <th>Date</th>
          <th>Git</th>
          <th>Health</th>
          <th>Status</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        ${moves.map(m => {
          const rolledBack = m.rolled_back;
          const date = new Date(m.timestamp).toLocaleDateString("en-US", {
            month: "short", day: "numeric", year: "numeric",
          });
          return `
            <tr class="history-row ${rolledBack ? 'history-row--rolled-back' : ''}" data-move-id="${esc(m.id)}">
              <td>${esc(m.project)}</td>
              <td class="mono history-path">${esc(shortPath(m.from))}</td>
              <td class="mono history-path">${esc(shortPath(m.to))}</td>
              <td class="history-date">${date}</td>
              <td class="history-check">${m.git_verified ? "✓" : "✗"}</td>
              <td class="history-check">${m.health_verified ? "✓" : "✗"}</td>
              <td>${rolledBack
                ? `<span class="history-status rolled-back">rolled back</span>`
                : `<span class="history-status moved">moved</span>`}</td>
              <td>
                ${rolledBack
                  ? ""
                  : `<button class="btn btn-ghost btn-sm rollback-btn"
                       onclick="doRollback(this.closest('tr').dataset.moveId)">Roll Back</button>`}
              </td>
            </tr>`;
        }).join("")}
      </tbody>
    </table>`;
}

async function doRollback(moveId) {
  const yes = await confirmAction({
    title: "Roll back this move?",
    message: "The folder will be moved to its original location and the registry will be updated.",
    confirmText: "Roll Back",
    danger: true,
  });
  if (!yes) return;
  try {
    const res  = await fetch("/api/organize/rollback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ move_id: moveId }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error);
    toast("Rolled back successfully", "success");
    await Promise.all([loadFolderMap(), loadRecommendations(), loadMoveHistory()]);
  } catch (e) {
    toast(`Rollback failed: ${e.message}`, "error");
  }
}

// ── Input prompt dialog ───────────────────────────────────────────────────

let _promptCleanup = null;

function promptAction({ title, hint = "", placeholder = "" }) {
  return new Promise(resolve => {
    if (_promptCleanup) { _promptCleanup(null); }

    $("promptTitle").textContent = title;
    $("promptHint").textContent  = hint;
    const input = $("promptInput");
    input.placeholder = placeholder;
    input.value = "";

    const overlay = $("promptOverlay");
    overlay.classList.add("open");
    setTimeout(() => input.focus(), 50);

    const cleanup = result => {
      _promptCleanup = null;
      overlay.classList.remove("open");
      $("promptOk").removeEventListener("click", onOk);
      $("promptCancel").removeEventListener("click", onCancel);
      input.removeEventListener("keydown", onKey);
      resolve(result);
    };

    const onOk     = () => cleanup(input.value.trim() || null);
    const onCancel = () => cleanup(null);
    const onKey    = e => {
      if (e.key === "Enter")  { e.preventDefault(); onOk(); }
      if (e.key === "Escape") { e.preventDefault(); onCancel(); }
    };

    _promptCleanup = cleanup;
    $("promptOk").addEventListener("click", onOk);
    $("promptCancel").addEventListener("click", onCancel);
    input.addEventListener("keydown", onKey);
  });
}

// ── Search shortcut hint ──────────────────────────────────────────────────

function initSearchHint() {
  const hint = $("searchShortcut");
  const si   = $("searchInput");
  if (!si || !hint) return;
  si.addEventListener("focus", () => { hint.style.display = "none"; });
  si.addEventListener("blur",  () => { if (!si.value) hint.style.display = ""; });
  si.addEventListener("input", () => { hint.style.display = si.value ? "none" : ""; });
}

// ── Utilities ──────────────────────────────────────────────────────────────

const $ = id => document.getElementById(id);

function esc(str) {
  return String(str ?? "")
    .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
    .replace(/"/g,"&quot;").replace(/'/g,"&#x27;");
}
function shortPath(p) { return (p||"").replace(/^\/Users\/[^/]+/,"~"); }
function statusLabel(s) {
  return { running:"● Running", stopped:"○ Stopped", conflict:"⚠ Conflict" }[s] ?? s;
}
// ── Styled confirm dialog ─────────────────────────────────────────────────

let _confirmCleanup = null;

function confirmAction({ title, message, confirmText, danger }) {
  return new Promise(resolve => {
    // Dismiss any previous dialog before opening a new one
    if (_confirmCleanup) { _confirmCleanup(false); }

    $("confirmTitle").textContent   = title || "Are you sure?";
    $("confirmMessage").textContent = message || "";
    const okBtn = $("confirmOk");
    okBtn.textContent = confirmText || "Confirm";
    okBtn.className   = danger ? "btn btn-danger" : "btn btn-primary";
    $("confirmOverlay").classList.add("open");
    okBtn.focus();

    function cleanup(result) {
      _confirmCleanup = null;
      $("confirmOverlay").classList.remove("open");
      okBtn.removeEventListener("click", onOk);
      $("confirmCancel").removeEventListener("click", onCancel);
      document.removeEventListener("keydown", onKey);
      resolve(result);
    }
    _confirmCleanup = cleanup;
    function onOk()     { cleanup(true); }
    function onCancel() { cleanup(false); }
    function onKey(e) {
      if (e.key === "Enter")  { e.preventDefault(); cleanup(true); }
      if (e.key === "Escape") { e.preventDefault(); cleanup(false); }
    }
    okBtn.addEventListener("click", onOk);
    $("confirmCancel").addEventListener("click", onCancel);
    document.addEventListener("keydown", onKey);
  });
}

function toast(msg, type="info") {
  const c = $("toastContainer");
  const t = document.createElement("div");
  t.className = `toast ${type}`; t.textContent = msg;
  c.appendChild(t); setTimeout(() => t.remove(), 3800);
}

// ── GitHub import ──────────────────────────────────────────────────────────

async function openGitHubImport() {
  try {
    const res  = await fetch("/api/github/status");
    const data = await res.json();
    if (data.configured) {
      runGitHubScan();
    } else {
      $("githubTokenOverlay").style.display = "flex";
      $("githubTokenInput").value = "";
      $("githubTokenError").textContent = "";
      setTimeout(() => $("githubTokenInput").focus(), 50);
    }
  } catch (e) {
    toast("Could not reach GitHub status endpoint.", "error");
  }
}

function closeGitHubTokenModal() {
  $("githubTokenOverlay").style.display = "none";
}

async function saveGitHubToken() {
  const token = $("githubTokenInput").value.trim();
  $("githubTokenError").textContent = "";
  if (!token) {
    $("githubTokenError").textContent = "Token is required.";
    return;
  }
  try {
    const res  = await fetch("/api/github/token", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
    });
    const data = await res.json();
    if (!data.ok) {
      $("githubTokenError").textContent = data.error || "Invalid token.";
      return;
    }
    closeGitHubTokenModal();
    runGitHubScan();
  } catch (e) {
    $("githubTokenError").textContent = "Network error — could not save token.";
  }
}

let _githubScanResults = [];

function closeGitHubImportModal() {
  $("githubImportOverlay").style.display = "none";
  _githubScanResults = [];
}

async function runGitHubScan() {
  $("githubImportOverlay").style.display = "flex";
  $("githubImportLoading").style.display = "block";
  $("githubImportTable").style.display   = "none";
  $("githubImportBtn").style.display     = "none";
  $("githubImportBanner").style.display  = "none";

  try {
    const res  = await fetch("/api/github/scan");
    const data = await res.json();
    if (!res.ok) {
      $("githubImportBanner").textContent    = data.error || "Scan failed.";
      $("githubImportBanner").style.display  = "block";
      $("githubImportLoading").style.display = "none";
      return;
    }
    _githubScanResults = data.map(r => ({
      ...r,
      _scraped: { port: r.port, start: r.start, notes: r.notes, tags: [...(r.tags||[])] },
    }));
    renderGitHubTable(_githubScanResults);
  } catch (e) {
    $("githubImportBanner").textContent    = "Network error: " + e.message;
    $("githubImportBanner").style.display  = "block";
    $("githubImportLoading").style.display = "none";
  }
}

function renderGitHubTable(rows) {
  $("githubImportLoading").style.display = "none";
  $("githubImportTable").style.display   = "table";
  const newRows = rows.filter(r => !r.registered);
  $("githubImportBtn").style.display = newRows.length ? "" : "none";

  $("githubImportRows").innerHTML = rows.map((r, i) => {
    const greyStyle = r.registered ? "opacity:0.4;pointer-events:none" : "";
    const amber     = v => !v ? "background:rgba(255,180,0,0.15)" : "";
    const checked   = (!r.registered && r.local_path) ? "checked" : "";
    const disabled  = r.registered ? "disabled" : "";
    return `<tr data-idx="${i}" style="${greyStyle};border-bottom:1px solid var(--border)">
      <td style="padding:6px 4px"><input type="checkbox" class="gh-check" data-idx="${i}" ${checked} ${disabled}></td>
      <td style="padding:6px 4px"><strong>${esc(r.name)}</strong>${r.is_fork ? ' <span style="font-size:11px;color:var(--text-muted)">(fork)</span>' : ""}</td>
      <td style="padding:6px 4px;${amber(r.local_path)}">
        <input class="form-control" style="font-size:12px;padding:2px 6px;width:200px" value="${esc(r.local_path||"")}" data-field="local_path" data-idx="${i}" ${disabled}>
      </td>
      <td style="padding:6px 4px;${amber(r.port)}">
        <input class="form-control" style="font-size:12px;padding:2px 6px;width:60px" value="${esc(r.port||"")}" data-field="port" data-idx="${i}" ${disabled}>
      </td>
      <td style="padding:6px 4px;${amber(r.start)}">
        <input class="form-control" style="font-size:12px;padding:2px 6px;width:180px" value="${esc(r.start||"")}" data-field="start" data-idx="${i}" ${disabled}>
      </td>
      <td style="padding:6px 4px">
        <input class="form-control" style="font-size:12px;padding:2px 6px;width:120px" value="${esc((r.tags||[]).join(", "))}" data-field="tags" data-idx="${i}" ${disabled}>
      </td>
      <td style="padding:6px 4px" id="gh-status-${i}">
        ${r.registered ? '<span style="font-size:11px;color:var(--text-muted)">Registered</span>' : '<span style="font-size:11px;color:var(--text-muted)">New</span>'}
      </td>
    </tr>`;
  }).join("");

  // Sync edits back to _githubScanResults
  $("githubImportRows").querySelectorAll("input[data-field]").forEach(inp => {
    inp.addEventListener("input", () => {
      const idx   = parseInt(inp.dataset.idx);
      const field = inp.dataset.field;
      _githubScanResults[idx][field] = inp.value;
    });
  });
}

function githubToggleAll(checked) {
  document.querySelectorAll(".gh-check:not(:disabled)").forEach(cb => cb.checked = checked);
}

async function importSelectedRepos() {
  const checked = [...document.querySelectorAll(".gh-check:checked:not(:disabled)")]
    .map(cb => parseInt(cb.dataset.idx));
  if (!checked.length) return;

  $("githubImportBtn").disabled = true;

  for (const idx of checked) {
    const r      = _githubScanResults[idx];
    const status = $(`gh-status-${idx}`);
    const tags   = typeof r.tags === "string"
      ? r.tags.split(",").map(t => t.trim()).filter(Boolean)
      : (r.tags || []);
    const port = parseInt(r.port);

    if (!r.local_path || !r.port || !r.start) {
      status.innerHTML = '<span style="color:var(--error,#e53935)">Missing fields</span>';
      continue;
    }
    if (isNaN(port)) {
      status.innerHTML = '<span style="color:var(--error,#e53935)">Invalid port</span>';
      continue;
    }

    status.innerHTML = "⏳";
    try {
      const res  = await fetch("/api/projects", {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name:      r.name,
          port:      port,
          directory: r.local_path,
          start:     r.start,
          tags,
          notes:     r.notes || "",
          source: r.full_name ? {
            type:      "github",
            full_name: r.full_name,
            scraped:   r._scraped || { port: r.port, start: r.start, notes: r.notes, tags: r.tags },
          } : undefined,
        }),
      });
      const data = await res.json();
      if (res.ok) {
        status.innerHTML = '<span style="color:#4caf50">✓ Imported</span>';
        _githubScanResults[idx].registered = true;
      } else {
        status.innerHTML = `<span style="color:var(--error,#e53935)">${esc(data.error || "Error")}</span>`;
      }
    } catch (e) {
      status.innerHTML = `<span style="color:var(--error,#e53935)">${esc(e.message)}</span>`;
    }
  }

  $("githubImportBtn").disabled = false;
  await refresh();
}

// ── Local project discovery ────────────────────────────────────────────────

let _discoverResults = [];

function openDiscover() {
  $("discoverOverlay").style.display = "flex";
}

function closeDiscover() {
  $("discoverOverlay").style.display = "none";
  _discoverResults = [];
  $("discoverTable").style.display   = "none";
  $("discoverHint").style.display    = "block";
  $("discoverLoading").style.display = "none";
  $("discoverImportBtn").style.display = "none";
  $("discoverBanner").style.display  = "none";
  $("discoverRows").innerHTML = "";
}

function setDiscoverDir(dir) {
  $("discoverDirInput").value = dir;
}

async function runLocalScan() {
  const dir = $("discoverDirInput").value.trim();
  if (!dir) return;

  $("discoverHint").style.display    = "none";
  $("discoverLoading").style.display = "block";
  $("discoverTable").style.display   = "none";
  $("discoverImportBtn").style.display = "none";
  $("discoverBanner").style.display  = "none";

  try {
    const res  = await fetch("/api/local-scan", {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ directory: dir }),
    });
    const data = await res.json();
    if (!res.ok) {
      $("discoverBanner").textContent   = data.error || "Scan failed.";
      $("discoverBanner").style.display = "block";
      $("discoverLoading").style.display = "none";
      return;
    }
    _discoverResults = data;
    renderDiscoverTable(data, dir);
  } catch (e) {
    $("discoverBanner").textContent   = "Network error: " + e.message;
    $("discoverBanner").style.display = "block";
    $("discoverLoading").style.display = "none";
  }
}

function renderDiscoverTable(rows, dir) {
  $("discoverLoading").style.display = "none";

  if (rows.length === 0) {
    $("discoverHint").textContent   = `No new projects found in ${esc(dir)}.`;
    $("discoverHint").style.display = "block";
    return;
  }

  $("discoverTable").style.display = "table";
  const newRows = rows.filter(r => !r.registered);
  $("discoverImportBtn").style.display = newRows.length ? "" : "none";

  $("discoverRows").innerHTML = rows.map((r, i) => {
    const greyStyle = r.registered ? "opacity:0.4;pointer-events:none" : "";
    const amber     = v => !v ? "background:rgba(255,180,0,0.15)" : "";
    const checked   = (!r.registered && r.port) ? "checked" : "";
    const disabled  = r.registered ? "disabled" : "";
    return `<tr data-idx="${i}" style="${greyStyle};border-bottom:1px solid var(--border)">
      <td style="padding:6px 4px"><input type="checkbox" class="disc-check" data-idx="${i}" ${checked} ${disabled}></td>
      <td style="padding:6px 4px"><strong>${esc(r.name)}</strong></td>
      <td style="padding:6px 4px;font-size:12px;color:var(--text-muted)">${esc(shortPath(r.directory))}</td>
      <td style="padding:6px 4px;${amber(r.port)}">
        <input class="form-control" style="font-size:12px;padding:2px 6px;width:60px"
               value="${esc(r.port||"")}" data-field="port" data-idx="${i}" ${disabled}>
      </td>
      <td style="padding:6px 4px;${amber(r.start)}">
        <input class="form-control" style="font-size:12px;padding:2px 6px;width:220px"
               value="${esc(r.start||"")}" data-field="start" data-idx="${i}" ${disabled}>
      </td>
      <td style="padding:6px 4px" id="disc-status-${i}">
        ${r.registered
          ? '<span style="font-size:11px;color:var(--text-muted)">Registered</span>'
          : '<span style="font-size:11px;color:var(--text-muted)">New</span>'}
      </td>
    </tr>`;
  }).join("");

  // Sync edits back to _discoverResults
  $("discoverRows").querySelectorAll("input[data-field]").forEach(inp => {
    inp.addEventListener("input", () => {
      _discoverResults[parseInt(inp.dataset.idx)][inp.dataset.field] = inp.value;
    });
  });
}

function discoverToggleAll(checked) {
  document.querySelectorAll(".disc-check:not(:disabled)").forEach(cb => cb.checked = checked);
}

async function importDiscovered() {
  const checked = [...document.querySelectorAll(".disc-check:checked:not(:disabled)")]
    .map(cb => parseInt(cb.dataset.idx));
  if (!checked.length) return;

  $("discoverImportBtn").disabled = true;

  for (const idx of checked) {
    const r      = _discoverResults[idx];
    const status = $(`disc-status-${idx}`);
    const port   = parseInt(r.port);

    if (!r.port || !r.start) {
      status.innerHTML = '<span style="color:var(--error,#e53935)">Missing fields</span>';
      continue;
    }
    if (isNaN(port)) {
      status.innerHTML = '<span style="color:var(--error,#e53935)">Invalid port</span>';
      continue;
    }

    status.innerHTML = "⏳";
    try {
      const res  = await fetch("/api/projects", {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name:      r.name,
          port:      port,
          directory: r.directory,
          start:     r.start,
          notes:     "",
          tags:      [],
        }),
      });
      const data = await res.json();
      if (res.ok) {
        status.innerHTML = '<span style="color:#4caf50">✓ Imported</span>';
        _discoverResults[idx].registered = true;
        const cb = document.querySelector(`.disc-check[data-idx="${idx}"]`);
        if (cb) cb.disabled = true;
      } else {
        status.innerHTML = `<span style="color:var(--error,#e53935)">${esc(data.error || "Error")}</span>`;
      }
    } catch (e) {
      status.innerHTML = `<span style="color:var(--error,#e53935)">${esc(e.message)}</span>`;
    }
  }

  $("discoverImportBtn").disabled = false;
  await refresh();
}
