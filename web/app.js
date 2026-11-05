/* iLO4 Commander — front end.
   No build step, no CDN: this has to work on a rack LAN with no internet. */

const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const el = (tag, cls, html) => { const n = document.createElement(tag);
  if (cls) n.className = cls; if (html != null) n.innerHTML = html; return n; };
const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
  ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[c]));
const store = {                       // localStorage can throw (private mode, previews)
  get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : v; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch {} },
};
const IN_APP = /iLOCommanderApp/.test(navigator.userAgent);

const state = {
  servers: [], snaps: {}, order: [], current: null, meta: null,
  recent: [], recentFor: null,
  mode: "max", sensorSort: "group", profId: null,
  curveCfg: null, curveFor: null, curveSaving: false,
  tab: store.get("ilo.tab", "dash"),
};
const S = () => state.snaps[state.current] || {};
const fansLive = () => S().thermal?.fans || [];
const temps = () => (S().thermal?.temperatures || []).filter(t => typeof t.reading === "number" && t.reading > 0);
// While the curve drives the fans, what it last sent is the ceiling that's in force.
const ceilings = () => {
  const c = S().curve, manual = S().fan_state?.ceilings || {};
  if (c?.config?.enabled && !c.tripped_at && Object.keys(c.fan_applied || {}).length)
    return { ...manual, ...c.fan_applied };
  return manual;
};
const hasFanSpeeds = () => fansLive().some(f => typeof f.reading === "number");
const fanTotal = () => Math.max(fansLive().length, S().fan_count || 0);
const curveStatus = () => S().curve || null;

/* ---------------- api ---------------- */
async function api(path, opts = {}) {
  const r = await fetch("/api" + path, {
    method: opts.method || (opts.body ? "POST" : "GET"),
    headers: opts.body ? { "Content-Type":"application/json" } : undefined,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
    credentials: "same-origin",
  });
  const data = await r.json().catch(() => ({}));
  if (r.status === 401 && path !== "/login") showGate();
  if (!r.ok) {
    const d = data.detail;
    throw new Error(Array.isArray(d) ? d.map(x => x.msg).join("; ") : (d || r.statusText));
  }
  return data;
}
const sapi = (path, opts) => {
  if (!state.current) return Promise.reject(new Error("Add a server first."));
  return api(`/servers/${encodeURIComponent(state.current)}${path}`, opts);
};
let toastTimer;
function toast(msg, kind = "") {
  const n = $("#toast"); n.textContent = msg; n.className = "toast " + kind; n.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => n.hidden = true, 4500);
}
const guard = fn => (...a) => Promise.resolve().then(() => fn(...a)).catch(e => toast(e.message, "bad"));
function timeAgo(ts) {
  if (!ts) return "never";
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}
const fmtClock = ts => new Date(ts * 1000).toLocaleString(undefined,
  { month:"short", day:"numeric", hour:"2-digit", minute:"2-digit" });

/* ---------------- tooltips ---------------- */
function showTip(anchor) {
  const text = anchor.getAttribute("data-tip"); if (!text) return;
  const tip = $("#tip");
  tip.textContent = text; tip.hidden = false;
  tip.style.left = "0px"; tip.style.top = "0px";
  const a = anchor.getBoundingClientRect(), t = tip.getBoundingClientRect();
  let left = a.left + a.width / 2 - t.width / 2;
  left = Math.max(8, Math.min(window.innerWidth - t.width - 8, left));
  let top = a.top - t.height - 9;
  if (top < 8) top = a.bottom + 9;
  tip.style.left = `${Math.round(left)}px`; tip.style.top = `${Math.round(top)}px`;
}
const hideTip = () => { $("#tip").hidden = true; };
document.addEventListener("mouseover", e => { const a = e.target.closest?.("[data-tip]"); if (a) showTip(a); });
document.addEventListener("mouseout", e => { if (e.target.closest?.("[data-tip]")) hideTip(); });
document.addEventListener("focusin", e => { const a = e.target.closest?.(".info[data-tip]"); if (a) showTip(a); });
document.addEventListener("focusout", hideTip);
document.addEventListener("click", e => {             // touch: tap the (i) to read it
  const a = e.target.closest?.(".info[data-tip]");
  if (a) { showTip(a); setTimeout(hideTip, 4000); }
});
window.addEventListener("scroll", hideTip, true);

/* ---------------- tabs ---------------- */
function showTab(tab) {
  if (!document.querySelector(`[data-panel="${tab}"]`)) tab = "dash";
  state.tab = tab; store.set("ilo.tab", tab);
  $$("#nav button").forEach(x => x.classList.toggle("on", x.dataset.tab === tab));
  $$("section[data-panel]").forEach(p => p.classList.toggle("on", p.dataset.panel === tab));
  hideTip();
  if (state.current) LOADERS[tab]?.();
  window.scrollTo({ top:0 });
}
$("#nav").onclick = e => { const b = e.target.closest("button[data-tab]"); if (b) showTab(b.dataset.tab); };
const isTab = t => state.tab === t;

/* ---------------- login ---------------- */
function showGate() {
  $("#login").hidden = false; $("#app").hidden = true;
  setTimeout(() => $("#loginPass").focus(), 0);
}
$("#loginForm").onsubmit = async e => {
  e.preventDefault();
  const btn = $("#loginForm button"); btn.disabled = true; $("#loginErr").textContent = "";
  try {
    await api("/login", { body:{ password: $("#loginPass").value } });
    $("#loginPass").value = "";
    $("#login").hidden = true; $("#app").hidden = false;
    boot();
  } catch (err) { $("#loginErr").textContent = err.message; $("#loginPass").select(); }
  finally { btn.disabled = false; }
};

/* ---------------- sensor taxonomy ----------------
   HP names sensors like "02-CPU 1" and "04-P1 DIMM 1-6". Match order matters:
   "18-PS 1 Inlet" is a power supply, not the chassis intake. */
const GROUPS = [
  ["Processors", /cpu|\bproc/i],
  ["Memory",     /dimm|memory/i],
  ["Storage",    /\bhd\b|hdd|drive|storage|sas|sata|backplane|expander|cage|smart ?array|\bhd max/i],
  ["Power",      /\bps\b|psu|power supply|\bvr\b|vreg|volt|batt/i],
  ["I/O",        /pci|nic|lom|chipset|i\/o|expansion|slot|mezz|sup\b|flexlom/i],
  ["Exhaust",    /exhaust|outlet/i],
  ["Intake",     /inlet|ambient|intake/i],
  ["System",     /./],
];
const GROUP_ORDER = ["Intake","Processors","Memory","Storage","Power","I/O","Exhaust","System"];
const groupOf = s => (GROUPS.find(([, re]) => re.test(`${s.name} ${s.context || ""}`)) || ["System"])[0];
const bucketed = rows => {
  const b = new Map(GROUP_ORDER.map(n => [n, []]));
  rows.forEach(s => b.get(groupOf(s)).push(s));
  return [...b].filter(([, list]) => list.length);
};
const levelOf = s =>
  s.critical && s.reading >= s.critical ? "critical"
  : s.caution && s.reading >= s.caution ? "caution" : "";

/* ---------------- server switcher, rail, fleet ---------------- */
function linkLamp(snap) {
  const l = snap?.link?.redfish;
  if (!snap?.id || l === "disabled") return "off";
  if (l === "down") return "critical";
  if (l === "degraded" || l === "connecting") return "caution";
  const h = (snap.system?.health || "").toLowerCase();
  return h === "ok" || !h ? "ok" : h === "warning" ? "caution" : "critical";
}
const hottestOf = snap => (snap?.thermal?.temperatures || [])
  .filter(t => typeof t.reading === "number").sort((a, b) => b.reading - a.reading)[0];
function fanModeOf(snap) {
  const c = snap?.curve;
  if (!snap || !c) return "—";
  if (c.tripped_at) return "watchdog";
  if (c.config?.enabled) return "curve";
  return Object.values(snap.fan_state?.ceilings || {}).some(v => v < 100) ? "manual" : "iLO auto";
}
function serverName(id) { return state.servers.find(s => s.id === id)?.name || id; }

function renderRail() {
  const s = S(), sys = s.system || {}, hot = hottestOf(s);
  $("#railName").textContent = state.current ? serverName(state.current) : "No servers";
  $("#railModel").textContent = [sys.model, s.host].filter(Boolean).join("  ·  ") || "—";
  $("#railPower").textContent = sys.power_state || "—";
  $("#railTemp").textContent = hot ? `${hot.reading}° ${hot.name.replace(/^\d+-/, "").split(" ")[0]}` : "—";
  $("#railWatts").textContent = s.power?.consumed_watts != null ? s.power.consumed_watts + " W" : "—";
  $("#railMode").textContent = fanModeOf(s);
  $("#lampHealth").className = "lamp " + linkLamp(s);
  const l = s.link || {};
  const rf = $(".links i[data-link=redfish]"), sh = $(".links i[data-link=ssh]");
  rf.className = l.redfish === "ok" ? "up" : l.redfish === "down" ? "down" : l.redfish ? "warn" : "";
  rf.dataset.tip = `Redfish: ${l.redfish || "—"}${l.error ? " — " + l.error : ""}. Last good reading ${timeAgo(l.last_ok)}.`;
  sh.className = l.patched === false ? "warn" : l.ssh ? "up" : l.ssh_error ? "down" : "";
  sh.dataset.tip = l.patched === false ? "SSH works, but this iLO isn't running patched firmware."
    : l.ssh ? "SSH connected — fan commands available."
    : `SSH idle${l.ssh_error ? " — last error: " + l.ssh_error : " (it connects when a command is sent)."}`;
  $("#floorNote").textContent = s.guards ? `minimum ${s.guards.min_pwm_percent}%` : "";
  document.title = state.current
    ? `${hot ? hot.reading + "° · " : ""}${serverName(state.current)} — iLO4 Commander` : "iLO4 Commander";
}

function renderSwitcherMenu() {
  const m = $("#srvMenu"); m.innerHTML = "";
  state.servers.forEach(cfg => {
    const snap = state.snaps[cfg.id], hot = hottestOf(snap);
    const b = el("button", "srv-item" + (cfg.id === state.current ? " on" : ""));
    b.setAttribute("role", "option");
    b.innerHTML = `<span class="lamp ${linkLamp(snap)}"></span>
      <span style="min-width:0"><div class="nm">${esc(cfg.name)}</div><div class="sub">${esc(cfg.host)}${cfg.enabled ? "" : " · paused"}</div></span>
      <span class="rt">${hot ? hot.reading + "°" : ""}${snap?.power?.consumed_watts != null ? " · " + snap.power.consumed_watts + "W" : ""}</span>`;
    b.onclick = () => { closeMenu(); selectServer(cfg.id); };
    m.appendChild(b);
  });
  const manage = el("button", "srv-item manage", `<span></span><span>Add or manage servers…</span><span></span>`);
  manage.onclick = () => { closeMenu(); openSheet(); };
  m.appendChild(manage);
}
const closeMenu = () => { $("#srvMenu").hidden = true; $("#srvBtn").setAttribute("aria-expanded", "false"); };
$("#srvBtn").onclick = e => {
  e.stopPropagation();
  const open = $("#srvMenu").hidden;
  if (open) { renderSwitcherMenu(); $("#srvMenu").hidden = false; $("#srvBtn").setAttribute("aria-expanded", "true"); }
  else closeMenu();
};
document.addEventListener("click", e => { if (!e.target.closest(".switcher")) closeMenu(); });

