/* Nexus Home — single-page app. No framework, no build step. */
"use strict";

const TOKEN = window.NEXUS_TOKEN;
const HEADERS = { "Authorization": `Bearer ${TOKEN}`, "Content-Type": "application/json" };

const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};

const CATEGORY_TINTS = {
  "trading": "var(--tint-trading)",
  "ai-tooling": "var(--tint-ai-tooling)",
  "scraping": "var(--tint-scraping)",
  "worship": "var(--tint-worship)",
  "other": "var(--tint-other)",
};

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------

async function api(path, opts = {}) {
  const resp = await fetch(path, { headers: HEADERS, ...opts });
  if (!resp.ok) {
    let detail = `${resp.status}`;
    try { detail = (await resp.json()).detail || detail; } catch (e) { /* ignore */ }
    throw new Error(detail);
  }
  return resp.json();
}

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

function relativeTime(iso) {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const s = Math.max(0, (Date.now() - then) / 1000);
  if (s < 90) return "just now";
  const m = s / 60; if (m < 90) return `${Math.round(m)}m ago`;
  const h = m / 60; if (h < 36) return `${Math.round(h)}h ago`;
  const d = h / 24; if (d < 45) return `${Math.round(d)}d ago`;
  return `${Math.round(d / 30)}mo ago`;
}

function monogram(name) {
  const words = name.trim().split(/\s+/);
  return (words.length >= 2 ? words[0][0] + words[1][0] : name.slice(0, 2)).toUpperCase();
}

function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
          .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

/* Tiny markdown renderer — headings, fences, inline code, bold/italic,
   links, lists, blockquotes, paragraphs. Enough for READMEs. */
