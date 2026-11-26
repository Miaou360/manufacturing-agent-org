# Agent Runbook Template

Use one page per scheduled agent so an on-call operator can verify health and recover from failures without reading source code or waiting for an AI review cycle.

---

## Global Operating Rules

- **Morning freeze window (`06:20–07:35` plant local time):** Never merge code, restart daemons, or run ad-hoc heavy queries while morning dispatches are in flight.
- **Holiday quiet mode (`plant_calendar.yaml`):** On scheduled closure days, agents still collect snapshots and update ledgers, routine emails are held locally (`HELD_QUIET_DAY`), and only owner-only failure alerts fire.
- **Login lock files (`*_login_blocked.json`):** Created automatically after a single authentication failure to avoid locking shared ERP/portal accounts. **Never delete the lock file until credentials are verified manually.**
- **Read-only cross-ledger rule:** Always inspect another agent's SQLite ledger with `?mode=ro` (`sqlite3 "file:path/to/ledger.db?mode=ro"`).

---

## `<AGENT_ID>` — `<Agent Display Name>`

### 1. What It Does and When

- **Schedule:** `<e.g., Mon–Sat 06:50 plant local time>` (Windows Task / Cron ID: `<TASK_NAME>`)
- **Inputs:** `<ERP / WMS / Supplier Portal export + read-only upstream ledgers>`
- **Outputs:** `<Owned SQLite ledger, Excel/PDF/CSV report, recipient channel>`
- **Freeze & Quiet-Day Behavior:** `<e.g., Runs normally on quiet days; holds routine mail locally>`

### 2. How to Tell It Is Healthy

1. **Status JSON:** `<agent_dir>/logs/<agent>_latest.json` shows `"status": "OK"` and today's timestamp within the expected slot grace window.
2. **Ledger Freshness:** Query owned ledger (`?mode=ro`) — latest run row matches today's workday (`06:00–05:59` boundary) and `row_count > 0`.
3. **HQ Morning Summary (`07:35`):** Slot badge is green (`OK`) and subject has no `[ALERT]` prefix.

### 3. Top 5 Failure Messages and Safe Recovery

| # | Exact Log Pattern to Look For | Root Cause | Safe Operator Action |
| :---: | :--- | :--- | :--- |
| 1 | `STALE_SNAPSHOT_HOLD` / `snapshot is older than 24 hours` | Upstream warehouse/ERP export failed; output held to prevent acting on old data. | Confirm source portal/ERP login works, run a fresh collection outside `06:20–07:35`, then re-run this agent. |
| 2 | `LOGIN_BLOCKED` / `*_login_blocked.json exists` | Single credential failure triggered the account-lockout brake. | Sign in manually in a browser to check password expiry, update `.env`, then remove the lock file and run once manually. |
| 3 | `UNKNOWN_VERIFY` | ERP posting button succeeded but status bar returned blank/timeout. | **Do NOT manually repost immediately.** Check ERP material documents (`MB51`) first to confirm whether the document posted; wait for nightly reconciliation if uncertain. |
| 4 | `PARTIAL_DOWNLOAD` / `G6_FAIL_CLOSED` | Downloaded row count fell below the weekday completeness floor (e.g., `<50%` of recent weekday average). | Inspect the raw export. If truncated, re-download; if genuine low-volume holiday eve, use the documented one-time manual override flag. |
| 5 | `SENT_BUT_UNREADABLE` / `FAIL_CLOSED` | Authoritative archived plan or required config is missing/corrupted; external send diverted to owner only. | Verify the archived attachment or `.env` configuration, restore the file, and re-run with `--resend`. |

### 4. Escalation Matrix (Who to Ask)

| Issue Domain | Contact Role | What to Attach |
| :--- | :--- | :--- |
| ERP session / network / portal outage | Plant IT / ERP Support | Exact timestamp, log tail, screenshot of error modal |
| Physical stock, missing scans, or receiving disputes | Warehouse / Receiving Lead | Part number, part description, bin code, barcode list |
| Supplier short/over delivery or schedule change | Materials / Procurement Planner | Last-sent truck plan file + delivery status workbook |
| Agent rule, threshold, or guard adjustment | Automation Owner | Link to run status JSON + proposed change note (branch + review required) |
