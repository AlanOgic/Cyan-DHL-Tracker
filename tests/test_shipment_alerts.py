from datetime import datetime, timedelta, timezone
from html import escape

import pytest

from dhl_client import TrackingEvent
from shipment_alerts import (
    ALERT_CODES,
    ALERT_MAX_EVENT_AGE,
    FAMILIES,
    build_alert,
    dhl_tracking_url,
    is_recent,
    mattermost_payload,
    needs_alert,
    odoo_record_url,
    status_code_in,
    ticket_note,
    ticket_values,
)

ODOO_URL = "https://odoo.example.com"

SHIPMENT = {
    "tracking_number": "1000000001",
    "shipment_ref": "SH0000-00001",
    "picking_id": 4930,
    "partner_id": 7538,
    "partner_name": "Example Broadcast & Co",
    "sale_order_id": 3520,
    "sale_order_name": "SO0000-0001",
    "last_status": "Status: [DF] Departed",
}

EVENT = TrackingEvent(
    code="HP",
    description="Shipment on hold <pending duty payment>",
    timestamp="2026-10-06T05:51:00-04:00",
    location="CINCINNATI HUB - Ohio - USA",
)


def make_alert(event=EVENT, next_steps="Pay duties online", shipment=SHIPMENT):
    return build_alert(shipment, event, next_steps)


# --- catalogue --------------------------------------------------------------

def test_catalogue_covers_the_four_families():
    assert set(FAMILIES) == {"customs", "delivery", "return", "incident"}
    assert {code for code, alert in ALERT_CODES.items() if alert.family.key == "customs"} == {"HP", "CD", "UD"}
    assert {code for code, alert in ALERT_CODES.items() if alert.family.key == "delivery"} == {
        "ND", "NH", "MD", "CA", "CC", "BA", "CM"
    }
    assert {code for code, alert in ALERT_CODES.items() if alert.family.key == "return"} == {"RD", "RT", "DD", "PD", "DS"}
    assert {code for code, alert in ALERT_CODES.items() if alert.family.key == "incident"} == {"OH", "SS", "MS", "MC"}


@pytest.mark.parametrize("family", ["customs", "delivery", "return", "incident"])
def test_every_family_has_a_valid_odoo_priority(family):
    assert FAMILIES[family].priority in {"0", "1", "2", "3"}


# --- status marker & decision -----------------------------------------------

@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("Status: [HP] Shipment on hold\nNext Steps: Pay", "HP"),
        ("Status: [101] Delivered", "101"),
        ("Status: Not Found", None),
        ("", None),
        (None, None),
    ],
)
def test_status_code_in_reads_the_marker(text, code):
    assert status_code_in(text) == code


@pytest.mark.parametrize(
    ("event_code", "known_code", "expected"),
    [("HP", "DF", True), ("HP", None, True), ("HP", "HP", False), ("CD", "HP", True), ("DF", "PL", False), (None, "HP", False)],
)
def test_needs_alert_only_when_an_action_code_appears(event_code, known_code, expected):
    assert needs_alert(event_code, known_code) is expected


# --- event age --------------------------------------------------------------

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


def event_at(timestamp):
    return TrackingEvent("HP", "On hold", timestamp, None)


def test_recent_events_are_within_the_alert_window():
    assert is_recent(event_at("2026-10-06T05:51:00-04:00"), NOW) is True
    assert is_recent(event_at((NOW - ALERT_MAX_EVENT_AGE).isoformat()), NOW) is True


def test_old_events_are_outside_the_alert_window():
    assert is_recent(event_at((NOW - ALERT_MAX_EVENT_AGE - timedelta(minutes=1)).isoformat()), NOW) is False


@pytest.mark.parametrize("timestamp", [None, "", "yesterday", "2026-10-06T05:51:00"])
def test_events_of_unknown_age_count_as_recent(timestamp):
    assert is_recent(event_at(timestamp), NOW) is True


# --- building ---------------------------------------------------------------

def test_build_alert_combines_shipment_event_and_catalogue():
    alert = make_alert()

    assert alert.tracking_number == "1000000001"
    assert alert.alert_code.code == "HP"
    assert alert.alert_code.label == "Held for payment"
    assert alert.alert_code.family.key == "customs"
    assert alert.event == EVENT
    assert alert.next_steps == "Pay duties online"


