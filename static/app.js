"use strict";

const $ = (s, root = document) => root.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const icon = (name) => `<svg class="i" aria-hidden="true"><use href="#i-${name}"/></svg>`;

const STATE_LABEL = { up: "Running", unhealthy: "Unhealthy", starting: "Starting", down: "Down", stopped: "Stopped", missing: "Not created", unknown: "Unknown" };
const KIND_LABEL = { compose: "Docker", "systemd-user": "systemd", systemd: "systemd" };
const LEVEL_ICON = { error: "x-circle", warn: "alert", ok: "check", boot: "restart", digest: "sun", info: "info", update: "update", agent: "sparkle" };
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
const size = (b) => (b >= 1024 ** 4 ? `${(b / 1024 ** 4).toFixed(1)} TB` : `${gb(b)} GB`);
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
/** Alert text marks moments the way Discord does (<t:1792794850:t>, :f with the date), so readers see their own time. */
const moments = (s) => String(s ?? "").replace(/<t:(\d+):([tf])>/g, (_, t, f) => f === "t" ? hm(+t)
  : new Date(t * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }));
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
/** Empty an element, and forget what put() last wrote there so its next put() isn't skipped. */
function clear(el) { el.innerHTML = ""; el._html = null; }
function cls(el, value) { if (el.className !== value) el.className = value; }

// ---- api + toasts ---------------------------------------------------------------

async function api(path, opts = {}) {
  const res = await fetch(path, { ...opts, headers: { "X-Pi-Dash": "1", "Content-Type": "application/json", ...(opts.headers || {}) } });
  if (res.status === 401) { location.reload(); throw new Error("Signed out"); }  // shows the sign-in page
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(body.error || res.statusText), { status: res.status });
  return body;
}

function toast(text, bad = false) {
  const box = $("#toasts");
  // The same message again (a button clicked twice) keeps the one showing; and never more than three at once.
  let el = [...box.children].find((t) => t._text === text && !t.classList.contains("out"));
  if (!el) {
    el = document.createElement("div");
    el.className = "toast" + (bad ? " bad" : "");
    el.innerHTML = icon(bad ? "x-circle" : "check") + `<span>${esc(text)}</span>`;
    el._text = text;
    box.append(el);
    while (box.children.length > 3) box.firstElementChild.remove();
  }
  clearTimeout(el._timer);
  el._timer = setTimeout(() => { el.classList.add("out"); setTimeout(() => el.remove(), 300); }, 3200);
}

// ---- Pi: stat cards with 24 h sparklines ----------------------------------------

const STATS = [
  { key: "cpu", icon: "cpu", label: "CPU", spark: true },
  { key: "mem", icon: "memory", label: "Memory", spark: true },
  { key: "temp", icon: "temp", label: "Temperature", spark: true },
  { key: "disk", icon: "disk", label: "Disk" },
];

function buildStats() {
  $("#stats").innerHTML = STATS.map((s) => `
    <div class="stat" id="stat-${s.key}">
      <div class="stat-head">${icon(s.icon)}<span>${s.label}</span></div>
      <div class="stat-value"><span class="skel">00%</span></div>
      <div class="stat-sub"><span class="skel">loading</span></div>
      ${s.spark
        ? `<svg class="spark" viewBox="0 0 100 34" preserveAspectRatio="none" role="img" aria-label="${s.label}, last 6 hours"><title>Last 6 hours</title><path class="area" d=""/><path class="line" d=""/></svg>`
        : `<div class="meter"><i></i></div><div class="meter-legend"><span class="used"></span><span class="free"></span></div><div class="drives" hidden></div>`}
    </div>`).join("");
  // key, icon, label, and a shorter label for phones ("" when the icon and value say it)
  $("#info").innerHTML = [["uptime", "clock", "Uptime", "Up"], ["power", "zap", "Power", "Power"], ["network", "wifi", "Network", ""],
    ["remote", "shield", "Remote access", ""], ["updates", "package", "OS updates", "Updates"], ["watchdog", "heart", "Outside watchdog", "Watchdog"]]
    .map(([key, ic, label, short]) => {
      const inner = `${icon(ic)}<span class="info-label" data-short="${short}">${label}</span><span class="info-value"><span class="skel">loading…</span></span>`;
      return key === "network"  // opens the list of Wi-Fi networks to switch to
        ? `<div class="menu-wrap"><button class="info-cell" id="info-network" data-menu aria-haspopup="menu" aria-expanded="false">${inner}<svg class="i chev" aria-hidden="true"><use href="#i-chevron"/></svg></button><div class="menu wifi-menu" id="wifi-menu" role="menu"></div></div>`
        : `<div class="info-cell" id="info-${key}" tabindex="0">${inner}</div>`;
    })
    .join("");
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
  el.title = `${$(".info-label", el).textContent}: ${title || $(".info-value", el).textContent}`;
}

