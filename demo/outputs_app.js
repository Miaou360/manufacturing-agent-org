/* Renders the sample outputs (window.OUTPUTS, built by tools/build_sample_outputs.py). No dependencies. */
(function () {
  "use strict";
  const D = window.OUTPUTS;
  const C = { hq: "#3987e5", inv: "#199e70", log: "#d95926" };
  const S = { green: "#199e70", blue: "#3987e5", orange: "#d95926" };
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const fmt = (n) => Number(n).toLocaleString("en-US");
  const md = (iso) => iso.slice(5).replace("-", "/");
  const part = (r) => `<td class="m">${esc(r.part_no)}</td><td>${esc(r.description)}</td>`;
  const stClass = { "Match": "ok", "OK": "ok", "Auto-cleared": "ok", "Covered": "ok", "Waiting": "hold", "LATE": "warn", "Held by guard": "warn",
                    "Needs a person": "bad", "FAILED": "bad" };
  const st = (s) => `<span class="st ${stClass[s] || "warn"}">${esc(s)}</span>`;
  const kpi = (v, label, sub) => `<div class="kpi"><b>${v}</b><span>${esc(label)}</span>${sub ? `<small>${esc(sub)}</small>` : ""}</div>`;
  const table = (head, rows) => `<div class="tbl-wrap"><table class="doc"><thead><tr>${head.map((h) =>
    `<th${h.startsWith("#") ? ' class="n"' : ""}>${esc(h.replace(/^#/, ""))}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table></div>`;
  const next = (html) => `<p class="next"><b>What happens next.</b> ${html}</p>`;
  const go = (id, text) => `<a href="#${id}">${esc(text)}</a>`;

  /* ---- small SVG charts ---- */
  function lineChart(series, labels, unit, yMin, yMax) {
    const W = 760, H = 230, L = 44, R = 96, T = 14, B = 30, n = labels.length;
    const x = (i) => L + (i * (W - L - R)) / (n - 1), y = (v) => T + ((yMax - v) * (H - T - B)) / (yMax - yMin);
    let g = "";
    for (let k = 0; k <= 4; k++) {
      const v = yMin + ((yMax - yMin) * k) / 4;
      g += `<line class="axis" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/><text x="${L - 8}" y="${y(v) + 4}" text-anchor="end">${Math.round(v * 10) / 10}${unit}</text>`;
    }
    labels.forEach((lab, i) => { if (i % 2 === (n - 1) % 2) g += `<text x="${x(i)}" y="${H - 8}" text-anchor="middle">${lab}</text>`; });
    series.forEach((s) => {
      const pts = s.values.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
      g += `<polyline fill="none" stroke="${s.color}" stroke-width="2" stroke-linejoin="round" points="${pts}"/>`;
      s.values.forEach((v, i) => {
        g += `<circle cx="${x(i)}" cy="${y(v)}" r="${i === n - 1 ? 4.5 : 3}" fill="${s.color}" stroke="#111a2c" stroke-width="2"><title>${s.name} · ${labels[i]}: ${v}${unit}</title></circle>`;
      });
      const last = s.values[n - 1];
      g += `<text class="lab" x="${x(n - 1) + 10}" y="${y(last) + 4}">${last}${unit}</text><text x="${x(n - 1) + 10}" y="${y(last) + 19}">${s.short || s.name}</text>`;
    });
    const legend = series.length > 1 ? `<div class="legend">${series.map((s) => `<span><i style="background:${s.color}"></i>${esc(s.name)}</span>`).join("")}</div>` : "";
    return `<div class="chart">${legend}<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(series.map((s) => s.name).join(" and "))} by day">${g}</svg></div>`;
  }
  function stackChart(rows, keys) {
    const W = 760, H = 240, L = 40, R = 12, T = 12, B = 30, n = rows.length;
    const max = Math.ceil(Math.max(...rows.map((r) => keys.reduce((a, k) => a + r[k.key], 0))) / 50) * 50;
    const bw = Math.min(52, ((W - L - R) / n) * 0.62), y = (v) => T + ((max - v) * (H - T - B)) / max;
    let g = "";
    for (let v = 0; v <= max; v += 50) { g += `<line class="axis" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/><text x="${L - 8}" y="${y(v) + 4}" text-anchor="end">${v}</text>`; }
    rows.forEach((r, i) => {
      const cx = L + ((i + 0.5) * (W - L - R)) / n; let acc = 0;
      keys.forEach((k, ki) => {
        const v = r[k.key], y1 = y(acc + v), h = y(acc) - y1 - (ki < keys.length - 1 ? 2 : 0);
        g += `<rect x="${cx - bw / 2}" y="${y1}" width="${bw}" height="${Math.max(0, h)}" rx="${ki === keys.length - 1 ? 4 : 0}" fill="${k.color}"><title>Week of ${md(r.week)} · ${k.name}: ${v}</title></rect>`;
        acc += v;
      });
      g += `<text x="${cx}" y="${y(acc) - 5}" text-anchor="middle" class="lab">${acc}</text><text x="${cx}" y="${H - 8}" text-anchor="middle">${md(r.week)}</text>`;
    });
    return `<div class="chart"><div class="legend">${keys.map((k) => `<span><i style="background:${k.color}"></i>${esc(k.name)}</span>`).join("")}</div>` +
      `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Backlog items per week by how they were closed">${g}</svg></div>`;
  }

  /* ---- the outputs ---- */
  const head = (o, lead) => `<div class="out-head"><div><h2>${esc(o.title)}</h2><p>${lead}</p></div>` +
    `<span class="unit-tag" style="--c:${C[o.dept]}">${esc(o.unit)}</span></div>` +
    `<div class="facts"><div class="fact"><h4>When</h4><p>${esc(o.when)}</p></div><div class="fact"><h4>Who receives it</h4><p>${esc(o.who)}</p></div>` +
    `<div class="fact"><h4>Decision it supports</h4><p>${esc(o.decision)}</p></div></div>`;

  const OUT = [
    { id: "morning", time: "07:35", dept: "hq", unit: "00_HQ · Task Manager", title: "Morning operations summary", tab: "Did every slot run?",
      when: "Daily 07:35, before the day shift meeting", who: "Operations owner", decision: "Is anything broken that needs attention before the shift starts?",
      render(o) {
        const m = D.morning;
        return head(o, "One mail that says whether the night's work happened. A slot that did not run, ran late or failed is raised to the top; a quiet morning is one line.") +
          `<div class="kpis">${kpi(m.ok, "slots on time")}${kpi(m.late, "late", "finished after its window")}${kpi(m.failed, "failed")}</div>` +
          table(["Slot", "Task", "Status", "Result"], m.slots.map((s) => `<tr><td class="m">${s.time}</td><td>${esc(s.task)}</td><td>${st(s.status)}</td><td>${esc(s.note)}</td></tr>`)) +
          next(`Each line is a summary of an output on this page: ${go("count", "the count list")}, ${go("backlog", "the backlog report")}, ${go("recon", "the reconciliation")}.`);
      } },
    { id: "count", time: "06:50", dept: "inv", unit: "00_JOB · Stock Accuracy", title: "Daily cycle count list", tab: "Which bins to count today",
      when: "Daily 06:50 (weekly long list on Saturday)", who: "Count team (prints the sheet), inventory lead",
      decision: "Where do 28 counts find the most errors? The list is ranked by risk; the count result comes back the same day.",
      render(o) {
        const c = D.count, lane = Object.fromEntries(c.lanes.map((l) => [l.lane, l]));
        const avg = (k) => Math.round(c.trend.reduce((a, t) => a + t[k], 0) / c.trend.length);
        let rows = [], cur = "";
        const laneNote = { "Model": "ranked by risk score", "Follow-up": "recount after an adjustment", "Explore": "random bins: the baseline the model has to beat" };
        c.rows.forEach((r) => {
          if (r.lane !== cur) { cur = r.lane; rows.push(`<tr class="grp"><td colspan="9">${esc(cur)} lane — ${laneNote[cur]}</td></tr>`); }
          const diff = r.counted_qty - r.system_qty;
          rows.push(`<tr><td class="n">${r.no}</td><td class="m">${esc(r.bin)}<small>${esc(r.facility)}</small></td>${part(r)}<td class="n">${fmt(r.system_qty)}</td>` +
            `<td class="n">${r.lane === "Model" ? r.score : "—"}</td><td><ul class="reasons">${r.reasons.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></td>` +
            `<td class="n">${fmt(r.counted_qty)}${diff ? `<small>${diff > 0 ? "+" : ""}${fmt(diff)}</small>` : ""}</td><td>${st(r.result)}</td></tr>`);
        });
        return head(o, `Out of ${fmt(c.bins_total)} occupied rack locations, the job picks the ${c.rows.length} most worth counting. Each bin carries the reasons it was picked, so the counter knows what to look for.`) +
          `<div class="kpis">${kpi(c.rows.length, "bins on today's list")}${kpi(lane.Model.hit_rate + "%", "errors found, model lane", `${lane.Model.mismatches} of ${lane.Model.bins} bins`)}` +
          `${kpi(lane.Explore.hit_rate + "%", "errors found, random lane", `${lane.Explore.mismatches} of ${lane.Explore.bins} bins`)}${kpi(avg("model") + "% vs " + avg("explore") + "%", "model vs random", `average of the last ${c.trend.length} counts`)}</div>` +
          `<h3 class="sub">Share of counted bins with an error <small>model-ranked bins against random bins</small></h3>` +
          lineChart([{ name: "Model lane", short: "model", color: S.blue, values: c.trend.map((t) => t.model) }, { name: "Random lane (baseline)", short: "random", color: S.orange, values: c.trend.map((t) => t.explore) }],
            c.trend.map((t) => md(t.date)), "%", 0, 60) +
          `<h3 class="sub">Count sheet with results <small>system quantity is hidden on the printed sheet (blind count)</small></h3>` +
          table(["#No", "Bin", "Part No", "Description", "#System qty", "#Risk", "Why this bin", "#Counted", "Result"], rows) +
          next(`Mismatches are recounted, then adjusted by a person; adjusted bins return in tomorrow's follow-up lane. Results feed the next ranking. ` +
            `Bins are the rack locations of the <a href="https://miaou360.github.io/3d-warehouse-visualizer/">3D Warehouse Visualizer</a> (N05-04-5 = North, rack 05, bay 04, level 5).`);
      } },
    { id: "interface", time: "every 30 min", dept: "inv", unit: "01_JOB · GR / GI Error", title: "Interface error resolution", tab: "Receipts and issues that failed to post",
      when: "Every 30 minutes, 06:00–22:00", who: "Inventory planner (only the rows that need a person)",
      decision: "Which failed postings can the agent fix safely, and which must a person look at?",
      render(o) {
        const f = D.interface, s = f.summary;
        return head(o, "When the warehouse system issues material but the ERP posting fails, the error sits in a queue and stock goes out of step. The job reads the queue, finds the cause, fixes what is safe and explains the rest.") +
          `<div class="kpis">${kpi(f.rows.length, "errors open this morning")}${kpi(s["Auto-cleared"], "cleared by the agent")}${kpi(s["Waiting"] + s["Held by guard"], "waiting or held", "re-checked on the next run")}${kpi(s["Needs a person"], "sent to a person", "with the cause attached")}</div>` +
          table(["Part No", "Description", "Movement", "#Qty", "First seen", "Cause", "What the agent did", "Status"], f.rows.map((r) =>
            `<tr>${part(r)}<td class="m">${r.movement}</td><td class="n">${fmt(r.qty)}</td><td class="m">${r.first_seen}</td><td>${esc(r.cause)}</td><td>${esc(r.action)}</td><td>${st(r.status)}</td></tr>`)) +
          next(`A part with an open error gets extra risk on ${go("count", "the count list")}. A row held by the accuracy guard is never forced: the transfer is posted only if plant stock accuracy does not go down.`);
      } },
    { id: "backlog", time: "08:15", dept: "inv", unit: "02_JOB · Backlog Clear", title: "Production backlog resolution report", tab: "Consumption postings that failed",
      when: "Runs 06:20, 08:15, 08:30 — one report after the last run", who: "Production control, inventory team, plant management",
      decision: "What is still blocking yesterday's production from being booked, and who has to act on each line?",
      render(o) {
        const b = D.backlog;
        return head(o, "Finished units are booked, but the component consumption fails when the ERP shows no stock at the line. The job moves stock that really exists to where the posting needs it, re-runs the postings and reports what is left and why.") +
          `<div class="kpis">${kpi(b.start, "items at 06:20")}${kpi(b.cleared.length, "cleared by the agent", fmt(b.cleared_qty) + " EA in " + b.cleared.length + " transfers")}${kpi(b.remaining.length, "left for people", "each with a cause")}${kpi(Math.round((100 * b.cleared.length) / b.start) + "%", "cleared without a person")}</div>` +
          `<h3 class="sub">Left in the backlog <small>the part of the report people act on</small></h3>` +
          table(["Part No", "Description", "#Missing qty", "#Stock in North", "Why it was not cleared", "Next step"], b.remaining.map((r) =>
            `<tr>${part(r)}<td class="n">${fmt(r.qty)}</td><td class="n">${fmt(r.north_stock)}</td><td>${esc(r.cause)}</td><td>${esc(r.next_step)}</td></tr>`)) +
          `<h3 class="sub">Transfers posted by the agent <small>${Object.entries(b.by_run).map(([k, v]) => `${k}: ${v}`).join(" · ")}</small></h3>` +
          table(["Run", "Part No", "Description", "#Qty", "From → To", "Document (sample no.)", "#Accuracy change"], b.cleared.map((r) =>
            `<tr><td class="m">${r.run}</td>${part(r)}<td class="n">${fmt(r.qty)}</td><td class="m">${r.from} → ${r.to}</td><td class="m">${r.document}</td><td class="n">+${r.delta.toFixed(2)} pt</td></tr>`)) +
          next(`Every transfer passes three guards first: the difference must persist across snapshots, no receipt or issue for the part may be in flight, and plant accuracy must not drop. ` +
            `Lines held by a guard go to ${go("count", "the count list")}; the week's pattern is in ${go("weekly", "the weekly analysis")}.`);
      } },
    { id: "recon", time: "07:30", dept: "inv", unit: "04_JOB · System Reconciliation", title: "ERP – WMS stock reconciliation", tab: "Do the two systems agree?",
      when: "Snapshots 05:45, report 07:30", who: "Inventory lead, finance (month end)", decision: "Is the stock accuracy figure moving, and which differences are real rather than timing?",
      render(o) {
        const r = D.recon;
        return head(o, "The ERP and the warehouse system each keep their own stock. Both are read at the same minute and compared part by part. A difference seen once is usually a posting in progress; one that survives several snapshots is a real error.") +
          `<div class="kpis">${kpi(r.accuracy + "%", "stock accuracy today")}${kpi(fmt(r.matched), "parts in agreement", "of " + fmt(r.parts))}${kpi(r.parts - r.matched, "parts with a difference")}${kpi(r.persistent, "persistent", "seen in 3 or more snapshots")}</div>` +
          `<h3 class="sub">Stock accuracy <small>average over parts of 1 − |ERP − WMS| ÷ the larger of the two</small></h3>` +
          lineChart([{ name: "Stock accuracy", short: "accuracy", color: S.green, values: r.trend.map((t) => t.accuracy) }], r.trend.map((t) => md(t.date)), "%",
            Math.floor(Math.min(...r.trend.map((t) => t.accuracy)) - 0.5), 100) +
          `<h3 class="sub">Largest differences</h3>` +
          table(["Part No", "Description", "#ERP", "#WMS", "#Difference", "#Snapshots", "Verdict"], r.variances.map((v) =>
            `<tr>${part(v)}<td class="n">${fmt(v.erp)}</td><td class="n">${fmt(v.wms)}</td><td class="n">${v.diff > 0 ? "+" : ""}${fmt(v.diff)}</td><td class="n">${v.snapshots}</td><td><span class="st ${v.snapshots >= 3 ? "bad" : v.snapshots === 2 ? "warn" : "hold"}">${esc(v.verdict)}</span></td></tr>`)) +
          next(`Persistent differences are added to ${go("count", "the count list")}. No job may post a correction while a difference is still classed as timing.`);
      } },
    { id: "srm", time: "06:40", dept: "log", unit: "00_JOB · Supplier Monitoring", title: "Short-term supply monitoring", tab: "Will the line run short before the next truck?",
      when: "06:40 and 13:40, Monday to Saturday", who: "Logistics planner; one sheet per supplier with a shortfall",
      decision: "Which trucks need more on them today, before the line stops?",
      render(o) {
        const s = D.srm;
        return head(o, "The supplier portal shows what each supplier plans to ship in two-hour windows. The job compares that plan with stock on hand and the line's need, and writes a truck plan for every supplier that would leave the line short.") +
          `<div class="kpis">${kpi(s.monitored, "parts watched in 2-hour windows")}${kpi(s.short, "would run short")}${kpi(s.suppliers.length, "suppliers to contact", s.suppliers.join(", "))}</div>` +
          table(["Window", "Part No", "Description", "Supplier", "#Line need", "#On hand", "#On the truck", "#Short", "Proposed change"], s.rows.map((r) =>
            `<tr><td class="m">${r.window}</td>${part(r)}<td>${esc(r.supplier)}</td><td class="n">${fmt(r.need_2h)}</td><td class="n">${fmt(r.on_hand)}</td><td class="n">${fmt(r.on_truck)}</td><td class="n">${r.short ? fmt(r.short) : "—"}</td><td>${r.short ? `<span class="st bad">${esc(r.action)}</span>` : st("Covered")}</td></tr>`)) +
          next(`Supplier sheets are drafts: they go to the planner, who sends them. The afternoon run checks whether the morning's changes were loaded. What is in the racks is what the <a href="https://miaou360.github.io/sequencing-pulling-list/">pulling list</a> draws from.`);
      } },
    { id: "weekly", time: "Fri 08:30", dept: "inv", unit: "03_JOB · Backlog Analysis", title: "Weekly backlog analysis", tab: "Why does the backlog keep coming back?",
      when: "Friday 08:30", who: "Plant management, production control, purchasing",
      decision: "Which root cause should the plant fix so the items stop appearing, instead of clearing them every morning?",
      render(o) {
        const w = D.weekly, last = w.weeks[w.weeks.length - 1], first = w.weeks[0];
        const share = (x) => Math.round((100 * x.auto_cleared) / x.opened);
        return head(o, "Clearing the backlog every morning treats the symptom. Once a week the job breaks the items down by cause and by part, so that the recurring ones can be fixed at the source.") +
          `<div class="kpis">${kpi(last.opened, "items last week", "down from " + first.opened + " eight weeks ago")}${kpi(share(last) + "%", "closed by the agent", "up from " + share(first) + "%")}${kpi(last.carried, "carried into the next week")}</div>` +
          `<h3 class="sub">Backlog items per week, by how they were closed</h3>` +
          stackChart(w.weeks, [{ key: "auto_cleared", name: "Closed by the agent", color: S.green }, { key: "by_person", name: "Closed by a person", color: S.blue }, { key: "carried", name: "Carried over", color: S.orange }]) +
          `<h3 class="sub">Root causes, last four weeks <small>share of items</small></h3>` +
          w.root_causes.map((c) => `<div class="bar-row"><span>${esc(c.cause)}</span><div class="track"><i style="width:${(c.share / w.root_causes[0].share) * 100}%"></i></div><span class="v">${c.share}%</span></div>`).join("") +
          `<h3 class="sub">Parts that came back most often <small>days in the backlog, last week</small></h3>` +
          table(["Part No", "Description", "#Days", "Main cause"], w.repeat_parts.map((r) => `<tr>${part(r)}<td class="n">${r.days} of 6</td><td>${esc(r.cause)}</td></tr>`)) +
          next(`The top cause is a storage-location rule, not a counting problem: a standing transfer rule for those parts removes the items for good. That proposal goes to a person; the agents do not change master data.`);
      } },
  ];

  const byId = Object.fromEntries(OUT.map((o) => [o.id, o]));
  function show(id) {
    const o = byId[id] || OUT[0];
    $("out-main").innerHTML = o.render(o);
    document.querySelectorAll(".out-tab").forEach((b) => b.setAttribute("aria-current", String(b.dataset.id === o.id)));
    document.title = `${o.title} — Manufacturing Agent Organization`;
  }
  const ORDER = ["count", "backlog", "interface", "recon", "srm", "weekly", "morning"];
  $("out-nav").innerHTML = ORDER.map((id) => byId[id]).map((o) => `<button class="out-tab" type="button" data-id="${o.id}" style="--c:${C[o.dept]}"><time>${o.time}</time><b>${esc(o.title)}</b><small>${esc(o.tab)}</small></button>`).join("");
  $("out-nav").addEventListener("click", (e) => { const b = e.target.closest(".out-tab"); if (b) location.hash = b.dataset.id; });
  window.addEventListener("hashchange", () => { show(location.hash.slice(1)); window.scrollTo({ top: 0 }); });
  $("notice").innerHTML = `<b>Sample outputs, synthetic data.</b> No real part, supplier, quantity or result. Day shown: ${D.meta.date}.`;
  $("asof").textContent = `Sample day ${D.meta.date} · seed ${D.meta.seed}`;
  show(location.hash.slice(1) || "count");
})();