function renderFleet() {
  const box = $("#fleet");
  box.hidden = state.servers.length < 2;
  if (box.hidden) return;
  box.innerHTML = "";
  state.servers.forEach(cfg => {
    const snap = state.snaps[cfg.id], hot = hottestOf(snap);
    const c = el("button", "fleet-card" + (cfg.id === state.current ? " on" : ""));
    const applied = Object.values(snap?.curve?.fan_applied || {});
    const fanPct = applied.length && !snap?.curve?.tripped_at ? Math.max(...applied) + "%" : ({ "watchdog":"iLO", "iLO auto":"iLO" }[fanModeOf(snap)] || fanModeOf(snap));
    const l = snap?.link?.redfish, ps = snap?.system?.power_state;
    const badge = !cfg.enabled ? `<span class="badge">paused</span>`
      : l === "down" ? `<span class="badge bad">offline</span>`
      : snap?.curve?.tripped_at ? `<span class="badge warn">watchdog</span>`
      : ps && ps !== "On" ? `<span class="badge warn">${esc(ps.toLowerCase())}</span>`
      : l === "degraded" ? `<span class="badge warn">slow</span>` : "";
    c.innerHTML = `<div class="fc-top"><span class="lamp ${linkLamp(snap)}"></span><span class="fc-name">${esc(cfg.name)}</span>${badge}</div>
      <div class="fc-model">${esc((snap?.system?.model || cfg.host).replace("ProLiant ", ""))}</div>
      <div class="fc-big">
        <div><span>hottest</span><b class="${hot ? levelOf(hot) : ""}">${hot ? hot.reading + "°" : "—"}</b></div>
        <div><span>fans</span><b>${esc(fanPct)}</b></div>
        <div><span>power</span><b>${snap?.power?.consumed_watts != null ? snap.power.consumed_watts + "W" : "—"}</b></div>
      </div>`;
    c.title = hot ? `Hottest: ${hot.name}` : "";
    c.onclick = () => selectServer(cfg.id);
    box.appendChild(c);
  });
}

function renderLinkAlert() {
  const box = $("#linkAlert"), s = S(), l = s.link || {};
  let kind = "", title = "", note = "", action = "";
  if (!state.current) { box.hidden = true; return; }
  if (s.enabled === false) {
    kind = "warn"; title = "Monitoring is paused for this server";
    note = "Nothing is read or written. Enable it again under Servers & settings.";
  } else if (l.redfish === "down") {
    kind = "bad"; title = "Can't reach this iLO";
    note = `${l.error || "No answer."} Showing the last reading from ${timeAgo(l.last_ok)}. Retrying automatically.`;
  } else if (l.redfish === "connecting" && l.fails >= 2) {
    // A worker that has only just started isn't worth a banner; one that can't get in is.
    kind = "warn"; title = "Can't connect to the iLO yet"; note = l.error || "";
  } else if (l.patched === false) {
    kind = "warn"; title = "This iLO isn't running patched firmware";
    note = "Sensors and history work, but fan commands won't. See the Tuning guide.";
  } else if (s.system?.power_state && s.system.power_state !== "On") {
    kind = "warn"; title = `Host is ${s.system.power_state.toLowerCase()}`;
    note = "Fan settings will be sent again automatically when it powers on.";
  }
  box.hidden = !kind;
  if (!kind) return;
  box.className = "alert " + (kind === "bad" ? "bad" : "");
  box.innerHTML = `<span class="lamp ${kind === "bad" ? "critical" : "caution"}" style="margin-top:.35rem"></span>
    <div><b>${esc(title)}</b><p>${esc(note)}</p></div>${action}`;
}

/* ---------------- control banner ---------------- */
function renderBanner() {
  const b = $("#controlBanner"), c = curveStatus(), s = S();
  let kind, title, note;
  const profs = c?.config?.profiles || [];
  const live = c?.profile_state || {};
  const managed = Object.entries(ceilings()).filter(([, v]) => v < 100);
  if (!c) { kind = ""; title = "No data yet"; note = ""; }
  else if (c.tripped_at) {
    kind = "tripped"; title = "Watchdog handed the fans back to iLO";
    note = `${c.trip_reason || ""} — ${timeAgo(c.tripped_at)}. ` +
      (c.config?.watchdog_auto_resume ? "The curve resumes by itself once everything has cooled down."
                                      : "Press Re-send to take control again.");
  } else if (c.config?.enabled) {
    kind = "curve";
    const running = profs.filter(p => live[p.id]);
    if (profs.length <= 1) {
      const st = live[profs[0]?.id];
      title = st?.mode === "target" ? "Adaptive control is running" : "Automatic curve is running";
      note = st?.mode === "target" ? `${st.sensor || "sensor"} at ${Math.round(st.temp)}° (target ${Math.round(st.goal)}°) → ${st.applied}%.`
           : st ? `${st.sensor || "sensor"} at ${Math.round(st.temp)}° → ceiling ${st.applied}%.`
                : s.system?.power_state && s.system.power_state !== "On" ? "Waiting for the host to power on."
                : "Waiting for the first cycle.";
    } else if (running.length) {
      title = `${running.length} of ${profs.length} curves running`;
      note = running.map(p => `${p.name}: ${Math.round(live[p.id].temp)}° → ${live[p.id].applied}%`).join("  ·  ");
    } else { title = `${profs.length} curves enabled`; note = "Waiting for the first cycle."; }
    if (c.last_error) note += `  ⚠ ${c.last_error}`;
  } else if (managed.length) {
    const lo = Math.min(...managed.map(([, v]) => v));
    kind = "manual"; title = `${managed.length} fan${managed.length > 1 ? "s" : ""} capped (lowest ${lo}%)`;
    note = "Manual limits. They're re-sent automatically after a reboot.";
  } else {
    kind = "auto"; title = "iLO is in charge of the fans";
    note = "No limits are active. The firmware is running its factory curve.";
  }
  b.className = "banner " + kind;
  $("#bannerTitle").textContent = title;
  $("#bannerNote").textContent = note;
  $("#reapplyBtn").lastChild.textContent = c?.tripped_at ? " Resume now" : " Re-send";
}
$("#reapplyBtn").onclick = guard(async () => {
  await sapi("/fan/reapply", { method:"POST" });
  toast("Fan settings will be sent on the next poll", "good");
});
$("#restoreBtn").onclick = guard(async () => {
  if (!confirm("Clear every manual fan limit, switch the curve off and hand control back to iLO?")) return;
  await sapi("/fan/restore", { method:"POST" });
  if (state.curveCfg) state.curveCfg.enabled = false;
  $("#curveOn").checked = false;
  toast("Fans returned to iLO control", "good");
  refreshSnapshot();
});

/* ---------------- fan cage ---------------- */
function renderCage() {
  const cage = $("#cage"), live = fansLive(), n = fanTotal();
  if (cage.children.length !== n) {
    cage.innerHTML = "";
    for (let i = 0; i < n; i++) {
      const slot = el("div", "slot");
      slot.innerHTML = `<div class="slot-val" data-val>—</div>
        <div class="track" data-i="${i}" role="slider" tabindex="0"
             aria-label="Fan ${i + 1} ceiling" aria-valuemin="0" aria-valuemax="100">
          <div class="ceil" data-ceil></div><div class="needle" data-needle data-rpm=""></div>
        </div><div class="slot-no">FAN ${i + 1}</div>`;
      cage.appendChild(slot);
      bindTrack(slot.querySelector(".track"));
    }
  }
  const ceil = ceilings();
  const speeds = hasFanSpeeds() || !S().thermal;
  $("#legendNeedle").hidden = !speeds;
  $("#legendNoSpeed").hidden = speeds;
  [...cage.children].forEach((slot, i) => {
    const f = live[i], c = ceil[i];
    const track = slot.querySelector(".track");
    if (track.dataset.dragging) return;
    const absent = f?.state === "Absent";
    const managed = c != null && c < 100;
    track.classList.toggle("auto", !managed);
    track.classList.toggle("absent", absent);
    track.setAttribute("aria-valuenow", c ?? 100);
    slot.querySelector("[data-ceil]").style.height = (c ?? 100) + "%";
    const rpm = typeof f?.reading === "number" ? f.reading : null;
    const needle = slot.querySelector("[data-needle]");
    needle.hidden = rpm == null;
    needle.style.bottom = (rpm ?? 0) + "%";
    needle.dataset.rpm = rpm != null ? rpm + "%" : "";
    slot.querySelector("[data-val]").textContent = absent ? "absent" : managed ? c + "%" : "auto";
  });
}
function bindTrack(track) {
  const pct = ev => {
    const r = track.getBoundingClientRect();
    return Math.round(Math.max(0, Math.min(100, (1 - (ev.clientY - r.top) / r.height) * 100)));
  };
  const paint = v => { track.querySelector("[data-ceil]").style.height = v + "%";
    track.closest(".slot").querySelector("[data-val]").textContent = v + "%"; };
  const move = ev => { if (track.dataset.dragging) { ev.preventDefault(); paint(pct(ev)); } };
  const up = ev => {
    if (!track.dataset.dragging) return;
    delete track.dataset.dragging;
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
    setFan(+track.dataset.i, pct(ev));
  };
  track.addEventListener("pointerdown", ev => {
    if (track.classList.contains("absent")) return;
    track.dataset.dragging = "1"; ev.preventDefault();
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up); paint(pct(ev));
  });
  track.addEventListener("keydown", ev => {
    const cur = ceilings()[+track.dataset.i] ?? 100;
    if (ev.key === "ArrowUp") setFan(+track.dataset.i, Math.min(100, cur + 5));
    else if (ev.key === "ArrowDown") setFan(+track.dataset.i, Math.max(0, cur - 5));
    else return;
    ev.preventDefault();
  });
}
const setFan = guard(async (fan, percent) => {
  const res = await sapi("/fan/pwm", { body:{ fan:String(fan), percent, mode: state.mode } });
  const fs = S().fan_state; if (fs) fs.ceilings[fan] = res.percent;
  const note = curveStatus()?.config?.enabled ? " — the curve will overwrite this on its next poll" : "";
  toast(`Fan ${fan + 1} → ${res.percent}% (${res.mode})${note}`, "good");
  renderFans(); renderBanner();
});

/* ---------------- overview readouts ---------------- */
function renderTemps() {
  const box = $("#tempList"); box.innerHTML = "";
  const rows = temps().sort((a, b) => b.reading - a.reading).slice(0, 10);
  if (!rows.length) { box.appendChild(el("p", "empty", S().thermal ? "No temperature sensors reported." : "Waiting for the first reading…")); return; }
  rows.forEach(x => {
    const cap = x.critical || x.caution || 100;
    const line = el("div", "tline");
    line.innerHTML = `<span class="tname">${esc(x.name)}</span>
      <span class="tval">${x.reading}°</span>
      <span class="tbar"><i class="${levelOf(x)}" style="width:${Math.min(100, x.reading / cap * 100)}%"></i></span>`;
    line.title = `Caution ${x.caution ?? "n/a"}°, critical ${x.critical ?? "n/a"}°`;
    line.onclick = () => window.HistoryView?.open("temp", x.name);
    box.appendChild(line);
  });
}

const SVGNS = "http://www.w3.org/2000/svg";
const mk = (parent, tag, attrs, text) => {
  const n = document.createElementNS(SVGNS, tag);
  Object.entries(attrs || {}).forEach(([k, v]) => n.setAttribute(k, v));
  if (text != null) n.textContent = text;
  parent.appendChild(n); return n;
};

