#!/usr/bin/env python3
"""Generate docs/dhl-tracking-codes.md, the DHL tracking codes reference, in Markdown so GitLab and GitHub render it.

Alert columns, priorities, colours, family names, limits and the status example come from the
tracker's own modules, so the page cannot drift from what the tracker does: tests/test_dhl_codes_doc.py
fails until the page is regenerated with `python dhl_codes_doc.py`.
"""
import re
from dataclasses import dataclass
from pathlib import Path

from alert_dispatch import DEFAULT_HELPDESK_TAGS, DEFAULT_HELPDESK_TEAM, DEFAULT_TICKET_SKIP_CODES
from automated_tracker import TRACKING_WINDOW_DAYS
from dhl_client import MIN_SECONDS_BETWEEN_CALLS
from odoo_client import TRACKING_EXPIRED_PREFIX, format_status_text
from shipment_alerts import ALERT_CODES, ALERT_MAX_EVENT_AGE, FAMILIES

OUTPUT_PATH = Path(__file__).resolve().parent / "docs" / "dhl-tracking-codes.md"
EXPRESS_EVENT_CODE_COUNT = 40  # official DHL Express event codes (API/status_1.csv)
DHL_DAILY_QUOTA = 250  # DHL developer portal default service level (not enforced by the tracker); see README
HOURLY_CALLS_PER_DAY = 24
EXAMPLE_TRACKING_NUMBER = "1234567890"  # fictitious: the GitHub copy of this page is public
STATUS_EXAMPLE = ("Shipment on hold pending duty payment", "Pay the duties online", "HP")
NO_ALERT = "—"

PRIORITY_LABELS = {"0": "Low", "1": "Medium", "2": "High", "3": "Urgent"}  # helpdesk.ticket priority
# Problem families keep the labels used in the Mattermost alerts
GROUP_TITLES = {"flow": "Normal journey", **{key: family.label for key, family in FAMILIES.items()}}


@dataclass(frozen=True)
class EventCode:
    code: str
    dhl_label: str
    meaning: str
    group: str
    no_estimated_delivery: bool = False  # "no EDD" in DHL's list
    new_since_june_2024: bool = False


EVENT_CODES = (
    EventCode("PU", "Shipment pick up", "Picked up from the shipper.", "flow"),
    EventCode("SA", "Shipment acceptance", "Accepted by DHL.", "flow"),
    EventCode("PL", "Processed at location", "Processed at a DHL facility.", "flow"),
    EventCode("DF", "Depart facility", "Left a DHL facility.", "flow"),
    EventCode("AF", "Arrived facility", "Arrived at a DHL facility.", "flow"),
    EventCode("AR", "Arrival in delivery facility", "Arrived at the station that will deliver it.", "flow"),
    EventCode("WC", "With delivering courier", "Out for delivery.", "flow"),
    EventCode("OK", "Delivery", "Delivered.", "flow", no_estimated_delivery=True),
    EventCode("TR", "Record of transfer", "Transfer recorded between two networks.", "flow"),
    EventCode("SM", "Scheduled for movement", "Booked on an upcoming movement.", "flow", new_since_june_2024=True),
    EventCode("FD", "Forward destination (DD's expected)",
              "Rerouted to another destination; delivery still expected.", "flow"),
    EventCode("AD", "Agreed delivery", "Delivery date or place agreed with the receiver.", "flow"),
    EventCode("SC", "Service changed", "DHL product or service changed.", "flow"),
    EventCode("IC", "In clearance processing", "Customs clearance in progress.", "customs"),
    EventCode("RR", "Response received", "Response received, often from customs.", "customs"),
    EventCode("CR", "Clearance release", "Cleared by customs.", "customs"),
    EventCode("BR", "Broker release", "Released by the broker.", "customs", no_estimated_delivery=True),
    EventCode("BN", "Customer broker notified", "The receiver's broker was notified.", "customs"),
    EventCode("CD", "Controllable clearance delay",
              "Customs delay we can unblock: a missing document or piece of information.", "customs"),
    EventCode("UD", "Uncontrollable clearance delay",
              "Customs delay outside our control: check or inspection.", "customs"),
    EventCode("HP", "Held for payment", "Held until duties and taxes are paid.", "customs"),
    EventCode("PY", "Payment", "Payment received.", "customs", new_since_june_2024=True),
    EventCode("ND", "Not delivered", "Not delivered.", "delivery"),
    EventCode("NH", "Not home", "The receiver was not at home.", "delivery", no_estimated_delivery=True),
    EventCode("MD", "Missed delivery cycle", "Missed the delivery round.", "delivery"),
    EventCode("CA", "Closed on arrival", "The receiver's premises were closed when the courier came.", "delivery",
              no_estimated_delivery=True),
    EventCode("CC", "Awaiting cnee collection",
              "Waiting for the receiver to collect it at a DHL station or service point.",
              "delivery", no_estimated_delivery=True),
    EventCode("BA", "Bad address", "Wrong or incomplete address.", "delivery"),
    EventCode("CM", "Customer moved", "The receiver has moved.", "delivery"),
    EventCode("RD", "Refused delivery", "Refused by the receiver.", "return", no_estimated_delivery=True),
    EventCode("RT", "Returned to consignor", "Returned to the shipper, that is, to us.", "return",
              no_estimated_delivery=True),
    EventCode("DD", "Delivered damaged", "Delivered damaged.", "return", no_estimated_delivery=True),
    EventCode("PD", "Partial delivery", "Partial delivery: pieces are missing.", "return",
              no_estimated_delivery=True),
    EventCode("DS", "Destroyed / disposal", "Shipment destroyed.", "return", no_estimated_delivery=True),
    EventCode("OH", "On hold", "Held at DHL.", "incident"),
    EventCode("SS", "Shipment stopped", "Shipment stopped.", "incident", no_estimated_delivery=True),
    EventCode("MS", "Mis-sort", "Sorting error, being rerouted.", "incident"),
    EventCode("MC", "Miscode", "Code or label error.", "incident"),
    EventCode("TP", "Forwarded to 3rd party - no DD's",
              "Handed over to a third-party carrier; no more delivery tracking.", "incident",
              no_estimated_delivery=True),
    EventCode("CS", "Closed shipment", "Shipment file closed.", "incident", no_estimated_delivery=True),
)

