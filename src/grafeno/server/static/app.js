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
const TASK_COL_KEY = "grafeno.taskColWidth";
const TASK_COL_MIN = 160;  // px: same floor as the task cells (app.css)
const TASK_COL_MAX = 4000; // px: caps absurd stored values
const TASK_COL_STEP = 20;  // px per ArrowLeft/ArrowRight on the resize handle
const CS_ENTER_KEY = "grafeno.csEnterSends";  // CLI session composer: Enter sends

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
function formatCountdown(seconds) {
  const total = Math.floor(Math.max(0, Math.floor(seconds || 0)) / 60);
  const days = Math.floor(total / 1440), hours = Math.floor((total % 1440) / 60), minutes = total % 60;
  const pad = (n) => String(n).padStart(2, "0");
  if (days) return days + "d " + pad(hours) + "h";
  if (hours) return hours + "h " + pad(minutes) + "m";
  return minutes + "m";
}
// Pending start of a task (server payload "schedule"), in the server's
// calendar: "today"/"tomorrow" use the UTC offset carried by at_iso.
function scheduleText(s, compact) {
  if (!s || !s.status) return "";
  const now = Date.now();
  const target = s.at_iso ? Date.parse(s.at_iso) : NaN;
  const known = !Number.isNaN(target);
  let stamp = "";
  if (known) {
    const m = /([+-])(\d\d):(\d\d)$/.exec(s.at_iso);
    const offset = m ? (m[1] === "-" ? -1 : 1) * (Number(m[2]) * 60 + Number(m[3])) : 0;
    const dayOf = (ms) => Math.floor((ms + offset * 60000) / 86400000);
    const delta = dayOf(target) - dayOf(now);
    const day = delta === 0 ? tr("det.sched.today") : delta === 1 ? tr("det.sched.tomorrow") : delta === -1 ? tr("det.sched.yesterday") : "";
    if (compact) stamp = when(s.at) + (day ? " (" + day + ")" : "");
    else {
      const secs = (target - now) / 1000;
      const rel = secs > 0 ? tr("det.sched.in", { d: formatCountdown(secs) }) : tr("det.sched.ago", { d: formatCountdown(-secs) });
      stamp = when(s.at) + " " + s.utc_offset + " (" + [day, rel].filter(Boolean).join(", ") + ")";
    }
  }
  if (s.status === "waiting_parent") {
    if (!s.parent_name) return tr("det.sched.parent_missing");
    if (known && target > now) return tr("det.sched.waiting_parent_at", { name: s.parent_name, at: stamp });
    return tr("det.sched.waiting_parent", { name: s.parent_name });
  }
  if (!known) return "";
  // Recomputed client side: a "future" payload turns overdue without a reload.
  if (target <= now) return tr("det.sched.overdue", { at: stamp });
  return compact ? stamp : tr("det.scheduled", { at: stamp });
}
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
// CSS class of a log line from its Rich style (task log tab).
function logStyleClass(style) {
  if (/red/.test(style)) return "l-err";
  if (/magenta/.test(style)) return "l-info";
  if (/cyan/.test(style)) return "l-tool";
  if (/dim/.test(style)) return "l-dim";
  return "";
}
// CSS class of a CLI session transcript entry by kind.
const SESSION_KIND_CLASS = { text: "", tool: "l-tool", info: "l-dim", error: "l-err" };
const taskPath = (id, rest) => "/api/v1/tasks/" + enc(id) + (rest || "");
const sessionPath = (id, rest) => "/api/v1/sessions/" + enc(id) + (rest || "");

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
      if (Date.now() - searchSelectEscapeAt < 100) { event.preventDefault(); return; }  // Escape closed a searchSelect panel
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
const IMG_EXT = ["png", "jpg", "jpeg", "gif", "webp"];
const VID_EXT = ["mp4", "webm", "m4v", "mov"];
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
// Files above PREVIEW_MAX_BYTES are never fetched automatically.
function viewableMedia(detail) {
  const sizes = new Map((detail.attachments || []).map((a) => [a.file, a.size]));
  return (detail.media || []).filter((name) => mediaKind(name) !== "file" && !((sizes.get(name) || 0) > PREVIEW_MAX_BYTES));
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
const MODELS_TTL_MS = 10 * 60 * 1000;  // same lifetime as the server cache
const store = {
  options: null,
  models: null,
  modelsAt: 0,
  modelsPromise: null,
  async getOptions(force) {
    if (!this.options || force) this.options = await api("GET", "/api/v1/options");
    return this.options;
  },
  getModels(refresh) {
    const stale = this.models && Date.now() - this.modelsAt > MODELS_TTL_MS;
    if (refresh || (!this.models && !this.modelsPromise) || (stale && !this.modelsPromise)) {
      this.modelsPromise = api("GET", "/api/v1/models" + (refresh ? "?refresh=1" : ""))
        .then((data) => { this.models = data; this.modelsAt = Date.now(); return data; })
        .finally(() => { this.modelsPromise = null; });
    }
    return this.modelsPromise || Promise.resolve(this.models);
  },
};
// Opens the configured web editor on a project folder in a new tab. The tab is
// opened synchronously (inside the click) so popup blockers keep the user
// gesture; it is pointed at the editor once the server returns the URL.
// target: { workdir } for a project or { task_id } for a task.
async function openEditor(target) {
  const tab = window.open("", "_blank");
  if (tab) tab.opener = null;  // same isolation as rel="noopener"
  try {
    const data = await api("POST", "/api/v1/editor/open", Object.assign({ host: location.hostname }, target));
    if (!/^https?:\/\//i.test(data.url || "")) throw new ApiFailure(0, "invalid editor URL");
    if (data.settings_error) toast(tr("web.editor.settings_failed", { error: data.settings_error }), "error");
    if (data.running === false) toast(tr("web.editor.not_running", { name: data.name }), "error");
    if (tab) tab.location.href = data.url;
    else if (!window.open(data.url, "_blank", "noopener")) toast(tr("web.editor.blocked", { url: data.url }), "error");
  } catch (err) {
    if (tab) tab.close();
    fail(err);
  }
}
function editorInfo() {
  return (store.options && store.options.editor) || { available: false, name: "" };
}
const mediaCache = new Map();
async function mediaUrl(taskId, name) {
  const key = taskId + "/" + name;
  if (!mediaCache.has(key)) {
    const blob = await api("GET", taskPath(taskId, "/media?name=" + enc(name)));
    mediaCache.set(key, URL.createObjectURL(blob));
  }
  return mediaCache.get(key);
}

// Saves a fetched blob as a file through a temporary object URL.
function saveBlob(blob, name) {
  const url = URL.createObjectURL(blob);
  h("a", { href: url, download: name }).click();
  setTimeout(() => URL.revokeObjectURL(url), 60000);  // the browser may still be reading it
}

// ---------------------------------------------------------------- live events
const live = {
  ws: null,
  ok: false,
  listeners: new Set(),
  sessionListeners: new Set(),  // CLI session events (session.changed / session.deleted)
  connect() {
    if (this.ws) { const old = this.ws; this.ws = null; try { old.close(); } catch (e) { /* closed */ } }
    const scheme = location.protocol === "https:" ? "wss://" : "ws://";
    const query = auth.token ? "?token=" + enc(auth.token) : "";
    let socket;
    try { socket = new WebSocket(scheme + location.host + "/api/v1/ws" + query); } catch (e) { return; }
    this.ws = socket;
    socket.onopen = () => {
      socket.send(JSON.stringify({ id: 1, method: "subscribe", params: { topics: ["tasks", "sessions"] } }));
      this.ok = true;
      setStatus("live");
    };
    socket.onmessage = (message) => {
      let data;
      try { data = JSON.parse(message.data); } catch (e) { return; }
      if (data.event === "task.changed" && data.task) this.listeners.forEach((fn) => fn(data.task));
      if (data.event === "session.changed" || data.event === "session.deleted") this.sessionListeners.forEach((fn) => fn(data));
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
// Effort levels of a model in a {model: [levels]} catalogue (mirror of
// drivers.model_levels): tolerates a "#variant" suffix and a missing or
// extra provider prefix when the bare name matches exactly one key.
function modelLevels(map, model) {
  if (!model || !map) return [];
  if (map[model]) return map[model].slice();
  const bare = model.split("#")[0];
  if (map[bare]) return map[bare].slice();
  const tail = bare.split("/").pop();
  const matches = Object.keys(map).filter((key) => key.split("/").pop() === tail);
  return matches.length === 1 ? map[matches[0]].slice() : [];
}

// Rows of cli + model (with filter) + effort per role, fed by /api/v1/models.
function rolesEditor(initial, opts) {
  opts = opts || {};
  const roleList = opts.roles || ROLES;
  const known = opts.clis || (store.options && store.options.known_clis) || ["opencode", "kimi", "codex", "claude", "cursor", "minimax"];
  const values = {};
  for (const role of roleList) values[role] = Object.assign({ cli: opts.clis ? known[0] : "opencode", model: "", effort: "" }, (initial || {})[role]);
  const status = h("div", { class: "help", text: tr("cfg.models.loading") });
  const grid = h("div", { class: "role-grid" },
    h("span", { class: "hdr", text: tr("web.ui.col_role") }), h("span", { class: "hdr", text: "CLI" }),
    h("span", { class: "hdr", text: tr("web.ui.col_model") }), h("span", { class: "hdr", text: tr("web.ui.col_effort") }));
  const rows = {};
  let catalog = store.models;
  for (const role of roleList) {
    const cliOptions = known.map((name) => option(name, name, name === values[role].cli));
    const cli = h("select", null, opts.defaultCliLabel ? option(opts.defaultCliLabel, "", !values[role].cli) : null, cliOptions);
    // CLI whose catalogue feeds the row: the selected one or the inherited fallback ("" = inherit).
    const catalogCli = () => cli.value || (opts.fallbackCli ? opts.fallbackCli() : "");
    let lastCatalogCli = catalogCli();
    const filter = h("input", { type: "search", placeholder: plain(tr("cfg.model.filter")) });
    const model = h("select");
    const effort = h("select");
    const effortHint = h("span", { class: "help effort-hint" });
    effortHint.hidden = true;
    const effortCell = h("div", { class: "effort-cell" }, effort, effortHint);
    rows[role] = { cli, filter, model, effort };
    const refreshModels = () => {
      const current = values[role];
      const list = catalog && catalog.models ? (catalog.models[catalogCli()] || []).slice() : [];
      // Only a changed INHERITED CLI (row CLI empty) resets the model here; an explicit CLI change
      // already clears it in the change listener, and set() (profiles) must keep the given model.
      if (current.model && list.length && !list.includes(current.model) && !cli.value && lastCatalogCli !== catalogCli()) current.model = "";
      lastCatalogCli = catalogCli();
      const needle = filter.value.trim().toLowerCase();
      let shown = needle ? list.filter((name) => name.toLowerCase().includes(needle)) : list;
      if (current.model && !shown.includes(current.model)) shown = [current.model].concat(shown);
      model.replaceChildren(option(plain(tr("cfg.model.prompt")), ""), ...shown.map((name) => option(name, name, name === current.model)));
      model.value = current.model;
      refreshEffort();
    };
    const refreshEffort = () => {
      const current = values[role];
      const cliVariants = catalog && catalog.variants ? catalog.variants[catalogCli()] : undefined;
      const levels = modelLevels(cliVariants, current.model);
      if (current.effort && !levels.includes(current.effort)) {
        if (levels.length) current.effort = ""; else levels.push(current.effort);
      }
      // Loaded catalogue without levels: a text explains it instead of a one-option selector.
      const fixed = cliVariants !== undefined && !levels.length;
      effort.hidden = fixed;
      effortHint.hidden = !fixed;
      effortHint.textContent = fixed ? plain(current.model ? tr("cfg.effort.none") : tr("cfg.effort.pick_model")) : "";
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
    const label = (opts.labels && opts.labels[role]) || tr("cfg.role." + role);
    grid.append(h("span", { class: "role-name", text: plain(label) }), cli,
      h("div", { class: "model-cell" }, filter, model), effortCell);
    refreshModels();
  }
  const reload = h("button", { type: "button", class: "small", text: tr("web.ui.reload_models"), onclick: () => load(true) });
  const load = (refresh) => {
    status.textContent = tr("cfg.models.loading");
    store.getModels(refresh).then((data) => {
      catalog = data;
      for (const role of roleList) rows[role].refresh();
      status.textContent = Object.entries(data.models || {}).map(([name, list]) =>
        list.length ? tr("cfg.models.count", { cli: name, count: list.length }) : tr("cfg.models.unavailable", { cli: name })).join(" · ");
    }).catch((err) => { status.textContent = String(err.message || err); });
  };
  load(false);
  return {
    node: opts.compact ? grid : h("div", null, grid, h("div", { class: "row" }, status, reload)),
    refresh() { for (const role of roleList) rows[role].refresh(); },
    values: () => { const out = {}; for (const role of roleList) out[role] = Object.assign({}, values[role]); return out; },
    set(roles) {
      for (const role of roleList) {
        Object.assign(values[role], { cli: "opencode", model: "", effort: "" }, roles[role] || {});
        rows[role].cli.value = values[role].cli;
        rows[role].filter.value = "";
        rows[role].refresh();
      }
    },
  };
}

// ---------------------------------------------------------------- directory suggestions
// Directory autocompletion (TUI DirectoryPicker) fed by /api/v1/fs/dirs. One
// shared <datalist> serves every input: only the focused one shows it.
const DIRS_LIST_ID = "dir-suggestions";
function dirSuggestions(input) {
  let list = $(DIRS_LIST_ID);
  if (!list) {
    list = h("datalist", { id: DIRS_LIST_ID });
    document.body.appendChild(list);
  }
  input.setAttribute("list", DIRS_LIST_ID);
  const load = debounce(async () => {
    try {
      const data = await api("GET", "/api/v1/fs/dirs?path=" + enc(input.value));
      list.replaceChildren(...data.dirs.map((dir) => option(dir, dir)));
    } catch (err) { /* autocompletion is best effort */ }
  }, 250);
  input.addEventListener("input", load);
  input.addEventListener("focus", load);
}

// ---------------------------------------------------------------- searchable select
// Select2-like single choice: a trigger button opens a panel with a search
// field and the filtered option list (arrows, Enter, Escape and mouse). The
// panel exists only while open (modal() never focuses a hidden field) and
// closes when the focus leaves the control.
let searchSelectSeq = 0;
let searchSelectEscapeAt = 0;  // modal() ignores the Escape that closed a panel
function foldText(text) {
  return String(text || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
}
function searchSelect(opts) {
  // opts: {items: [{value, label, detail?}], value?, emptyLabel?, placeholder?, noItems?,
  //        searchPlaceholder?, noMatches?, ariaLabel?, className?, onChange?(value)}
  const uid = "ss-" + (++searchSelectSeq);
  let items = (opts.items || []).slice();
  let current = opts.value || "";
  let shown = [], active = -1, panel = null, search = null, list = null, empty = null;
  const label = h("span", { class: "ss-label" });
  const trigger = h("button", { type: "button", class: "ss-trigger", "aria-haspopup": "listbox",
    "aria-expanded": "false", "aria-label": opts.ariaLabel || null }, label);
  const root = h("div", { class: "ss" + (opts.className ? " " + opts.className : "") }, trigger);
  // The optional empty entry ("All projects", "Any state") is always listed first.
  const entries = () => (opts.emptyLabel ? [{ value: "", label: opts.emptyLabel, empty: true }] : []).concat(items);
  const textOf = (item) => (item.detail ? item.label + " (" + item.detail + ")" : item.label);
  const syncLabel = () => {
    const item = entries().find((entry) => entry.value === current);
    let text = item ? textOf(item) : opts.emptyLabel;
    if (!text) text = (items.length ? opts.placeholder : opts.noItems || opts.placeholder) || "";
    label.textContent = text;
    trigger.title = text;
    trigger.classList.toggle("placeholder", !item && !opts.emptyLabel);
  };
  const setActive = (index, noScroll) => {
    const nodes = list.children;
    if (!nodes.length) { active = -1; search.removeAttribute("aria-activedescendant"); return; }
    active = Math.max(0, Math.min(nodes.length - 1, index));
    for (let i = 0; i < nodes.length; i++) nodes[i].classList.toggle("active", i === active);
    search.setAttribute("aria-activedescendant", nodes[active].id);
    if (!noScroll) nodes[active].scrollIntoView({ block: "nearest" });  // hover never scrolls
  };
  const renderList = () => {
    const needle = foldText(search.value.trim());
    shown = entries().filter((item) => item.empty || !needle
      || foldText(item.label + " " + (item.detail || "") + " " + item.value).includes(needle));
    list.replaceChildren(...shown.map((item, index) => h("li", {
      id: uid + "-" + index, role: "option", class: "ss-option" + (item.value === current ? " selected" : ""),
      "aria-selected": item.value === current ? "true" : "false",
      onmousedown: (event) => event.preventDefault(),  // keep the focus in the search field
      onclick: (event) => { event.preventDefault(); choose(item.value); },
      onmousemove: () => { if (active !== index) setActive(index, true); },
    }, h("span", { class: "ss-text", text: item.label }),
      item.detail ? h("span", { class: "ss-detail", text: item.detail }) : null)));
    empty.hidden = shown.some((item) => !item.empty);
    empty.textContent = items.length ? (opts.noMatches || tr("web.ui.no_matches")) : (opts.noItems || tr("web.ui.no_matches"));
  };
  const close = (refocus) => {
    if (!panel) return;
    const node = panel;
    panel = search = list = empty = null;
    shown = [];
    active = -1;
    root.classList.remove("open");
    trigger.setAttribute("aria-expanded", "false");
    if (refocus) trigger.focus();  // before removing: the focus never leaves the control
    node.remove();
  };
  const choose = (value) => {
    const changed = value !== current;
    current = value;
    syncLabel();
    close(true);
    if (changed && opts.onChange) opts.onChange(value);
  };
  const onKey = (event) => {
    if (event.key === "ArrowDown") { event.preventDefault(); setActive(active + 1); }
    else if (event.key === "ArrowUp") { event.preventDefault(); setActive(active - 1); }
    else if (event.key === "Enter") {
      event.preventDefault();  // inside a <dialog> form Enter would submit it
      if (active >= 0 && shown[active]) choose(shown[active].value);
    } else if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();  // neither the dialog nor the task detail handle this Escape
      searchSelectEscapeAt = Date.now();
      close(true);
    }
  };
  const open = () => {
    if (panel) return;
    search = h("input", { type: "search", class: "ss-search", autocomplete: "off", role: "combobox",
      placeholder: opts.searchPlaceholder || "", "aria-label": opts.searchPlaceholder || opts.ariaLabel || null,
      "aria-expanded": "true", "aria-controls": uid + "-list", "aria-autocomplete": "list" });
    list = h("ul", { class: "ss-list", id: uid + "-list", role: "listbox" });
    empty = h("div", { class: "ss-empty", hidden: true });
    panel = h("div", { class: "ss-panel", tabindex: "-1" }, search, list, empty);
    search.addEventListener("input", () => {
      renderList();
      // With a query, skip the always-first empty entry so Enter picks the first match.
      const first = search.value.trim() ? shown.findIndex((item) => !item.empty) : -1;
      setActive(first >= 0 ? first : 0);
    });
    panel.addEventListener("keydown", onKey);
    // Clicks inside the panel never reach a wrapping <label> (it would re-click the trigger).
    panel.addEventListener("click", (event) => { if (event.target !== search) event.preventDefault(); });
    root.appendChild(panel);
    root.classList.add("open");
    trigger.setAttribute("aria-expanded", "true");
    renderList();
    setActive(Math.max(0, shown.findIndex((item) => item.value === current)));
    search.focus();
  };
  trigger.addEventListener("mousedown", (event) => { if (panel) event.preventDefault(); });
  trigger.addEventListener("click", () => (panel ? close(true) : open()));
  trigger.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") { event.preventDefault(); open(); }
  });
  root.addEventListener("focusout", (event) => {
    if (panel && !root.contains(event.relatedTarget)) close(false);
  });
  syncLabel();
  return {
    node: root,
    value: () => current,
    setValue(value) { current = value || ""; syncLabel(); if (panel) renderList(); },
    setItems(nextItems, value) {
      items = nextItems.slice();
      if (value !== undefined) current = value || "";
      syncLabel();
      if (panel) { renderList(); setActive(active); }  // keeps the typed query
    },
    close: () => close(false),
  };
}

// ---------------------------------------------------------------- project selector
function projectSelector(projects) {
  // Every project of /api/v1/projects, searchable by name or path (searchSelect).
  const items = projects.slice().sort((a, b) => {
    const byName = a.name.toLowerCase().localeCompare(b.name.toLowerCase());
    return byName || a.workdir.toLowerCase().localeCompare(b.workdir.toLowerCase());
  }).map((item) => ({ value: item.workdir, label: item.name || item.workdir, detail: item.workdir }));
  const picker = searchSelect({
    items, className: "cs-project-selector", ariaLabel: tr("cs.field.workdir"),
    placeholder: plain(tr("cs.workdir.prompt")), noItems: plain(tr("projects.empty.no_projects")),
    searchPlaceholder: plain(tr("projects.filter.placeholder")), noMatches: plain(tr("projects.empty.filter")),
  });
  return { node: picker.node, value: () => picker.value().trim() };
}

// ---------------------------------------------------------------- list editors
function listEditor(columns, rows, emptyText) {
  // columns: [{key, label, type?: "text"|"select"|"phases"|"dir"|"check", options?, placeholder?}]
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
        } else if (col.type === "check") {
          control = h("input", { type: "checkbox" });
          control.checked = item[col.key] !== false;
          item[col.key] = control.checked;
          control.addEventListener("change", () => { item[col.key] = control.checked; });
        } else {
          control = h("input", { type: "text", value: item[col.key] || "", placeholder: col.placeholder || "",
            class: col.type === "dir" ? "mono" : undefined });
          if (col.type === "dir") dirSuggestions(control);
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
  const addRow = (initial) => {
    const row = {};
    columns.forEach((col) => {
      row[col.key] = col.default !== undefined ? col.default
        : (col.type === "select" ? col.options[0][1] : col.type === "check" ? true : "");
    });
    Object.assign(row, initial || {});
    items.push(row);
    render();
    // Focus the first field of the new row: adding is a single click.
    const first = body.lastElementChild && body.lastElementChild.querySelector("input, select");
    if (first) first.focus();
  };
  const add = h("button", { type: "button", class: "small", text: "+ " + tr("web.ui.add"),
    onclick: () => addRow() });
  return {
    node: h("div", { class: "list-editor" }, table, empty, add),
    values: () => items.map((item) => Object.assign({}, item)),
    add: (row) => addRow(row),
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
// Mirrors media.MAX_ATTACHMENT_BYTES. Files up to INLINE_MAX_FILE (stt.MAX_AUDIO_BYTES,
// so audio keeps its transcription) travel base64-encoded inside the JSON request
// while the inline total fits INLINE_MAX_TOTAL; bigger ones are streamed as raw bodies.
const MAX_ATTACH_FILE = 500 * 1024 * 1024;
const INLINE_MAX_FILE = 25 * 1024 * 1024;
const INLINE_MAX_TOTAL = 48 * 1024 * 1024;
const PREVIEW_MAX_BYTES = 64 * 1024 * 1024;  // media tab: larger files are fetched only on demand
// Human readable byte count (same format as media.format_size).
function formatBytes(size) {
  if (size < 1024) return size + " B";
  if (size < 1024 * 1024) return (size / 1024).toFixed(1) + " KB";
  return (size / (1024 * 1024)).toFixed(1) + " MB";
}
function attachmentsCollector(targets, hint) {
  const files = [];
  const chips = h("div", { class: "chips" });
  const input = h("input", { type: "file", multiple: true });
  const progress = h("div", { class: "upload-progress small-text muted" });
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
      if (file.size > MAX_ATTACH_FILE) {
        toast(file.name + ": " + tr("web.ui.attachments_too_large"), "warn");
        continue;
      }
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
    node: h("div", null, input, h("div", { class: "drop-hint", text: hint || tr("web.ui.paste_hint") }), chips, progress),
    // Splits the files between the JSON request (base64) and raw streamed uploads.
    split() {
      const inline = [];
      const raw = [];
      let total = 0;
      for (const file of files) {
        if (file.size <= INLINE_MAX_FILE && total + file.size <= INLINE_MAX_TOTAL) { inline.push(file); total += file.size; }
        else raw.push(file);
      }
      return { inline, raw };
    },
    async payload(list) {
      const out = [];
      for (const file of (list || files).slice()) out.push({ name: file.name, data: await fileBase64(file) });
      return out;
    },
    // Streams each file of a task as a raw body (in sequence) with a progress line.
    async upload(taskId, list, cycle) {
      await this.uploadTo((file) => taskUploadUrl(taskId, file, cycle), list);
    },
    // Streams each file as the raw body of POST urlFor(file) (in sequence, no
    // base64) and returns the parsed responses in the same order.
    async uploadTo(urlFor, list) {
      const results = [];
      for (const file of list || files) {
        try {
          results.push(await uploadRaw(urlFor(file), file, (loaded, total) => {
            progress.textContent = tr("web.ui.uploading", { name: file.name, percent: Math.floor(loaded * 100 / total) });
          }));
        } catch (err) {
          progress.textContent = "";
          throw new ApiFailure(err && err.status ? err.status : 0, tr("web.ui.upload_failed", { name: file.name, error: err && err.message ? err.message : String(err) }));
        }
      }
      progress.textContent = "";
      return results;
    },
    clear() { files.splice(0, files.length); render(); },
    list: () => files.slice(),
    bytes: () => files.reduce((total, file) => total + file.size, 0),
    setDisabled(value) { input.disabled = !!value; },
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

function taskUploadUrl(taskId, file, cycle) {
  let url = taskPath(taskId, "/attachments?name=" + enc(file.name));
  if (cycle) url += "&cycle=" + enc(String(cycle));
  return url;
}

// Streams one file as the raw body of POST <url> (task or CLI session
// attachments: no base64, no full read into memory). XMLHttpRequest instead of
// fetch: it reports upload progress.
function uploadRaw(url, file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    if (auth.token) xhr.setRequestHeader("Authorization", "Bearer " + auth.token);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = (event) => { if (event.lengthComputable && onProgress) onProgress(event.loaded, event.total); };
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText || "{}"); } catch (e) { data = {}; }
      if (xhr.status === 401) { askToken(); reject(new ApiFailure(401, "unauthorized")); return; }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else reject(new ApiFailure(xhr.status, data.error || xhr.statusText));
    };
    xhr.onerror = () => { setStatus("offline"); reject(new ApiFailure(0, "network error")); };
    xhr.send(file);
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

// ---------------------------------------------------------------- task column width
// The task column of the list takes 60% of the viewport by default (app.css).
// Dragging the right edge of its header (or the arrow keys on that handle)
// resizes it and the width is kept in localStorage, so the panel reopens with
// it; a double click on the handle forgets it and restores the default.
function clampTaskColWidth(width) {
  return Math.min(TASK_COL_MAX, Math.max(TASK_COL_MIN, Math.round(width)));
}
function loadTaskColWidth() {
  let saved = null;
  try { saved = localStorage.getItem(TASK_COL_KEY); } catch (e) { saved = null; }
  const width = Number.parseInt(saved, 10);
  return Number.isFinite(width) ? clampTaskColWidth(width) : null;
}
function saveTaskColWidth(width) {
  try {
    if (width === null) localStorage.removeItem(TASK_COL_KEY);
    else localStorage.setItem(TASK_COL_KEY, String(width));
  } catch (e) { /* storage blocked */ }
}
// null restores the default of app.css (style property assignment: allowed by the CSP).
function applyTaskColWidth(th, width) {
  const value = width === null ? "" : width + "px";
  th.style.width = value;
  th.style.minWidth = value; // without it the auto table layout shrinks the column
}
function taskColResizer(th) {
  const label = tr("web.ui.col_resize");
  const handle = h("span", { class: "col-resizer", role: "separator", tabindex: "0",
    "aria-orientation": "vertical", "aria-label": label, title: label });
  let drag = null; // { x, width, applied } while the pointer is down
  const resize = (width) => {
    width = clampTaskColWidth(width);
    applyTaskColWidth(th, width);
    return width;
  };
  const endDrag = () => {
    if (!drag) return;
    if (drag.applied !== null) saveTaskColWidth(drag.applied); // a plain click keeps the default
    drag = null;
    document.body.classList.remove("col-resizing");
  };
  handle.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    event.preventDefault();
    drag = { x: event.clientX, width: th.getBoundingClientRect().width, applied: null };
    handle.setPointerCapture(event.pointerId);
    document.body.classList.add("col-resizing");
  });
  handle.addEventListener("pointermove", (event) => {
    if (!drag || (drag.applied === null && event.clientX === drag.x)) return;
    drag.applied = resize(drag.width + event.clientX - drag.x);
  });
  handle.addEventListener("pointerup", endDrag);
  handle.addEventListener("pointercancel", endDrag);
  handle.addEventListener("lostpointercapture", endDrag);
  handle.addEventListener("dblclick", () => { applyTaskColWidth(th, null); saveTaskColWidth(null); });
  handle.addEventListener("keydown", (event) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const step = event.key === "ArrowLeft" ? -TASK_COL_STEP : TASK_COL_STEP;
    saveTaskColWidth(resize(th.getBoundingClientRect().width + step));
  });
  return handle;
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
    this.projectPicker = searchSelect({
      emptyLabel: tr("web.ui.all_projects"), value: f.project, ariaLabel: tr("web.ui.all_projects"),
      searchPlaceholder: plain(tr("projects.filter.placeholder")), noMatches: plain(tr("projects.empty.filter")),
      onChange: (value) => { f.project = value; this.renderList(); },
    });
    const done = h("select", null, ["all", "hide", "only"].map((mode) => option(tr("tasks.done." + mode), mode, f.done === mode)));
    done.addEventListener("change", () => { f.done = done.value; this.renderList(); });
    const statePicker = searchSelect({
      items: BOOT.states.map((value) => ({ value, label: tr("state." + value) })),
      emptyLabel: tr("web.ui.any_state"), value: f.state, ariaLabel: tr("web.ui.any_state"),
      searchPlaceholder: tr("web.ui.filter_states"),
      onChange: (value) => { f.state = value; this.renderList(); },
    });
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
    const taskHead = h("th", { class: "name", text: tr("tasks.col.task") });
    applyTaskColWidth(taskHead, loadTaskColWidth());
    taskHead.appendChild(taskColResizer(taskHead));
    const table = h("table", { id: "task-table" },
      h("thead", null, h("tr", null,
        taskHead, h("th", { text: tr("tasks.col.state") }),
        h("th", { class: "col-opt", text: tr("tasks.col.scheduled") }),
        h("th", { class: "num col-opt", text: tr("tasks.col.iter") }), h("th", { class: "num col-opt", text: tr("tasks.col.tokens") }),
        h("th", { class: "num col-opt", text: tr("tasks.col.duration") }), h("th", { class: "col-opt", text: tr("tasks.col.updated") }),
        h("th", { class: "col-opt", text: tr("tasks.col.workdir") }))),
      this.rows);
    this.tableWrap = h("div", { class: "table-wrap" }, h("div", { class: "task-table-container" }, table), this.listEmpty);
    const list = h("section", { id: "list-pane" },
      h("div", { class: "filters" }, search, this.projectPicker.node, done, statePicker.node, date, today, reload),
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
    const current = this.filters.project;
    const seen = new Set();
    const names = [];
    // Only projects with tasks: folders without tasks live in the Projects tab.
    for (const item of this.projects) { if (item.count && !seen.has(item.workdir)) { seen.add(item.workdir); names.push([item.workdir, item.count]); } }
    for (const task of this.tasks) {
      const key = projectOf(task);
      if (!seen.has(key)) { seen.add(key); names.push([key, null]); }
    }
    // An unknown current project shows "All projects", like the former <select>.
    this.projectPicker.setItems(names.map(([name, count]) => ({ value: name, label: count === null ? name : name + " (" + count + ")" })), current);
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
        h("td", { class: "when col-opt sched " + ((task.schedule && task.schedule.status) || ""), text: scheduleText(task.schedule, true), title: scheduleText(task.schedule, false) }),
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
    // Total time of the visible tasks (TUI footer).
    let seconds = 0;
    for (const task of tasks) seconds += task.duration_seconds || 0;
    // Native replaceChildren() renders null as the text "null", so optional nodes are filtered out.
    this.summary.replaceChildren(...[
      seconds ? h("div", { text: tr("tasks.time.summary", { duration: formatDuration(seconds) }) }) : null,
      h("div", { text: tr("web.ui.count", { shown: tasks.length, total: this.tasks.length }) }),
    ].filter(Boolean));
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
    if (!store.options) store.getOptions().then(() => { if (this.detail) this.renderHead(); }).catch(() => {});
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
    const schedule = scheduleText(d.schedule, false);
    if (schedule) meta.push(schedule);
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
        btn(tr("det.bind.discard"), a.discard, () => this.simpleAction("discard", "det.mark.discard.title", "det.mark.discard.body", true), "danger")),
      h("div", { class: "action-group" }, this.editorButton(), this.sessionButton()));
  },
  sessionButton() {
    const task = this.detail.task;
    const remoteSession = !!(store.options && store.options.session && store.options.session.active);
    const remote = !!task.remote;
    const button = h("button", { class: "small", text: tr("web.task.new_session"), disabled: remote || remoteSession,
      onclick: () => sessionsView.newSession({ workdir: task.workdir, task_id: task.id, task_name: task.name }) });
    button.title = remoteSession ? tr("cs.unavailable.remote") : (remote ? tr("web.task.new_session.remote") : tr("cs.context.hint", { name: task.name }));
    return button;
  },
  editorButton() {
    const task = this.detail.task;
    const info = editorInfo();
    const remote = !!task.remote;
    const button = h("button", { class: "small", text: tr("web.editor.open"), disabled: !info.available || remote,
      onclick: () => openEditor({ task_id: task.id }) });
    button.title = remote ? tr("web.editor.remote") : (info.available ? info.name : tr("web.editor.none"));
    return button;
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
            close({ request: text.value.trim(), files });
          } })));
    });
    if (!result) return;
    const parts = result.files.split();
    try {
      // Big files go first, tagged with the cycle about to start.
      if (parts.raw.length) {
        toast(tr("web.ui.uploading", { name: parts.raw[0].name, percent: 0 }));
        await result.files.upload(task.id, parts.raw, task.cycle + 1);
      }
    } catch (err) { fail(err); return; }
    const body = { request: result.request };
    if (parts.inline.length) body.attachments = await result.files.payload(parts.inline);
    await this.post("/extend", body, tr("web.ui.started"));
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
    // viewableMedia() skips files above PREVIEW_MAX_BYTES so big images are never fetched for thumbnails.
    const referenced = viewableMedia(d).filter((name) => /\.(png|jpe?g|gif|webp)$/i.test(name) && (task.description || "").includes("media/" + name));
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
      [tr("web.ui.field_scheduled"), scheduleText(d.schedule, false)],
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
    const meta = new Map((d.attachments || []).map((a) => [a.file, a]));
    const files = attachmentsCollector([]);
    const upload = h("button", { type: "button", class: "primary", text: tr("media.upload"), onclick: async () => {
      if (!files.count()) return;
      upload.disabled = true;
      try {
        await files.upload(d.task.id);
        toast(tr("media.uploaded"));
        files.clear();  // avoids duplicate re-uploads
        // Media is a file tab: refresh() does not repaint it on its own, so force it.
        await this.renderTab(true);
      } catch (err) {
        fail(err);
      } finally {
        upload.disabled = false;
      }
    } });
    const uploader = h("div", { class: "media-upload" }, h("span", { class: "lbl", text: tr("media.upload_title") }), files.node, upload);
    if (!names.length) return h("div", null, uploader, h("div", { class: "empty", text: tr("media.empty") }));
    const viewable = viewableMedia(d);
    const grid = h("div", { class: "media-grid" });
    for (const name of names) {
      const item = h("div", { class: "media-item" });
      grid.appendChild(item);
      const kind = mediaKind(name);
      const size = (meta.get(name) && meta.get(name).size) || 0;
      if (kind === "file" || size > PREVIEW_MAX_BYTES) {
        // Not previewable or too big: fetched only when the user asks for it.
        const label = tr("web.ui.download") + (size ? " (" + formatBytes(size) + ")" : "");
        const button = h("button", { type: "button", text: label, onclick: async () => {
          button.disabled = true;
          try {
            const url = await mediaUrl(d.task.id, name);
            const link = h("a", { href: url, download: (meta.get(name) && meta.get(name).name) || name });
            link.click();
          } catch (err) {
            fail(err);
          } finally {
            button.disabled = false;
          }
        } });
        item.prepend(button);
      } else mediaUrl(d.task.id, name).then((url) => {
        let preview = null;
        if (kind === "image") preview = h("img", { src: url, alt: name });
        else if (kind === "video") preview = h("video", { src: url, preload: "metadata", muted: true });
        else if (kind === "audio") preview = h("div", { class: "media-audio-tile", title: name });
        if (preview) preview.addEventListener("click", () => viewer.open(d.task.id, viewable, viewable.indexOf(name)));
        item.prepend(preview || h("a", { href: url, download: name, text: tr("web.ui.download") }));
      }).catch(fail);
      const original = meta.get(name) && meta.get(name).name;
      item.appendChild(h("div", { class: "cap", text: "media/" + name + (original && original !== name ? " (" + original + ")" : "") }));
    }
    return h("div", null, uploader, grid);
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
    const log = h("div", { class: "log" }, entries.map((entry) => h("div", { class: logStyleClass(entry.style), text: entry.text })));
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