const TR = { l:36, r:40, t:14, b:24 };
function renderTrend() {
  const svg = $("#trend"), samples = state.recent.filter(s => s.ts > Date.now() / 1000 - 3600);
  const W = svg.clientWidth || 600, H = svg.clientHeight || 190;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.innerHTML = "";
  if (samples.length < 2) {
    mk(svg, "text", { x:W / 2, y:H / 2, fill:"#626D7D", "text-anchor":"middle", "font-size":"12" }, "Collecting…");
    $("#trendLegend").innerHTML = ""; return;
  }
  const hot = samples.map(s => Math.max(0, ...Object.values(s.temps || {}).filter(Number.isFinite)));
  const fan = samples.map(s => { const v = Object.values(s.fans || {}).filter(Number.isFinite);
    return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null; });
  const fanKnown = fan.some(v => v != null);
  const tMax = Math.max(40, Math.ceil(Math.max(...hot) / 10) * 10);
  const tMin = Math.min(20, Math.floor(Math.min(...hot) / 10) * 10);
  const t0 = samples[0].ts, t1 = samples.at(-1).ts;
  const pw = W - TR.l - TR.r, ph = H - TR.t - TR.b;
  const X = ts => TR.l + (ts - t0) / ((t1 - t0) || 1) * pw;
  const YT = v => H - TR.b - (v - tMin) / (tMax - tMin) * ph;
  const YF = v => H - TR.b - v / 100 * ph;
  for (let g = 0; g <= 4; g++) {
    const y = TR.t + g / 4 * ph;
    mk(svg, "line", { x1:TR.l, y1:y, x2:W - TR.r, y2:y, stroke:"#1C232E" });
    mk(svg, "text", { x:TR.l - 7, y:y + 3, fill:"#F2A93B", "font-size":"9.5", "text-anchor":"end",
      "font-family":"monospace", opacity:".8" }, Math.round(tMax - g / 4 * (tMax - tMin)) + "°");
    mk(svg, "text", { x:W - TR.r + 7, y:y + 3, fill:"#4C8DFF", "font-size":"9.5",
      "font-family":"monospace", opacity:".8" }, Math.round(100 - g / 4 * 100) + "%");
  }
  mk(svg, "text", { x:TR.l, y:H - 6, fill:"#626D7D", "font-size":"9.5", "font-family":"monospace" },
    `−${Math.max(1, Math.round((t1 - t0) / 60))}m`);
  mk(svg, "text", { x:W - TR.r, y:H - 6, fill:"#626D7D", "font-size":"9.5", "text-anchor":"end",
    "font-family":"monospace" }, "now");
  const path = (arr, scale) => arr.map((v, i) => `${i ? "L" : "M"}${X(samples[i].ts).toFixed(1)},${scale(v).toFixed(1)}`).join("");
  if (fanKnown) mk(svg, "path", { d:path(fan.map(v => v ?? 0), YF), fill:"none", stroke:"#4C8DFF", "stroke-width":"1.7", "stroke-dasharray":"4 3" });
  mk(svg, "path", { d:path(hot, YT), fill:"none", stroke:"#F2A93B", "stroke-width":"1.7", "stroke-linejoin":"round" });
  mk(svg, "circle", { cx:X(t1), cy:YT(hot.at(-1)), r:3, fill:"#F2A93B" });
  $("#trendLegend").innerHTML =
    `<span><i style="background:#F2A93B"></i>hottest sensor <b>${Math.round(hot.at(-1))}°C</b></span>` +
    (fanKnown ? `<span><i style="background:#4C8DFF"></i>mean fan speed <b>${Math.round(fan.at(-1) ?? 0)}%</b></span>`
              : `<span>fan speed isn't reported by this iLO</span>`);
}
async function loadRecent() {
  const sid = state.current;
  const { samples } = await sapi("/recent?limit=720");
  if (sid !== state.current) return;
  state.recent = samples; state.recentFor = sid;
  renderTrend(); drawSparks();
}
function pushRecent(snap) {                    // keep the trend live between fetches
  if (snap.id !== state.current || state.recentFor !== snap.id || !snap.thermal) return;
  const ts = snap.link?.last_ok; if (!ts || state.recent.at(-1)?.ts >= ts) return;
  state.recent.push({ ts,
    temps: Object.fromEntries(snap.thermal.temperatures.map(t => [t.name, t.reading])),
    fans: Object.fromEntries(snap.thermal.fans.map(f => [f.name, f.reading])),
    watts: snap.power?.consumed_watts });
  if (state.recent.length > 720) state.recent.shift();
}

function renderActivity() {
  const box = $("#activity"), evs = S().events || [];
  box.innerHTML = "";
  if (!evs.length) { box.appendChild(el("p", "empty", "Nothing yet. Power changes, re-sent settings and watchdog trips show up here.")); return; }
  evs.forEach(e => box.appendChild(activityRow(e)));
}
function activityRow(e) {
  const r = el("div", "act ev-" + (e.kind || "info"));
  r.innerHTML = `<time>${esc(fmtClock(e.ts))}</time><span>${esc(e.message)}</span>`;
  return r;
}

/* ---------------- sensors tab ---------------- */
function sensorCard(s) {
  const level = levelOf(s);
  const cap = Math.max(s.critical || 0, s.caution || 0, s.reading) * 1.1 || 100;
  const card = el("div", "scard " + level);
  const marks = [
    s.caution ? `<u style="left:${Math.min(99, s.caution / cap * 100)}%"></u>` : "",
    s.critical ? `<u class="crit" style="left:${Math.min(99, s.critical / cap * 100)}%"></u>` : "",
  ].join("");
  card.innerHTML = `
    <div class="scard-top"><span class="scard-name">${esc(s.name)}</span>
      <span class="scard-val">${s.reading}<small>°C</small></span></div>
    <div class="gauge"><i class="${level}" style="width:${Math.min(100, s.reading / cap * 100)}%"></i>${marks}</div>
    <svg class="spark" data-spark="${esc(s.name)}"></svg>
    <div class="scard-foot"><span>${s.caution ? "caution " + s.caution + "°" : "no threshold"}</span>
      <span>${s.critical ? "critical " + s.critical + "°" : ""}</span></div>`;
  card.onclick = () => window.HistoryView?.open("temp", s.name);
  return card;
}
function drawSparks() {
  const H = 26;
  $$("[data-spark]").forEach(svg => {
    const name = svg.dataset.spark, kind = svg.dataset.kind || "temps";
    const series = state.recent.map(s => s[kind]?.[name]).filter(Number.isFinite);
    svg.innerHTML = "";
    if (series.length < 2) return;
    const W = svg.clientWidth || 180;
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    const lo = Math.min(...series), hi = Math.max(...series), span = (hi - lo) || 1;
    const d = series.map((v, i) =>
      `${i ? "L" : "M"}${(i / (series.length - 1) * W).toFixed(1)},${(H - 3 - (v - lo) / span * (H - 7)).toFixed(1)}`).join("");
    mk(svg, "path", { d: `${d} L${W},${H} L0,${H} Z`, fill:"rgba(76,141,255,.10)", stroke:"none" });
    mk(svg, "path", { d, fill:"none", stroke:"#4C8DFF", "stroke-width":"1.3", opacity:".85" });
  });
}
function renderSensors() {
  const box = $("#sensorGrid"), rows = temps();
  box.innerHTML = "";
  if (!rows.length) box.appendChild(el("p", "empty", "Waiting for the first reading…"));
  else if (state.sensorSort === "hot") {
    const grid = el("div", "sensor-grid");
    rows.sort((a, b) => b.reading - a.reading).forEach(s => grid.appendChild(sensorCard(s)));
    box.appendChild(grid);
  } else {
    for (const [name, list] of bucketed(rows)) {
      const sec = el("section", "sensor-group");
      const hot = Math.max(...list.map(s => s.reading));
      sec.appendChild(el("h3", null, `${name} <span style="color:var(--dim)">${list.length} · hottest ${hot}°</span>`));
      const grid = el("div", "sensor-grid");
      list.sort((a, b) => b.reading - a.reading).forEach(s => grid.appendChild(sensorCard(s)));
      sec.appendChild(grid); box.appendChild(sec);
    }
  }
  const fanBox = $("#sensorFans"); fanBox.innerHTML = "";
  const ceil = ceilings();
  fansLive().forEach((f, i) => {
    const known = typeof f.reading === "number", v = known ? f.reading : 0, c = ceil[i];
    const card = el("div", "scard");
    card.innerHTML = `
      <div class="scard-top"><span class="scard-name">${esc(f.name)}</span>
        <span class="scard-val">${f.state === "Absent" || !known ? "—" : v}<small>${known ? "%" : ""}</small></span></div>
      <div class="gauge"><i style="width:${v}%"></i>${c != null && c < 100 ? `<u style="left:${c}%"></u>` : ""}</div>
      <svg class="spark" data-spark="${esc(f.name)}" data-kind="fans"></svg>
      <div class="scard-foot"><span>${esc(f.state === "Absent" ? "absent" : known ? f.health || "" : "speed not reported")}</span>
        <span>${c != null && c < 100 ? "ceiling " + c + "%" : "iLO controlled"}</span></div>`;
    card.onclick = () => window.HistoryView?.open("fan", f.name);
    fanBox.appendChild(card);
  });
  const p = S().power;
  const kv = $("#powerBox"); kv.innerHTML = "";
  const rowsKV = p ? [["now", p.consumed_watts != null ? p.consumed_watts + " W" : "—"],
    ["average", p.average_watts != null ? p.average_watts + " W" : "—"],
    ["peak", p.max_watts != null ? p.max_watts + " W" : "—"],
    ...p.supplies.map(s => [s.name, `${s.output ?? "?"} W / ${s.capacity ?? "?"} W · ${s.health || "?"}`])] : [];
  if (!rowsKV.length) kv.appendChild(el("p", "empty", "No power data."));
  rowsKV.forEach(([k, v]) => kv.appendChild(el("div", "r", `<span class="k">${esc(k)}</span><span class="v">${esc(v)}</span>`)));
  drawSparks();
}
$("#sensorSort").onclick = e => {
  const b = e.target.closest("button[data-sort]"); if (!b) return;
  $$("#sensorSort button").forEach(x => x.classList.toggle("on", x === b));
  state.sensorSort = b.dataset.sort; renderSensors();
};

/* ---------------- fans tab: manual ---------------- */
function renderFans() {
  const box = $("#fanRows"); box.innerHTML = "";
  const ceil = ceilings(), live = fansLive(), c = curveStatus();
  const profs = state.curveCfg?.profiles || [];
  for (let i = 0; i < fanTotal(); i++) {
    const known = ceil[i] != null && ceil[i] < 100, cur = ceil[i] ?? 100;
    const rpm = typeof live[i]?.reading === "number" ? live[i].reading : null;
    const owner = c?.config?.enabled && profs.length > 1 ? profs.find(p => assignOf(i) === p.id) : null;
    const row = el("div", "fan-row");
    row.innerHTML = `<span class="fr-name">${owner ? `<i class="pdot" style="background:${profColor(owner.id)}"></i>` : ""}FAN ${i + 1}</span>
      <input type="range" min="0" max="100" value="${cur}" aria-label="Fan ${i + 1}">
      <output class="${known ? "" : "muted"}">${known ? cur + "%" : "auto"}</output>
      <span class="fr-rpm">${rpm != null ? rpm + "%" : ""}</span>
      <button class="ghost">Release</button>`;
    const range = row.querySelector("input"), out = row.querySelector("output");
    range.oninput = () => { out.textContent = range.value + "%"; out.classList.remove("muted"); };
    range.onchange = () => setFan(i, +range.value);
    row.querySelector("button").onclick = guard(async () => {
      await sapi("/fan/unlock", { body:{ fan:String(i) } });
      const fs = S().fan_state; if (fs) delete fs.ceilings[i];
      renderFans(); renderCage(); renderBanner();
      toast(`Fan ${i + 1} handed back to iLO`, "good");
    });
    box.appendChild(row);
  }
}
$("#allSlider").oninput = e => $("#allOut").textContent = e.target.value + "%";
$("#allApply").onclick = guard(async () => {
  const res = await sapi("/fan/pwm", { body:{ fan:"all", percent:+$("#allSlider").value, mode:state.mode } });
  const fs = S().fan_state; if (fs) for (let i = 0; i < fanTotal(); i++) fs.ceilings[i] = res.percent;
  renderFans(); renderCage(); renderBanner();
  toast(`All fans → ${res.percent}%`, "good");
});
$("#modeBox").onclick = e => {
  const b = e.target.closest("button[data-mode]"); if (!b) return;
  $$("#modeBox button").forEach(x => x.classList.toggle("on", x === b));
  state.mode = b.dataset.mode;
  if (state.mode === "lock") toast("Locked fans can't ramp up on their own. Keep an eye on the temperatures.", "bad");
};