GLOBAL_STATUSES = (
    ("pre-transit", "Label created, not yet handed over to DHL."),
    ("transit", "On its way, including in customs or on hold."),
    ("delivered", "Delivered. The delivery is marked delivered in Odoo."),
    ("failure", "Failed: not delivered, returned, lost."),
    ("unknown", "DHL does not know yet."),
)
NUMERIC_STATUSES = (("104", "pre-transit"), ("102", "transit"), ("101", "delivered"), ("103", "failure"))

API_FIELDS = (
    ("Global status", "status.statusCode", "Yes", "Always present (required field)."),
    ("Latest event code", "events[].status", "Yes", "Two-letter Express code."),
    ("Description", "description", "Yes", "In English, or in French with `language=fr`."),
    ("Next steps", "status.nextSteps", "Sometimes", "Only when DHL has an instruction."),
    ("Full history", "events[]", "Yes", "30 to 45 events per shipment, with date, time and place."),
    ("Event location", "location.address", "Yes", "City or hub, for example CINCINNATI HUB."),
    ("Product", "details.product", "Yes", "EXPRESS 12:00 (Y), EXPRESS WORLDWIDE (P)."),
    ("Origin and destination", "origin, destination", "Yes", "City and country only."),
    ("Pieces", "details.totalNumberOfPieces", "Yes", "Number of pieces and their IDs."),
    ("Proof of delivery", "details.proofOfDelivery", "Sometimes", "Links to the POD and signature, once delivered."),
    ("Shipper and receiver", "details.shipper, consignee", "Yes", ""),
    ("Estimated delivery date", "estimatedTimeOfDelivery", "No", "Empty on our Express shipments."),
    ("Weight and dimensions", "details.weight, dimensions", "No", "Empty on our Express shipments."),
)

TITLE_LEVELS = "One event, three levels"
TITLE_CODES = f"The {EXPRESS_EVENT_CODE_COUNT} Express event codes"
TITLE_TICKETS = "When a ticket is opened"
TITLE_API = "What the API returns for our shipments"
TITLE_LIMITS = "Known limits"


def alert_label(code: str) -> str:
    """Alert column: what the tracker does when this code is a shipment's latest event."""
    alert = ALERT_CODES.get(code)
    if alert is None:
        return NO_ALERT
    if code in DEFAULT_TICKET_SKIP_CODES:
        return "Mattermost"
    return f"**Ticket** · {PRIORITY_LABELS[alert.family.priority]}"


def _codes(codes) -> str:
    return " ".join(f"`{code}`" for code in codes)


def _anchor(title: str) -> str:
    """Heading anchor as GitLab and GitHub build it (lowercase, punctuation dropped, spaces to hyphens)."""
    return re.sub(r"[^\w\- ]", "", title.lower()).replace(" ", "-")


def _table(header: tuple, rows) -> list:
    return [
        f"| {' | '.join(header)} |",
        f"|{'|'.join('---' for _ in header)}|",
        *(f"| {' | '.join(row)} |" for row in rows),
    ]


