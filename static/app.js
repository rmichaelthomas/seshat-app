// ── State ──────────────────────────────────────────────────────────────────

let projects     = [];
let orphans      = [];
let groups       = [];
let activeFilter = "all";
let selectedName = null;
let activeView   = "projects";   // "projects" | "vault" | "organize"
let routerStatus = null;   // result of GET /api/router/status
let hostnames = [];   // [{project_name, hostname, port}] from /api/router/hostnames

// ── Boot ───────────────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
  initFilters();
  initProjectModal();
  initGroupModal();
  initVaultKeyModal();
  $("vaultBtn").addEventListener("click", toggleVaultView);
  $("organizeBtn").addEventListener("click", toggleOrganizeView);
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

    if (activeView === "projects") {
      render();
      if (selectedName) updateDetailPanel(selectedName);
    } else {
      renderGroups();   // keep sidebar counts fresh
      renderCounts();
    }
  } catch (_) { /* server may be restarting */ }
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

function showProjectView() {
  activeView = "projects";
  $("projectView").style.display  = "block";
  $("vaultView").style.display    = "none";
  $("organizeView").style.display = "none";
  $("vaultBtn").classList.remove("active");
  $("organizeBtn").classList.remove("active");
  $("addProjectBtn").style.display = "";
  closeDetail();
  render();
}

async function installVaultDeps() {
  const badge = document.querySelector(".vault-enc-badge.warn");
  const btn   = badge && badge.querySelector("button");
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
    if (badge) badge.innerHTML =
      `<span style="color:var(--yellow)">✓ Installed — restart Seshat to activate encryption</span>`;
  } catch (e) {
    toast("Install failed: " + e.message, "error");
    if (btn) { btn.disabled = false; btn.textContent = "Fix: Install deps"; }
  }
}

async function showVaultView() {
  activeView = "vault";
  $("projectView").style.display  = "none";
  $("vaultView").style.display    = "block";
  $("organizeView").style.display = "none";
  $("vaultBtn").classList.add("active");
  $("organizeBtn").classList.remove("active");
  $("addProjectBtn").style.display = "none";
  closeDetail();
  await renderVaultView();
}

async function showOrganizeView() {
  activeView = "organize";
  $("projectView").style.display  = "none";
  $("vaultView").style.display    = "none";
  $("organizeView").style.display = "block";
  $("organizeBtn").classList.add("active");
  $("vaultBtn").classList.remove("active");
  $("addProjectBtn").style.display = "none";
  closeDetail();
  await Promise.all([loadFolderMap(), loadRecommendations(), loadMoveHistory()]);
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
    el.textContent = "⏳";
    if (body) body.style.display = "none";
    return;
  }
  el.textContent = ok ? "✅" : "❌";
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
  const visible = (activeFilter === "all" || activeFilter === "orphans")
    ? projects
    : projects.filter(p => p.status === activeFilter);

  if (visible.length === 0) {
    const label = { running: "running", stopped: "stopped", conflict: "in conflict" }[activeFilter] ?? activeFilter;
    shelf.innerHTML = `<div class="empty-state"><div class="empty-state-title">No ${label} projects</div></div>`;
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
  const ssCls  = isRunning ? "stop-btn" : "start-btn";
  const ssIcon = isRunning ? "■" : "▶";
  return `
    <div class="project-row ${p.status}" data-name="${esc(p.name)}">
      <div><div class="status-light ${lightClass}"></div></div>
      <div>
        <div class="project-name">${esc(p.name)}</div>
        ${tags ? `<div class="project-tags">${tags}</div>` : ""}
        ${conflictLine}${errorLine}
      </div>
      <div class="project-port">:${p.port}</div>
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
        <button class="action-btn stop-btn" onclick="stopOrphan(${o.port})" title="Stop process">■</button>
      </div>
    </div>`).join("");
}

function renderGroups() {
  const list = $("groupList");
  if (!list) return;
  if (groups.length === 0) {
    list.innerHTML = `<div style="padding:4px 10px;font-size:11px;color:var(--text-muted)">No groups yet</div>`;
    return;
  }
  list.innerHTML = groups.map(g => {
    const count = (g.projects || []).length;
    return `
      <div class="group-item" title="${esc((g.projects||[]).join(', '))}">
        <span class="group-name">${esc(g.name)}</span>
        <span style="font-size:10px;color:var(--text-muted);margin-right:4px">${count}</span>
        <div class="group-actions">
          <button class="group-btn start"  onclick="startGroup('${esc(g.name)}')"  title="Start all">▶</button>
          <button class="group-btn stop"   onclick="stopGroup('${esc(g.name)}')"   title="Stop all">■</button>
          <button class="group-btn delete" onclick="deleteGroup('${esc(g.name)}')" title="Remove group">✕</button>
        </div>
      </div>`;
  }).join("");
}

