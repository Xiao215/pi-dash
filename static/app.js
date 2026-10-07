"use strict";

const $ = (s, root = document) => root.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const icon = (name) => `<svg class="i" aria-hidden="true"><use href="#i-${name}"/></svg>`;

const STATE_LABEL = { up: "Running", unhealthy: "Unhealthy", starting: "Starting", down: "Down", stopped: "Stopped", missing: "Not created", unknown: "Unknown" };
const KIND_LABEL = { compose: "Docker", "systemd-user": "systemd", systemd: "systemd" };
const LEVEL_ICON = { error: "x-circle", warn: "alert", ok: "check", boot: "restart", digest: "sun", info: "info", update: "update" };
const ALERT_LEVELS = new Set(["error", "warn", "ok", "boot", "update"]);
const RUNNING = new Set(["up", "unhealthy", "starting"]);

let last = null;
let lastOk = 0;
let feedFilter = localGet("feedFilter") || "problems";
const busy = new Map();       // service name -> action in progress
const openEvents = new Set(); // expanded activity rows
let feedNewest = 0;

function localGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function localSet(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } }

// ---- formatting ---------------------------------------------------------------

const gb = (b) => (b / 1024 ** 3).toFixed(1);
const mb = (b) => (b >= 1024 ** 3 ? `${gb(b)} GB` : `${Math.round(b / 1024 ** 2)} MB`);
function dur(sec) {
  sec = Math.max(0, Math.floor(sec));
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m`;
  return `${sec}s`;
}
function ago(t, now = Date.now() / 1000) {
  const s = now - t;
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  if (s < 7 * 86400) return `${Math.round(s / 86400)} d ago`;
  return new Date(t * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
const clock = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
const hm = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

/** Set innerHTML only when it changed, so hover states and transitions survive refreshes. */
function put(el, html) {
  if (el._html !== html) { el.innerHTML = html; el._html = html; }
}
function cls(el, value) { if (el.className !== value) el.className = value; }

// ---- api + toasts ---------------------------------------------------------------

async function api(path, opts = {}) {
  const res = await fetch(path, { ...opts, headers: { "X-Pi-Dash": "1", "Content-Type": "application/json", ...(opts.headers || {}) } });
  if (res.status === 401) { location.reload(); throw new Error("Signed out"); }  // shows the sign-in page
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || res.statusText);
  return body;
}

function toast(text, bad = false) {
  const el = document.createElement("div");
  el.className = "toast" + (bad ? " bad" : "");
  el.innerHTML = icon(bad ? "x-circle" : "check") + `<span>${esc(text)}</span>`;
  $("#toasts").append(el);
  setTimeout(() => { el.classList.add("out"); setTimeout(() => el.remove(), 300); }, 3200);
}

// ---- Pi: stat cards with 24 h sparklines ----------------------------------------

const STATS = [
  { key: "cpu", icon: "cpu", label: "CPU", spark: true, range: [0, 100] },
  { key: "mem", icon: "memory", label: "Memory", spark: true, range: [0, 100] },
  { key: "temp", icon: "temp", label: "Temperature", spark: true, range: [20, 90] },
  { key: "disk", icon: "disk", label: "Disk" },
];

function buildStats() {
  $("#stats").innerHTML = STATS.map((s) => `
    <div class="stat" id="stat-${s.key}">
      <div class="stat-head">${icon(s.icon)}<span>${s.label}</span></div>
      <div class="stat-value"><span class="skel">00%</span></div>
      <div class="stat-sub"><span class="skel">loading</span></div>
      ${s.spark
        ? `<svg class="spark" viewBox="0 0 100 34" preserveAspectRatio="none"><path class="area" d=""/><path class="line" d=""/></svg><div class="spark-axis"><span>6 h ago</span><span>now</span></div>`
        : `<div class="meter"><i></i></div><div class="meter-legend"><span class="used"></span><span class="free"></span></div>`}
    </div>`).join("");
  $("#info").innerHTML = ["uptime:clock:Uptime", "power:zap:Power", "network:wifi:Network", "remote:shield:Remote access"].map((x) => {
    const [key, ic, label] = x.split(":");
    return `<div class="info-cell" id="info-${key}">${icon(ic)}<div><div class="info-label">${label}</div><div class="info-value"><span class="skel">loading…</span></div></div></div>`;
  }).join("");
}

function sparkPaths(series, [lo, hi]) {
  const n = series.length;
  const x = (i) => (n === 1 ? 100 : (i / (n - 1)) * 100);
  const y = (v) => 32 - (Math.min(hi, Math.max(lo, v)) - lo) / (hi - lo) * 30;
  let line = "", area = "", seg = [];
  const flush = () => {
    if (!seg.length) return;
    if (seg.length === 1) seg.push([seg[0][0] + 0.6, seg[0][1]]);
    line += "M" + seg.map(([a, b]) => `${a.toFixed(2)},${b.toFixed(2)}`).join("L");
    area += `M${seg[0][0].toFixed(2)},34L` + seg.map(([a, b]) => `${a.toFixed(2)},${b.toFixed(2)}`).join("L") + `L${seg[seg.length - 1][0].toFixed(2)},34Z`;
    seg = [];
  };
  series.forEach((v, i) => (v == null ? flush() : seg.push([x(i), y(v)])));
  flush();
  return { line, area };
}

function setStat(key, { value, sub, level = "", series, range, used, free, pct }) {
  const el = $(`#stat-${key}`);
  cls(el, `stat ${level}`);
  put($(".stat-value", el), value);
  put($(".stat-sub", el), sub);
  if (series) {
    const { line, area } = sparkPaths(series, range);
    const [a, l] = el.querySelectorAll(".spark path");
    if (l.getAttribute("d") !== line) { l.setAttribute("d", line); a.setAttribute("d", area); }
  }
  if (pct != null) {
    $(".meter > i", el).style.width = `${Math.min(100, pct).toFixed(1)}%`;
    put($(".meter-legend .used", el), used);
    put($(".meter-legend .free", el), free);
  }
}

