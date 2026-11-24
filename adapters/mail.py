"""Mail adapter interface and deterministic in-memory fake for simulation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class MailAdapter(Protocol):
    """Boundary for sending operational notifications and owner alerts."""

    def send(
        self,
        *,
        recipients: list[str],
        subject: str,
        body: str,
        attachments: list[str] | None = None,
    ) -> str:
        ...


@dataclass
class FakeMailAdapter:
    """In-memory mail fake that records messages without sending any real email."""

    sent_messages: list[dict[str, Any]] = field(default_factory=list)

    def send(
        self,
        *,
        recipients: list[str],
        subject: str,
        body: str,
        attachments: list[str] | None = None,
    ) -> str:
        msg_id = f"MAIL-{len(self.sent_messages) + 1:04d}"
        self.sent_messages.append(
            {
                "message_id": msg_id,
                "recipients": list(recipients),
                "subject": subject,
                "body": body,
                "attachments": list(attachments or ()),
            }
        )
        return msg_id
