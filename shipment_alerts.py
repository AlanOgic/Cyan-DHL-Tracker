"""DHL Express event codes that need someone to act, and how an alert about one is worded.

Pure functions only: deciding, building and formatting. Sending is alert_dispatch's job.
Codes and labels come from DHL's Express event list (API/status_1.csv).
"""
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape
from typing import Optional
from urllib.parse import quote

from dhl_client import TrackingEvent

DHL_PUBLIC_TRACKING_URL = (
    "https://www.dhl.com/global-en/home/tracking/tracking-express.html?submit=1&tracking-id={tracking_number}"
)
# odoo_client.format_status_text writes the code as "Status: [HP] …"
STATUS_CODE_PATTERN = re.compile(r"^Status: \[([A-Za-z0-9]+)\]")
# Older events (e.g. on long-forgotten pickings listed by the CLI) are recorded, never alerted
ALERT_MAX_EVENT_AGE = timedelta(days=7)


@dataclass(frozen=True)
class AlertFamily:
    key: str
    label: str
    priority: str  # helpdesk.ticket priority: "0" low … "3" urgent
    color: str  # Mattermost attachment colour


@dataclass(frozen=True)
class AlertCode:
    code: str
    label: str
    family: AlertFamily


FAMILIES = {
    family.key: family
    for family in (
        AlertFamily("customs", "Customs & payment", "2", "#F2A30C"),
        AlertFamily("delivery", "Delivery failure & address", "2", "#F26B1D"),
        AlertFamily("return", "Refused, returned or damaged", "3", "#E0341F"),
        AlertFamily("incident", "DHL incident", "1", "#10BCCF"),
    )
}

_CODES_BY_FAMILY = {
    "customs": {"HP": "Held for payment", "CD": "Controllable clearance delay", "UD": "Uncontrollable clearance delay"},
    "delivery": {
        "ND": "Not delivered", "NH": "Not home", "MD": "Missed delivery cycle", "CA": "Closed on arrival",
        "CC": "Awaiting consignee collection", "BA": "Bad address", "CM": "Customer moved",
    },
    "return": {
        "RD": "Refused delivery", "RT": "Returned to consignor", "DD": "Delivered damaged",
        "PD": "Partial delivery", "DS": "Destroyed / disposal",
    },
    "incident": {"OH": "On hold", "SS": "Shipment stopped", "MS": "Mis-sort", "MC": "Miscode"},
}

ALERT_CODES = {
    code: AlertCode(code, label, FAMILIES[family])
    for family, codes in _CODES_BY_FAMILY.items()
    for code, label in codes.items()
}


@dataclass(frozen=True)
class ShipmentAlert:
    tracking_number: str
    shipment_ref: str
    picking_id: int
    partner_id: Optional[int]
    partner_name: str
    sale_order_id: Optional[int]
    sale_order_name: Optional[str]
    alert_code: AlertCode
    event: TrackingEvent
    next_steps: Optional[str]


def status_code_in(status_text: Optional[str]) -> Optional[str]:
    """Event code recorded in an Odoo last-status text, if any."""
    match = STATUS_CODE_PATTERN.match(status_text or "")
    return match.group(1) if match else None


def needs_alert(event_code: Optional[str], known_code: Optional[str]) -> bool:
    """Alert when the latest DHL event needs action and is not the code already recorded for the shipment."""
    return event_code in ALERT_CODES and event_code != known_code


# Mattermost Markdown: outside text (partner names, DHL wording) must not format, link or @mention
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_\[\]()#>~|])")


def _plain(text: Optional[str]) -> str:
    """Neutralise @mentions (@all, @channel, @here, @user) with a zero-width space."""
    return (text or "").replace("@", "@\u200b")


def _md(text: Optional[str]) -> str:
    return _MARKDOWN_SPECIAL.sub(r"\\\1", _plain(text))


def is_recent(event: TrackingEvent, now: datetime) -> bool:
    """True when the event is within ALERT_MAX_EVENT_AGE of `now` (timezone-aware). Unknown age counts as recent."""
    try:
        happened = datetime.fromisoformat(event.timestamp or "")
    except (TypeError, ValueError):
        return True
    if happened.tzinfo is None:
        return True
    return now - happened <= ALERT_MAX_EVENT_AGE


def build_alert(
    shipment: dict, event: TrackingEvent, next_steps: Optional[str], tracking_number: Optional[str] = None
) -> ShipmentAlert:
    """`tracking_number`: the DHL number with the problem, when the picking's reference holds several."""
    alert_code = ALERT_CODES.get(event.code or "")
    if alert_code is None:
        raise ValueError(f"DHL event code {event.code!r} is not a problem code")
    return ShipmentAlert(
        tracking_number=tracking_number or shipment["tracking_number"],
        shipment_ref=shipment["shipment_ref"],
        picking_id=shipment["picking_id"],
        partner_id=shipment["partner_id"] or None,
        partner_name=shipment["partner_name"],
        sale_order_id=shipment.get("sale_order_id"),
        sale_order_name=shipment.get("sale_order_name"),
        alert_code=alert_code,
        event=event,
        next_steps=next_steps,
    )