function setInfo(key, value, level = "", title = "") {
  const el = $(`#info-${key}`);
  cls(el, `info-cell ${level}`);
  put($(".info-value", el), value);
  el.title = title;
}

function renderPi(data) {
  const pi = data.pi, h = data.history || {};
  const memUsed = pi.mem.total - pi.mem.available;
  const memPct = (100 * memUsed) / pi.mem.total;
  const diskPct = (100 * pi.disk.used) / pi.disk.total;
  const lvl = (v, warn, bad) => (v >= bad ? "bad" : v >= warn ? "warn" : "");
  const peaks = h.peak || {};
  const peakText = (key, unit) => (peaks[key] != null ? ` · 6 h peak ${peaks[key].toFixed(0)}${unit}` : "");

  setStat("cpu", { value: `${pi.cpu.toFixed(0)}<small>%</small>`, sub: `load ${pi.load[0].toFixed(2)}${peakText("cpu", "%")}`, level: lvl(pi.cpu, 70, 90), series: h.cpu, range: [0, 100] });
  setStat("mem", { value: `${gb(memUsed)}<small> / ${gb(pi.mem.total)} GB</small>`, sub: `${mb(pi.mem.available)} available`, level: lvl(memPct, 80, 90), series: h.mem, range: [0, 100] });
  if (pi.temp == null) setStat("temp", { value: "—", sub: "no temperature sensor", series: [], range: [20, 90] });
  else setStat("temp", { value: `${pi.temp.toFixed(0)}<small>°C</small>`, sub: (pi.temp >= 80 ? "throttling" : pi.temp >= 70 ? "warm" : "cool") + peakText("temp", "°C"), level: lvl(pi.temp, 70, 80), series: h.temp, range: [20, 90] });
  setStat("disk", { value: `${diskPct.toFixed(0)}<small>%</small>`, sub: `${gb(pi.disk.free)} GB free`, level: lvl(diskPct, 80, 90), pct: diskPct, used: `${gb(pi.disk.used)} GB used`, free: `${gb(pi.disk.total)} GB` });

  setInfo("uptime", dur(pi.uptime) + (pi.reboot_required ? " · restart pending" : ""), pi.reboot_required ? "warn" : "",
    pi.reboot_required ? "An installed update needs a restart." : `Up since ${new Date(Date.now() - pi.uptime * 1000).toLocaleString()}`);
  const now = pi.power.flags.filter((f) => f.endsWith("now"));
  setInfo("power", now.length ? "Low voltage now" : pi.power.flags.length ? "Dipped since boot" : "Stable", now.length ? "bad" : pi.power.flags.length ? "warn" : "good",
    pi.power.flags.join(", ") || "No under-voltage or throttling");
  const net = pi.network || {};
  const ip = net.addresses?.wlan0 || net.addresses?.eth0 || "";
  setInfo("network", net.wifi ? `${esc(net.wifi.ssid)} · ${net.wifi.signal}%` : net.addresses?.eth0 ? "Ethernet" : "Offline",
    net.wifi && net.wifi.signal < 40 ? "warn" : "", ip ? `Local address ${ip}` : "");
  const ts = net.tailscale;
  const tsOn = ts?.state === "Running";
  setInfo("remote", tsOn ? `Tailscale · ${esc(ts.name.split(".")[0])}` : ts ? "Tailscale signed out" : "Not set up", tsOn ? "good" : "warn",
    tsOn ? `Reachable from your devices at ${ts.name}` : "Run sudo tailscale up on the Pi");

  $("#model").textContent = [pi.model.replace(" Model ", " ").replace(/ Rev [\d.]+/, ""), pi.os.replace("GNU/Linux ", "")].filter(Boolean).join(" · ");
}

