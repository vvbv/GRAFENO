/* GRAFENO web panel: single-page client of the REST + WebSocket API.
 *
 * Mirrors the TUI screens: task list (filters, chains, totals), task detail
 * (phase bar, agents, activity, every pipeline action, artifacts, media,
 * log, tokens), the full new-task form, the usage reports and the global
 * settings. Every text comes from the GRAFENO i18n catalog injected in the
 * page; every DOM node is built with textContent (no HTML injection).
 */
"use strict";

const BOOT = JSON.parse(document.getElementById("grafeno-boot").textContent);
const S = BOOT.strings;
const ROLES = ["first", "planner", "implementer", "reviewer", "final"];
const RUNNING = new Set(["first_step", "planning", "implementing", "reviewing", "fixing", "finalizing"]);
const TERMINAL = new Set(["done", "discarded"]);
const ART_KINDS = ["first", "plan", "review", "final"];
const TOKEN_KEY = "grafeno.token";
const WARN_AFTER_S = 90;   // no output: warning (TUI activity bar)
const STALL_AFTER_S = 300; // no output: possible stall

// ---------------------------------------------------------------- helpers
function tr(key, vars) {
  let text = S[key];
  if (text === undefined) text = key;
  if (vars) text = text.replace(/\{(\w+)\}/g, (match, name) => (name in vars ? String(vars[name]) : match));
  return text;
}
// TUI strings may carry Rich markup ([b]...[/b]) and key hints like "([p])".
function plain(text) {
  return String(text).replace(/\[\/?[a-z ]+\]/g, "").replace(/\s*\(\[[^\]]{1,3}\][^)]*\)/g, "").replace(/\s*\[[a-zA-Z]\]/g, "");
}
const $ = (id) => document.getElementById(id);

function h(tag, attrs, ...children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [key, value] of Object.entries(attrs)) {
      if (value === undefined || value === null || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key === "style" && typeof value === "object") Object.assign(node.style, value);
      else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
      else if (key in node && typeof value !== "string") node[key] = value;
      else node.setAttribute(key, value === true ? "" : value);
    }
  }
  for (const child of children.flat()) {
    if (child === undefined || child === null || child === false) continue;
    node.appendChild(typeof child === "string" || typeof child === "number" ? document.createTextNode(String(child)) : child);
  }
  return node;
}
function option(label, value, selected) {
  const node = new Option(label, value);
  if (selected) node.selected = true;
  return node;
}
function formatTokens(value) {
  value = value || 0;
  if (value >= 1e6) return (value / 1e6).toFixed(1) + "M";
  if (value >= 1e3) return (value / 1e3).toFixed(1) + "k";
  return String(value);
}
function tokenPair(input, output) { return "↑" + formatTokens(input) + " ↓" + formatTokens(output); }
function formatDuration(seconds) {
  seconds = Math.max(0, Math.round(seconds || 0));
  const hours = Math.floor(seconds / 3600), minutes = Math.floor((seconds % 3600) / 60), secs = seconds % 60;
  const pad = (n) => String(n).padStart(2, "0");
  if (hours) return hours + "h " + pad(minutes) + "m " + pad(secs) + "s";
  if (minutes) return minutes + "m " + pad(secs) + "s";
  return secs + "s";
}
function when(iso) { return (iso || "").replace("T", " "); }
function debounce(fn, ms) {
  let timer = null;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
}
function badgeClass(state) {
  if (state === "done") return "badge done";
  if (state === "failed") return "badge failed";
  if (state === "planned") return "badge planned";
  if (RUNNING.has(state)) return "badge run";
  if (state === "paused" || state === "discarded") return "badge " + state;
  return "badge";
}
function stateBadge(task) {
  return h("span", { class: badgeClass(task.state), text: task.state_label || tr("state." + task.state) });
}
function projectOf(task) { return task.remote ? task.remote : task.workdir; }