function renderMarkdown(md) {
  const lines = md.replace(/\r\n/g, "\n").split("\n");
  const out = [];
  let inCode = false, codeBuf = [], listType = null, para = [];

  const flushPara = () => {
    if (para.length) { out.push(`<p>${inline(para.join(" "))}</p>`); para = []; }
  };
  const flushList = () => { if (listType) { out.push(`</${listType}>`); listType = null; } };
  const inline = (s) => {
    let h = escapeHtml(s);
    h = h.replace(/`([^`]+)`/g, "<code>$1</code>");
    h = h.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    h = h.replace(/\*([^*]+)\*/g, "<em>$1</em>");
    h = h.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, "<em>[image: $1]</em>");
    h = h.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    return h;
  };

  for (const line of lines) {
    if (line.startsWith("```")) {
      if (inCode) { out.push(`<pre><code>${escapeHtml(codeBuf.join("\n"))}</code></pre>`); codeBuf = []; }
      else { flushPara(); flushList(); }
      inCode = !inCode;
      continue;
    }
    if (inCode) { codeBuf.push(line); continue; }

    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) { flushPara(); flushList(); out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`); continue; }

    const ul = line.match(/^\s*[-*+]\s+(.*)$/);
    const ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (ul || ol) {
      flushPara();
      const want = ul ? "ul" : "ol";
      if (listType !== want) { flushList(); out.push(`<${want}>`); listType = want; }
      out.push(`<li>${inline((ul || ol)[1])}</li>`);
      continue;
    }
    if (line.startsWith(">")) { flushPara(); flushList(); out.push(`<blockquote>${inline(line.slice(1).trim())}</blockquote>`); continue; }
    if (line.trim() === "") { flushPara(); flushList(); continue; }
    para.push(line.trim());
  }
  if (inCode && codeBuf.length) out.push(`<pre><code>${escapeHtml(codeBuf.join("\n"))}</code></pre>`);
  flushPara(); flushList();
  return out.join("\n");
}

// ---------------------------------------------------------------------------
// Toasts + confirm
// ---------------------------------------------------------------------------

function toast(message, ms = 3200) {
  let root = $(".toast-root");
  if (!root) { root = el("div", "toast-root"); document.body.appendChild(root); }
  const t = el("div", "toast glass-strong", message);
  root.appendChild(t);
  setTimeout(() => t.remove(), ms);
}

function confirmDialog({ title, body, confirmLabel, danger }) {
  return new Promise((resolve) => {
    const scrim = el("div", "modal-scrim");
    const box = el("div", "confirm-box glass-strong");
    box.appendChild(el("h2", null, title));
    const p = el("p"); p.textContent = body; box.appendChild(p);
    const foot = el("div", "modal-foot");
    const cancel = el("button", "btn", "Cancel");
    const ok = el("button", `btn ${danger ? "btn-danger" : "btn-accent"}`, confirmLabel);
    foot.append(cancel, ok);
    box.appendChild(foot);
    scrim.appendChild(box);
    $("#modal-root").appendChild(scrim);
    const close = (v) => { scrim.remove(); resolve(v); };
    cancel.onclick = () => close(false);
    ok.onclick = () => close(true);
    scrim.onclick = (e) => { if (e.target === scrim) close(false); };
  });
}

// ---------------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------------

let activeStatusPoll = null;

function stopStatusPoll() {
  if (activeStatusPoll) { clearInterval(activeStatusPoll); activeStatusPoll = null; }
}

function route() {
  stopStatusPoll();
  const hash = location.hash || "#/";
  const m = hash.match(/^#\/project\/(.+)$/);
  if (m) renderDetail(decodeURIComponent(m[1]));
  else renderWall();
}
window.addEventListener("hashchange", route);

// ---------------------------------------------------------------------------
// WALL
// ---------------------------------------------------------------------------

async function renderWall() {
  const view = $("#view");
  view.innerHTML = "";
  const header = el("div", "wall-header");
  header.appendChild(el("h1", null, "Projects"));
  const count = el("span", "wall-count", "");
  header.appendChild(count);
  header.appendChild(el("span", "spacer"));
  const addBtn = el("button", "btn btn-accent", "＋ Add Projects");
  addBtn.onclick = openAddFlow;
  header.appendChild(addBtn);
  view.appendChild(header);

  const wall = el("div", "wall");
  view.appendChild(wall);

  // glass skeletons while loading
  for (let i = 0; i < 6; i++) {
    const sk = el("div", "card glass skeleton");
    sk.innerHTML = `<div class="card-preview sk"></div>
      <div class="card-body">
        <div class="sk" style="height:18px;width:60%"></div>
        <div class="sk" style="height:44px"></div>
        <div class="sk" style="height:14px;width:40%"></div>
      </div>`;
    wall.appendChild(sk);
  }

  let projects;
  try {
    projects = await api("/projects");
  } catch (e) {
    wall.innerHTML = "";
    toast(`Failed to load projects: ${e.message}`);
    return;
  }
  wall.innerHTML = "";
  count.textContent = String(projects.length);

  if (!projects.length) {
    const empty = el("div", "empty glass");
    empty.innerHTML = `<div class="empty-mark">✦</div>
      <h2>No projects yet</h2>
      <p>Scan ~/Documents and pick which projects the Nexus should know about.</p>`;
    const cta = el("button", "btn btn-accent", "＋ Add Projects");
    cta.onclick = openAddFlow;
    empty.appendChild(cta);
    wall.appendChild(empty);
    wall.style.gridTemplateColumns = "1fr";
    return;
  }

  for (const p of projects) wall.appendChild(projectCard(p));
}

function projectCard(p) {
  const card = el("article", "card glass");
  card.onclick = () => { location.hash = `#/project/${encodeURIComponent(p.slug)}`; };

  const tint = CATEGORY_TINTS[p.category] || CATEGORY_TINTS.other;
  const preview = el("div", "card-preview");
  if (p.preview) {
    const img = el("img"); img.src = p.preview; img.alt = "";
    preview.appendChild(img);
  } else {
    preview.style.background = `linear-gradient(135deg, ${tint}, color-mix(in srgb, ${tint} 55%, #1b2230))`;
    preview.textContent = monogram(p.name);
  }
  card.appendChild(preview);

  const body = el("div", "card-body");
  body.appendChild(el("div", "card-name", p.name));
  body.appendChild(el("div", "card-summary", p.summary || "(not summarized yet)"));

  const pills = el("div", "card-pills");
  for (const s of (p.stack || []).slice(0, 5)) pills.appendChild(el("span", "pill", s));
  body.appendChild(pills);

  const foot = el("div", "card-foot");
  foot.appendChild(el("span", `status-dot status-${p.status || "archived"}`));
  foot.appendChild(el("span", null, p.status || "unknown"));
  const when = relativeTime(p.last_indexed);
  if (when) foot.appendChild(el("span", null, `· indexed ${when}`));
  body.appendChild(foot);

  card.appendChild(body);
  return card;
}