// ---------------------------------------------------------------- projects view
// Projects table order: name (case-insensitive), task count or latest task
// activity. Projects without activity always go last; ties fall back to the
// name and then the folder.
function compareProjects(a, b, key, dir) {
  const byName = a.name.localeCompare(b.name, undefined, { sensitivity: "base" }) || a.workdir.localeCompare(b.workdir);
  if (key === "last_worked" && !a.last_worked !== !b.last_worked) return a.last_worked ? -1 : 1;
  let result = 0;
  if (key === "tasks") result = Number(a.count || 0) - Number(b.count || 0);
  else if (key === "last_worked") result = String(a.last_worked || "").localeCompare(String(b.last_worked || ""));
  else result = byName;
  if (dir === "desc") result = -result;
  return result || byName;
}

// Projects found in the working directories (first-level subfolders of each
// root, like the TUI projects screen): server-side favorites (star) in a
// Favorites group above the per-root groups, sortable Project/Tasks/Last
// worked headers (sorted separately within favorites and each root), a text
// filter and the task count of each folder. A click (or Enter) on a project offers to
// create a task in it: the new-task form opens with the folder preset.
const projectsView = {
  data: null,
  filter: "",
  sortKey: "name",
  sortDir: "asc",
  mount() {
    this.data = null;
    this.search = h("input", { type: "search", placeholder: tr("projects.filter.placeholder"), value: this.filter });
    this.search.addEventListener("input", () => { this.filter = this.search.value; this.render(); });
    this.createButton = h("button", { type: "button", class: "primary small", text: tr("projects.create.button"),
      onclick: () => this.createProject() });
    this.createButton.hidden = true;  // shown by render() when the server allows creation
    this.body = h("div", null, h("div", { class: "empty", text: tr("web.ui.loading") }));
    $("main").replaceChildren(h("div", { class: "page" }, h("div", { class: "page-inner" },
      h("h2", { text: tr("tasks.bind.projects") }),
      h("p", { class: "subtitle", text: tr("projects.subtitle") }),
      h("div", { class: "toolbar" }, this.search,
        h("button", { type: "button", class: "small", text: tr("tasks.bind.reload"), onclick: () => this.load() }),
        this.createButton,
        h("a", { class: "small-text", href: "#/settings/workspaces", text: tr("projects.edit_workspaces") })),
      this.body)));
    this.load();
  },
  unmount() {},
  async load() {
    try {
      this.data = await api("GET", "/api/v1/projects");
    } catch (err) { fail(err); return; }
    try { await store.getOptions(); } catch (err) { /* the editor button stays disabled */ }
    this.render();
  },
  sortBy(key) {
    if (this.sortKey === key) this.sortDir = this.sortDir === "asc" ? "desc" : "asc";
    else {
      this.sortKey = key;
      this.sortDir = key === "name" ? "asc" : "desc";
    }
    this.render();
  },
  sortHeader(key, label, className) {
    const active = this.sortKey === key;
    const sort = active ? (this.sortDir === "asc" ? "ascending" : "descending") : "none";
    const arrow = active ? (this.sortDir === "asc" ? " \u25B2" : " \u25BC") : "";
    return h("th", { class: className || null, "aria-sort": sort },
      h("button", { type: "button", class: "sort", text: label + arrow,
        title: tr("projects.sort.by", { column: label }), onclick: () => this.sortBy(key) }));
  },
  sorted(items) {
    return items.slice().sort((a, b) => compareProjects(a, b, this.sortKey, this.sortDir));
  },
  async toggleFavorite(item) {
    const favorite = !item.favorite;
    item.favorite = favorite;  // optimistic: the star and the groups change at once
    this.render();
    try {
      const result = await api("POST", "/api/v1/projects/favorite", { workdir: item.workdir, favorite });
      item.favorite = !!result.favorite;
    } catch (err) {
      item.favorite = !favorite;
      fail(err);
    }
    this.render();
  },
  render() {
    const data = this.data;
    if (!data) return;
    this.createButton.hidden = !!data.session || !(data.create && data.create.available);
    const notice = (key) => this.body.replaceChildren(h("div", { class: "empty", text: tr(key) }));
    if (data.session) { notice("projects.session"); return; }
    const roots = data.workspaces || [];
    if (!roots.length) { notice("projects.empty.no_workspaces"); return; }
    const all = (data.projects || []).filter((item) => item.workspace);
    const needle = this.filter.trim().toLowerCase();
    const shown = all.filter((item) => !needle || (item.name + " " + item.workdir).toLowerCase().includes(needle));
    const favorites = this.sorted(shown.filter((item) => item.favorite));
    const rows = [];
    if (favorites.length) {
      rows.push(h("tr", { class: "root favorites" }, h("td", { colspan: "5", text: tr("projects.group.favorites") })));
      for (const item of favorites) rows.push(this.row(item));
    }
    for (const root of roots) {
      const inRoot = all.filter((item) => item.workspace === root.path);
      const items = this.sorted(shown.filter((item) => item.workspace === root.path && !item.favorite));
      if (!items.length && (needle || inRoot.length)) continue;  // filtered out, or every project is a favorite
      rows.push(h("tr", { class: "root" }, h("td", { colspan: "5" },
        h("span", { class: "mono", text: root.entry }),
        root.exists ? null : h("span", { class: "badge failed", text: tr("projects.root_missing") }))));
      for (const item of items) rows.push(this.row(item));
    }
    let note = tr("projects.count", { shown: shown.length, total: all.length }) + " · " + tr("projects.hint");
    if (!all.length) note = tr("projects.empty.no_projects");
    else if (!shown.length) note = tr("projects.empty.filter");
    this.body.replaceChildren(
      h("div", { class: "card" }, h("div", { class: "table-wrap" }, h("table", { id: "project-table" },
        h("thead", null, h("tr", null,
          h("th", { class: "favorite", "aria-label": tr("projects.col.favorite"), title: tr("projects.col.favorite") }),
          this.sortHeader("name", tr("projects.col.name")),
          this.sortHeader("tasks", tr("projects.col.tasks"), "num"),
          this.sortHeader("last_worked", tr("projects.col.last_worked")),
          h("th", { text: tr("projects.col.path") }))),
        h("tbody", null, rows)))),
      h("p", { class: "muted small-text", text: note }));
  },
  row(item) {
    const action = tr(item.favorite ? "projects.favorite.remove" : "projects.favorite.add", { name: item.name });
    const star = h("button", { type: "button", class: "project-favorite" + (item.favorite ? " active" : ""),
      text: item.favorite ? "\u2605" : "\u2606", title: action, "aria-label": action,
      "aria-pressed": item.favorite ? "true" : "false",
      onclick: (event) => { event.stopPropagation(); this.toggleFavorite(item); } });
    // Enter/Space on the star must not also open the project actions (row keydown).
    star.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") event.stopPropagation(); });
    const row = h("tr", { class: "project", tabindex: "0" },
      h("td", { class: "favorite" }, star),
      h("td", { class: "name", text: item.name }),
      h("td", { class: "num", text: String(item.count || 0) }),
      h("td", { class: "when", text: when(item.last_worked) || "\u2014" }),
      h("td", { class: "path mono", text: item.workdir }));
    row.addEventListener("click", () => this.choose(item));
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); this.choose(item); }
    });
    return row;
  },
  async choose(item) {
    const title = tr("projects.actions.title", { name: item.name });
    const choice = await modal({}, (form, close) => {
      form.append(
        h("div", { class: "project-actions-header" },
          h("h3", { text: title }),
          h("button", { type: "button", class: "icon project-actions-close", text: "×",
            title: tr("common.close"), "aria-label": tr("common.close"),
            onclick: () => close(undefined) })),
        h("div", { class: "detail mono", text: item.workdir }),
        h("div", { class: "buttons project-actions-buttons" },
          h("button", { type: "button", text: tr("web.editor.open"), disabled: !editorInfo().available,
            title: editorInfo().available ? editorInfo().name : tr("web.editor.none"),
            onclick: () => { openEditor({ workdir: item.workdir }); close(undefined); } }),
          h("button", { type: "button", text: tr("cs.open"), onclick: () => close("sessions") }),
          h("button", { type: "submit", class: "primary", autofocus: true, text: tr("projects.action.new_task"),
            onclick: () => close("new") })));
    });
    if (choice === "new") location.hash = "#/new/" + enc(item.workdir);
    if (choice === "sessions") location.hash = "#/sessions/" + enc(item.workdir);
  },
  async createProject() {
    const create = (this.data && this.data.create) || {};
    const created = await this.createDialog(create);
    if (!created) return;
    toast(tr("projects.create.done", { name: created.project.name }));
    await this.load();
    this.choose(created.project);  // next step: new task or CLI sessions in it
  },
  // Resolves with the POST /api/v1/projects response, or undefined when cancelled.
  createDialog(create) {
    const opts = { title: tr("projects.create.title") };
    return modal(opts, (form, close) => {
      const git = create.git !== false;
      const modes = ["plain", "init", "clone"].filter((mode) => (create.modes || ["init", "clone", "plain"]).includes(mode));
      const initial = git ? (create.default_mode || "init") : "plain";
      const mode = h("select", null, modes.map((value) =>
        option(tr("projects.create.mode." + value), value, value === initial)));
      for (const item of mode.options) if (!git && item.value !== "plain") item.disabled = true;
      const name = h("input", { type: "text", maxLength: create.name_max_length || 100 });
      const url = h("input", { type: "text", class: "mono", placeholder: "https://github.com/user/repo.git" });
      const roots = create.workspaces || [];
      const workspace = h("select", { class: "mono" }, roots.map((path, index) => option(path, path, index === 0)));
      const nameField = field(tr("projects.create.name"), name);
      const urlField = field(tr("projects.create.url"), url, { help: tr("projects.create.url.help") });
      const workspaceField = field(tr("projects.create.workspace"), workspace);
      workspaceField.hidden = roots.length < 2;  // a single root needs no choice
      const error = h("div", { class: "form-error" });
      error.hidden = true;
      const cancel = h("button", { type: "button", text: tr("common.cancel"), onclick: () => close(undefined) });
      const submit = h("button", { type: "submit", class: "primary", text: tr("common.create") });
      const sync = () => {
        const clone = mode.value === "clone";
        urlField.hidden = !clone;
        name.placeholder = clone ? tr("projects.create.name_from_url") : "";
      };
      mode.addEventListener("change", () => { sync(); error.hidden = true; });
      sync();
      const busy = (on) => {
        opts.sticky = on;  // modal() reads it on every Escape: no dismissal mid-request
        submit.disabled = on; cancel.disabled = on; mode.disabled = on;
        submit.textContent = on ? tr(mode.value === "clone" ? "projects.create.cloning" : "projects.create.creating") : tr("common.create");
      };
      submit.addEventListener("click", async () => {
        if (submit.disabled) return;
        const payload = { mode: mode.value, name: name.value.trim() };
        if (mode.value === "clone") payload.url = url.value.trim();
        if (roots.length) payload.workspace = workspace.value;
        const missing = mode.value === "clone" ? (!payload.url && "projects.create.error.url") : (!payload.name && "projects.create.error.name");
        if (missing) { error.textContent = tr(missing); error.hidden = false; return; }
        error.hidden = true;
        busy(true);
        try {
          const data = await api("POST", "/api/v1/projects", payload);
          busy(false);
          close(data);
        } catch (err) {
          busy(false);
          if (err && err.status === 401) return;  // the token dialog takes over
          error.textContent = (err && err.message) || String(err);
          error.hidden = false;
        }
      });
      form.append(
        field(tr("projects.create.mode"), mode, git ? null : { help: tr("projects.create.no_git") }),
        workspaceField, urlField, nameField, error,
        h("div", { class: "buttons" }, cancel, submit));
    });
  },
};