@pytest.mark.parametrize("code", ["DF", None])
def test_build_alert_rejects_an_event_that_is_not_a_problem(code):
    event = TrackingEvent(code, "Departed facility", EVENT.timestamp, None)

    with pytest.raises(ValueError, match="not a problem code"):
        build_alert(SHIPMENT, event, None)


def test_links():
    assert dhl_tracking_url("1000000001").endswith("tracking-id=1000000001")
    assert odoo_record_url(ODOO_URL, "stock.picking", 4930) == "https://odoo.example.com/odoo/stock.picking/4930"


# --- Mattermost -------------------------------------------------------------

def test_mattermost_payload_summarises_the_alert():
    payload = mattermost_payload(make_alert(), ODOO_URL, ticket_id=77)

    assert "1000000001" in payload["text"]
    assert "[HP] Held for payment" in payload["text"]
    attachment = payload["attachments"][0]
    assert attachment["color"] == FAMILIES["customs"].color
    assert attachment["title_link"] == dhl_tracking_url("1000000001")
    fields = {field["title"]: field["value"] for field in attachment["fields"]}
    assert fields["Customer"] == "Example Broadcast & Co"
    assert "https://odoo.example.com/odoo/stock.picking/4930" in fields["Delivery"]
    assert "https://odoo.example.com/odoo/helpdesk.ticket/77" in fields["Helpdesk"]
    assert fields["Next steps"] == "Pay duties online"


def test_mattermost_payload_neutralises_markdown_and_mentions_from_outside_text():
    shipment = {**SHIPMENT, "partner_name": "@all [Acme](https://evil.example)"}
    event = TrackingEvent("HP", "Call *now* @channel", "2026-10-06T05:51:00-04:00", "HUB_1")

    payload = mattermost_payload(make_alert(event=event, shipment=shipment), ODOO_URL)

    attachment = payload["attachments"][0]
    rendered = "\n".join([payload["text"], attachment["title"], *(field["value"] for field in attachment["fields"])])
    shown = rendered + "\n" + attachment["fallback"]  # fallback is plain text: no Markdown, but mentions still notify
    assert "@all" not in shown and "@channel" not in shown
    assert "[Acme](https://evil.example)" not in rendered
    assert r"Call \*now\*" in shown


def test_mattermost_payload_without_ticket_or_next_steps():
    payload = mattermost_payload(make_alert(next_steps=None), ODOO_URL)

    titles = {field["title"] for field in payload["attachments"][0]["fields"]}
    assert "Helpdesk" not in titles
    assert "Next steps" not in titles


# --- Helpdesk ticket --------------------------------------------------------

def test_ticket_values_link_customer_and_sale_order():
    values = ticket_values(make_alert(), team_id=3, tag_ids=[79], odoo_url=ODOO_URL)

    assert values["name"] == "DHL 1000000001 [HP] Held for payment - Example Broadcast & Co"
    assert values["team_id"] == 3
    assert values["partner_id"] == 7538
    assert values["sale_order_id"] == 3520
    assert values["priority"] == FAMILIES["customs"].priority
    assert values["tag_ids"] == [[6, 0, [79]]]


def test_ticket_description_escapes_dhl_text():
    description = ticket_values(make_alert(), team_id=3, tag_ids=[], odoo_url=ODOO_URL)["description"]

    assert "&lt;pending duty payment&gt;" in description
    assert "<pending" not in description
    assert "Example Broadcast &amp; Co" in description
    assert escape(dhl_tracking_url("1000000001")) in description


def test_ticket_values_without_partner_or_sale_order():
    shipment = {**SHIPMENT, "partner_id": False, "sale_order_id": None, "sale_order_name": None}

    values = ticket_values(make_alert(shipment=shipment), team_id=3, tag_ids=[], odoo_url=ODOO_URL)

    assert "partner_id" not in values
    assert "sale_order_id" not in values
    assert values["tag_ids"] == [[6, 0, []]]


def test_ticket_note_escapes_and_names_the_code():
    note = ticket_note(make_alert())

    assert "[HP] Held for payment" in note
    assert "&lt;pending duty payment&gt;" in note