// ---- services ---------------------------------------------------------------------

const cards = new Map();

function buildCard(name) {
  const el = document.createElement("article");
  el.className = "svc appear";
  el.dataset.name = name;
  el.innerHTML = `
    <div class="svc-top">
      <div class="svc-main">
        <div class="svc-title"><span class="dot"></span><h3></h3><span class="badge"></span><span class="state-text"></span></div>
        <p class="svc-desc"></p>
        <p class="problem" hidden></p>
        <div class="facts"></div>
      </div>
      <div class="actions"></div>
    </div>
    <div class="source-row">
      <div class="git"></div>
      <label class="auto" title="Checks every 5 minutes and updates by itself when there's something new"><input type="checkbox"><span class="switch"></span>Auto-update</label>
    </div>
    <div class="uptime">
      <div class="timeline"><div class="track"></div><div class="ticks"></div></div>
      <div class="uptime-pct"></div>
    </div>`;
  return el;
}

function actionButtons(s) {
  const working = busy.get(s.name) || s.busy;
  const running = RUNNING.has(s.state);
  const b = (action, label, ic, extra = "", count = "") => {
    const spinning = working === action;
    return `<button class="btn ${extra}" data-svc="${esc(s.name)}" data-action="${action}" ${working ? "disabled" : ""}>${spinning ? '<span class="spin"></span>' : icon(ic)}<span>${label}</span>${count}</button>`;
  };
  const behind = s.source?.behind || 0;
  return [
    b("logs", "Logs", "logs", "quiet"),
    running ? b("restart", "Restart", "restart") : "",
    running ? b("stop", "Stop", "stop", "danger-text") : b("start", "Start", "play"),
    s.can_update ? b("update", "Update", "update", behind ? "accent" : "", behind ? `<span class="count">${behind}</span>` : "") : "",
  ].join("");
}