// The "send with Enter" preference of the CLI session composer is kept per
// browser; storage may be blocked (private mode), so failures are ignored.
function loadEnterSends() {
  try { return localStorage.getItem(CS_ENTER_KEY) === "1"; } catch (e) { return false; }
}
function saveEnterSends(on) {
  try {
    if (on) localStorage.setItem(CS_ENTER_KEY, "1");
    else localStorage.removeItem(CS_ENTER_KEY);
  } catch (e) { /* storage blocked */ }
}

// ---------------------------------------------------------------- CLI sessions view
// #/sessions -> every session; #/sessions/<folder> -> the sessions of a project;
// #/sessions/<folder>/<id> -> the conversation. Everything goes through /api/v1/sessions.
const sessionsView = {
  workdir: "", sessionId: "", data: null, entries: [], total: 0, session: null,
  timers: [], polling: false, onLive: null, onKey: null, box: null, headNode: null, composer: null, files: null,
  mountSeq: 0, deleting: false, enterSends: false, sending: false,
  mount(params) {
    this.mountSeq += 1;  // invalidates async work started by a previous mount
    this.deleting = false;
    this.sending = false;
    this.workdir = (params && params[0]) || "";
    this.sessionId = (params && params[1]) || "";
    this.data = null; this.entries = []; this.total = 0; this.session = null;
    this.onKey = (event) => {
      if (event.key === "Escape" && !document.querySelector("dialog[open], .lightbox")) this.back();
    };
    document.addEventListener("keydown", this.onKey);
    if (this.sessionId) this.openChat(); else this.loadList();
  },
  unmount() {
    this.mountSeq += 1;
    this.timers.forEach(clearInterval);
    this.timers = [];
    if (this.onLive) { live.sessionListeners.delete(this.onLive); this.onLive = null; }
    if (this.onKey) { document.removeEventListener("keydown", this.onKey); this.onKey = null; }
  },
  back() {
    if (this.sessionId) location.hash = "#/sessions/" + enc(this.workdir);
    else if (this.workdir) location.hash = "#/projects";
  },
  async loadList() {
    const token = this.mountSeq;
    $("main").replaceChildren(h("div", { class: "page" }, h("div", { class: "empty", text: tr("web.ui.loading") })));
    try {
      this.data = await api("GET", "/api/v1/sessions" + (this.workdir ? "?workdir=" + enc(this.workdir) : ""));
    } catch (err) { fail(err); return; }
    if (token !== this.mountSeq) return;
    this.renderList();
    if (!this.onLive) {
      this.onLive = debounce(() => {
        if (this.sessionId) return;
        api("GET", "/api/v1/sessions" + (this.workdir ? "?workdir=" + enc(this.workdir) : ""))
          .then((data) => { this.data = data; this.renderList(); }).catch(fail);
      }, 300);
      live.sessionListeners.add(this.onLive);
    }
  },
  renderList() {
    const data = this.data;
    const sessions = data.sessions || [];
    const title = this.workdir ? tr("cs.title.project", { name: this.workdir.split("/").filter(Boolean).pop() || this.workdir }) : tr("cs.title");
    const toolbar = h("div", { class: "toolbar" },
      this.workdir ? h("button", { type: "button", class: "small", text: tr("common.back"), onclick: () => this.back() }) : null,
      data.available === false ? null : h("button", { type: "button", class: "primary small", text: tr("cs.new"), onclick: () => this.newSession() }),
      h("button", { type: "button", class: "small", text: tr("tasks.bind.reload"), onclick: () => this.loadList() }));
    let body;
    if (data.available === false) body = h("div", { class: "empty", text: tr("cs.unavailable.remote") });
    else if (!sessions.length) body = h("div", { class: "empty", text: tr(this.workdir ? "cs.empty" : "cs.empty.all") });
    else {
      body = h("div", { class: "card" }, h("div", { class: "table-wrap" }, h("table", { id: "session-table" },
        h("thead", null, h("tr", null,
          h("th", { text: tr("cs.col.name") }),
          this.workdir ? null : h("th", { text: tr("cs.col.project") }),
          h("th", { text: tr("cs.col.agent") }),
          h("th", { text: tr("cs.col.state") }),
          h("th", { class: "num", text: tr("cs.col.turns") }),
          h("th", { text: tr("cs.col.updated") }))),
        h("tbody", null, sessions.map((item) => this.row(item))))));
    }
    $("main").replaceChildren(h("div", { class: "page" }, h("div", { class: "page-inner" },
      h("h2", { text: title }), h("p", { class: "subtitle", text: tr("cs.subtitle") }), toolbar, body)));
  },
  stateBadge(item) {
    if (item.running) return h("span", { class: "badge run", text: tr("cs.state.running") });
    if (item.state === "closed") return h("span", { class: "badge paused", text: tr("cs.state.closed") });
    return h("span", { class: "badge", text: tr("cs.state.active") });
  },
  row(item) {
    const open = () => { location.hash = "#/sessions/" + enc(item.workdir) + "/" + enc(item.id); };
    const row = h("tr", { class: "project", tabindex: "0" },
      h("td", { class: "name", text: item.name }),
      this.workdir ? null : h("td", { title: item.workdir, text: item.project_name }),
      h("td", { text: (item.cli + " " + (item.model || "")).trim() }),
      h("td", null, this.stateBadge(item)),
      h("td", { class: "num", text: String(item.turns || 0) }),
      h("td", { text: when(item.updated_at) }));
    row.addEventListener("click", open);
    row.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); open(); } });
    return row;
  },
  async newSession(preset) {
    preset = preset || { workdir: this.workdir };  // callable from other views: no mounted state
    const projectsRequest = preset.workdir ? Promise.resolve({ projects: [] }) : api("GET", "/api/v1/projects");
    let options, projectData;
    try {
      [options, projectData] = await Promise.all([store.getOptions(), projectsRequest]);
    } catch (err) { fail(err); return; }
    const clis = options.session_clis || [];
    if (!clis.length) { toast(tr("det.no_clis"), "warn"); return; }
    const implementer = options.defaults && options.defaults.roles && options.defaults.roles.implementer;
    const cliDefault = clis.includes(options.session_cli_default) ? options.session_cli_default : clis[0];
    const initial = implementer && implementer.cli === cliDefault ? implementer : { cli: cliDefault, model: "", effort: "" };
    const values = await modal({ title: tr("cs.new.title"), wide: true }, (form, close) => {
      let folder;
      if (preset.workdir) {
        const input = h("input", { type: "text", value: preset.workdir, readOnly: true, class: "mono" });
        folder = { node: input, value: () => input.value.trim() };
      } else {
        folder = projectSelector(projectData.projects || []);
      }
      const name = h("input", { type: "text",
        placeholder: preset.task_id ? tr("cs.task_session_name", { name: preset.task_name }) : "" });
      const roles = rolesEditor({ session: initial }, { roles: ["session"], labels: { session: tr("cs.role") }, clis });
      const prompt = h("textarea", { rows: 3 });
      form.append(
        h("label", { class: "field" }, h("span", { class: "lbl", text: tr("cs.field.workdir") }), folder.node),
        h("label", { class: "field" }, h("span", { class: "lbl", text: tr("cs.field.name") }), name),
        roles.node,
        h("label", { class: "field" }, h("span", { class: "lbl", text: tr("cs.field.prompt") }), prompt),
        ...(preset.task_id ? [h("p", { class: "help", text: tr("cs.context.hint", { name: preset.task_name }) })] : []),
        h("div", { class: "buttons" },
          h("button", { type: "button", text: tr("common.cancel"), onclick: () => close(undefined) }),
          h("button", { type: "submit", class: "primary", text: tr("cs.new"), onclick: () => {
            const role = roles.values().session;
            const workdir = folder.value();
            if (!workdir) { toast(tr("cs.error.select_workdir"), "warn"); return; }
            const result = { workdir, name: name.value.trim(), cli: role.cli, model: role.model, effort: role.effort, prompt: prompt.value.trim() };
            if (preset.task_id) Object.assign(result, { task_id: preset.task_id });
            close(result);
          } })));
    });
    if (!values) return;
    try {
      const data = await api("POST", "/api/v1/sessions", values);
      toast(tr("cs.created"));
      location.hash = "#/sessions/" + enc(data.session.workdir) + "/" + enc(data.session.id);
    } catch (err) { fail(err); }
  },
  async openChat() {
    $("main").replaceChildren(h("div", { class: "page" }, h("div", { class: "empty", text: tr("web.ui.loading") })));
    const token = this.mountSeq;
    let data;
    try { data = await api("GET", sessionPath(this.sessionId, "/transcript")); } catch (err) { if (token === this.mountSeq) { fail(err); this.back(); } return; }
    if (token !== this.mountSeq) return;
    this.session = data.session;
    this.entries = data.entries || [];
    this.total = data.total || 0;
    this.headNode = h("div", { class: "detail-head" });
    this.box = h("div", { class: "cs-transcript" });
    this.composer = h("div", { class: "cs-compose" });
    $("main").replaceChildren(h("div", { class: "cs-chat" }, this.headNode, this.box,
      h("div", { class: "cs-hint", id: "cs-hint" }), this.composer));
    this.renderHead();
    this.buildComposer();
    this.box.replaceChildren();
    if (!this.entries.length) this.box.appendChild(h("div", { class: "empty", text: tr("cs.empty.transcript") }));
    this.appendEntries(this.entries);
    this.box.scrollTop = this.box.scrollHeight;
    this.timers.push(setInterval(() => this.poll(), 1500));
    this.onLive = (event) => {
      if (event.session_id === this.sessionId) {
        if (!this.deleting) toast(tr("cs.deleted"));  // remove() shows its own toast
        this.back();
        return;
      }
      if (event.session && event.session.id === this.sessionId) this.poll();
    };
    live.sessionListeners.add(this.onLive);
  },
  renderHead() {
    const item = this.session;
    const tokens = tokenPair(item.tokens.input, item.tokens.output);
    const running = item.running;
    const closed = item.state === "closed";
    const button = (label, cls, fn, disabled) => h("button", { type: "button", class: "small " + cls, text: label, disabled: !!disabled, onclick: fn });
    // Native replaceChildren() renders null as the text "null", so the optional Stop button is filtered out.
    this.headNode.replaceChildren(...[
      h("button", { type: "button", class: "small", text: tr("common.back"), onclick: () => this.back() }),
      h("strong", { text: item.name }),
      h("span", { class: "muted", text: (item.cli + " " + (item.model || "")).trim() }),
      this.stateBadge(item),
      h("span", { class: "muted", text: tr("cs.tokens", { tokens, turns: item.turns }) }),
      h("span", { class: "spacer" }),
      running ? button(tr("cs.stop"), "", () => this.action("cancel")) : null,
      button(tr("cs.rename"), "", () => this.rename()),
      button(tr(closed ? "cs.reopen" : "cs.close"), "", () => this.action(closed ? "reopen" : "close"), running),
      button(tr("cs.delete"), "danger", () => this.remove()),
    ].filter(Boolean));
    const hint = $("cs-hint");
    if (hint) hint.textContent = closed ? tr("cs.closed_notice")
      : (item.initial_context ? tr("cs.context.pending") + " " : "") + tr("cs.hint.auto", { workdir: item.workdir });
  },
  buildComposer() {
    // Created once per chat: later session changes only toggle `disabled`, so a draft survives.
    this.enterSends = loadEnterSends();
    const prompt = h("textarea");
    this.files = attachmentsCollector([prompt], tr("cs.attachments_hint"));
    this.uploaded = new Map();  // File -> stored name of the session attachment
    prompt.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" || event.isComposing) return;  // IME composition confirms with Enter
      const modifier = event.ctrlKey || event.metaKey;
      const plainEnter = this.enterSends && !event.shiftKey && !event.altKey;
      if (!modifier && !plainEnter) return;  // Shift+Enter (or Enter with the option off): new line
      event.preventDefault();
      if (this.sendButton.disabled) { toast(tr("cs.busy"), "warn"); return; }
      this.send();
    });
    this.prompt = prompt;
    this.sendButton = h("button", { type: "button", class: "primary", text: tr("cs.send"), onclick: () => this.send() });
    const enter = check(tr("cs.enter_sends"), this.enterSends);
    enter.node.title = tr("cs.enter_sends.title");
    enter.box.addEventListener("change", () => {
      this.enterSends = enter.box.checked;
      saveEnterSends(this.enterSends);
      this.applyEnterMode();
      this.prompt.focus();
    });
    const editor = h("div", { class: "cs-editor" }, prompt, this.files.node);
    this.composer.replaceChildren(editor, h("div", { class: "cs-actions" }, this.sendButton, enter.node));
    this.applyEnterMode();
    this.updateComposer();
  },
  applyEnterMode() {
    this.prompt.placeholder = tr(this.enterSends ? "cs.prompt.placeholder_enter" : "cs.prompt.placeholder_web");
  },
  updateComposer() {
    const item = this.session;
    this.prompt.disabled = item.state === "closed";
    this.sendButton.disabled = item.state === "closed" || item.running || this.sending;
    this.files.setDisabled(item.state === "closed" || this.sending);
  },
  appendEntries(list) {
    if (!list.length) return;
    const box = this.box;
    const stick = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    const placeholder = box.querySelector(".empty");
    if (placeholder) placeholder.remove();
    for (const entry of list) box.appendChild(this.entryNode(entry));
    if (stick) box.scrollTop = box.scrollHeight;
  },
  entryNode(entry) {
    if (entry.kind === "user") return h("div", { class: "cs-user", text: entry.text });
    if (entry.kind === "text") return h("div", { class: "cs-text" }, renderMarkdown(entry.text));
    return h("div", { class: "cs-line " + (SESSION_KIND_CLASS[entry.kind] || ""), text: entry.text });
  },
  async poll() {
    if (this.polling || !this.sessionId) return;
    this.polling = true;
    try {
      const data = await api("GET", sessionPath(this.sessionId, "/transcript?after=" + this.total));
      this.appendEntries(data.entries || []);
      this.total = data.total;
      const changed = JSON.stringify(data.session) !== JSON.stringify(this.session);
      this.session = data.session;
      if (changed) { this.renderHead(); this.updateComposer(); }
    } catch (err) { if (err && err.status === 404) this.back(); }
    finally { this.polling = false; }
  },
  async send() {
    if (this.sending) return;
    const text = this.prompt.value.trim();
    if (!text && !this.files.count()) { toast(tr("cs.error.empty_prompt"), "error"); return; }
    this.sending = true;
    this.updateComposer();
    try {
      // Files go to the session media dir as raw bodies and the agent gets their
      // paths. Files already stored by a failed send are not uploaded again.
      const files = this.files.list();
      const pending = files.filter((file) => !this.uploaded.has(file));
      const results = await this.files.uploadTo((file) => sessionPath(this.sessionId, "/attachments?name=" + enc(file.name)), pending);
      pending.forEach((file, index) => this.uploaded.set(file, results[index].attachment.file));
      const attachments = files.map((file) => this.uploaded.get(file));
      await api("POST", sessionPath(this.sessionId, "/send"), { prompt: text, attachments });
      this.prompt.value = "";
      this.files.clear();
      this.uploaded = new Map();
    } catch (err) { if (err && err.status === 409) toast(err.message, "error"); else fail(err); return; }
    finally { this.sending = false; this.updateComposer(); }
    this.poll();
  },
  async action(name) {
    try {
      const data = await api("POST", sessionPath(this.sessionId, "/" + name));
      this.session = data.session;
      this.renderHead();
      this.updateComposer();
    } catch (err) { fail(err); }
  },
  async rename() {
    const name = await modal({ title: tr("cs.rename") }, (form, close) => {
      const input = h("input", { type: "text", value: this.session.name || "" });
      form.append(h("label", { class: "field" }, input),
        h("div", { class: "buttons" },
          h("button", { type: "button", text: tr("common.cancel"), onclick: () => close(undefined) }),
          h("button", { type: "submit", class: "primary", text: tr("common.save"), onclick: () => close(input.value.trim()) })));
    });
    if (!name) return;
    try {
      const data = await api("POST", sessionPath(this.sessionId, "/edit"), { name });
      this.session = data.session;
      this.renderHead();
    } catch (err) { fail(err); }
  },
  async remove() {
    const ok = await confirmDialog(tr("cs.delete"), tr("cs.delete.confirm", { name: this.session.name }), "", tr("cs.delete"), true);
    if (!ok) return;
    this.deleting = true;
    try {
      await api("DELETE", sessionPath(this.sessionId));
      toast(tr("cs.deleted"));
      this.back();
    } catch (err) { this.deleting = false; fail(err); }
  },
};

