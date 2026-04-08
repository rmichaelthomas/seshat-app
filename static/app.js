// ── State ──────────────────────────────────────────────────────────────────

let projects     = [];
let orphans      = [];
let activeFilter = "all";
let selectedName = null;

// ── Boot ───────────────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
  initFilters();
  initModal();
  refresh();
  setInterval(refresh, 5000);
});

// ── Data ───────────────────────────────────────────────────────────────────

async function refresh() {
  try {
    const [projRes, orphanRes] = await Promise.all([
      fetch("/api/projects"),
      fetch("/api/orphans"),
    ]);
    projects = await projRes.json();
    orphans  = await orphanRes.json();
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

  const visible = activeFilter === "all" || activeFilter === "orphans"
    ? projects
    : projects.filter(p => p.status === activeFilter);

  if (visible.length === 0) {
    const label = { running: "running", stopped: "stopped", conflict: "in conflict" }[activeFilter] ?? activeFilter;
    shelf.innerHTML = `
      <div class="empty-state">
        <div class="empty-state-title">No ${label} projects</div>
      </div>`;
    return;
  }

  shelf.innerHTML = visible.map(projectRowHTML).join("");
  attachRowEvents(shelf);

  // Re-apply selected highlight
  if (selectedName) {
    shelf.querySelector(`[data-name="${CSS.escape(selectedName)}"]`)
      ?.classList.add("selected");
  }
}

function projectRowHTML(p) {
  const isRunning  = p.status === "running";
  const tags = (p.tags || []).slice(0, 3).map(t => `<span class="tag">${esc(t)}</span>`).join("");

  const conflictLine = p.status === "conflict" && p.process_name
    ? `<div class="conflict-inline">⚠ Port in use by <code>${esc(p.process_name)}</code> (PID ${p.pid})</div>`
    : "";

  const ssIcon  = isRunning ? "■" : "▶";
  const ssTitle = isRunning ? "Stop" : "Start";
  const ssCls   = isRunning ? "stop-btn" : "start-btn";

  return `
    <div class="project-row ${p.status}" data-name="${esc(p.name)}">
      <div><div class="status-light ${p.status}"></div></div>
      <div class="project-name-cell">
        <div class="project-name">${esc(p.name)}</div>
        ${tags ? `<div class="project-tags">${tags}</div>` : ""}
        ${conflictLine}
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

  if (!show || orphans.length === 0) {
    section.style.display = "none";
    return;
  }

  section.style.display = "block";
  list.innerHTML = orphans.map(o => `
    <div class="orphan-row">
      <div class="orphan-port">:${o.port}</div>
      <div class="orphan-info">
        <div class="orphan-name">
          ${esc(o.name)}
          <span class="orphan-pid">(PID ${o.pid})</span>
        </div>
        <div class="orphan-cmd">${esc(o.cmdline || "—")}</div>
      </div>
      <div style="display:flex;gap:4px;flex-shrink:0">
        <button class="action-btn stop-btn" onclick="stopOrphan(${o.port})" title="Stop process">■</button>
      </div>
    </div>`).join("");
}

// ── Detail panel ───────────────────────────────────────────────────────────

function selectProject(name) {
  selectedName = name;

  document.querySelectorAll(".project-row").forEach(r =>
    r.classList.toggle("selected", r.dataset.name === name)
  );

  updateDetailPanel(name);
  $("detailPanel").classList.add("open");
}

function updateDetailPanel(name) {
  const p = projects.find(x => x.name === name);
  if (!p) return;

  const isRunning  = p.status === "running";
  const isConflict = p.status === "conflict";

  const conflictBlock = isConflict ? `
    <div class="conflict-message">
      ⚠ Port ${p.port} is in use by <code>${esc(p.process_name || "unknown")}</code>
      (PID ${p.pid || "?"})${p.process_cmd ? ` — ${esc(p.process_cmd.slice(0, 80))}` : ""}.
      Stop that process or reassign this project's port.
    </div>` : "";

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

  const nameAttr = esc(p.name).replace(/'/g, "\\'");
  const dirAttr  = esc(p.directory).replace(/'/g, "\\'");
  const urlVal   = esc(p.url || `http://localhost:${p.port}`);

  $("detailInner").innerHTML = `
    <div class="detail-close-row">
      <button class="icon-btn" onclick="closeDetail()">✕</button>
    </div>

    <div class="detail-name">${esc(p.name)}</div>
    <div class="detail-url">localhost:${p.port}</div>

    <div class="detail-status ${p.status}">${statusLabel(p.status)}</div>

    ${conflictBlock}

    <div class="detail-actions">
      ${isRunning
        ? `<button class="detail-btn stop"  onclick="stopProject('${nameAttr}')">■ Stop</button>`
        : `<button class="detail-btn start" onclick="startProject('${nameAttr}')">▶ Start</button>`}
      <button class="detail-btn" onclick="window.open('${urlVal}','_blank')">↗ Open in Browser</button>
      <button class="detail-btn" onclick="apiOpen('${dirAttr}','finder')">📁 Open in Finder</button>
      <button class="detail-btn" onclick="apiOpen('${dirAttr}','terminal')">⌘ Open in Terminal</button>
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
      ${tagsBlock}
      ${notesBlock}
      ${pidBlock}
    </div>

    <div class="detail-section">
      <div class="detail-section-title">Danger Zone</div>
      <button class="detail-btn danger" onclick="removeProject('${nameAttr}')">
        Remove from Registry
      </button>
    </div>
  `;
}

function closeDetail() {
  selectedName = null;
  document.querySelectorAll(".project-row").forEach(r => r.classList.remove("selected"));
  $("detailPanel").classList.remove("open");
}

// ── Project actions ────────────────────────────────────────────────────────

async function startProject(name) {
  try {
    const res  = await fetch(`/api/projects/${encodeURIComponent(name)}/start`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error);
    toast(`${name} started`, "success");
    await refresh();
  } catch (e) {
    toast(e.message, "error");
  }
}

async function stopProject(name) {
  try {
    const res  = await fetch(`/api/projects/${encodeURIComponent(name)}/stop`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error);
    toast(`${name} stopped`, "success");
    await refresh();
  } catch (e) {
    toast(e.message, "error");
  }
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
  } catch (e) {
    toast(e.message, "error");
  }
}

