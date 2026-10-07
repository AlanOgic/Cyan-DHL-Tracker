"""Write one DHL tracking result to Odoo, alerting first when a new action code appears.

Every script that writes DHL statuses to Odoo goes through ShipmentSync, so the
"[CODE]" marker in the last-status field (which stops repeat alerts) stays consistent.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from alert_dispatch import build_alert_dispatcher
from dhl_client import is_transient_error, latest_event, shipment_number, summarize_status
from odoo_client import OdooClient
from odoo_json2 import Json2Client, OdooConfig
from shipment_alerts import build_alert, is_recent, needs_alert, status_code_in


@dataclass(frozen=True)
class SyncResult:
    status: str
    next_steps: Optional[str]
    delivered: bool
    event_code: Optional[str]
    alerted: bool


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ShipmentSync:
    def __init__(self, odoo_client: Any, dispatcher: Any, clock: Callable[[], datetime] = _utc_now):
        self.odoo_client = odoo_client
        self.dispatcher = dispatcher
        self._clock = clock
        # Code written per tracking number by this process: pickings sharing a tracking
        # number are loaded with the same stale status, and must not alert twice
        self._written_codes: dict = {}

    def apply(self, shipment: dict, tracking_data: dict) -> SyncResult:
        status, next_steps, delivered = summarize_status(tracking_data)
        tracking_number = shipment["tracking_number"]
        known_code = self._written_codes.get(tracking_number, status_code_in(shipment.get("last_status")))
        if is_transient_error(tracking_data) or (tracking_data.get("error") and known_code):
            # The real status is unknown: never replace a known DHL status (and its marker) with an error
            return SyncResult(status, next_steps, delivered, None, False)

        event = latest_event(tracking_data)
        event_code = event.code if event else None
        problem_event = (
            event if event is not None and needs_alert(event_code, known_code) and is_recent(event, self._clock())
            else None
        )
        alerted = problem_event is not None and self.dispatcher.dispatch(
            build_alert(shipment, problem_event, next_steps, tracking_number=shipment_number(tracking_data))
        )
        alert_failed = problem_event is not None and not alerted
        if alert_failed:
            marker = None  # no marker: the alert is tried again at the next check
        else:
            marker = event_code or known_code

        # A delivered shipment whose alert failed stays tracked, or the alert would be lost
        close_as_delivered = delivered and not alert_failed
        if close_as_delivered:
            self.odoo_client.update_delivery_status(tracking_number, delivered=True)
        else:
            self.odoo_client.update_delivery_status(
                tracking_number, delivered=False, current_status=status, next_steps=next_steps, event_code=marker
            )
        self._written_codes[tracking_number] = event_code if close_as_delivered else marker
        return SyncResult(status, next_steps, close_as_delivered, event_code, alerted)


def build_from_env() -> tuple:
    """(OdooClient, ShipmentSync) sharing one JSON-2 connection, configured from the environment."""
    config = OdooConfig.from_env()
    transport = Json2Client(config)
    odoo_client = OdooClient(transport=transport)
    return odoo_client, ShipmentSync(odoo_client, build_alert_dispatcher(transport, config.url))
