"""Action: send_delivery_status (data-derived recipient approval guard + hold_and_alert_owner)."""

from __future__ import annotations

from actions import raise_system_alert
from actions.action_log import ActionLog, ActionLogEntry
from actions.guards import GuardResult, run_guards
from adapters.mail import MailAdapter
from ontology.registry import OntologyRegistry

ACTION_NAME = "send_delivery_status"


def execute(
    *,
    registry: OntologyRegistry,
    action_log: ActionLog,
    mail: MailAdapter,
    owner: str,
    recipients: list[str],
    owner_approved: bool,
    subject: str,
    body: str,
    started_at: str,
) -> tuple[ActionLogEntry, list[GuardResult]]:
    """Evaluate owner_approved_recipients from recipient list and approval state."""
    spec = registry.get_action(ACTION_NAME)
    registry.require_guards_implemented(ACTION_NAME)
    results = run_guards(
        [g.id for g in spec.guards],
        {
            "owner": owner,
            "recipients": list(recipients),
            "owner_approved": bool(owner_approved),
        },
    )

    if all(r.passed for r in results):
        out_ref = mail.send(recipients=list(recipients), subject=subject, body=body)
        outcome = "DONE"
    else:
        out_ref = "held:recipient_not_approved"
        outcome = "HELD"

    entry = action_log.record(
        action_type=ACTION_NAME,
        performed_by=spec.performed_by,
        started_at=started_at,
        inputs_ref=f"recipients={','.join(recipients)}",
        guard_results=results,
        outcome=outcome,
        approval=spec.approval,
        outputs_ref=out_ref,
    )

    if outcome == "HELD" and spec.on_guard_fail == "hold_and_alert_owner":
        reasons = "; ".join(g.reason for g in results if not g.passed)
        raise_system_alert.execute(
            registry=registry,
            action_log=action_log,
            mail=mail,
            performed_by=spec.performed_by,
            owner=owner,
            recipients=[owner],
            subject=f"[System alert] Delivery status held ({subject})",
            body=f"send_delivery_status held by guard(s): {reasons}",
            started_at=started_at,
        )

    return entry, results
