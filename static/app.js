"use strict";

const $ = (s, root = document) => root.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const icon = (name) => `<svg class="i" aria-hidden="true"><use href="#i-${name}"/></svg>`;

const STATE_LABEL = { up: "Running", unhealthy: "Unhealthy", starting: "Starting", down: "Down", stopped: "Stopped", missing: "Not created", unknown: "Unknown" };
const KIND_LABEL = { compose: "Docker", "systemd-user": "systemd", systemd: "systemd" };
const LEVEL_ICON = { error: "x-circle", warn: "alert", ok: "check", boot: "restart", digest: "sun", info: "info", update: "update" };
const ALERT_LEVELS = new Set(["error", "warn", "ok", "boot", "update"]);
const RUNNING = new Set(["up", "unhealthy", "starting"]);

// The version of pi-dash this page came with; when the server reports another, pi-dash was updated.
const PAGE_VERSION = new URL(document.currentScript.src).searchParams.get("v");
let last = null;
let lastOk = 0;
let feedFilter = localGet("feedFilter") || "problems";
const busy = new Map();       // service name -> action in progress
const openEvents = new Set(); // expanded activity rows

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
const dayHm = (t) => new Date(t * 1000).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" });
function rate(b) {
  if (b == null) return "—";
  if (b < 1000) return `${Math.round(b)} B/s`;
  if (b < 1000 * 1024) return `${(b / 1024).toFixed(b < 10 * 1024 ? 1 : 0)} KB/s`;
  return `${(b / 1024 ** 2).toFixed(1)} MB/s`;
}
const mbText = (v) => (v >= 1024 ? `${(v / 1024).toFixed(1)} GB` : `${Math.round(v)} MB`);
function every(sec) {
  for (const [unit, n] of [["day", 86400], ["hour", 3600], ["minute", 60]]) {
    if (sec >= n && sec % n === 0) return sec === n ? unit : `${sec / n} ${unit}s`;
  }
  return dur(sec);
}

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
  $("#info").innerHTML = ["uptime:clock:Uptime", "power:zap:Power", "network:wifi:Network", "remote:shield:Remote access",
    "updates:package:OS updates", "watchdog:heart:Outside watchdog"].map((x) => {
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
    pi.reboot_required ? `An installed update needs a restart${pi.reboot_packages?.length ? `: ${pi.reboot_packages.join(", ")}` : ""}.`
      : `Up since ${new Date(Date.now() - pi.uptime * 1000).toLocaleString()}`);
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

  const u = pi.updates;
  const checked = u?.checked ? ` Package lists refreshed ${ago(u.checked, data.now)}.` : "";
  if (!u) setInfo("updates", "Checking…", "", "Asks apt what it would upgrade, every 10 minutes.");
  else if (!u.count) setInfo("updates", "Up to date", "good", `Nothing to upgrade.${checked}`);
  else setInfo("updates", `${u.count} waiting${u.security ? ` · ${u.security} security` : ""}`, u.security ? "warn" : "",
    `${u.packages.join(", ")}${u.count > u.packages.length ? ", …" : ""}.${checked} Install with sudo apt full-upgrade.`);

  const hb = data.heartbeat;
  if (!hb) setInfo("watchdog", "Not set up", "", "Add a [heartbeat] url to config.toml so a service outside the Pi tells you when it goes offline.");
  else if (hb.ok === null) setInfo("watchdog", "Starting…", "", "");
  else if (hb.ok) setInfo("watchdog", `Pinged ${ago(hb.t, data.now)}`, "good", `Pings every ${every(hb.every)}. If they stop, the watchdog alerts you.`);
  else setInfo("watchdog", "Can't reach it", "warn", `Last try ${ago(hb.t, data.now)}: ${hb.error}`);

  renderNet(data);

  $("#model").textContent = [pi.model.replace(" Model ", " ").replace(/ Rev [\d.]+/, ""), pi.os.replace("GNU/Linux ", "")].filter(Boolean).join(" · ");
}

// ---- network: 6 h of download/upload, with internet outages shaded ----------------------