function renderPi(data) {
  const pi = data.pi, h = data.history || {};
  const memUsed = pi.mem.total - pi.mem.available;
  const memPct = (100 * memUsed) / pi.mem.total;
  const lvl = (v, warn, bad) => (v >= bad ? "bad" : v >= warn ? "warn" : "");
  const peaks = h.peak || {};
  const peakText = (key, unit) => (peaks[key] != null ? ` · 6 h peak ${peaks[key].toFixed(0)}${unit}` : "");

  setStat("cpu", { value: `${pi.cpu.toFixed(0)}<small>%</small>`, sub: `load ${pi.load[0].toFixed(2)}${peakText("cpu", "%")}`, level: lvl(pi.cpu, 70, 90), series: h.cpu, range: [0, 100] });
  setStat("mem", { value: `${gb(memUsed)}<small> / ${gb(pi.mem.total)} GB</small>`, sub: `${mb(pi.mem.available)} available`, level: lvl(memPct, 80, 90), series: h.mem, range: [0, 100] });
  if (pi.temp == null) setStat("temp", { value: "—", sub: "no temperature sensor", series: [], range: [20, 90] });
  else setStat("temp", { value: `${pi.temp.toFixed(0)}<small>°C</small>`, sub: (pi.temp >= 80 ? "throttling" : pi.temp >= 70 ? "warm" : "cool") + peakText("temp", "°C"), level: lvl(pi.temp, 70, 80), series: h.temp, range: [20, 90] });
  renderDrives(pi, lvl);

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

const drivePct = (d) => (100 * d.used) / d.total;

/** The Disk card: one big bar for a single drive; with more, the fullest in the headline and a bar per drive. */
function renderDrives(pi, lvl) {
  const drives = pi.drives?.length ? pi.drives : [{ name: "SD card", path: "/", ...pi.disk }];
  const present = drives.filter((d) => !d.missing && d.total), missing = drives.filter((d) => d.missing);
  const el = $("#stat-disk"), list = $(".drives", el);
  const many = drives.length > 1;
  $(".meter", el).hidden = $(".meter-legend", el).hidden = many;
  list.hidden = !many;
  if (!present.length) return setStat("disk", { value: "—", sub: missing.length ? "drive missing" : "no drive found", level: missing.length ? "warn" : "" });
  const top = present.reduce((a, d) => (drivePct(d) > drivePct(a) ? d : a));
  const pct = drivePct(top);
  const level = lvl(pct, 80, 90) || (missing.length ? "warn" : "");
  if (!many) {
    return setStat("disk", { value: `${pct.toFixed(0)}<small>%</small>`, sub: `${size(top.free)} free`, level, pct,
      used: `${size(top.used)} used`, free: size(top.total) });
  }
  const sub = missing.length ? `${missing.map((d) => d.name).join(", ")} missing` : `${top.name} fullest · ${size(top.free)} free`;
  setStat("disk", { value: `${pct.toFixed(0)}<small>%</small>`, sub: esc(sub), level });
  put(list, drives.map((d) => {
    if (d.missing) {
      return `<div class="drive warn" title="${esc(`${d.path} is in /etc/fstab but isn't mounted`)}"><span class="drive-name">${esc(d.name)}</span><span class="drive-bar"><i></i></span><span class="drive-pct">missing</span></div>`;
    }
    const p = drivePct(d);
    const title = [d.path, d.model, `${size(d.used)} of ${size(d.total)} used`, `${size(d.free)} free`].filter(Boolean).join(" · ");
    return `<div class="drive ${lvl(p, 80, 90)}" title="${esc(title)}"><span class="drive-name">${esc(d.name)}</span><span class="drive-bar"><i style="width:${Math.min(100, p).toFixed(1)}%"></i></span><span class="drive-pct">${p.toFixed(0)}%</span></div>`;
  }).join(""));
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
  const bad = jobs.filter((j) => ["failed", "late", "off", "missing"].includes(j.state)).length;
  put($("#jobs-count"), bad ? `${bad} need${bad > 1 ? "" : "s"} attention` : `${jobs.length} on schedule`);
  put($("#jobs"), jobs.map((j) => {
    const [label, tone] = JOB_STATE[j.state] || [esc(j.state), ""];
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
  const firstData = Math.min(...opts.series.map((s) => { const i = s.data.findIndex((v) => v != null); return i < 0 ? n : i; }));
  if (firstData >= n) {
    svg += `<text class="nodata" x="${f(L + PW / 2)}" y="${f(T + PH / 2)}">No data yet</text>`;
  } else if (firstData / n > 0.15) {  // recording started partway through the range
    const x0 = x(opts.since + firstData * opts.step);
    svg += `<rect class="unrecorded" x="${L}" y="${T}" width="${f(x0 - L)}" height="${PH}"/>`;
    if (x0 - L > 90) svg += `<text class="nodata" x="${f((L + x0) / 2)}" y="${f(T + PH / 2)}">Not recorded</text>`;
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
        <div class="svc-title"><span class="dot"></span><h3><a class="svc-link"></a></h3><span class="state-text"></span><span class="badge"></span></div>
        <p class="svc-desc"></p>
      </div>
      <div class="actions"></div>
    </div>
    <p class="problem" hidden></p>
    <div class="facts"></div>
    <div class="uptime">
      <div class="timeline"><div class="track"></div><div class="ticks"></div></div>
      <div class="uptime-pct"></div>
    </div>
    <div class="source-row">
      <div class="git"></div>
      <label class="auto" title="Checks every 5 minutes and updates by itself when there's something new"><input type="checkbox"><span class="switch"></span>Auto-update</label>
    </div>`;
  return el;
}

const WORKING = { start: "Starting…", stop: "Stopping…", restart: "Restarting…", update: "Updating…" };

/** The buttons you need right now (Open, Logs, and Start or Update when they matter); the rest in a ⋯ menu. */
function actionButtons(s) {
  const working = busy.get(s.name) || s.busy;
  const running = RUNNING.has(s.state);
  const behind = s.source?.behind || 0;
  const name = esc(s.name);
  const b = (action, label, ic, extra = "", count = "") =>
    `<button class="btn ${extra}" data-svc="${name}" data-action="${action}" ${working ? "disabled" : ""}>${icon(ic)}<span>${label}</span>${count}</button>`;
  const item = (action, label, ic, extra = "") =>
    `<button role="menuitem" class="${extra}" data-svc="${name}" data-action="${action}">${icon(ic)}${label}</button>`;
  let host = "";
  try { host = s.url ? new URL(s.url).host : ""; } catch { /* not a full URL: the button still works */ }
  const shown = [];
  if (working) shown.push(`<span class="working" role="status"><span class="spin small"></span>${WORKING[working] || "Working…"}</span>`);
  else if (!running) shown.push(b("start", "Start", "play", "accent"));
  else if (s.can_update && behind) shown.push(b("update", "Update", "update", "accent", `<span class="count">${behind}</span>`));
  if (s.url) shown.push(`<a class="btn" href="${esc(s.url)}" target="_blank" rel="noopener" title="Open ${esc(host || s.url)}">${icon("external")}<span>Open</span></a>`);
  shown.push(b("logs", "Logs", "logs"));
  const menu = [
    running ? item("restart", "Restart", "restart") : "",
    s.can_update && !(running && behind) ? item("update", "Update now", "update") : "",  // else it's a button already
    running ? `<hr>${item("stop", "Stop", "stop", "danger")}` : "",
  ].join("");
  if (menu && !working) {
    shown.push(`<div class="menu-wrap"><button class="btn icon" data-menu aria-haspopup="menu" aria-expanded="false" aria-label="More actions for ${name}" title="More">${icon("more")}</button><div class="menu" role="menu">${menu}</div></div>`);
  }
  return shown.join("");
}

function renderServices(data) {
  const box = $("#services");
  if (!data.services.length) {
    put(box, `<div class="empty">No services yet. Add them to <code>~/services/services.json</code>, then restart pi-dash.</div>`);
    cards.clear();
    return;
  }
  if (box.querySelector(".empty, .skel-card")) clear(box);
  const up = data.services.filter((s) => s.state === "up").length, total = data.services.length;
  put($("#svc-count"), up === total ? `${total} running` : `${up} of ${total} running`);
  const seen = new Set();
  data.services.forEach((s, i) => {
    seen.add(s.name);
    let el = cards.get(s.name);
    if (!el) { el = buildCard(s.name); el.style.animationDelay = `${i * 60}ms`; cards.set(s.name, el); box.append(el); }
    const foot = !!(s.source || s.can_update);
    cls(el, `svc ${s.state}${foot ? "" : " no-foot"}${el.classList.contains("appear") ? " appear" : ""}`);
    $(".source-row", el).hidden = !foot;
    cls($(".dot", el), `dot ${s.state}`);
    const link = $(".svc-link", el);
    put(link, esc(s.name) + icon("chevron"));
    link.href = `#service/${encodeURIComponent(s.name)}`;
    link.title = `${s.name}: charts, history and setup`;
    put($(".badge", el), KIND_LABEL[s.kind] || esc(s.kind));
    const st = $(".state-text", el);
    cls(st, `state-text ${s.state}`);
    put(st, STATE_LABEL[s.state] || esc(s.state));
    put($(".svc-desc", el), esc(s.description));

    const prob = $(".problem", el);
    prob.hidden = !s.problem;
    put(prob, s.problem ? `${icon("alert")}<span>${esc(s.problem)}</span>` : "");

    const running = RUNNING.has(s.state);
    const facts = [
      s.since && `<span>${running ? "Up" : "Stopped"} <b>${running ? dur(data.now - s.since) : ago(s.since, data.now)}</b></span>`,
      running && s.cpu != null && `<span>CPU <b>${s.cpu.toFixed(1)}%</b></span>`,
      running && s.mem != null && `<span>Memory <b>${mb(s.mem)}</b></span>`,
      s.health && `<span>Health <b class="${s.health.ok ? "" : "bad-text"}">${s.health.ok ? `${s.health.ms} ms` : "failing"}</b></span>`,
      s.restarts ? `<span>Restarts <b>${s.restarts}</b></span>` : "",
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
    const label = `${when(a)}–${hm(b)} · ${STATE_LABEL[st] || esc(st)} (${len})`;
    return `<i class="${esc(st)}" style="left:${pos(a).toFixed(3)}%;width:${(pos(b) - pos(a)).toFixed(3)}%" title="${label}"></i>`;
  }).join(""));
  put($(".ticks", el), ticks.filter(([t]) => t > since + span * 0.04 && t < now - span * 0.1)  // none past the start, none crowding "now"
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
function dayLabel(t, now) {
  const d = new Date(t * 1000), today = new Date(now * 1000), yesterday = new Date(now * 1000);
  yesterday.setDate(today.getDate() - 1);
  if (d.toDateString() === today.toDateString()) return "Today";
  if (d.toDateString() === yesterday.toDateString()) return "Yesterday";
  return d.toLocaleDateString([], { weekday: "long", month: "short", day: "numeric" });
}
/** Within the last hour "12 min ago"; otherwise the time of day (the day is in the group heading). */
const whenText = (t, now) => (now - t < 3600 ? ago(t, now) : hm(t));

/** Back-to-back events that say the same thing on the same day, e.g. one service updating again and again. */
function foldRepeats(shown) {
  const runs = [];
  for (const e of shown) {
    const run = runs[runs.length - 1], a = run?.[0];
    if (a && a.level === e.level && a.title === e.title && a.service === e.service
        && new Date(a.t * 1000).toDateString() === new Date(e.t * 1000).toDateString()) run.push(e);
    else runs.push([e]);
  }
  return runs;
}

/** Puts each row after the previous one, moving it only when it's out of place (so open rows don't jump). */
function placer(parent) {
  let prev = null;
  return (li) => {
    if (prev ? prev.nextElementSibling !== li : parent.firstElementChild !== li) prev ? prev.after(li) : parent.prepend(li);
    prev = li;
  };
}

function setWhen(li, t, now) {
  const el = $(":scope > .ev .ev-when .t", li);
  const text = whenText(t, now);
  if (el.textContent !== text) el.textContent = text;
  el.title = new Date(t * 1000).toLocaleString();
}

/** One event. Inside a folded group the icon and title are already on the group's row, so it shows just the detail. */
function eventRow(e, existing, now, fresh, nested = false) {
  const key = `${e.t}`;
  let li = existing.get(key);
  if (!li) {
    li = document.createElement("li");
    li.dataset.key = key;
    if (fresh) li.classList.add("enter");
    const text = moments(e.detail);
    const expandable = (e.log && e.log.length) || text.length > 140;
    const tag = expandable ? "button" : "div";
    const detail = text ? `<span class="ev-detail">${esc(expandable && !(e.log && e.log.length) ? text.slice(0, 140) + "…" : text)}</span>` : "";
    const body = nested && e.detail ? detail : `<span class="ev-title">${esc(e.title)}${e.service && !nested ? `<span class="chip">${esc(e.service)}</span>` : ""}</span>${detail}`;
    li.innerHTML = `
      <${tag} class="ev${e.detail ? " has-detail" : ""}" ${expandable ? `aria-expanded="false"` : ""}>
        ${nested ? "" : `<span class="ev-ico ${esc(e.level)}">${icon(LEVEL_ICON[e.level] || "info")}</span>`}
        <span class="ev-body">${body}</span>
        <span class="ev-when"><span class="t"></span>${expandable ? icon("chevron") : ""}</span>
      </${tag}>
      ${expandable ? `<div class="ev-log"><div><pre>${(e.log && e.log.length ? e.log : text.split("\n")).map((l) => `<span>${esc(l)}</span>`).join("")}</pre>${!nested && ["error", "warn"].includes(e.level) ? `<div class="ev-ask"><button class="btn" type="button" data-ask-why>${icon("sparkle")}Ask why this happened</button></div>` : ""}</div></div>` : ""}`;
    li._event = e;
    if (openEvents.has(key)) li.classList.add("open");
  }
  setWhen(li, e.t, now);
  return li;
}

/** Several of the same event as one row ("selfmp3 updated · 4 times since 14:02") that opens to list them. */
function repeatRow(run, existing, now, isFresh) {
  const a = run[0], oldest = run[run.length - 1];
  const key = `run:${oldest.t}`;
  let li = existing.get(key);
  if (!li) {
    li = document.createElement("li");
    li.dataset.key = key;
    li.className = "repeats";
    li.innerHTML = `
      <button class="ev has-detail" aria-expanded="false">
        <span class="ev-ico ${esc(a.level)}">${icon(LEVEL_ICON[a.level] || "info")}</span>
        <span class="ev-body"><span class="ev-title">${esc(a.title)}${a.service ? `<span class="chip">${esc(a.service)}</span>` : ""}</span><span class="ev-detail"></span></span>
        <span class="ev-when"><span class="t"></span>${icon("chevron")}</span>
      </button>
      <div class="ev-log"><div><ol class="feed-sub"></ol></div></div>`;
    if (openEvents.has(key)) li.classList.add("open");
  }
  const summary = `${run.length} times since ${hm(oldest.t)}`;
  const d = $(":scope > .ev .ev-detail", li);
  if (d.textContent !== summary) d.textContent = summary;
  setWhen(li, a.t, now);
  const sub = $(".feed-sub", li);
  const members = new Map([...sub.children].map((m) => [m.dataset.key, m]));
  const place = placer(sub);
  const keep = new Set();
  for (const e of run) {
    const m = eventRow(e, members, now, isFresh(e), true);
    keep.add(m.dataset.key);
    place(m);
  }
  for (const [k, m] of members) if (!keep.has(k)) m.remove();
  return li;
}

/** Newest first, grouped by day, repeats folded; new rows slide in, expanded rows stay open across refreshes. */
function renderEvents(list, shown, now, emptyText) {
  const first = !list._rendered;
  if (!shown.length) {
    put(list, `<li class="none">${emptyText}</li>`);
    list._rendered = true;
    return;
  }
  list.querySelector(".none")?.remove();
  list._html = null;
  const isFresh = (e) => !first && e.t > (list._newest || 0);
  const existing = new Map([...list.children].map((li) => [li.dataset.key, li]));
  const keep = new Set();
  const place = placer(list);
  let lastDay = null;
  for (const run of foldRepeats(shown)) {
    const day = new Date(run[0].t * 1000).toDateString();
    if (day !== lastDay) {
      lastDay = day;
      const dkey = `day:${day}`;
      keep.add(dkey);
      let dli = existing.get(dkey);
      if (!dli) { dli = document.createElement("li"); dli.className = "day"; dli.dataset.key = dkey; }
      const label = dayLabel(run[0].t, now);
      if (dli.textContent !== label) dli.textContent = label;
      place(dli);
    }
    const li = run.length > 1 ? repeatRow(run, existing, now, isFresh) : eventRow(run[0], existing, now, isFresh(run[0]));
    keep.add(li.dataset.key);
    place(li);
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
  // The last lines are the ones that explain a crash, so open at the bottom.
  const pre = $(":scope > .ev-log pre", li);
  if (pre && li.classList.contains("open")) pre.scrollTop = pre.scrollHeight;
  li.classList.contains("open") ? openEvents.add(li.dataset.key) : openEvents.delete(li.dataset.key);
});

$("#feed-filter").addEventListener("click", (ev) => {
  const b = ev.target.closest("button[data-filter]");
  if (!b) return;
  feedFilter = b.dataset.filter;
  localSet("feedFilter", feedFilter);
  syncFilter();
  clear($("#feed"));
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
  const drives = pi.drives || [{ path: "/", ...pi.disk }];
  const piBad = pi.power.flags.some((f) => f.endsWith("now")) || pi.temp >= 80 || drives.some((d) => !d.missing && d.total && d.used / d.total >= 0.9);
  const lost = drives.filter((d) => d.missing);
  let c = "up", text = "All systems normal";
  if (bad.length || piBad) { c = "down"; text = bad.length ? `${bad.map((s) => s.name).join(", ")} ${bad.length > 1 ? "are" : "is"} down` : "The Pi needs attention"; }
  else if (meh.length) { c = "warn"; text = `${meh.map((s) => s.name).join(", ")} ${meh.every((s) => s.state === "starting") ? "starting" : "unhealthy"}`; }
  else if (lost.length) { c = "warn"; text = `Drive missing: ${lost.map((d) => d.path).join(", ")}`; }
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
  setBanner(wifiBanner(data) || (muted ? { html: `${icon("bell-off")}<span class="grow">Discord alerts are muted until <b>${hm(data.muted_until)}</b>. Problems still show up here.</span><button class="btn" data-mute="0">Turn back on</button>` } : null));
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
  if (off === bannerOffline && !(off && wifiWatch)) return;
  bannerOffline = false;
  if (off) {
    setBanner(wifiWatch ? { html: wifiOfflineText() } : { bad: true, html: `${icon("alert")}<span class="grow">Can't reach the Pi. Retrying every few seconds…</span>` });
    bannerOffline = true;
    cls($("#overall"), wifiWatch ? "pill warn" : "pill down");
    put($("#overall .pill-text"), wifiWatch ? "Switching Wi-Fi" : "Offline");
  } else if (last) {
    setBanner(null);
  }
}

// ---- refresh loop ---------------------------------------------------------------------

function render(data) {
  if (PAGE_VERSION && data.version && data.version !== PAGE_VERSION && !$("#drawer").classList.contains("open") && !$("dialog[open]")
      && !agentOpen()) {
    location.reload();  // load the new page; waits while logs, the assistant or a dialog are open
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
  const ask = $("#ask-btn");
  if (!askOff && !ask.classList.contains("checking")) {  // a greyed-out button keeps saying why
    ask.classList.toggle("attn", data.assistant_waiting > 0);
    ask.title = data.assistant_waiting ? "A chat is waiting for you" : "Ask the assistant (/)";
  }
  if (page) renderPage(data);
  if (firstRender) document.body.classList.remove("loading");
}

let inflight = false, again = false;
async function refresh() {
  if (inflight) { again = true; return; }  // e.g. right after an action: refresh once this one is back
  inflight = true;
  let data;
  try {
    data = await api("/api/state", { signal: AbortSignal.timeout(10000) });  // a stalled link counts as offline
  } catch {
    if (Date.now() - lastOk > 8000 || !last) setOffline(true);
    return;
  } finally {
    inflight = false;
    if (again) { again = false; setTimeout(refresh); }
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
let drawerGen = 0;  // bumped on every open: a request that comes back later for an older drawer is dropped

function drawerStatus(text, dot = "") {
  $("#drawer-sub-text").textContent = text;
  cls($("#drawer-sub .live-dot"), `live-dot ${dot}`);
}
function openDrawer(title) {
  closeStreams();
  drawerGen++;
  $("#drawer-title").textContent = title;
  log.innerHTML = `<div class="placeholder">Loading…</div>`;
  // A filter from the last drawer would quietly hide lines here, maybe with its controls hidden too.
  $("#log-search").value = ""; filterText = "";
  $("#errors-only").checked = false; log.classList.remove("errors-only");
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
  let pending = [], timer = 0, initial = true, opened = false;
  const es = stream = new EventSource(url);
  es.onopen = () => {
    if (stream !== es) return;
    if (opened) {  // a reconnect sends the last lines again: start over rather than show them twice
      clearTimeout(timer);
      pending = []; timer = 0; initial = true;
      log.innerHTML = "";
    }
    opened = true;
    drawerStatus("Live: new lines appear as they're written");
  };
  es.onmessage = (ev) => {
    pending.push(JSON.parse(ev.data));
    if (!timer) timer = setTimeout(() => {
      if (stream !== es) return;
      appendLines(pending, !initial);
      pending = []; timer = 0; initial = false;
    }, initial ? 150 : 60);
  };
  es.onerror = () => {
    if (stream !== es) return;
    if (es.readyState === EventSource.CLOSED) drawerStatus("Disconnected. Close and open the logs again.", "bad");
    else drawerStatus("Reconnecting…", "off");
  };
  setTimeout(() => { if (stream === es && log.querySelector(".placeholder")) log.innerHTML = `<div class="placeholder">No log lines yet.</div>`; }, 2500);
}

/** A container that logs somewhere pi-dash can't read: say why there's nothing, and how to fix it. */
function showLogDriverHelp(s) {
  openDrawer(s.name);
  $("#log-tools").hidden = true;
  drawerStatus(`Logs go to Docker's ${s.log_driver} driver, not the system journal`, "off");
  log.innerHTML = `<div class="placeholder help">
    <p><b>pi-dash can't see ${esc(s.name)}'s logs.</b> Its container writes them with Docker's <code>${esc(s.log_driver)}</code> log driver,
    and pi-dash reads the system journal. So there's nothing here, no alerts about errors in its log, and crash alerts come without log lines.</p>
    <p>To fix it, remove the <code>logging:</code> section from its compose file (or set <code>driver: journald</code>), then recreate the container:</p>
    <pre>cd ${esc(s.dir || "<its folder>")} && docker compose up -d</pre>
    <p>Until then: <code>docker logs -f ${esc(s.name)}</code> on the Pi.</p></div>`;
}

/** What a scheduled job sent along with its last ping. */
async function showOutput(name) {
  openDrawer(name);
  $("#log-tools").hidden = false;
  log.classList.remove("job");
  drawerStatus("Output sent with the last run", "off");
  const gen = drawerGen;
  try {
    const { lines } = await api(`/api/scheduled/${encodeURIComponent(name)}/output`);
    if (gen !== drawerGen) return;
    if (!lines.length) { log.innerHTML = `<div class="placeholder">The last run sent no output.</div>`; return; }
    log.innerHTML = "";
    appendLines(lines.map((msg) => ({ msg, error: /\b(error|fatal|failed)\b/i.test(msg) })), false);
  } catch (e) { if (gen === drawerGen) log.innerHTML = `<div class="placeholder">${esc(e.message)}</div>`; }
}

function showJob(job) {
  openDrawer(`${job.service}`);
  $("#log-tools").hidden = true;
  log.classList.add("job");
  drawerStatus(`${job.action[0].toUpperCase() + job.action.slice(1)} in progress…`, "busy");
  let seen = 0;
  const gen = drawerGen;
  const poll = async () => {
    try {
      const j = await api(`/api/jobs/${job.id}?from=${seen}`);
      if (gen !== drawerGen) return;
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
    } catch (e) {
      if (gen !== drawerGen) return;
      if (e.status === 404) return drawerStatus("Lost track of it: pi-dash restarted meanwhile", "bad");
      // otherwise offline for a moment: keep trying
    }
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
$("#scrim").onclick = () => (agentOpen() ? closeAgent() : closeDrawer());

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
  if (action === "logs") {
    const s = last?.services.find((x) => x.name === name);
    return s?.log_driver && s.log_driver !== "journald" ? showLogDriverHelp(s) : showLogs(name);
  }
  const svc = last?.services.find((s) => s.name === name);
  if (CONFIRM[action] && !(await confirmBox(...CONFIRM[action](name, svc)))) return;
  busy.set(name, action);
  const rerender = () => { if (last) { renderServices(last); if (page) renderPage(last); } };
  rerender();
  try {
    const job = await api(`/api/services/${encodeURIComponent(name)}/${action}`, { method: "POST" });
    if (action === "update") showJob(job);
    const wait = async () => {
      const j = await api(`/api/jobs/${job.id}?from=999999`)
        .catch((e) => (e.status === 404 ? { done: true, lost: true } : { done: false }));  // 404: pi-dash restarted
      if (!j.done) return setTimeout(wait, 600);
      busy.delete(name);
      const past = { start: "started", stop: "stopped", restart: "restarted", update: "updated" }[action];
      if (j.lost) toast(`Lost track of ${name}'s ${action}: pi-dash restarted meanwhile`, true);
      else if (j.ok) toast(`${name} ${past}`);
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
  if (!b) return;
  closeMenus();
  act(b.dataset.svc, b.dataset.action);
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
document.addEventListener("click", (e) => {
  const opener = e.target.closest("button[data-menu]");
  if (opener) {
    const m = opener.nextElementSibling;
    closeMenus(m);
    const open = m.classList.toggle("open");
    opener.setAttribute("aria-expanded", open);
    if (open) m.querySelector("button")?.focus({ preventScroll: true });
    return;
  }
  if (!e.target.closest(".menu")) closeMenus();
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if ($(".menu.open")) closeMenus();
  else if (agentOpen() && !$("dialog[open]")) closeAgent();
  else if ($("#drawer").classList.contains("open")) closeDrawer();
  else if (page && !$("dialog[open]")) location.hash = "";
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
  let name = null;
  try { name = m ? decodeURIComponent(m[1]) : null; } catch { /* a malformed link: show the home page */ }
  if (name === page) return;
  page = name;
  pageReq++;  // a history request still on its way is for the page we left
  $("#home").hidden = !!page;
  $("#svc-page").hidden = !page;
  clearTimeout(pageTimer);
  pageData = null;
  if (page) {
    for (const id of ["#sp-cpu", "#sp-mem", "#sp-ms"]) { clear($(id)); $(id)._key = null; }
    clear($("#sp-feed")); $("#sp-feed")._rendered = false;
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
  if (!page) return;
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
    cls($("#svc-page .page-head .dot"), "dot");
    put($("#sp-kind"), "");
    cls($("#sp-state"), "state-text");
    put($("#sp-state"), "Not in services.json");
    put($("#sp-desc"), "");
    $("#sp-problem").hidden = true;
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
  put($("#sp-desc"), esc(s.description));
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
    s.log_driver && s.log_driver !== "journald" ? `<span class="warn-text">Logs not in the journal (${esc(s.log_driver)})</span>` : "",
    s.health && !s.health.ok ? `<span>Health check <b>${esc(s.health.error || "failing")}</b></span>` : "",
  ].filter(Boolean).join(""));
  put($("#sp-actions"), actionButtons(s));
  renderTiles(s);
  renderCode(s, data.now);
}

function tile(label, value, sub, level = "") {
  return `<div class="stat ${level}"><div class="stat-head"><span>${label}</span></div><div class="stat-value">${value}</div><div class="stat-sub" title="${esc(sub)}">${sub}</div></div>`;
}

function renderTiles(s) {
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
    tile("CPU now", running && s.cpu != null ? `${s.cpu.toFixed(1)}<small>%</small>` : "—",
      h && peak.cpu != null ? `average ${cpuAvg.toFixed(1)}% · busiest minute ${peak.cpu.toFixed(0)}%` : "of the whole Pi"),
    tile("Memory now", running && s.mem != null ? `${mb(s.mem).replace(/ (MB|GB)$/, "<small> $1</small>")}` : "—",
      h && peak.mem != null ? `highest ${mbText(peak.mem)} · ${rl}` : ""),
  ];
  if (s.health || h?.config?.health) {
    tiles.push(tile("Health check", s.health ? (s.health.ok ? `${s.health.ms}<small> ms</small>` : "Failing") : "—",
      msAvg != null ? `average ${Math.round(msAvg)} ms · slowest ${Math.round(peak.ms)} ms` : "response time", s.health && !s.health.ok ? "bad" : ""));
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
  if (e.target.closest("a, button, label, input, .menu") || String(window.getSelection())) return;
  const card = e.target.closest(".svc[data-name]");
  if (card) location.hash = `service/${encodeURIComponent(card.dataset.name)}`;
});

// ---- Wi-Fi: pick a network; the Pi switches, checks the internet works, or goes back -------------

let wifiList = null;  // the last scan: {device, networks}
let wifiScan = 0;     // bumped per scan, so a slow one can't overwrite a newer one
let wifiWatch = null; // the switch this page is following: {ssid, started} (started is null until the Pi answers)

function bars(signal) {
  const lit = signal >= 75 ? 4 : signal >= 50 ? 3 : signal >= 25 ? 2 : 1;
  return `<svg class="bars" viewBox="0 0 16 16" aria-hidden="true">${[3, 6.3, 9.6, 13]
    .map((h, i) => `<rect x="${0.5 + i * 4}" y="${15 - h}" width="3" height="${h}" rx="1"${i < lit ? ' class="lit"' : ""}/>`).join("")}</svg>`;
}

/** What joining takes: "open" (OWE included: encrypted, no password), "password", or "login" (802.1X: a username
 *  and password of your own, as at offices and universities). */
const wifiNeeds = (n) => (/802\.1X/.test(n.security) ? "login" : /WPA|WEP/.test(n.security) ? "password" : "open");

function renderWifiMenu(scanning) {
  const m = $("#wifi-menu");
  const switching = Boolean(wifiWatch);
  const head = `<p class="menu-label"><span>Wi-Fi networks</span>${scanning ? `<span class="spin small" title="Scanning"></span>` : ""}</p>`;
  let body;
  if (!wifiList) body = `<p class="menu-note"><span class="spin small"></span>Looking for networks…</p>`;
  else if (wifiList.error) body = `<p class="menu-note">${icon("alert")}${esc(wifiList.error)}</p>`;
  else if (!wifiList.device) body = `<p class="menu-note">This machine has no Wi-Fi.</p>`;
  else if (!wifiList.networks.length) body = `<p class="menu-note">No networks in range.</p>`;
  else body = wifiList.networks.map((n, i) => {
    const needs = wifiNeeds(n);
    const what = [`${n.signal}% signal`, n.security || "open", n.saved && !n.active ? "saved" : ""].filter(Boolean).join(" · ");
    return `<button role="menuitem" class="wifi-row${n.active ? " on" : ""}" data-wifi="${i}" title="${esc(what)}"${switching ? " disabled" : ""}>
      ${bars(n.signal)}<span class="name">${esc(n.ssid)}</span>${n.saved && !n.active ? `<span class="saved">Saved</span>` : ""}${needs !== "open" ? icon("lock") : ""}${n.active ? icon("check") : ""}</button>`;
  }).join("");
  const hadFocus = m.contains(document.activeElement);
  put(m, head + body);
  if (hadFocus || document.activeElement === $("#info-network")) m.querySelector("button:not(:disabled)")?.focus({ preventScroll: true });
}

async function loadWifi() {
  const gen = ++wifiScan;
  renderWifiMenu(true);
  let list;
  try { list = await api("/api/wifi", { signal: AbortSignal.timeout(40000) }); }
  catch (e) { list = wifiList?.networks ? wifiList : { error: e.message }; }
  if (gen !== wifiScan) return;
  wifiList = list;
  renderWifiMenu(false);
}

/** Keep the menu on screen: it opens to the right of the chip, which can sit near the right edge on a phone. */
function placeWifiMenu() {
  const m = $("#wifi-menu");
  m.style.left = "0px";
  const over = m.getBoundingClientRect().right - (document.documentElement.clientWidth - 16);
  if (over > 0) m.style.left = `${-over}px`;
}

document.addEventListener("click", (e) => {
  if (e.target.closest("#info-network") && $("#wifi-menu").classList.contains("open")) { placeWifiMenu(); loadWifi(); }
  const row = e.target.closest("[data-wifi]");
  if (row && !row.disabled) {
    const n = wifiList.networks[Number(row.dataset.wifi)];
    closeMenus();
    if (!n.active) joinWifi(n);
  }
});

$("#wifi-show").addEventListener("change", (e) => { $("#wifi-pass").type = e.target.checked ? "text" : "password"; });
$("#wifi-cancel").onclick = () => $("#wifi-dialog").close();  // not a submit button, so Enter in the password field joins

async function joinWifi(n) {
  const now = last?.pi.network?.wifi?.ssid;
  const login = wifiNeeds(n) === "login" && !n.saved;
  const needsPass = login || (wifiNeeds(n) === "password" && !n.saved);
  const dlg = $("#wifi-dialog"), pass = $("#wifi-pass"), user = $("#wifi-user");
  $("#wifi-title").textContent = `Join ${n.ssid}?`;
  $("#wifi-body").textContent = `This page drops out for a few seconds while the Pi switches, then reconnects by itself.`
    + (now ? ` If ${n.ssid} doesn't connect or has no internet, the Pi goes back to ${now} within about a minute.` : "");
  $("#wifi-pass-row").hidden = !needsPass;
  $("#wifi-user-field").hidden = $("#wifi-cert-note").hidden = !login;
  user.value = "";
  user.required = login;
  pass.value = "";
  pass.type = "password";
  $("#wifi-show").checked = false;
  pass.required = needsPass;
  pass.minLength = needsPass && !login && !/WEP/.test(n.security) ? 8 : 0;  // WPA passwords are 8 to 63 characters
  dlg.returnValue = "";
  dlg.showModal();
  (login ? user : needsPass ? pass : $("#wifi-ok")).focus();
  const ok = await new Promise((resolve) => dlg.addEventListener("close", () => resolve(dlg.returnValue === "ok"), { once: true }));
  const password = needsPass ? pass.value : "", username = login ? user.value.trim() : "";
  pass.value = "";
  if (!ok) return;
  const before = last?.wifi_switch?.started ?? null;
  wifiWatch = { ssid: n.ssid, started: null, before, asked: Date.now() };
  renderWifiMenu(false);
  if (last) renderOverall(last);
  try {
    const sw = await api("/api/wifi", { method: "POST", body: JSON.stringify({ ssid: n.ssid, password, username }) });
    wifiWatch = { ...wifiWatch, started: sw.started };
  } catch (e) {
    if (e.status) { wifiWatch = null; toast(e.message, true); }  // the Pi said no; otherwise it may have started and dropped us
  }
  refresh();
}

const WIFI_PHASE = {
  joining: (s) => `Joining ${s.ssid}…`,
  checking: (s) => `Joined ${s.ssid}. Checking the internet works…`,
  "going-back": (s) => `${s.ssid} didn't work. Going back to ${s.previous}…`,
};

/** Follow a switch in /api/state: the banner while it runs, a toast when it's over. */
function wifiBanner(data) {
  const sw = data.wifi_switch;
  if (sw && sw.state !== "done" && wifiWatch?.started !== sw.started && !(wifiWatch && wifiWatch.started === null)) {
    wifiWatch = { ssid: sw.ssid, started: sw.started, asked: Date.now() - (data.now - sw.started) * 1000 };  // e.g. a reload mid-switch
  }
  if (!wifiWatch) return null;
  const mine = sw && (wifiWatch.started === null ? sw.started !== wifiWatch.before : sw.started === wifiWatch.started);
  if (!mine) {
    if (wifiWatch.started !== null || Date.now() - wifiWatch.asked > 20000) {  // pi-dash restarted, or the request never got there
      toast(`Lost track of the switch to ${wifiWatch.ssid}`, true);
      wifiWatch = null;
      return null;
    }
    return { html: `<span class="spin small"></span><span class="grow">Asking the Pi to join ${esc(wifiWatch.ssid)}…</span>` };
  }
  if (sw.state === "done") {
    toast(sw.message || (sw.ok ? `Connected to ${sw.ssid}` : `Couldn't join ${sw.ssid}`), !sw.ok);
    wifiWatch = null;
    wifiList = null;  // scan again next time
    if ($("#wifi-menu").classList.contains("open")) loadWifi();
    return null;
  }
  wifiWatch.started = sw.started;
  const phase = (WIFI_PHASE[sw.state] || WIFI_PHASE.joining)(sw);
  return { html: `<span class="spin small"></span><span class="grow">${esc(phase)} This page may drop out for a few seconds; it reconnects by itself.</span>` };
}

/** While the Pi is switching, the page losing it is expected: say so, and after a while, where else to look. */
function wifiOfflineText() {
  const ts = last?.pi.network?.tailscale;
  const hint = Date.now() - wifiWatch.asked > 90000 && ts?.name && !location.hostname.endsWith(".ts.net")
    ? ` If you opened this page by its local address, that can change on another network: try <b>${esc(ts.name)}</b>.` : "";
  return `<span class="spin small"></span><span class="grow">Switching Wi-Fi to ${esc(wifiWatch.ssid)}. Waiting for the Pi to come back…${hint}</span>`;
}

// ---- the assistant: Claude looks around the Pi on its own and asks before changing anything ---------------

const agentEl = $("#agent"), chatEl = $("#chat"), askInput = $("#ask-input");
let agentInfo = null;        // GET /api/agent: on, problem, chats, spent_today, budget, user, host
let chat = null;             // the open chat: {id, title, status, cost, seq, events (by number)}
let chatSource = null;       // its event stream
let chatGen = 0;             // bumped per chat shown: answers for one that's no longer open are dropped
let chatDown = 0;            // when the stream dropped (0: connected)
let reconnected = "";        // shown on a running card after the connection came back
let askContext = null;       // the Activity entry a new chat is about
let editing = null, editText = "";  // the card whose command is being edited, and the edit so far
let replyDraft = { call: null, text: "" };  // what's typed in a running command's reply box
const openSteps = new Set(), openOutput = new Set();  // "Looked at" boxes and outputs left open
const RISKY = /connection|Restarts|off|secrets/;

let askOff = "";  // why the Ask button is greyed out, or "" when it works

async function loadAgent() {
  let why = "";
  try {
    agentInfo = await api("/api/agent");
    why = agentInfo.on ? "" : agentInfo.problem;
  } catch (e) {
    agentInfo = null;
    why = e.status === 403 ? "The assistant only answers over Tailscale." : "Couldn't reach the assistant.";
  }
  askOff = why;
  const b = $("#ask-btn");
  cls(b, `btn ask${why ? " off" : b.classList.contains("attn") ? " attn" : ""}`);
  b.title = why || "Ask the assistant (/)";
  b.setAttribute("aria-disabled", why ? "true" : "false");
  document.body.classList.toggle("has-assistant", !why);
}

const agentOpen = () => agentEl.classList.contains("open");

async function openAgent({ chatId = null, context = null, ask = "" } = {}) {
  closeMenus();
  if ($("#drawer").classList.contains("open")) closeDrawer();
  agentEl.classList.add("open");
  agentEl.setAttribute("aria-hidden", "false");
  $("#scrim").classList.add("open");
  document.body.style.overflow = "hidden";
  await loadAgent();
  if (chatId) showChat(chatId);
  else showHome(context);
  if (ask) { askInput.value = ask; submitAsk(); }
}
function closeAgent() {
  stopChatStream();
  chatGen++;
  agentEl.classList.remove("open");
  agentEl.setAttribute("aria-hidden", "true");
  $("#scrim").classList.remove("open");
  document.body.style.overflow = "";
}
function stopChatStream() {
  if (chatSource) { chatSource.close(); chatSource = null; }
}

function agentSub(text, dot = "off") {
  $("#agent-sub-text").textContent = text;
  cls($("#agent-sub .live-dot"), `live-dot ${dot}`);
}
const whoText = () => (agentInfo?.host ? `${agentInfo.host} · runs as ${agentInfo.user}` : "");
const STATUS_TEXT = { thinking: ["Thinking…", "busy"], running: ["Running a command…", "busy"], waiting: ["Waiting for you", "busy"] };

/** A new chat: the recent ones to go back to, and the box to type in. */
function showHome(context = null) {
  stopChatStream();
  chatGen++;
  chat = null; editing = null; askContext = context;
  agentSub(whoText());
  const info = agentInfo || {};
  let html = "";
  if (info.problem) html += `<div class="note warn">${icon("alert")}<span>${esc(info.problem)}</span></div>`;
  if (info.chats?.length) {
    html += `<div><p class="recent-label">Recent</p><div class="recent">${info.chats.slice(0, 12).map((c) =>
      `<div class="recent-row"><button type="button" data-chat="${esc(c.id)}"><span>${esc(c.title)}</span>${c.status === "waiting" ? `<span class="t wait"><span class="live-dot busy"></span>Waiting for you</span>`
        : ["thinking", "running"].includes(c.status) ? `<span class="t">Working…</span>` : `<span class="t">${esc(ago(c.t))}</span>`}</button><button type="button" class="recent-del" data-del="${esc(c.id)}" title="Remove" aria-label="Remove this chat">${icon("x")}</button></div>`).join("")}</div></div>`;
  }
  chatEl._blocks = null;
  clear(chatEl);
  chatEl.innerHTML = html;
  renderContext();
  syncComposer();
  setTimeout(() => askInput.focus(), 50);
}

function showChat(id) {
  stopChatStream();
  const gen = ++chatGen;
  chat = { id, title: "", status: "", cost: 0, seq: 0, events: [] };
  editing = null; askContext = null; reconnected = "";
  renderContext();
  clear(chatEl);
  chatEl._blocks = new Map();
  chatEl._stick = true;
  connectChat(gen);
}

function connectChat(gen) {
  stopChatStream();
  const src = new EventSource(`/api/agent/chats/${encodeURIComponent(chat.id)}/stream?since=${chat.seq}`);
  chatSource = src;
  src.onmessage = (m) => {
    if (gen !== chatGen) return;
    if (chatDown) {
      const secs = Math.round((Date.now() - chatDown) / 1000);
      reconnected = `Lost the connection for ${secs} s. Reconnected; still running.`;
      chatDown = 0;
    }
    mergeChat(JSON.parse(m.data));
  };
  src.onerror = () => {  // reconnect ourselves, so it asks only for what changed since
    if (gen !== chatGen) return;
    src.close();
    if (!chatDown) chatDown = Date.now();
    agentSub("Reconnecting…", "bad");
    setTimeout(async () => {
      if (gen !== chatGen || !agentOpen()) return;
      // Gone (removed elsewhere) rather than unreachable: back to the list instead of retrying forever.
      const r = await fetch(`/api/agent/chats/${encodeURIComponent(chat.id)}?since=${chat.seq}`).catch(() => null);
      if (gen !== chatGen) return;
      if (r?.status === 404) { await loadAgent(); showHome(); } else connectChat(gen);
    }, 2000);
  };
}

function mergeChat(d) {
  Object.assign(chat, { title: d.title, status: d.status, cost: d.cost, tokens: d.tokens, seq: Math.max(chat.seq, d.seq) });
  for (const e of d.events) chat.events[e.n] = e;
  if (!chat.events.some((e) => e?.type === "ask" && e.state === "running")) reconnected = "";
  const [text, dot] = STATUS_TEXT[chat.status] || [whoText(), "off"];
  agentSub(text, dot);
  renderChat();
  syncComposer();
}

/** The little Markdown the assistant writes: paragraphs, lists, **bold**, `code`, fenced code. Escaped first. */
function md(text) {
  // Code spans first (stars inside them are literal), then bold, which may wrap code: **`uptime` worked**.
  const inline = (s) => {
    const codes = [];
    s = s.replace(/`([^`]+)`/g, (_, c) => `\u0000${codes.push(c) - 1}\u0000`);
    return s.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[i]}</code>`);
  };
  const out = [];
  let para = [], list = null, fence = null;
  const flush = () => {
    if (para.length) out.push(`<p>${para.map(inline).join("<br>")}</p>`);
    if (list) out.push(`<${list.tag}>${list.items.map((i) => `<li>${inline(i)}</li>`).join("")}</${list.tag}>`);
    para = []; list = null;
  };
  for (const line of esc(text).split("\n")) {
    if (fence) {
      if (/^\s*```/.test(line)) { out.push(`<pre>${fence.join("\n")}</pre>`); fence = null; } else fence.push(line);
      continue;
    }
    if (/^\s*```/.test(line)) { flush(); fence = []; continue; }
    const item = line.match(/^\s*(?:(\d+)[.)]|[-*•])\s+(.*)$/);
    if (item) {
      const tag = item[1] ? "ol" : "ul";
      if (para.length || (list && list.tag !== tag)) flush();
      list ??= { tag, items: [] };
      list.items.push(item[2]);
    } else if (!line.trim()) flush();
    else { if (list) flush(); para.push(line); }
  }
  if (fence) out.push(`<pre>${fence.join("\n")}</pre>`);
  flush();
  return out.join("");
}

const mmss = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

function stepsHtml(items, key) {
  const running = items.some((s) => s.status === "running");
  const n = items.length;
  return `<details class="steps" data-steps="${key}"${openSteps.has(key) ? " open" : ""}>
    <summary>${icon("search")}<span>${running ? "Looking…" : "Looked at"} <b>${n} thing${n > 1 ? "s" : ""}</b></span>${icon("chevron").replace('class="i"', 'class="i chev"')}</summary>
    ${items.map((s) => `<div class="step"><div class="step-row ${s.status}">${icon(s.status === "running" ? "clock" : s.status === "error" ? "x-circle" : "check")}<code>${esc(s.label)}</code><span class="t">${esc(hm(s.t))}</span></div>${s.output ? `<pre class="out">${esc(s.output)}</pre>` : ""}</div>`).join("")}
  </details>`;
}

function askHtml(e) {
  const risky = e.labels.some((l) => RISKY.test(l));
  const badges = e.labels.map((l) => `<span class="badge ${RISKY.test(l) ? "risk" : "change"}">${esc(l)}</span>`).join("");
  const head = (right) => `<div class="ask-head"><h4>${esc(e.title || "Run this?")}</h4>${badges}${right}</div>`;
  const cmd = e.tool !== "run_command" ? ""
    : editing === e.call ? `<textarea class="cmd" id="edit-cmd" spellcheck="false" autocapitalize="off">${esc(editText)}</textarea>`
    : `<div class="cmd">${esc(e.command)}</div>`;
  const edited = e.edited_from ? `<p class="edited">You edited it. It suggested <code>${esc(e.edited_from)}</code></p>` : "";
  const lines = (e.lines || []).map(esc).join("\n");
  const card = (extra, body) => `<div class="ask-card${extra}" data-call="${esc(e.call)}">${body}</div>`;
  if (e.state === "waiting") {
    const buttons = editing === e.call
      ? `<button class="btn" type="button" data-ask="cancel-edit">Cancel</button><button class="btn accent" type="button" data-ask="run">${icon("play")}Run this</button>`
      : `${e.tool === "run_command" ? `<button class="btn quiet" type="button" data-ask="edit">Edit</button>` : ""}<button class="btn" type="button" data-ask="skip">Not now</button><button class="btn accent" type="button" data-ask="run">${icon("play")}Run</button>`;
    return card(risky ? " risky" : "", `${head(`<span class="wait"><span class="live-dot busy"></span>Waiting for you</span>`)}
      ${e.why ? `<p class="ask-why">${esc(e.why)}</p>` : ""}${cmd}
      ${e.effects.length ? `<ul class="effects">${e.effects.map((x) => `<li>${icon("info")}<span>${esc(x)}</span></li>`).join("")}</ul>` : ""}
      <div class="ask-foot"><span class="grow">Runs as ${esc(agentInfo?.user || "you")}. Recorded in Activity.</span>${buttons}</div>`);
  }
  if (e.state === "declined") {
    return card(" settled", `${head("")}${cmd}<div class="result off">${icon("x-circle")}Not run</div>`);
  }
  if (e.state === "running") {
    const prompt = e.prompt ? `${lines ? "\n" : ""}${esc(e.prompt)}` : "";
    const secret = /pass(word|phrase)|secret|token|pin\b/i.test(e.prompt || "");
    return card(risky ? " risky" : "", `${head(`<span class="wait"><span class="live-dot busy"></span>Running · ${mmss(Date.now() / 1000 - (e.started || Date.now() / 1000))}</span>`)}
      ${cmd}${edited}<pre class="run-log">${lines}${prompt}</pre>
      ${e.tool === "run_command" ? `<form class="run-reply" data-reply="${esc(e.call)}"><input type="${secret ? "password" : "text"}" placeholder="${e.prompt ? "Answer it here, then Enter" : "Type to the command, then Enter"}" autocomplete="off" autocapitalize="off" spellcheck="false" aria-label="Reply to the command"><button class="btn icon quiet" type="submit" title="Send">${icon("send")}</button></form>` : ""}
      ${reconnected ? `<p class="edited">${icon("check")} ${esc(reconnected)}</p>` : ""}
      <div class="ask-foot"><span class="grow">You approved this at ${esc(hm(e.decided))}.</span><button class="btn" type="button" data-ask="stop">${icon("stop")}Stop</button></div>`);
  }
  const took = dur((e.finished || 0) - (e.started || 0));
  const result = e.state === "done" ? `<div class="result ok">${icon("check")}Done in ${took}</div>`
    : e.state === "failed" ? `<div class="result bad">${icon("x-circle")}Failed${e.exit != null ? ` with exit code ${e.exit}` : ""} after ${took}</div>`
    : `<div class="result off">${icon("stop")}Stopped after ${took}</div>`;
  return card(" settled", `${head(`<span class="wait">You approved · ${esc(hm(e.decided))}</span>`)}${cmd}${edited}
    ${lines ? `<details data-output="${esc(e.call)}"${openOutput.has(e.call) ? " open" : ""}><summary>${icon("logs")}Output (${e.lines.length} line${e.lines.length > 1 ? "s" : ""})</summary><pre class="run-log">${lines}</pre></details>`
    : `<p class="edited">No output</p>`}${result}`);
}

const NOTE_ICON = { error: "x-circle", warn: "alert", info: "info" };

function chatBlocks() {
  const out = [];
  let steps = null;
  for (const e of chat.events) {
    if (!e || e.gone || (e.type === "text" && !e.text)) continue;
    if (e.type === "step") {
      if (!steps) { steps = { key: `steps-${e.n}`, items: [] }; out.push(steps); }
      steps.items.push(e);
      continue;
    }
    steps = null;
    out.push({ key: `e-${e.n}`, e });
  }
  for (const b of out) {
    const e = b.e;
    b.html = b.items ? stepsHtml(b.items, b.key)
      : e.type === "user" ? `${e.about ? `<p class="you-about">${icon("sparkle")}About: ${esc(e.about)}</p>` : ""}<div class="you">${esc(e.text)}</div>`
      : e.type === "text" ? `<div class="say${e.done ? "" : " typing"}">${md(e.text)}</div>`
      : e.type === "flag" ? `<div class="note warn">${icon("alert")}<span>${esc(e.text)}</span></div>`
      : e.type === "note" ? `<div class="note ${esc(e.level)}">${icon(NOTE_ICON[e.level] || "info")}<span>${esc(e.text)}</span></div>`
      : e.type === "ask" ? askHtml(e) : "";
  }
  const lastText = out.length && out[out.length - 1].e?.type === "text" && !out[out.length - 1].e.done;
  if (chat.status === "thinking" && !lastText) out.push({ key: "thinking", html: `<div class="thinking"><span class="live-dot busy"></span>Thinking…</div>` });
  return out;
}

/** Keyed blocks, so only what changed is redrawn: open boxes stay open and an edit in progress survives. */
function renderChat() {
  if (!chat) return;
  const typing = document.activeElement?.closest?.(".run-reply")?.dataset.reply;
  const atBottom = chatEl._stick || chatEl.scrollHeight - chatEl.scrollTop - chatEl.clientHeight < 80;
  const blocks = chatEl._blocks;
  const keep = new Set();
  const place = placer(chatEl);
  for (const b of chatBlocks()) {
    let el = blocks.get(b.key);
    if (!el) {
      el = document.createElement("div");
      el.className = "block";
      el.dataset.key = b.key;
      blocks.set(b.key, el);
    }
    put(el, b.html);
    if (!el._shown) { el._shown = true; el.firstElementChild?.classList.add("appear"); }
    keep.add(b.key);
    place(el);
  }
  for (const [k, el] of blocks) if (!keep.has(k)) { el.remove(); blocks.delete(k); }
  // A redrawn card gets a new reply box: put back what was typed, and the cursor.
  const box = replyDraft.call && chatEl.querySelector(`[data-reply="${CSS.escape(replyDraft.call)}"] input`);
  if (box && box.value !== replyDraft.text) box.value = replyDraft.text;
  if (box && typing === replyDraft.call && document.activeElement !== box) box.focus({ preventScroll: true });
  chatEl.querySelectorAll(".ask-card:not(.settled) .run-log").forEach((pre) => { pre.scrollTop = pre.scrollHeight; });
  if (atBottom) chatEl.scrollTop = chatEl.scrollHeight;
  chatEl._stick = false;
  setCost();
}

function renderContext() {
  put($("#agent-ctx"), askContext && !chat
    ? `<span class="chip-ctx">${icon("sparkle")}<span>About: ${esc(askContext.title)}</span><button type="button" id="ctx-remove" title="Remove">${icon("x")}</button></span>` : "");
}

function setCost() {
  const el = $("#agent-cost");
  const k = (n) => (n >= 1000 ? `${Math.round(n / 1000)}k` : `${n || 0}`);
  if (agentInfo && !agentInfo.metered) el.textContent = chat ? `This chat: ${k(chat.tokens)} tokens on your plan` : "On your Claude plan";
  else el.textContent = chat ? `This chat ≈ $${(chat.cost || 0).toFixed(2)}`
    : agentInfo ? `Today $${(agentInfo.spent_today || 0).toFixed(2)} of $${agentInfo.budget}` : "";
}

const chatBusy = () => chat && ["thinking", "running"].includes(chat.status);

function syncComposer() {
  const blocked = Boolean(agentInfo?.problem) && !chat;
  askInput.disabled = blocked;
  const stop = chatBusy() && !askInput.value.trim();
  const b = $("#ask-send");
  cls(b, stop ? "btn stop" : "btn accent");
  put(b, icon(stop ? "stop" : "send"));
  b.title = stop ? "Stop" : "Send";
  b.disabled = blocked;
  askInput.placeholder = chatBusy() ? "Working… you can still type" : chat ? "Reply…" : "Ask about the Pi, a service, an alert…";
  setCost();
}

function autosize() {
  askInput.style.height = "auto";
  askInput.style.height = `${Math.min(askInput.scrollHeight, 140)}px`;
}

async function submitAsk() {
  const text = askInput.value.trim();
  if (!text) {
    if (chatBusy()) await api(`/api/agent/chats/${chat.id}/stop`, { method: "POST" }).catch((e) => toast(e.message, true));
    return;
  }
  askInput.value = "";
  autosize();
  syncComposer();
  try {
    if (chat) {
      await api(`/api/agent/chats/${chat.id}/messages`, { method: "POST", body: JSON.stringify({ text }) });
    } else {
      const c = await api("/api/agent/chats", { method: "POST", body: JSON.stringify({ text, context: askContext }) });
      showChat(c.id);
      loadAgent();
    }
  } catch (e) {
    toast(e.message, true);
    askInput.value = text;
    syncComposer();
  }
}

async function decideAsk(call, run) {
  const ask = chat.events.find((e) => e?.call === call && e.type === "ask");
  const command = editing === call && run && editText.trim() !== ask.command ? editText.trim() : undefined;
  chatEl.querySelectorAll(`[data-call="${CSS.escape(call)}"] button`).forEach((b) => { b.disabled = true; });
  try {
    await api(`/api/agent/chats/${chat.id}/decide`, { method: "POST", body: JSON.stringify({ call, run, command }) });
    editing = null;
  } catch (e) {
    toast(e.message, true);
    renderChat();
  }
}

$("#composer").addEventListener("submit", (e) => { e.preventDefault(); submitAsk(); });
askInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); submitAsk(); }
});
askInput.addEventListener("input", () => { autosize(); syncComposer(); });
$("#ask-btn").onclick = () => {
  const b = $("#ask-btn");
  if (b.classList.contains("off")) return toast(askOff, true);
  openAgent();  // also while still checking: the panel says what it finds
};
$("#agent-close").onclick = closeAgent;
$("#agent-new").onclick = async () => { await loadAgent(); showHome(); };
$("#agent-ctx").addEventListener("click", (e) => {
  if (e.target.closest("#ctx-remove")) { askContext = null; renderContext(); askInput.focus(); }
});

chatEl.addEventListener("click", async (e) => {
  const del = e.target.closest("[data-del]");
  if (del) {
    const row = del.closest(".recent-row");
    row.hidden = true;
    try {
      await api(`/api/agent/chats/${del.dataset.del}/delete`, { method: "POST" });
      row.remove();
      agentInfo.chats = agentInfo.chats.filter((c) => c.id !== del.dataset.del);
      if (!agentInfo.chats.length) showHome(askContext);
    } catch (err) { row.hidden = false; toast(err.message, true); }
    return;
  }
  const open = e.target.closest("[data-chat]");
  if (open) return showChat(open.dataset.chat);
  const b = e.target.closest("[data-ask]");
  if (!b || !chat) return;
  const call = b.closest("[data-call]").dataset.call;
  const ev = chat.events.find((x) => x?.call === call && x.type === "ask");
  const act = b.dataset.ask;
  if (act === "run") decideAsk(call, true);
  else if (act === "skip") decideAsk(call, false);
  else if (act === "stop") api(`/api/agent/chats/${chat.id}/stop`, { method: "POST" }).catch((err) => toast(err.message, true));
  else if (act === "edit") {
    editing = call; editText = ev.command;
    renderChat();
    const ta = $("#edit-cmd");
    ta.focus();
    ta.setSelectionRange(ta.value.length, ta.value.length);
  } else if (act === "cancel-edit") { editing = null; renderChat(); }
});
chatEl.addEventListener("input", (e) => {
  if (e.target.id === "edit-cmd") editText = e.target.value;
  const r = e.target.closest(".run-reply");
  if (r) replyDraft = { call: r.dataset.reply, text: e.target.value };
});
chatEl.addEventListener("focusin", (e) => {
  const r = e.target.closest(".run-reply");
  if (r && replyDraft.call !== r.dataset.reply) replyDraft = { call: r.dataset.reply, text: e.target.value };
});
chatEl.addEventListener("submit", async (e) => {
  const form = e.target.closest(".run-reply");
  if (!form || !chat) return;
  e.preventDefault();
  const input = form.querySelector("input");
  const text = input.value;
  input.value = "";
  replyDraft = { call: form.dataset.reply, text: "" };
  try {
    await api(`/api/agent/chats/${chat.id}/reply`, { method: "POST", body: JSON.stringify({ call: form.dataset.reply, text }) });
  } catch (err) { toast(err.message, true); }
});
chatEl.addEventListener("toggle", (e) => {
  const d = e.target;
  for (const [attr, set] of [["steps", openSteps], ["output", openOutput]]) {
    const key = d.dataset?.[attr];
    if (key) d.open ? set.add(key) : set.delete(key);
  }
}, true);

// The running time on a command's card.
setInterval(() => {
  if (agentOpen() && chat?.events.some((e) => e?.type === "ask" && e.state === "running")) renderChat();
}, 1000);

// "Ask why this happened" under a problem in Activity.
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-ask-why]");
  if (!b) return;
  const ev = b.closest("li")._event;
  const context = { title: ev.title, detail: ev.detail, service: ev.service, log: ev.log, when: new Date(ev.t * 1000).toLocaleString() };
  openAgent({ context, ask: "Why did this happen?" });
});

document.addEventListener("keydown", (e) => {
  if (e.key !== "/" || e.metaKey || e.ctrlKey || e.altKey || agentOpen() || askOff) return;
  if (e.target.closest("input, textarea, select, [contenteditable]") || $("dialog[open]")) return;
  e.preventDefault();
  openAgent();
});

// ---- start ---------------------------------------------------------------------------

buildStats();
syncFilter();
$("#services").innerHTML = `<div class="svc skel-card" style="height:150px"></div>`.repeat(2);
document.querySelectorAll(".skel-card").forEach((c) => c.classList.add("skel"));
route();
refresh();
loadAgent();