// ---------------------------------------------------------------------------
// ADD FLOW
// ---------------------------------------------------------------------------

async function openAddFlow() {
  const scrim = el("div", "modal-scrim");
  const modal = el("div", "modal glass-strong");
  modal.innerHTML = `<div class="modal-header">
      <h2>Add Projects</h2>
      <button class="icon-btn modal-close" aria-label="Close">✕</button>
    </div>
    <p class="modal-sub">Scanning ~/Documents for projects the Nexus doesn't know yet…</p>`;

  const manualRow = el("div", "manual-add-row");
  const manualInput = el("input", "run-cmd-input");
  manualInput.type = "text";
  manualInput.placeholder = "/absolute/path/to/project";
  const manualBtn = el("button", "btn", "Add by path");
  manualRow.append(manualInput, manualBtn);
  modal.appendChild(manualRow);

  const list = el("div", "modal-list");
  modal.appendChild(list);
  const foot = el("div", "modal-foot");
  const cancel = el("button", "btn", "Cancel");
  const approve = el("button", "btn btn-accent", "Index selected");
  approve.disabled = true;
  foot.append(cancel, approve);
  modal.appendChild(foot);
  scrim.appendChild(modal);
  $("#modal-root").appendChild(scrim);

  let closable = true;
  const close = () => { if (closable) scrim.remove(); };
  cancel.onclick = close;
  $(".modal-close", modal).onclick = close;
  // Only closes when the click target IS the scrim itself — clicks that
  // bubble up from inside .modal have e.target set to the inner element,
  // so they never match and never propagate a close.
  scrim.onclick = (e) => { if (e.target === scrim) close(); };

  manualBtn.onclick = async () => {
    const path = manualInput.value.trim();
    if (!path) return;
    manualBtn.disabled = true;
    manualInput.disabled = true;
    const originalLabel = manualBtn.textContent;
    manualBtn.textContent = "Adding…";
    try {
      // Bypasses discovery/marker filtering entirely — the user is vouching
      // for this path being a project, regardless of what discover() found.
      await api("/projects/manual", {
        method: "POST",
        body: JSON.stringify({ path }),
      });
      toast(`Added ${path}`);
      close();
      renderWall();
    } catch (e) {
      toast(`Add by path failed: ${e.message}`);
      manualBtn.disabled = false;
      manualInput.disabled = false;
      manualBtn.textContent = originalLabel;
    }
  };

  let candidates;
  try {
    candidates = await api("/projects/discover", { method: "POST" });
  } catch (e) {
    $(".modal-sub", modal).textContent = `Discovery failed: ${e.message}`;
    return;
  }

  if (!candidates.length) {
    $(".modal-sub", modal).textContent = "Nothing new — every discovered project is already indexed.";
    approve.remove();
    cancel.textContent = "Close";
    return;
  }
  $(".modal-sub", modal).textContent =
    `${candidates.length} un-indexed project${candidates.length > 1 ? "s" : ""} found. Approved projects get read (README + file tree) and summarized.`;

  const rows = [];
  for (const c of candidates) {
    const row = el("label", "cand");
    const cb = el("input"); cb.type = "checkbox"; cb.checked = c.inferred_status === "active";
    const main = el("div", "cand-main");
    main.appendChild(el("div", "cand-name", c.name));
    main.appendChild(el("div", "cand-path", c.path));
    const meta = el("div", "cand-meta");
    for (const lang of (c.languages || []).slice(0, 3)) meta.appendChild(el("span", "pill", lang));
    meta.appendChild(el("span", `status-dot status-${c.inferred_status}`));
    const state = el("span", "cand-state", "");
    meta.appendChild(state);
    row.append(cb, main, meta);
    list.appendChild(row);
    rows.push({ cand: c, cb, state });
  }
  const refresh = () => { approve.disabled = !rows.some(r => r.cb.checked); };
  rows.forEach(r => { r.cb.onchange = refresh; });
  refresh();

  approve.onclick = async () => {
    const selected = rows.filter(r => r.cb.checked);
    approve.disabled = true; cancel.disabled = true; closable = false;
    let okCount = 0;
    for (const r of selected) {
      r.state.textContent = "⏳";
      try {
        // one slug per call → per-project progress; one failure doesn't abort the batch
        const [result] = await api("/projects/index", {
          method: "POST",
          body: JSON.stringify({ slugs: [r.cand.slug] }),
        });
        if (result && result.ok) { r.state.textContent = "✓"; okCount++; }
        else { r.state.textContent = "✗"; r.state.title = (result && result.error) || "failed"; }
      } catch (e) {
        r.state.textContent = "✗"; r.state.title = e.message;
      }
    }
    closable = true; cancel.disabled = false;
    cancel.textContent = "Done";
    toast(`Indexed ${okCount}/${selected.length} project${selected.length > 1 ? "s" : ""}`);
    renderWall();
  };
}