function renderNet(data) {
  const h = data.history || {}, net = data.pi.net || {};
  if (!h.rx) return;
  const peak = h.peak || {};
  const outages = (h.offline || []).some(Boolean);
  put($("#net-now"), `
    <span class="legend-item"><span class="key s1"></span>Download <b>${rate(net.rx)}</b></span>
    <span class="legend-item"><span class="key s2"></span>Upload <b>${rate(net.tx)}</b></span>
    ${outages ? `<span class="legend-item"><span class="key band"></span>Internet down</span>` : ""}
    <span class="legend-item muted">6 h peak ↓ ${rate(peak.rx)} · ↑ ${rate(peak.tx)}</span>`);
  const since = data.now - 6 * 3600;
  chart($("#net-chart"), {
    since, step: 300, height: 120, bands: h.offline, bandLabel: "Internet was down",
    series: [{ label: "Download", cls: "s1", data: h.rx }, { label: "Upload", cls: "s2", data: h.tx }],
    fmt: (v) => rate(v), nice: niceRate, floor: 10 * 1024, ticks: clockTicks(since, data.now, 1),
    label: `Network over the last 6 hours: download peaked at ${rate(peak.rx)}, upload at ${rate(peak.tx)}`,
  });
}

// ---- scheduled jobs -----------------------------------------------------------------------

const JOB_STATE = {
  ok: ["On schedule", "up"], running: ["Running", "starting"], waiting: ["Waiting for its first run", "stopped"],
  failed: ["Last run failed", "down"], late: ["Didn't run on time", "unhealthy"], off: ["Timer is off", "unhealthy"],
  missing: ["Timer not found", "down"],
};

function renderJobs(data) {
  const jobs = data.jobs || [];
  $("#jobs-section").hidden = !jobs.length;
  if (!jobs.length) return;
  put($("#jobs"), jobs.map((j) => {
    const [label, tone] = JOB_STATE[j.state] || [j.state, ""];
    const r = j.last;
    const facts = [
      r ? `<span>${r.ok ? "Last run" : "Failed"} <b>${ago(r.t, data.now)}</b>${r.took != null ? ` · took ${dur(r.took)}` : ""}${!r.ok && r.code ? ` · exit code ${r.code}` : ""}</span>` : "",
      j.next ? (j.next > data.now ? `<span>Next <b>in ${dur(j.next - data.now)}</b></span>` : `<span>Was due <b>${ago(j.next, data.now)}</b></span>`) : "",
      j.every ? `<span>Every <b>${every(j.every)}</b></span>` : "",
      `<span>${j.kind === "timer" ? `${icon("calendar")}${esc(j.timer)}${j.user ? " (user)" : ""}` : `pings <code>/api/ping/${esc(j.name)}</code>`}</span>`,
    ].filter(Boolean).join("");
    const runs = j.runs.map((x) => `<i class="${x.ok ? "ok" : "bad"}" title="${esc(new Date(x.t * 1000).toLocaleString())} · ${x.ok ? "worked" : "failed"}${x.took != null ? ` in ${dur(x.took)}` : ""}"></i>`).join("");
    const btn = (action, text, ic) => `<button class="btn${action === "logs" ? " quiet" : ""}" data-job="${esc(j.name)}" data-jobaction="${action}" data-kind="${j.kind}">${icon(ic)}<span>${text}</span></button>`;
    return `<li class="job">
      <span class="dot ${tone}"></span>
      <div class="job-main">
        <div class="job-title"><h3>${esc(j.name)}</h3><span class="state-text ${tone}">${label}</span></div>
        ${j.description ? `<p class="svc-desc">${esc(j.description)}</p>` : ""}
        <div class="facts">${facts}</div>
      </div>
      <div class="runs" title="Last runs, oldest first">${runs}</div>
      <div class="actions">${j.has_log ? btn("logs", j.kind === "timer" ? "Logs" : "Output", "logs") : ""}${j.can_run ? btn("run", "Run now", "play") : ""}</div>
    </li>`;
  }).join(""));
}

document.addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-jobaction]");
  if (!b) return;
  const name = b.dataset.job;
  if (b.dataset.jobaction === "logs") {
    if (b.dataset.kind === "timer") return showLogs(name, `/api/scheduled/${encodeURIComponent(name)}/logs`);
    return showOutput(name);
  }
  if (!(await confirmBox(`Run ${name} now?`, "Starts it right away, outside its schedule.", "Run"))) return;
  try {
    await api(`/api/scheduled/${encodeURIComponent(name)}/run`, { method: "POST" });
    toast(`Started ${name}`);
    refresh();
  } catch (err) { toast(err.message, true); }
});

