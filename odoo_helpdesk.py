"""Odoo Helpdesk tickets for shipment problems, over the JSON-2 API.

A ticket is opened only when DHL reports a problem code (see shipment_alerts). A new
problem on a shipment whose ticket is still open becomes an internal note on it, so a
shipment never has two open tickets. The tracker only posts internal notes,
so the customer linked to a ticket is never e-mailed by it.
"""
import logging
from typing import Any, Optional

from odoo_json2 import OdooError
from shipment_alerts import ShipmentAlert, ticket_note, ticket_values

logger = logging.getLogger(__name__)

TEAM_MODEL = "helpdesk.team"
TAG_MODEL = "helpdesk.tag"
TICKET_MODEL = "helpdesk.ticket"
INTERNAL_NOTE_SUBTYPE = "mail.mt_note"


class HelpdeskClient:
    def __init__(self, transport: Any, team_name: str, tag_names: tuple = (), odoo_url: str = ""):
        self._transport = transport
        self.team_name = team_name
        self.tag_names = tuple(tag_names)
        self._odoo_url = odoo_url
        self._team_id: Optional[int] = None
        self._tag_ids: Optional[list] = None

    def _get_team_id(self) -> Optional[int]:
        if self._team_id is None:
            teams = self._transport.call(TEAM_MODEL, "search_read", domain=[["name", "=", self.team_name]], fields=["id"], limit=1)
            self._team_id = teams[0]["id"] if teams else None
        return self._team_id

    def _get_tag_ids(self) -> list:
        if not self.tag_names:
            return []
        if self._tag_ids is None:
            tags = self._transport.call(TAG_MODEL, "search_read", domain=[["name", "in", list(self.tag_names)]], fields=["id"])
            self._tag_ids = [tag["id"] for tag in tags]
        return self._tag_ids

    def _find_open_ticket(self, team_id: int, tracking_number: str) -> Optional[int]:
        ticket_ids = self._transport.call(
            TICKET_MODEL,
            "search",
            domain=[["team_id", "=", team_id], ["name", "ilike", tracking_number], ["stage_id.fold", "=", False]],
            limit=1,
        )
        return ticket_ids[0] if ticket_ids else None

    def raise_ticket(self, alert: ShipmentAlert) -> Optional[int]:
        """Note the alert on the open ticket for this shipment, or open one. Returns the ticket id, None on failure."""
        try:
            team_id = self._get_team_id()
            if team_id is None:
                logger.error("Helpdesk team %r not found; no ticket for %s", self.team_name, alert.tracking_number)
                return None

            ticket_id = self._find_open_ticket(team_id, alert.tracking_number)
            if ticket_id is not None:
                self._transport.call(
                    TICKET_MODEL,
                    "message_post",
                    ids=[ticket_id],
                    body=ticket_note(alert),
                    body_is_html=True,  # the note's DHL text is escaped by ticket_note
                    message_type="comment",
                    subtype_xmlid=INTERNAL_NOTE_SUBTYPE,
                )
                return ticket_id

            created = self._transport.call(
                TICKET_MODEL, "create", vals_list=[ticket_values(alert, team_id, self._get_tag_ids(), self._odoo_url)]
            )
        except OdooError as exc:
            logger.error("Helpdesk ticket for %s failed: %s", alert.tracking_number, exc)
            return None
        return created[0] if isinstance(created, list) else created
