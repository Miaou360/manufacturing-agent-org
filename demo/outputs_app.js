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
      unit: "Inventory · Stock Accuracy",
      title: "Daily cycle count list",
      tab: "Which bins to count today",
      when: "Daily 06:50 (weekly high-rack list on Saturday at 06:50)",
      who: "Warehouse team (prints the sheet), inventory lead",
      decision: "Where do 30 counts find the most errors? The list is ranked by risk; the count result comes back the same day.",
      lead: "Out of occupied rack and staging locations across both facilities, the agent picks the daily bins (and Saturday high-rack bins) most worth counting. The 10-sheet workbook carries the blind count sheet alongside diagnostic tabs.",
      nextHtml:
        `Mismatches are recounted, then adjusted by a person; adjusted bins return in tomorrow's follow-up lane. Weekly hit-rate tracking is in ${go("hitrate", "the Saturday hit-rate brief")}. ` +
        `Bins are the rack locations of the <a href="https://miaou360.github.io/3d-warehouse-visualizer/">3D Warehouse Visualizer</a> (N05-04-4 = North, rack 05, bay 04, level 4).`,
    },
    {
      id: "hitrate",
      time: "Sat 09:40",
      dept: "inv",
      unit: "Inventory · Stock Accuracy",
      title: "Weekly floor brief & cycle count hit-rate",
      tab: "Does risk ranking beat random counting?",
      when: "Saturday 09:40, after the week's count sheets are reconciled",
      who: "Operations owner (floor brief & 4-sheet evidence workbook)",
      decision: "Which zones have repeat short-box concentration, and is the risk-ranked lane finding more real errors than random baseline counts?",
      lead: "Compares model-ranked bins against random control bins across 13 count runs, separates timing noise from confirmed floor discrepancies, and maps zone-level discrepancy concentration.",
      nextHtml:
        `High-miss zones and repeat bins feed Monday's ${go("count", "daily cycle count list")} weights and ${go("weekly", "the weekly backlog root-cause report")}.`,
    },
    {
      id: "interface",
      time: "07:30",
      dept: "inv",
      unit: "Inventory · GR / GI Errors",
      title: "Daily GI error cumulative report",
      tab: "Receipts and issues that failed to post",
      when: "Continuous daemon every hour on the hour (24 h); deletion candidates 07:18; daily report 07:30; post-check 08:08",
      who: "Inventory planner (only the rows held by guards or needing deletion/GR review)",
      decision: "Which failed interface postings did the agent clear overnight, which are held by arrival/cooldown guards, and which stale rows are ready for deletion approval?",
      lead: "Summarizes 24-hour automated transfer postings (movement 311), arrival-guard releases, repeat-failure cooldowns, active remaining errors, and verified deletion candidates.",
      nextHtml:
        `Parts with active interface errors block ${go("backlog", "Backlog Clear transfers")} automatically so stock is never double-deducted, and feed extra risk points into ${go("count", "the cycle count list")}.`,
    },
    {
      id: "backlog",
      time: "08:30",
      dept: "inv",
      unit: "Inventory · Backlog Clear",
      title: "Production backlog resolution report",
      tab: "Consumption postings that failed",
      when: "Runs 06:20 snapshot, 08:15 clear, 08:30 second pass and report",
      who: "Production control, inventory team, plant management",
      decision: "What is still blocking yesterday's production from being booked, and who has to act on each remaining line?",
      lead: "Finished units are booked, but component consumption fails when the ERP shows no stock at the line. The agent posts guarded storage-location transfers across morning passes and classifies every remaining line by root cause.",
      nextHtml:
        `Every transfer passes three guards first: persistence across snapshots, no open receipt/issue error on the part (${go("interface", "GR / GI Errors check")}), and non-decreasing plant accuracy. ` +
        `Recurring parts are decomposed every Friday in ${go("weekly", "the weekly 3-question visual report")}.`,
    },
    {
      id: "weekly",
      time: "Fri 08:30",
      dept: "inv",
      unit: "Inventory · Backlog Analysis",
      title: "Weekly backlog 3-question visual report",
      tab: "Why does the backlog keep coming back?",
      when: "Friday 08:30 (5-page landscape visual report + cover mail + 3-sheet workbook)",
      who: "Plant management, production control, purchasing, warehouse leadership",
      decision: "For every recurring backlog part: was material left unscanned between warehouse and line, was warehouse stock truly zero / unposted at receiving, or did 4-week line usage exceed supply?",
      lead: "Clearing the backlog every morning treats the symptom. Every Friday this 5-page visual report cross-examines 4-week supply vs usage, floor cycle counts, and GR/GI history to assign each part to its true operational owner.",
      nextHtml:
        `Unscanned-transfer repeat bins go to floor supervision and ${go("count", "cycle counting")}; unposted receipts go to receiving; persistent 4-week usage gaps go to production engineering for BOM / pack-size review.`,
    },
    {
      id: "recon",
      time: "06:45",
      dept: "inv",
      unit: "Inventory · System Reconciliation",
      title: "ERP – WMS stock reconciliation",
      tab: "Do the two systems agree?",
      when: "Daily 06:45 paired ERP and WMS snapshots, executive mail and 4-sheet workbook",
      who: "Inventory lead, finance (month end)",
      decision: "Is official stock accuracy improving day over day, and which part-level differences are persistent book mismatches rather than in-flight timing?",
      lead: "Reads ERP and WMS inventory at the morning boundary, computes official quantity-weighted accuracy and strict match rate, and tracks multi-day chronic variances.",
      nextHtml:
        `Persistent variances feed ${go("count", "Stock Accuracy cycle counting")} and guard ${go("backlog", "Backlog Clear")} / ${go("interface", "GR / GI Errors")} against accuracy-degrading transfers.`,
    },
    {
      id: "release_lag",
      time: "04:15",
      dept: "inv",
      unit: "Inventory · Release Lag",
      title: "Release lag report (rack bins)",
      tab: "Released boxes still sitting in a rack bin",
      when: "Daily 04:15 (working days exclude Sunday)",
      who: "Warehouse team, inventory lead",
      decision: "Which boxes were already released to production two or more working days ago yet still sit in a rack bin — never moved, or moved without a scan?",
      lead: "Lists every released box still sitting in a Facility North rack bin with no top-N cutoff, sorted by signal priority: refilled with another part, duplicate elsewhere, same part in this bin, or no extra signal.",
      nextHtml:
        `Refilled and duplicate-elsewhere bins are verified on the morning floor walk and cross-checked with ${go("count", "Stock Accuracy cycle counting")}.`,
    },
    {
      id: "srm",
      time: "06:40 / 13:40",
      dept: "log",
      unit: "Logistics · Truck Dispatch",
      title: "Supplier truck loading plan (leveled & capped)",
      tab: "What goes on each truck today & next working day?",
      when: "06:40 (today) and 13:40 (updated through the next working day), Monday to Saturday",
      who: "Suppliers (Supplier A / B; Supplier C internal-only) + logistics planner",
      decision: "Which boxes must move to an earlier truck so the line never runs short, trailers stay within 24 / 40-cart caps (or 1.7 × day average), and output verification flags any portal discrepancy?",
      lead: "Projects 2-hour stock from 06:00, pulls boxes forward only when stock would drop below zero or a truck exceeds its cap / overload limit, verifies every line against the portal (V1–V6), and writes a 2-sheet workbook plus a 5-sheet internal monitoring workbook.",
      nextHtml:
        `If a late portal order change arrives after 13:40, ${go("srm-updated", "the manual PM refresh")} sends an UPDATED diff-highlighted plan only to changed suppliers. The next morning at 05:00, ${go("delivery", "Receipt Check")} compares yesterday's plan against actual scans.`,
    },
    {
      id: "srm-updated",
      time: "15:40 PM",
      dept: "log",
      unit: "Logistics · Truck Dispatch",
      title: "UPDATED supplier truck plan (late SRM change)",
      tab: "What changed since the 13:40 plan?",
      when: "On-demand afternoon refresh after a late SRM order update",
      who: "Only suppliers whose truck loading plan actually changed (unchanged suppliers receive nothing)",
      decision: "Which exact part/truck cells changed relative to the 13:40 plan already sent, so the supplier dock can adjust loading without re-checking every line?",
      lead: "Re-downloads SRM exports, diffs each supplier's Truck plan semantically against the workbook sent at 13:40, highlights only the changed cells in yellow (previous qty in cell note), and prepends the replacement notice at the top of the mail.",
      nextHtml:
        `Unchanged suppliers (Supplier B in this sample: 0 cell diffs) are automatically skipped. Updated files replace the baseline checked at 05:00 by ${go("delivery", "Receipt Check")}.`,
    },
    {
      id: "delivery",
      time: "05:00",
      dept: "log",
      unit: "Logistics · Receipt Check",
      title: "Daily delivery check (yesterday's trucks)",
      tab: "Did yesterday's trucks bring what was planned?",
      when: "Daily 05:00, once all yesterday's trucks are closed",
      who: "Operations owner (summary mail + 4-sheet internal workbook); supplier files wait for owner approval",
      decision: "Across every closed truck from yesterday, which lines arrived DELIVERED, SHORT, OVER, NOT_DELIVERED, or as an UNPLANNED_RECEIPT?",
      lead: "Compares yesterday's warehouse goods-receipt scans against yesterday's truck loading plans in one 05:00 batch and mails a single owner-only summary listing every truck status and exception line.",
      nextHtml:
        `Supplier-facing delivery status workbooks are prepared in the same format and held until the owner approves external delivery. Receipt-to-ERP posting gaps are checked at 07:45 by ${go("gr_recon", "GR Reconciliation")}.`,
    },
    {
      id: "gr_recon",
      time: "07:45",
      dept: "log",
      unit: "Logistics · GR Reconciliation",
      title: "Goods-receipt reconciliation (WMS vs ERP)",
      tab: "Did every dock scan post cleanly in the ERP?",
      when: "Monday to Saturday 07:45 (14-day rolling window)",
      who: "Operations owner (07:45 cover mail + 5-sheet reconciliation workbook)",
      decision: "Which warehouse receipt scans never created an ERP 101 posting, were cleared without posting, or were reversed in the ERP while warehouse stock stays open?",
      lead: "Matches every warehouse receipt bundle to its ERP receipt posting across 14 working days, grouping outcomes into clean matches versus P0/P1 anomalies (categories 1 through 9) and separating provisional D-1 bundles.",
      nextHtml:
        `Silent warehouse-only scans (category 5) and open ERP reversals (category 9) are flagged at P0 for immediate dock/ERP review, while pending errors (category 3) stay linked to ${go("interface", "GR / GI Errors")}.`,
    },
    {
      id: "morning",
      time: "07:35",
      dept: "hq",
      unit: "HQ · Task Manager",
      title: "Morning operations summary",
      tab: "Did every slot run?",
      when: "Daily 07:35, before the day shift meeting",
      who: "Operations owner",
      decision: "Did all 25 scheduled pipelines, ledger backups, and scheduled tasks complete cleanly before the shift starts?",
      lead: "One HQ health brief auditing every night and early-morning pipeline slot, SQLite ledger backup integrity, disk headroom, and model-vs-random cycle count lift.",
      nextHtml:
        `Each row summarizes one live pipeline on this page: ${go("count", "Stock Accuracy")}, ${go("interface", "GR / GI Errors")}, ${go("backlog", "Backlog Clear")}, ${go("recon", "System Reconciliation")}, ${go("srm", "Truck Dispatch")}, and ${go("alerts", "System Alerts")}.`,
    },
    {
      id: "alerts",
      time: "On guard fail",
      dept: "hq",
      unit: "HQ · System Alerts",
      title: "Owner-only system alert & morning callout",
      tab: "What happens when a guard holds a run?",
      when: "Immediately when a guard holds an action or flags a verification failure; echoed in the 07:35 summary",
      who: "Operations owner only (never operational recipients)",
      decision: "Why was today's count list held (snapshot 25.3 h old), what exact guard failed, and what action should the owner take before re-running?",
      lead: "Shows the owner-only alert mail triggered when the freshness guard holds a count list, paired with the matching red [ALERT] row in the 07:35 morning operations summary.",
      nextHtml:
        `Operational recipients never receive broken or stale outputs; once the fresh warehouse snapshot lands, the owner approves a clean replay of ${go("count", "Stock Accuracy")}.`,
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

  const ORDER = [
    "count",
    "hitrate",
    "interface",
    "backlog",
    "weekly",
    "recon",
    "release_lag",
    "srm",
    "srm-updated",
    "delivery",
    "gr_recon",
    "morning",
    "alerts",
  ];
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