// ---- charts: a time series with a crosshair tooltip ------------------------------------

function niceMax(v) {
  if (!(v > 0)) return 1;
  const p = 10 ** Math.floor(Math.log10(v));
  return [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10].find((m) => m * p >= v - 1e-9) * p;
}

/** Round in the unit the axis shows: 1.5 MB/s, not 1,500,000 B/s. */
const niceRate = (v) => (v < 1000 * 1024 ? niceMax(v / 1024) * 1024 : niceMax(v / 1024 ** 2) * 1024 ** 2);

/** Ticks on round clock times: every `hours` hours, or midnights (labelled by weekday) for 24. */
function clockTicks(since, now, hours) {
  const d = new Date(since * 1000);
  d.setMinutes(0, 0, 0);
  d.setHours(Math.ceil((d.getHours() + 1) / hours) * hours);
  const out = [];
  for (let t = d.getTime() / 1000; t < now; t += hours * 3600) {
    const at = new Date(t * 1000);
    out.push([t, hours >= 24 ? at.toLocaleDateString([], { weekday: "short" }) : at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false })]);
  }
  return out;
}

/**
 * Draw a line chart of bucketed values into `el`.
 * opts: { since, step, series: [{ label, cls, data }], fmt(v, top), nice(max), floor, ticks: [[t, label]],
 *         bands: [bool] (shaded buckets), bandLabel, area, height, label (for screen readers) }
 */