async function loadPresets() {
  const { presets } = await sapi("/presets");
  const box = $("#presetList"); box.innerHTML = "";
  presets.forEach(p => {
    const c = el("span", "chip", `${esc(p.name)} <b>${p.speeds.all != null ? p.speeds.all + "%" : "custom"}</b> <x title="Delete">×</x>`);
    c.onclick = guard(async ev => {
      if (ev.target.tagName === "X") {
        if (!confirm(`Delete preset "${p.name}"?`)) return;
        await sapi("/presets", { method:"PUT", body:{ presets: presets.filter(x => x.name !== p.name) } });
        return loadPresets();
      }
      await sapi("/presets/apply", { body:{ name:p.name, mode:state.mode } });
      toast(`Applied "${p.name}"`, "good"); refreshSnapshot();
    });
    box.appendChild(c);
  });
  $("#presetSave").onclick = guard(async () => {
    const name = $("#presetName").value.trim(); if (!name) return toast("Give the preset a name first.", "bad");
    const speeds = {}; Object.entries(ceilings()).forEach(([k, v]) => speeds[k] = v);
    if (!Object.keys(speeds).length) return toast("No manual limits are set — nothing to save.", "bad");
    await sapi("/presets", { method:"PUT", body:{ presets:[...presets.filter(x => x.name !== name), { name, speeds }] } });
    $("#presetName").value = ""; loadPresets(); toast("Preset saved", "good");
  });
}

/* ---------------- curve editor ----------------
   Structure is built once and then only mutated. Rebuilding the SVG on every
   pointermove destroyed the node holding pointer capture, which killed the
   drag after a single step. */
let curveDragging = false, clickGuard = false;
const COARSE = matchMedia("(pointer:coarse)").matches;
const CV = { w:800, h:330, l:52, r:20, t:22, b:44, tmin:20, tmax:100 };
const PW = () => CV.w - CV.l - CV.r, PH = () => CV.h - CV.t - CV.b;
const CX = c => CV.l + (c - CV.tmin) / (CV.tmax - CV.tmin) * PW();
const CY = p => CV.h - CV.b - p / 100 * PH();
const XC = x => CV.tmin + (x - CV.l) / PW() * (CV.tmax - CV.tmin);
const YP = y => (CV.h - CV.b - y) / PH() * 100;
const clampT = v => Math.round(Math.max(CV.tmin, Math.min(CV.tmax, v)));
const clampP = v => Math.round(Math.max(0, Math.min(100, v)));
const CE = { line:null, area:null, marker:null, hover:null, nodes:[] };
let selPoint = null;

const PROF_COLORS = ["#4C8DFF", "#A78BFA", "#22D3EE", "#F472B6", "#94A3B8"];
const profiles = () => state.curveCfg?.profiles || [];
const profColor = id => PROF_COLORS[Math.max(0, profiles().findIndex(p => p.id === id)) % PROF_COLORS.length];
const curProf = () => profiles().find(p => p.id === state.profId) || profiles()[0] || null;
const assignOf = i => state.curveCfg?.assign?.[String(i)];
const fansOf = id => Object.entries(state.curveCfg?.assign || {}).filter(([, v]) => v === id).map(([k]) => +k);