// ---------------------------------------------------------------- new task view
const newTaskView = {
  // params[0]: project folder preset from the Projects tab (#/new/<folder>).
  async mount(params) {
    const preset = (params && params[0]) || "";
    const main = $("main");
    main.replaceChildren(h("div", { class: "page" }, h("div", { class: "empty", text: tr("web.ui.loading") })));
    let options, tasks;
    try {
      [options, tasks] = await Promise.all([store.getOptions(true), api("GET", "/api/v1/tasks")]);
    } catch (err) { fail(err); return; }
    main.replaceChildren(this.build(options, tasks.tasks || [], preset));
  },
  unmount() {},
  build(options, tasks, preset) {
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
    const workdir = h("input", { type: "text", value: preset || (session ? "" : def.workdir), class: "mono",
      placeholder: session ? tr("nt.workdir.remote.placeholder") : "" });
    if (!session) dirSuggestions(workdir);
    const loadIssues = debounce(async () => {
      if (session) return;
      try {
        const data = await api("GET", "/api/v1/issues?workdir=" + enc(workdir.value));
        issues = data.issues || [];
        issueSelect.replaceChildren(option("—", ""), ...issues.map((issue) => option("#" + issue.number + " " + issue.title, String(issue.number))));
        issueField.hidden = !issues.length;
      } catch (err) { issueField.hidden = true; }
    }, 700);
    workdir.addEventListener("input", loadIssues);
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
          : field(tr("nt.workdir"), workdir, { full: true }),
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
        h("button", { type: "button", text: tr("common.cancel"), onclick: () => { location.hash = preset ? "#/projects" : "#/tasks"; } }), submit));
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
        const parts = files.split();
        if (parts.inline.length) payload.attachments = await files.payload(parts.inline);
        if (repeat.value && !automode.box.checked) toast(tr("nt.repeat.forces_automode"), "warn");
        const data = await api("POST", "/api/v1/tasks", payload);
        for (const item of data.transcriptions || []) {
          if (item.error) toast(tr("web.ui.transcription_failed") + " " + item.name + ": " + item.error, "warn");
        }
        if (parts.raw.length) {
          try {
            await files.upload(data.task.id, parts.raw);
          } catch (err) {  // the task exists: report and open it without starting
            fail(err);
            location.hash = "#/tasks/" + enc(data.task.id);
            return;
          }
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
const QUOTA_WINDOW_KEYS = {
  five_hour: "reports.quota.window.five_hour", seven_day: "reports.quota.window.seven_day",
  seven_day_opus: "reports.quota.window.seven_day_opus", seven_day_sonnet: "reports.quota.window.seven_day_sonnet",
  extra_usage: "reports.quota.window.extra_usage", month_total: "reports.quota.window.month_total",
  month_code: "reports.quota.window.month_code", primary: "reports.quota.window.primary",
  secondary: "reports.quota.window.secondary",
};
const QUOTA_STATUS_KEYS = {
  ok: "reports.quota.status.ok", unsupported: "reports.quota.status.unsupported",
  not_installed: "reports.quota.status.not_installed", no_credentials: "reports.quota.status.no_credentials",
  auth_expired: "reports.quota.status.auth_expired", no_data: "reports.quota.status.no_data",
  error: "reports.quota.status.error",
};
function quotaWindowLabel(key) { return QUOTA_WINDOW_KEYS[key] ? tr(QUOTA_WINDOW_KEYS[key]) : key; }
function quotaStatus(cli) {
  let text = QUOTA_STATUS_KEYS[cli.status] ? tr(QUOTA_STATUS_KEYS[cli.status]) : cli.status;
  if (cli.stale) text += " (" + tr("reports.quota.stale") + ")";
  if (cli.source === "stream") text += " (" + tr("reports.quota.source.stream") + ")";
  return text;
}
function quotaUsedBy(cli) {
  const names = (cli.used_by_default ? [tr("reports.quota.default")] : []).concat(cli.profiles || []);
  return names.join(", ");
}
// Table rows of one serialized CLI quota: one per window, or one status row.
function quotaRows(cli) {
  const status = quotaStatus(cli), usedBy = quotaUsedBy(cli);
  const windows = cli.windows || [];
  if (!windows.length) {
    const detail = cli.message ? " (" + cli.message.slice(0, 80) + ")" : "";
    return [h("tr", null, h("td", { class: "mono", text: cli.cli }), h("td"), h("td"), h("td"), h("td"),
      h("td", { text: status + detail }), h("td", { text: usedBy }))];
  }
  return windows.map((w, index) => {
    const used = Math.max(0, Math.min(100, Number(w.used_percent) || 0));
    const severity = used >= 95 ? " danger" : used >= 80 ? " warn" : "";
    return h("tr", null,
      h("td", { class: "mono", text: index === 0 ? cli.cli : "" }),
      h("td", { text: quotaWindowLabel(w.key) }),
      h("td", { class: "bar-cell" }, h("div", { class: "quota-used" },
        h("div", { class: "bar" + severity, style: { width: used.toFixed(1) + "%" } }),
        h("span", { class: "small-text", text: used.toFixed(0) + "%" }))),
      h("td", { class: "num", text: (Number(w.remaining_percent) || 0).toFixed(0) + "%" }),
      h("td", { title: when(w.resets_at), text: w.resets_in_seconds == null ? "" : tr("reports.quota.resets_in", { time: formatCountdown(w.resets_in_seconds) }) }),
      h("td", { text: status }),
      h("td", { text: usedBy }));
  });
}
const reportsView = {
  mount() {
    this.from = h("input", { type: "date" });
    this.to = h("input", { type: "date" });
    const periodButton = (label, period) => h("button", { class: "small", text: tr(label), onclick: () => this.load({ period }) });
    this.body = h("div");
    this.quotaBox = h("div", { class: "card" });
    this.timers = [];
    this.quotaInterval = 300000;
    $("main").replaceChildren(h("div", { class: "page" }, h("div", { class: "page-inner" },
      h("h2", { text: tr("tasks.bind.reports") }),
      h("p", { class: "subtitle", text: tr("reports.subtitle") }),
      h("div", { class: "toolbar" },
        periodButton("reports.period.today", "day"), periodButton("reports.period.week", "week"), periodButton("reports.period.month", "month"),
        this.from, this.to,
        h("button", { class: "small primary", text: tr("reports.period.apply"), onclick: () => this.load({ from: this.from.value, to: this.to.value }) })),
      this.quotaBox,
      this.body)));
    this.load({ period: "day" });
    this.loadQuota(false);
    this.scheduleQuota();
  },
  unmount() { (this.timers || []).forEach(clearInterval); this.timers = []; },
  scheduleQuota() {
    (this.timers || []).forEach(clearInterval);
    this.timers = [setInterval(() => this.loadQuota(false), this.quotaInterval)];
  },
  async loadQuota(live) {
    let data;
    try {
      data = await api("GET", "/api/v1/quota" + (live ? "" : "?cached=1"));
    } catch (err) { fail(err); return; }
    if (!this.quotaBox || !this.quotaBox.isConnected) return;  // view left meanwhile
    const interval = (Number(data.refresh_seconds) || 300) * 1000;
    if (interval !== this.quotaInterval) { this.quotaInterval = interval; this.scheduleQuota(); }
    this.renderQuota(data);
  },
  renderQuota(data) {
    const clis = data.clis || [];
    const head = ["reports.quota.col.cli", "reports.quota.col.window", "reports.quota.col.used", "reports.quota.col.remaining",
      "reports.quota.col.resets", "reports.quota.col.status", "reports.quota.col.used_by"];
    const rows = [].concat(...clis.map(quotaRows));
    this.quotaBox.replaceChildren(...[
      h("h3", { text: tr("reports.quota.title") }),
      h("div", { class: "toolbar" }, h("button", { class: "small", text: tr("reports.quota.refresh"), onclick: () => this.loadQuota(true) })),
      h("p", { class: "help", text: tr("reports.quota.subtitle", { minutes: Math.round((data.refresh_seconds || 300) / 60) }) }),
      h("p", { class: "muted", text: tr("reports.quota.fetched_at", { time: when(data.fetched_at) }) }),
      data.throttled ? h("p", { class: "quota-warning", text: tr("reports.quota.throttled", { seconds: data.retry_after }) }) : null,
      clis.length
        ? h("div", { class: "table-wrap" }, h("table", null,
          h("thead", null, h("tr", null, head.map((key) => h("th", { text: tr(key) })))),
          h("tbody", null, rows)))
        : h("div", { class: "help", text: tr("reports.quota.empty") })].filter(Boolean));
  },
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
const SKILL_PATH = "/api/v1/skill.md";
const SKILL_FILE = "skill.md";

const settingsView = {
  // params[0] === "workspaces" or "web": open scrolled to that card.
  async mount(params) {
    $("main").replaceChildren(h("div", { class: "page" }));
    let data;
    try {
      [data] = await Promise.all([api("GET", "/api/v1/settings"), store.getOptions().catch(() => null)]);
    } catch (err) { fail(err); return; }
    $("main").replaceChildren(this.build(data));
    if (params && (params[0] === "workspaces" || params[0] === "web")) {
      const target = $(params[0] === "web" ? "cfg-web" : "cfg-workspaces");
      if (target) target.scrollIntoView({ block: "start" });
    }
  },
  unmount() { applyTheme(BOOT.web && BOOT.web.theme); },  // undo the preview of an unsaved theme
  build(data) {
    let tgParser = null;  // Telegram parser row; follows the planner CLI when it inherits it
    const roles = rolesEditor(data.roles, { onChange: () => { if (tgParser) tgParser.refresh(); } });
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
    const workspaces = listEditor([
      { key: "path", label: tr("cfg.workspaces.col.path"), type: "dir", placeholder: "~/code" },
    ], (data.workspaces || []).map((path) => ({ path })), tr("cfg.workspaces.empty"));
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
    const startupCommands = listEditor([
      { key: "name", label: tr("cfg.startup.name") },
      { key: "command", label: tr("cfg.startup.command"), placeholder: "code serve-web --port 8080" },
      { key: "workdir", label: tr("cfg.startup.workdir"), type: "dir" },
      { key: "settings_file", label: tr("cfg.startup.settings_file") },
      { key: "settings", label: tr("cfg.startup.settings"), placeholder: "{\"workbench.colorTheme\": \"Default Dark Modern\"}" },
      { key: "open_url", label: tr("cfg.startup.open_url"), placeholder: "http://{host}:8080/?folder={folder}" },
      { key: "enabled", label: tr("cfg.startup.enabled"), type: "check", default: true },
      { key: "stop_on_exit", label: tr("cfg.startup.stop_on_exit"), type: "check", default: true },
    ], data.startup || [], tr("cfg.startup.empty"));
    const presets = data.startup_presets || [];
    const presetSelect = h("select", null, presets.map((item) => option(tr("cfg.startup.preset." + item.key), item.key)));
    const addPreset = h("button", { type: "button", class: "small", text: tr("cfg.startup.add_preset"),
      onclick: () => {
        const chosen = presets.find((item) => item.key === presetSelect.value);
        if (!chosen) return;
        const row = Object.assign({}, chosen);
        delete row.key;
        startupCommands.add(row);
      } });
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
    tgParser = rolesEditor({ parser: { cli: tg.parser_cli || "", model: tg.parser_model, effort: tg.parser_effort || "" } }, {
      roles: ["parser"], compact: true, defaultCliLabel: plain(tr("cfg.tg.parser.default")),
      fallbackCli: () => roles.values().planner.cli, labels: { parser: tr("cfg.tg.parser.title") },
    });
    const tgParserTimeout = h("input", { type: "number", min: 30, max: 3600, value: tg.parser_timeout });
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
    const web = data.web || {};
    const instanceName = h("input", { type: "text", maxlength: 60, value: web.instance_name || "", placeholder: tr("web.cfg.instance_name.placeholder") });
    const theme = h("select", null, option(tr("web.theme.auto"), "", !web.theme),
      (data.web_themes || []).map((value) => option(tr("web.theme." + value), value, value === web.theme)));
    theme.addEventListener("change", () => applyTheme(theme.value));  // live preview; reverted on leave if not saved
    const faviconInput = h("input", { type: "file", accept: (web.favicon_types || []).join(",") });
    const faviconClear = check(tr("web.cfg.favicon.clear"), false);
    const faviconMaxKiB = Math.floor((web.favicon_max_bytes || 262144) / 1024);
    const faviconNode = h("div", null,
      web.favicon_set
        ? h("div", { class: "favicon-preview" }, h("img", { src: "/favicon.ico?v=" + enc(String(web.favicon_version || 0)), alt: "" }), faviconClear.node)
        : h("p", { class: "help", text: tr("web.cfg.favicon.none") }),
      faviconInput);
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
      h("div", { class: "card", id: "cfg-web" }, h("h3", { text: tr("web.cfg.title") }),
        h("p", { class: "help", text: tr("web.cfg.help") }),
        h("div", { class: "form-grid" },
          field(tr("web.cfg.instance_name"), instanceName, { help: tr("web.cfg.instance_name.help") }),
          field(tr("web.cfg.theme"), theme),
          // Not field(): its outer <label> would forward clicks to the remove checkbox inside faviconNode.
          h("div", { class: "field full" }, h("span", { class: "lbl", text: plain(tr("web.cfg.favicon")) }), faviconNode,
            h("span", { class: "help", text: tr("web.cfg.favicon.help", { size: faviconMaxKiB }) })))),
      h("div", { class: "card", id: "cfg-workspaces" }, h("h3", { text: plain(tr("cfg.workspaces")) }),
        h("p", { class: "help", text: tr("cfg.workspaces.help") }), workspaces.node),
      card(tr("cfg.editor"), h("div", { class: "checks" }, editorEnabled.node),
        h("div", { class: "form-grid", style: { marginTop: "12px" } }, field(tr("cfg.editor.name"), editorName), field(tr("cfg.editor.mode"), editorMode), field(tr("cfg.editor.side"), editorSide))),
      h("div", { class: "card", id: "cfg-startup" }, h("h3", { text: plain(tr("cfg.startup")) }),
        h("p", { class: "help", text: tr("cfg.startup.help") }),
        h("div", { class: "form-grid" }, field(tr("cfg.startup.preset"), h("div", { class: "row" }, presetSelect, addPreset))),
        startupCommands.node),
      card(tr("cfg.references"), h("p", { class: "help", text: tr("refs.warning") }), refs.node),
      card(tr("cfg.triggers"), h("p", { class: "help", text: tr("trig.help") }), triggers.node),
      card(tr("cfg.profiles"), h("p", { class: "help", text: tr("cfg.profiles.help") }), profiles.node),
      card(tr("cfg.telegram"), h("p", { class: "help", text: tr("web.ui.restart_notice") }),
        h("div", { class: "checks" }, tgEnabled.node, tgConfirm.node, tgGroupAll.node),
        h("div", { class: "form-grid", style: { marginTop: "12px" } },
          field(tr("cfg.tg.token"), tgToken.node), field(tr("cfg.tg.chats"), tgChats),
          h("div", { class: "full" }, h("p", { class: "help", text: tr("cfg.tg.parser.help") }), tgParser.node),
          field(tr("cfg.tg.parser_timeout"), tgParserTimeout),
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
      this.skillCard(),
      h("div", { class: "form-actions" }, save));
    const secretValue = (target, key, item) => {
      if (item.clear.box.checked) target[key + "_clear"] = true;
      else if (item.input.value.trim()) target[key] = item.input.value.trim();
    };
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const parser = tgParser.values().parser;
      const telegram = {
        enabled: tgEnabled.box.checked, confirm_create: tgConfirm.box.checked, group_all: tgGroupAll.box.checked,
        allowed_chat_ids: tgChats.value, parser_cli: parser.cli, parser_model: parser.model,
        parser_effort: parser.effort, parser_timeout: Number(tgParserTimeout.value),
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
        workspaces: workspaces.values().map((item) => (item.path || "").trim()).filter(Boolean),
        references: refs.values().filter((ref) => (ref.name || "").trim() && (ref.path || "").trim()),
        triggers: triggers.values().filter((item) => (item.name || "").trim() && (item.description || "").trim()),
        startup: startupCommands.values().filter((item) => (item.name || "").trim() || (item.command || "").trim()),
        profiles: profiles.values(),
        telegram, api: apiSection,
      };
      const webSection = { instance_name: instanceName.value, theme: theme.value };
      const file = faviconInput.files && faviconInput.files[0];
      if (file) {
        if (file.size > (web.favicon_max_bytes || 262144)) { toast(tr("web.cfg.favicon.too_large", { size: faviconMaxKiB }), "error"); return; }
        try { webSection.favicon = { name: file.name, data: await fileBase64(file) }; } catch (err) { fail(err); return; }
      } else if (faviconClear.box.checked) {
        webSection.favicon_clear = true;
      }
      payload.web = webSection;
      save.disabled = true;
      try {
        const saved = await api("POST", "/api/v1/settings", payload);
        BOOT.web = { instance_name: saved.web.instance_name, theme: saved.web.theme, favicon_version: saved.web.favicon_version };
        applyBranding(BOOT.web);
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
  // Download card of the API skill. It goes through api() so the request
  // authenticates like the rest of the panel, with the token never in the URL.
  skillCard() {
    const download = h("button", { type: "button", text: tr("web.cfg.skill.download"), onclick: async () => {
      download.disabled = true;
      try {
        saveBlob(await api("GET", SKILL_PATH), SKILL_FILE);
      } catch (err) {
        fail(err);
      } finally {
        download.disabled = false;
      }
    } });
    return h("div", { class: "card", id: "cfg-skill" }, h("h3", { text: tr("web.cfg.skill.title") }),
      h("p", { class: "help", text: tr("web.cfg.skill.help") }),
      h("div", { class: "row" }, download),
      h("div", { class: "skill-install" }, renderMarkdown(tr("web.cfg.skill.install"))));
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
  views: { tasks: tasksView, projects: projectsView, sessions: sessionsView, new: newTaskView, reports: reportsView, settings: settingsView },
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

// ---------------------------------------------------------------- branding
// Instance name, theme and favicon of this GRAFENO instance ([web] section).
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme || "auto";
}
function applyBranding(web) {
  web = web || {};
  applyTheme(web.theme);
  const name = (web.instance_name || "").trim();
  const badge = $("instance");
  badge.textContent = name;
  badge.title = name;
  badge.hidden = !name;
  document.title = name ? name + " - " + tr("web.ui.title") : tr("web.ui.title");
  const icon = $("favicon");
  if (icon) icon.href = "/favicon.ico?v=" + encodeURIComponent(String(web.favicon_version || 0));
}

// ---------------------------------------------------------------- boot
function boot() {
  applyBranding(BOOT.web);
  $("version").textContent = "v" + BOOT.version;
  $("nav-tasks").textContent = tr("web.nav.tasks");
  $("nav-projects").textContent = tr("tasks.bind.projects");
  $("nav-sessions").textContent = tr("cs.title");
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