function lineChart(el, opts) {
  const key = JSON.stringify([opts.since - (opts.since % opts.step), opts.series.map((s) => s.data), opts.bands, el.clientWidth]);
  if (el._key === key) return;
  el._key = key;
  el._opts = opts;
  const W = Math.max(240, el.clientWidth || 600), H = opts.height || 150;
  const n = opts.series[0].data.length;
  let hi = 0;
  for (const s of opts.series) for (const v of s.data) if (v != null && v > hi) hi = v;
  const top = (opts.nice || niceMax)(Math.max(hi * 1.05, opts.floor || 0));
  const yLabels = [0, top / 2, top].map((v) => opts.fmt(v, top));
  const L = 10 + 6.4 * Math.max(...yLabels.map((t) => t.length)), R = 8, T = 8, B = 22, PW = W - L - R, PH = H - T - B;
  const end = opts.since + n * opts.step;
  const x = (t) => L + ((t - opts.since) / (end - opts.since)) * PW;
  const xi = (i) => x(opts.since + (i + 0.5) * opts.step);
  const y = (v) => T + PH - (Math.min(top, Math.max(0, v)) / top) * PH;
  const f = (v) => v.toFixed(1);

  let svg = "";
  (opts.bands || []).forEach((b, i) => {
    if (b) svg += `<rect class="band" x="${f(x(opts.since + i * opts.step))}" y="${T}" width="${f(PW / n + 0.5)}" height="${PH}"/>`;
  });
  [0, 0.5, 1].forEach((k, j) => {
    const yy = f(y(top * k));
    svg += `<line class="grid${k ? "" : " base"}" x1="${L}" x2="${W - R}" y1="${yy}" y2="${yy}"/><text class="ylab" x="${f(L - 8)}" y="${yy}" dy="0.32em">${esc(yLabels[j])}</text>`;
  });
  const ticks = (opts.ticks || []).filter(([t]) => x(t) > L + 16 && x(t) < W - R - 16);
  const gap = ticks.length > 1 ? x(ticks[1][0]) - x(ticks[0][0]) : PW;
  const keep = Math.ceil(46 / gap);  // every k-th tick, so labels never touch
  ticks.forEach(([t, label], i) => {
    if (i % keep === 0) svg += `<text class="xlab" x="${f(x(t))}" y="${H - 5}">${esc(label)}</text>`;
  });
  for (const s of opts.series) {
    let line = "", area = "", seg = [];
    const flush = () => {
      if (!seg.length) return;
      if (seg.length === 1) seg.push([seg[0][0] + 1.5, seg[0][1]]);
      const pts = seg.map(([a, b]) => `${f(a)},${f(b)}`).join("L");
      line += "M" + pts;
      area += `M${f(seg[0][0])},${f(T + PH)}L${pts}L${f(seg[seg.length - 1][0])},${f(T + PH)}Z`;
      seg = [];
    };
    s.data.forEach((v, i) => (v == null ? flush() : seg.push([xi(i), y(v)])));
    flush();
    if (opts.area) svg += `<path class="area ${s.cls}" d="${area}"/>`;
    svg += `<path class="line ${s.cls}" d="${line}"/>`;
  }
  svg += `<line class="cross" x1="0" x2="0" y1="${T}" y2="${T + PH}" visibility="hidden"/>`;
  svg += opts.series.map((s) => `<circle class="pt ${s.cls}" r="4" visibility="hidden"/>`).join("");
  el.innerHTML = `<div class="chart-plot"><svg class="plot" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" tabindex="0" role="img" aria-label="${esc(opts.label || "")}">${svg}</svg><div class="tip" hidden></div></div>`;

  const plot = $("svg", el), tip = $(".tip", el), cross = $(".cross", el), pts = el.querySelectorAll(".pt");
  const show = (i) => {
    i = Math.max(0, Math.min(n - 1, i));
    el._hover = i;
    const cx = xi(i);
    cross.setAttribute("x1", cx); cross.setAttribute("x2", cx); cross.setAttribute("visibility", "visible");
    const a = opts.since + i * opts.step;
    const when = opts.step >= 3600 ? `${dayHm(a)}–${hm(a + opts.step)}` : `${hm(a)}–${hm(a + opts.step)}`;
    let rows = "";
    opts.series.forEach((s, k) => {
      const v = s.data[i];
      pts[k].setAttribute("visibility", v == null ? "hidden" : "visible");
      if (v != null) { pts[k].setAttribute("cx", cx); pts[k].setAttribute("cy", y(v)); }
      rows += `<div class="tip-row"><span class="key ${s.cls}"></span><b>${v == null ? "no data" : esc(opts.fmt(v))}</b>${opts.series.length > 1 ? `<span>${esc(s.label)}</span>` : ""}</div>`;
    });
    if (opts.bands?.[i]) rows += `<div class="tip-row"><span class="key band"></span><span>${esc(opts.bandLabel || "")}</span></div>`;
    tip.innerHTML = `<div class="tip-when">${when}</div>${rows}`;
    tip.hidden = false;
    const tw = tip.offsetWidth;
    tip.style.left = `${cx + 12 + tw > W ? cx - 12 - tw : cx + 12}px`;
  };
  const hide = () => {
    el._hover = null;
    tip.hidden = true;
    cross.setAttribute("visibility", "hidden");
    pts.forEach((p) => p.setAttribute("visibility", "hidden"));
  };
  plot.addEventListener("pointermove", (e) => show(Math.floor(((e.clientX - plot.getBoundingClientRect().left - L) / PW) * n)));
  plot.addEventListener("pointerleave", hide);
  plot.addEventListener("blur", hide);
  plot.addEventListener("focus", () => show(el._hover ?? n - 1));
  plot.addEventListener("keydown", (e) => {
    const step = { ArrowLeft: -1, ArrowRight: 1, Home: -n, End: n }[e.key];
    if (step == null) return;
    e.preventDefault();
    show((el._hover ?? n - 1) + step);
  });
  if (opts.keepHover != null) show(opts.keepHover);
}