async function stopOrphan(port) {
  if (!confirm(`Stop the process on port ${port}?`)) return;
  try {
    const res  = await fetch(`/api/orphans/${port}/stop`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error);
    toast(`Process on :${port} stopped`, "success");
    await refresh();
  } catch (e) {
    toast(e.message, "error");
  }
}

async function apiOpen(path, mode) {
  try {
    const res = await fetch("/api/open", {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ path, mode }),
    });
    if (!res.ok) {
      const d = await res.json();
      throw new Error(d.error);
    }
  } catch (e) {
    toast(`Could not open: ${e.message}`, "error");
  }
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

// ── Modal ──────────────────────────────────────────────────────────────────

function initModal() {
  $("addProjectBtn").addEventListener("click", openModal);
  $("modalClose").addEventListener("click",   closeModal);
  $("cancelBtn").addEventListener("click",    closeModal);

  $("modalOverlay").addEventListener("click", e => {
    if (e.target === $("modalOverlay")) closeModal();
  });

  // Auto-suggest URL as port is typed
  $("addProjectForm").querySelector("[name='port']").addEventListener("input", e => {
    const urlField = $("addProjectForm").querySelector("[name='url']");
    if (!urlField.value) {
      urlField.placeholder = `http://localhost:${e.target.value}`;
    }
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
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error);
      toast(`${payload.name} registered`, "success");
      closeModal();
      await refresh();
      selectProject(payload.name);
    } catch (err) {
      $("formError").textContent = err.message;
    }
  });
}

function openModal() {
  $("modalOverlay").classList.add("open");
  setTimeout(() => $("addProjectForm").querySelector("[name='name']").focus(), 60);
}

function closeModal() {
  $("modalOverlay").classList.remove("open");
  $("addProjectForm").reset();
  $("formError").textContent = "";
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
  const container = $("toastContainer");
  const t = document.createElement("div");
  t.className   = `toast ${type}`;
  t.textContent = msg;
  container.appendChild(t);
  setTimeout(() => t.remove(), 3500);
}
