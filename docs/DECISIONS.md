# Design decisions

Short records of the choices that shape this system. Each entry gives the decision, the reason, and the cost of the choice.

## 1. One ledger per job, read-only across boundaries

**Decision.** Each job owns one SQLite ledger. Other jobs may read it, but only through read-only connections.

**Why.** A job that fails halfway, or a bad change to one job, must not corrupt another job's data. SQLite fits a single workstation that runs scheduled jobs.

**Cost.** Questions that cross jobs need read-only joins, and no single store holds the whole plant. The ontology layer gives them a shared vocabulary.

## 2. Append-only snapshots with provenance

**Decision.** Loads never overwrite. Each snapshot records the source file hash and the time it was captured.

**Why.** "What did the system see at 06:50?" must have an exact answer. That makes every list and every posting reproducible, and every past morning replayable against a change.

**Cost.** Ledgers grow. Raw files are deleted after seven days; snapshots are kept.

## 3. The ontology is data, and actions are the only writers

**Decision.** Object types, links and actions live in YAML and are validated in code. Only actions change objects. Every action declares its guards, and the registry refuses to run an action whose guard is missing.

**Why.** Rules about the plant should sit on the objects they describe, and every automated change should be explainable after the fact.

**Cost.** A new rule needs a model change, not just a code change.

## 4. Scope belongs to objects, not names

**Decision.** Whether stock is counted is a property of its storage location and its bin, applied per box.

**Why.** In production, a name pattern let two bins in an excluded storage location onto a list. Names change and mix; the booked storage location does not lie.

**Cost.** Mixed bins have to be evaluated per box rather than per bin.

## 5. Rules first, the model in shadow

**Decision.** Count lists come from transparent rule lanes. A gradient-boosted model scores the same bins in shadow and replaces a lane only if it beats it on the same days' results.

**Why.** The floor needs a reason for every bin. A model has to earn the switch on the same days' count results.

**Cost.** Better rankings arrive later than they could.

## 6. A random baseline on every list

**Decision.** Part of each daily list is a random sample.

**Why.** Without it, the hit rate only measures how well the risk lanes agree with themselves.

**Cost.** A few counts per day go to bins with no risk signal.

## 7. Facts only

**Decision.** A count result stores only what was verified. A blank stays null, never zero, and no quantity is inferred from notes.

**Why.** Count results become training labels and accuracy figures. One invented zero distorts both.

**Cost.** Some results stay incomplete until someone recounts.

## 8. Pull forward, never push back

**Decision.** Truck leveling only moves orders to earlier trucks. It changes a supplier's orders only when a truck is over its cap or over the overload threshold, and then as few lines as possible.

**Why.** A late truck stops a line. An early box only waits on a rack.

**Cost.** Some plans are less even than they could be.

## 9. Owner-only alerts and a second lock on outside mail

**Decision.** System alerts go to the owner only. A new kind of mail to suppliers stays owner-only until the owner approves it in writing.

**Why.** Operational mailboxes should carry decisions, not failures. A wrong mail to a supplier can't be unsent.

**Cost.** The owner reads more mail.

## 10. Agents write code under the same discipline

**Decision.** Every code change follows the same path: branch, review by a second agent, approval by a person, then merge outside the morning freeze.

**Why.** The agents that run the plant also change their own code. The review is where silent regressions get caught.

**Cost.** A small fix can wait a few hours for its review.

## 11. Holiday quiet mode: keep collecting, hold routine mail

**Decision.** On plant closure days defined in the plant calendar, scheduled jobs still run and update ledgers, routine emails are held locally instead of sent, and failure alerts still go to the owner.

**Why.** Stopping collection breaks multi-day snapshots and post-holiday comparisons, while sending empty routine emails during a holiday creates noise for suppliers and staff.

**Cost.** Held messages accumulate locally and must be verified before returning to normal operations.

## 12. Dedicated ERP automation sessions with automatic fallback

**Decision.** Automation acquires dedicated read and write ERP sessions tracked by window handle instead of sharing the operator's default session, falling back only when the ERP session cap leaves no free slot.

**Why.** Operators work interactively in the ERP during business hours. Sharing one session risks disrupting live screens or colliding with manual transactions.

**Cost.** Session lifecycle tracking, stale-handle cleanup, and per-role locks add complexity to the ERP adapter.

## 13. Supplier dispatch modes and guarded shipment-status mail

**Decision.** Supplier dispatch supports fixed-slot leveling, dynamic dispatch (holding the daily portal total fixed, shifting same-day times freely, and adding minimum extra boxes only on projected stockout), and daily-pallet-pickup summaries (for partners such as Plant 2 / Supplier B). Morning shipment-status mail uses a staged review gate and a receipt-completeness guard (`G6`) before emailing external suppliers.

**Why.** Different supplier contracts need different dispatch rules, and a single incomplete warehouse export would otherwise send false shortage notices to every partner.

**Cost.** Each supplier mode and completeness baseline needs its own test coverage and explicit onboarding approval.

## 14. Post-reboot and on-demand HQ health check

**Decision.** HQ runs a read-only system health check on startup after a reboot and on demand: scheduler parity, daemon freshness, ERP/portal/mail connectivity, stale locks, disk space, and nightly backup age.

**Why.** After an unexpected reboot or configuration change, checking every dependency in one pass surfaces broken prerequisites before the next scheduled slot fires.

**Cost.** Health probes must remain strictly read-only so checking the system never alters live sessions or sends mail.