const CURVE_PRESETS = {
  silent:     [[35,8],[55,12],[65,22],[75,40],[85,72],[95,100]],
  balanced:   [[35,10],[50,18],[60,30],[70,50],[80,80],[90,100]],
  aggressive: [[30,18],[45,32],[55,48],[65,68],[75,88],[85,100]],
};
const svgPt = ev => {
  const r = $("#curveEditor").getBoundingClientRect();
  return { x:(ev.clientX - r.left) / r.width * CV.w, y:(ev.clientY - r.top) / r.height * CV.h };
};
function curveValueAt(points, temp) {          // mirrors app/curve.py
  const p = points.slice().sort((a, b) => a[0] - b[0]);
  if (!p.length) return 100;
  if (temp <= p[0][0]) return p[0][1];
  if (temp >= p.at(-1)[0]) return p.at(-1)[1];
  for (let i = 0; i < p.length - 1; i++) {
    const [t0, v0] = p[i], [t1, v1] = p[i + 1];
    if (temp >= t0 && temp <= t1) return v0 + (v1 - v0) * (temp - t0) / ((t1 - t0) || 1);
  }
  return p.at(-1)[1];
}
function sensorReadingFor(sel) {
  const all = temps(); if (!all.length) return null;
  let pool = all;
  if (sel === "__cpu__") pool = all.filter(x => /cpu|proc/i.test(x.name));
  else if (sel !== "__hottest__") pool = all.filter(x => x.name === sel);
  if (!pool.length) pool = all;
  return pool.reduce((a, b) => (b.reading > a.reading ? b : a));
}
const selectedSensor = () => sensorReadingFor(curProf()?.sensor || "__cpu__");
// The server's minimum fan speed: nothing below it ever reaches a fan.
const floorPct = () => +(S().guards?.min_pwm_percent ?? state.servers.find(s => s.id === state.current)?.min_pwm_percent ?? 0);
const effective = (points, temp) => Math.max(floorPct(), curveValueAt(points, temp));
function fillSensorSelect() {
  const sel = $("#curveSensor"), chosen = curProf()?.sensor || "__cpu__";
  let html = `<optgroup label="Follow the hottest">
      <option value="__cpu__">Hottest CPU sensor</option>
      <option value="__hottest__">Hottest sensor of any kind</option></optgroup>`;
  for (const [name, list] of bucketed(temps())) {
    html += `<optgroup label="${name}">` + list.sort((a, b) => b.reading - a.reading)
      .map(s => `<option value="${esc(s.name)}">${esc(s.name)} — ${s.reading}°</option>`).join("") + "</optgroup>";
  }
  if (![...html.matchAll(/value="([^"]*)"/g)].some(m => m[1] === esc(chosen)))
    html += `<optgroup label="Saved"><option value="${esc(chosen)}">${esc(chosen)} (not reporting)</option></optgroup>`;
  sel.innerHTML = html;
  sel.value = chosen;
}
function fitCurve() {
  const svg = $("#curveEditor");
  if (!svg.clientWidth) return;
  CV.w = svg.clientWidth; CV.h = svg.clientHeight;
  CV.l = CV.w < 500 ? 40 : 52;
  svg.setAttribute("viewBox", `0 0 ${CV.w} ${CV.h}`);
  drawCurve();
}
function drawCurve() {
  const svg = $("#curveEditor"), prof = curProf();
  if (!prof || !CV.w) return;
  prof.points.sort((a, b) => a[0] - b[0]);
  svg.innerHTML = "";
  const s = selectedSensor();
  if (s?.caution) {
    const x1 = CX(Math.max(CV.tmin, Math.min(CV.tmax, s.caution)));
    mk(svg, "rect", { x:x1, y:CV.t, width:Math.max(0, CV.w - CV.r - x1), height:PH(), fill:"rgba(242,169,59,.07)" });
    mk(svg, "text", { x:x1 + 5, y:CV.h - CV.b - 6, fill:"#F2A93B", "font-size":"9.5", "font-family":"monospace", opacity:".8" }, "caution");
  }
  if (s?.critical) {
    const x2 = CX(Math.max(CV.tmin, Math.min(CV.tmax, s.critical)));
    mk(svg, "rect", { x:x2, y:CV.t, width:Math.max(0, CV.w - CV.r - x2), height:PH(), fill:"rgba(240,85,90,.10)" });
  }
  const step = CV.w < 500 ? 20 : 10;
  for (let c = CV.tmin; c <= CV.tmax; c += step) {
    mk(svg, "line", { x1:CX(c), y1:CV.t, x2:CX(c), y2:CV.h - CV.b, stroke:"#1C232E" });
    mk(svg, "text", { x:CX(c), y:CV.h - CV.b + 17, fill:"#626D7D", "font-size":"10", "text-anchor":"middle", "font-family":"monospace" }, c);
  }
  for (let p = 0; p <= 100; p += 25) {
    mk(svg, "line", { x1:CV.l, y1:CY(p), x2:CV.w - CV.r, y2:CY(p), stroke:"#1C232E" });
    mk(svg, "text", { x:CV.l - 8, y:CY(p) + 3.5, fill:"#626D7D", "font-size":"10", "text-anchor":"end", "font-family":"monospace" }, p + "%");
  }
  mk(svg, "text", { x:CV.l + PW() / 2, y:CV.h - 6, fill:"#626D7D", "font-size":"10", "text-anchor":"middle", "font-family":"monospace" }, "sensor temperature  °C");
  const fl = floorPct();
  if (fl > 0) {
    mk(svg, "rect", { x:CV.l, y:CY(fl), width:PW(), height:CY(0) - CY(fl), fill:"rgba(98,109,125,.10)" });
    mk(svg, "line", { x1:CV.l, y1:CY(fl), x2:CV.w - CV.r, y2:CY(fl), stroke:"#98A2B2", "stroke-dasharray":"6 4", opacity:".7" });
    mk(svg, "text", { x:CV.w - CV.r - 4, y:CY(fl) - 5, fill:"#98A2B2", "font-size":"10", "text-anchor":"end", "font-family":"monospace" }, `minimum ${fl}%`);
  }
  profiles().filter(o => o.id !== prof.id).forEach(o => {
    const pts = o.points.slice().sort((a, b) => a[0] - b[0]);
    mk(svg, "path", { d: pts.map((q, i) => `${i ? "L" : "M"}${CX(q[0]).toFixed(1)},${CY(q[1]).toFixed(1)}`).join(""),
      fill:"none", stroke:profColor(o.id), "stroke-width":"1.4", "stroke-dasharray":"5 4", opacity:".35" });
  });
  // Adaptive mode: the target line, what it has learned, and any suggestion.
  if (prof.mode === "target") {
    const goal = curveStatus()?.profile_state?.[prof.id]?.goal ?? prof.target_c;
    const gx = CX(Math.max(CV.tmin, Math.min(CV.tmax, goal)));
    mk(svg, "line", { x1:gx, y1:CV.t, x2:gx, y2:CV.h - CV.b, stroke:"#2FC08B", "stroke-width":"1.5", "stroke-dasharray":"6 3" });
    mk(svg, "text", { x:gx - 5, y:CV.t + 26, fill:"#2FC08B", "font-size":"10.5", "text-anchor":"end",
      "font-family":"monospace" }, `target ${Math.round(goal)}°`);
  }
  const sug = state.suggest?.pid === prof.id ? state.suggest : null;
  (sug?.samples || []).forEach(p => mk(svg, "circle", {
    cx:CX(Math.max(CV.tmin, Math.min(CV.tmax, p.t))), cy:CY(p.p), r:2, fill:"#2FC08B", opacity:".35" }));
  if (sug?.points) {
    mk(svg, "path", { d: sug.points.map((q, i) => `${i ? "L" : "M"}${CX(q[0]).toFixed(1)},${CY(q[1]).toFixed(1)}`).join(""),
      fill:"none", stroke:"#2FC08B", "stroke-width":"2.2", "stroke-dasharray":"7 4" });
    sug.points.forEach(q => mk(svg, "circle", { cx:CX(q[0]), cy:CY(q[1]), r:3.5, fill:"#2FC08B" }));
  }
  const accent = profColor(prof.id);
  // In adaptive mode the points are only the starting speed, so play them down.
  const dim = prof.mode === "target" ? ".35" : "1";
  CE.area   = mk(svg, "path", { fill:accent, opacity: prof.mode === "target" ? ".04" : ".11", stroke:"none" });
  CE.ext    = mk(svg, "path", { fill:"none", stroke:accent, "stroke-width":"1.4", "stroke-dasharray":"3 4", opacity:".55" });
  CE.line   = mk(svg, "path", { fill:"none", stroke:accent, "stroke-width":"2.2", "stroke-linejoin":"round", "stroke-linecap":"round", opacity:dim });
  CE.hover  = mk(svg, "g", { opacity:"0" });
  CE.marker = mk(svg, "g", {});
  CE.nodes = prof.points.map(p => {
    const g = mk(svg, "g", { class:"pt" });
    mk(g, "circle", { r:COARSE ? 24 : 18, fill:"transparent" });
    const dot = mk(g, "circle", { r:6, fill:"#10141B", stroke:accent, "stroke-width":"2.2" });
    const lbl = mk(g, "text", { fill:"#98A2B2", "font-size":"10", "text-anchor":"middle", "font-family":"monospace", opacity:"0" });
    let moved = false;
    g.addEventListener("pointerdown", ev => {
      ev.preventDefault(); ev.stopPropagation();
      curveDragging = true; moved = false; selectPoint(p);
      const move = e2 => { const q = svgPt(e2); p[0] = clampT(XC(q.x)); p[1] = clampP(YP(q.y));
        moved = true; updateCurve(); renderPtEditor(); };
      const up = () => {
        curveDragging = false;
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", up);
        window.removeEventListener("pointercancel", up);
        if (!moved) return;
        clickGuard = true; setTimeout(() => { clickGuard = false; }, 250);
        drawCurve(); saveCurve();
      };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
      window.addEventListener("pointercancel", up);
    });
    return { p, g, dot, lbl };
  });
  svg.onpointermove = ev => {
    if (curveDragging || ev.target.closest(".pt") || ev.pointerType === "touch") { CE.hover.setAttribute("opacity", "0"); return; }
    const q = svgPt(ev);
    if (q.x < CV.l || q.x > CV.w - CV.r) { CE.hover.setAttribute("opacity", "0"); return; }
    const temp = clampT(XC(q.x)), pct = Math.round(effective(prof.points, temp));
    CE.hover.innerHTML = ""; CE.hover.setAttribute("opacity", "1");
    mk(CE.hover, "line", { x1:CX(temp), y1:CV.t, x2:CX(temp), y2:CV.h - CV.b, stroke:"#98A2B2", "stroke-dasharray":"2 4", opacity:".55" });
    mk(CE.hover, "circle", { cx:CX(temp), cy:CY(pct), r:3.5, fill:"#98A2B2" });
    const left = CX(temp) < CV.w / 2;
    mk(CE.hover, "text", { x:CX(temp) + (left ? 9 : -9), y:CY(pct) - 9, fill:"#E8ECF3", "font-size":"10.5",
      "font-family":"monospace", "text-anchor":left ? "start" : "end" }, `${temp}° → ${pct}%`);
  };
  svg.onpointerleave = () => CE.hover.setAttribute("opacity", "0");
  let lastTap = 0;
  const addAt = ev => {
    const q = svgPt(ev);
    if (q.x < CV.l || q.x > CV.w - CV.r || q.y < CV.t || q.y > CV.h - CV.b) return;
    const np = [clampT(XC(q.x)), clampP(YP(q.y))];
    prof.points.push(np); selPoint = np; drawCurve(); renderPtEditor(); saveCurve();
  };
  svg.ondblclick = ev => { if (!clickGuard && !ev.target.closest(".pt")) addAt(ev); };
  svg.onclick = ev => {
    if (clickGuard || ev.target.closest(".pt")) return;
    const now = Date.now();
    if (COARSE && now - lastTap < 350) { lastTap = 0; return addAt(ev); }   // double-tap
    lastTap = now;
    selPoint = null; renderPtEditor(); updateCurve();
  };
  updateCurve();
}
let rebinding = false;
function updateCurve() {
  const prof = curProf();
  if (!prof || !CE.line) return;
  const stale = CE.nodes.length !== prof.points.length || CE.nodes.some(n => !prof.points.includes(n.p));
  if (stale && !rebinding) { rebinding = true; try { drawCurve(); } finally { rebinding = false; } return; }
  const pts = prof.points.slice().sort((a, b) => a[0] - b[0]);
  const d = pts.map((p, i) => `${i ? "L" : "M"}${CX(p[0]).toFixed(1)},${CY(p[1]).toFixed(1)}`).join("");
  CE.line.setAttribute("d", d);
  CE.area.setAttribute("d", `${d} L${CX(pts.at(-1)[0])},${CY(0)} L${CX(pts[0][0])},${CY(0)} Z`);
  // Below the first point and above the last, the curve holds flat.
  const ex = `M${CV.l},${CY(pts[0][1])} L${CX(pts[0][0])},${CY(pts[0][1])} ` +
             `M${CX(pts.at(-1)[0])},${CY(pts.at(-1)[1])} L${CV.w - CV.r},${CY(pts.at(-1)[1])}`;
  CE.ext.setAttribute("d", ex);
  CE.nodes.forEach(n => {
    const x = CX(n.p[0]), y = CY(n.p[1]), on = n.p === selPoint;
    n.g.querySelectorAll("circle").forEach(c => { c.setAttribute("cx", x); c.setAttribute("cy", y); });
    n.lbl.setAttribute("x", x); n.lbl.setAttribute("y", y - 14);
    n.lbl.textContent = `${n.p[0]}° / ${n.p[1]}%`;
    n.lbl.setAttribute("opacity", on ? "1" : "0");
    n.dot.setAttribute("stroke", on ? "#E8ECF3" : profColor(prof.id));
    n.dot.setAttribute("r", on ? 7.5 : 6);
  });
  CE.marker.innerHTML = "";
  const live = selectedSensor(), c = curveStatus();
  const st = c?.profile_state?.[prof.id];
  const nowTemp = c?.config?.enabled && st ? st.temp : live?.reading;
  if (nowTemp != null) {
    const x = CX(Math.max(CV.tmin, Math.min(CV.tmax, nowTemp)));
    // Adaptive mode has no curve value: show the speed it actually chose.
    const wanted = prof.mode === "target" && st ? st.applied : Math.round(effective(prof.points, nowTemp));
    const left = x < CV.w / 2;
    mk(CE.marker, "line", { x1:x, y1:CV.t, x2:x, y2:CV.h - CV.b, stroke:"#F2A93B", "stroke-dasharray":"3 3" });
    mk(CE.marker, "circle", { cx:x, cy:CY(wanted), r:6, fill:"#10141B", stroke:"#F2A93B", "stroke-width":"2.2" });
    mk(CE.marker, "text", { x:x + (left ? 10 : -10), y:CV.t + 13, fill:"#F2A93B", "font-size":"10.5",
      "font-family":"monospace", "text-anchor":left ? "start" : "end" }, `now ${Math.round(nowTemp)}° → ${wanted}%`);
    if (c?.config?.enabled && st && st.applied !== wanted) {
      mk(CE.marker, "circle", { cx:x, cy:CY(st.applied), r:4.5, fill:"#4C8DFF" });
      mk(CE.marker, "text", { x:x + (left ? 10 : -10), y:CY(st.applied) - 10, fill:"#4C8DFF", "font-size":"10.5",
        "font-family":"monospace", "text-anchor":left ? "start" : "end" }, `ramping: ${st.applied}%`);
    }
  }
  showCurveLive();
}
function selectPoint(p) { selPoint = p; renderPtEditor(); updateCurve(); }
function renderPtEditor() {
  const box = $("#ptEditor"), prof = curProf();
  if (!selPoint || !prof || !prof.points.includes(selPoint)) { box.hidden = true; return; }
  box.hidden = false;
  $("#ptTemp").value = selPoint[0]; $("#ptPct").value = selPoint[1];
  $("#ptDel").disabled = prof.points.length <= 2;
}
$("#ptTemp").oninput = () => { if (selPoint) { selPoint[0] = clampT(+$("#ptTemp").value); updateCurve(); } };
$("#ptPct").oninput  = () => { if (selPoint) { selPoint[1] = clampP(+$("#ptPct").value); updateCurve(); } };
$("#ptTemp").onchange = $("#ptPct").onchange = () => { drawCurve(); saveCurve(); };
$("#ptDel").onclick = () => {
  const prof = curProf();
  if (!selPoint || !prof || prof.points.length <= 2) return;
  prof.points = prof.points.filter(q => q !== selPoint);
  selPoint = null; renderPtEditor(); drawCurve(); saveCurve();
};
$("#ptAdd").onclick = () => {
  const prof = curProf(); if (!prof) return;
  const pts = prof.points.slice().sort((a, b) => a[0] - b[0]);
  let gap = -1, at = 0;
  for (let i = 0; i < pts.length - 1; i++) { const g = pts[i + 1][0] - pts[i][0]; if (g > gap) { gap = g; at = i; } }
  const np = pts.length > 1 ? [Math.round((pts[at][0] + pts[at + 1][0]) / 2), Math.round((pts[at][1] + pts[at + 1][1]) / 2)] : [60, 40];
  prof.points.push(np); selPoint = np; drawCurve(); renderPtEditor(); saveCurve();
};
$("#curvePresets").onclick = e => {
  const b = e.target.closest("button[data-preset]"), prof = curProf();
  if (!b || !prof) return;
  prof.points = CURVE_PRESETS[b.dataset.preset].map(p => p.slice());
  selPoint = null; renderPtEditor(); drawCurve(); saveCurve();
  toast(`Loaded the "${b.dataset.preset}" curve`, "good");
};
function showCurveLive() {
  const c = curveStatus(), prof = curProf(); if (!c || !prof) return;
  const box = $("#curveLive"), live = selectedSensor(), st = c.profile_state?.[prof.id];
  const n = fansOf(prof.id).length, fanWord = `${n} fan${n === 1 ? "" : "s"}`, dot = profColor(prof.id);
  if (c.tripped_at) box.innerHTML = `<span class="live-dot"></span>Paused by the watchdog — ${esc(c.trip_reason || "")}`;
  else if (st?.learning)
    box.innerHTML = `<span class="live-dot" style="background:var(--uid);box-shadow:0 0 8px var(--uid)"></span>
      Learning, step <b>${st.learning.step}</b> of ${st.learning.of}: ${fanWord} held at <b>${st.applied}%</b> —
      ${esc(st.sensor || "sensor")} <b>${Math.round(st.temp)}°</b>, about ${Math.ceil(st.learning.left_s / 60)} min left`;
  else if (c.config?.enabled && st && st.mode === "target")
    box.innerHTML = `<span class="live-dot" style="background:var(--ok);box-shadow:0 0 8px var(--ok)"></span>
      Adaptive: ${esc(st.sensor || "sensor")} <b>${Math.round(st.temp)}°</b> (target <b>${Math.round(st.goal)}°</b>)
      → ${fanWord} at <b>${st.applied}%</b>` +
      (st.goal < prof.target_c ? ` <span class="live-err">target capped at ${Math.round(st.goal)}° to stay clear of the watchdog</span>` : "");
  else if (c.config?.enabled && st)
    box.innerHTML = `<span class="live-dot" style="background:${dot};box-shadow:0 0 8px ${dot}"></span>
      ${esc(st.sensor || "sensor")} <b>${Math.round(st.temp)}°</b> → ${fanWord} capped at <b>${st.applied}%</b>`;
  else if (live && prof.mode === "target")
    box.innerHTML = `<span class="live-dot idle"></span>${esc(live.name)} is at <b>${live.reading}°</b> — adaptive mode will hold it at or below <b>${prof.target_c}°</b>`;
  else if (live)
    box.innerHTML = `<span class="live-dot idle"></span>${esc(live.name)} is at <b>${live.reading}°</b> — this curve would give <b>${Math.round(effective(prof.points, live.reading))}%</b> to ${fanWord}`;
  else box.textContent = c.config?.enabled ? "Enabled — waiting for the first cycle." : `Off. Would drive ${fanWord}.`;
  if (c.last_error) box.innerHTML += ` <span class="live-err">${esc(c.last_error)}</span>`;
}
function renderProfileBar() {
  const bar = $("#profBar"); bar.innerHTML = "";
  profiles().forEach(pr => {
    const tab = el("span", "prof-tab" + (pr.id === curProf()?.id ? " on" : ""));
    tab.innerHTML = `<i class="pdot" style="background:${profColor(pr.id)}"></i>${esc(pr.name)} <span class="pstate"></span>`;
    tab.onclick = () => { state.profId = pr.id; selPoint = null; renderCurveUI(); };
    bar.appendChild(tab);
  });
  const add = el("span", "prof-tab prof-add", "+ Add curve");
  add.onclick = guard(async () => {
    const cfg = state.curveCfg;
    let n = cfg.profiles.length + 1, id;
    do { id = "profile_" + n++; } while (cfg.profiles.some(pr => pr.id === id));
    cfg.profiles.push({ id, name:`Curve ${cfg.profiles.length + 1}`, sensor:"__cpu__",
                        points: CURVE_PRESETS.balanced.map(pt => pt.slice()) });
    state.profId = id; selPoint = null;
    await saveCurve(); renderCurveUI();
    toast("New curve added — tap the fans it should drive", "good");
  });
  bar.appendChild(add);
  updateProfileLive();
}
function updateProfileLive() {
  const c = curveStatus();
  $$("#profBar .prof-tab:not(.prof-add)").forEach((tab, i) => {
    const pr = profiles()[i]; if (!pr) return;
    const st = c?.profile_state?.[pr.id], n = fansOf(pr.id).length;
    tab.querySelector(".pstate").textContent = st ? `${st.applied}%` : `${n} fan${n === 1 ? "" : "s"}`;
  });
  $$("#assignChips .fchip .fpct").forEach((span, i) => {
    const pct = fansLive()[i]?.reading;
    span.textContent = typeof pct === "number" ? pct + "%" : "";
  });
}
function renderAssignChips() {
  const box = $("#assignChips"); box.innerHTML = "";
  const prof = curProf(); if (!prof) return;
  for (let i = 0; i < fanTotal(); i++) {
    const owner = assignOf(i) || profiles()[0]?.id;
    const ownerProf = profiles().find(pr => pr.id === owner);
    const chip = el("div", "fchip" + (owner === prof.id ? " mine" : ""));
    chip.innerHTML = `<i class="pdot" style="background:${profColor(owner)}"></i>F${i + 1}<span class="fpct"></span>`;
    chip.title = owner === prof.id ? `Fan ${i + 1} follows this curve.` : `Fan ${i + 1} follows "${ownerProf?.name || "?"}". Tap to move it here.`;
    chip.onclick = guard(async () => {
      if (owner === prof.id) return;
      state.curveCfg.assign[String(i)] = prof.id;
      await saveCurve(); renderAssignChips(); renderProfileBar();
    });
    box.appendChild(chip);
  }
  updateProfileLive();
}
$("#profName").onchange = guard(async () => {
  const prof = curProf(); if (!prof) return;
  const name = $("#profName").value.trim();
  if (name) prof.name = name; else $("#profName").value = prof.name;
  await saveCurve(); renderProfileBar();
});
$("#profDel").onclick = guard(async () => {
  const cfg = state.curveCfg, prof = curProf();
  if (!prof || cfg.profiles.length <= 1) return toast("At least one curve has to stay.", "bad");
  const fallback = cfg.profiles.find(p => p.id !== prof.id);
  if (!confirm(`Delete "${prof.name}"? Its fans move to "${fallback.name}".`)) return;
  Object.keys(cfg.assign).forEach(k => { if (cfg.assign[k] === prof.id) cfg.assign[k] = fallback.id; });
  cfg.profiles = cfg.profiles.filter(p => p.id !== prof.id);
  state.profId = fallback.id; selPoint = null;
  await saveCurve(); renderCurveUI();
});

function adoptCurve(force) {
  const c = curveStatus(); if (!c) return;
  if (!force && state.curveFor === state.current && state.curveCfg) {
    if (!state.curveSaving) state.curveCfg.enabled = c.config.enabled;   // watchdog / other tabs
    return;
  }
  state.curveCfg = JSON.parse(JSON.stringify(c.config));
  state.curveFor = state.current;
  if (!profiles().some(p => p.id === state.profId)) state.profId = profiles()[0]?.id;
  selPoint = null;
}
function renderCurveUI() {
  const cfg = state.curveCfg; if (!cfg) return;
  $("#curveOn").checked = !!cfg.enabled;
  $("#curveState").textContent = cfg.enabled ? "on" : "off";
  $("#curveState").classList.toggle("on", !!cfg.enabled);
  $("#curveHyst").value = cfg.hysteresis_c;
  $("#curveUp").value = cfg.ramp_up_step;
  $("#curveDown").value = cfg.ramp_down_step;
  $("#curveResume").checked = cfg.watchdog_auto_resume !== false;
  $("#profName").value = curProf()?.name || "";
  renderModeRow();
  if (document.activeElement !== $("#minPwm")) $("#minPwm").value = floorPct();
  minPwmHint();
  fillSensorSelect(); renderProfileBar(); renderAssignChips(); renderPtEditor(); fitCurve();
}
const saveCurve = guard(async () => {
  if (!state.curveCfg) return;
  state.curveSaving = true;
  const cfg = state.curveCfg;
  cfg.enabled = $("#curveOn").checked;
  cfg.hysteresis_c = +$("#curveHyst").value;
  cfg.ramp_up_step = +$("#curveUp").value;
  cfg.ramp_down_step = +$("#curveDown").value;
  cfg.watchdog_auto_resume = $("#curveResume").checked;
  try {
    const fresh = await sapi("/curve", { method:"PUT", body:{ config: cfg } });
    // Keep our own objects: the SVG nodes and chip handlers point at them.
    const s = S(); if (s.curve) Object.assign(s.curve, fresh, { config: cfg });
    cfg.assign = Object.assign(cfg.assign, fresh.config.assign);
  } finally { state.curveSaving = false; }
  $("#curveState").textContent = cfg.enabled ? "on" : "off";
  $("#curveState").classList.toggle("on", !!cfg.enabled);
  renderBanner(); updateProfileLive(); renderRail();
  return true;
});
/* ---- adaptive mode and curve suggestion ---- */
function renderModeRow() {
  const prof = curProf(); if (!prof) return;
  const adaptive = prof.mode === "target";
  $$("#profMode button").forEach(b => b.classList.toggle("on", b.dataset.pm === (prof.mode || "curve")));
  $("#targetBox").hidden = !adaptive;
  if (document.activeElement !== $("#targetC")) $("#targetC").value = prof.target_c ?? 65;
  $("#curveHint").textContent = adaptive
    ? "Adaptive mode: the points are only where it starts. The dashed green line is the target; the fans settle on the quietest speed that holds it."
    : "Drag a point to reshape the curve, or tap one to type exact numbers. Double-click (double-tap) empty space to add a point.";
  const sug = state.suggest?.pid === prof.id ? state.suggest : null;
  $("#suggestBox").hidden = !sug;
  if (sug) {
    $("#suggestBox").classList.toggle("bad", !!sug.error);
    const m = sug.model;
    $("#suggestText").textContent = sug.error ||
      `Learned from ${sug.pairs} steady 5-minute periods (${sug.hours} h, ${sug.observed[0]}–${sug.observed[1]}°): ` +
      `how hot this part runs for a given load, room temperature and fan speed — the model predicts it within ` +
      `±${m.error_c}°C. The dashed green curve is designed so the fans sit as low as they can when it's easy and ` +
      `reach just enough to hold ${sug.target}° at the highest load and warmest room seen (plus margin)` +
      (sug.trip ? `, then climb to 100% before the watchdog trip point (${sug.trip}°).` : ".") +
      (sug.notes?.length ? " " + sug.notes.join(" ") : "");
    const p = sug.predict;
    $("#suggestPredict").innerHTML = p ? `
      <div><span>Typical load</span><b>${p.typical.temp}° at ${p.typical.fan}%</b><small>${p.typical.power} W, room ${p.typical.inlet}°</small></div>
      <div><span>Peak load</span><b>${p.peak.temp}° at ${p.peak.fan}%</b><small>${p.peak.power} W, room ${p.peak.inlet}°</small></div>` : "";
    if (document.activeElement !== $("#sugTarget") && sug.target) $("#sugTarget").value = Math.round(sug.target);
  }
  updateLearnButton();
}
function updateLearnButton() {
  const prof = curProf(); if (!prof) return;
  const run = curveStatus()?.profile_state?.[prof.id]?.learning;
  $("#learnBtn").textContent = run ? `Stop learning (${Math.ceil(run.left_s / 60)} min left)` : "Learn (~30 min)";
  $("#learnBtn").classList.toggle("danger", !!run);
}
$("#learnBtn").onclick = guard(async () => {
  const prof = curProf(); if (!prof) return;
  if (curveStatus()?.profile_state?.[prof.id]?.learning) {
    await sapi(`/curve/learn?profile=${encodeURIComponent(prof.id)}`, { method:"DELETE" });
    return toast("Learning stopped — normal control resumed", "good");
  }
  if (!confirm(`Learn how "${prof.name}" responds to airflow?\n\nFor about 30 minutes its fans are held at: normal +20%, ` +
               `then +10%, then 5% below normal — 10 minutes each. It stops at once if the temperature gets near its ` +
               `limit, and everything is logged. Afterwards, Suggest curve can design a curve for this server.`)) return;
  const r = await sapi("/curve/learn", { body:{ profile: prof.id } });
  toast(`Learning started: ${r.levels.join("%, ")}% for 10 minutes each`, "good");
});
async function runSuggest(target) {
  const prof = curProf(); if (!prof) return;
  $("#suggestBtn").disabled = true;
  try {
    const t = target ? `&target=${encodeURIComponent(target)}` : "";
    const r = await sapi(`/curve/suggest?profile=${encodeURIComponent(prof.id)}&days=7${t}`);
    state.suggest = { pid: prof.id, ...r };
  } finally { $("#suggestBtn").disabled = false; }
  renderModeRow(); drawCurve();
}
$("#sugRecalc").onclick = guard(() => runSuggest(+$("#sugTarget").value || null));
$("#profMode").onclick = guard(async e => {
  const b = e.target.closest("button[data-pm]"), prof = curProf();
  if (!b || !prof || prof.mode === b.dataset.pm) return;
  prof.mode = b.dataset.pm;
  if (prof.mode === "target") {
    // Start just above where this sensor is now, so switching causes no jolt
    // (an HD controller at 87° with a 65° target would go straight to 100%).
    const now = selectedSensor()?.reading;
    prof.target_c = Math.max(30, Math.min(95, now != null ? Math.round(now) + 3 : (prof.target_c || 65)));
  }
  renderModeRow(); drawCurve();
  if (await saveCurve())
    toast(prof.mode === "target" ? `Adaptive: keeping ${prof.name} at or below ${prof.target_c}°` : "Back to the fixed curve", "good");
});
$("#targetC").onchange = guard(async () => {
  const prof = curProf(); if (!prof) return;
  prof.target_c = Math.max(30, Math.min(95, Math.round(+$("#targetC").value || 65)));
  $("#targetC").value = prof.target_c;
  drawCurve();
  if (await saveCurve()) toast(`Target set to ${prof.target_c}°`, "good");
});
$("#suggestBtn").onclick = guard(() => runSuggest(null));
$("#suggestDiscard").onclick = () => { state.suggest = null; renderModeRow(); drawCurve(); };
$("#suggestApply").onclick = guard(async () => {
  const prof = curProf(), sug = state.suggest;
  if (!prof || !sug?.points || sug.pid !== prof.id) return;
  if (!confirm(`Replace the points of "${prof.name}" with the suggested curve and switch it to Fixed curve?`)) return;
  prof.points = sug.points.map(p => p.slice());
  prof.mode = "curve";
  state.suggest = null; selPoint = null;
  renderModeRow(); drawCurve(); renderPtEditor();
  if (await saveCurve()) toast("Suggested curve applied", "good");
});

function minPwmHint() {
  const v = +$("#minPwm").value;
  $("#minPwmHint").textContent = v < 5 ? "Below 5% some fans stall and iLO reports a fan fault."
    : v !== floorPct() ? "Not saved yet." : "";
  $("#minPwmHint").className = "hint" + (v < 5 ? " warn-note" : "");
}
$("#minPwm").oninput = minPwmHint;
$("#minPwmSave").onclick = guard(async () => {
  const v = Math.max(0, Math.min(60, Math.round(+$("#minPwm").value)));
  if (v < 5 && !confirm(`Allow fans down to ${v}%? Some fans stall that slowly, and iLO then spins everything to full speed.`)) return;
  await api(`/servers/${encodeURIComponent(state.current)}`, { method:"PUT", body:{ server:{ min_pwm_percent: v } } });
  if (S().guards) S().guards.min_pwm_percent = v;
  await loadServers();
  $("#minPwm").value = v; minPwmHint(); drawCurve(); renderRail();
  toast(`Minimum fan speed set to ${v}%`, "good");
});
$("#minPwm").onkeydown = e => { if (e.key === "Enter") $("#minPwmSave").click(); };
$("#curveSave").onclick = async () => { if (await saveCurve()) toast("Curve settings saved", "good"); };
$("#curveOn").onchange = async () => {
  if (!await saveCurve()) return;
  toast($("#curveOn").checked ? "Curve on — it survives reboots, no need to toggle it again"
                              : "Curve off — fans handed back to iLO", "good");
};
$("#curveSensor").onchange = () => {
  const prof = curProf(); if (prof) prof.sensor = $("#curveSensor").value;
  saveCurve(); drawCurve();
};
new ResizeObserver(() => fitCurve()).observe($("#curveEditor"));
new ResizeObserver(() => renderTrend()).observe($("#trend"));

/* ---------------- tuning ---------------- */
const out = txt => { $("#tuningOut").textContent = txt || "(no output)"; $("#tuningOut").scrollTop = 0; };
const run = (label, p) => guard(async () => { out(`${label}…`); const r = await p(); out(r.output ?? r.raw); })();
$$("[data-fang]").forEach(b => b.onclick = () => {
  if (b.dataset.fang === "stop" && !confirm("Stop iLO's fan loop? Fans will hold their current speed and won't react to heat until you press Start loop.")) return;
  run("fan g " + b.dataset.fang, () => sapi("/fan/global", { body:{ op: b.dataset.fang } }));
});
$$("[data-info-cmd]").forEach(b => b.onclick = () => run("fan info " + b.dataset.infoCmd, () => sapi("/fan/info?section=" + b.dataset.infoCmd)));
$("#dumpBtn").onclick = () => run("fan dump", () => sapi("/fan/dump"));
$("#rateApply").onclick = () => run("fan rate", () => sapi("/fan/rate", { body:{ ms:+$("#rateMs").value } }));
$("#pidApply").onclick = () => run("fan pid", () => sapi("/fan/pid", { body:{ index:+$("#pidIdx").value, key:$("#pidKey").value, value:$("#pidVal").value } }));
$("#tApply").onclick = () => {
  if ($("#tKey").value === "off" && !confirm("Ignore this sensor? Only do this for one you're sure isn't reporting real heat.")) return;
  run("fan t", () => sapi("/fan/temp", { body:{ index:+$("#tIdx").value, key:$("#tKey").value, value:$("#tVal").value } }));
};
$("#hApply").onclick = () => run("fan h", () => sapi("/fan/tach", { body:{ index:+$("#hIdx").value, key:$("#hKey").value, value:$("#hVal").value } }));
$("#rawFanRun").onclick = () => run($("#rawFanCmd").value, () => sapi("/fan/raw", { body:{ command:$("#rawFanCmd").value } }));
$("#rawFanCmd").onkeydown = e => { if (e.key === "Enter") $("#rawFanRun").click(); };
$("#rawExamples").onclick = e => { const c = e.target.closest("[data-raw]"); if (c) { $("#rawFanCmd").value = c.dataset.raw; $("#rawFanCmd").focus(); } };

/* ---------------- settings sheet ---------------- */
function openSheet() { $("#sheet").hidden = false; renderServerList(); loadSheetExtras(); }
function closeSheet() { $("#sheet").hidden = true; $("#serverFormSec").hidden = true; }
$("#settingsBtn").onclick = openSheet;
$("#onboardAdd").onclick = () => { openSheet(); openServerForm(null); };
$$("#sheet [data-close]").forEach(n => n.onclick = closeSheet);
$$("#delDialog [data-close]").forEach(n => n.onclick = () => $("#delDialog").hidden = true);
document.addEventListener("keydown", e => {
  // 1-5 switch tabs, unless you're typing somewhere or a dialog is open.
  if (/^[1-5]$/.test(e.key) && !e.ctrlKey && !e.metaKey && !e.altKey &&
      !e.target.closest?.("input,select,textarea") && $("#sheet").hidden && state.servers.length) {
    showTab(["dash", "sensors", "history", "fans", "tuning"][+e.key - 1]); return;
  }
  if (e.key !== "Escape") return;
  if (!$("#delDialog").hidden) $("#delDialog").hidden = true;
  else if (!$("#sheet").hidden) closeSheet();
  closeMenu();
});

function renderServerList() {
  const box = $("#serverList"); box.innerHTML = "";
  if (!state.servers.length) box.appendChild(el("p", "empty", "No servers yet."));
  state.servers.forEach((cfg, idx) => {
    const snap = state.snaps[cfg.id];
    const l = snap?.link?.redfish;
    const r = el("div", "srv-row");
    r.innerHTML = `<span class="lamp ${linkLamp(snap)}"></span>
      <span style="min-width:0"><div class="nm">${esc(cfg.name)}</div>
        <div class="sub">${esc(cfg.host)} · ${cfg.enabled ? esc(l || "starting") : "paused"}${snap?.link?.patched === false ? " · unpatched" : ""}</div></span>
      <span class="acts">
        <button class="ghost" data-a="up" ${idx ? "" : "disabled"} aria-label="Move up">↑</button>
        <button class="ghost" data-a="edit">Edit</button>
        <button class="ghost" data-a="toggle">${cfg.enabled ? "Pause" : "Resume"}</button>
        <button class="ghost danger" data-a="del">Remove</button>
      </span>`;
    r.querySelector("[data-a=edit]").onclick = () => openServerForm(cfg);
    r.querySelector("[data-a=toggle]").onclick = guard(async () => {
      await api(`/servers/${cfg.id}`, { method:"PUT", body:{ server:{ enabled: !cfg.enabled } } });
      await loadServers(); renderServerList();
      toast(cfg.enabled ? `${cfg.name} paused` : `${cfg.name} resumed`, "good");
    });
    r.querySelector("[data-a=up]").onclick = guard(async () => {
      const ids = state.servers.map(s => s.id);
      [ids[idx - 1], ids[idx]] = [ids[idx], ids[idx - 1]];
      await api("/servers/order", { method:"PUT", body:{ ids } });
      await loadServers(); renderServerList();
    });
    r.querySelector("[data-a=del]").onclick = () => askDelete(cfg);
    box.appendChild(r);
  });
}
let editing = null;
function openServerForm(cfg) {
  editing = cfg;
  const f = $("#serverForm"), d = state.meta?.defaults || {};
  $("#serverFormTitle").textContent = cfg ? `Edit ${cfg.name}` : "Add server";
  f.reset();
  const v = cfg || { username:"Administrator", ssh_port:22, https_port:443, poll_seconds:d.poll_seconds ?? 10,
    min_pwm_percent:d.min_pwm_percent ?? 8, watchdog_margin_c:d.watchdog_margin_c ?? 5,
    reassert_seconds:300, watchdog_enabled:true, enabled:true };
  for (const [k, val] of Object.entries(v)) {
    const inp = f.elements[k]; if (!inp) continue;
    if (inp.type === "checkbox") inp.checked = !!val; else inp.value = val ?? "";
  }
  f.elements.reassert_minutes.value = Math.round((v.reassert_seconds ?? 300) / 60);
  f.elements.password.placeholder = cfg ? "unchanged" : "";
  if (cfg) f.elements.password.value = "";
  $("#testBox").hidden = true;
  $("#serverFormSec").hidden = false;
  $("#serverFormSec").scrollIntoView({ behavior:"smooth", block:"start" });
  setTimeout(() => f.elements[cfg ? "name" : "host"].focus(), 150);
}
function readServerForm() {
  const f = $("#serverForm"), o = {};
  for (const inp of f.elements) {
    if (!inp.name) continue;
    if (inp.type === "checkbox") o[inp.name] = inp.checked;
    else if (inp.type === "number") o[inp.name] = inp.value === "" ? undefined : +inp.value;
    else o[inp.name] = inp.value.trim();
  }
  o.reassert_seconds = (o.reassert_minutes || 0) * 60; delete o.reassert_minutes;
  if (editing) { o.id = editing.id; if (!o.password) delete o.password; }
  return o;
}
$("#addServerBtn").onclick = () => openServerForm(null);
$("#serverCancel").onclick = () => { $("#serverFormSec").hidden = true; };
$("#serverTest").onclick = guard(async () => {
  const box = $("#testBox"); box.hidden = false; box.innerHTML = "Testing — this can take up to 30 seconds on an old iLO…";
  $("#serverTest").disabled = true;
  try {
    const r = await api("/servers/test", { body:{ server: readServerForm() } });
    const rf = r.redfish, sh = r.ssh;
    box.innerHTML = `<div class="${rf.ok ? "ok" : "bad"}">${rf.ok ? "✓" : "✗"} Redfish: ${rf.ok
        ? esc(`${rf.model || "?"} · ${rf.serial || ""} · power ${rf.power_state || "?"}`) : esc(rf.error)}</div>
      <div class="${sh.ok ? (sh.patched ? "ok" : "warn") : "bad"}">${sh.ok ? (sh.patched ? "✓" : "!") : "✗"} SSH: ${sh.ok
        ? (sh.patched ? "connected — patched firmware, fan control available" : "connected, but the fan command is missing (unpatched firmware)")
        : esc(sh.error)}</div>`;
  } finally { $("#serverTest").disabled = false; }
});
$("#serverForm").onsubmit = guard(async e => {
  e.preventDefault();
  const body = readServerForm();
  $("#serverSave").disabled = true;
  try {
    const r = editing
      ? await api(`/servers/${editing.id}`, { method:"PUT", body:{ server: body } })
      : await api("/servers", { body:{ server: body } });
    toast(editing ? "Server updated" : `${r.server.name} added`, "good");
    $("#serverFormSec").hidden = true;
    await loadServers();
    if (!editing || !state.current) selectServer(r.server.id);
    renderServerList();
  } finally { $("#serverSave").disabled = false; }
});
let deleting = null;
function askDelete(cfg) {
  deleting = cfg;
  $("#delName").textContent = cfg.name;
  $("#delRestore").checked = !!cfg.enabled; $("#delRestore").disabled = !cfg.enabled;
  $("#delHistory").checked = false;
  $("#delDialog").hidden = false;
}
$("#delConfirm").onclick = guard(async () => {
  const cfg = deleting; if (!cfg) return;
  $("#delConfirm").disabled = true;
  try {
    const r = await api(`/servers/${cfg.id}?restore=${$("#delRestore").checked}&purge_history=${$("#delHistory").checked}`, { method:"DELETE" });
    $("#delDialog").hidden = true;
    toast(r.restore_error ? `Removed, but handing fans back failed: ${r.restore_error}` : `${cfg.name} removed`, r.restore_error ? "bad" : "good");
    delete state.snaps[cfg.id];
    await loadServers();
    if (state.current === cfg.id) selectServer(state.servers[0]?.id || null);
    renderServerList();
  } finally { $("#delConfirm").disabled = false; }
});

async function loadSheetExtras() {
  const st = await api(`/history/meta?server=${encodeURIComponent(state.current || "")}`).catch(() => null);
  if (st) {
    $("#histRetention").value = st.settings.raw_retention_hours;
    const s = st.stats, mb = (s.db_bytes / 1048576).toFixed(1);
    $("#dbStats").textContent = `${(s.rows.raw + s.rows.m5 + s.rows.h1).toLocaleString()} rows · ${mb} MB · oldest ${s.oldest_ts ? new Date(s.oldest_ts * 1000).toLocaleDateString() : "—"}`;
  }
  const inf = await api("/integrations/influx").catch(() => null);
  if (inf) renderInflux(inf);
  renderInstall();
}
$("#histRetentionSave").onclick = guard(async () => {
  await api("/history/settings", { method:"PUT", body:{ raw_retention_hours:+$("#histRetention").value } });
  toast("Retention updated", "good");
});
function influxVersionFields() {
  const v = $("#influxVersion").value;
  $$("#influxForm [data-v]").forEach(n => n.hidden = n.dataset.v !== v);
}
$("#influxVersion").onchange = influxVersionFields;
function renderInflux(st) {
  const c = st.config;
  $("#influxOn").checked = !!c.enabled;
  $("#influxVersion").value = String(c.version || 1);
  $("#influxUrl").value = c.url || ""; $("#influxMeasurement").value = c.measurement || "ilo4";
  $("#influxDb").value = c.database || ""; $("#influxUser").value = c.username || ""; $("#influxPass").value = c.password || "";
  $("#influxOrg").value = c.org || ""; $("#influxBucket").value = c.bucket || ""; $("#influxToken").value = c.token || "";
  $("#influxTls").checked = c.verify_tls !== false;
  influxVersionFields();
  const parts = [`${st.queued} queued`, `${st.sent_total} sent`];
  if (st.dropped_total) parts.push(`${st.dropped_total} dropped`);
  parts.push(st.last_ok ? `last write ${timeAgo(st.last_ok)}` : c.enabled ? "no successful write yet" : "off");
  $("#influxStatus").textContent = st.last_error ? `⚠ ${st.last_error}` : parts.join(" · ");
}
const influxForm = () => ({
  enabled: $("#influxOn").checked, version: +$("#influxVersion").value, url: $("#influxUrl").value.trim(),
  measurement: $("#influxMeasurement").value.trim() || "ilo4", verify_tls: $("#influxTls").checked,
  database: $("#influxDb").value.trim(), username: $("#influxUser").value.trim(), password: $("#influxPass").value,
  org: $("#influxOrg").value.trim(), bucket: $("#influxBucket").value.trim(), token: $("#influxToken").value,
});
$("#influxTest").onclick = guard(async () => {
  $("#influxStatus").textContent = "Testing…";
  const r = await api("/integrations/influx/test", { body:{ config: influxForm() } });
  $("#influxStatus").textContent = r.ok ? "✓ Connected" : `✗ ${r.detail}`;
});
$("#influxSave").onclick = guard(async () => {
  renderInflux(await api("/integrations/influx", { method:"PUT", body:{ config: influxForm() } }));
  toast("InfluxDB settings saved", "good");
});
$("#logoutBtn").onclick = guard(async () => { await api("/logout", { method:"POST" }); location.reload(); });

/* ---------------- install as app ---------------- */
let installEvt = null;
window.addEventListener("beforeinstallprompt", e => { e.preventDefault(); installEvt = e; renderInstall(); });
function renderInstall() {
  const standalone = matchMedia("(display-mode: standalone)").matches || navigator.standalone;
  $("#installBtn").hidden = !installEvt || standalone || IN_APP;
  $("#changePanelBtn").hidden = !IN_APP;
  let hint = "";
  if (IN_APP || standalone) hint = "";
  else if (!window.isSecureContext) hint = "Installing as an app needs HTTPS (browser rule). Set SSL_CERT/SSL_KEY — see the README — or use the Android app.";
  else if (/iphone|ipad/i.test(navigator.userAgent)) hint = "On iPhone: Share → Add to Home Screen.";
  else if (!installEvt) hint = "Use your browser's menu → Install app / Add to Home screen.";
  $("#installHint").textContent = hint;
}
$("#installBtn").onclick = async () => { if (!installEvt) return; installEvt.prompt(); await installEvt.userChoice; installEvt = null; renderInstall(); };
$("#changePanelBtn").onclick = () => { location.href = "https://localhost/?change=1"; };
if ("serviceWorker" in navigator && window.isSecureContext) navigator.serviceWorker.register("/sw.js").catch(() => {});

/* ---------------- data flow ---------------- */
async function loadServers() {
  const r = await api("/servers");
  state.servers = r.servers;
  r.snapshots.forEach(s => { if (!state.snaps[s.id] || state.snaps[s.id].seq !== s.seq) state.snaps[s.id] = s; });
  for (const id of Object.keys(state.snaps)) if (!state.servers.some(s => s.id === id)) delete state.snaps[id];
  state.order = state.servers.map(s => s.id);
  $("#emptyState").hidden = state.servers.length > 0;
  $$("section[data-panel]").forEach(p => p.style.display = state.servers.length ? "" : "none");
  $("#nav").style.visibility = state.servers.length ? "" : "hidden";
  renderRail(); renderFleet();
}
async function refreshSnapshot() {
  if (!state.current) return;
  try { state.snaps[state.current] = await sapi("/snapshot"); renderAll(); } catch {}
}
function selectServer(id) {
  state.current = id; store.set("ilo.server", id || "");
  state.recent = []; state.recentFor = null; state.curveFor = null; state.curveCfg = null; state.suggest = null;
  $("#cage").innerHTML = "";
  if (!id) { renderAll(); return; }
  adoptCurve(true);
  renderAll();
  loadRecent().catch(() => {});
  window.HistoryView?.serverChanged();
  if (state.curveCfg) renderCurveUI();
  LOADERS[state.tab]?.();
}

let renderQueued = false;
function renderAll() {
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(() => {
    renderQueued = false;
    renderRail(); renderFleet(); renderLinkAlert();
    if (!state.current) return;
    if (isTab("dash")) { renderBanner(); renderCage(); renderTemps(); renderTrend(); renderActivity(); }
    else renderBanner();
    if (isTab("sensors")) renderSensors();
    if (isTab("fans")) {
      const first = state.curveFor !== state.current;
      adoptCurve(false);
      if (first) renderCurveUI();
      else {
        $("#curveOn").checked = !!state.curveCfg?.enabled;
        $("#curveState").textContent = state.curveCfg?.enabled ? "on" : "off";
        $("#curveState").classList.toggle("on", !!state.curveCfg?.enabled);
        if (!curveDragging) updateCurve();
        updateProfileLive(); updateLearnButton();
      }
      if (!document.activeElement?.matches?.("#fanRows input")) renderFans();
    }
  });
}

let wsTries = 0;
function connectWs() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => { wsTries = 0; $("#livePill").hidden = true; };
  ws.onmessage = ev => {
    const d = JSON.parse(ev.data);
    const known = state.servers.map(s => s.id).join();
    d.snapshots.forEach(s => { state.snaps[s.id] = s; pushRecent(s); });
    if (d.ids.join() !== known) loadServers().then(() => {
      if (!state.current || !state.servers.some(s => s.id === state.current)) selectServer(state.servers[0]?.id || null);
    }).catch(() => {});
    renderAll();
  };
  ws.onclose = ev => {
    if (ev.code === 4401) return showGate();
    if (wsTries > 0) $("#livePill").hidden = false;     // ignore a single blip
    setTimeout(connectWs, Math.min(15000, 1500 * ++wsTries));
  };
}

