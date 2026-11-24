"""Action: raise_system_alert (owner-only alert delivery)."""

from __future__ import annotations

from actions.action_log import ActionLog, ActionLogEntry
from actions.guards import GuardResult, run_guards
from adapters.mail import MailAdapter
from ontology.registry import OntologyRegistry

ACTION_NAME = "raise_system_alert"


def execute(
    *,
    registry: OntologyRegistry,
    action_log: ActionLog,
    mail: MailAdapter,
    performed_by: str,
    owner: str,
    recipients: list[str],
    subject: str,
    body: str,
    started_at: str,
) -> tuple[ActionLogEntry, list[GuardResult]]:
    """Evaluate owner-only guard from recipients/owner, send alert on pass, and log."""
    if not str(performed_by).startswith("agent."):
        raise ValueError(f"raise_system_alert performed_by must start with 'agent.', got {performed_by!r}")

    spec = registry.get_action(ACTION_NAME)
    registry.require_guards_implemented(ACTION_NAME)
    results = run_guards(
        [g.id for g in spec.guards],
        {"owner": owner, "recipients": list(recipients)},
    )

    if all(r.passed for r in results):
        out_ref = mail.send(recipients=list(recipients), subject=subject, body=body)
        outcome = "DONE"
    else:
        out_ref = "held:non_owner_recipient"
        outcome = "HELD"

    entry = action_log.record(
        action_type=ACTION_NAME,
        performed_by=performed_by,
        started_at=started_at,
        inputs_ref=f"subject={subject}",
        guard_results=results,
        outcome=outcome,
        approval=spec.approval,
        outputs_ref=out_ref,
    )
    return entry, results