function renderServices(data) {
  const box = $("#services");
  if (!data.services.length) {
    put(box, `<div class="empty">No services yet. Add them to <code>~/services/services.json</code>, then restart pi-dash.</div>`);
    cards.clear();
    return;
  }
  if (box.querySelector(".empty, .skel-card")) box.innerHTML = "";
  const seen = new Set();
  data.services.forEach((s, i) => {
    seen.add(s.name);
    let el = cards.get(s.name);
    if (!el) { el = buildCard(s.name); el.style.animationDelay = `${i * 60}ms`; cards.set(s.name, el); box.append(el); }
    cls(el, `svc ${s.state}${el.classList.contains("appear") ? " appear" : ""}`);
    cls($(".dot", el), `dot ${s.state}`);
    put($("h3", el), esc(s.name));
    put($(".badge", el), KIND_LABEL[s.kind] || esc(s.kind));
    const st = $(".state-text", el);
    cls(st, `state-text ${s.state}`);
    put(st, STATE_LABEL[s.state] || esc(s.state));
    put($(".svc-desc", el), esc(s.description) + (s.url ? ` <a href="${esc(s.url)}" target="_blank" rel="noopener">Open${icon("external")}</a>` : ""));

    const prob = $(".problem", el);
    prob.hidden = !s.problem;
    put(prob, s.problem ? `${icon("alert")}<span>${esc(s.problem)}</span>` : "");

    const running = RUNNING.has(s.state);
    const facts = [
      s.since && `<span>${running ? "Up for" : "Stopped"} <b>${running ? dur(data.now - s.since) : ago(s.since, data.now)}</b></span>`,
      running && s.cpu != null && `<span>CPU <b>${s.cpu.toFixed(1)}%</b></span>`,
      running && s.mem != null && `<span>Memory <b>${mb(s.mem)}</b></span>`,
      `<span>Restarts <b>${s.restarts ?? 0}</b></span>`,
      s.health && `<span>Health check <b>${s.health.ok ? `${s.health.ms} ms` : "failing"}</b></span>`,
      !running && s.exit_code ? `<span>Exit code <b>${s.exit_code}</b></span>` : "",
    ].filter(Boolean).join("");
    put($(".facts", el), facts);

    const g = s.source;
    let line = "";
    if (g?.kind === "git") {
      line = `${icon("git")}<code>${esc(g.sha)}</code><span class="subject" title="${esc(g.subject)}">${esc(g.subject)}</span><span class="nowrap">· ${ago(g.time, data.now)}</span>`
        + (g.behind ? `<span class="new" title="${esc(g.newest)}">· ${g.behind} new on GitHub</span>` : "");
    } else if (g?.kind === "image") {
      line = `${icon("box")}<code>${esc(g.sha || "?")}</code><span class="subject" title="${esc(g.image)}">${esc(g.image)}</span>${g.time ? `<span class="nowrap">· built ${ago(g.time, data.now)}</span>` : ""}`
        + (g.behind ? `<span class="new">· newer image published</span>` : "");
    }
    if (g?.dirty) line += `<span class="new warn-text">· local changes</span>`;
    if (g?.check_failed) line += `<span class="new warn-text">· couldn't check for updates</span>`;
    if (s.busy === "update") line += `<span class="new"><span class="spin small"></span>updating…</span>`;
    put($(".git", el), line);
    const auto = $(".auto", el);
    auto.hidden = !s.can_update;
    const toggle = $("input", auto);
    toggle.dataset.svc = s.name;
    if (document.activeElement !== toggle) toggle.checked = !!s.auto_update;

    put($(".actions", el), actionButtons(s));

    renderTimeline(el, s, data.now);
  });
  for (const [name, el] of cards) if (!seen.has(name)) { el.remove(); cards.delete(name); }
}

/** The last 24 h as one strip: each stretch as long as it lasted, so a 2-minute blip looks like one. */
function renderTimeline(el, s, now) {
  const start = now - 86400;
  const pos = (t) => Math.max(0, Math.min(100, ((t - start) / 86400) * 100));
  const segs = (s.timeline || []).map(([a, b, st]) => {
    const mins = Math.max(1, Math.round((b - a) / 60));
    const len = mins >= 120 ? `${Math.floor(mins / 60)} h ${mins % 60} min` : `${mins} min`;
    const label = `${hm(a)}–${hm(b)} · ${STATE_LABEL[st] || st} (${len})`;
    return `<i class="${st}" style="left:${pos(a).toFixed(3)}%;width:${(pos(b) - pos(a)).toFixed(3)}%" title="${label}"></i>`;
  }).join("");
  put($(".track", el), segs);
  // Ticks every 6 hours on the clock (00:00, 06:00, …), so you can tell when things happened.
  const ticks = [];
  const first = new Date(start * 1000);
  first.setMinutes(0, 0, 0);
  first.setHours(Math.ceil((first.getHours() + 1) / 6) * 6);
  for (let t = first.getTime() / 1000; t < now - 9000; t += 6 * 3600) {  // none crowding "now"
    ticks.push(`<span style="left:${pos(t).toFixed(2)}%">${new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false })}</span>`);
  }
  put($(".ticks", el), ticks.join("") + `<span class="now">now</span>`);
  const pct = s.uptime24 == null ? "—" : `${s.uptime24 >= 99.95 ? "100" : s.uptime24.toFixed(s.uptime24 >= 99 ? 2 : 1)}%`;
  const inc = s.incidents ? `${s.incidents} incident${s.incidents > 1 ? "s" : ""}` : "no incidents";
  put($(".uptime-pct", el), `<span><b>${pct}</b> uptime</span><span class="sub">${inc} · 24 h</span>`);
}

// ---- activity -----------------------------------------------------------------------