/** Charts follow their box's width (and draw properly once a hidden page becomes visible). */
const chartSizer = new ResizeObserver((entries) => {
  for (const e of entries) {
    const el = e.target;
    if (el._opts && Math.abs(e.contentRect.width - (el._w || 0)) > 2) {
      el._w = e.contentRect.width;
      lineChart(el, { ...el._opts, keepHover: el._hover });
    }
  }
});
function chart(el, opts) {
  if (!el._observed) { chartSizer.observe(el); el._observed = true; }
  lineChart(el, { ...opts, keepHover: el._hover });
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
        <div class="svc-title"><span class="dot"></span><h3><a class="svc-link"></a></h3><span class="badge"></span><span class="state-text"></span></div>
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
    const link = $(".svc-link", el);
    put(link, esc(s.name) + icon("chevron"));
    link.href = `#service/${encodeURIComponent(s.name)}`;
    link.title = `${s.name}: charts, history and setup`;
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

/** One strip from `since` to now: each stretch as long as it lasted, so a 2-minute blip looks like one. */
function drawTimeline(el, segs, since, now, ticks) {
  const span = now - since;
  const pos = (t) => Math.max(0, Math.min(100, ((t - since) / span) * 100));
  const when = span > 86400 ? dayHm : hm;
  put($(".track", el), (segs || []).map(([a, b, st]) => {
    const mins = Math.max(1, Math.round((b - a) / 60));
    const len = mins >= 120 ? `${Math.floor(mins / 60)} h ${mins % 60} min` : `${mins} min`;
    const label = `${when(a)}–${hm(b)} · ${STATE_LABEL[st] || st} (${len})`;
    return `<i class="${st}" style="left:${pos(a).toFixed(3)}%;width:${(pos(b) - pos(a)).toFixed(3)}%" title="${label}"></i>`;
  }).join(""));
  put($(".ticks", el), ticks.filter(([t]) => t < now - span * 0.1)  // none crowding "now"
    .map(([t, label]) => `<span style="left:${pos(t).toFixed(2)}%">${esc(label)}</span>`).join("") + `<span class="now">now</span>`);
}

/** The card's last 24 h, with ticks every 6 hours on the clock so you can tell when things happened. */
function renderTimeline(el, s, now) {
  drawTimeline($(".timeline", el), s.timeline, now - 86400, now, clockTicks(now - 86400, now, 6));
  const pct = s.uptime24 == null ? "—" : `${s.uptime24 >= 99.95 ? "100" : s.uptime24.toFixed(s.uptime24 >= 99 ? 2 : 1)}%`;
  const inc = s.incidents ? `${s.incidents} incident${s.incidents > 1 ? "s" : ""}` : "no incidents";
  put($(".uptime-pct", el), `<span><b>${pct}</b> uptime</span><span class="sub">${inc} · 24 h</span>`);
}

// ---- activity -----------------------------------------------------------------------

function renderFeed(events, now) {
  const shown = events.filter((e) => feedFilter === "all" || ALERT_LEVELS.has(e.level)).slice(0, 50);
  renderEvents($("#feed"), shown, now, feedFilter === "all" ? "Nothing here yet." : "No alerts. Everything has been running smoothly.");
}

/** Newest first; new rows slide in, expanded rows stay open across refreshes. */
function renderEvents(list, shown, now, emptyText) {
  const first = !list._rendered;
  if (!shown.length) {
    put(list, `<li class="none">${emptyText}</li>`);
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
      if (!first && e.t > (list._newest || 0)) li.classList.add("enter");
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
  list._newest = Math.max(list._newest || 0, ...shown.map((e) => e.t));
  list._rendered = true;
}

document.addEventListener("click", (ev) => {
  const btn = ev.target.closest(".feed button.ev");
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
  document.title = `${c === "up" ? "" : c === "down" ? "(!) " : "(·) "}${page ? `${page} · ${data.hostname}` : `${data.hostname} dashboard`}`;

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
  if (PAGE_VERSION && data.version && data.version !== PAGE_VERSION && !$("#drawer").classList.contains("open") && !$("#confirm").open) {
    location.reload();  // load the new page; waits while logs or a dialog are open
    return;
  }
  const firstRender = !last;
  last = data;
  $("#host").textContent = data.hostname;
  document.querySelectorAll(".auth-only").forEach((el) => { el.hidden = !data.auth; });
  renderPi(data);
  renderServices(data);
  renderJobs(data);
  renderFeed(data.events, data.now);
  renderOverall(data);
  if (page) renderPage(data);
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

function showLogs(name, url = `/api/services/${encodeURIComponent(name)}/logs`) {
  openDrawer(`${name}`);
  $("#log-tools").hidden = false;
  log.classList.remove("job");
  drawerStatus("Connecting…", "off");
  let pending = [], timer = 0, initial = true;
  stream = new EventSource(url);
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

/** What a scheduled job sent along with its last ping. */
async function showOutput(name) {
  openDrawer(name);
  $("#log-tools").hidden = false;
  log.classList.remove("job");
  drawerStatus("Output sent with the last run", "off");
  try {
    const { lines } = await api(`/api/scheduled/${encodeURIComponent(name)}/output`);
    log.innerHTML = lines.length ? "" : `<div class="placeholder">The last run sent no output.</div>`;
    appendLines(lines.map((msg) => ({ msg, error: /\b(error|fatal|failed)\b/i.test(msg) })), false);
  } catch (e) { log.innerHTML = `<div class="placeholder">${esc(e.message)}</div>`; }
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
  else if (page && !$("#confirm").open) location.hash = "";
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

// ---- a service's own page: #service/<name> --------------------------------------------

let page = null;  // the service whose page is open
let pageRange = ["6h", "24h", "7d"].includes(localGet("range")) ? localGet("range") : "24h";
let pageData = null, pageTimer = 0, pageReq = 0;
const RANGE_TEXT = { "6h": ["6 h", "5-minute averages", 1], "24h": ["24 h", "15-minute averages", 3], "7d": ["7 days", "hourly averages", 24] };

function route() {
  const m = location.hash.match(/^#service\/(.+)$/);
  const name = m ? decodeURIComponent(m[1]) : null;
  if (name === page) return;
  page = name;
  $("#home").hidden = !!page;
  $("#svc-page").hidden = !page;
  clearTimeout(pageTimer);
  pageData = null;
  if (page) {
    for (const id of ["#sp-cpu", "#sp-mem", "#sp-ms"]) { $(id).innerHTML = ""; $(id)._key = null; }
    $("#sp-feed").innerHTML = ""; $("#sp-feed")._rendered = false;
    put($("#sp-code"), ""); put($("#sp-setup"), ""); put($("#sp-uptime"), "");
    syncRange();
    loadPage();
    if (last) renderPage(last);
  }
  if (last) renderOverall(last);
  window.scrollTo(0, 0);
}
window.addEventListener("hashchange", route);

async function loadPage() {
  clearTimeout(pageTimer);
  const name = page, req = ++pageReq;
  $("#sp-body").classList.add("refetch");  // keep the old charts, dimmed, while the new ones load
  try {
    const h = await api(`/api/services/${encodeURIComponent(name)}/history?range=${pageRange}`);
    if (req !== pageReq) return;
    pageData = h;
    renderHistory();
    if (last) renderPage(last);
  } catch (e) {
    if (req === pageReq && last) toast(e.message, true);
  } finally {
    if (req === pageReq) {
      $("#sp-body").classList.remove("refetch");
      pageTimer = setTimeout(function again() { document.hidden ? (pageTimer = setTimeout(again, 5000)) : loadPage(); }, 60000);
    }
  }
}

function syncRange() {
  document.querySelectorAll("#sp-range button").forEach((b) => b.setAttribute("aria-selected", b.dataset.range === pageRange));
  put($("#sp-range-note"), RANGE_TEXT[pageRange][1]);
}
$("#sp-range").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-range]");
  if (!b || b.dataset.range === pageRange) return;
  pageRange = b.dataset.range;
  localSet("range", pageRange);
  syncRange();
  loadPage();
});

/** The live part, every 5 s: state, facts, buttons, the headline numbers. */
function renderPage(data) {
  const s = data.services.find((x) => x.name === page);
  if (!s) {
    put($("#sp-name"), esc(page));
    put($("#sp-state"), "Not in services.json");
    $("#sp-body").hidden = true;
    put($("#sp-actions"), "");
    return;
  }
  $("#sp-body").hidden = false;
  cls($("#svc-page .page-head .dot"), `dot ${s.state}`);
  put($("#sp-name"), esc(s.name));
  put($("#sp-kind"), KIND_LABEL[s.kind] || esc(s.kind));
  cls($("#sp-state"), `state-text ${s.state}`);
  put($("#sp-state"), STATE_LABEL[s.state] || esc(s.state));
  put($("#sp-desc"), esc(s.description) + (s.url ? ` <a href="${esc(s.url)}" target="_blank" rel="noopener">Open${icon("external")}</a>` : ""));
  const prob = $("#sp-problem");
  prob.hidden = !s.problem;
  put(prob, s.problem ? `${icon("alert")}<span>${esc(s.problem)}</span>` : "");
  const running = RUNNING.has(s.state);
  put($("#sp-facts"), [
    s.since && `<span>${running ? "Up for" : "Stopped"} <b>${running ? dur(data.now - s.since) : ago(s.since, data.now)}</b></span>`,
    `<span>Restarts <b>${s.restarts ?? 0}</b></span>`,
    s.pid ? `<span>PID <b>${s.pid}</b></span>` : "",
    !running && s.exit_code ? `<span>Exit code <b>${s.exit_code}</b></span>` : "",
    s.oom ? `<span class="warn-text">Last stop: out of memory</span>` : "",
    s.health && !s.health.ok ? `<span>Health check <b>${esc(s.health.error || "failing")}</b></span>` : "",
  ].filter(Boolean).join(""));
  put($("#sp-actions"), actionButtons(s));
  renderTiles(s, data.now);
  renderCode(s, data.now);
}

function tile(label, value, sub, level = "") {
  return `<div class="stat ${level}"><div class="stat-head"><span>${label}</span></div><div class="stat-value">${value}</div><div class="stat-sub">${sub}</div></div>`;
}

function renderTiles(s, now) {
  const h = pageData, rl = RANGE_TEXT[pageRange][0];
  const avg = (xs) => { const v = (xs || []).filter((x) => x != null); return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null; };
  const running = RUNNING.has(s.state);
  const up = h?.uptime;
  const upText = up == null ? "—" : `${up >= 99.95 ? "100" : up.toFixed(up >= 99 ? 2 : 1)}<small>%</small>`;
  const inc = h ? `${h.incidents ? `${h.incidents} incident${h.incidents > 1 ? "s" : ""}` : "no incidents"} · ${rl}` : "…";
  const peak = h?.peak || {};
  const cpuAvg = avg(h?.cpu), msAvg = avg(h?.ms);
  const tiles = [
    tile("Uptime", upText, inc, up != null && up < 99 ? "warn" : ""),
    tile("CPU", running && s.cpu != null ? `${s.cpu.toFixed(1)}<small>%</small>` : "—",
      h && peak.cpu != null ? `avg ${cpuAvg.toFixed(1)}% · peak ${peak.cpu.toFixed(1)}%` : "of the whole Pi"),
    tile("Memory", running && s.mem != null ? `${mb(s.mem).replace(/ (MB|GB)$/, "<small> $1</small>")}` : "—",
      h && peak.mem != null ? `peak ${mbText(peak.mem)} · ${rl}` : ""),
  ];
  if (s.health || h?.config?.health) {
    tiles.push(tile("Health check", s.health ? (s.health.ok ? `${s.health.ms}<small> ms</small>` : "Failing") : "—",
      msAvg != null ? `avg ${Math.round(msAvg)} ms · peak ${Math.round(peak.ms)} ms` : "response time", s.health && !s.health.ok ? "bad" : ""));
  }
  put($("#sp-tiles"), tiles.join(""));
}

/** What runs now, what an update would bring, and the auto-update switch. */
function renderCode(s, now) {
  const g = s.source, c = pageData?.code;
  const commit = (x) => `<li><code>${esc(x.sha)}</code><span class="subject" title="${esc(x.subject)}">${esc(x.subject)}</span><span class="when">${esc(x.author)} · ${ago(x.time, now)}</span></li>`;
  let html = "";
  if (g?.kind === "git") {
    html += `<p class="code-now">${icon("git")}Running <code>${esc(g.sha)}</code> from ${ago(g.time, now)}${g.dirty ? ` · <span class="warn-text">local changes</span>` : ""}</p>`;
    if (c?.pending?.length) html += `<h4>${c.pending.length} new on GitHub${g.behind > c.pending.length ? ` (newest ${c.pending.length} shown)` : ""}: what Update brings</h4><ul class="commits new">${c.pending.map(commit).join("")}</ul>`;
    else if (!g.behind) html += `<p class="muted small">Up to date with GitHub${g.checked ? `, checked ${ago(g.checked, now)}` : ""}.</p>`;
    if (c?.recent?.length) html += `<h4>Running now</h4><ul class="commits">${c.recent.map(commit).join("")}</ul>`;
  } else if (g?.kind === "image") {
    html += `<p class="code-now">${icon("box")}<code>${esc(g.image)}</code></p><p class="muted small">Running <code>${esc(g.sha || "?")}</code>${g.time ? `, built ${ago(g.time, now)}` : ""}. `
      + (g.behind ? `<b class="accent-text">A newer image was published.</b>` : `That's the newest published image${g.checked ? `, checked ${ago(g.checked, now)}` : ""}.`) + `</p>`;
  } else {
    html = `<p class="muted small">Not tracked: it isn't a git checkout or a published image, so there's nothing to compare against.</p>`;
  }
  if (g?.check_failed) html += `<p class="warn-text small">Couldn't check for updates last time.</p>`;
  put($("#sp-code"), html);
  put($("#sp-auto"), s.can_update ? `<label class="auto" title="Checks every 5 minutes and updates by itself when there's something new"><input type="checkbox" data-svc="${esc(s.name)}" ${s.auto_update ? "checked" : ""}><span class="switch"></span>Auto-update</label>` : "");
}

/** The slow part, every minute or on a new range: charts, the status strip, history, setup. */
function renderHistory() {
  const h = pageData, now = h.now;
  const [rl, , tickHours] = RANGE_TEXT[pageRange];
  const ticks = clockTicks(h.since, now, window.innerWidth < 600 && tickHours < 24 ? tickHours * 2 : tickHours);
  drawTimeline($("#sp-timeline"), h.timeline, h.since, now, ticks);
  const up = h.uptime == null ? "no data yet" : `${h.uptime >= 99.95 ? "100" : h.uptime.toFixed(h.uptime >= 99 ? 2 : 1)}% up over ${rl}`;
  put($("#sp-uptime"), up);
  const peak = h.peak || {};
  chart($("#sp-cpu"), { since: h.since, step: h.step, area: true, series: [{ label: "CPU", cls: "s0", data: h.cpu }], floor: 1, ticks,
    fmt: (v, top) => `${v.toFixed((top ?? v) < 5 ? 1 : 0)}%`, label: `CPU over ${rl}, peak ${peak.cpu ?? "—"}%` });
  chart($("#sp-mem"), { since: h.since, step: h.step, area: true, series: [{ label: "Memory", cls: "s0", data: h.mem }], floor: 50, ticks,
    fmt: (v) => mbText(v), label: `Memory over ${rl}, peak ${peak.mem != null ? mbText(peak.mem) : "—"}` });
  put($("#sp-mem-note"), peak.mem != null ? `peak ${mbText(peak.mem)}` : "");
  $("#sp-ms-card").hidden = !h.config.health;
  if (h.config.health) {
    chart($("#sp-ms"), { since: h.since, step: h.step, area: true, series: [{ label: "Response time", cls: "s0", data: h.ms }], floor: 10, ticks,
      fmt: (v) => `${Math.round(v)} ms`, label: `Health check response time over ${rl}` });
  }
  const cfg = h.config;
  const row = (k, v) => (v ? `<dt>${k}</dt><dd>${v}</dd>` : "");
  put($("#sp-setup"), [
    row("Runs as", { compose: "Docker Compose", "systemd-user": "systemd user unit", systemd: "systemd unit" }[cfg.kind] || esc(cfg.kind)),
    row("Container", cfg.container && `<code>${esc(cfg.container)}</code>`),
    row("Unit", cfg.unit && `<code>${esc(cfg.unit)}</code>`),
    row("Folder", cfg.dir && `<code>${esc(cfg.dir)}</code>${cfg.repo ? ` (code in <code>${esc(cfg.repo)}</code>)` : ""}`),
    row("Health check", cfg.health && `<code>${esc(cfg.health)}</code>`),
    row("Extra check", cfg.check && `<code>${esc(cfg.check)}</code>`),
    row("Update steps", cfg.update?.length ? `<pre>${cfg.update.map(esc).join("\n")}</pre>` : "none: Update is off"),
    row("Link", cfg.url && `<a href="${esc(cfg.url)}" target="_blank" rel="noopener">${esc(cfg.url)}</a>`),
  ].join(""));
  renderEvents($("#sp-feed"), h.events, now, `Nothing happened to it in the last ${rl}.`);
}

/** A click anywhere on a card that isn't a control opens the service's page. */
$("#services").addEventListener("click", (e) => {
  if (e.target.closest("a, button, label, input") || String(window.getSelection())) return;
  const card = e.target.closest(".svc[data-name]");
  if (card) location.hash = `service/${encodeURIComponent(card.dataset.name)}`;
});

// ---- start ---------------------------------------------------------------------------

buildStats();
syncFilter();
$("#services").innerHTML = `<div class="svc skel-card" style="height:150px"></div>`.repeat(2);
document.querySelectorAll(".skel-card").forEach((c) => c.classList.add("skel"));
route();
refresh();