// ---------------------------------------------------------------------------
// DETAIL
// ---------------------------------------------------------------------------

async function renderDetail(slug) {
  const view = $("#view");
  view.innerHTML = "";
  const shell = el("div", "detail glass");
  shell.innerHTML = `<button class="back-link">← Back to projects</button><p class="detail-summary">Loading…</p>`;
  view.appendChild(shell);
  $(".back-link", shell).onclick = () => { location.hash = "#/"; };

  let d;
  try {
    d = await api(`/projects/${encodeURIComponent(slug)}/detail`);
  } catch (e) {
    $(".detail-summary", shell).textContent = `Failed to load: ${e.message}`;
    return;
  }
  const note = d.note;
  shell.innerHTML = "";

  const back = el("button", "back-link", "← Back to projects");
  back.onclick = () => { location.hash = "#/"; };
  shell.appendChild(back);

  const top = el("div", "detail-top");
  top.appendChild(el("h1", null, note.name));
  top.appendChild(el("span", `status-dot status-${note.status}`));
  top.appendChild(el("span", "pill", note.category));
  for (const s of (note.stack || []).slice(0, 6)) top.appendChild(el("span", "pill", s));
  shell.appendChild(top);

  shell.appendChild(el("p", "detail-summary", note.summary || "(no summary)"));

  const actions = el("div", "detail-actions");
  const mkAction = (label, fn, cls = "btn") => {
    const b = el("button", cls, label); b.onclick = fn; actions.appendChild(b); return b;
  };
  mkAction("Open in VS Code", async () => {
    try { await api(`/projects/${slug}/open`, { method: "POST", body: JSON.stringify({ target: "vscode" }) }); toast("Opened in VS Code"); }
    catch (e) { toast(`VS Code failed: ${e.message}`); }
  });
  mkAction("Reveal in Finder", async () => {
    try { await api(`/projects/${slug}/open`, { method: "POST", body: JSON.stringify({ target: "finder" }) }); toast("Revealed in Finder"); }
    catch (e) { toast(`Finder failed: ${e.message}`); }
  });
  const reBtn = mkAction("Re-index", async () => {
    reBtn.disabled = true; reBtn.textContent = "Re-indexing…";
    try {
      const [r] = await api("/projects/index", { method: "POST", body: JSON.stringify({ slugs: [slug] }) });
      if (r && r.ok) { toast("Summary refreshed"); renderDetail(slug); }
      else { toast(`Re-index failed: ${(r && r.error) || "unknown"}`); reBtn.disabled = false; reBtn.textContent = "Re-index"; }
    } catch (e) { toast(`Re-index failed: ${e.message}`); reBtn.disabled = false; reBtn.textContent = "Re-index"; }
  });
  mkAction("Remove", async () => {
    const yes = await confirmDialog({
      title: `Remove ${note.name}?`,
      body: "This deletes the Nexus's note about the project only. The real folder on disk is not touched.",
      confirmLabel: "Remove note",
      danger: true,
    });
    if (!yes) return;
    try { await api(`/projects/${slug}`, { method: "DELETE" }); toast("Project note removed"); location.hash = "#/"; }
    catch (e) { toast(`Remove failed: ${e.message}`); }
  }, "btn btn-danger");
  shell.appendChild(actions);

  renderRunSection(shell, slug, note);

  if (!d.folder_exists) {
    const warn = el("p", "detail-summary");
    warn.textContent = "⚠ The folder no longer exists at the recorded path.";
    shell.appendChild(warn);
  }

  if (d.readme) {
    const sec = el("div", "detail-section");
    sec.appendChild(el("h3", null, "README"));
    const box = el("div", "readme glass");
    box.innerHTML = renderMarkdown(d.readme);
    sec.appendChild(box);
    shell.appendChild(sec);
  }

  if (d.file_tree && d.file_tree.length) {
    const sec = el("div", "detail-section");
    sec.appendChild(el("h3", null, "Files"));
    const tree = el("div", "file-tree glass");
    tree.textContent = d.file_tree
      .map(x => (x.includes("/") ? "  " + x.split("/").pop() + (x.endsWith("/") ? "/" : "") : x))
      .join("\n");
    sec.appendChild(tree);
    shell.appendChild(sec);
  }

  if (d.git) {
    const sec = el("div", "detail-section");
    sec.appendChild(el("h3", null, "Git"));
    const row = el("div", "git-row glass");
    const cell = (k, v) => { const s = el("span"); s.innerHTML = `<span class="k">${k}</span>${escapeHtml(String(v))}`; row.appendChild(s); };
    cell("branch", d.git.branch || "?");
    if (d.git.last_commit_msg) cell("last commit", d.git.last_commit_msg);
    if (d.git.last_commit_iso) cell("when", relativeTime(d.git.last_commit_iso));
    cell("state", d.git.dirty ? "dirty" : "clean");
    sec.appendChild(row);
    shell.appendChild(sec);
  }
}