function renderFeed(events, now) {
  const list = $("#feed");
  const shown = events.filter((e) => feedFilter === "all" || ALERT_LEVELS.has(e.level)).slice(0, 50);
  const first = !list._rendered;
  if (!shown.length) {
    put(list, `<li class="none">${feedFilter === "all" ? "Nothing here yet." : "No alerts. Everything has been running smoothly."}</li>`);
    list._rendered = true;
    return;
  }
  list.querySelector(".none")?.remove();
  list._html = null;
  const existing = new Map([...list.children].map((li) => [li.dataset.key, li]));
  const keep = new Set();
  let prev = null;
  for (const e of shown) {
    const key = `${e.t}`;
    keep.add(key);
    let li = existing.get(key);
    const expandable = (e.log && e.log.length) || (e.detail && e.detail.length > 140);
    if (!li) {
      li = document.createElement("li");
      li.dataset.key = key;
      if (!first && e.t > feedNewest) li.classList.add("enter");
      const tag = expandable ? "button" : "div";
      li.innerHTML = `
        <${tag} class="ev" ${expandable ? `aria-expanded="false"` : ""}>
          <span class="ev-ico ${e.level}">${icon(LEVEL_ICON[e.level] || "info")}</span>
          <span><span class="ev-title">${esc(e.title)}${e.service ? `<span class="chip">${esc(e.service)}</span>` : ""}</span>${e.detail ? `<span class="ev-detail" style="display:block">${esc(expandable && !(e.log && e.log.length) ? e.detail.slice(0, 140) + "…" : e.detail)}</span>` : ""}</span>
          <span class="ev-when"><span class="t"></span>${expandable ? icon("chevron") : ""}</span>
        </${tag}>
        ${expandable ? `<div class="ev-log"><div><pre>${esc(e.log && e.log.length ? e.log.join("\n") : e.detail)}</pre></div></div>` : ""}`;
      if (openEvents.has(key)) li.classList.add("open");
    }
    const t = $(".ev-when .t", li);
    t.textContent = ago(e.t, now);
    t.title = new Date(e.t * 1000).toLocaleString();
    if (prev ? prev.nextElementSibling !== li : list.firstElementChild !== li) {
      prev ? prev.after(li) : list.prepend(li);
    }
    prev = li;
  }
  for (const [key, li] of existing) if (!keep.has(key)) li.remove();
  feedNewest = Math.max(feedNewest, ...events.map((e) => e.t));
  list._rendered = true;
}

$("#feed").addEventListener("click", (ev) => {
  const btn = ev.target.closest("button.ev");
  if (!btn) return;
  const li = btn.parentElement;
  li.classList.toggle("open");
  btn.setAttribute("aria-expanded", li.classList.contains("open"));
  li.classList.contains("open") ? openEvents.add(li.dataset.key) : openEvents.delete(li.dataset.key);
});

$("#feed-filter").addEventListener("click", (ev) => {
  const b = ev.target.closest("button[data-filter]");
  if (!b) return;
  feedFilter = b.dataset.filter;
  localSet("feedFilter", feedFilter);
  syncFilter();
  $("#feed").innerHTML = "";
  $("#feed")._rendered = false;
  if (last) renderFeed(last.events, last.now);
});
function syncFilter() {
  document.querySelectorAll("#feed-filter button").forEach((b) => b.setAttribute("aria-selected", b.dataset.filter === feedFilter));
}

// ---- overall status, mute banner -----------------------------------------------

function renderOverall(data) {
  const bad = data.services.filter((s) => ["down", "missing"].includes(s.state));
  const meh = data.services.filter((s) => ["unhealthy", "starting"].includes(s.state));
  const pi = data.pi;
  const piBad = pi.power.flags.some((f) => f.endsWith("now")) || pi.temp >= 80 || pi.disk.used / pi.disk.total >= 0.9;
  let c = "up", text = "All systems normal";
  if (bad.length || piBad) { c = "down"; text = bad.length ? `${bad.map((s) => s.name).join(", ")} ${bad.length > 1 ? "are" : "is"} down` : "The Pi needs attention"; }
  else if (meh.length) { c = "warn"; text = `${meh.map((s) => s.name).join(", ")} ${meh.every((s) => s.state === "starting") ? "starting" : "unhealthy"}`; }
  const pill = $("#overall");
  cls(pill, `pill ${c}`);
  put($(".pill-text", pill), esc(text));
  document.title = `${c === "up" ? "" : c === "down" ? "(!) " : "(·) "}${data.hostname} dashboard`;

  const muted = data.muted_until > data.now;
  const muteBtn = $("#mute-btn");
  muteBtn.classList.toggle("on", muted);
  put(muteBtn, icon(muted ? "bell-off" : "bell"));
  muteBtn.title = muted ? `Discord alerts muted until ${hm(data.muted_until)}` : "Discord alerts";
  $("#unmute-item").hidden = !muted;
  setBanner(muted ? { html: `${icon("bell-off")}<span class="grow">Discord alerts are muted until <b>${hm(data.muted_until)}</b>. Problems still show up here.</span><button class="btn" data-mute="0">Turn back on</button>` } : null);
}

