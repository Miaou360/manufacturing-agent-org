/* The operations floor: source systems -> agents at their desks -> the people who receive the work.
   Rendered by app.js (renderOffice / updateOffice). No dependencies. */
(function () {
  "use strict";
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const mins = (t) => { const [h, m] = t.split(":").map(Number); return h * 60 + m; };
  const svg = (d) => `<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
  const ICON = {
    hq: svg('<path d="M12 3l7 3v5c0 4.5-3 8-7 10-4-2-7-5.5-7-10V6z"/><path d="M9 12l2 2 4-4"/>'),
    acc: svg('<rect x="5" y="4" width="14" height="17" rx="2"/><path d="M9 4V3h6v1"/><path d="M8.5 10l1.5 1.5 2.5-2.500M8.5 15.500l1.5 1.5 2.5-2.500M15 10.500h1.500M15 16h1.5"/>'),
    gi: svg('<path d="M4 8h13l-3-3M20 16H7l3 3"/>'),
    bkl: svg('<path d="M4 7l8-3 8 3-8 3z"/><path d="M4 12l8 3 8-3M4 17l8 3 8-3"/>'),
    ana: svg('<path d="M4 20V4M4 20h16"/><rect x="7" y="12" width="3" height="5"/><rect x="12" y="8" width="3" height="9"/><rect x="17" y="10" width="3" height="7"/>'),
    rec: svg('<path d="M12 4v16M6 20h12M5 8h14"/><path d="M5 8l-2.5 6a3 3 0 005 0zM19 8l-2.5 6a3 3 0 005 0z"/>'),
    rbin: svg('<path d="M4 8h16v11a1 1 0 01-1 1H5a1 1 0 01-1-1z"/><path d="M3 5h18v3H3zM9.5 13.500h5M12 11.500l-2.5 2 2.5 2"/>'),
    col: svg('<ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v6c0 1.7 3.1 3 7 3s7-1.3 7-3V6M5 12v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6"/>'),
    srm: svg('<path d="M3 7h11v9H3zM14 10h4l3 3v3h-7z"/><circle cx="7" cy="17.5" r="1.8"/><circle cx="17" cy="17.5" r="1.8"/>'),
    erp: svg('<rect x="4" y="4" width="16" height="6" rx="1.5"/><rect x="4" y="14" width="16" height="6" rx="1.5"/><path d="M7.5 7h.01M7.5 17h.01M11 7h5M11 17h5"/>'),
    wms: svg('<path d="M3 10l9-6 9 6v10H3z"/><path d="M8 20v-6h8v6M8 17h8"/>'),
    portal: svg('<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.500c2.5 2.5 3.5 5.5 3.5 8.500s-1 6-3.5 8.500c-2.5-2.5-3.5-5.5-3.5-8.500s1-6 3.5-8.500z"/>'),
    floor: svg('<path d="M4 6h3M4 12h3M4 18h3M10 6h10M10 12h10M10 18h10"/>'),
    person: svg('<circle cx="12" cy="8" r="3.5"/><path d="M5 20c.800-3.5 3.5-5.5 7-5.500s6.2 2 7 5.5"/>'),
    team: svg('<circle cx="9" cy="8.5" r="3"/><circle cx="16.5" cy="9.5" r="2.3"/><path d="M3.5 19.500c.600-3 2.7-4.8 5.5-4.800s4.9 1.8 5.5 4.800M15 14.800c2.7-.300 4.7 1.2 5.5 3.7"/>'),
    doc: svg('<path d="M7 3h7l4 4v14H7z"/><path d="M14 3v4h4M10 12h5M10 16h5"/>'),
  };
  const ROLE = { // one plain line per desk: what this agent is for
    hq: "Keeps everyone from colliding; reports every morning", acc: "Picks the bins worth counting today",
    gi: "Fixes receipts and issues that failed to post", bkl: "Unblocks yesterday's production bookings",
    ana: "Finds why the backlog keeps coming back", rec: "Checks that ERP and WMS agree",
    rbin: "Finds material left in receiving bins", col: "Brings in the night's raw data",
    srm: "Warns before the line runs short",
  };
  const SYSTEMS = [
    { icon: "erp", name: "ERP", sub: "stock, postings, production" }, { icon: "wms", name: "Warehouse system", sub: "every box and bin" },
    { icon: "portal", name: "Supplier portal", sub: "delivery plans" }, { icon: "floor", name: "Shop floor", sub: "count sheets come back" },
  ];
  const PEOPLE = [
    { id: "count", icon: "team", name: "Count team", sub: "counts the bins on the list" },
    { id: "inv", icon: "person", name: "Inventory lead", sub: "decides adjustments and deletions" },
    { id: "prod", icon: "team", name: "Production control & management", sub: "acts on what is still blocked" },
    { id: "logi", icon: "person", name: "Logistics planner", sub: "calls suppliers, changes trucks" },
    { id: "owner", icon: "person", name: "Operations owner", sub: "approves changes, reads the summary" },
  ];
  const DELIVER = [ // unit, time, document, recipient, sample output id, weekly tag
    ["rbin", "04:15", "Return-bin report", "inv"], ["srm", "06:40", "Supplier truck plan", "logi", "srm"], ["rec", "06:45", "Reconciliation", "inv", "recon"],
    ["acc", "06:50", "Daily count list", "count", "count"], ["gi", "07:23", "Deletion candidates", "inv"], ["gi", "07:30", "Error report", "inv", "interface"],
    ["hq", "07:35", "Morning summary", "owner", "morning"], ["bkl", "08:30", "Backlog report", "prod", "backlog"],
    ["ana", "08:30", "Weekly analysis", "prod", "weekly", "Fri"], ["acc", "09:40", "Weekly hit-rate report", "inv", "hitrate", "Sat"],
    ["srm", "13:40", "Truck plan for tomorrow", "logi", "srm"],
  ].map(([unit, t, doc, to, out, tag]) => ({ unit, t, m: mins(t), doc, to, out, tag }));

  let O, onSelect;
  window.renderOffice = function (root, org, select) {
    O = org; onSelect = select;
    const color = (u) => O.depts[u.dept].color;
    const desk = (u) => `<button type="button" class="unit desk${u.id === "hq" ? " desk-hq" : ""}" data-unit="${u.id}" style="--c:${color(u)}" aria-pressed="false">` +
      `<span class="avatar">${ICON[u.id]}</span><span class="who"><small>${esc(u.code)}</small><b>${esc(u.name)}</b><em>${esc(ROLE[u.id])}</em></span>` +
      `<span class="state" data-state="${u.id}"><i></i><span></span></span></button>`;
    const room = (d) => `<div class="room" style="--c:${O.depts[d].color}"><div class="room-name"><i></i>${esc(O.depts[d].name)} <small>${((n) => n + (n === 1 ? " agent" : " agents"))(O.units.filter((u) => u.dept === d).length)}</small></div>` +
      `<div class="desks">${O.units.filter((u) => u.dept === d).map(desk).join("")}</div></div>`;
    root.innerHTML =
      `<div class="col-side"><h3>They read</h3>${SYSTEMS.map((s) => `<div class="sys"><span class="avatar">${ICON[s.icon]}</span><span><b>${esc(s.name)}</b><small>${esc(s.sub)}</small></span></div>`).join("")}</div>` +
      `<div class="arrow" aria-hidden="true"><span>read</span></div>` +
      `<div class="floor"><h3>The agents <small>select a desk</small></h3>${desk(O.units.find((u) => u.id === "hq"))}<div class="rooms">${room("inv")}${room("log")}</div></div>` +
      `<div class="arrow" aria-hidden="true"><span>deliver</span></div>` +
      `<div class="col-side people"><h3>People receive</h3>${PEOPLE.map((p) => `<div class="inbox" data-inbox="${p.id}"><span class="avatar">${ICON[p.icon]}</span>` +
        `<span class="who"><b>${esc(p.name)}</b><small>${esc(p.sub)}</small></span><span class="badge" aria-label="documents received">0</span><ul></ul></div>`).join("")}</div>`;
    root.querySelectorAll(".desk").forEach((b) => b.addEventListener("click", () => onSelect(b.dataset.unit)));
  };

  // now = minute of the day; running = [{unit, name}]; nextOf(unitId) -> "HH:MM" of the unit's next start or null
  window.updateOffice = function (now, running, nextOf) {
    const busy = {};
    running.forEach((r) => { busy[r.unit] = r.name; });
    O.units.forEach((u) => {
      const s = document.querySelector(`[data-state="${u.id}"]`); if (!s) return;
      const b = s.closest(".desk");
      b.classList.toggle("running", !!busy[u.id]);
      const nx = nextOf(u.id);
      s.lastElementChild.textContent = busy[u.id] ? `Working: ${busy[u.id]}` : nx ? `Next run ${nx}` : "Done for today";
    });
    PEOPLE.forEach((p) => {
      const box = document.querySelector(`[data-inbox="${p.id}"]`); if (!box) return;
      const got = DELIVER.filter((d) => d.to === p.id && d.m <= now);
      box.querySelector(".badge").textContent = got.length;
      box.classList.toggle("has", got.length > 0);
      box.classList.toggle("fresh", got.some((d) => now - d.m < 20));
      box.querySelector("ul").innerHTML = got.map((d) => `<li${now - d.m < 20 ? ' class="new"' : ""}><time>${d.t}</time>` +
        (d.out ? `<a href="outputs.html#${d.out}">${esc(d.doc)}</a>` : `<span>${esc(d.doc)}</span>`) + (d.tag ? `<em>${d.tag}</em>` : "") + `</li>`).join("");
    });
  };
})();
