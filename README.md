# Manufacturing Agent Organization

A plant's stock lives in two systems, the ERP and the warehouse system. They drift apart every day: postings fail, receipts arrive late, and boxes sit somewhere other than where the screen says. People used to find these differences one by one. This repository is the operations team that now finds them on a schedule. It is a set of agents that do four things every day:

- read both systems every morning;
- fix what is safe to fix;
- plan supplier trucks so the assembly line never runs short;
- hand people a short list of what needs a decision.

**This is a curated, runnable extract of a system that runs every day at a manufacturing plant.** The design, the rules and the guard logic come from production. The data, and the adapters that talk to the real ERP, warehouse system, supplier portal and mail, are not in this repository. Here everything runs on a synthetic plant.

[![Operations floor](https://img.shields.io/badge/Operations_floor-live_demo-0a84ff?style=for-the-badge&logo=github)](https://miaou360.github.io/manufacturing-agent-org/)
[![Sample outputs](https://img.shields.io/badge/Sample_outputs-one_working_day-f4c152?style=for-the-badge)](https://miaou360.github.io/manufacturing-agent-org/demo/outputs.html)
[![Live map](https://img.shields.io/badge/Live_map-jobs_%26_schedule-30b45a?style=for-the-badge)](https://miaou360.github.io/manufacturing-agent-org/demo/index.html)

[![The operations floor: one weekday of the agents, drawn in pixels. Robots are agents, people are people.](docs/screenshots/operations_floor.png)](https://miaou360.github.io/manufacturing-agent-org/)

## Five layers

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/img/layers-dark.svg">
  <img alt="Five layers: sources, ledgers, ontology, logic and actions. People sit at the bottom, and agents run the actions." src="docs/img/layers-light.svg">
</picture>

| Layer | What lives there | In this repository |
| --- | --- | --- |
| 0 · Sources | Files exactly as the ERP, the warehouse system and the supplier portal export them | `adapters/` (fakes; the real adapters are private) |
| 1 · Ledgers | One SQLite ledger per job. Daily snapshots are only ever appended. Every load records its source file hash and time. Reads across jobs are read-only. | `ledgers/` |
| 2 · Ontology | The object types of the plant, the links between them, and the actions that change them, each with its guards | [`ontology/`](ontology/) · [docs](docs/ONTOLOGY.md) |
| 3 · Logic | Code that reads object properties and writes results back as properties: turnover profile, count selection, truck leveling, mismatch cause, receipt matching | `logic/` |
| 4 · Actions | The only way an object changes. Each run checks its guards, writes the action log, then acts or hands the item to a person. | `actions/` |
| Agents | Who runs which action, and when | [`agents/schedule.yaml`](agents/schedule.yaml) |

The ontology is data, not prose: **15 object types, 22 links, 8 actions and 16 guards**, defined in YAML and validated in code on every run. An action whose guard is not implemented refuses to run. Departments and jobs are not object types. They are the actors that run the actions. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for what lives in each layer.

## What the agents produce

Ten agents in two departments, coordinated by HQ. Names match the [operations floor](https://miaou360.github.io/manufacturing-agent-org/).

| Agent | When | What it hands over | Decision it supports |
| --- | --- | --- | --- |
| **Inventory 00** · Stock Accuracy | 06:50 Mon–Sat · Sat 09:40 | Daily count list for the warehouse team; Saturday high-rack list; weekly hit-rate report | Which bins are most worth counting today? Each bin is ranked by risk with its reason, beside a random baseline that keeps the hit rate honest. |
| **Inventory 01** · GR/GI Errors | Hourly · 07:18 · 07:30 | Guarded stock transfers; deletion candidates for approval; daily error report | Which failed receipt and issue postings were fixed safely, and which need a person? |
| **Inventory 02** · Backlog Clear | 06:20 · 08:15 · 08:30 | Guarded stock transfers; backlog report for production control | Which blocked production bookings can be cleared from warehouse stock without hurting accuracy? |
| **Inventory 03** · Backlog Analysis | Fri 08:30 | Weekly analysis | Why does the backlog keep coming back, and which team can fix the cause? |
| **Inventory 04** · System Reconciliation | 06:45 | Stock accuracy, value at risk, register of chronic mismatches | Which differences between the ERP and the warehouse system are real, and which are only timing? |
| **Inventory 05** · Release Lag | 04:15 | Release-lag report, every box ranked by signal | Which boxes were released to production but still sit in a rack bin after two working days: never moved, or moved without a scan? |
| **Inventory 06** · Data Collector | 02:15 · 05:45 · 14:00 | Raw operations ledger: movements, pulling plan, bills of material | None directly. Every other agent reads it. |
| **Logistics 00** · Truck Dispatch | 06:40 · 13:40 Mon–Sat | Truck plan emailed to each supplier; internal monitoring workbook | What goes on each fixed truck, so that no trailer is overloaded and no line runs short? |
| **Logistics 02** · Receipt Check | 05:00 | Delivery status for yesterday's trucks | Which materials arrived short or over against yesterday's truck plan? |
| **Logistics 03** · GR Reconciliation | 07:45 Mon–Sat | Receipt reconciliation, only the lines that need a check | Which receipts did the ERP never book, and which ERP receipts have no scan behind them? |
| **HQ** · Task Manager | 03:10 · 07:35 | Ledger backups; morning operations summary; owner-only system alerts | Did every job run, and is anything broken before the shift starts? |

## Run one day

```bash
pip install -r requirements.txt
python -m simulation.run_day --seed 7 --date 2026-11-24
pytest -q
```

The run follows the agents' schedule on a synthetic two-facility plant: the same parts, suppliers and racks as the two sibling repositories below. The date is a Tuesday, so the 05:00 receipt check has Monday's trucks to check.

It writes its outputs to `out/`:
- the count list;
- the truck plans;
- the delivery status;
- `action_log.csv`, with one row per action and the result of every guard;
- `summary.md`.

**Built-in scenarios, so you can watch each guard work:**

| Scenario | What the agents do |
| --- | --- |
| Stock in a storage location that is not counted | Those bins never reach the list. |
| A warehouse snapshot older than 24 hours | The list is held, and the owner is alerted. |
| A transfer while a receipt is still on its way to the ERP | `nothing_in_flight` fails, and the transfer goes to a person. |
| A truck over its trailer cap | Orders are pulled to earlier trucks, never pushed later. |
| A portal export that repeats an order line | The truck plan fails its output check but still goes out, with a check-the-portal flag on that line, and the owner is alerted. |
| An interface error tied to a goods receipt | Handed to a person, never cleared. |

## How it is kept safe

- **Guards before every action.** An ERP stock transfer is posted only when three guards agree:
  - the difference persists across two snapshots;
  - nothing for the material is still in flight;
  - plant stock accuracy does not go down.

  Anything else is explained and handed to a person.
- **Facts only.** A blank count means *not counted*, never zero. No quantity is inferred.
- **Owner-only alerts.** Failures, stale data and held actions go to the owner, never to operational recipients.
- **Change control.** Every change goes on a branch, is reviewed by a second agent and approved by a person, then merged. Nothing changes during the morning window, 06:20–07:35. Only one agent at a time may hold the ERP session.

Details: [docs/SAFETY.md](docs/SAFETY.md) · design decisions: [docs/DECISIONS.md](docs/DECISIONS.md)

## Repository map

```text
ontology/     objects.yaml · links.yaml · actions.yaml · registry.py (validate, materialize, diagram)
ledgers/      append-only snapshot store with provenance, schema.sql
logic/        turnover_profile · count_selection · truck_leveling · mismatch_cause · receipt_matching
actions/      guards · action_log · one module per action
adapters/     interfaces + fakes for ERP, warehouse system, supplier portal, mail
agents/       schedule.yaml · run_agent.py
simulation/   synthetic plant + run_day
tests/        registry, guards, logic invariants, end-to-end day
demo/         GitHub Pages: operations floor, live map, sample outputs
docs/         ARCHITECTURE · ONTOLOGY · SAFETY · DECISIONS
```

## One plant, three views

The three repositories describe the same synthetic plant with the same parts and racks.

| Repository | Question it answers |
| --- | --- |
| [3d-warehouse-visualizer](https://github.com/Miaou360/3d-warehouse-visualizer) ([live](https://miaou360.github.io/3d-warehouse-visualizer/)) | Where is every pallet, how old is it, and will the racks hold next month's plan? |
| [sequencing-pulling-list](https://github.com/Miaou360/sequencing-pulling-list) ([live](https://miaou360.github.io/sequencing-pulling-list/)) | Which of those pallets should be pulled to the line, and in what order? |
| **manufacturing-agent-org** (this one) | Who keeps the systems in step every day, and what do they hand to people? |

## License

MIT License — Copyright (c) 2026 Seunghun Yea. See [LICENSE](LICENSE).