// ---------------------------------------------------------------- markdown
// Small safe Markdown renderer (headings, lists, code, quotes, tables,
// emphasis, links): the output is built with DOM nodes only.
const INLINE = /(`[^`]+`)|(\*\*[^*]+\*\*|__[^_]+__)|(\*[^*\s][^*]*\*|_[^_\s][^_]*_)|(\[[^\]]+\]\([^)\s]+\))|(https?:\/\/[^\s<>()]+)/g;
function inline(text) {
  const frag = document.createDocumentFragment();
  let last = 0;
  text.replace(INLINE, (match, code, strong, em, link, url, offset) => {
    if (offset > last) frag.appendChild(document.createTextNode(text.slice(last, offset)));
    if (code) frag.appendChild(h("code", { text: code.slice(1, -1) }));
    else if (strong) frag.appendChild(h("strong", null, inline(strong.slice(2, -2))));
    else if (em) frag.appendChild(h("em", null, inline(em.slice(1, -1))));
    else if (link) {
      const parts = /^\[([^\]]+)\]\(([^)\s]+)\)$/.exec(link);
      frag.appendChild(safeLink(parts[2], parts[1]));
    } else if (url) frag.appendChild(safeLink(url, url));
    last = offset + match.length;
    return match;
  });
  if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
  return frag;
}
function safeLink(href, label) {
  if (!/^https?:\/\//i.test(href)) return document.createTextNode(label);
  return h("a", { href, target: "_blank", rel: "noopener noreferrer", text: label });
}
function splitRow(line) {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
}
function renderMarkdown(source) {
  const root = h("div", { class: "md" });
  const lines = String(source || "").replace(/\r\n?/g, "\n").split("\n");
  let index = 0;
  let paragraph = [];
  const flush = () => {
    if (paragraph.length) root.appendChild(h("p", null, inline(paragraph.join(" "))));
    paragraph = [];
  };
  while (index < lines.length) {
    const line = lines[index];
    let match;
    if (/^\s*```/.test(line)) {
      flush();
      const body = [];
      index++;
      while (index < lines.length && !/^\s*```/.test(lines[index])) body.push(lines[index++]);
      index++;
      root.appendChild(h("pre", null, h("code", { text: body.join("\n") })));
      continue;
    }
    if ((match = /^(#{1,6})\s+(.*)$/.exec(line))) {
      flush();
      root.appendChild(h("h" + match[1].length, null, inline(match[2].replace(/\s*#+\s*$/, ""))));
      index++;
      continue;
    }
    if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) { flush(); root.appendChild(h("hr")); index++; continue; }
    if (/^\s*\|.*\|\s*$/.test(line) && index + 1 < lines.length && /^\s*\|?\s*:?-{3,}/.test(lines[index + 1])) {
      flush();
      const table = h("table");
      table.appendChild(h("thead", null, h("tr", null, splitRow(line).map((cell) => h("th", null, inline(cell))))));
      const body = h("tbody");
      index += 2;
      while (index < lines.length && /^\s*\|.*\|\s*$/.test(lines[index])) {
        body.appendChild(h("tr", null, splitRow(lines[index]).map((cell) => h("td", null, inline(cell)))));
        index++;
      }
      table.appendChild(body);
      root.appendChild(table);
      continue;
    }
    if (/^\s*>/.test(line)) {
      flush();
      const quote = [];
      while (index < lines.length && /^\s*>/.test(lines[index])) quote.push(lines[index++].replace(/^\s*>\s?/, ""));
      root.appendChild(h("blockquote", null, renderMarkdown(quote.join("\n"))));
      continue;
    }
    if ((match = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/.exec(line))) {
      flush();
      const ordered = /\d/.test(match[2]);
      const list = h(ordered ? "ol" : "ul");
      while (index < lines.length && (match = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/.exec(lines[index]))) {
        let text = match[3];
        const item = h("li", { style: { marginLeft: Math.min(match[1].length, 12) * 6 + "px" } });
        const box = /^\[([ xX])\]\s+(.*)$/.exec(text);
        if (box) { item.appendChild(document.createTextNode(box[1] === " " ? "☐ " : "☑ ")); text = box[2]; }
        item.appendChild(inline(text));
        list.appendChild(item);
        index++;
      }
      root.appendChild(list);
      continue;
    }
    if (!line.trim()) { flush(); index++; continue; }
    paragraph.push(line.trim());
    index++;
  }
  flush();
  return root;
}

// ---------------------------------------------------------------- toasts / status
function toast(message, kind) {
  const node = h("div", { class: "toast" + (kind ? " " + kind : ""), text: message });
  $("toasts").appendChild(node);
  setTimeout(() => node.remove(), kind === "error" ? 8000 : 4000);
}
function fail(err) {
  if (err && err.status === 401) return;
  toast(tr("web.ui.error") + ": " + (err && err.message ? err.message : err), "error");
}
function setStatus(kind) {
  $("status-dot").className = "dot " + kind;
  $("status-text").textContent = tr("web.ui." + kind);
}

// ---------------------------------------------------------------- token + api
const auth = { token: "" };
function storeToken(token) {
  auth.token = token;
  try { if (token) localStorage.setItem(TOKEN_KEY, token); else localStorage.removeItem(TOKEN_KEY); } catch (e) { /* storage blocked */ }
  $("logout").hidden = !token;
}
function initToken() {
  const params = new URLSearchParams(location.search);
  const fromUrl = params.get("token");
  if (fromUrl) {
    storeToken(fromUrl);
    params.delete("token");
    const query = params.toString();
    history.replaceState(null, "", location.pathname + (query ? "?" + query : "") + location.hash);
    return;
  }
  let saved = "";
  try { saved = localStorage.getItem(TOKEN_KEY) || ""; } catch (e) { saved = ""; }
  storeToken(saved);
}
class ApiFailure extends Error {
  constructor(status, message) { super(message); this.status = status; }
}
let tokenDialogOpen = false;
async function api(method, path, body) {
  const headers = {};
  if (auth.token) headers.Authorization = "Bearer " + auth.token;
  const init = { method, headers };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, init);
  } catch (err) {
    setStatus("offline");
    throw new ApiFailure(0, String(err));
  }
  if (response.status === 401) {
    askToken();
    throw new ApiFailure(401, "unauthorized");
  }
  // The WebSocket shares the credential: open it once the API accepts us.
  if (response.ok && !live.ws) live.connect();
  const type = response.headers.get("Content-Type") || "";
  if (!type.includes("application/json")) {
    if (!response.ok) throw new ApiFailure(response.status, response.statusText);
    return response.blob();
  }
  const text = await response.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch (e) { data = {}; }
  if (!response.ok) throw new ApiFailure(response.status, data.error || response.statusText);
  return data;
}
const enc = encodeURIComponent;
const taskPath = (id, rest) => "/api/v1/tasks/" + enc(id) + (rest || "");

// ---------------------------------------------------------------- dialogs
// Generic modal: build(form, close) fills the form; resolves with the value
// passed to close() (undefined when dismissed).
function modal(options, build) {
  return new Promise((resolve) => {
    const dialog = h("dialog", { class: options.wide ? "wide" : "" });
    const form = h("form", { method: "dialog" });
    dialog.appendChild(form);
    let settled = false;
    const close = (value) => {
      if (settled) return;
      settled = true;
      dialog.close();
      // Removed on the next tick: the click that closes it may still submit the form.
      setTimeout(() => dialog.remove(), 0);
      resolve(value);
    };
    dialog.addEventListener("cancel", (event) => {
      if (options.sticky) { event.preventDefault(); return; }
      event.preventDefault();
      close(undefined);
    });
    form.addEventListener("submit", (event) => event.preventDefault());
    if (options.title) form.appendChild(h("h3", { text: options.title }));
    build(form, close);
    document.body.appendChild(dialog);
    dialog.showModal();
    const focus = form.querySelector("textarea, input:not([type=checkbox]), select");
    if (focus) focus.focus();
  });
}
function confirmDialog(title, body, detail, okLabel, danger) {
  return modal({ title }, (form, close) => {
    if (body) form.appendChild(h("div", { class: "body", text: body }));
    if (detail) form.appendChild(h("div", { class: "detail", text: detail }));
    form.appendChild(h("div", { class: "buttons" },
      h("button", { type: "button", text: tr("common.cancel"), onclick: () => close(false) }),
      h("button", { type: "submit", class: danger ? "danger solid" : "primary", text: okLabel || tr("det.mark.confirm"), onclick: () => close(true) })));
  });
}
function askToken() {
  if (tokenDialogOpen) return;
  tokenDialogOpen = true;
  modal({ title: tr("web.ui.token_title"), sticky: true }, (form, close) => {
    const input = h("input", { type: "password", autocomplete: "current-password", required: true });
    form.append(
      h("p", { class: "help", text: tr("web.ui.token_help") }),
      h("label", { class: "field" }, h("span", { class: "lbl", text: tr("web.ui.token_label") }), input),
      h("div", { class: "buttons" }, h("button", { type: "submit", class: "primary", text: tr("web.ui.token_submit"),
        onclick: () => { if (input.value.trim()) close(input.value.trim()); } })));
  }).then((token) => {
    tokenDialogOpen = false;
    if (!token) return;
    storeToken(token);
    if (live.ws) live.connect();
    router.reload();
  });
}
// ---------------------------------------------------------------- media viewer
// Extension sets shared by the media tab, the description thumbnails and the
// viewer (mirrors IMAGE_SUFFIXES/AUDIO_SUFFIXES of media.py).
const IMG_EXT = ["png", "jpg", "jpeg"];
const VID_EXT = ["mp4", "webm"];
const AUD_EXT = ["ogg", "oga", "opus", "mp3", "wav", "m4a", "flac"];
function mediaExt(name) { return name.split(".").pop().toLowerCase(); }
function mediaKind(name) {
  const ext = mediaExt(name);
  if (IMG_EXT.includes(ext)) return "image";
  if (VID_EXT.includes(ext)) return "video";
  if (AUD_EXT.includes(ext)) return "audio";
  return "file";
}
// Media the viewer can render, keeping the order of the task media list.
function viewableMedia(detail) {
  return (detail.media || []).filter((name) => mediaKind(name) !== "file");
}

// Full-screen overlay for the task media (image/video/audio) with previous and
// next navigation via on-screen arrows and the ArrowLeft/ArrowRight keys;
// Escape, a click on the backdrop or a route change closes it. The overlay
// keeps the ``lightbox`` class so the tasks screen Escape guard (which ignores
// Escape while a ``.lightbox`` or an open dialog exists) keeps working.
const viewer = {
  box: null, taskId: "", names: [], index: 0, loadToken: 0, onKey: null, onHash: null,
  open(taskId, names, index) {
    this.close();
    if (!names.length) return;
    this.taskId = taskId;
    this.names = names;
    this.index = Math.max(0, Math.min(index, names.length - 1));
    this.box = h("div", { class: "lightbox", onclick: (event) => { if (event.target === this.box) this.close(); } });
    document.body.appendChild(this.box);
    this.onKey = (event) => {
      if (event.key === "Escape") { event.preventDefault(); this.close(); }
      else if (event.key === "ArrowLeft") { event.preventDefault(); this.step(-1); }
      else if (event.key === "ArrowRight") { event.preventDefault(); this.step(1); }
    };
    document.addEventListener("keydown", this.onKey);
    this.onHash = () => this.close();
    window.addEventListener("hashchange", this.onHash);
    this.show();
  },
  close() {
    this.loadToken += 1;
    if (this.onKey) { document.removeEventListener("keydown", this.onKey); this.onKey = null; }
    if (this.onHash) { window.removeEventListener("hashchange", this.onHash); this.onHash = null; }
    if (this.box) { this.box.remove(); this.box = null; }
  },
  step(delta) {
    if (this.names.length < 2) return;
    this.index = (this.index + delta + this.names.length) % this.names.length;
    this.show();
  },
  async show() {
    const token = ++this.loadToken;
    const name = this.names[this.index];
    const url = await mediaUrl(this.taskId, name).catch(() => null);
    if (!this.box || token !== this.loadToken) return;  // closed or navigated meanwhile
    const kind = mediaKind(name);
    let node;
    if (!url) node = h("div", { class: "lb-error", text: name });
    else if (kind === "image") node = h("img", { src: url, alt: name });
    else if (kind === "video") node = h("video", { src: url, controls: true, autoplay: true });
    else node = h("audio", { src: url, controls: true, autoplay: true });
    const children = [node];
    if (this.names.length > 1) {
      children.push(
        h("button", { class: "lb-nav lb-prev", text: "‹", title: tr("web.media.prev"), "aria-label": tr("web.media.prev"), onclick: (event) => { event.stopPropagation(); this.step(-1); } }),
        h("button", { class: "lb-nav lb-next", text: "›", title: tr("web.media.next"), "aria-label": tr("web.media.next"), onclick: (event) => { event.stopPropagation(); this.step(1); } }),
      );
    }
    children.push(h("div", { class: "lb-caption", text: "media/" + name + "  " + tr("web.media.counter", { n: this.index + 1, total: this.names.length }) }));
    this.box.replaceChildren(...children);
  },
};

// ---------------------------------------------------------------- shared data
const store = {
  options: null,
  models: null,
  modelsPromise: null,
  async getOptions(force) {
    if (!this.options || force) this.options = await api("GET", "/api/v1/options");
    return this.options;
  },
  getModels(refresh) {
    if (refresh || (!this.models && !this.modelsPromise)) {
      this.modelsPromise = api("GET", "/api/v1/models" + (refresh ? "?refresh=1" : ""))
        .then((data) => { this.models = data; return data; })
        .finally(() => { this.modelsPromise = null; });
    }
    return this.modelsPromise || Promise.resolve(this.models);
  },
};
const mediaCache = new Map();
async function mediaUrl(taskId, name) {
  const key = taskId + "/" + name;
  if (!mediaCache.has(key)) {
    const blob = await api("GET", taskPath(taskId, "/media?name=" + enc(name)));
    mediaCache.set(key, URL.createObjectURL(blob));
  }
  return mediaCache.get(key);
}

// ---------------------------------------------------------------- live events
const live = {
  ws: null,
  ok: false,
  listeners: new Set(),
  connect() {
    if (this.ws) { const old = this.ws; this.ws = null; try { old.close(); } catch (e) { /* closed */ } }
    const scheme = location.protocol === "https:" ? "wss://" : "ws://";
    const query = auth.token ? "?token=" + enc(auth.token) : "";
    let socket;
    try { socket = new WebSocket(scheme + location.host + "/api/v1/ws" + query); } catch (e) { return; }
    this.ws = socket;
    socket.onopen = () => {
      socket.send(JSON.stringify({ id: 1, method: "subscribe", params: { topics: ["tasks"] } }));
      this.ok = true;
      setStatus("live");
    };
    socket.onmessage = (message) => {
      let data;
      try { data = JSON.parse(message.data); } catch (e) { return; }
      if (data.event === "task.changed" && data.task) this.listeners.forEach((fn) => fn(data.task));
    };
    socket.onclose = () => {
      if (this.ws !== socket) return;
      this.ok = false;
      this.ws = null;
      setStatus("polling");
      setTimeout(() => { if (!this.ws) api("GET", "/api/v1/status").catch(() => {}); }, 5000);
    };
  },
};

// ---------------------------------------------------------------- roles editor
// Rows of cli + model (with filter) + effort per role, fed by /api/v1/models.
function rolesEditor(initial, opts) {
  opts = opts || {};
  const known = (store.options && store.options.known_clis) || ["opencode", "kimi", "codex", "claude", "cursor", "minimax"];
  const values = {};
  for (const role of ROLES) values[role] = Object.assign({ cli: "opencode", model: "", effort: "" }, (initial || {})[role]);
  const status = h("div", { class: "help", text: tr("cfg.models.loading") });
  const grid = h("div", { class: "role-grid" },
    h("span", { class: "hdr", text: tr("web.ui.col_role") }), h("span", { class: "hdr", text: "CLI" }),
    h("span", { class: "hdr", text: tr("web.ui.col_model") }), h("span", { class: "hdr", text: tr("web.ui.col_effort") }));
  const rows = {};
  let catalog = store.models;
  for (const role of ROLES) {
    const cli = h("select", null, known.map((name) => option(name, name, name === values[role].cli)));
    const filter = h("input", { type: "search", placeholder: plain(tr("cfg.model.filter")) });
    const model = h("select");
    const effort = h("select");
    rows[role] = { cli, filter, model, effort };
    const refreshModels = () => {
      const current = values[role];
      const list = catalog && catalog.models ? (catalog.models[cli.value] || []).slice() : [];
      if (current.model && list.length && !list.includes(current.model) && current.cli !== cli.value) current.model = "";
      const needle = filter.value.trim().toLowerCase();
      let shown = needle ? list.filter((name) => name.toLowerCase().includes(needle)) : list;
      if (current.model && !shown.includes(current.model)) shown = [current.model].concat(shown);
      model.replaceChildren(option(plain(tr("cfg.model.prompt")), ""), ...shown.map((name) => option(name, name, name === current.model)));
      model.value = current.model;
      refreshEffort();
    };
    const refreshEffort = () => {
      const current = values[role];
      const levels = catalog && catalog.variants ? ((catalog.variants[cli.value] || {})[current.model] || []).slice() : [];
      if (current.effort && !levels.includes(current.effort)) {
        if (levels.length) current.effort = ""; else levels.push(current.effort);
      }
      effort.replaceChildren(option(plain(tr("cfg.effort.prompt")), ""), ...levels.map((level) => option(level, level, level === current.effort)));
      effort.value = current.effort;
    };
    cli.addEventListener("change", () => {
      values[role].model = "";
      values[role].effort = "";
      values[role].cli = cli.value;
      filter.value = "";
      refreshModels();
      if (opts.onChange) opts.onChange();
    });
    filter.addEventListener("input", refreshModels);
    model.addEventListener("change", () => { values[role].model = model.value; refreshEffort(); if (opts.onChange) opts.onChange(); });
    effort.addEventListener("change", () => { values[role].effort = effort.value; if (opts.onChange) opts.onChange(); });
    rows[role].refresh = refreshModels;
    grid.append(h("span", { class: "role-name", text: plain(tr("cfg.role." + role)) }), cli,
      h("div", { class: "model-cell" }, filter, model), effort);
    refreshModels();
  }
  const reload = h("button", { type: "button", class: "small", text: tr("web.ui.reload_models"), onclick: () => load(true) });
  const load = (refresh) => {
    status.textContent = tr("cfg.models.loading");
    store.getModels(refresh).then((data) => {
      catalog = data;
      for (const role of ROLES) rows[role].refresh();
      status.textContent = Object.entries(data.models || {}).map(([name, list]) =>
        list.length ? tr("cfg.models.count", { cli: name, count: list.length }) : tr("cfg.models.unavailable", { cli: name })).join(" · ");
    }).catch((err) => { status.textContent = String(err.message || err); });
  };
  load(false);
  return {
    node: h("div", null, grid, h("div", { class: "row" }, status, reload)),
    values: () => { const out = {}; for (const role of ROLES) out[role] = Object.assign({}, values[role]); return out; },
    set(roles) {
      for (const role of ROLES) {
        Object.assign(values[role], { cli: "opencode", model: "", effort: "" }, roles[role] || {});
        rows[role].cli.value = values[role].cli;
        rows[role].filter.value = "";
        rows[role].refresh();
      }
    },
  };
}

// ---------------------------------------------------------------- list editors
function listEditor(columns, rows, emptyText) {
  // columns: [{key, label, type?: "text"|"select"|"phases", options?}]
  const items = (rows || []).map((row) => Object.assign({}, row));
  const body = h("tbody");
  const table = h("table", null, h("thead", null, h("tr", null, columns.map((col) => h("th", { text: col.label })), h("th"))), body);
  const empty = h("div", { class: "help", text: emptyText || "" });
  const render = () => {
    body.replaceChildren();
    items.forEach((item, index) => {
      const cells = columns.map((col) => {
        let control;
        if (col.type === "select") {
          control = h("select", null, col.options.map(([label, value]) => option(label, value, item[col.key] === value)));
          control.value = item[col.key] || col.options[0][1];
          item[col.key] = control.value;
          control.addEventListener("change", () => { item[col.key] = control.value; });
        } else if (col.type === "phases") {
          control = phasesPicker(item, col);
        } else {
          control = h("input", { type: "text", value: item[col.key] || "", placeholder: col.placeholder || "" });
          control.addEventListener("input", () => { item[col.key] = control.value; });
        }
        return h("td", null, control);
      });
      body.appendChild(h("tr", null, cells, h("td", { class: "actions-cell" },
        h("button", { type: "button", class: "small danger", text: "✕", title: tr("web.ui.remove"),
          onclick: () => { items.splice(index, 1); render(); } }))));
    });
    empty.hidden = items.length > 0;
  };
  render();
  const add = h("button", { type: "button", class: "small", text: "+ " + tr("web.ui.add"),
    onclick: () => { const row = {}; columns.forEach((col) => { row[col.key] = col.default || (col.type === "select" ? col.options[0][1] : ""); }); items.push(row); render(); } });
  return {
    node: h("div", { class: "list-editor" }, table, empty, add),
    values: () => items.map((item) => Object.assign({}, item)),
  };
}
function phasesPicker(item, col) {
  const wrap = h("div", { class: "checks" });
  const all = h("input", { type: "checkbox" });
  const chosen = new Set((item[col.key] || "all") === "all" ? [] : String(item[col.key]).split(",").map((s) => s.trim()).filter(Boolean));
  all.checked = (item[col.key] || "all") === "all";
  const boxes = col.stages.map((stage) => {
    const box = h("input", { type: "checkbox" });
    box.checked = chosen.has(stage);
    box.disabled = all.checked;
    box.addEventListener("change", () => {
      if (box.checked) chosen.add(stage); else chosen.delete(stage);
      item[col.key] = col.stages.filter((s) => chosen.has(s)).join(",") || "all";
    });
    return h("label", { class: "check small-text" }, box, plain(tr("hook.stage." + stage)));
  });
  all.addEventListener("change", () => {
    boxes.forEach((label) => { label.firstChild.disabled = all.checked; });
    item[col.key] = all.checked ? "all" : (col.stages.filter((s) => chosen.has(s)).join(",") || "all");
  });
  item[col.key] = item[col.key] || "all";
  wrap.append(h("label", { class: "check small-text" }, all, tr("trig.all_phases")), ...boxes);
  return wrap;
}

// ---------------------------------------------------------------- attachments
// Collects files from a file input, paste (images) and drag & drop.
function attachmentsCollector(targets) {
  const files = [];
  const chips = h("div", { class: "chips" });
  const input = h("input", { type: "file", multiple: true });
  const render = () => {
    chips.replaceChildren(...files.map((file, index) => {
      const thumb = file.type.startsWith("image/") ? h("img", { src: URL.createObjectURL(file), alt: "" }) : null;
      return h("span", { class: "chip" }, thumb, file.name,
        h("button", { type: "button", text: "✕", title: tr("web.ui.remove"), onclick: () => { files.splice(index, 1); render(); } }));
    }));
  };
  const add = (list) => {
    for (const file of list) {
      if (files.length >= 10) { toast(tr("web.ui.max_attachments"), "warn"); break; }
      files.push(file);
    }
    render();
  };
  input.addEventListener("change", () => { add(Array.from(input.files || [])); input.value = ""; });
  for (const target of targets) {
    target.addEventListener("paste", (event) => {
      const items = Array.from((event.clipboardData && event.clipboardData.items) || []);
      const images = items.filter((item) => item.kind === "file" && item.type.startsWith("image/")).map((item) => item.getAsFile()).filter(Boolean);
      if (!images.length) return;
      event.preventDefault();
      add(images.map((file, index) => new File([file], "pasted-" + Date.now() + "-" + index + "." + (file.type.split("/")[1] || "png"), { type: file.type })));
      toast(tr("web.ui.image_pasted"));
    });
    target.addEventListener("dragover", (event) => { event.preventDefault(); target.classList.add("dropping"); });
    target.addEventListener("dragleave", () => target.classList.remove("dropping"));
    target.addEventListener("drop", (event) => {
      event.preventDefault();
      target.classList.remove("dropping");
      add(Array.from((event.dataTransfer && event.dataTransfer.files) || []));
    });
  }
  return {
    node: h("div", null, input, h("div", { class: "drop-hint", text: tr("web.ui.paste_hint") }), chips),
    async payload() {
      const out = [];
      for (const file of files) out.push({ name: file.name, data: await fileBase64(file) });
      return out;
    },
    count: () => files.length,
  };
}
function fileBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1] || "");
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

// ---------------------------------------------------------------- form helpers
function field(label, control, extra) {
  return h("label", { class: "field" + (extra && extra.full ? " full" : "") },
    h("span", { class: "lbl", text: plain(label) }), control, extra && extra.help ? h("span", { class: "help", text: extra.help }) : null);
}
function check(label, checked) {
  const box = h("input", { type: "checkbox" });
  box.checked = !!checked;
  return { box, node: h("label", { class: "check" }, box, plain(label)) };
}

// ---------------------------------------------------------------- tasks view
const tasksView = {
  tasks: [],
  hidden: new Set(),
  projects: [],
  selected: null,
  detail: null,
  tab: "desc",
  file: {},
  filters: { search: "", project: "", done: "all", state: "", date: "" },
  timers: [],
  root: null,
  lastLogKey: "",
  planAsked: new Set(),

  mount(params) {
    this.selected = params[0] || null;
    this.detail = null;
    this.headNode = null;
    this.lastLogKey = "";
    if (params[1]) this.tab = params[1];
    const main = $("main");
    main.replaceChildren(this.build());
    if (this.selected) this.showDetail(); else document.body.classList.remove("show-detail");
    this.refresh();
    this.timers.push(setInterval(() => this.tick(), 1000));
    this.timers.push(setInterval(() => this.refresh(true), 15000));
    this.onLive = (task) => this.applyEvent(task);
    live.listeners.add(this.onLive);
    // Escape goes back to the list, like the TUI detail screen.
    this.onKey = (event) => {
      if (event.key === "Escape" && this.selected && !document.querySelector("dialog[open], .lightbox")) {
        location.hash = "#/tasks";
      }
    };
    document.addEventListener("keydown", this.onKey);
  },
  unmount() {
    this.timers.forEach(clearInterval);
    this.timers = [];
    live.listeners.delete(this.onLive);
    document.removeEventListener("keydown", this.onKey);
    document.body.classList.remove("show-detail");
  },
  showList() {
    document.body.classList.remove("show-detail");
    this.renderList();
    if (this.tableWrap) this.tableWrap.scrollTop = this.listScroll || 0;
  },
  showDetail() {
    if (this.tableWrap && !document.body.classList.contains("show-detail")) this.listScroll = this.tableWrap.scrollTop;
    document.body.classList.add("show-detail");
    this.detailNode.replaceChildren(h("div", { class: "empty", text: tr("web.ui.loading") }));
    window.scrollTo(0, 0);
  },
  build() {
    const f = this.filters;
    const search = h("input", { type: "search", placeholder: tr("web.ui.search"), value: f.search });
    search.addEventListener("input", () => { f.search = search.value; this.renderList(); });
    this.projectSelect = h("select");
    this.projectSelect.addEventListener("change", () => { f.project = this.projectSelect.value; this.renderList(); });
    const done = h("select", null, ["all", "hide", "only"].map((mode) => option(tr("tasks.done." + mode), mode, f.done === mode)));
    done.addEventListener("change", () => { f.done = done.value; this.renderList(); });
    const state = h("select", null, option(tr("web.ui.any_state"), ""), BOOT.states.map((value) => option(tr("state." + value), value, f.state === value)));
    state.addEventListener("change", () => { f.state = state.value; this.renderList(); });
    const date = h("input", { type: "date", value: f.date, title: tr("tasks.bind.date") });
    const today = h("button", { type: "button", class: "small" });
    const syncToday = () => { today.textContent = tr(f.date ? "tasks.date.all_dates" : "tasks.date.today"); };
    date.addEventListener("change", () => { f.date = date.value; syncToday(); this.renderList(); });
    today.addEventListener("click", () => {
      f.date = f.date ? "" : new Date().toLocaleDateString("sv-SE");
      date.value = f.date;
      syncToday();
      this.renderList();
    });
    syncToday();
    const reload = h("button", { type: "button", class: "small", text: tr("tasks.bind.reload"), onclick: () => this.refresh() });
    this.rows = h("tbody");
    this.listEmpty = h("div", { class: "empty", hidden: true, text: tr("web.ui.empty") });
    this.summary = h("div", { class: "list-summary" });
    const table = h("table", { id: "task-table" },
      h("thead", null, h("tr", null,
        h("th", { text: tr("tasks.col.task") }), h("th", { text: tr("tasks.col.state") }),
        h("th", { class: "num col-opt", text: tr("tasks.col.iter") }), h("th", { class: "num col-opt", text: tr("tasks.col.tokens") }),
        h("th", { class: "num col-opt", text: tr("tasks.col.duration") }), h("th", { class: "col-opt", text: tr("tasks.col.updated") }),
        h("th", { class: "col-opt", text: tr("tasks.col.workdir") }))),
      this.rows);
    this.tableWrap = h("div", { class: "table-wrap" }, table, this.listEmpty);
    const list = h("section", { id: "list-pane" },
      h("div", { class: "filters" }, search, this.projectSelect, done, state, date, today, reload),
      this.tableWrap, this.summary);
    this.detailNode = h("div", { id: "detail" });
    return h("div", { class: "screens" }, list, h("section", { id: "detail-pane" }, this.detailNode));
  },
  async refresh(quiet) {
    try {
      const [data, projects] = await Promise.all([api("GET", "/api/v1/tasks"), api("GET", "/api/v1/projects")]);
      this.tasks = data.tasks || [];
      this.hidden = new Set(data.done_hidden_ids || []);
      this.projects = projects.projects || [];
      if (!live.ok) setStatus("polling");
      this.renderProjects();
      this.renderList();
      if (this.selected) await this.loadDetail(!quiet && !this.detail);
    } catch (err) {
      if (!quiet) fail(err);
    }
  },
  applyEvent(task) {
    const index = this.tasks.findIndex((item) => item.id === task.id);
    if (index >= 0) this.tasks[index] = Object.assign({}, this.tasks[index], task);
    else { this.refresh(true); return; }
    this.renderList();
    if (task.id === this.selected) this.loadDetail(false).catch(fail);
  },
  renderProjects() {
    const select = this.projectSelect;
    const current = this.filters.project;
    const seen = new Set();
    const names = [];
    for (const item of this.projects) { if (!seen.has(item.workdir)) { seen.add(item.workdir); names.push([item.workdir, item.count]); } }
    for (const task of this.tasks) {
      const key = projectOf(task);
      if (!seen.has(key)) { seen.add(key); names.push([key, null]); }
    }
    select.replaceChildren(option(tr("web.ui.all_projects"), ""), ...names.map(([name, count]) => option(count === null ? name : name + " (" + count + ")", name, name === current)));
    select.value = seen.has(current) ? current : "";
  },
  visible() {
    const f = this.filters;
    const text = f.search.trim().toLowerCase();
    return this.tasks.filter((task) => {
      if (f.project && projectOf(task) !== f.project && task.workdir !== f.project) return false;
      if (f.state && task.state !== f.state) return false;
      if (f.done === "hide" && this.hidden.has(task.id)) return false;
      if (f.done === "only" && task.state !== "done") return false;
      if (f.date && (task.created_at || "").slice(0, 10) !== f.date) return false;
      if (text && !(task.name + " " + task.workdir + " " + task.remote + " " + task.id).toLowerCase().includes(text)) return false;
      return true;
    });
  },
  renderList() {
    const tasks = this.visible();
    this.rows.replaceChildren(...tasks.map((task) => {
      const name = h("td", { class: "name" });
      if (task.running) name.appendChild(h("span", { class: "run-mark", text: "▶" }));
      if (task.depth) name.appendChild(h("span", { class: "chain", text: "  ".repeat(task.depth) + "└ " }));
      name.appendChild(document.createTextNode(task.name));
      if (task.repeat_mode) name.appendChild(h("span", { class: "tag", text: "↻", title: tr("nt.repeat") }));
      const tokens = task.tokens && (task.tokens.input || task.tokens.output) ? tokenPair(task.tokens.input, task.tokens.output) : "";
      const row = h("tr", { class: task.id === this.selected ? "active" : "" },
        name, h("td", null, stateBadge(task)),
        h("td", { class: "num col-opt", text: String(task.iteration || 0) }),
        h("td", { class: "num col-opt", text: tokens }),
        h("td", { class: "num col-opt", text: task.duration_seconds ? formatDuration(task.duration_seconds) : "" }),
        h("td", { class: "when col-opt", text: when(task.updated_at) }),
        h("td", { class: "project col-opt", text: projectOf(task), title: projectOf(task) }));
      row.addEventListener("click", () => { location.hash = "#/tasks/" + enc(task.id); });
      return row;
    }));
    this.listEmpty.hidden = tasks.length > 0;
    if (!this.tasks.length) this.listEmpty.textContent = tr("web.ui.no_tasks");
    else this.listEmpty.textContent = tr("web.ui.empty");
    // Totals of the visible tasks: tokens by CLI+model and time (TUI footer).
    const totals = {};
    let seconds = 0;
    for (const task of tasks) {
      seconds += task.duration_seconds || 0;
      for (const [label, pair] of Object.entries(task.tokens_by_agent || {})) {
        const entry = totals[label] || (totals[label] = [0, 0]);
        entry[0] += pair[0];
        entry[1] += pair[1];
      }
    }
    const parts = Object.entries(totals).sort((a, b) => (b[1][0] + b[1][1]) - (a[1][0] + a[1][1]) || a[0].localeCompare(b[0]))
      .map(([label, pair]) => label + ": " + tokenPair(pair[0], pair[1]));
    this.summary.replaceChildren(
      parts.length ? h("div", { text: tr("tasks.tokens.summary", { summary: parts.join(" · ") }) }) : null,
      seconds ? h("div", { text: tr("tasks.time.summary", { duration: formatDuration(seconds) }) }) : null,
      h("div", { text: tr("web.ui.count", { shown: tasks.length, total: this.tasks.length }) }));
  },

  // ------------------------------------------------------------ detail
  async loadDetail(full) {
    if (!this.selected) return;
    let data;
    try {
      data = await api("GET", taskPath(this.selected));
    } catch (err) {
      if (err.status === 404) { location.hash = "#/tasks"; return; }
      throw err;
    }
    const previous = this.detail;
    this.detail = data.task;
    this.detail.fetchedAt = Date.now();
    const changed = !previous || previous.task.id !== this.detail.task.id;
    this.renderDetail(full || changed, previous);
  },
  renderDetail(full, previous) {
    const d = this.detail;
    const task = d.task;
    this.detailNode.style.display = "flex";
    if (full || !this.headNode) {
      this.headNode = h("div", { class: "detail-head" });
      this.tabsNode = h("nav", { class: "tabs" });
      this.bodyNode = h("div", { class: "tab-body" });
      this.detailNode.replaceChildren(this.headNode, this.tabsNode, this.bodyNode);
      this.file = {};
    }
    this.renderHead();
    this.renderTabs();
    const stateChanged = previous && (previous.task.state !== task.state || previous.task.cycle !== task.cycle || previous.task.iteration !== task.iteration);
    const fileTab = ART_KINDS.includes(this.tab) || this.tab === "media";
    if (full || stateChanged || !fileTab) this.renderTab(full);
    this.maybeAskPlan();
  },
  renderHead() {
    const d = this.detail;
    const task = d.task;
    const meta = [];
    if (task.cycle > 1) meta.push(tr("det.cycle", { n: task.cycle }));
    meta.push(task.workdir);
    if (d.remote_target) meta.push(tr("det.remote", { target: d.remote_target + (task.remote_os ? " (" + task.remote_os + ")" : "") }));
    if (task.scheduled_at) meta.push(tr("det.scheduled", { at: when(task.scheduled_at) }));
    if (task.repeat_mode) meta.push(tr("det.repeat." + task.repeat_mode, { n: task.repeat_count, minutes: task.repeat_interval_minutes }));
    if (task.branch) meta.push("⎇ " + task.branch);
    const back = h("button", { class: "link back", text: "← " + tr("common.back"), onclick: () => { location.hash = "#/tasks"; } });
    const nodes = [
      back,
      h("div", { class: "row" }, h("h2", { text: task.name }), stateBadge(Object.assign({}, task, { state_label: d.state_label }))),
      h("div", { class: "detail-meta" }, meta.map((item) => h("span", { text: item }))),
      this.phaseBar(),
      this.agentsBar(),
    ];
    if (d.models_missing && d.models_missing.length) {
      nodes.push(h("div", { class: "banner danger", text: tr("det.models_missing", { items: d.models_missing.join(" · ") }) }));
    }
    if (!(store.options && store.options.available_clis && store.options.available_clis.length)) {
      if (store.options) nodes.push(h("div", { class: "banner warn", text: tr("det.no_clis") }));
    }
    this.activityNode = h("div", { class: "activity" });
    nodes.push(this.activityNode);
    if (task.state === "planned" && d.actions.approve_plan) {
      nodes.push(h("div", { class: "banner ok" },
        h("div", { text: plain(tr("pc.title")) + " · " + plain(tr("web.ui.plan_ready_hint")) }),
        h("div", { class: "row" },
          h("button", { class: "primary small", text: tr("pc.implement"), onclick: () => this.approvePlan() }),
          h("button", { class: "small", text: tr("web.ui.view_plan"), onclick: () => this.setTab("plan") }))));
    }
    nodes.push(this.actionsBar());
    this.headNode.replaceChildren(...nodes);
    this.renderActivity();
  },
  phaseBar() {
    const d = this.detail;
    const bar = h("div", { class: "phasebar" });
    (d.phase_bar || []).forEach((phase, index) => {
      if (index) bar.appendChild(h("span", { class: "phase-sep" }));
      let label = phase.label;
      if (phase.key === "review" && d.task.iteration > 0) label += " ×" + d.task.iteration;
      bar.appendChild(h("span", { class: "phase " + phase.status }, h("span", { class: "pip" }), label));
    });
    return bar;
  },
  agentsBar() {
    const d = this.detail;
    const nodes = [];
    if (d.task.profile) nodes.push(h("span", null, tr("det.profile") + ": ", h("span", { class: "prof", text: d.task.profile })));
    for (const agent of d.agents || []) {
      nodes.push(h("span", null, agent.label + ": ", h("b", { text: agent.cli + "/" + (agent.model || "default") + (agent.effort ? " #" + agent.effort : "") }),
        agent.tokens ? h("span", { class: "tok", text: " " + tokenPair(agent.tokens.input, agent.tokens.output) }) : null));
    }
    return h("div", { class: "agents" }, nodes);
  },
  renderActivity() {
    if (!this.activityNode || !this.detail) return;
    const d = this.detail;
    const run = d.runtime || {};
    const drift = (Date.now() - (d.fetchedAt || Date.now())) / 1000;
    const tokens = d.token_totals || { input: 0, output: 0 };
    const parts = [];
    if (run.running) {
      const frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏";
      parts.push(h("span", { class: "spin", text: frames[Math.floor(Date.now() / 250) % frames.length] }));
      parts.push(h("span", { class: "label", text: run.phase_label }));
      parts.push(h("span", { class: "el", text: formatDuration(run.phase_elapsed_seconds + drift) }));
      parts.push(h("span", { text: tr("act.events", { count: run.event_count }) }));
      const silence = run.silence_seconds + drift;
      if (silence >= STALL_AFTER_S) parts.push(h("span", { class: "stall", text: plain(tr("web.ui.stall", { duration: formatDuration(silence) })) }));
      else if (silence >= WARN_AFTER_S) parts.push(h("span", { class: "warn", text: tr("act.warn", { duration: formatDuration(silence) }) }));
      else parts.push(h("span", { text: tr("act.last", { duration: formatDuration(silence) }) }));
      parts.push(h("span", { text: tr("act.total", { total: formatDuration(run.total_seconds + drift) }) }));
    } else if (d.durations && Object.keys(d.durations).length) {
      parts.push(h("span", { text: tr("act.idle_total", { total: formatDuration(run.total_seconds) }) }));
    } else {
      parts.push(h("span", { text: tr("act.idle") }));
    }
    if (tokens.input || tokens.output) parts.push(h("span", { text: tr("det.tokens", { input: formatTokens(tokens.input), output: formatTokens(tokens.output) }) }));
    if (d.usage_waiting) parts.push(h("span", { class: "warn", text: tr("state.waiting") }));
    this.activityNode.replaceChildren(...parts.flatMap((part, index) => (index ? [h("span", { class: "muted", text: "·" }), part] : [part])));
  },
  actionsBar() {
    const a = this.detail.actions || {};
    const btn = (label, enabled, handler, cls) => h("button", { class: "small" + (cls ? " " + cls : ""), text: plain(label), disabled: !enabled, onclick: handler });
    return h("div", { class: "actions" },
      h("div", { class: "action-group" },
        btn(tr("det.bind.automode"), a.automode, () => this.runPhase("automode"), "primary"),
        btn(tr("det.bind.plan"), a.plan, () => this.runPhase("plan")),
        btn(tr("det.bind.implement"), a.implement, () => this.runPhase("implement")),
        btn(tr("det.bind.review"), a.review, () => this.runPhase("review")),
        btn(tr("det.bind.fix"), a.fix, () => this.runPhase("fix")),
        btn(tr("det.bind.final"), a.final, () => this.runPhase("final")),
        btn(tr("det.bind.tests"), a.tests, () => this.runPhase("tests"))),
      h("div", { class: "action-group" },
        btn(tr("det.bind.more"), a.extend, () => this.askMore()),
        btn(tr("det.bind.continue"), a.continue, () => this.simpleAction("continue", "det.continue.title", "det.continue.body")),
        btn(tr("det.bind.resume"), a.resume, () => this.simpleAction("resume", "det.resume.title", "det.resume.body")),
        btn(tr("det.bind.cancel"), a.cancel, () => this.cancelRun(), "danger")),
      h("div", { class: "action-group" },
        btn(tr("det.bind.edit"), a.edit, () => this.editTask()),
        btn(tr("det.bind.agents"), a.roles, () => this.editRoles()),
        btn(tr("det.bind.complete"), a.mark_done, () => this.simpleAction("mark-done", "det.mark.done.title", "det.mark.done.body")),
        btn(tr("det.bind.restart"), a.reset, () => this.simpleAction("reset", "det.restart.title", "det.restart.body", true)),
        btn(tr("det.bind.discard"), a.discard, () => this.simpleAction("discard", "det.mark.discard.title", "det.mark.discard.body", true), "danger")));
  },
  async post(path, body, okMessage) {
    try {
      const result = await api("POST", taskPath(this.selected, path), body || {});
      toast(okMessage || tr("web.ui.done_ok"));
      await this.refresh(true);
      return result;
    } catch (err) {
      fail(err);
      return null;
    }
  },
  async runPhase(phase) {
    const d = this.detail;
    const task = d.task;
    const roleOf = { plan: "planner", implement: "implementer", review: "reviewer", fix: "implementer", final: "final" };
    const lines = [];
    if (roleOf[phase]) {
      const role = d[roleOf[phase]] || {};
      lines.push(plain(tr("pconf.agent", { cli: role.cli, model: role.model || "default" })));
    } else if (phase === "automode") {
      for (const role of ["planner", "implementer", "reviewer", "final"]) {
        const cfg = d[role] || {};
        lines.push(plain(tr("pconf.role." + role, { cli: cfg.cli, model: cfg.model || "default" })));
      }
    }
    lines.push(plain(tr("pconf.project", { workdir: task.workdir })));
    if (phase === "automode" && task.confirm_plan) lines.push(tr("pconf.pause_notice"));
    if (phase === "tests") lines.push(plain(tr("pconf.command", { command: task.test_command })));
    const title = tr("pconf.question", { title: tr("phaseinfo." + phase + ".title") });
    const ok = await confirmDialog(title, tr("phaseinfo." + phase + ".what").replace(/\n/g, " "), lines.join("\n"), tr("pconf.run"));
    if (ok) await this.post("/run", { phase }, tr("web.ui.started"));
  },
  async simpleAction(path, titleKey, bodyKey, danger) {
    const ok = await confirmDialog(plain(tr(titleKey)), plain(tr(bodyKey)), "", tr("det.mark.confirm"), danger);
    if (ok) await this.post("/" + path);
  },
  async cancelRun() {
    await this.post("/pause", {}, tr("det.cancel"));
  },
  async approvePlan() {
    await this.post("/approve-plan", {}, tr("web.ui.started"));
  },
  maybeAskPlan() {
    const d = this.detail;
    const key = d.task.id + ":" + d.task.cycle;
    if (!d.runtime || !d.runtime.pending_plan_confirm || this.planAsked.has(key) || !d.actions.approve_plan) return;
    this.planAsked.add(key);
    const count = (d.artifacts.plan || []).length;
    confirmDialog(plain(tr("pc.title")), plain(tr("pc.body", { count })), "", tr("pc.implement")).then((ok) => {
      if (ok) this.approvePlan();
    });
  },
  async askMore() {
    const task = this.detail.task;
    const d = this.detail;
    const result = await modal({ title: tr("rm.title", { cycle: task.cycle + 1 }) }, (form, close) => {
      const text = h("textarea", { rows: 6, required: true });
      const files = attachmentsCollector([text]);
      const body = tr("rm.body", {
        planner: d.planner.cli, approval: task.confirm_plan ? tr("rm.approval") : "",
        implementer: d.implementer.cli, reviewer: d.reviewer.cli,
      });
      form.append(field(tr("rm.prompt"), text), h("div", { class: "detail", text: body }),
        field(tr("web.ui.attachments"), files.node),
        h("div", { class: "buttons" },
          h("button", { type: "button", text: tr("common.cancel"), onclick: () => close(undefined) }),
          h("button", { type: "submit", class: "primary", text: tr("rm.accept"), onclick: async () => {
            if (!text.value.trim()) { toast(tr("rm.error.empty"), "error"); return; }
            close({ request: text.value.trim(), attachments: await files.payload() });
          } })));
    });
    if (result) await this.post("/extend", result, tr("web.ui.started"));
  },
  async editTask() {
    const d = this.detail;
    const task = d.task;
    const result = await modal({ title: tr("et.title") }, (form, close) => {
      const name = h("input", { type: "text", value: task.name, required: true });
      const desc = h("textarea", { rows: 10 });
      desc.value = task.description;
      const parent = h("select", null, option(tr("web.ui.parent_none"), ""),
        (d.rechain_candidates || []).map((item) => option(item.name + " (" + item.id + ")", item.id, item.id === task.parent_id)));
      parent.value = task.parent_id || "";
      form.append(field(tr("et.name"), name), field(tr("et.description"), desc), field(tr("et.parent"), parent),
        h("div", { class: "buttons" },
          h("button", { type: "button", text: tr("common.cancel"), onclick: () => close(undefined) }),
          h("button", { type: "submit", class: "primary", text: tr("common.save"), onclick: () => {
            if (!name.value.trim()) { toast(tr("et.error.name_required"), "error"); return; }
            close({ name: name.value.trim(), description: desc.value, parent_id: parent.value });
          } })));
    });
    if (result) await this.post("/edit", result, tr("det.info_updated", { name: result.name }));
  },
  async editRoles() {
    const d = this.detail;
    const task = d.task;
    const options = await store.getOptions().catch(() => null);
    const profiles = (options && options.profiles) || [];
    const result = await modal({ title: tr("roles.title", { name: task.name }), wide: true }, (form, close) => {
      const initial = {};
      for (const role of ROLES) initial[role] = d[role];
      let profileSelect = null;
      const editor = rolesEditor(initial, { onChange: () => { if (profileSelect && profileSelect.value) profileSelect.value = ""; } });
      form.append(h("p", { class: "help", text: plain(tr("roles.body")) }));
      if (profiles.length) {
        profileSelect = h("select", null, option(tr("roles.profile.none"), ""), profiles.map((p) => option(p.name, p.name, p.name === task.profile)));
        profileSelect.value = profiles.some((p) => p.name === task.profile) ? task.profile : "";
        profileSelect.addEventListener("change", () => {
          const chosen = profiles.find((p) => p.name === profileSelect.value);
          if (chosen) { editor.set(chosen.roles); profileSelect.value = chosen.name; }
        });
        form.append(field(tr("roles.profile"), profileSelect));
      }
      form.append(editor.node, h("div", { class: "buttons" },
        h("button", { type: "button", text: tr("common.cancel"), onclick: () => close(undefined) }),
        h("button", { type: "submit", class: "primary", text: tr("common.save"),
          onclick: () => close({ roles: editor.values(), profile: profileSelect ? profileSelect.value : "" }) })));
    });
    if (result) await this.post("/roles", result, tr("roles.saved"));
  },

  // ------------------------------------------------------------ tabs
  tabsList() {
    const d = this.detail;
    const tabs = [["desc", tr("det.tab.desc")], ["info", tr("web.ui.tab_info")]];
    tabs.push(["media", tr("det.tab.media"), (d.media || []).length]);
    if (d.task.first_prompt || (d.artifacts.first || []).length) tabs.push(["first", tr("det.tab.first"), (d.artifacts.first || []).length]);
    tabs.push(["plan", tr("det.tab.plan"), (d.artifacts.plan || []).length]);
    tabs.push(["review", tr("det.tab.review"), (d.artifacts.review || []).length]);
    tabs.push(["final", tr("det.tab.final"), (d.artifacts.final || []).length]);
    tabs.push(["log", tr("det.tab.log")], ["tokens", tr("det.tab.tokens")]);
    return tabs;
  },
  renderTabs() {
    const tabs = this.tabsList();
    if (!tabs.some(([key]) => key === this.tab)) this.tab = "desc";
    this.tabsNode.replaceChildren(...tabs.map(([key, label, count]) => h("button", {
      class: key === this.tab ? "active" : "", onclick: () => this.setTab(key) },
      label, count ? h("span", { class: "count", text: String(count) }) : null)));
  },
  setTab(tab) {
    this.tab = tab;
    history.replaceState(null, "", "#/tasks/" + enc(this.selected) + "/" + tab);
    this.renderTabs();
    this.renderTab(true);
  },
  async renderTab(full) {
    const d = this.detail;
    const tab = this.tab;
    const body = this.bodyNode;
    // File tabs scroll the file list and the selected file independently.
    body.classList.toggle("files-mode", ART_KINDS.includes(tab));
    try {
      if (tab === "desc") body.replaceChildren(await this.descTab());
      else if (tab === "info") body.replaceChildren(this.infoTab());
      else if (tab === "media") body.replaceChildren(await this.mediaTab());
      else if (ART_KINDS.includes(tab)) {
        // Keep the list position (and the reading position of the same file)
        // across re-renders; a newly selected file opens at its top.
        const oldList = body.querySelector(".file-list");
        const oldView = body.querySelector(".file-view");
        const listTop = oldList ? oldList.scrollTop : 0;
        const viewTop = oldView && oldView.dataset.path === this.file[tab] ? oldView.scrollTop : 0;
        body.replaceChildren(await this.filesTab(tab, full));
        const list = body.querySelector(".file-list");
        const view = body.querySelector(".file-view");
        if (list) list.scrollTop = listTop;
        if (view && view.dataset.path === this.file[tab]) view.scrollTop = viewTop;
      }
      else if (tab === "log") await this.logTab(full);
      else if (tab === "tokens") body.replaceChildren(this.tokensTab());
    } catch (err) {
      fail(err);
    }
    if (d !== this.detail) return;
  },
  async descTab() {
    const d = this.detail;
    const task = d.task;
    const wrap = h("div");
    wrap.appendChild(h("div", { class: "desc", text: task.description || tr("det.desc.empty") }));
    // Thumbnails of the images referenced with media/... tokens.
    const referenced = (d.media || []).filter((name) => /\.(png|jpe?g)$/i.test(name) && (task.description || "").includes("media/" + name));
    if (referenced.length) {
      const viewable = viewableMedia(d);
      const thumbs = h("div", { class: "thumbs" });
      wrap.appendChild(thumbs);
      for (const name of referenced) {
        mediaUrl(task.id, name).then((url) => thumbs.appendChild(h("img", { src: url, alt: name, title: "media/" + name, onclick: () => viewer.open(task.id, viewable, viewable.indexOf(name)) }))).catch(() => {});
      }
    }
    const extensions = Object.entries(task.extensions || this.detail.extensions || {}).sort((a, b) => Number(a[0]) - Number(b[0]));
    for (const [cycle, request] of extensions) {
      wrap.append(h("h3", { class: "small-text muted", text: tr("det.cycle", { n: cycle }) }), h("div", { class: "desc", text: request }));
    }
    return wrap;
  },
  infoTab() {
    const d = this.detail;
    const task = d.task;
    const yes = (flag) => tr(flag ? "web.ui.yes" : "web.ui.no");
    const refs = (d.references || []).map((ref) => ref.name + " → " + ref.path + (ref.description ? " (" + ref.description + ")" : "")).join("\n");
    const pairs = [
      [tr("web.ui.field_id"), task.id],
      [tr("web.ui.field_state"), d.state_label],
      [tr("web.ui.field_workdir"), task.workdir],
      [tr("web.ui.field_remote"), task.remote],
      [tr("web.ui.field_profile"), task.profile],
      [tr("web.ui.field_origin"), task.origin],
      [tr("web.ui.field_automode"), yes(task.automode)],
      [plain(tr("nt.confirm_plan")), yes(task.confirm_plan)],
      [plain(tr("nt.branch")), yes(task.create_branch)],
      [tr("web.ui.field_cycle"), String(task.cycle)],
      [tr("web.ui.field_iteration"), task.iteration + " / " + task.max_iterations],
      [tr("web.ui.field_branch"), task.branch],
      [tr("web.ui.field_tests"), task.test_command],
      [tr("web.ui.field_scheduled"), when(task.scheduled_at)],
      [tr("web.ui.field_parent"), task.parent_id],
      [tr("nt.repeat"), task.repeat_mode ? tr("det.repeat." + task.repeat_mode, { n: task.repeat_count, minutes: task.repeat_interval_minutes }) : ""],
      [tr("nt.plan_reuse"), task.repeat_mode ? tr("nt.plan_reuse." + task.plan_reuse) : ""],
      [tr("web.ui.field_failed_phase"), task.failed_phase ? tr("phase." + task.failed_phase) : ""],
      [tr("web.ui.field_hook"), task.hook_command ? task.hook_command + " [" + (task.hook_stages || "-") + "] " + task.hook_mode : ""],
      [plain(tr("nt.first_prompt")), task.first_prompt],
      [plain(tr("nt.final_prompt")), task.final_prompt],
      [tr("nt.refs.use_global"), yes(task.use_global_references)],
      [tr("nt.refs.use_project"), yes(task.use_project_references)],
      [tr("nt.refs.task"), refs],
      [tr("web.ui.field_tokens"), tokenPair(d.token_totals.input, d.token_totals.output)],
      [tr("web.ui.field_duration"), formatDuration(d.total_duration_seconds)],
      [tr("web.ui.field_created"), when(task.created_at)],
      [tr("web.ui.field_updated"), when(task.updated_at)],
    ];
    const dl = h("dl", { class: "info" });
    for (const [label, value] of pairs) {
      if (value === "" || value === null || value === undefined) continue;
      dl.append(h("dt", { text: label }), h("dd", { text: value }));
    }
    return dl;
  },
  async mediaTab() {
    const d = this.detail;
    const names = d.media || [];
    if (!names.length) return h("div", { class: "empty", text: tr("media.empty") });
    const viewable = viewableMedia(d);
    const grid = h("div", { class: "media-grid" });
    for (const name of names) {
      const item = h("div", { class: "media-item" });
      grid.appendChild(item);
      const kind = mediaKind(name);
      mediaUrl(d.task.id, name).then((url) => {
        let preview = null;
        if (kind === "image") preview = h("img", { src: url, alt: name });
        else if (kind === "video") preview = h("video", { src: url, preload: "metadata", muted: true });
        else if (kind === "audio") preview = h("div", { class: "media-audio-tile", title: name });
        if (preview) preview.addEventListener("click", () => viewer.open(d.task.id, viewable, viewable.indexOf(name)));
        item.prepend(preview || h("a", { href: url, download: name, text: tr("web.ui.download") }));
      }).catch(fail);
      item.appendChild(h("div", { class: "cap", text: "media/" + name }));
    }
    return grid;
  },
  async filesTab(kind, full) {
    const d = this.detail;
    // Cycle 1 lives in the phase root, extensions under ciclo-NN/.
    const cycleOf = (path) => { const match = /^ciclo-(\d+)\//.exec(path); return match ? Number(match[1]) : 1; };
    const files = (d.artifacts[kind] || []).slice().sort((a, b) => cycleOf(a) - cycleOf(b) || a.localeCompare(b));
    if (!files.length) return h("div", { class: "empty", text: tr("web.ui.no_files") });
    let selected = this.file[kind];
    if (!selected || !files.includes(selected)) {
      // Default: the newest file of the current cycle.
      const current = files.filter((path) => cycleOf(path) === d.task.cycle);
      selected = (current.length ? current : files)[(current.length ? current : files).length - 1];
      this.file[kind] = selected;
    }
    const list = h("div", { class: "file-list" });
    let lastGroup = null;
    for (const path of files) {
      const group = cycleOf(path);
      if (group !== lastGroup) {
        list.appendChild(h("div", { class: "group", text: tr("det.cycle", { n: group }) }));
        lastGroup = group;
      }
      list.appendChild(h("button", { class: path === selected ? "active" : "", text: path.split("/").pop(),
        onclick: () => { this.file[kind] = path; this.renderTab(true); } }));
    }
    const view = h("div", { class: "file-view", "data-path": selected });
    const data = await api("GET", taskPath(d.task.id, "/artifact?kind=" + kind + "&path=" + enc(selected)));
    view.append(h("div", { class: "toolbar" }, h("span", { class: "mono", text: kind + "/" + selected }),
      h("button", { class: "small", text: tr("web.ui.copy"), onclick: () => navigator.clipboard && navigator.clipboard.writeText(data.content).then(() => toast(tr("web.ui.copied"))) })),
    renderMarkdown(data.content));
    return h("div", { class: "files-layout" }, list, view);
  },
  async logTab(full) {
    const d = this.detail;
    const body = this.bodyNode;
    const data = await api("GET", taskPath(d.task.id, "/logs?limit=1000"));
    const entries = data.entries || [];
    const key = d.task.id + ":" + entries.length + ":" + (entries.length ? entries[entries.length - 1].text : "");
    if (!full && key === this.lastLogKey) return;
    this.lastLogKey = key;
    const stick = body.scrollHeight - body.scrollTop - body.clientHeight < 40;
    if (!entries.length) { body.replaceChildren(h("div", { class: "empty", text: tr("web.ui.no_log") })); return; }
    const styleClass = (style) => {
      if (/red/.test(style)) return "l-err";
      if (/magenta/.test(style)) return "l-info";
      if (/cyan/.test(style)) return "l-tool";
      if (/dim/.test(style)) return "l-dim";
      return "";
    };
    const log = h("div", { class: "log" }, entries.map((entry) => h("div", { class: styleClass(entry.style), text: entry.text })));
    body.replaceChildren(h("div", { class: "toolbar" }, tr("web.ui.log_lines", { count: entries.length }),
      h("button", { class: "small", text: tr("web.ui.refresh"), onclick: () => this.logTab(true) })), log);
    if (stick || full) body.scrollTop = body.scrollHeight;
  },
  tokensTab() {
    const d = this.detail;
    const total = d.token_totals || { input: 0, output: 0 };
    if (!total.input && !total.output) return h("div", { class: "empty", text: tr("det.tokens.empty") });
    const table = (rows) => h("table", null, h("tbody", null, rows.map(([label, input, output]) =>
      h("tr", null, h("td", { text: label }), h("td", { class: "num", text: "↑" + formatTokens(input) }), h("td", { class: "num", text: "↓" + formatTokens(output) })))));
    return h("div", { class: "tokens" },
      h("div", { text: tr("det.tokens.total", { input: formatTokens(total.input), output: formatTokens(total.output) }) }),
      h("h3", { text: tr("det.tokens.by_phase") }), table((d.tokens_by_phase || []).map((row) => [row.label, row.input, row.output])),
      h("h3", { text: tr("det.tokens.by_agent") }), table((d.tokens_by_agent || []).map((row) => [row.label, row.input, row.output])));
  },
  tick() {
    if (!this.detail) return;
    this.renderActivity();
    const now = Date.now();
    const running = this.detail.runtime && this.detail.runtime.running;
    if (running && now - (this.detail.fetchedAt || 0) > 2500) {
      this.detail.fetchedAt = now;  // throttle while the request is in flight
      this.loadDetail(false).then(() => { if (this.tab === "log") this.logTab(false); }).catch(() => {});
    }
  },
};

// ---------------------------------------------------------------- new task view
const newTaskView = {
  async mount() {
    const main = $("main");
    main.replaceChildren(h("div", { class: "page" }, h("div", { class: "empty", text: tr("web.ui.loading") })));
    let options, tasks;
    try {
      [options, tasks] = await Promise.all([store.getOptions(true), api("GET", "/api/v1/tasks")]);
    } catch (err) { fail(err); return; }
    main.replaceChildren(this.build(options, tasks.tasks || []));
  },
  unmount() {},
  build(options, tasks) {
    const def = options.defaults;
    const session = options.session && options.session.active;
    const name = h("input", { type: "text", placeholder: tr("nt.name.placeholder"), required: true });
    const desc = h("textarea", { rows: 8 });
    const files = attachmentsCollector([desc]);
    const issueSelect = h("select");
    const issueField = field(tr("nt.issue"), issueSelect);
    issueField.hidden = true;
    let issues = [];
    issueSelect.addEventListener("change", () => {
      const issue = issues.find((item) => String(item.number) === issueSelect.value);
      if (!issue) return;
      name.value = issue.title;
      desc.value = (issue.body || "").trim() || issue.title;
    });
    const workdir = h("input", { type: "text", list: "nt-dirs", value: session ? "" : def.workdir, class: "mono",
      placeholder: session ? tr("nt.workdir.remote.placeholder") : "" });
    const dirs = h("datalist", { id: "nt-dirs" });
    const loadDirs = debounce(async () => {
      if (session) return;
      try {
        const data = await api("GET", "/api/v1/fs/dirs?path=" + enc(workdir.value));
        dirs.replaceChildren(...data.dirs.map((dir) => option(dir, dir)));
      } catch (err) { /* autocompletion is best effort */ }
    }, 250);
    const loadIssues = debounce(async () => {
      if (session) return;
      try {
        const data = await api("GET", "/api/v1/issues?workdir=" + enc(workdir.value));
        issues = data.issues || [];
        issueSelect.replaceChildren(option("—", ""), ...issues.map((issue) => option("#" + issue.number + " " + issue.title, String(issue.number))));
        issueField.hidden = !issues.length;
      } catch (err) { issueField.hidden = true; }
    }, 700);
    workdir.addEventListener("input", () => { loadDirs(); loadIssues(); });
    loadDirs();
    loadIssues();
    const remoteInput = h("input", { type: "text", class: "mono", placeholder: tr("nt.remote.placeholder") });
    const profile = h("select", null, option(tr("nt.profile.default"), ""), options.profiles.map((p) => option(p.name, p.name)));
    const profileHelp = h("span", { class: "help" });
    const defaultSummary = (options.defaults && options.defaults.roles_summary) || "";
    profileHelp.textContent = defaultSummary;  // the general option starts preselected
    profile.addEventListener("change", () => {
      const chosen = options.profiles.find((p) => p.name === profile.value);
      profileHelp.textContent = chosen ? chosen.summary : defaultSummary;
    });
    const schedule = h("input", { type: "datetime-local" });
    const parent = h("select", null, option(tr("web.ui.parent_none"), ""), tasks.map((task) => option(task.name + " (" + task.id + ")", task.id)));
    const repeat = h("select", null, option(tr("nt.repeat.none"), ""), option(tr("nt.repeat.interval"), "interval"), option(tr("nt.repeat.infinite"), "infinite"));
    const minutes = h("input", { type: "number", min: 1, value: 60 });
    const planReuse = h("select", null, ["reuse", "replan", "reevaluate"].map((value) => option(tr("nt.plan_reuse." + value), value)));
    const minutesField = field(tr("nt.repeat.interval_minutes"), minutes);
    const reuseField = field(tr("nt.plan_reuse"), planReuse);
    const syncRepeat = () => { minutesField.hidden = repeat.value !== "interval"; reuseField.hidden = !repeat.value; };
    repeat.addEventListener("change", syncRepeat);
    syncRepeat();
    const tests = h("input", { type: "text", class: "mono", value: def.test_command, placeholder: tr("nt.tests.placeholder") });
    const firstPrompt = h("textarea", { rows: 3 });
    firstPrompt.value = def.first_prompt;
    const finalPrompt = h("textarea", { rows: 3 });
    finalPrompt.value = def.final_prompt;
    const automode = check(tr("nt.automode"), def.automode);
    const confirmPlan = check(tr("nt.confirm_plan"), def.confirm_plan);
    const branch = check(tr("nt.branch"), def.create_branch);
    const startNow = check(tr("web.ui.start_now"), false);
    const hook = h("input", { type: "text", class: "mono", placeholder: tr("nt.hook.placeholder") });
    const stageBoxes = options.hook_stages.map((stage) => [stage, check(tr("hook.stage." + stage), false)]);
    const hookBoth = check(tr("nt.hook.both"), false);
    const useGlobal = check(tr("nt.refs.use_global"), true);
    const useProject = check(tr("nt.refs.use_project"), true);
    const refs = listEditor([
      { key: "name", label: tr("refs.name") },
      { key: "description", label: tr("refs.description") },
      { key: "path", label: tr("refs.path") },
    ], [], tr("refs.empty"));
    const submit = h("button", { type: "submit", class: "primary", text: tr("common.create") });
    const form = h("form", { class: "page-inner" },
      h("h2", { text: tr("nt.title") }),
      h("p", { class: "subtitle", text: tr("web.ui.new_subtitle") }),
      h("div", { class: "card" }, h("div", { class: "form-grid" },
        field(tr("nt.name"), name, { full: true }),
        issueField,
        field(tr("nt.description"), desc, { full: true }),
        h("div", { class: "field full" }, h("span", { class: "lbl", text: tr("web.ui.attachments") }), files.node),
        session
          ? field(tr("nt.workdir.remote"), workdir, { full: true, help: options.session.label })
          : field(tr("nt.workdir"), h("div", null, workdir, dirs), { full: true }),
        session ? null : field(tr("nt.remote"), remoteInput, { full: true }),
        options.profiles.length ? field(tr("nt.profile"), h("div", null, profile, profileHelp), { full: true }) : null)),
      h("div", { class: "card" }, h("h3", { text: tr("web.ui.section_pipeline") }),
        h("div", { class: "checks" }, automode.node, confirmPlan.node, branch.node),
        h("div", { class: "form-grid", style: { marginTop: "12px" } },
          field(tr("nt.tests"), tests, { full: true }),
          field(tr("nt.first_prompt"), firstPrompt, { full: true }),
          field(tr("nt.final_prompt"), finalPrompt, { full: true }))),
      h("div", { class: "card" }, h("h3", { text: tr("web.ui.section_schedule") }),
        h("div", { class: "form-grid" },
          field(tr("nt.schedule"), schedule),
          field(tr("nt.parent"), parent, { help: tr("web.ui.parent_help") }),
          field(tr("nt.repeat"), repeat),
          minutesField, reuseField)),
      h("div", { class: "card" }, h("h3", { text: plain(tr("nt.hook")) }),
        field("", hook, { help: tr("hook.help") }),
        h("div", { class: "checks" }, stageBoxes.map(([, item]) => item.node)),
        h("div", { class: "checks", style: { marginTop: "6px" } }, hookBoth.node)),
      h("div", { class: "card" }, h("h3", { text: tr("cfg.references") }),
        h("p", { class: "help", text: tr("refs.warning") }),
        h("div", { class: "checks" }, useGlobal.node, useProject.node),
        h("div", { class: "lbl small-text muted", style: { margin: "10px 0 4px" }, text: tr("nt.refs.task") }), refs.node),
      h("div", { class: "form-actions" }, startNow.node, h("span", { class: "grow" }),
        h("button", { type: "button", text: tr("common.cancel"), onclick: () => { location.hash = "#/tasks"; } }), submit));
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!name.value.trim()) { toast(tr("nt.error.name_required"), "error"); return; }
      submit.disabled = true;
      try {
        const payload = {
          name: name.value.trim(),
          description: desc.value.trim(),
          workdir: workdir.value.trim(),
          automode: automode.box.checked,
          confirm_plan: confirmPlan.box.checked,
          create_branch: branch.box.checked,
          test_command: tests.value.trim(),
          first_prompt: firstPrompt.value.trim(),
          final_prompt: finalPrompt.value.trim(),
          scheduled_at: schedule.value ? schedule.value.replace("T", " ") : "",
          parent_id: parent.value,
          repeat_mode: repeat.value,
          repeat_interval_minutes: Number(minutes.value) || 0,
          plan_reuse: planReuse.value,
          hook_command: hook.value.trim(),
          hook_stages: stageBoxes.filter(([, item]) => item.box.checked).map(([stage]) => stage),
          hook_mode: hookBoth.box.checked ? "both" : "override",
          use_global_references: useGlobal.box.checked,
          use_project_references: useProject.box.checked,
          references: refs.values().filter((ref) => (ref.name || "").trim() && (ref.path || "").trim()),
        };
        if (!session && remoteInput.value.trim()) payload.remote = remoteInput.value.trim();
        if (profile.value) payload.profile = profile.value;
        if (files.count()) payload.attachments = await files.payload();
        if (repeat.value && !automode.box.checked) toast(tr("nt.repeat.forces_automode"), "warn");
        const data = await api("POST", "/api/v1/tasks", payload);
        for (const item of data.transcriptions || []) {
          if (item.error) toast(tr("web.ui.transcription_failed") + " " + item.name + ": " + item.error, "warn");
        }
        if (startNow.box.checked && !payload.parent_id) await api("POST", taskPath(data.task.id, "/start"));
        toast(tr("web.ui.created"));
        location.hash = "#/tasks/" + enc(data.task.id);
      } catch (err) {
        fail(err);
      } finally {
        submit.disabled = false;
      }
    });
    setTimeout(() => name.focus(), 0);
    return h("div", { class: "page" }, form);
  },
};

// ---------------------------------------------------------------- reports view
const reportsView = {
  mount() {
    this.from = h("input", { type: "date" });
    this.to = h("input", { type: "date" });
    const periodButton = (label, period) => h("button", { class: "small", text: tr(label), onclick: () => this.load({ period }) });
    this.body = h("div");
    $("main").replaceChildren(h("div", { class: "page" }, h("div", { class: "page-inner" },
      h("h2", { text: tr("tasks.bind.reports") }),
      h("p", { class: "subtitle", text: tr("reports.subtitle") }),
      h("div", { class: "toolbar" },
        periodButton("reports.period.today", "day"), periodButton("reports.period.week", "week"), periodButton("reports.period.month", "month"),
        this.from, this.to,
        h("button", { class: "small primary", text: tr("reports.period.apply"), onclick: () => this.load({ from: this.from.value, to: this.to.value }) })),
      this.body)));
    this.load({ period: "day" });
  },
  unmount() {},
  async load(query) {
    let data;
    try {
      const params = new URLSearchParams(Object.entries(query).filter(([, value]) => value));
      data = await api("GET", "/api/v1/reports?" + params.toString());
    } catch (err) { fail(err); return; }
    this.from.value = data.from;
    this.to.value = data.to;
    const stat = (label, value) => h("div", { class: "stat" }, h("div", { class: "k", text: label }), h("div", { class: "v", text: value }));
    const range = data.from === data.to ? tr("reports.range.day", { day: data.from }) : tr("reports.range", { start: data.from, end: data.to });
    const maxDay = Math.max(1, ...data.by_day.map((row) => row.input + row.output));
    const section = (title, head, rows) => h("div", { class: "card" }, h("h3", { text: title }),
      rows.length ? h("div", { class: "table-wrap" }, h("table", null, h("thead", null, h("tr", null, head.map(([label, cls]) => h("th", { class: cls || "", text: label })))), h("tbody", null, rows)))
        : h("div", { class: "help", text: tr("reports.empty") }));
    const num = (text) => h("td", { class: "num", text });
    this.body.replaceChildren(
      h("p", { class: "muted", text: range }),
      h("div", { class: "stats" },
        stat(tr("reports.col.tokens_in"), formatTokens(data.tokens.input)),
        stat(tr("reports.col.tokens_out"), formatTokens(data.tokens.output)),
        stat(tr("reports.col.time"), formatDuration(data.seconds)),
        stat(tr("reports.col.tasks"), String(data.tasks)),
        stat(tr("reports.col.project"), String(data.projects))),
      section(tr("reports.section.by_day"),
        [[tr("reports.col.date")], [tr("reports.col.tokens_in"), "num"], [tr("reports.col.tokens_out"), "num"], [tr("reports.col.time"), "num"], [""]],
        data.by_day.map((row) => h("tr", null, h("td", { text: row.date }), num(formatTokens(row.input)), num(formatTokens(row.output)),
          num(row.seconds ? formatDuration(row.seconds) : ""),
          h("td", { class: "bar-cell" }, h("div", { class: "bar", style: { width: (100 * (row.input + row.output) / maxDay).toFixed(1) + "%" } }))))),
      section(tr("reports.section.by_model"),
        [[tr("reports.col.model")], [tr("reports.col.tokens_in"), "num"], [tr("reports.col.tokens_out"), "num"]],
        data.by_model.map((row) => h("tr", null, h("td", { text: row.label }), num(formatTokens(row.input)), num(formatTokens(row.output))))),
      section(tr("reports.section.by_project"),
        [[tr("reports.col.project")], [tr("reports.col.tokens_in"), "num"], [tr("reports.col.tokens_out"), "num"], [tr("reports.col.time"), "num"], [tr("reports.col.tasks"), "num"]],
        data.by_project.map((row) => h("tr", null, h("td", { class: "mono small-text", text: row.project }), num(formatTokens(row.input)), num(formatTokens(row.output)),
          num(row.seconds ? formatDuration(row.seconds) : ""), num(String(row.tasks))))));
  },
};

// ---------------------------------------------------------------- settings view
const settingsView = {
  async mount() {
    $("main").replaceChildren(h("div", { class: "page" }));
    let data;
    try {
      [data] = await Promise.all([api("GET", "/api/v1/settings"), store.getOptions().catch(() => null)]);
    } catch (err) { fail(err); return; }
    $("main").replaceChildren(this.build(data));
  },
  unmount() {},
  build(data) {
    const roles = rolesEditor(data.roles);
    const am = data.automode;
    const amEnabled = check(tr("cfg.am.enabled"), am.enabled);
    const amBranch = check(tr("cfg.am.branch"), am.create_branch);
    const amConfirm = check(tr("cfg.am.confirm_plan"), am.confirm_plan);
    const maxIter = h("input", { type: "number", min: 1, value: am.max_iterations });
    const tests = h("input", { type: "text", class: "mono", value: am.test_command, placeholder: tr("nt.tests.placeholder") });
    const autoUpdate = check(tr("cfg.upd.enabled"), data.auto_update);
    const selfUpdate = check(tr("cfg.self_update.enabled"), data.self_update);
    const firstPrompt = h("textarea", { rows: 3 });
    firstPrompt.value = data.first_prompt;
    const finalPrompt = h("textarea", { rows: 3 });
    finalPrompt.value = data.final_prompt;
    const hookCommand = h("input", { type: "text", class: "mono", value: data.hook.command, placeholder: tr("hook.placeholder") });
    const hookStages = data.hook_stages.map((stage) => [stage, check(tr("hook.stage." + stage), data.hook.stages.includes(stage))]);
    const editorEnabled = check(tr("cfg.editor.enabled"), data.editor.enabled);
    const editorName = h("select", null, option("—", ""), data.editors_available.map((name) => option(name, name, name === data.editor.editor)));
    if (data.editor.editor && !data.editors_available.includes(data.editor.editor)) editorName.appendChild(option(data.editor.editor, data.editor.editor, true));
    const editorMode = h("select", null, ["window", "split", "none"].map((mode) => option(tr("cfg.editor.mode." + mode), mode, mode === data.editor.mode)));
    const editorSide = h("select", null, ["left", "right"].map((side) => option(tr("cfg.editor.side." + side), side, side === data.editor.side)));
    const language = h("select", null, option("English", "en", data.language === "en"), option("Español", "es", data.language === "es"));
    const promptLanguage = h("select", null, option(tr("cfg.language.prompts.same"), "", !data.prompt_language),
      option("English", "en", data.prompt_language === "en"), option("Español", "es", data.prompt_language === "es"));
    const workspaces = h("input", { type: "text", class: "mono", value: data.workspaces.join(", "), placeholder: "~/code, ~/work" });
    const refs = listEditor([
      { key: "name", label: tr("refs.name") },
      { key: "description", label: tr("refs.description") },
      { key: "path", label: tr("refs.path") },
    ], data.references, tr("refs.empty"));
    const triggers = listEditor([
      { key: "name", label: tr("refs.col.name") },
      { key: "description", label: tr("trig.description") },
      { key: "phases", label: tr("trig.col.phases"), type: "phases", stages: data.trigger_stages },
      { key: "timing", label: tr("trig.col.timing"), type: "select", default: "after", options: data.trigger_timings.map((value) => [tr("trig.timing." + value), value]) },
      { key: "workdir", label: tr("web.ui.col_workdir"), placeholder: tr("web.ui.same_workdir") },
    ], data.triggers, tr("web.ui.no_triggers"));
    const profiles = this.profilesEditor(data.profiles);
    const tg = data.telegram;
    const tgEnabled = check(tr("cfg.tg.enabled"), tg.enabled);
    const tgConfirm = check(tr("cfg.tg.confirm"), tg.confirm_create);
    const tgGroupAll = check(tr("cfg.tg.group_all"), tg.group_all);
    const secret = (isSet, placeholder) => {
      const input = h("input", { type: "password", autocomplete: "new-password", placeholder: isSet ? tr("web.ui.secret_keep") : (placeholder || "") });
      const clear = check(tr("web.ui.secret_clear"), false);
      return { input, clear, node: h("div", null, input, isSet ? h("div", { class: "row" }, h("span", { class: "secret-state", text: "✓ " + tr("web.ui.secret_set") }), clear.node) : null) };
    };
    const tgToken = secret(tg.bot_token_set, tr("cfg.tg.token.placeholder"));
    const tgChats = h("input", { type: "text", value: tg.allowed_chat_ids });
    const tgParserCli = h("select", null, option(tr("cfg.tg.parser.default"), ""), data.known_clis.map((cli) => option(cli, cli, cli === tg.parser_cli)));
    const tgParserModel = h("input", { type: "text", value: tg.parser_model });
    const tgWorkdir = h("input", { type: "text", class: "mono", value: tg.default_workdir });
    const sttUrl = h("input", { type: "text", class: "mono", value: tg.stt_url });
    const sttKey = secret(tg.stt_key_set, tr("cfg.tg.stt.key.placeholder"));
    const sttModel = h("input", { type: "text", value: tg.stt_model });
    const ttsEnabled = check(tr("cfg.tg.tts.enabled"), tg.tts_enabled);
    const ttsUrl = h("input", { type: "text", class: "mono", value: tg.tts_url });
    const ttsKey = secret(tg.tts_key_set, "");
    const ttsModel = h("input", { type: "text", value: tg.tts_model });
    const ttsVoice = h("input", { type: "text", value: tg.tts_voice });
    const apiEnabled = check(tr("cfg.api.enabled"), data.api.enabled);
    const apiHost = h("input", { type: "text", class: "mono", value: data.api.host });
    const apiPort = h("input", { type: "number", min: 1, max: 65535, value: data.api.port });
    const apiTokens = secret(data.api.tokens_set, tr("cfg.api.tokens.placeholder"));
    const save = h("button", { type: "submit", class: "primary", text: tr("common.save") });
    const card = (title, ...children) => h("div", { class: "card" }, h("h3", { text: plain(title) }), ...children);
    const form = h("form", { class: "page-inner" },
      h("h2", { text: tr("tasks.bind.config") }),
      h("p", { class: "subtitle mono small-text", text: data.path }),
      card(tr("cfg.roles"), roles.node),
      card(tr("cfg.automode"), h("div", { class: "checks" }, amEnabled.node, amBranch.node, amConfirm.node),
        h("div", { class: "form-grid", style: { marginTop: "12px" } }, field(tr("cfg.max_iter"), maxIter), field(tr("cfg.tests"), tests))),
      card(tr("web.ui.section_prompts"), h("div", { class: "form-grid" },
        field(tr("cfg.first_prompt"), firstPrompt, { full: true }), field(tr("cfg.final_prompt"), finalPrompt, { full: true }))),
      card(tr("cfg.hook"), field(tr("cfg.hook.command"), hookCommand, { help: tr("hook.help") }),
        h("div", { class: "lbl small-text muted", text: tr("cfg.hook.stages") }), h("div", { class: "checks" }, hookStages.map(([, item]) => item.node))),
      card(tr("cfg.updates"), h("div", { class: "checks" }, autoUpdate.node, selfUpdate.node)),
      card(tr("cfg.language"), h("p", { class: "help", text: tr("cfg.language.help") }),
        h("div", { class: "form-grid" }, field(tr("cfg.language.gui"), language), field(tr("cfg.language.prompts"), promptLanguage))),
      card(tr("cfg.workspaces"), h("p", { class: "help", text: tr("cfg.workspaces.help") }), field(tr("cfg.workspaces.label"), workspaces)),
      card(tr("cfg.editor"), h("div", { class: "checks" }, editorEnabled.node),
        h("div", { class: "form-grid", style: { marginTop: "12px" } }, field(tr("cfg.editor.name"), editorName), field(tr("cfg.editor.mode"), editorMode), field(tr("cfg.editor.side"), editorSide))),
      card(tr("cfg.references"), h("p", { class: "help", text: tr("refs.warning") }), refs.node),
      card(tr("cfg.triggers"), h("p", { class: "help", text: tr("trig.help") }), triggers.node),
      card(tr("cfg.profiles"), h("p", { class: "help", text: tr("cfg.profiles.help") }), profiles.node),
      card(tr("cfg.telegram"), h("p", { class: "help", text: tr("web.ui.restart_notice") }),
        h("div", { class: "checks" }, tgEnabled.node, tgConfirm.node, tgGroupAll.node),
        h("div", { class: "form-grid", style: { marginTop: "12px" } },
          field(tr("cfg.tg.token"), tgToken.node), field(tr("cfg.tg.chats"), tgChats),
          field(tr("cfg.tg.parser"), tgParserCli), field(tr("cfg.tg.parser_model"), tgParserModel),
          field(tr("cfg.tg.workdir"), tgWorkdir, { full: true }),
          h("div", { class: "full lbl small-text muted", text: tr("cfg.tg.stt") }),
          field(tr("cfg.tg.stt.url"), sttUrl, { full: true }), field(tr("cfg.tg.stt.key"), sttKey.node), field(tr("cfg.tg.stt.model"), sttModel),
          h("div", { class: "full lbl small-text muted", text: tr("cfg.tg.tts") }),
          h("div", { class: "full" }, ttsEnabled.node),
          field(tr("cfg.tg.tts.url"), ttsUrl, { full: true }), field(tr("cfg.tg.tts.key"), ttsKey.node), field(tr("cfg.tg.tts.model"), ttsModel),
          field(tr("cfg.tg.tts.voice"), ttsVoice))),
      card(tr("cfg.api"), h("p", { class: "help", text: tr("cfg.api.tokens.help") + " " + tr("web.ui.restart_notice") }),
        h("div", { class: "checks" }, apiEnabled.node),
        h("div", { class: "form-grid", style: { marginTop: "12px" } },
          field(tr("cfg.api.host"), apiHost), field(tr("cfg.api.port"), apiPort), field(tr("cfg.api.tokens"), apiTokens.node, { full: true }))),
      h("div", { class: "form-actions" }, save));
    const secretValue = (target, key, item) => {
      if (item.clear.box.checked) target[key + "_clear"] = true;
      else if (item.input.value.trim()) target[key] = item.input.value.trim();
    };
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const telegram = {
        enabled: tgEnabled.box.checked, confirm_create: tgConfirm.box.checked, group_all: tgGroupAll.box.checked,
        allowed_chat_ids: tgChats.value, parser_cli: tgParserCli.value, parser_model: tgParserModel.value,
        default_workdir: tgWorkdir.value, stt_url: sttUrl.value, stt_model: sttModel.value,
        tts_enabled: ttsEnabled.box.checked, tts_url: ttsUrl.value, tts_model: ttsModel.value, tts_voice: ttsVoice.value,
      };
      secretValue(telegram, "bot_token", tgToken);
      secretValue(telegram, "stt_key", sttKey);
      secretValue(telegram, "tts_key", ttsKey);
      const apiSection = { enabled: apiEnabled.box.checked, host: apiHost.value, port: Number(apiPort.value) };
      secretValue(apiSection, "tokens", apiTokens);
      const payload = {
        roles: roles.values(),
        automode: { enabled: amEnabled.box.checked, create_branch: amBranch.box.checked, confirm_plan: amConfirm.box.checked,
          max_iterations: Number(maxIter.value), test_command: tests.value },
        auto_update: autoUpdate.box.checked, self_update: selfUpdate.box.checked,
        first_prompt: firstPrompt.value, final_prompt: finalPrompt.value,
        hook: { command: hookCommand.value, stages: hookStages.filter(([, item]) => item.box.checked).map(([stage]) => stage) },
        editor: { enabled: editorEnabled.box.checked, editor: editorName.value, mode: editorMode.value, side: editorSide.value },
        language: language.value, prompt_language: promptLanguage.value,
        workspaces: workspaces.value.split(",").map((item) => item.trim()).filter(Boolean),
        references: refs.values().filter((ref) => (ref.name || "").trim() && (ref.path || "").trim()),
        triggers: triggers.values().filter((item) => (item.name || "").trim() && (item.description || "").trim()),
        profiles: profiles.values(),
        telegram, api: apiSection,
      };
      save.disabled = true;
      try {
        await api("POST", "/api/v1/settings", payload);
        store.options = null;
        toast(tr("cfg.saved"));
        if (language.value !== BOOT.lang) setTimeout(() => location.reload(), 600);
        else this.mount();
      } catch (err) {
        fail(err);
      } finally {
        save.disabled = false;
      }
    });
    return h("div", { class: "page" }, form);
  },
  profilesEditor(initial) {
    const items = (initial || []).map((profile) => ({ name: profile.name, roles: profile.roles }));
    const wrap = h("div");
    const editors = [];
    const sync = () => { editors.forEach(([entry, editor]) => { entry.roles = editor.values(); }); };
    const render = () => {
      editors.length = 0;
      wrap.replaceChildren(...items.map((item, index) => {
        const nameInput = h("input", { type: "text", value: item.name, placeholder: tr("web.ui.profile_name") });
        nameInput.addEventListener("input", () => { item.name = nameInput.value; });
        const editor = rolesEditor(item.roles);
        editors.push([item, editor]);
        return h("div", { class: "profile-card" },
          h("div", { class: "row", style: { marginBottom: "8px" } }, field(tr("web.ui.profile_name"), nameInput), h("span", { class: "grow" }),
            h("button", { type: "button", class: "small danger", text: tr("web.ui.remove"), onclick: () => { sync(); items.splice(index, 1); render(); } })),
          editor.node);
      }), h("button", { type: "button", class: "small", text: "+ " + tr("web.ui.add_profile"), onclick: () => {
        sync();
        items.push({ name: "", roles: (store.options && store.options.defaults.roles) || {} });
        render();
      } }));
    };
    render();
    return {
      node: wrap,
      values: () => editors.map(([item, editor]) => ({ name: (item.name || "").trim(), roles: editor.values() })).filter((item) => item.name),
    };
  },
};

// ---------------------------------------------------------------- router
const router = {
  current: null,
  views: { tasks: tasksView, new: newTaskView, reports: reportsView, settings: settingsView },
  parse() {
    const parts = (location.hash.replace(/^#\/?/, "") || "tasks").split("/").map(decodeURIComponent);
    const name = this.views[parts[0]] ? parts[0] : "tasks";
    return { name, params: parts.slice(1) };
  },
  reload() {
    // Remount the current view (e.g. after entering the access token).
    if (this.current) this.current.view.unmount();
    this.current = null;
    this.render();
  },
  render() {
    const { name, params } = this.parse();
    if (this.current && this.current.name === "tasks" && name === "tasks") {
      // Same view: only the selection changes (keeps filters and scroll).
      const view = tasksView;
      const id = params[0] || null;
      if (params[1]) view.tab = params[1];
      if (id !== view.selected) {
        if (!params[1]) view.tab = "desc";
        view.selected = id;
        view.detail = null;
        view.headNode = null;
        view.lastLogKey = "";
        if (!id) {
          view.showList();
        } else {
          view.showDetail();
          view.loadDetail(true).catch(fail);
        }
      }
      return;
    }
    if (this.current) this.current.view.unmount();
    const view = this.views[name];
    this.current = { name, view };
    document.querySelectorAll("#nav a").forEach((link) => link.classList.toggle("active", link.dataset.route === name));
    view.mount(params);
  },
};

// ---------------------------------------------------------------- boot
function boot() {
  document.title = tr("web.ui.title");
  $("version").textContent = "v" + BOOT.version;
  $("nav-tasks").textContent = tr("web.nav.tasks");
  $("nav-new").textContent = tr("web.nav.new");
  $("nav-reports").textContent = tr("tasks.bind.reports");
  $("nav-settings").textContent = tr("tasks.bind.config");
  $("logout").textContent = tr("web.ui.logout");
  $("logout").addEventListener("click", () => {
    storeToken("");
    const socket = live.ws;
    live.ws = null;
    if (socket) socket.close();
    askToken();
  });
  initToken();
  setStatus("polling");
  store.getOptions().catch(() => null);
  window.addEventListener("hashchange", () => router.render());
  router.render();
}
boot();