/* Run section: editable run_command + Start/Stop + live output panel.
   Phase 3B minimal version — process state is in-memory on the orchestrator
   only (lost on restart), documented in OPEN_QUESTIONS.md. */
function renderRunSection(shell, slug, note) {
  const sec = el("div", "detail-section");
  sec.appendChild(el("h3", null, "Run"));

  const row = el("div", "run-row");
  const input = el("input", "run-cmd-input");
  input.type = "text";
  input.placeholder = "e.g. python app.py";
  input.value = note.run_command || "";
  row.appendChild(input);

  const saveBtn = el("button", "btn", "Save");
  const startBtn = el("button", "btn btn-accent", "Start");
  row.append(saveBtn, startBtn);
  sec.appendChild(row);

  const output = el("pre", "run-output file-tree glass");
  output.hidden = true;
  sec.appendChild(output);
  shell.appendChild(sec);

  const renderOutput = (lines) => {
    output.hidden = lines.length === 0;
    output.textContent = lines.join("\n");
    output.scrollTop = output.scrollHeight;
  };

  const setRunning = (running) => {
    startBtn.textContent = running ? "Stop" : "Start";
    startBtn.classList.toggle("btn-danger", running);
    startBtn.classList.toggle("btn-accent", !running);
    startBtn.disabled = !running && !input.value.trim();
  };
  setRunning(false);

  const poll = async () => {
    try {
      const status = await api(`/projects/${slug}/status`);
      setRunning(status.running);
      renderOutput(status.output || []);
      if (!status.running) stopStatusPoll();
    } catch (e) {
      stopStatusPoll();
    }
  };

  saveBtn.onclick = async () => {
    saveBtn.disabled = true;
    try {
      await api(`/projects/${slug}`, {
        method: "PATCH",
        body: JSON.stringify({ run_command: input.value.trim() }),
      });
      note.run_command = input.value.trim();
      toast("Run command saved");
      setRunning(false);
    } catch (e) {
      toast(`Save failed: ${e.message}`);
    } finally {
      saveBtn.disabled = false;
    }
  };

  startBtn.onclick = async () => {
    startBtn.disabled = true;
    stopStatusPoll();
    try {
      if (startBtn.textContent === "Stop") {
        const status = await api(`/projects/${slug}/stop`, { method: "POST" });
        setRunning(status.running);
        renderOutput(status.output || []);
      } else {
        const status = await api(`/projects/${slug}/start`, { method: "POST" });
        setRunning(status.running);
        renderOutput(status.output || []);
        if (status.running) activeStatusPoll = setInterval(poll, 2000);
      }
    } catch (e) {
      toast(`${startBtn.textContent === "Stop" ? "Stop" : "Start"} failed: ${e.message}`);
    } finally {
      startBtn.disabled = false;
    }
  };

  input.addEventListener("input", () => {
    if (startBtn.textContent === "Start") startBtn.disabled = !input.value.trim();
  });

  // Reflect live status on load — a process may already be running (e.g.
  // started from another tab, or the detail view was left and re-opened).
  poll().then(() => {
    if (startBtn.textContent === "Stop" && !activeStatusPoll) {
      activeStatusPoll = setInterval(poll, 2000);
    }
  });
}