def dhl_tracking_url(tracking_number: str) -> str:
    return DHL_PUBLIC_TRACKING_URL.format(tracking_number=quote(tracking_number))


def odoo_record_url(odoo_url: str, model: str, record_id: int) -> str:
    return f"{odoo_url}/odoo/{model}/{record_id}"


def _headline(alert: ShipmentAlert) -> str:
    return f"[{alert.alert_code.code}] {alert.alert_code.label}"


def _when_where(event: TrackingEvent) -> str:
    return " · ".join(part for part in (event.timestamp, event.location) if part)


# --- Mattermost (Markdown) ----------------------------------------------------

def mattermost_payload(alert: ShipmentAlert, odoo_url: str, ticket_id: Optional[int] = None) -> dict:
    headline = _headline(alert)  # catalogue text, safe as Markdown
    picking_link = f"[{_md(alert.shipment_ref)}]({odoo_record_url(odoo_url, 'stock.picking', alert.picking_id)})"
    optional_fields = [
        ("Sale order", alert.sale_order_name and alert.sale_order_id and
         f"[{_md(alert.sale_order_name)}]({odoo_record_url(odoo_url, 'sale.order', alert.sale_order_id)})", True),
        ("Next steps", alert.next_steps and _md(alert.next_steps), False),
        ("Helpdesk", ticket_id and f"[Ticket #{ticket_id}]({odoo_record_url(odoo_url, 'helpdesk.ticket', ticket_id)})", True),
    ]
    fields = [
        {"short": True, "title": "Customer", "value": _md(alert.partner_name)},
        {"short": True, "title": "Delivery", "value": picking_link},
        {"short": False, "title": "DHL event", "value": f"{_md(alert.event.description)} ({_md(_when_where(alert.event))})"},
        *({"short": short, "title": title, "value": value} for title, value, short in optional_fields if value),
    ]
    return {
        "username": "DHL Tracker",
        "text": f"**DHL alert · {alert.alert_code.family.label}** · `{_plain(alert.tracking_number)}` {headline}",
        "attachments": [{
            "fallback": _plain(f"DHL {alert.tracking_number} {headline} · {alert.partner_name}"),  # plain text
            "color": alert.alert_code.family.color,
            "title": f"{headline} · {_md(alert.partner_name)}",
            "title_link": dhl_tracking_url(alert.tracking_number),
            "fields": fields,
        }],
    }


# --- Odoo Helpdesk (HTML; DHL and partner text is escaped) ----------------------

def _link(url: str, text: str) -> str:
    return f'<a href="{escape(url)}">{escape(text)}</a>'


def _ticket_description(alert: ShipmentAlert, odoo_url: str) -> str:
    rows = [
        ("DHL tracking", _link(dhl_tracking_url(alert.tracking_number), alert.tracking_number)),
        ("Delivery", _link(odoo_record_url(odoo_url, "stock.picking", alert.picking_id), alert.shipment_ref)),
        ("Sale order", alert.sale_order_id and alert.sale_order_name and
         _link(odoo_record_url(odoo_url, "sale.order", alert.sale_order_id), alert.sale_order_name)),
        ("Customer", escape(alert.partner_name)),
        ("DHL event", escape(f"{_headline(alert)}: {alert.event.description}")),
        ("When / where", escape(_when_where(alert.event))),
        ("Next steps (DHL)", alert.next_steps and escape(alert.next_steps)),
    ]
    items = "".join(f"<li><strong>{escape(label)}:</strong> {value}</li>" for label, value in rows if value)
    return f"<p>DHL reported an event that needs action ({escape(alert.alert_code.family.label)}).</p><ul>{items}</ul>"


def ticket_values(alert: ShipmentAlert, team_id: int, tag_ids: list, odoo_url: str) -> dict:
    """helpdesk.ticket create values. The customer is linked only when known."""
    values = {
        "name": f"DHL {alert.tracking_number} {_headline(alert)} - {alert.partner_name}",
        "team_id": team_id,
        "priority": alert.alert_code.family.priority,
        "tag_ids": [[6, 0, list(tag_ids)]],
        "description": _ticket_description(alert, odoo_url),
    }
    links = {"partner_id": alert.partner_id, "sale_order_id": alert.sale_order_id}
    return {**values, **{field: value for field, value in links.items() if value}}


def ticket_note(alert: ShipmentAlert) -> str:
    """Internal note added to an already open ticket for the same tracking number."""
    details = " · ".join(part for part in (_when_where(alert.event), alert.next_steps and f"Next steps: {alert.next_steps}") if part)
    note = f"<p>New DHL event <strong>{escape(_headline(alert))}</strong>: {escape(alert.event.description)}</p>"
    return note + (f"<p>{escape(details)}</p>" if details else "")
