// ── State ──────────────────────────────────────────────────────────────────

let projects     = [];
let orphans      = [];
let groups       = [];
let activeFilter = "all";
let selectedName = null;

// ── Boot ───────────────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
  initFilters();
  initProjectModal();
  initGroupModal();
  refresh();
  setInterval(refresh, 5000);
});

// ── Data ───────────────────────────────────────────────────────────────────

async function refresh() {
  try {
    const [projRes, orphanRes, groupRes] = await Promise.all([
      fetch("/api/projects"),
      fetch("/api/orphans"),
      fetch("/api/groups"),
    ]);
    projects = await projRes.json();
    orphans  = await orphanRes.json();
    groups   = await groupRes.json();
    render();
    if (selectedName) updateDetailPanel(selectedName);
  } catch (_) {
    // Server may be restarting — fail silently
  }
}

// ── Render ─────────────────────────────────────────────────────────────────

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
    shelf.querySelector(`[data-name="${CSS.escape(selectedName)}"]`)
      ?.classList.add("selected");
  }
}

function projectRowHTML(p) {
  const isRunning  = p.status === "running";
  const hasError   = isRunning && p.has_error && p.recent_error;
  const lightClass = hasError ? "error" : p.status;

  const tags = (p.tags || []).slice(0, 3).map(t => `<span class="tag">${esc(t)}</span>`).join("");

  const conflictLine = p.status === "conflict" && p.process_name
    ? `<div class="conflict-inline">⚠ Port in use by <code>${esc(p.process_name)}</code> (PID ${p.pid})</div>`
    : "";

  const errorLine = hasError && p.recent_error
    ? `<div class="error-preview">⚠ ${esc(p.recent_error.short || p.recent_error.message.slice(0, 60))}</div>`
    : "";

  const ssIcon  = isRunning ? "■" : "▶";
  const ssCls   = isRunning ? "stop-btn" : "start-btn";
  const ssTitle = isRunning ? "Stop" : "Start";

  return `
    <div class="project-row ${p.status}" data-name="${esc(p.name)}">
      <div><div class="status-light ${lightClass}"></div></div>
      <div>
        <div class="project-name">${esc(p.name)}</div>
        ${tags ? `<div class="project-tags">${tags}</div>` : ""}
        ${conflictLine}
        ${errorLine}
      </div>
      <div class="project-port">:${p.port}</div>
      <div class="project-dir">${esc(shortPath(p.directory))}</div>
      <div class="project-actions">
        <button class="action-btn start-stop-btn ${ssCls}" title="${ssTitle}">${ssIcon}</button>
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

    row.addEventListener("click", e => {
      if (e.target.closest(".action-btn")) return;
      selectProject(name);
    });

    row.querySelector(".start-stop-btn").addEventListener("click", e => {
      e.stopPropagation();
      p.status === "running" ? stopProject(name) : startProject(name);
    });
    row.querySelector(".open-browser-btn").addEventListener("click", e => {
      e.stopPropagation();
      window.open(p.url || `http://localhost:${p.port}`, "_blank");
    });
    row.querySelector(".open-finder-btn").addEventListener("click", e => {
      e.stopPropagation();
      apiOpen(p.directory, "finder");
    });
    row.querySelector(".open-term-btn").addEventListener("click", e => {
      e.stopPropagation();
      apiOpen(p.directory, "terminal");
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
      <div class="group-item" title="${esc((g.projects || []).join(', '))}">
        <span class="group-name">${esc(g.name)}</span>
        <span style="font-size:10px;color:var(--text-muted);margin-right:4px">${count}</span>
        <div class="group-actions">
          <button class="group-btn start" onclick="startGroup('${esc(g.name)}')" title="Start all">▶</button>
          <button class="group-btn stop"  onclick="stopGroup('${esc(g.name)}')"  title="Stop all">■</button>
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
  loadLogs(name);   // kick off log fetch
}

function updateDetailPanel(name) {
  const p = projects.find(x => x.name === name);
  if (!p) return;

  const isRunning  = p.status === "running";
  const isConflict = p.status === "conflict";
  const hasError   = isRunning && p.has_error && p.recent_error;

  // Status badge
  const statusClass  = hasError ? "error" : p.status;
  const statusText   = hasError ? "⚠ Running with errors" : statusLabel(p.status);

  // Conflict block
  const conflictBlock = isConflict ? `
    <div class="conflict-message">
      ⚠ Port ${p.port} is in use by <code>${esc(p.process_name || "unknown")}</code>
      (PID ${p.pid || "?"})${p.process_cmd ? ` — ${esc(p.process_cmd.slice(0, 80))}` : ""}.
      Stop that process or reassign this project's port.
    </div>` : "";

  // Error block
  const errorBlock = hasError ? renderErrorBlock(p.recent_error, p.directory) : "";

  // Tags, notes, PID
  const tagsBlock = (p.tags && p.tags.length) ? `
    <div class="detail-field">
      <div class="detail-label">Tags</div>
      <div class="detail-value">${p.tags.map(t => `<span class="tag">${esc(t)}</span>`).join(" ")}</div>
    </div>` : "";

  const notesBlock = p.notes ? `
    <div class="detail-field">
      <div class="detail-label">Notes</div>
      <div class="detail-value notes">${esc(p.notes)}</div>
    </div>` : "";

  const pidBlock = (p.pid && isRunning) ? `
    <div class="detail-field">
      <div class="detail-label">PID</div>
      <div class="detail-value mono">${p.pid}</div>
    </div>` : "";

  // Dependencies block
  const depsBlock = renderDependencies(p.dependencies || []);

  // Attr-safe name / dir for onclick handlers
  const safeN = p.name.replace(/\\/g, "\\\\").replace(/'/g, "\\'");
  const safeD = (p.directory || "").replace(/\\/g, "\\\\").replace(/'/g, "\\'");
  const urlVal = esc(p.url || `http://localhost:${p.port}`);

  $("detailInner").innerHTML = `
    <div class="detail-close-row">
      <button class="icon-btn" onclick="closeDetail()">✕</button>
    </div>
    <div class="detail-name">${esc(p.name)}</div>
    <div class="detail-url">localhost:${p.port}</div>
    <div class="detail-status ${statusClass}">${statusText}</div>

    ${conflictBlock}
    ${errorBlock}

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
      <div class="detail-field">
        <div class="detail-label">Directory</div>
        <div class="detail-value mono">${esc(p.directory)}</div>
      </div>
      <div class="detail-field">
        <div class="detail-label">Start Command</div>
        <div class="detail-value mono">${esc(p.start)}</div>
      </div>
      ${tagsBlock}${notesBlock}${pidBlock}
    </div>

    ${depsBlock}

    <div class="detail-section" id="logSection">
      <div class="detail-section-header">
        <div class="detail-section-title" style="margin:0;border:none;padding:0">Output Log</div>
        <button class="detail-section-action" onclick="loadLogs('${safeN}')">↺ Refresh</button>
      </div>
      <div class="log-viewer" id="logViewer">
        <div class="log-empty">Loading…</div>
      </div>
    </div>

    <div class="detail-section">
      <div class="detail-section-title">Danger Zone</div>
      <button class="detail-btn danger" onclick="removeProject('${safeN}')">Remove from Registry</button>
    </div>
  `;
}

function renderErrorBlock(err, projectDir) {
  if (!err) return "";
  const safePath = (err.file_ref?.path || "").replace(/\\/g, "\\\\").replace(/'/g, "\\'");
  const openFileBtn = err.file_ref ? `
    <button class="error-action-btn" onclick="apiOpen('${esc(safePath)}','editor')">Open File</button>` : "";
  const locationText = err.short ? `📍 ${esc(err.short)}` : "";
  return `
    <div class="error-block">
      <div class="error-block-message">${esc(err.message)}</div>
      ${locationText ? `<div class="error-block-location">${locationText}</div>` : ""}
      <div class="error-block-actions">
        <button class="error-action-btn" onclick="copyError(this)">Copy</button>
        ${openFileBtn}
      </div>
    </div>`;
}

function renderDependencies(deps) {
  if (!deps || deps.length === 0) return "";
  const icons = { tunnel: "🔗", database: "🗄️", api: "⚡", hosting: "🌐" };
  const items = deps.map(d => `
    <div class="dep-item">
      <span class="dep-icon">${icons[d.type] || "○"}</span>
      <div class="dep-info">
        <div class="dep-label">${esc(d.label || d.provider)}</div>
        <div class="dep-provider">${esc(d.provider)} · ${esc(d.type)}</div>
      </div>
      <span class="dep-status">unknown</span>
    </div>`).join("");
  return `
    <div class="detail-section">
      <div class="detail-section-title">Dependencies</div>
      <div class="dep-list">${items}</div>
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

    if (!data.lines || data.lines.length === 0) {
      viewer.innerHTML = `<div class="log-empty">No log output yet. Start the project to see output here.</div>`;
      return;
    }

    viewer.innerHTML = data.lines.map(line => {
      let cls = "";
      if (SEP_LINE_RE.test(line))     cls = "is-sep";
      else if (ERROR_LINE_RE.test(line))   cls = "is-error";
      else if (WARNING_LINE_RE.test(line)) cls = "is-warning";
      return `<div class="log-line ${cls}">${esc(line)}</div>`;
    }).join("");

    // Scroll to bottom
    viewer.scrollTop = viewer.scrollHeight;
  } catch (e) {
    viewer.innerHTML = `<div class="log-empty">Could not load logs.</div>`;
  }
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
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ path, mode }),
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
    const started = (data.results || []).filter(r => r.status === "started").length;
    const skipped = (data.results || []).filter(r => r.status === "already_running").length;
    const failed  = (data.results || []).filter(r => r.error).length;
    let msg = `${name}: ${started} started`;
    if (skipped) msg += `, ${skipped} already running`;
    if (failed)  msg += `, ${failed} failed`;
    toast(msg, failed ? "error" : "success");
    await refresh();
  } catch (e) { toast(e.message, "error"); }
}

async function stopGroup(name) {
  try {
    const res  = await fetch(`/api/groups/${encodeURIComponent(name)}/stop`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error);
    const stopped = (data.results || []).filter(r => r.status === "stopped").length;
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
      render();
    });
  });
}

// ── Project modal ──────────────────────────────────────────────────────────

function initProjectModal() {
  $("addProjectBtn").addEventListener("click", openProjectModal);
  $("modalClose").addEventListener("click",   closeProjectModal);
  $("cancelBtn").addEventListener("click",    closeProjectModal);
  $("modalOverlay").addEventListener("click", e => {
    if (e.target === $("modalOverlay")) closeProjectModal();
  });

  $("addProjectForm").querySelector("[name='port']").addEventListener("input", e => {
    const urlField = $("addProjectForm").querySelector("[name='url']");
    if (!urlField.value) urlField.placeholder = `http://localhost:${e.target.value}`;
  });

  $("addProjectForm").addEventListener("submit", async e => {
    e.preventDefault();
    const fd   = new FormData(e.target);
    const port = parseInt(fd.get("port"), 10);
    const tags = fd.get("tags").split(",").map(t => t.trim()).filter(Boolean);

    const payload = {
      name:      fd.get("name").trim(),
      port,
      directory: fd.get("directory").trim(),
      start:     fd.get("start").trim(),
      url:       fd.get("url").trim() || `http://localhost:${port}`,
      tags,
      notes:     fd.get("notes").trim(),
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
      closeProjectModal();
      await refresh();
      selectProject(payload.name);
    } catch (err) {
      $("formError").textContent = err.message;
    }
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
  $("groupModalOverlay").addEventListener("click", e => {
    if (e.target === $("groupModalOverlay")) closeGroupModal();
  });

  $("addGroupForm").addEventListener("submit", async e => {
    e.preventDefault();
    const fd       = new FormData(e.target);
    const name     = fd.get("groupName").trim();
    const selected = [...document.querySelectorAll("#groupProjectCheckboxes input:checked")]
      .map(cb => cb.value);

    $("groupFormError").textContent = "";
    try {
      const res  = await fetch("/api/groups", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, projects: selected }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error);
      toast(`Group "${name}" created`, "success");
      closeGroupModal();
      await refresh();
    } catch (err) {
      $("groupFormError").textContent = err.message;
    }
  });
}

function openGroupModal() {
  // Populate checkboxes with current projects
  const box = $("groupProjectCheckboxes");
  if (projects.length === 0) {
    box.innerHTML = `<div style="color:var(--text-muted);font-size:12px;padding:4px">No projects registered yet.</div>`;
  } else {
    box.innerHTML = projects.map(p => `
      <label class="checkbox-item">
        <input type="checkbox" value="${esc(p.name)}">
        <span>${esc(p.name)}</span>
        <span class="check-port">:${p.port}</span>
      </label>`).join("");
  }
  $("groupModalOverlay").classList.add("open");
  setTimeout(() => $("addGroupForm").querySelector("[name='groupName']").focus(), 60);
}

function closeGroupModal() {
  $("groupModalOverlay").classList.remove("open");
  $("addGroupForm").reset();
  $("groupFormError").textContent = "";
}

// ── Utilities ──────────────────────────────────────────────────────────────

const $ = id => document.getElementById(id);

function esc(str) {
  return String(str ?? "")
    .replace(/&/g,  "&amp;")
    .replace(/</g,  "&lt;")
    .replace(/>/g,  "&gt;")
    .replace(/"/g,  "&quot;")
    .replace(/'/g,  "&#x27;");
}

function shortPath(p) {
  return (p || "").replace(/^\/Users\/[^/]+/, "~");
}

function statusLabel(s) {
  return { running: "● Running", stopped: "○ Stopped", conflict: "⚠ Conflict" }[s] ?? s;
}

function toast(msg, type = "info") {
  const c = $("toastContainer");
  const t = document.createElement("div");
  t.className   = `toast ${type}`;
  t.textContent = msg;
  c.appendChild(t);
  setTimeout(() => t.remove(), 3800);
}