def _intro() -> list:
    contents = " · ".join(
        f"[{title}](#{_anchor(title)})"
        for title in (TITLE_LEVELS, TITLE_CODES, TITLE_TICKETS, TITLE_API, TITLE_LIMITS)
    )
    return [
        "# DHL tracking codes",
        "",
        "> Generated by `dhl_codes_doc.py` from the tracker's code: do not edit by hand. "
        "After changing the alert codes, run `python dhl_codes_doc.py`; "
        "`tests/test_dhl_codes_doc.py` fails until the page is up to date.",
        "",
        "What DHL returns for each of our Express shipments, how to read its codes, "
        "and which ones open a Helpdesk ticket.",
        "",
        "Sources: DHL Shipment Tracking – Unified API v1.5.8 (`API/track_v1.5.8.yaml`), "
        "DHL's official list of Express codes (`API/status_1.csv`), real responses for our shipments "
        "(October 2026). All our DHL carriers in Odoo are Express products.",
        "",
        f"**Contents:** {contents}",
    ]


def _levels() -> list:
    return [
        f"## {TITLE_LEVELS}",
        "",
        "Every DHL event carries three layers of information. Example, with a fictitious number: "
        f"shipment `{EXAMPLE_TRACKING_NUMBER}`, EXPRESS 12:00 from Brussels to Ohio, "
        "on 2026-10-06 at 05:51 (−04:00) at CINCINNATI HUB, Ohio, USA.",
        "",
        *_table(("Level", "API field", "Example", "What it is for"), (
            ("1 · Global status", "`status.statusCode`", "`transit`",
             f"{len(GLOBAL_STATUSES)} values shared by every DHL service. "
             "**The only information used to decide that a shipment is delivered.**"),
            ("2 · Express code", "`events[].status`", "`DF`",
             f"Depart facility. {EXPRESS_EVENT_CODE_COUNT} two-letter codes that say what happened, "
             "and so whether to act."),
            ("3 · Description", "`description`", "Shipment has departed from a DHL facility CINCINNATI HUB - USA",
             "Free text, in French with `language=fr`. Never used to decide: "
             "\"The shipment could not be delivered\" also contains the word \"delivered\"."),
        )),
        "",
        f"### The {len(GLOBAL_STATUSES)} global statuses",
        "",
        *_table(("Status", "Meaning"), ((f"`{status}`", meaning) for status, meaning in GLOBAL_STATUSES)),
        "",
        f"### The {len(NUMERIC_STATUSES)} numeric Express codes",
        "",
        "They appear on the shipment's global status, not on its events.",
        "",
        *_table(("Code", "Global status"), ((f"`{code}`", f"`{status}`") for code, status in NUMERIC_STATUSES)),
    ]


def _event_row(event: EventCode) -> tuple:
    notes = "".join((
        " _(no estimated delivery)_" if event.no_estimated_delivery else "",
        " _(since June 2024)_" if event.new_since_june_2024 else "",
    ))
    return f"`{event.code}`", event.dhl_label, event.meaning + notes, alert_label(event.code)


def _codes_section() -> list:
    skipped = _codes(sorted(DEFAULT_TICKET_SKIP_CODES))
    ticketed = [code for code in ALERT_CODES if code not in DEFAULT_TICKET_SKIP_CODES]
    lines = [
        f"## {TITLE_CODES}",
        "",
        f"{len(ALERT_CODES)} codes report a problem: {len(ticketed)} open a ticket, and {skipped} "
        "are only posted to Mattermost, because DHL usually resolves them on its own "
        "(default setting, configurable with `HELPDESK_SKIP_CODES`). "
        "The others describe the parcel's normal journey.",
        "",
        "_no estimated delivery_: DHL gives no estimated delivery date with this event "
        "(\"no EDD\" in its list).",
    ]
    for group, title in GROUP_TITLES.items():
        events = [event for event in EVENT_CODES if event.group == group]
        lines += [
            "",
            f"### {title} · {len(events)} codes",
            "",
            *_table(("Code", "DHL label", "What it means", "Alert"), map(_event_row, events)),
        ]
    return [
        *lines,
        "",
        "Seen on our shipments but missing from DHL's list: `SD`, \"Shipment information received\". "
        "DHL warns that new codes may appear. An unknown code never opens a ticket.",
    ]