// ── Detail panel ───────────────────────────────────────────────────────────

function selectProject(name) {
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

  const conflictBlock = isConflict ? `
    <div class="conflict-message">
      ⚠ Port ${p.port} is in use by <code>${esc(p.process_name||"unknown")}</code>
      (PID ${p.pid||"?"})${p.process_cmd ? ` — ${esc(p.process_cmd.slice(0,80))}` : ""}.
      Stop that process or reassign this project's port.
    </div>` : "";
  const errorBlock = hasError ? renderErrorBlock(p.recent_error) : "";
  const tagsBlock  = (p.tags&&p.tags.length) ? `
    <div class="detail-field"><div class="detail-label">Tags</div>
    <div class="detail-value">${p.tags.map(t=>`<span class="tag">${esc(t)}</span>`).join(" ")}</div></div>` : "";
  const notesBlock = p.notes ? `
    <div class="detail-field"><div class="detail-label">Notes</div>
    <div class="detail-value notes">${esc(p.notes)}</div></div>` : "";
  const pidBlock = (p.pid&&isRunning) ? `
    <div class="detail-field"><div class="detail-label">PID</div>
    <div class="detail-value mono">${p.pid}</div></div>` : "";

  const depsBlock = renderDependencies(p.dependencies || [], p.dep_status || [], p.name);

  const safeN = p.name.replace(/\\/g,"\\\\").replace(/'/g,"\\'");
  const safeD = (p.directory||"").replace(/\\/g,"\\\\").replace(/'/g,"\\'");
  const urlVal = esc(p.url||`http://localhost:${p.port}`);

  $("detailInner").innerHTML = `
    <div class="detail-close-row"><button class="icon-btn" onclick="closeDetail()">✕</button></div>
    <div class="detail-name">${esc(p.name)}</div>
    <div class="detail-url">localhost:${p.port}</div>
    <div class="detail-status ${statusCls}">${statusTxt}</div>
    ${_hostnameDetailFieldHTML(p.name)}
    ${conflictBlock}${errorBlock}
    <div class="detail-actions">
      ${isRunning
        ? `<button class="detail-btn stop"  onclick="stopProject('${safeN}')">■ Stop</button>`
        : `<button class="detail-btn start" onclick="startProject('${safeN}')">▶ Start</button>`}
      <button class="detail-btn" onclick="window.open('${urlVal}','_blank')">↗ Open in Browser</button>
      <button class="detail-btn" onclick="apiOpen('${safeD}','finder')">📁 Open in Finder</button>
      <button class="detail-btn" onclick="apiOpen('${safeD}','terminal')">⌘ Open in Terminal</button>
    </div>
    <div class="detail-section">
      <div class="detail-section-title">Configuration</div>
      <div class="detail-field"><div class="detail-label">Directory</div>
        <div class="detail-value mono">${esc(p.directory)}</div></div>
      <div class="detail-field"><div class="detail-label">Start Command</div>
        <div class="detail-value mono">${esc(p.start)}</div></div>
      ${tagsBlock}${notesBlock}${pidBlock}
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
    <div class="detail-section">
      <div class="detail-section-title">Danger Zone</div>
      <button class="detail-btn danger" onclick="removeProject('${safeN}')">Remove from Registry</button>
    </div>`;
  if (prevLog && prevLog !== '<div class="log-empty">Loading\u2026</div>') $("logViewer").innerHTML = prevLog;
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

function closeDetail() {
  selectedName = null;
  document.querySelectorAll(".project-row").forEach(r => r.classList.remove("selected"));
  $("detailPanel").classList.remove("open");
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

    const encBadge = summary.encrypted
      ? `<span class="vault-enc-badge">🔒 Encrypted · Keychain</span>`
      : `<span class="vault-enc-badge warn">⚠ Unencrypted &nbsp;<button class="btn btn-sm" style="font-size:11px;padding:2px 8px;vertical-align:middle" onclick="installVaultDeps()">Fix: Install deps</button></span>`;

    const missingAudit = audit.filter(a => a.missing_from.length > 0);
    const unusedAudit  = audit.filter(a => a.unused);

    $("vaultContent").innerHTML = `
      <div class="vault-view">

        <div class="vault-view-header">
          <div>
            <div class="vault-view-title">⚿ Vault ${encBadge}</div>
            <div class="vault-view-meta">${summary.key_count} shared key${summary.key_count !== 1 ? "s" : ""}</div>
          </div>
          <button class="btn btn-ghost btn-sm" onclick="showProjectView()">← Projects</button>
        </div>

        <!-- Shared keys -->
        <div class="vault-section">
          <div class="vault-section-header">
            <div class="vault-section-title">Shared Keys</div>
            <button class="btn btn-ghost btn-sm" onclick="openVaultKeyModal('shared')">+ Add Key</button>
          </div>
          <div id="sharedKeysList">${renderSharedKeyRows(summary.keys, audit)}</div>
        </div>

        <!-- Per-project overrides -->
        <div class="vault-section">
          <div class="vault-section-header">
            <div class="vault-section-title">Per-Project Overrides</div>
          </div>
          <div id="overridesList">${renderOverrideGroups(summary.project_overrides, audit)}</div>
        </div>

        <!-- Audit: missing keys -->
        ${missingAudit.length || unusedAudit.length ? `
        <div class="vault-section">
          <div class="vault-section-header">
            <div class="vault-section-title">Audit</div>
          </div>
          <div>${renderAuditRows(missingAudit, unusedAudit)}</div>
        </div>` : ""}

        <!-- Import from .env -->
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

function renderSharedKeyRows(keys, audit) {
  if (!keys || keys.length === 0) {
    return `<div class="vault-empty">No shared keys yet. Add your first key above.</div>`;
  }
  return keys.map(key => {
    const a = audit.find(x => x.key === key) || {};
    const usage = a.declared_by && a.declared_by.length
      ? a.declared_by.join(", ")
      : `<span style="color:var(--text-muted)">unused</span>`;
    return `
      <div class="vault-key-row" data-key="${esc(key)}">
        <div>
          <div class="vault-key-name">${esc(key)}</div>
          <div class="vault-key-usage">${usage}</div>
        </div>
        <div class="vault-key-value" id="keyval-${esc(key)}">••••••••</div>
        <div class="vault-row-actions">
          <button class="vault-row-btn reveal-key-btn" data-key="${esc(key)}" title="Reveal">👁</button>
          <button class="vault-row-btn edit-key-btn"   data-key="${esc(key)}" title="Edit">✎</button>
          <button class="vault-row-btn delete delete-key-btn" data-key="${esc(key)}" title="Delete">✕</button>
        </div>
      </div>`;
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
          <button class="vault-row-btn" onclick="openVaultKeyModal('override','${esc(proj)}')" title="Add override for this project" style="font-size:12px">+ Add</button>
        </div>
        ${rows}
      </div>`;
  }).join("");
}

function renderAuditRows(missing, unused) {
  const rows = [];
  missing.forEach(a => {
    rows.push(`
      <div class="audit-row">
        <div class="audit-key">${esc(a.key)}</div>
        <div class="audit-status warn">⚠ Missing for: ${esc(a.missing_from.join(", "))}</div>
      </div>`);
  });
  unused.forEach(a => {
    rows.push(`
      <div class="audit-row">
        <div class="audit-key">${esc(a.key)}</div>
        <div class="audit-status unused">No project declares this key</div>
      </div>`);
  });
  return rows.join("");
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
  // Reveal shared key
  document.querySelectorAll(".reveal-key-btn").forEach(btn => {
    btn.addEventListener("click", async () => {
      const key = btn.dataset.key;
      const el  = $(`keyval-${key}`);
      if (el.classList.contains("revealed")) {
        el.textContent = "••••••••"; el.classList.remove("revealed");
        el.closest(".vault-key-row").querySelector(".vault-hostname-hint")?.remove();
        return;
      }
      try {
        const res = await fetch(`/api/vault/keys/${encodeURIComponent(key)}`);
        const d   = await res.json();
        el.textContent = d.value; el.classList.add("revealed");
        const hint = buildLocalhostHint(d.value, key, null);
        if (hint) {
          el.closest(".vault-key-row")
            .querySelector(".vault-row-actions")
            .insertAdjacentHTML("beforebegin", hint);
        }
      } catch (_) { toast("Could not reveal key", "error"); }
    });
  });

  // Edit shared key
  document.querySelectorAll(".edit-key-btn").forEach(btn => {
    btn.addEventListener("click", () => openVaultKeyModal("shared", null, btn.dataset.key));
  });

  // Delete shared key
  document.querySelectorAll(".delete-key-btn").forEach(btn => {
    btn.addEventListener("click", async () => {
      const key = btn.dataset.key;
      if (!confirm(`Delete "${key}" from the vault?\n\nProjects that depend on it will lose access.`)) return;
      await fetch(`/api/vault/keys/${encodeURIComponent(key)}`, { method: "DELETE" });
      toast(`${key} deleted`, "success");
      await renderVaultView();
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

  // Edit override
  document.querySelectorAll(".edit-ov-btn").forEach(btn => {
    btn.addEventListener("click", () => openVaultKeyModal("override", btn.dataset.proj, btn.dataset.key));
  });

  // Delete override
  document.querySelectorAll(".delete-ov-btn").forEach(btn => {
    btn.addEventListener("click", async () => {
      const { proj, key } = btn.dataset;
      if (!confirm(`Remove override for "${key}" from "${proj}"?`)) return;
      await fetch(`/api/vault/overrides/${encodeURIComponent(proj)}/${encodeURIComponent(key)}`, { method: "DELETE" });
      toast(`Override removed`, "success");
      await renderVaultView();
    });
  });
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
  try {
    const res  = await fetch(`/api/projects/${encodeURIComponent(name)}/start`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error);
    toast(`${name} started`, "success");
    await refresh();
    if (selectedName === name) loadLogs(name);
  } catch (e) { toast(e.message, "error"); }
}

async function stopProject(name) {
  try {
    const res  = await fetch(`/api/projects/${encodeURIComponent(name)}/stop`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error);
    toast(`${name} stopped`, "success");
    await refresh();
  } catch (e) { toast(e.message, "error"); }
}

async function removeProject(name) {
  if (!confirm(`Remove "${name}" from the registry?\n\nThis will not delete any files.`)) return;
  try {
    const res  = await fetch(`/api/projects/${encodeURIComponent(name)}`, { method: "DELETE" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error);
    toast(`${name} removed`, "success");
    closeDetail();
    await refresh();
  } catch (e) { toast(e.message, "error"); }
}

async function stopOrphan(port) {
  if (!confirm(`Stop the process on port ${port}?`)) return;
  try {
    const res  = await fetch(`/api/orphans/${port}/stop`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error);
    toast(`Process on :${port} stopped`, "success");
    await refresh();
  } catch (e) { toast(e.message, "error"); }
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
  if (!confirm(`Remove group "${name}"?\n\nProjects will not be affected.`)) return;
  try {
    const res  = await fetch(`/api/groups/${encodeURIComponent(name)}`, { method: "DELETE" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error);
    toast(`Group "${name}" removed`, "success");
    await refresh();
  } catch (e) { toast(e.message, "error"); }
}

// ── Filters ────────────────────────────────────────────────────────────────

function initFilters() {
  document.querySelectorAll(".filter-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      activeFilter = btn.dataset.filter;
      document.querySelectorAll(".filter-btn").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      if (activeView !== "projects") showProjectView();
      else render();
    });
  });
}

// ── Project modal ──────────────────────────────────────────────────────────

function initProjectModal() {
  $("addProjectBtn").addEventListener("click", openProjectModal);
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

function openProjectModal() {
  $("modalOverlay").classList.add("open");
  setTimeout(() => $("addProjectForm").querySelector("[name='name']").focus(), 60);
}
function closeProjectModal() {
  $("modalOverlay").classList.remove("open");
  $("addProjectForm").reset();
  $("formError").textContent = "";
}

// ── Group modal ────────────────────────────────────────────────────────────

function initGroupModal() {
  $("addGroupBtn").addEventListener("click",    openGroupModal);
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
    if (!confirm(
      `"${projectName}" is currently running. Moving it won't affect the running process, ` +
      `but the next start will use the new location.\n\nContinue?`
    )) return;
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
    if (!confirm(
      `${runningNames.length} project${runningNames.length > 1 ? "s are" : " is"} currently running: ` +
      `${runningNames.join(", ")}.\n\n` +
      `Moving them won't affect running processes, but the next start will use the new locations.\n\nContinue?`
    )) return;
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
  if (!confirm("Roll back this move? The folder will be moved to its original location and the registry will be updated.")) return;
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
    _githubScanResults = data;
    renderGitHubTable(data);
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