let bannerOffline = false;
function setBanner(b) {
  if (bannerOffline) return;
  const el = $("#banner");
  if (!b) { el.hidden = true; el._html = null; return; }
  el.className = "banner" + (b.bad ? " bad" : "");
  put(el, b.html);
  el.hidden = false;
}
function setOffline(off) {
  document.body.classList.toggle("offline", off);
  if (off === bannerOffline) return;
  bannerOffline = false;
  if (off) {
    setBanner({ bad: true, html: `${icon("alert")}<span class="grow">Can't reach the Pi. Retrying every few seconds…</span>` });
    bannerOffline = true;
    cls($("#overall"), "pill down");
    put($("#overall .pill-text"), "Offline");
  } else if (last) {
    setBanner(null);
  }
}

// ---- refresh loop ---------------------------------------------------------------------

function render(data) {
  const firstRender = !last;
  last = data;
  $("#host").textContent = data.hostname;
  document.querySelectorAll(".auth-only").forEach((el) => { el.hidden = !data.auth; });
  renderPi(data);
  renderServices(data);
  renderFeed(data.events, data.now);
  renderOverall(data);
  if (firstRender) document.body.classList.remove("loading");
}

let inflight = false;
async function refresh() {
  if (inflight) return;
  inflight = true;
  let data;
  try {
    data = await api("/api/state");
  } catch {
    if (Date.now() - lastOk > 8000 || !last) setOffline(true);
    return;
  } finally {
    inflight = false;
  }
  lastOk = Date.now();
  setOffline(false);
  render(data); // a bug here should show in the console, not look like "offline"
}
setInterval(() => { if (!document.hidden) refresh(); }, 5000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });

// ---- drawer: live logs + job output -----------------------------------------------

const log = $("#log");
let stream = null, jobTimer = null, following = true, filterText = "", unseen = 0;

function drawerStatus(text, dot = "") {
  $("#drawer-sub-text").textContent = text;
  cls($("#drawer-sub .live-dot"), `live-dot ${dot}`);
}
function openDrawer(title) {
  closeStreams();
  $("#drawer-title").textContent = title;
  log.innerHTML = `<div class="placeholder">Loading…</div>`;
  following = true; unseen = 0; $("#jump").hidden = true;
  $("#drawer").classList.add("open");
  $("#drawer").setAttribute("aria-hidden", "false");
  $("#scrim").classList.add("open");
  document.body.style.overflow = "hidden";
}
function closeStreams() {
  if (stream) { stream.close(); stream = null; }
  if (jobTimer) { clearTimeout(jobTimer); jobTimer = null; }
}
function closeDrawer() {
  closeStreams();
  $("#drawer").classList.remove("open");
  $("#drawer").setAttribute("aria-hidden", "true");
  $("#scrim").classList.remove("open");
  document.body.style.overflow = "";
}

function highlight(msg) {
  if (!filterText) return esc(msg);
  const i = msg.toLowerCase().indexOf(filterText);
  if (i < 0) return esc(msg);
  return esc(msg.slice(0, i)) + `<mark>${esc(msg.slice(i, i + filterText.length))}</mark>` + esc(msg.slice(i + filterText.length));
}
function applyFilter(line) {
  line.classList.toggle("hide", !!filterText && !line._msg.toLowerCase().includes(filterText));
  $(".msg", line).innerHTML = highlight(line._msg);
}

function appendLines(rows, fresh) {
  log.querySelector(".placeholder")?.remove();
  const frag = document.createDocumentFragment();
  for (const r of rows) {
    const div = document.createElement("div");
    div.className = "line" + (r.error ? " err" : "") + (r.cmd ? " cmd" : "") + (fresh ? " fresh" : "");
    div._msg = r.msg;
    div.innerHTML = `<span class="ts">${r.t ? clock(r.t) : ""}</span><span class="msg"></span>`;
    applyFilter(div);
    frag.append(div);
  }
  log.append(frag);
  while (log.childElementCount > 5000) log.firstElementChild.remove();
  if (following) log.scrollTop = log.scrollHeight;
  else if (rows.length) { unseen += rows.length; $("#jump").hidden = false; $("#jump").lastChild.textContent = ` ${unseen} new line${unseen > 1 ? "s" : ""}`; }
}