// ---------------------------------------------------------------------------
// CHAT
// ---------------------------------------------------------------------------

const chat = {
  panel: null, log: null, input: null, send: null,
  mode: "auto", threadId: null, streaming: false,
  lastAnsweredModel: null,
};

function initChat() {
  chat.panel = $("#chat-panel");
  chat.log = $("#chat-log");
  chat.input = $("#chat-input");
  chat.send = $("#chat-send");

  $("#chat-fab").onclick = () => { chat.panel.hidden = false; $("#chat-fab").hidden = true; chat.input.focus(); };
  $("#chat-close").onclick = () => { chat.panel.hidden = true; $("#chat-fab").hidden = false; };

  for (const btn of document.querySelectorAll(".mode-opt")) {
    btn.onclick = () => {
      document.querySelectorAll(".mode-opt").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      chat.mode = btn.dataset.mode;
    };
  }

  chat.send.onclick = sendTurn;
  chat.input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendTurn(); }
  });
  chat.input.addEventListener("input", () => {
    chat.input.style.height = "auto";
    chat.input.style.height = Math.min(chat.input.scrollHeight, 130) + "px";
  });

  $("#routing-btn").onclick = toggleRoutingPopover;
}

async function toggleRoutingPopover() {
  const pop = $("#routing-popover");
  if (!pop.hidden) { pop.hidden = true; return; }
  pop.innerHTML = "Checking pool health…";
  pop.hidden = false;
  try {
    const h = await fetch("/health").then(r => r.json());
    pop.innerHTML = "";
    const r1 = h.rungs && h.rungs["1"];
    const mkRow = (state, label, extra) => {
      const row = el("div", "routing-row");
      row.appendChild(el("span", `dot dot-${state}`));
      const m = el("span", "m", label); row.appendChild(m);
      if (extra) row.appendChild(el("span", "until", extra));
      pop.appendChild(row);
    };
    mkRow(r1 && r1.healthy ? "live" : "dead", "claude (rung 1, subscription)");
    for (const p of h.pool || []) {
      const state = p.state === "live" ? "live" : (p.state === "dead" ? "dead" : "untested");
      let extra = "";
      if (p.state === "dead" && p.until) extra = `retry ${relativeTime(p.until).replace(" ago", "")}`;
      if (p.state === "dead" && p.until) {
        const mins = Math.max(0, Math.round((new Date(p.until) - Date.now()) / 60000));
        extra = `retry in ${mins}m`;
      }
      mkRow(state, p.model, extra);
    }
  } catch (e) {
    pop.textContent = `Health check failed: ${e.message}`;
  }
}

function chatMsg(cls, text) {
  const m = el("div", `msg ${cls}`, text);
  chat.log.appendChild(m);
  chat.log.scrollTop = chat.log.scrollHeight;
  return m;
}