const LOADERS = {
  dash: () => { renderAll(); if (state.recentFor !== state.current) loadRecent().catch(() => {}); },
  sensors: () => { renderSensors(); if (state.recentFor !== state.current) loadRecent().catch(() => {}); },
  history: () => window.HistoryView?.show(),
  fans: () => { adoptCurve(state.curveFor !== state.current); renderCurveUI(); renderFans(); loadPresets().catch(e => toast(e.message, "bad")); },
  tuning: () => {},
};

async function boot() {
  state.meta = await api("/meta");
  $("#ver").textContent = "v" + state.meta.version;
  await loadServers();
  const saved = store.get("ilo.server", "");
  const first = state.servers.find(s => s.id === saved)?.id || state.servers[0]?.id || null;
  connectWs();
  showTab(state.tab);
  selectServer(first);
  setInterval(() => { if (state.current && (isTab("dash") || isTab("sensors"))) loadRecent().catch(() => {}); }, 120000);
}

// Wait for history.js too: it registers window.HistoryView.
document.addEventListener("DOMContentLoaded", async () => {
  const a = await fetch("/api/auth", { credentials:"same-origin" }).then(r => r.json()).catch(() => ({}));
  if (a.required && !a.signed_in) showGate();
  else { $("#app").hidden = false; boot().catch(e => toast(e.message, "bad")); }
});
