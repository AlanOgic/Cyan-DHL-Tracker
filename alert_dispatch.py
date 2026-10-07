"""Send a shipment alert to the Odoo Helpdesk, then to Mattermost.

Settings (all optional):
- HELPDESK_TEAM: team that receives tickets (default "Logistics & Shipping"; empty disables tickets)
- HELPDESK_TAGS: comma-separated ticket tags (default "Shipping Related")
- HELPDESK_SKIP_CODES: problem codes reported on Mattermost only, without a ticket
  (default "OH,MD,NH": on hold, missed delivery cycle, not home usually resolve themselves)
- ALERT_WEBHOOK_URL: Mattermost incoming webhook for alerts (unset: no Mattermost alert)
"""
import logging
import os
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

import requests

from odoo_helpdesk import HelpdeskClient
from shipment_alerts import ALERT_CODES, ShipmentAlert, mattermost_payload

logger = logging.getLogger(__name__)

DEFAULT_HELPDESK_TEAM = "Logistics & Shipping"
DEFAULT_HELPDESK_TAGS = ("Shipping Related",)
DEFAULT_TICKET_SKIP_CODES = frozenset({"OH", "MD", "NH"})
WEBHOOK_TIMEOUT_SECONDS = 30


class AlertConfigError(ValueError):
    """Raised when an alert setting is invalid."""


class AlertDispatcher:
    def __init__(
        self,
        helpdesk: Optional[HelpdeskClient],
        webhook_url: Optional[str],
        odoo_url: str,
        session: Optional[requests.Session] = None,
        ticket_skip_codes: frozenset = frozenset(),
    ):
        self.helpdesk = helpdesk
        self.webhook_url = webhook_url
        self.odoo_url = odoo_url
        self.ticket_skip_codes = ticket_skip_codes
        self._session = session or requests.Session()

    def dispatch(self, alert: ShipmentAlert) -> bool:
        """True when at least one configured channel got the alert (nothing configured counts as sent).

        A channel that fails is logged, not retried: retrying would repeat the alert on the channel
        that worked. Only an alert nobody received is worth sending again.
        """
        helpdesk = None if alert.alert_code.code in self.ticket_skip_codes else self.helpdesk
        ticket_id = helpdesk.raise_ticket(alert) if helpdesk is not None else None
        outcomes = (
            *((ticket_id is not None,) if helpdesk is not None else ()),
            *((self._notify_mattermost(self.webhook_url, alert, ticket_id),) if self.webhook_url else ()),
        )
        return not outcomes or any(outcomes)

    def _notify_mattermost(self, webhook_url: str, alert: ShipmentAlert, ticket_id: Optional[int]) -> bool:
        try:
            response = self._session.post(
                webhook_url,
                json=mattermost_payload(alert, self.odoo_url, ticket_id),
                timeout=WEBHOOK_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            logger.error("Mattermost alert for %s failed: %s", alert.tracking_number, exc)
            return False
        if response.status_code != 200:
            logger.error("Mattermost alert for %s returned HTTP %s", alert.tracking_number, response.status_code)
            return False
        return True


def _webhook_url(environ: Mapping[str, str]) -> Optional[str]:
    url = environ.get("ALERT_WEBHOOK_URL", "").strip()
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        # The URL carries the webhook's secret key: never echo it in the error
        raise AlertConfigError("ALERT_WEBHOOK_URL must be an https:// Mattermost incoming webhook URL")
    return url


def _tag_names(environ: Mapping[str, str]) -> tuple:
    setting = environ.get("HELPDESK_TAGS")
    if setting is None:
        return DEFAULT_HELPDESK_TAGS
    return tuple(name.strip() for name in setting.split(",") if name.strip())


def _ticket_skip_codes(environ: Mapping[str, str]) -> frozenset:
    setting = environ.get("HELPDESK_SKIP_CODES")
    if setting is None:
        return DEFAULT_TICKET_SKIP_CODES
    codes = frozenset(code.strip().upper() for code in setting.split(",") if code.strip())
    unknown = sorted(codes - set(ALERT_CODES))
    if unknown:
        raise AlertConfigError(
            f"HELPDESK_SKIP_CODES has codes that never raise an alert: {', '.join(unknown)} "
            f"(problem codes: {', '.join(sorted(ALERT_CODES))})"
        )
    return codes


def build_alert_dispatcher(transport: Any, odoo_url: str, environ: Mapping[str, str] = os.environ) -> AlertDispatcher:
    team_name = environ.get("HELPDESK_TEAM", DEFAULT_HELPDESK_TEAM).strip()
    helpdesk = HelpdeskClient(transport, team_name, _tag_names(environ), odoo_url) if team_name else None
    return AlertDispatcher(helpdesk, _webhook_url(environ), odoo_url, ticket_skip_codes=_ticket_skip_codes(environ))