def _priority_rows() -> list:
    rows = [
        (
            GROUP_TITLES[family.key],
            PRIORITY_LABELS[family.priority],
            f"`{family.color}`",
            _codes(code for code, alert in ALERT_CODES.items()
                   if alert.family.key == family.key and code not in DEFAULT_TICKET_SKIP_CODES),
        )
        for family in FAMILIES.values()
    ]
    return [*rows, ("No ticket", "Mattermost only", NO_ALERT, _codes(sorted(DEFAULT_TICKET_SKIP_CODES)))]


def _tickets_section() -> list:
    max_age_days = ALERT_MAX_EVENT_AGE.days
    return [
        f"## {TITLE_TICKETS}",
        "",
        "There is no ticket per shipment. The tracker raises an alert only when DHL reports a problem "
        f"and all four conditions hold. {_codes(sorted(DEFAULT_TICKET_SKIP_CODES))} "
        "are posted to Mattermost without a ticket.",
        "",
        "1. **Latest DHL event**: the tracker reads the shipment's most recent event.",
        f"2. **Problem code**: its code is one of the {len(ALERT_CODES)} problem codes.",
        "3. **Not reported yet**: the shipment's Odoo status does not already carry this code.",
        f"4. **Recent**: the event is less than {max_age_days} days old. "
        "Older events are recorded in Odoo, without a ticket.",
        "",
        "### Helpdesk ticket",
        "",
        f"- Team {DEFAULT_HELPDESK_TEAM}, tag{'s' if len(DEFAULT_HELPDESK_TAGS) > 1 else ''} "
        f"{', '.join(DEFAULT_HELPDESK_TAGS)}.",
        "- Customer and sale order linked, with links to the delivery and to the DHL tracking page.",
        "- A new problem on a shipment whose ticket is still open becomes an internal note on that ticket, "
        "not a second ticket.",
        "- The tracker only writes internal notes. The customer never gets an e-mail.",
        "",
        "### Mattermost message",
        "",
        "One message per alert, in the family's colour, with a link to the ticket. "
        "Active as soon as the channel's address is set (`ALERT_WEBHOOK_URL`).",
        "",
        "### Status in Odoo",
        "",
        "The delivery's status field keeps the latest DHL code, so the same problem is never reported twice:",
        "",
        "```",
        format_status_text(*STATUS_EXAMPLE),
        "```",
        "",
        "### Priorities and colours",
        "",
        *_table(("Family", "Ticket priority", "Mattermost colour", "Codes"), _priority_rows()),
    ]


def _api_section() -> list:
    return [
        f"## {TITLE_API}",
        "",
        "Checked on real Express shipments, in transit and delivered.",
        "",
        *_table(("Data", "API field", "Available", "Note"),
                ((label, f"`{path}`", available, note) for label, path, available, note in API_FIELDS)),
    ]


def _limits_section() -> list:
    expired_label = TRACKING_EXPIRED_PREFIX.removeprefix("Status: ")
    return [
        f"## {TITLE_LIMITS}",
        "",
        f"- **DHL quota**: {DHL_DAILY_QUOTA} calls a day and at most one call every "
        f"{MIN_SECONDS_BETWEEN_CALLS} seconds, shared by every script. The hourly check costs "
        f"{HOURLY_CALLS_PER_DAY} calls a day per tracked shipment.",
        f"- **{TRACKING_WINDOW_DAYS}-day window**: the automated check only tracks deliveries validated in the "
        f"last {TRACKING_WINDOW_DAYS} days. An older shipment can only be tracked by hand, by its number, "
        "with `shiptracker.py`.",
        "- **Several numbers in one field**: Odoo appends \",number\" to a picking's reference each time a DHL "
        "label is created for it or for a linked picking (re-generated label, return). These are separate "
        "shipments, not pieces: a multi-piece Express shipment keeps a single number. The tracker tracks "
        "each number, and the most recent shipment decides: a return still on its way keeps the picking "
        "tracked, and a label that was never used blocks nothing.",
        f"- **Tracking expired**: an undelivered picking validated more than {TRACKING_WINDOW_DAYS} days ago "
        f"gets \"{expired_label}\" and is no longer tracked. Past that point DHL no longer knows the number, "
        "or returns the events of a newer shipment: DHL reuses its Express numbers.",
    ]


def render() -> str:
    sections = (_intro(), _levels(), _codes_section(), _tickets_section(), _api_section(), _limits_section())
    lines = [line for section in sections for line in (*section, "")]
    footer = (
        "---\n\n_The alert columns, priorities, colours and limits are generated from the tracker's code, "
        "so they cannot drift from what it does._\n"
    )
    return "\n".join(lines) + "\n" + footer


def main(output: Path = OUTPUT_PATH) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render(), encoding="utf-8")
    print(f"wrote {output}")


if __name__ == "__main__":  # pragma: no cover
    main()
