/* Live page of the organization: operations floor (office.js), 24-hour schedule, data flow, change loop. No dependencies. */
(function () {
  "use strict";
  const O = window.ORG;
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; };
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const mins = (t) => { const [h, m] = t.split(":").map(Number); return h * 60 + m; };
  const hhmm = (m) => `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
  const unitById = Object.fromEntries(O.units.map((u) => [u.id, u]));
  const color = (u) => O.depts[u.dept].color;
  let selected = "acc";

  // every task as concrete start minutes of the day
  function starts(task) {
    if (task.daemon) { // hourly on the hour, 24h
      const out = [];
      for (let m = 0; m < 1440; m += 60) out.push(m);
      return out;
    }
    if (task.every) { const out = [], lim = task.until || 1439; for (let m = mins(task.t); m <= lim; m += task.every) out.push(m); return out; }
    return [mins(task.t)];
  }
  const RUN = 12; // minutes a task is shown as running

  // ---------- stats
  O.stats.forEach((s) => $("stats").appendChild(el("div", "stat", `<b>${esc(s.value)}</b><span>${esc(s.label)}</span><small>${esc(s.sub)}</small>`)));
  $("asof").textContent = `As of ${O.asOf}.`;

  // ---------- organization
  function unitButton(u, extra) {
    const b = el("button", "unit" + (extra ? " " + extra : ""), `<small>${esc(u.code)}</small>${esc(u.name)}` + (extra === "hq" ? `<span class="one">${esc(u.one)}</span>` : ""));
    b.type = "button"; b.style.setProperty("--c", color(u)); b.dataset.unit = u.id;
    b.setAttribute("aria-pressed", String(u.id === selected));
    b.addEventListener("click", () => select(u.id));
    return b;
  }
  function renderOrg() { window.renderOffice($("org"), O, select); }
  const SAMPLE = { hq: "morning", acc: "count", gi: "interface", bkl: "backlog", ana: "weekly", rec: "recon", srm: "srm", dlv: "delivery" };
  function renderDetail() {
    const u = unitById[selected];
    const tasks = O.tasks.filter((t) => t.unit === u.id).sort((a, b) => mins(a.t) - mins(b.t));
    const list = (a) => a.map((x) => `<span class="chip">${esc(x)}</span>`).join("");
    $("detail").innerHTML =
      `<div><h3>${esc(u.name)}</h3><p class="lead">${esc(u.one)}</p><ul>${u.does.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>` +
      `<div><h4>Reads</h4><div class="chips">${list(u.inputs)}</div><h4>Produces</h4><div class="chips">${list(u.outputs)}</div>${SAMPLE[u.id] ? `<a class="sample" href="outputs.html#${SAMPLE[u.id]}">See a sample output →</a>` : ""}</div>` +
      `<div><h4>Guards</h4><div class="chips">${u.guards.map((x) => `<span class="chip guard">${esc(x)}</span>`).join("")}</div>` +
      `<h4>Schedule</h4><div class="sched">${tasks.map((t) => `<b>${t.daemon ? "24 h" : t.every ? (t.every < 60 ? t.every + " min" : (t.every / 60) + " h") : esc(t.t)}</b><span>${esc(t.name)}</span><em>${esc(t.days)}</em>`).join("") || (u.planned ? "<span>Planned — next phase after 00_JOB & 02_JOB</span>" : "<span>Runs inside other jobs</span>")}</div></div>`;
  }
  function select(id) {
    selected = id;
    document.querySelectorAll(".unit").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.unit === id)));
    renderDetail(); drawTimeline();
  }

  // ---------- timeline
  const NS = "http://www.w3.org/2000/svg";
  const svg = (tag, attrs, text) => { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); if (text != null) e.textContent = text; return e; };
  const LANE_H = 30, LEFT = 190, TOP = 26, RIGHT = 16;
  const lanes = O.units.filter((u) => O.tasks.some((t) => t.unit === u.id));
  let now = Number($("clock").value);
  function drawTimeline() {
    const root = $("timeline");
    const W = Math.max(860, root.parentNode.clientWidth);
    const H = TOP + lanes.length * LANE_H + 8;
    const x = (m) => LEFT + (m / 1440) * (W - LEFT - RIGHT);
    root.setAttribute("viewBox", `0 0 ${W} ${H}`); root.setAttribute("height", H); root.textContent = "";
    root.appendChild(svg("rect", { class: "shift", x: x(360), y: TOP - 4, width: x(1080) - x(360), height: H - TOP }));
    for (let h = 0; h <= 24; h += 2) {
      root.appendChild(svg("line", { class: "grid", x1: x(h * 60), x2: x(h * 60), y1: TOP - 4, y2: H - 4 }));
      if (h < 24) root.appendChild(svg("text", { class: "hour", x: x(h * 60) + 3, y: 12 }, String(h).padStart(2, "0")));
    }
    lanes.forEach((u, i) => {
      const y = TOP + i * LANE_H + LANE_H / 2;
      const dim = selected !== u.id && selected !== "hq" ? 0.55 : 1;
      root.appendChild(svg("rect", { x: 0, y: y - 9, width: 4, height: 18, rx: 2, fill: color(u) }));
      root.appendChild(svg("text", { class: "lane-label", x: 12, y: y + 4, opacity: selected === u.id ? 1 : 0.85, "font-weight": selected === u.id ? 600 : 400 }, u.name));
      root.appendChild(svg("line", { class: "grid", x1: LEFT, x2: W - RIGHT, y1: y, y2: y }));
      O.tasks.filter((t) => t.unit === u.id).forEach((t) => {
        starts(t).forEach((m) => {
          const small = t.daemon || t.every;
          const active = now >= m && now < m + RUN;
          const g = svg(small ? "rect" : "circle", small
            ? { x: x(m) - 1.5, y: y - 6, width: 3, height: 12, rx: 1.5 }
            : { cx: x(m), cy: y, r: active ? 7 : 5 });
          g.setAttribute("class", "mark"); g.setAttribute("fill", active ? "#f4c152" : color(u)); g.setAttribute("opacity", active ? 1 : dim);
          if (!small) { g.setAttribute("stroke", "#111a2c"); g.setAttribute("stroke-width", 2); g.setAttribute("tabindex", 0); }
          g.dataset.tip = `${hhmm(m)} · ${t.name}|${u.name} · ${t.days}`;
          g.addEventListener("click", () => select(u.id));
          root.appendChild(g);
        });
      });
    });
    root.appendChild(svg("line", { class: "playhead", x1: x(now), x2: x(now), y1: 16, y2: H - 4 }));
  }
  function renderNow() {
    const running = [];
    O.tasks.forEach((t) => starts(t).forEach((m) => { if (now >= m && now < m + RUN) running.push([t, m]); }));
    const next = O.tasks.filter((t) => !t.daemon && !t.every).map((t) => [t, mins(t.t)]).filter(([, m]) => m > now).sort((a, b) => a[1] - b[1])[0];
    const ids = new Set(running.map(([t]) => t.unit));
    const nextOf = (id) => { const c = []; O.tasks.filter((t) => t.unit === id).forEach((t) => starts(t).forEach((m) => { if (m > now) c.push(m); })); return c.length ? hhmm(Math.min(...c)) : null; };
    window.updateOffice(now, running.map(([t]) => ({ unit: t.unit, name: t.name })), nextOf);
    $("clock-out2").textContent = hhmm(now);
    $("now").innerHTML = (running.length
      ? "Running: " + running.map(([t, m]) => `<span class="chip"><i style="background:${color(unitById[t.unit])}"></i><b>${esc(t.name)}</b> · ${esc(unitById[t.unit].name)} · ${hhmm(m)}</span>`).join("")
      : "Nothing running.") + (next ? ` <span>Next: ${hhmm(next[1])} ${esc(next[0].name)}</span>` : "");
    $("clock-out").textContent = hhmm(now);
  }
  function setNow(m) { now = ((m % 1440) + 1440) % 1440; $("clock").value = now; drawTimeline(); renderNow(); }
  $("clock").addEventListener("input", (e) => { stop(); setNow(Number(e.target.value)); });
  let timer = null;
  const playLabel = (txt) => { $("play").textContent = txt; $("play2").textContent = txt; };
  function stop() { if (timer) { clearInterval(timer); timer = null; playLabel("▶ Play the day"); } }
  let played = false;
  function togglePlay() {
    if (timer) return stop();
    playLabel("❚❚ Pause");
    if (!played || now > 1380) setNow(120);          // first press: start before the night collection
    played = true;
    timer = setInterval(() => { if (now >= 1435) { stop(); return; } setNow(now + 5); }, 110);
  }
  $("play").addEventListener("click", togglePlay);
  $("play2").addEventListener("click", togglePlay);

  // tooltips (hover + keyboard focus)
  const tip = $("tip");
  function showTip(target, cx, cy) {
    const [a, b] = target.dataset.tip.split("|");
    tip.innerHTML = `<b>${esc(a)}</b><span>${esc(b)}</span>`; tip.hidden = false;
    const w = tip.offsetWidth; tip.style.left = Math.min(window.innerWidth - w - 8, cx + 12) + "px"; tip.style.top = cy + 14 + "px";
  }
  $("timeline").addEventListener("mousemove", (e) => { if (e.target.dataset && e.target.dataset.tip) showTip(e.target, e.clientX, e.clientY); else tip.hidden = true; });
  $("timeline").addEventListener("mouseleave", () => { tip.hidden = true; });
  $("timeline").addEventListener("focusin", (e) => { if (e.target.dataset.tip) { const r = e.target.getBoundingClientRect(); showTip(e.target, r.left, r.bottom); } });
  $("timeline").addEventListener("focusout", () => { tip.hidden = true; });
  $("timeline").addEventListener("keydown", (e) => { if ((e.key === "Enter" || e.key === " ") && e.target.dataset.tip) { e.preventDefault(); e.target.dispatchEvent(new MouseEvent("click")); } });

  // ---------- flow + loop
  const flow = $("flow");
  flow.appendChild(el("div", "flow-col", `<h3>Sources</h3>${O.flow.sources.map((s) => `<div>${esc(s)}</div>`).join("")}`));
  O.flow.stages.forEach((s) => flow.appendChild(el("div", "flow-col" + (s.name === "Guard" ? " guard" : ""), `<h3>${esc(s.name)}</h3>${s.items.map((i) => `<div>${esc(i)}</div>`).join("")}`)));
  O.loop.forEach((s) => $("loop").appendChild(el("li", s.who === "Owner" ? "owner" : "", `<b>${esc(s.who)}</b><span>${esc(s.what)}</span><small>${esc(s.note)}</small>`)));

  renderOrg(); select(selected); renderNow();
  let rz; window.addEventListener("resize", () => { clearTimeout(rz); rz = setTimeout(drawTimeline, 120); });
})();
