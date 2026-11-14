/* Renders the real-format sample outputs (window.OUTPUTS + demo/reports/*.html). No dependencies. */
(function () {
  "use strict";
  const D = window.OUTPUTS;
  const C = { hq: "#3987e5", inv: "#199e70", log: "#d95926" };
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const next = (html) => `<p class="next"><b>What happens next.</b> ${html}</p>`;
  const go = (id, text) => `<a href="#${id}">${esc(text)}</a>`;

  const head = (o, lead) =>
    `<div class="out-head"><div><h2>${esc(o.title)}</h2><p>${lead}</p></div>` +
    `<span class="unit-tag" style="--c:${C[o.dept]}">${esc(o.unit)}</span></div>` +
    `<div class="facts"><div class="fact"><h4>When</h4><p>${esc(o.when)}</p></div><div class="fact"><h4>Who receives it</h4><p>${esc(o.who)}</p></div>` +
    `<div class="fact"><h4>Decision it supports</h4><p>${esc(o.decision)}</p></div></div>`;

  const OUT = [
    {
      id: "count",
      time: "06:50",
      dept: "inv",
      unit: "00_JOB · Stock Accuracy",
      title: "Daily cycle count list",
      tab: "Which bins to count today",
      when: "Daily 06:50 (weekly long list on Saturday)",
      who: "Count team (prints the sheet), inventory lead",
      decision: "Where do 30 counts find the most errors? The list is ranked by risk; the count result comes back the same day.",
      lead: "Out of occupied rack and staging locations across both facilities, the job picks the 30 daily bins (and 52 Saturday high-rack bins) most worth counting. Each bin carries the exact risk reasons and blind count sheet layout.",
      nextHtml:
        `Mismatches are recounted, then adjusted by a person; adjusted bins return in tomorrow's follow-up lane. Weekly hit-rate tracking is in ${go("hitrate", "the Saturday hit-rate brief")}. ` +
        `Bins are the rack locations of the <a href="https://miaou360.github.io/3d-warehouse-visualizer/">3D Warehouse Visualizer</a> (N05-04-4 = North, rack 05, bay 04, level 4).`,
    },
    {
      id: "hitrate",
      time: "Sat 09:40",
      dept: "inv",
      unit: "00_JOB · Stock Accuracy",
      title: "Weekly floor brief & cycle count hit-rate",
      tab: "Does risk ranking beat random counting?",
      when: "Saturday 09:40, after the week's count sheets are reconciled",
      who: "Inventory lead, warehouse floor supervisors, operations owner",
      decision: "Which zones have repeat short-box concentration, and is the risk-ranked lane finding more real errors than random baseline counts?",
      lead: "Compares model-ranked bins against random control bins across 13 count runs, separates timing noise from confirmed floor discrepancies, and maps zone-level discrepancy concentration.",
      nextHtml:
        `High-miss zones and repeat bins feed Monday's ${go("count", "daily cycle count list")} weights and ${go("weekly", "the weekly backlog root-cause report")}.`,
    },
    {
      id: "backlog",
      time: "08:30",
      dept: "inv",
      unit: "02_JOB · Backlog Clear",
      title: "Production backlog (COGI) resolution report",
      tab: "Consumption postings that failed",
      when: "Runs 06:20, 08:15, 08:30 — cumulative report and workbook after 2-pass MaxClear",
      who: "Production control, inventory team, plant management",
      decision: "What is still blocking yesterday's production from being booked, and who has to act on each remaining line?",
      lead: "Finished units are booked, but component consumption fails when the ERP shows no stock at the line. The job posts safe storage-location transfers across morning cycles, re-runs COGI, and classifies every remaining line by root cause.",
      nextHtml:
        `Every transfer passes three guards first: persistence across snapshots, no open receipt/issue error on the part (${go("interface", "01_JOB interface check")}), and non-decreasing plant accuracy. ` +
        `Recurring parts are decomposed every Friday in ${go("weekly", "the weekly 3-question visual report")}.`,
    },
    {
      id: "interface",
      time: "07:30",
      dept: "inv",
      unit: "01_JOB · GR / GI Error",
      title: "Daily GI error cumulative report",
      tab: "Receipts and issues that failed to post",
      when: "Continuous daemon every 30 min (06:00–22:00); daily cumulative report at 07:30",
      who: "Inventory planner (only the rows held by guards or needing deletion/GR review)",
      decision: "Which failed interface postings did the agent clear overnight, which are held by safety/cooldown guards, and which stale portal rows are ready for deletion?",
      lead: "Summarizes 24-hour automated MB1B 311 transfers, 4-hour arrival-guard releases (D109), 24-hour repeat-failure cooldowns (D110), active remaining errors, and verified portal deletion drafts (D107).",
      nextHtml:
        `Parts with active interface errors block ${go("backlog", "02_JOB backlog transfers")} automatically so stock is never double-deducted, and feed extra risk points into ${go("count", "the cycle count list")}.`,
    },
    {
      id: "weekly",
      time: "Fri 08:30",
      dept: "inv",
      unit: "03_JOB · Backlog Analysis",
      title: "Weekly backlog 3-question visual report",
      tab: "Why does the backlog keep coming back?",
      when: "Friday 08:30 (5-page landscape visual report + cover mail + 3-sheet Excel)",
      who: "Plant management, production control, purchasing, warehouse leadership",
      decision: "For every recurring backlog part: was material left unscanned between warehouse and line, was warehouse stock truly zero / unposted at receiving, or did 4-week line usage exceed supply?",
      lead: "Clearing the backlog every morning treats the symptom. Every Friday this 5-page visual report cross-examines 4-week supply vs usage, floor cycle counts, and GR/GI history to assign each part to its true operational owner.",
      nextHtml:
        `Unscanned-transfer repeat bins go to floor supervision and ${go("count", "cycle counting")}; unposted receipts go to receiving; persistent 4-week usage gaps go to production engineering for BOM / pack-size review.`,
    },
    {
      id: "srm",
      time: "06:40",
      dept: "log",
      unit: "00_JOB · Supplier Monitoring",
      title: "Short-term supply monitoring & truck plan",
      tab: "Will the line run short before the next truck?",
      when: "06:40 (today + 2 days) and 13:40 (tomorrow + 2 days), Monday to Saturday",
      who: "Logistics planner; one truck-loading workbook drafted per supplier",
      decision: "Which supplier trucks need earlier loading or additional boxes today before a 2-hour production window runs negative?",
      lead: "Compares 2-hour production requirements and 06:00 stock against SRM delivery windows, moves orders onto fixed supplier truck slots (10-12 / 12-14 / 16-18 / 22-24 + Pallet Truck), and highlights changed or additional quantities.",
      nextHtml:
        `Supplier workbooks and mails are saved as drafts for planner review before sending. Tomorrow-onward shortages show 'Recheck 13:40' until afternoon orders post. Rack stock ties directly to the <a href="https://miaou360.github.io/sequencing-pulling-list/">sequencing pulling list</a>.`,
    },
    {
      id: "recon",
      time: "07:30",
      dept: "inv",
      unit: "04_JOB · System Reconciliation",
      title: "ERP – WMS stock reconciliation",
      tab: "Do the two systems agree?",
      when: "Paired snapshots at 05:45, executive report and 4-sheet workbook at 07:30",
      who: "Inventory lead, finance (month end)",
      decision: "Is official stock accuracy improving day over day, and which part-level differences are persistent book mismatches rather than in-flight timing?",
      lead: "Reads ERP and WMS inventory at the morning boundary, computes official quantity-weighted accuracy and strict match rate, and tracks multi-day chronic variances.",
      nextHtml:
        `Persistent variances feed ${go("count", "00_JOB cycle counting")} and guard ${go("backlog", "02_JOB")} / ${go("interface", "01_JOB")} against accuracy-degrading transfers.`,
    },
    {
      id: "morning",
      time: "07:35",
      dept: "hq",
      unit: "00_HQ · Task Manager",
      title: "Morning operations summary",
      tab: "Did every slot run?",
      when: "Daily 07:35, before the day shift meeting",
      who: "Operations owner",
      decision: "Did all 21 scheduled pipelines, ledger backups, and Task Scheduler registrations complete cleanly before the shift starts?",
      lead: "One HQ health brief auditing every night and early-morning pipeline slot, SQLite ledger backup integrity, disk headroom, and model-vs-random cycle count lift.",
      nextHtml:
        `Each row summarizes one live pipeline on this page: ${go("count", "00_JOB cycle count")}, ${go("interface", "01_JOB GI daemon")}, ${go("backlog", "02_JOB backlog clear")}, ${go("recon", "04_JOB reconciliation")}, and ${go("srm", "SRM truck planning")}.`,
    },
  ];

  const byId = Object.fromEntries(OUT.map((o) => [o.id, o]));
  const activeViewIdx = {};

  function syncFrame() {
    const f = $("report-frame");
    if (!f) return;
    let doc;
    try {
      doc = f.contentDocument;
    } catch (_e) {
      return;
    }
    if (!doc || !doc.body || !doc.documentElement) return;
    const cw = f.clientWidth || 800;
    doc.documentElement.dataset.narrow = cw < 580 ? "1" : "0";

    const slots = doc.querySelectorAll(".slide-slot");
    if (slots.length) {
      const avail = Math.max(240, cw - 16);
      const s = Math.min(1, avail / 1123);
      const padLeft = Math.max(0, Math.floor((avail - 1123 * s) / 2));
      slots.forEach((slot) => {
        const slide = slot.querySelector(".slide");
        if (!slide) return;
        slot.style.display = "block";
        slot.style.height = Math.ceil(794 * s) + "px";
        slot.style.paddingLeft = padLeft + "px";
        slide.style.width = "1123px";
        slide.style.height = "794px";
        slide.style.transformOrigin = "top left";
        slide.style.transform = "scale(" + s.toFixed(4) + ")";
      });
    }

    f.style.height = "260px";
    const h = Math.max(doc.documentElement.scrollHeight, doc.body.scrollHeight, 360);
    f.style.height = h + 12 + "px";
  }

  function bindFrameEvents() {
    const f = $("report-frame");
    if (!f) return;
    f.addEventListener("load", () => {
      syncFrame();
      try {
        const doc = f.contentDocument;
        if (doc) {
          doc.querySelectorAll(".wb-radio").forEach((r) => r.addEventListener("change", () => setTimeout(syncFrame, 20)));
        }
      } catch (_e) {
        /* same-origin static reports */
      }
      setTimeout(syncFrame, 120);
    });
  }

  function renderOutput(o) {
    const views = (D.reports && D.reports[o.id]) || [];
    const idx = activeViewIdx[o.id] || 0;
    const cur = views[idx] || views[0];
    const bar =
      views.length > 1
        ? `<div class="view-bar" role="tablist" aria-label="Report artifacts"><span class="view-bar-label">Real artifacts:</span>` +
          views
            .map(
              (v, i) =>
                `<button type="button" class="view-btn" data-vidx="${i}" aria-pressed="${i === idx}"><b>${esc(v.label)}</b><small>${esc(v.kind)}</small></button>`
            )
            .join("") +
          `</div>`
        : `<div class="view-single"><span>Real artifact:</span><b>${esc(cur.label)}</b><small>${esc(cur.kind)}</small></div>`;

    $("out-main").innerHTML =
      head(o, esc(o.lead)) +
      bar +
      `<div class="report-stage"><iframe class="report-frame" id="report-frame" src="${esc(cur.src)}" title="${esc(o.title)} — ${esc(cur.label)}"></iframe></div>` +
      next(o.nextHtml);

    $("out-main")
      .querySelectorAll(".view-btn")
      .forEach((btn) =>
        btn.addEventListener("click", () => {
          activeViewIdx[o.id] = Number(btn.dataset.vidx);
          renderOutput(o);
        })
      );
    bindFrameEvents();
  }

  function show(rawHash) {
    const cleanId = (rawHash || "").split("?")[0];
    const o = byId[cleanId] || byId.count || OUT[0];
    renderOutput(o);
    document.querySelectorAll(".out-tab").forEach((b) => b.setAttribute("aria-current", String(b.dataset.id === o.id)));
    document.title = `${o.title} — Manufacturing Agent Organization`;
  }

  const ORDER = ["count", "hitrate", "backlog", "interface", "weekly", "srm", "recon", "morning"];
  $("out-nav").innerHTML = ORDER.map((id) => byId[id])
    .filter(Boolean)
    .map(
      (o) =>
        `<button class="out-tab" type="button" data-id="${o.id}" style="--c:${C[o.dept]}"><time>${esc(o.time)}</time><b>${esc(o.title)}</b><small>${esc(o.tab)}</small></button>`
    )
    .join("");
  $("out-nav").addEventListener("click", (e) => {
    const b = e.target.closest(".out-tab");
    if (b) location.hash = b.dataset.id;
  });
  window.addEventListener("hashchange", () => {
    show(location.hash.slice(1));
    window.scrollTo({ top: 0 });
  });
  window.addEventListener("resize", syncFrame);

  $("notice").innerHTML = `<b>Sample outputs, synthetic data.</b> No real part, supplier, quantity or result. Day shown: ${esc(D.meta.date)}.`;
  $("asof").textContent = `Sample day ${D.meta.date} · seed ${D.meta.seed}`;
  show(location.hash.slice(1) || "count");
})();