function showLogs(name) {
  openDrawer(`${name}`);
  $("#log-tools").hidden = false;
  log.classList.remove("job");
  drawerStatus("Connecting…", "off");
  let pending = [], timer = 0, initial = true;
  stream = new EventSource(`/api/services/${encodeURIComponent(name)}/logs`);
  stream.onopen = () => drawerStatus("Live: new lines appear as they're written");
  stream.onmessage = (ev) => {
    pending.push(JSON.parse(ev.data));
    if (!timer) timer = setTimeout(() => {
      appendLines(pending, !initial);
      pending = []; timer = 0; initial = false;
    }, initial ? 150 : 60);
  };
  stream.onerror = () => drawerStatus("Reconnecting…", "off");
  setTimeout(() => { if (log.querySelector(".placeholder")) log.innerHTML = `<div class="placeholder">No log lines yet.</div>`; }, 2500);
}

function showJob(job) {
  openDrawer(`${job.service}`);
  $("#log-tools").hidden = true;
  log.classList.add("job");
  drawerStatus(`${job.action[0].toUpperCase() + job.action.slice(1)} in progress…`, "busy");
  let seen = 0;
  const poll = async () => {
    try {
      const j = await api(`/api/jobs/${job.id}?from=${seen}`);
      seen = j.total;
      appendLines(j.lines.map((msg) => ({ msg, cmd: msg.startsWith("$ "), error: !msg.startsWith("$ ") && /\b(error|fatal|failed)\b/i.test(msg) })), true);
      if (j.done) {
        drawerStatus(j.ok ? "Finished" : "Failed", j.ok ? "" : "bad");
        const end = document.createElement("div");
        end.className = `end ${j.ok ? "ok" : "bad"}`;
        end.innerHTML = icon(j.ok ? "check" : "x-circle") + (j.ok ? "Done" : "Stopped at the step that failed");
        log.append(end);
        if (following) log.scrollTop = log.scrollHeight;
        return;
      }
    } catch { /* keep trying */ }
    jobTimer = setTimeout(poll, 600);
  };
  poll();
}

log.addEventListener("scroll", () => {
  following = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  if (following) { unseen = 0; $("#jump").hidden = true; }
});
$("#jump").onclick = () => { log.scrollTo({ top: log.scrollHeight, behavior: "smooth" }); };
$("#log-search").addEventListener("input", (e) => {
  filterText = e.target.value.trim().toLowerCase();
  log.querySelectorAll(".line").forEach(applyFilter);
});
$("#wrap").onchange = (e) => log.classList.toggle("wrap", e.target.checked);
$("#errors-only").onchange = (e) => log.classList.toggle("errors-only", e.target.checked);
$("#drawer-close").onclick = closeDrawer;
$("#scrim").onclick = closeDrawer;

// ---- dialogs, menus, actions ------------------------------------------------------

function confirmBox(title, body, okLabel, danger = false) {
  const dlg = $("#confirm");
  $("#confirm-title").textContent = title;
  $("#confirm-body").textContent = body;
  const ok = $("#confirm-ok");
  ok.textContent = okLabel;
  ok.className = `btn primary${danger ? " danger" : ""}`;
  dlg.returnValue = "";
  dlg.showModal();
  ok.focus();
  return new Promise((resolve) => dlg.addEventListener("close", () => resolve(dlg.returnValue === "ok"), { once: true }));
}

const CONFIRM = {
  stop: (n) => [`Stop ${n}?`, "It stays off, with no alerts and no automatic restart, until you start it again.", "Stop", true],
  restart: (n) => [`Restart ${n}?`, "It goes offline for a few seconds.", "Restart"],
  update: (n, s) => [`Update ${n}?`, s?.source?.kind === "image"
    ? `${s.source.behind ? "A newer image was published. " : "You're on the newest image. "}Pulls it and restarts ${n}.`
    : `${s?.source?.behind ? `${s.source.behind} new commit${s.source.behind > 1 ? "s" : ""} on GitHub. ` : ""}Pulls the latest code and restarts it. Building can take a few minutes.`, "Update"],
};