async function sendTurn() {
  const text = chat.input.value.trim();
  if (!text || chat.streaming) return;
  chat.input.value = ""; chat.input.style.height = "auto";
  chat.streaming = true; chat.send.disabled = true;

  chatMsg("msg-user", text);
  const answer = chatMsg("msg-assistant", "…");
  let buffer = "";
  let gotToken = false;

  const body = {
    user_message: text,
    mode: chat.mode,
    include_projects: $("#ctx-projects").checked,
  };
  if (chat.threadId) body.thread_id = chat.threadId;

  try {
    const resp = await fetch("/turn", { method: "POST", headers: HEADERS, body: JSON.stringify(body) });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let raw = "";

    const handle = (evName, data) => {
      if (evName === "thread") { chat.threadId = data.thread_id; return; }
      if (evName === "meta") {
        const meta = el("div", "msg-meta");
        meta.appendChild(el("span", null, `mode: ${data.mode}`));
        const pill = el("span", "model-pill", shortModel(data.model));
        pill.title = data.model;
        meta.appendChild(pill);
        chat.log.insertBefore(meta, answer);
        chat.lastAnsweredModel = data.model;
        return;
      }
      if (evName === "handover") {
        const line = el("div", "handover-line",
          `${shortModel(data.from_model)} ${humanReason(data.reason || data.trigger)} → ${shortModel(data.to_model)}`);
        chat.log.insertBefore(line, answer);
        const pill = chat.log.querySelector(".msg-meta:last-of-type .model-pill");
        if (pill) { pill.textContent = shortModel(data.to_model); pill.title = data.to_model; }
        chat.lastAnsweredModel = data.to_model;
        return;
      }
      if (evName === "token" && data.text) {
        buffer += data.text; gotToken = true;
        answer.textContent = buffer;
        chat.log.scrollTop = chat.log.scrollHeight;
        return;
      }
      if (evName === "error") {
        answer.textContent = (buffer ? buffer + "\n\n" : "") + `⚠ ${data.message}`;
      }
    };

    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      raw += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = raw.indexOf("\n\n")) !== -1) {
        const block = raw.slice(0, idx); raw = raw.slice(idx + 2);
        let evName = "message", data = null;
        for (const line of block.split("\n")) {
          if (line.startsWith("event: ")) evName = line.slice(7).trim();
          else if (line.startsWith("data: ")) { try { data = JSON.parse(line.slice(6)); } catch (e) { data = null; } }
        }
        if (data !== null) handle(evName, data);
      }
    }
    if (!gotToken && answer.textContent === "…") answer.textContent = "(no reply)";
  } catch (e) {
    answer.textContent = `⚠ ${e.message}`;
  } finally {
    chat.streaming = false; chat.send.disabled = false;
  }
}

function shortModel(model) {
  if (!model) return "?";
  let m = model.replace(":free", "");
  if (m.includes("/")) m = m.split("/")[1];
  return m.replace("claude-", "");
}

function humanReason(reason) {
  switch (reason) {
    case "model_unavailable": return "unavailable";
    case "rate_limited": return "rate-limited";
    case "quota_exhausted": return "quota reached";
    default: return reason || "handed over";
  }
}

// ---------------------------------------------------------------------------
// LIVE MODEL SIDEBAR
// ---------------------------------------------------------------------------

const MODEL_SIDEBAR_POLL_MS = 7000; // within the 5-10s range

function initModelSidebar() {
  const fab = $("#model-fab");
  const panel = $("#model-sidebar");
  fab.onclick = () => {
    panel.hidden = !panel.hidden;
    if (!panel.hidden) refreshModelSidebar();
  };
  $("#model-sidebar-close").onclick = () => { panel.hidden = true; };

  refreshModelSidebar();
  setInterval(refreshModelSidebar, MODEL_SIDEBAR_POLL_MS);
}

async function refreshModelSidebar() {
  const last = $("#model-sidebar-last");
  if (chat.lastAnsweredModel) {
    last.textContent = `Last answered: ${shortModel(chat.lastAnsweredModel)}`;
    last.title = chat.lastAnsweredModel;
  } else {
    last.textContent = "No turns answered yet this session";
  }

  const list = $("#model-sidebar-list");
  try {
    const h = await fetch("/health", { headers: HEADERS }).then((r) => r.json());
    list.innerHTML = "";
    const addRow = (state, label, tier) => {
      const r = el("div", "routing-row");
      r.appendChild(el("span", `dot dot-${state}`));
      r.appendChild(el("span", "m", label));
      if (tier) r.appendChild(el("span", "until", tier));
      list.appendChild(r);
    };
    const r1 = h.rungs && h.rungs["1"];
    addRow(r1 && r1.healthy ? "live" : "dead", "claude (rung 1)", "");
    for (const p of h.pool || []) {
      const state = p.state === "live" ? "live" : (p.state === "dead" ? "dead" : "untested");
      addRow(state, shortModel(p.model), p.tier || "");
    }
  } catch (e) {
    list.textContent = `Health check failed: ${e.message}`;
  }
}

// ---------------------------------------------------------------------------
// boot
// ---------------------------------------------------------------------------

initChat();
initModelSidebar();
route();
