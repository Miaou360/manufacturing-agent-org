# Safety

These agents post to a production ERP, send plans to suppliers, and tell the warehouse team which bins to count. This page lists what keeps them from doing harm. Rules attached to an action are guards; the rest are runtime rules and change control.

## Guards on actions

Every action declares its guards in [`ontology/actions.yaml`](../ontology/actions.yaml). The registry refuses to run an action whose guard is not implemented. When a guard fails, the agent never forces the change. It holds the item and alerts the owner, hands it to a person, or sends it with flags.

| Action | Guard | What it prevents |
| --- | --- | --- |
| Post stock transfer | `persistent_difference` | Moving stock against a difference that is only interface latency. The difference must appear in two snapshots at least 15 minutes apart, and the older one must be at most 60 minutes old. |
| | `nothing_in_flight` | Moving stock while a receipt or issue for the same material is still travelling from the warehouse system to the ERP. The movement would be booked twice. |
| | `accuracy_not_down` | Any move that would lower plant stock accuracy, even if it clears the local error. |
| Clear interface error | `not_receipt_linked` | Clearing an error that belongs to a goods receipt. Those always go to a person. |
| | `protected_movement` | Clearing protected movement types. |
| Publish count list | `fresh_snapshot` | Sending the floor a list built on a warehouse snapshot older than 24 hours. The list is held and the owner is alerted. |
| | `count_scope` | Counting stock in a storage location that is not counted, or a bin outside the list's scope. |
| | `recent_match_skip` | Recounting a bin whose last count matched while its barcode set is unchanged (7 days daily, 14 days weekly). |
| Record count result | `facts_only` | Turning a blank into zero, or inferring a quantity from a note. |
| Send truck plan | `no_shortage` | Any plan whose projected stock drops below zero. Orders are only pulled to earlier trucks. |
| | `minimal_change` | Changing a supplier's orders when no truck is overloaded. Overloaded means over the trailer cap, or over 1.7 times the day's average truck load. |
| | `trailer_cap` | Planning more carts than a trailer holds. |
| | `output_verified` | Sending a plan that disagrees with the portal export: totals per material and day, negative quantities, unknown truck slots, repeated lines. The plan still goes out, the failing lines are flagged for a manual portal check, and the owner is alerted. |
| Send delivery status | `owner_approved_recipients` | Mail reaching suppliers before the owner approves supplier delivery, enforced with a staged review gate on new templates or partners. |
| | `receipt_completeness_guard` | Sending false-shortage supplier notices when the warehouse receipt export is partial (`G6` completeness check holds the mail and alerts the owner). |
| Change count scope | `reviewed_change` | Scope changes that a second agent has not reviewed and the owner has not approved. |
| Raise system alert | `owner_only` | Failure and stale-data alerts reaching operational mailboxes. |

## Runtime rules

- **Morning freeze, 06:20–07:35.** No code changes and no heavy batch work while the morning outputs are produced.
- **Dedicated ERP automation sessions with single-writer mutex.** Automation opens isolated read and write ERP sessions tracked by window handle so a person's own ERP sessions are never touched, with automatic fallback only when the ERP session cap is reached. A mutex serializes ERP writes.
- **Holiday quiet mode.** On plant closure days defined in the plant calendar, scheduled jobs still run to preserve ledger continuity, routine operational and supplier emails are held locally, and failure alerts continue to reach the owner.
- **Post-reboot and on-demand health check.** HQ verifies scheduler registration parity, continuous daemon liveness, ERP/portal/mail reachability, stale lock absence, free disk space, and nightly backup age before trusting unattended runs.
- **Read-only across ledgers.** A job opens another job's ledger only with `?mode=ro`.
- **Append-only ledgers with provenance.** Nothing is overwritten. Every load records its source file hash and time.
- **Stale data is visible.** The morning summary marks any output built on old data, even when the output was sent.
- **No secrets in code.** Credentials, hostnames, partner names and real part numbers live only in an uncommitted `.env`. A pre-commit scanner blocks them from being committed.

## Change control

Agents write code too, so code changes follow the same idea as actions:

1. **Branch.** All work happens in its own worktree and branch. Nothing is committed straight to `main`.
2. **Review by a second agent.** The reviewer reads the diff and the evidence (tests, dry-run against the last real morning, before/after comparison), not the author's summary.
3. **Approval by a person,** then merge outside the freeze window and at least 30 minutes before the job runs.
4. **Post-merge check** on `main`: tests, a replay of the last real morning, and the secret scanner.

The review step earns its place. A change that passed its own tests had a pasted block that would have stopped the morning summary for every scheduled job. The review caught it from the diff, and the change was held.
