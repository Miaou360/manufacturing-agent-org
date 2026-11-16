/* Interactive renderer for demo/architecture.html.
   Inline SVG + HTML only, zero external libraries. Clicking any job highlights its ledger, read-only links, swim lane, and outputs. */
(function () {
  "use strict";

  const esc = (s) =>
    String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  const svgIcon = (d) =>
    `<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;

  const ICON = {
    hq: svgIcon('<path d="M12 3l7 3v5c0 4.5-3 8-7 10-4-2-7-5.5-7-10V6z"/><path d="M9 12l2 2 4-4"/>'),
    acc: svgIcon('<rect x="5" y="4" width="14" height="17" rx="2"/><path d="M9 4V3h6v1"/><path d="M8.5 10l1.5 1.5 2.5-2.5M8.5 15.5l1.5 1.5 2.5-2.5M15 10.5h1.5M15 16h1.5"/>'),
    gi: svgIcon('<path d="M4 8h13l-3-3M20 16H7l3 3"/>'),
    bkl: svgIcon('<path d="M4 7l8-3 8 3-8 3z"/><path d="M4 12l8 3 8-3M4 17l8 3 8-3"/>'),
    ana: svgIcon('<path d="M4 20V4M4 20h16"/><rect x="7" y="12" width="3" height="5"/><rect x="12" y="8" width="3" height="9"/><rect x="17" y="10" width="3" height="7"/>'),
    rec: svgIcon('<path d="M12 4v16M6 20h12M5 8h14"/><path d="M5 8l-2.5 6a3 3 0 005 0zM19 8l-2.5 6a3 3 0 005 0z"/>'),
    rbin: svgIcon('<path d="M4 8h16v11a1 1 0 01-1 1H5a1 1 0 01-1-1z"/><path d="M3 5h18v3H3zM9.5 13.5h5M12 11.5l-2.5 2 2.5 2"/>'),
    col: svgIcon('<ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v6c0 1.7 3.1 3 7 3s7-1.3 7-3V6M5 12v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6"/>'),
    srm: svgIcon('<path d="M3 7h11v9H3zM14 10h4l3 3v3h-7z"/><circle cx="7" cy="17.5" r="1.8"/><circle cx="17" cy="17.5" r="1.8"/>'),
    dlv: svgIcon('<path d="M4 5h16v14H4z"/><path d="M8 9v6M11 9v6M14 9v3M17 9v3"/><path d="M13.5 15.5l1.5 1.5 3-3"/>'),
  };

  const LEDGERS = [
    {
      unit: "hq",
      db: "JOBS.yaml + locks/",
      snapshot: false,
      outId: "morning",
      outLabel: "Morning operations brief (07:35)",
      tables: [
        ["JOBS.yaml", "job_id, schedule_slot, entry_script, freeze_window"],
        ["sap_session.lock", "owner_pid, acquired_at, script_name"],
        ["provenance_cache", "db_path, mtime_ns, byte_size, sha256_prefix"],
        ["backup_manifest", "run_date, ledger_name, wal_checkpoint_ok"],
      ],
      reads: ["acc", "gi", "bkl", "rec", "rbin", "col", "srm", "dlv"],
    },
    {
      unit: "acc",
      db: "inventory_history.db",
      snapshot: true,
      outId: "count",
      outLabel: "Daily count list (06:50) & Weekly hit rate",
      tables: [
        ["wms_barcode_daily", "snapshot_date, bin_code, barcode, part_no, qty"],
        ["barcode_lifecycle", "barcode, part_no, gr_date, gi_date, stay_days"],
        ["material_ontology", "part_no, median_turnover_days, mover_class"],
        ["audit_assignment", "run_id, bin_code, lane_code, risk_score, prob"],
        ["audit_result", "assignment_id, counted_qty, book_qty, diff_qty"],
        ["shadow_scores_v2", "snapshot_date, bin_code, p_error_v2, top_factor"],
      ],
      reads: ["gi", "col"],
    },
    {
      unit: "gi",
      db: "gi_error_history.db",
      snapshot: true,
      outId: "interface",
      outLabel: "Interface error report (07:30) & deletion list",
      tables: [
        ["gi_error_snapshots", "batch_id, snapshot_time, part_no, mvt, sloc, qty"],
        ["gr_error_snapshots", "batch_id, snapshot_time, part_no, sloc, error_qty"],
        ["transfer_postings", "batch_id, part_no, from_sloc, to_sloc, doc_no, status"],
        ["delete_candidates", "report_date, sumno, barcode, gate_verdict, status"],
      ],
      reads: ["col"],
    },
    {
      unit: "bkl",
      db: "backlog_history.db",
      snapshot: true,
      outId: "backlog",
      outLabel: "Post-clearing backlog report (08:30)",
      tables: [
        ["cogi_snapshots", "snapshot_id, part_no, sloc, mvt_type, order_qty"],
        ["backlog_transfers", "batch_id, part_no, from_sloc, to_sloc, doc_no, status"],
        ["daily_backlog_metrics", "run_date, run_time, total_items, resolved_items"],
        ["deficit_tracker", "part_no, sloc, erp_stock, wms_stock, consecutive_days"],
      ],
      reads: ["gi", "col"],
    },
    {
      unit: "ana",
      db: "backlog_analytics.db",
      snapshot: false,
      outId: "weekly",
      outLabel: "Friday 3-question visual analysis (08:30)",
      tables: [
        ["mart_daily_part_flow", "work_date, part_no, prod_gi_qty, transfer_qty"],
        ["mart_root_cause_weekly", "week_start, part_no, cause_bucket, repeat_days"],
        ["line_stock_baseline", "part_no, line_sloc, normal_buffer_qty"],
      ],
      reads: ["bkl", "rec", "col"],
    },
    {
      unit: "rec",
      db: "system_reconciliation.db",
      snapshot: true,
      outId: "recon",
      outLabel: "Plant ERP–WMS reconciliation (06:45)",
      tables: [
        ["accuracy_daily_summary", "run_date, official_accuracy_pct, matched_parts"],
        ["location_variance_snap", "run_date, part_no, sloc, erp_qty, wms_qty, diff"],
        ["chronic_mismatch_log", "part_no, sloc, consecutive_snapshots, verdict"],
      ],
      reads: ["gi", "bkl"],
    },
    {
      unit: "rbin",
      db: "r_bins_history.db",
      snapshot: true,
      outId: null,
      outLabel: "Receiving staging-bin lag report (04:15)",
      tables: [
        ["staging_bin_daily", "scan_date, bin_code, barcode, part_no, dwell_hours"],
        ["lag_alert_history", "scan_date, bin_code, part_no, threshold_class"],
        ["bin_clearance_metrics", "scan_date, open_boxes, cleared_within_24h"],
      ],
      reads: [],
    },
    {
      unit: "col",
      db: "ops_raw.db",
      snapshot: false,
      outId: null,
      outLabel: "Nightly movement & Sunday BOM feed (02:15)",
      tables: [
        ["mb51_raw", "mat_doc_no, item_no, part_no, sloc, mvt_type, qty_entry, dt"],
        ["bom_snapshot", "snapshot_date, parent_part, component_part, usage_qty"],
        ["pulling_plan_snap", "plan_date, bin_code, part_no, planned_pull_qty"],
        ["ingest_watermarks", "source_name, covered_until_dt, row_checksum"],
      ],
      reads: [],
    },
    {
      unit: "srm",
      db: "inbox/ + sent_snapshots/",
      snapshot: true,
      outId: "srm",
      outLabel: "Supplier truck plan (06:40 / 13:40 / UPDATED)",
      tables: [
        ["srm_require_2h", "run_slot, part_no, supplier_id, slot_start, req_qty"],
        ["srm_order_plan", "run_slot, part_no, truck_window, srm_order_qty"],
        ["truck_plan_leveled", "supplier_id, part_no, truck_slot, load_qty, carts"],
        ["pm_last_sent_json", "run_date, supplier_id, sent_at, workbook_sha256"],
      ],
      reads: ["rec"],
    },
    {
      unit: "dlv",
      db: "delivery_check.db",
      snapshot: true,
      outId: "delivery",
      outLabel: "Supplier delivery-status file (every 10 min)",
      tables: [
        ["plan_horizon_parts", "work_date, part_key, box_qty, start_sap, load_json"],
        ["truck_runs", "truck_id, supplier_key, slot_idx, status, quiet_polls"],
        ["truck_plan_lines", "truck_id, part_key, planned_pcs, planned_boxes"],
        ["wms_gr_barcodes", "barcode, mat_no, gr_time, gr_qty, assigned_truck_id"],
        ["delivery_verdicts", "truck_id, part_key, verdict, line_shortage_alarm"],
      ],
      reads: ["srm", "rec"],
    },
  ];

  const SWIMLANES = [
    {
      unit: "col",
      source: "ERP movement & BOM exports",
      collect: "02:15 daily movement dump; Sun 04:40 BOM",
      ledger: "<code>ops_raw.db</code> watermarked append",
      decide: "Deduplicate document legs & tag production vs transfer flows",
      guard: "Session recovery + watermark gap check before retention",
      act: "Publishes read-only movement feed for 5 sibling jobs",
      report: "Slot status to <a href='outputs.html#morning'>07:35 summary</a>",
    },
    {
      unit: "rbin",
      source: "WMS box-level snapshot",
      collect: "04:15 receiving & return staging scan",
      ledger: "<code>r_bins_history.db</code> dwell tracker",
      decide: "Rank boxes left in receiving bins past shift threshold",
      guard: "Read-only scan + sharp row-drop anomaly banner",
      act: "Flags aged pallets for put-away before morning trucks arrive",
      report: "04:15 lag list → Inventory lead",
    },
    {
      unit: "rec",
      source: "ERP stock + WMS snapshot",
      collect: "06:45 plant-wide stock export",
      ledger: "<code>system_reconciliation.db</code>",
      decide: "Separate transient timing drift from multi-day variance",
      guard: "0-row / stale-export abort + 24h freshness check",
      act: "Writes canonical morning ERP stock file for other jobs",
      report: "<a href='outputs.html#recon'>Reconciliation</a> → Inventory & finance",
    },
    {
      unit: "srm",
      source: "Supplier portal + morning ERP stock",
      collect: "06:40 (today), 13:40 (tomorrow) & manual PM refresh",
      ledger: "<code>inbox/</code> &amp; <code>pm_last_sent</code>",
      decide: "Zero-shortage pull + 1.7x / 33-cart minimal-change leveling",
      guard: "Never push later, day-total invariance, changed-only resend",
      act: "Writes 2-sheet Truck Plan (and diff-only UPDATED plan)",
      report: "<a href='outputs.html#srm'>Truck plan</a> &amp; <a href='outputs.html#srm-updated'>UPDATED</a>",
    },
    {
      unit: "dlv",
      source: "Frozen truck plan + WMS 101 GR scans",
      collect: "Every 10 min around active truck windows",
      ledger: "<code>delivery_check.db</code>",
      decide: "Attribute GR barcodes to trucks; project hybrid 2h stock",
      guard: "?mode=ro boundary, missing-start-stock suppression, dual mail lock",
      act: "Closes settled trucks &amp; builds Plan vs Delivered workbook",
      report: "<a href='outputs.html#delivery'>Delivery-status</a> → Suppliers &amp; planner",
    },
    {
      unit: "acc",
      source: "WMS boxes, GR/GI flow, floor sheets",
      collect: "Every 2h stage flow + 06:50 count run",
      ledger: "<code>inventory_history.db</code>",
      decide: "4-lane bin selection (Model, Follow-up, Capacity, Explore)",
      guard: "Per-bin cool-down, verified counts only, shadow before live",
      act: "Dispatches 1-page count sheet; ingests returned counts",
      report: "<a href='outputs.html#count'>Count list</a> & <a href='outputs.html#hitrate'>Sat hit rate</a>",
    },
    {
      unit: "gi",
      source: "WMS–ERP interface error queues",
      collect: "30m day / 1h night daemon + 07:18 snapshot",
      ledger: "<code>gi_error_history.db</code>",
      decide: "Classify root cause; score 1–7 deletion gates at 07:23",
      guard: "Delta_Accuracy ≥ 0, chain lock, submit hard-lock",
      act: "Posts safe 311/312 transfers; builds prefilled draft link",
      report: "<a href='outputs.html#interface'>07:30 error report</a> → Inventory planner",
    },
    {
      unit: "bkl",
      source: "ERP backflush queue + stock",
      collect: "06:20 snapshot, 08:15 run, 08:30 MaxClear",
      ledger: "<code>backlog_history.db</code>",
      decide: "Match line deficits to surplus warehouse stock",
      guard: "Warehouse floor >= WMS physical; UNKNOWN_VERIFY anti-repost",
      act: "Transfers stock, saves COGI, runs 1 retry pass if needed",
      report: "<a href='outputs.html#backlog'>Single team report</a> → Production",
    },
    {
      unit: "ana",
      source: "Movements + backlog + variances",
      collect: "Fri 08:30 read-only cross-ledger build",
      ledger: "<code>backlog_analytics.db</code> mart",
      decide: "Decompose 4-week supply vs usage & repeat chronic parts",
      guard: "Strict ?mode=ro URI connections across sibling DBs",
      act: "Compiles 5-page visual slide deck + filterable workbook",
      report: "<a href='outputs.html#weekly'>Weekly analysis</a> → Plant management",
    },
    {
      unit: "hq",
      source: "JOBS.yaml + all job run logs",
      collect: "03:10 WAL backup + 07:35 slot probe",
      ledger: "<code>locks/</code> & backup manifest",
      decide: "Verify every morning slot finished inside its window",
      guard: "ERP GUI lock, browser lock, GitGuard secret scanner",
      act: "Checkpoints 9 SQLite ledgers; blocks unsafe commits",
      report: "<a href='outputs.html#morning'>07:35 summary</a> → Operations owner",
    },
  ];

  // Coordinates for the inline SVG read-only dependency diagram (viewBox 880 x 230)
  const NODE_POS = {
    col: { x: 74, y: 58, label: "06 Collector", sub: "ops_raw.db" },
    rec: { x: 74, y: 170, label: "04 Reconcile", sub: "morning stock" },
    gi: { x: 236, y: 58, label: "01 GR/GI", sub: "gi_error.db" },
    bkl: { x: 236, y: 170, label: "02 Backlog", sub: "backlog.db" },
    acc: { x: 398, y: 58, label: "00 Accuracy", sub: "inventory.db" },
    ana: { x: 398, y: 170, label: "03 Analysis", sub: "analytics.db" },
    srm: { x: 560, y: 58, label: "00 Dispatch", sub: "truck plans" },
    dlv: { x: 710, y: 58, label: "02 Delivery", sub: "delivery.db" },
    rbin: { x: 560, y: 170, label: "05 Return-Bin", sub: "r_bins.db" },
    hq: { x: 830, y: 124, label: "00 HQ", sub: "07:35 probe" },
  };

  function unitMap() {
    const m = {};
    window.ORG.units.forEach((u) => {
      m[u.id] = u;
    });
    return m;
  }

  function deptColor(u) {
    return window.ORG.depts[u.dept].color;
  }

  function renderPills(unitsById, selectFn) {
    const host = document.getElementById("arch-pills");
    host.innerHTML = window.ORG.units
      .filter((u) => !u.planned)
      .map((u) => {
        const c = deptColor(u);
        return (
          `<button type="button" class="arch-pill" data-unit="${u.id}" style="--c:${c}" aria-pressed="false">` +
          `${ICON[u.id]}<b>${esc(u.name)}</b><small>${esc(u.code)}</small></button>`
        );
      })
      .join("");
    host.querySelectorAll(".arch-pill").forEach((btn) => {
      btn.addEventListener("click", () => selectFn(btn.dataset.unit));
    });
  }

  function renderRoSvg(unitsById, selectFn) {
    const svg = document.getElementById("ro-svg");
    const edges = [];
    LEDGERS.forEach((l) => {
      l.reads.forEach((srcId) => {
        if (NODE_POS[srcId] && NODE_POS[l.unit]) {
          edges.push({ from: srcId, to: l.unit, hqProbe: l.unit === "hq" });
        }
      });
    });

    const defs =
      `<defs>` +
      `<marker id="arr-ro" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">` +
      `<path d="M 0 1 L 9 5 L 0 9 z" fill="#f4c152"/></marker>` +
      `<marker id="arr-hq" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse">` +
      `<path d="M 0 1 L 9 5 L 0 9 z" fill="#6382b8"/></marker>` +
      `</defs>`;

    const edgeSvg = edges
      .map((e) => {
        const a = NODE_POS[e.from];
        const b = NODE_POS[e.to];
        const x1 = a.x + 56;
        const y1 = a.y;
        const x2 = b.x - 56;
        const y2 = b.y;
        const mx = (x1 + x2) / 2;
        const d = `M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`;
        const stroke = e.hqProbe ? "#4b6594" : "#f4c152";
        const dash = e.hqProbe ? "3 4" : "5 4";
        const marker = e.hqProbe ? "url(#arr-hq)" : "url(#arr-ro)";
        return `<path class="ro-edge" data-from="${e.from}" data-to="${e.to}" d="${d}" fill="none" stroke="${stroke}" stroke-width="1.6" stroke-dasharray="${dash}" marker-end="${marker}" opacity="0.72"/>`;
      })
      .join("");

    const nodeSvg = Object.keys(NODE_POS)
      .map((id) => {
        const p = NODE_POS[id];
        const u = unitsById[id];
        const c = deptColor(u);
        return (
          `<g class="ro-node" data-unit="${id}" transform="translate(${p.x - 54}, ${p.y - 24})" tabindex="0" role="button" aria-label="${esc(u.name)} ledger">` +
          `<rect width="108" height="48" rx="9" fill="#141f35" stroke="${c}" stroke-width="1.8"/>` +
          `<text x="54" y="20" text-anchor="middle" fill="#eef3fb" font-family="Segoe UI, sans-serif" font-size="11.5" font-weight="600">${esc(p.label)}</text>` +
          `<text x="54" y="36" text-anchor="middle" fill="#8ea0bf" font-family="Consolas, monospace" font-size="10.2">${esc(p.sub)}</text>` +
          `</g>`
        );
      })
      .join("");

    svg.innerHTML = defs + edgeSvg + nodeSvg;
    svg.querySelectorAll(".ro-node").forEach((g) => {
      g.addEventListener("click", () => selectFn(g.dataset.unit));
      g.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          selectFn(g.dataset.unit);
        }
      });
    });
  }

  function renderLedgerCards(unitsById, selectFn) {
    const grid = document.getElementById("ledger-grid");
    grid.innerHTML = LEDGERS.map((l) => {
      const u = unitsById[l.unit];
      const c = deptColor(u);
      const roBadges = [];
      if (l.snapshot) {
        roBadges.push(`<span class="ro-tag">reads morning stock</span>`);
      }
      l.reads.forEach((rId) => {
        if (l.unit !== "hq") {
          roBadges.push(`<span class="ro-tag">?mode=ro ← ${esc(unitsById[rId].code)}</span>`);
        }
      });
      if (l.unit === "hq") {
        roBadges.push(`<span class="ro-tag">probes & backs up all 9 DBs</span>`);
      }
      if (!roBadges.length) {
        roBadges.push(`<span class="ro-tag">primary source ledger</span>`);
      }
      return (
        `<article class="ledger-card" data-unit="${l.unit}" style="--c:${c}" tabindex="0" role="button" aria-label="${esc(u.name)} ledger details">` +
        `<div class="ledger-head">` +
        `<div class="ledger-title"><span class="avatar" style="--c:${c}">${ICON[l.unit]}</span>` +
        `<div><small>${esc(window.ORG.depts[u.dept].name)} · ${esc(u.code)}</small><b>${esc(u.name)}</b></div></div>` +
        `<span class="db-badge">${esc(l.db)}</span></div>` +
        `<ul class="tbl-list">${l.tables
          .map(([t, cols]) => `<li><code>${esc(t)}</code><span>${esc(cols)}</span></li>`)
          .join("")}</ul>` +
        `<div class="ro-links">${roBadges.join("")}</div>` +
        `</article>`
      );
    }).join("");

    grid.querySelectorAll(".ledger-card").forEach((el) => {
      el.addEventListener("click", () => selectFn(el.dataset.unit));
      el.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          selectFn(el.dataset.unit);
        }
      });
    });
  }

  function renderSwimlanes(unitsById, selectFn) {
    const host = document.getElementById("swim-table");
    const hdr =
      `<div class="swim-hdr">` +
      `<div>Job / Agent</div><div>1. Source</div><div>2. Collect</div><div>3. Ledger</div>` +
      `<div>4. Decide</div><div class="guard-col">5. Guard</div><div>6. Act</div><div>7. Report → Person</div>` +
      `</div>`;
    const rows = SWIMLANES.map((s) => {
      const u = unitsById[s.unit];
      const c = deptColor(u);
      return (
        `<div class="swim-row" data-unit="${s.unit}" style="--c:${c}">` +
        `<div class="swim-cell who-col"><small>${esc(window.ORG.depts[u.dept].name)} · ${esc(u.code)}</small><b>${esc(u.name)}</b></div>` +
        `<div class="swim-cell">${esc(s.source)}</div>` +
        `<div class="swim-cell">${esc(s.collect)}</div>` +
        `<div class="swim-cell">${s.ledger}</div>` +
        `<div class="swim-cell">${esc(s.decide)}</div>` +
        `<div class="swim-cell guard-col">${esc(s.guard)}</div>` +
        `<div class="swim-cell">${esc(s.act)}</div>` +
        `<div class="swim-cell">${s.report}</div>` +
        `</div>`
      );
    }).join("");
    host.innerHTML = hdr + rows;
    host.querySelectorAll(".swim-row").forEach((row) => {
      row.addEventListener("click", (ev) => {
        if (ev.target.closest("a")) return;
        selectFn(row.dataset.unit);
      });
    });
  }

  function renderDecisionSvg(selectFn) {
    const svg = document.getElementById("decision-svg");
    svg.innerHTML = `
      <defs>
        <marker id="arr-dec" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
          <path d="M 0 1 L 9 5 L 0 9 z" fill="#199e70"/>
        </marker>
        <marker id="arr-fb" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
          <path d="M 0 1 L 9 5 L 0 9 z" fill="#f4c152"/>
        </marker>
      </defs>
      <g data-unit="acc" style="cursor:pointer">
        <rect x="14" y="18" width="176" height="92" rx="10" fill="#141f35" stroke="#199e70" stroke-width="1.8"/>
        <text x="28" y="40" fill="#eef3fb" font-size="12.5" font-weight="600">1. Morning Inputs</text>
        <text x="28" y="59" fill="#9fb0cc" font-size="11">• WMS box &amp; lot snapshot</text>
        <text x="28" y="75" fill="#9fb0cc" font-size="11">• 2h GR/GI &amp; stage flow</text>
        <text x="28" y="91" fill="#9fb0cc" font-size="11">• Open GI &amp; variance flags</text>

        <path d="M 190 64 L 226 64" stroke="#199e70" stroke-width="2" marker-end="url(#arr-dec)"/>

        <rect x="228" y="18" width="198" height="92" rx="10" fill="#141f35" stroke="#199e70" stroke-width="1.8"/>
        <text x="242" y="40" fill="#eef3fb" font-size="12.5" font-weight="600">2. Feature &amp; Guard Filter</text>
        <text x="242" y="59" fill="#9fb0cc" font-size="11">• FIFO lot overtaking &amp; dwell</text>
        <text x="242" y="75" fill="#9fb0cc" font-size="11">• Vendor relabel-suspect blanking</text>
        <text x="242" y="91" fill="#9fb0cc" font-size="11">• Per-bin cool-down + scope guard</text>

        <path d="M 426 64 L 462 64" stroke="#199e70" stroke-width="2" marker-end="url(#arr-dec)"/>

        <rect x="464" y="18" width="198" height="92" rx="10" fill="#141f35" stroke="#f4c152" stroke-width="1.8"/>
        <text x="478" y="40" fill="#f8dfa1" font-size="12.5" font-weight="600">3. 4-Lane Fixed-Cap Sheet</text>
        <text x="478" y="59" fill="#d9e2f2" font-size="11">• Follow-up · Explore (random)</text>
        <text x="478" y="75" fill="#d9e2f2" font-size="11">• Capacity · Model (top risk)</text>
        <text x="478" y="91" fill="#9fb0cc" font-size="11">• 06:50 sheet → floor count team</text>

        <path d="M 662 64 L 698 64" stroke="#199e70" stroke-width="2" marker-end="url(#arr-dec)"/>

        <rect x="700" y="18" width="166" height="92" rx="10" fill="#141f35" stroke="#199e70" stroke-width="1.8"/>
        <text x="714" y="40" fill="#eef3fb" font-size="12.5" font-weight="600">4. Ground Truth</text>
        <text x="714" y="59" fill="#9fb0cc" font-size="11">• Verified counts only</text>
        <text x="714" y="75" fill="#9fb0cc" font-size="11">• Uncounted stays NULL</text>
        <text x="714" y="91" fill="#9fb0cc" font-size="11">• Next-day 06:51 check</text>

        <path d="M 783 110 L 783 146 L 327 146 L 327 112" fill="none" stroke="#f4c152" stroke-width="1.8" stroke-dasharray="5 4" marker-end="url(#arr-fb)"/>
        <text x="555" y="139" text-anchor="middle" fill="#f4c152" font-family="Consolas, monospace" font-size="10.8">Closed feedback loop: model vs random hit-rate lift + chronic bin memory + shadow v2 scoring</text>
      </g>
    `;
    svg.querySelector("g").addEventListener("click", () => selectFn("acc"));
  }

  function renderLogisticsSvg(selectFn) {
    const svg = document.getElementById("logistics-svg");
    if (!svg) return;
    svg.innerHTML = `
      <defs>
        <marker id="arr-log" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
          <path d="M 0 1 L 9 5 L 0 9 z" fill="#d95926"/>
        </marker>
        <marker id="arr-warn" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
          <path d="M 0 1 L 9 5 L 0 9 z" fill="#f4c152"/>
        </marker>
      </defs>
      <g data-unit="srm" style="cursor:pointer">
        <rect x="10" y="16" width="124" height="92" rx="9" fill="#141f35" stroke="#d95926" stroke-width="1.8"/>
        <text x="20" y="37" fill="#eef3fb" font-size="11.8" font-weight="600">1. SRM Order</text>
        <text x="20" y="55" fill="#9fb0cc" font-size="10.4">• 2h &amp; daily portal</text>
        <text x="20" y="71" fill="#9fb0cc" font-size="10.4">• 06:00 ERP stock</text>
        <text x="20" y="87" fill="#9fb0cc" font-size="10.4">• Late PM changes</text>

        <path d="M 134 62 L 154 62" stroke="#d95926" stroke-width="2" marker-end="url(#arr-log)"/>

        <rect x="156" y="16" width="154" height="92" rx="9" fill="#141f35" stroke="#f4c152" stroke-width="1.8"/>
        <text x="166" y="37" fill="#f8dfa1" font-size="11.8" font-weight="600">2. Leveling (00_JOB)</text>
        <text x="166" y="55" fill="#d9e2f2" font-size="10.4">• Rule 1: 0 shortages</text>
        <text x="166" y="71" fill="#d9e2f2" font-size="10.4">• Rule 2: &gt;1.7× or &gt;33 cap</text>
        <text x="166" y="87" fill="#9fb0cc" font-size="10.4">• Fewest parts pulled</text>

        <path d="M 310 62 L 330 62" stroke="#d95926" stroke-width="2" marker-end="url(#arr-log)"/>

        <rect x="332" y="16" width="136" height="92" rx="9" fill="#141f35" stroke="#d95926" stroke-width="1.8"/>
        <text x="342" y="37" fill="#eef3fb" font-size="11.8" font-weight="600">3. Supplier Plan</text>
        <text x="342" y="55" fill="#9fb0cc" font-size="10.4">• 06:40 / 13:40 xlsx</text>
        <text x="342" y="71" fill="#9fb0cc" font-size="10.4">• Total carts (boxes)</text>
        <text x="342" y="87" fill="#f8dfa1" font-size="10.4">• UPDATED diff-only</text>
      </g>

      <path d="M 468 62 L 488 62" stroke="#d95926" stroke-width="2" marker-end="url(#arr-log)"/>

      <g data-unit="dlv" style="cursor:pointer">
        <rect x="490" y="16" width="118" height="92" rx="9" fill="#141f35" stroke="#d95926" stroke-width="1.8"/>
        <text x="500" y="37" fill="#eef3fb" font-size="11.8" font-weight="600">4. WMS GR</text>
        <text x="500" y="55" fill="#9fb0cc" font-size="10.4">• 10-min poll</text>
        <text x="500" y="71" fill="#9fb0cc" font-size="10.4">• Mvt 101 scans</text>
        <text x="500" y="87" fill="#9fb0cc" font-size="10.4">• Quiet-poll close</text>

        <path d="M 608 62 L 628 62" stroke="#d95926" stroke-width="2" marker-end="url(#arr-log)"/>

        <rect x="630" y="16" width="122" height="92" rx="9" fill="#141f35" stroke="#d95926" stroke-width="1.8"/>
        <text x="640" y="37" fill="#eef3fb" font-size="11.8" font-weight="600">5. Delivery Check</text>
        <text x="640" y="55" fill="#9fb0cc" font-size="10.4">• Plan vs Delivered</text>
        <text x="640" y="71" fill="#9fb0cc" font-size="10.4">• Hybrid 2h stock</text>
        <text x="640" y="87" fill="#9fb0cc" font-size="10.4">• Next-truck gap</text>

        <path d="M 752 62 L 772 62" stroke="#d95926" stroke-width="2" marker-end="url(#arr-log)"/>

        <rect x="774" y="16" width="96" height="92" rx="9" fill="#141f35" stroke="#f4c152" stroke-width="1.8"/>
        <text x="784" y="37" fill="#f8dfa1" font-size="11.8" font-weight="600">6. Warning</text>
        <text x="784" y="55" fill="#d9e2f2" font-size="10.4">• Red shortfall</text>
        <text x="784" y="71" fill="#d9e2f2" font-size="10.4">• Full boxes</text>
        <text x="784" y="87" fill="#9fb0cc" font-size="10.4">• Pre-cutoff</text>
      </g>

      <path d="M 822 108 L 822 156 L 400 156 L 400 110" fill="none" stroke="#f4c152" stroke-width="1.7" stroke-dasharray="5 4" marker-end="url(#arr-warn)"/>
      <text x="610" y="148" text-anchor="middle" fill="#f4c152" font-family="Consolas, monospace" font-size="10.5">Recovery loop: supplier adds missing boxes onto the next scheduled truck before line stock reaches zero</text>
      <text x="235" y="174" text-anchor="middle" fill="#9fb0cc" font-family="Segoe UI, sans-serif" font-size="10.6">Leveling rule: pull forward whole boxes only (never push later) · keep ≥50% stock margin · largest box count first</text>
    `;
    svg.querySelectorAll("g[data-unit]").forEach((g) => {
      g.addEventListener("click", () => selectFn(g.dataset.unit));
    });
  }

  function renderApprovalLoop() {
    const ol = document.getElementById("arch-loop");
    ol.innerHTML = window.ORG.loop
      .map(
        (s) =>
          `<li class="${s.who === "Owner" ? "owner" : ""}"><b>${esc(s.who)}</b><span>${esc(s.what)}</span><small>${esc(s.note)}</small></li>`
      )
      .join("");
  }

  function init() {
    const unitsById = unitMap();
    let current = null;

    function selectUnit(id) {
      current = current === id || !id ? null : id;
      document.body.classList.toggle("has-focus", !!current);

      const peers = new Set();
      if (current) {
        peers.add(current);
        const entry = LEDGERS.find((l) => l.unit === current);
        if (entry) entry.reads.forEach((r) => peers.add(r));
        LEDGERS.forEach((l) => {
          if (l.reads.includes(current)) peers.add(l.unit);
        });
      }

      document.querySelectorAll("[data-unit]").forEach((el) => {
        const u = el.dataset.unit;
        const active = u === current;
        const peer = !active && peers.has(u);
        el.classList.toggle("is-active", active);
        el.classList.toggle("is-peer", peer);
        if (el.hasAttribute("aria-pressed")) {
          el.setAttribute("aria-pressed", active ? "true" : "false");
        }
      });

      document.querySelectorAll("#ro-svg .ro-edge").forEach((edge) => {
        const hit = current && (edge.dataset.from === current || edge.dataset.to === current);
        edge.classList.toggle("is-active", !!hit);
      });

      const bar = document.getElementById("arch-focus-bar");
      if (!current) {
        bar.hidden = true;
      } else {
        const u = unitsById[current];
        const l = LEDGERS.find((x) => x.unit === current);
        bar.hidden = false;
        document.getElementById("focus-text").innerHTML =
          `<b>${esc(u.code)} · ${esc(u.name)}</b> — ${esc(u.one)} Ledger: <code>${esc(l.db)}</code>`;
        const outLink = document.getElementById("focus-out-link");
        if (l.outId) {
          outLink.hidden = false;
          outLink.href = `outputs.html#${l.outId}`;
          outLink.textContent = `Open sample: ${l.outLabel} →`;
        } else {
          outLink.hidden = true;
        }
      }
    }

    renderPills(unitsById, selectUnit);
    renderRoSvg(unitsById, selectUnit);
    renderLedgerCards(unitsById, selectUnit);
    renderSwimlanes(unitsById, selectUnit);
    renderDecisionSvg(selectUnit);
    renderLogisticsSvg(selectUnit);
    renderApprovalLoop();

    document.getElementById("clear-focus").addEventListener("click", () => selectUnit(null));
    document.querySelectorAll(".safety-card[data-unit], .decision-stage[data-unit]").forEach((el) => {
      el.addEventListener("click", () => selectUnit(el.dataset.unit));
    });
    document.getElementById("asof").textContent = `Architecture snapshot aligned with ${window.ORG.asOf}`;

    if (location.hash) {
      const h = location.hash.slice(1);
      if (unitsById[h]) selectUnit(h);
    }
  }

  init();
})();