async function act(name, action) {
  if (action === "logs") return showLogs(name);
  const svc = last?.services.find((s) => s.name === name);
  if (CONFIRM[action] && !(await confirmBox(...CONFIRM[action](name, svc)))) return;
  busy.set(name, action);
  if (last) renderServices(last);
  try {
    const job = await api(`/api/services/${encodeURIComponent(name)}/${action}`, { method: "POST" });
    if (action === "update") showJob(job);
    const wait = async () => {
      const j = await api(`/api/jobs/${job.id}?from=999999`).catch(() => ({ done: false }));
      if (!j.done) return setTimeout(wait, 600);
      busy.delete(name);
      const past = { start: "started", stop: "stopped", restart: "restarted", update: "updated" }[action];
      if (j.ok) toast(`${name} ${past}`);
      else { toast(`Couldn't ${action} ${name}`, true); if (action !== "update") showJob(job); }
      refresh();
    };
    wait();
  } catch (e) {
    busy.delete(name);
    toast(`${name}: ${e.message}`, true);
    refresh();
  }
}

document.addEventListener("click", (e) => {
  const b = e.target.closest("button[data-action]");
  if (b) act(b.dataset.svc, b.dataset.action);
});

document.addEventListener("change", async (e) => {
  const box = e.target.closest(".auto input");
  if (!box) return;
  const name = box.dataset.svc, on = box.checked;
  try {
    await api(`/api/services/${encodeURIComponent(name)}/auto`, { method: "POST", body: JSON.stringify({ on }) });
    toast(on ? `Auto-update on for ${name}` : `Auto-update off for ${name}`);
    box.blur();
    refresh();
  } catch (err) { box.checked = !on; toast(err.message, true); }
});

function closeMenus(except) {
  document.querySelectorAll(".menu.open").forEach((m) => {
    if (m === except) return;
    m.classList.remove("open");
    m.previousElementSibling.setAttribute("aria-expanded", "false");
  });
}
for (const [btnId, menuId] of [["#mute-btn", "#mute-menu"], ["#more-btn", "#more-menu"]]) {
  $(btnId).addEventListener("click", (e) => {
    e.stopPropagation();
    const m = $(menuId);
    closeMenus(m);
    const open = m.classList.toggle("open");
    $(btnId).setAttribute("aria-expanded", open);
    if (open) m.querySelector("button:not([hidden])")?.focus();
  });
}
document.addEventListener("click", (e) => { if (!e.target.closest(".menu")) closeMenus(); });
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if ($(".menu.open")) closeMenus();
  else if ($("#drawer").classList.contains("open")) closeDrawer();
});

document.addEventListener("click", async (e) => {
  const b = e.target.closest("[data-mute]");
  if (!b) return;
  closeMenus();
  const hours = Number(b.dataset.mute);
  try {
    await api("/api/mute", { method: "POST", body: JSON.stringify({ hours }) });
    toast(hours ? `Discord alerts muted for ${hours} h` : "Discord alerts are back on");
    refresh();
  } catch (err) { toast(err.message, true); }
});

$("#sign-out").onclick = async () => {
  closeMenus();
  await fetch("/api/logout", { method: "POST", headers: { "X-Pi-Dash": "1" } }).catch(() => {});
  location.reload();
};
$("#test-alert").onclick = async () => {
  closeMenus();
  try { await api("/api/test-alert", { method: "POST" }); toast("Test alert sent to Discord"); refresh(); }
  catch (e) { toast(e.message, true); }
};
$("#send-digest").onclick = async () => {
  closeMenus();
  try { await api("/api/digest", { method: "POST" }); toast("Summary sent to Discord"); refresh(); }
  catch (e) { toast(e.message, true); }
};
for (const [id, action, title, body, ok] of [
  ["#pi-reboot", "reboot", "Restart the Pi?", "Every service goes offline for about a minute, then comes back by itself.", "Restart"],
  ["#pi-poweroff", "poweroff", "Shut down the Pi?", "Everything stops. To turn it back on, unplug the power and plug it in again.", "Shut down"],
]) {
  $(id).onclick = async () => {
    closeMenus();
    if (!(await confirmBox(title, body, ok, true))) return;
    try {
      await api(`/api/pi/${action}`, { method: "POST" });
      toast(action === "reboot" ? "Restarting… this page reconnects by itself" : "Shutting down…");
    } catch (e) { toast(e.message, true); }
  };
}

// ---- start ---------------------------------------------------------------------------

buildStats();
syncFilter();
$("#services").innerHTML = `<div class="svc skel-card" style="height:150px"></div>`.repeat(2);
document.querySelectorAll(".skel-card").forEach((c) => c.classList.add("skel"));
refresh();
