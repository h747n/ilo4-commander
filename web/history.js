/* History tab: any sensor, any window, with min/max bands and event markers.
   Uses the helpers from app.js ($, api, state, mk, esc, bucketed, toast…). */

(() => {
  const RANGES = { "1h":3600, "6h":21600, "24h":86400, "7d":604800, "30d":2592000, "90d":7776000 };
  const UNIT = { temp:"°C", fan:"%", power:"W", target:"%" };
  const PALETTE = ["#4C8DFF","#F2A93B","#A78BFA","#22D3EE","#F472B6","#34D399","#FB923C","#94A3B8","#E879F9","#FACC15"];
  const EVENT_COLOR = { power:"#4C8DFF", reassert:"#2FC08B", watchdog:"#F2A93B", handback:"#F2A93B", link:"#F0555A", tune:"#4C8DFF", anomaly:"#F0555A", info:"#98A2B2" };
  const H = {
    kind:"temp", range: store.get("ilo.hrange", "24h"), end:null,
    sel:{ temp:null, fan:null, power:null, target:null }, colors:new Map(),
    hidden:new Set(), meta:null, data:null, req:0, metaFor:null, timer:null,
  };

  const colorOf = name => {
    if (!H.colors.has(name)) {
      const used = new Set(H.colors.values());
      H.colors.set(name, PALETTE.find(c => !used.has(c)) || PALETTE[H.colors.size % PALETTE.length]);
    }
    return H.colors.get(name);
  };
  const names = () => H.meta?.names?.[H.kind] || [];
  const selected = () => H.sel[H.kind] || new Set();
  const windowNow = () => {
    const until = H.end || Math.floor(Date.now() / 1000);
    return { since: until - RANGES[H.range], until };
  };

  function defaultSel(kind) {
    const all = H.meta?.names?.[kind] || [];
    if (kind !== "temp") return new Set(all.slice(0, 10));
    const pick = all.filter(n => /cpu|inlet|ambient/i.test(n));
    return new Set((pick.length ? pick : all.slice(0, 4)).slice(0, 8));
  }

  async function loadMeta() {
    const sid = state.current; if (!sid) return;
    H.meta = await api(`/history/meta?server=${encodeURIComponent(sid)}`);
    if (sid !== state.current) return;
    H.metaFor = sid;
    for (const k of Object.keys(H.sel)) {
      const have = H.meta.names[k] || [];
      if (!H.sel[k]) H.sel[k] = defaultSel(k);
      else H.sel[k] = new Set([...H.sel[k]].filter(n => have.includes(n)));
      if (!H.sel[k].size) H.sel[k] = defaultSel(k);
    }
    const st = H.meta.stats;
    $("#histStorage").textContent = `Full resolution for ${st.raw_retention_hours} h, 5-minute averages for 35 days, hourly for 2 years · ${(st.db_bytes / 1048576).toFixed(1)} MB on disk`;
    renderPicker();
  }

  /* ---------- sensor picker ---------- */
  function renderPicker() {
    const box = $("#histSeries"); box.innerHTML = "";
    const f = $("#histFilter").value.trim().toLowerCase();
    const list = names().filter(n => !f || n.toLowerCase().includes(f));
    if (!names().length) { box.appendChild(el("p", "empty", "Nothing recorded for this server yet — give it a minute.")); return; }
    if (!list.length) { box.appendChild(el("p", "empty", "No sensor matches that filter.")); return; }
    const groups = H.kind === "temp" ? bucketed(list.map(n => ({ name:n }))) : [["", list.map(n => ({ name:n }))]];
    for (const [label, items] of groups) {
      const wrap = el("div", "hist-group");
      if (label) wrap.appendChild(el("span", "hist-group-label", label));
      const row = el("div", "chips");
      items.forEach(({ name }) => {
        const on = selected().has(name);
        const c = el("span", "chip" + (on ? " on" : ""),
          `${on ? `<i class="dot" style="background:${colorOf(name)}"></i>` : ""}${esc(name)}`);
        if (on) c.style.borderColor = colorOf(name);
        c.onclick = () => {
          const s = selected();
          if (s.has(name)) { s.delete(name); H.colors.delete(name); }
          else if (s.size >= 24) return toast("Up to 24 lines at once.", "bad");
          else s.add(name);
          renderPicker(); loadChart();
        };
        row.appendChild(c);
      });
      wrap.appendChild(row); box.appendChild(wrap);
    }
  }
  $("#histFilter").oninput = renderPicker;
  $("#histAll").onclick = () => {
    const f = $("#histFilter").value.trim().toLowerCase();
    names().filter(n => !f || n.toLowerCase().includes(f)).slice(0, 24).forEach(n => selected().add(n));
    renderPicker(); loadChart();
  };
  $("#histNone").onclick = () => { selected().clear(); H.colors.clear(); renderPicker(); loadChart(); };

  /* ---------- controls ---------- */
  $("#histKind").onclick = e => {
    const b = e.target.closest("button[data-kind]"); if (!b) return;
    $$("#histKind button").forEach(x => x.classList.toggle("on", x === b));
    H.kind = b.dataset.kind; H.hidden.clear(); $("#histFilter").value = "";
    renderPicker(); loadChart();
  };
  function setRange(r) {
    H.range = r; store.set("ilo.hrange", r);
    $$("#histRange button").forEach(x => x.classList.toggle("on", x.dataset.range === r));
  }
  $("#histRange").onclick = e => {
    const b = e.target.closest("button[data-range]"); if (!b) return;
    setRange(b.dataset.range); loadChart();
  };
  const shift = dir => {
    const now = Math.floor(Date.now() / 1000);
    const end = (H.end || now) + dir * RANGES[H.range];
    H.end = end >= now ? null : end;
    loadChart();
  };
  $("#histPrev").onclick = () => shift(-1);
  $("#histNext").onclick = () => shift(1);
  $("#histNow").onclick = () => { H.end = null; loadChart(); };
  $("#histEnd").onchange = () => {
    const v = $("#histEnd").value; if (!v) return;
    const t = Math.floor(new Date(v).getTime() / 1000);
    H.end = t >= Date.now() / 1000 ? null : t; loadChart();
  };
  $("#histBand").onchange = $("#histEvents").onchange = () => draw();
  $("#histCsv").onclick = () => {
    if (!selected().size) return toast("Pick at least one sensor first.", "bad");
    const { since, until } = windowNow();
    const q = [...selected()].map(n => "name=" + encodeURIComponent(n)).join("&");
    location.href = `/api/history/export?server=${encodeURIComponent(state.current)}&kind=${H.kind}&${q}&since=${since}&until=${until}`;
  };

  function toLocalInput(ts) {
    const d = new Date(ts * 1000), p = n => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
  }
  function renderWindowLabel(since, until) {
    const long = RANGES[H.range] > 86400;
    const f = ts => new Date(ts * 1000).toLocaleString(undefined, long
      ? { month:"short", day:"numeric", hour:"2-digit", minute:"2-digit" }
      : { month:"short", day:"numeric", hour:"2-digit", minute:"2-digit" });
    $("#histWindow").textContent = `${f(since)} → ${H.end ? f(until) : "now"}`;
    $("#histNext").disabled = !H.end;
    $("#histNow").disabled = !H.end;
    $("#histEnd").value = toLocalInput(until);
  }

  /* ---------- data ---------- */
  async function loadChart() {
    if (!state.current || H.metaFor !== state.current) return;
    const my = ++H.req;
    const { since, until } = windowNow();
    renderWindowLabel(since, until);
    const sel = [...selected()];
    if (!sel.length) { H.data = { series:[], events:[], since, until }; draw("Pick at least one sensor below."); renderStats(); renderEvents(); return; }
    const width = $("#histChart").clientWidth || 800;
    const points = Math.max(60, Math.min(1500, Math.round(width / 1.5)));
    const q = sel.map(n => "name=" + encodeURIComponent(n)).join("&");
    try {
      const d = await api(`/history/query?server=${encodeURIComponent(state.current)}&kind=${H.kind}&${q}&since=${since}&until=${until}&points=${points}`);
      if (my !== H.req) return;
      H.data = d;
    } catch (e) { if (my === H.req) { H.data = null; draw(e.message); } return; }
    draw(); renderStats(); renderEvents();
    scheduleRefresh();
  }
  function scheduleRefresh() {
    clearTimeout(H.timer);
    if (H.end || state.tab !== "history") return;
    H.timer = setTimeout(() => { if (state.tab === "history" && !document.hidden) loadChart(); },
      RANGES[H.range] <= 21600 ? 20000 : 120000);
  }

  /* ---------- chart ---------- */
  const PAD = { l:46, r:14, t:14, b:26 };
  let geom = null;
  const niceSteps = [60, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800, 1209600];
  function timeTicks(t0, t1, maxTicks) {
    const span = t1 - t0;
    const step = niceSteps.find(s => span / s <= maxTicks) || niceSteps.at(-1);
    const off = new Date().getTimezoneOffset() * 60;            // align ticks to local time
    const first = Math.ceil((t0 - off) / step) * step + off;
    const out = [];
    for (let t = first; t <= t1; t += step) out.push(t);
    return { ticks: out, step };
  }
  function fmtTick(ts, step) {
    const d = new Date(ts * 1000);
    if (step >= 86400) return d.toLocaleDateString(undefined, { month:"short", day:"numeric" });
    if (d.getHours() === 0 && d.getMinutes() === 0) return d.toLocaleDateString(undefined, { month:"short", day:"numeric" });
    return d.toLocaleTimeString(undefined, { hour:"2-digit", minute:"2-digit" });
  }
  function niceRange(lo, hi, kind) {
    if (kind === "fan" || kind === "target") return [0, 100];
    if (lo === hi) { lo -= 1; hi += 1; }
    const pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
    const mag = Math.pow(10, Math.floor(Math.log10(hi - lo)));
    const step = [1, 2, 2.5, 5, 10].map(m => m * mag / 2).find(s => (hi - lo) / s <= 6) || mag;
    return [Math.floor(lo / step) * step, Math.ceil(hi / step) * step];
  }

  function draw(msg) {
    const svg = $("#histChart");
    const W = svg.clientWidth || 800, Hh = svg.clientHeight || 340;
    svg.setAttribute("viewBox", `0 0 ${W} ${Hh}`);
    svg.innerHTML = ""; geom = null; $("#histTip").hidden = true;
    const d = H.data;
    const series = (d?.series || []).filter(s => s.points.length && !H.hidden.has(s.name));
    if (msg || !series.length) {
      mk(svg, "text", { x:W / 2, y:Hh / 2, fill:"#626D7D", "text-anchor":"middle", "font-size":"12.5" },
        msg || (d?.series?.length ? "No data in this window." : "Pick at least one sensor below."));
      return;
    }
    const band = $("#histBand").checked;
    const all = series.flatMap(s => s.points);
    const [vMin, vMax] = niceRange(Math.min(...all.map(p => band ? p.min : p.avg)),
                                   Math.max(...all.map(p => band ? p.max : p.avg)), H.kind);
    const t0 = d.since, t1 = d.until;
    const pw = W - PAD.l - PAD.r, ph = Hh - PAD.t - PAD.b;
    const X = ts => PAD.l + (ts - t0) / ((t1 - t0) || 1) * pw;
    const Y = v => Hh - PAD.b - (v - vMin) / ((vMax - vMin) || 1) * ph;

    for (let g = 0; g <= 5; g++) {
      const v = vMin + (vMax - vMin) * g / 5, y = Y(v);
      mk(svg, "line", { x1:PAD.l, y1:y, x2:W - PAD.r, y2:y, stroke:"#1C232E" });
      mk(svg, "text", { x:PAD.l - 7, y:y + 3, fill:"#626D7D", "font-size":"9.5", "text-anchor":"end",
        "font-family":"monospace" }, (Math.abs(vMax - vMin) < 10 ? v.toFixed(1) : Math.round(v)) + (g === 5 ? UNIT[H.kind] : ""));
    }
    const { ticks, step } = timeTicks(t0, t1, W < 500 ? 4 : 7);
    ticks.forEach(ts => {
      mk(svg, "line", { x1:X(ts), y1:PAD.t, x2:X(ts), y2:Hh - PAD.b, stroke:"#161C26" });
      mk(svg, "text", { x:X(ts), y:Hh - 8, fill:"#626D7D", "font-size":"9.5", "text-anchor":"middle",
        "font-family":"monospace" }, fmtTick(ts, step));
    });

    // A gap longer than a few buckets means no data (iLO down, server paused): break the line.
    const gapOf = pts => {
      if (pts.length < 3) return Infinity;
      const ds = pts.slice(1).map((p, i) => p.ts - pts[i].ts).sort((a, b) => a - b);
      return Math.max(ds[Math.floor(ds.length / 2)] * 4, 120);
    };
    series.forEach(s => {
      const c = colorOf(s.name), pts = s.points, gap = gapOf(pts);
      const segs = []; let cur = [];
      pts.forEach((p, i) => { if (i && p.ts - pts[i - 1].ts > gap) { segs.push(cur); cur = []; } cur.push(p); });
      segs.push(cur);
      segs.forEach(seg => {
        if (band && seg.length > 1 && seg.some(p => p.max - p.min > 0.01)) {
          const top = seg.map((p, i) => `${i ? "L" : "M"}${X(p.ts).toFixed(1)},${Y(p.max).toFixed(1)}`).join("");
          const bot = seg.slice().reverse().map(p => `L${X(p.ts).toFixed(1)},${Y(p.min).toFixed(1)}`).join("");
          mk(svg, "path", { d: top + bot + "Z", fill:c, opacity: series.length > 4 ? ".07" : ".13", stroke:"none" });
        }
        mk(svg, "path", { d: seg.map((p, i) => `${i ? "L" : "M"}${X(p.ts).toFixed(1)},${Y(p.avg).toFixed(1)}`).join(""),
          fill:"none", stroke:c, "stroke-width": series.length > 6 ? "1.3" : "1.7", "stroke-linejoin":"round" });
        if (seg.length === 1) mk(svg, "circle", { cx:X(seg[0].ts), cy:Y(seg[0].avg), r:2, fill:c });
      });
    });

    const evs = $("#histEvents").checked ? (d.events || []) : [];
    evs.forEach(e => {
      const x = X(e.ts); if (x < PAD.l || x > W - PAD.r) return;
      const col = EVENT_COLOR[e.kind] || "#98A2B2";
      mk(svg, "line", { x1:x, y1:PAD.t, x2:x, y2:Hh - PAD.b, stroke:col, "stroke-dasharray":"2 3", opacity:".7" });
      mk(svg, "path", { d:`M${x - 4},${PAD.t} L${x + 4},${PAD.t} L${x},${PAD.t + 6} Z`, fill:col });
    });

    const cross = mk(svg, "g", { opacity:"0" });
    geom = { X, Y, t0, t1, pw, W, Hh, series, evs, cross };
  }

  function nearest(pts, ts) {
    let lo = 0, hi = pts.length - 1;
    while (hi - lo > 1) { const m = (lo + hi) >> 1; if (pts[m].ts < ts) lo = m; else hi = m; }
    return Math.abs(pts[lo].ts - ts) <= Math.abs(pts[hi].ts - ts) ? pts[lo] : pts[hi];
  }
  function hover(ev) {
    if (!geom) return;
    const svg = $("#histChart"), r = svg.getBoundingClientRect();
    const x = (ev.clientX - r.left) / r.width * geom.W;
    if (x < PAD.l || x > geom.W - PAD.r) return leave();
    const ts = geom.t0 + (x - PAD.l) / geom.pw * (geom.t1 - geom.t0);
    const g = geom.cross; g.innerHTML = ""; g.setAttribute("opacity", "1");
    mk(g, "line", { x1:x, y1:PAD.t, x2:x, y2:geom.Hh - PAD.b, stroke:"#98A2B2", "stroke-dasharray":"2 4", opacity:".6" });
    const rows = geom.series.map(s => {
      const p = nearest(s.points, ts);
      if (Math.abs(p.ts - ts) > (geom.t1 - geom.t0) / 40) return null;
      mk(g, "circle", { cx:geom.X(p.ts), cy:geom.Y(p.avg), r:3.2, fill:colorOf(s.name), stroke:"#10141B", "stroke-width":"1" });
      return { s, p };
    }).filter(Boolean).sort((a, b) => b.p.avg - a.p.avg);
    const near = geom.evs.filter(e => Math.abs(geom.X(e.ts) - x) < 6);
    const tip = $("#histTip");
    const banded = $("#histBand").checked;
    tip.innerHTML = `<div class="t">${new Date(ts * 1000).toLocaleString(undefined, { month:"short", day:"numeric", hour:"2-digit", minute:"2-digit" })}</div>` +
      rows.slice(0, 12).map(({ s, p }) => `<div class="r"><i class="dot" style="background:${colorOf(s.name)};width:8px;height:8px;border-radius:50%;display:inline-block"></i>
        <span>${esc(s.name)}</span><b>${p.avg}${UNIT[H.kind]}</b>${banded && p.max - p.min > 0.05 ? `<small style="color:var(--faint)">${p.min}–${p.max}</small>` : ""}</div>`).join("") +
      (rows.length > 12 ? `<div class="r"><span>+${rows.length - 12} more</span></div>` : "") +
      near.map(e => `<div class="ev">▾ ${esc(e.message)}</div>`).join("");
    tip.hidden = !rows.length && !near.length;
    const wrap = svg.parentElement.getBoundingClientRect();
    const px = ev.clientX - wrap.left, tw = tip.offsetWidth;
    tip.style.left = `${px + 14 + tw > wrap.width ? Math.max(0, px - tw - 14) : px + 14}px`;
    tip.style.top = `${Math.max(4, Math.min(ev.clientY - wrap.top - 20, wrap.height - tip.offsetHeight - 4))}px`;
  }
  function leave() { if (geom) geom.cross.setAttribute("opacity", "0"); $("#histTip").hidden = true; }
  $("#histChart").addEventListener("pointermove", hover);
  $("#histChart").addEventListener("pointerdown", hover);
  $("#histChart").addEventListener("pointerleave", leave);
  new ResizeObserver(() => { if (H.data) draw(); }).observe($("#histChart"));

  /* ---------- stats table and events ---------- */
  function renderStats() {
    const box = $("#histStats"), d = H.data;
    const rows = (d?.series || []).filter(s => s.points.length);
    if (!rows.length) { box.innerHTML = ""; return; }
    const u = UNIT[H.kind];
    const f = v => (Math.abs(v) >= 100 ? Math.round(v) : Math.round(v * 10) / 10) + u;
    box.innerHTML = `<table><thead><tr><th>Sensor</th><th>Now</th><th>Min</th><th>Avg</th><th>Max</th><th>Max at</th></tr></thead><tbody></tbody></table>`;
    const tb = box.querySelector("tbody");
    rows.sort((a, b) => Math.max(...b.points.map(p => p.max)) - Math.max(...a.points.map(p => p.max))).forEach(s => {
      const pts = s.points;
      const mn = Math.min(...pts.map(p => p.min));
      const top = pts.reduce((a, b) => (b.max > a.max ? b : a));
      const avg = pts.reduce((a, p) => a + p.avg, 0) / pts.length;
      const tr = el("tr", H.hidden.has(s.name) ? "dim" : "");
      tr.innerHTML = `<td><span class="dot" style="background:${colorOf(s.name)}"></span>${esc(s.name)}</td>
        <td>${f(pts.at(-1).avg)}</td><td>${f(mn)}</td><td>${f(avg)}</td><td>${f(top.max)}</td>
        <td>${new Date(top.ts * 1000).toLocaleString(undefined, { month:"short", day:"numeric", hour:"2-digit", minute:"2-digit" })}</td>`;
      tr.title = "Click to hide or show this line";
      tr.onclick = () => { H.hidden.has(s.name) ? H.hidden.delete(s.name) : H.hidden.add(s.name); draw(); renderStats(); };
      tb.appendChild(tr);
    });
  }
  const EVENT_FILTERS = { all:null, handback:["watchdog", "handback"], power:["power"], link:["link"], reassert:["reassert"] };
  H.evFilter = "all";
  $("#histEvFilter").onclick = e => {
    const b = e.target.closest("button[data-ev]"); if (!b) return;
    $$("#histEvFilter button").forEach(x => x.classList.toggle("on", x === b));
    H.evFilter = b.dataset.ev; renderEvents();
  };
  function renderEvents() {
    const box = $("#histEventList"); box.innerHTML = "";
    const want = EVENT_FILTERS[H.evFilter];
    const evs = (H.data?.events || []).filter(e => !want || want.includes(e.kind));
    if (!evs.length) { box.appendChild(el("p", "empty", want ? "None of these in this window." : "No power changes, re-sends or watchdog trips in this window.")); return; }
    evs.forEach(e => box.appendChild(activityRow(e)));
  }

  /* ---------- public ---------- */
  window.HistoryView = {
    async show() {
      if (H.metaFor !== state.current) await loadMeta().catch(e => toast(e.message, "bad"));
      else loadMeta().catch(() => {});
      setRange(H.range);
      loadChart();
    },
    open(kind, name) {
      H.kind = kind; H.end = null;
      H.sel[kind] = new Set([name]); H.colors.clear(); H.hidden.clear();
      $$("#histKind button").forEach(x => x.classList.toggle("on", x.dataset.kind === kind));
      showTab("history");
    },
    serverChanged() {
      H.metaFor = null; H.meta = null; H.data = null; H.hidden.clear(); H.colors.clear();
      for (const k of Object.keys(H.sel)) H.sel[k] = null;
    },
  };
})();
