from dhl_client import TrackingEvent
from odoo_helpdesk import HelpdeskClient
from odoo_json2 import OdooError
from shipment_alerts import build_alert

ODOO_URL = "https://odoo.example.com"

SHIPMENT = {
    "tracking_number": "1000000001",
    "shipment_ref": "SH0000-00001",
    "picking_id": 4930,
    "partner_id": 7538,
    "partner_name": "Example Broadcast",
    "sale_order_id": 3520,
    "sale_order_name": "SO0000-0001",
    "last_status": "",
}
ALERT = build_alert(SHIPMENT, TrackingEvent("HP", "On hold", "2026-10-06T05:51:00-04:00", "CINCINNATI"), None)


class FakeTransport:
    def __init__(self, responses=None, fail_on=()):
        self.responses = responses or {}
        self.fail_on = set(fail_on)
        self.calls = []

    def call(self, model, method, ids=None, **kwargs):
        self.calls.append({"model": model, "method": method, "ids": ids, "kwargs": kwargs})
        if (model, method) in self.fail_on:
            raise OdooError("boom")
        return self.responses[(model, method)]


def make_client(responses=None, fail_on=(), team="Logistics & Shipping", tags=("Shipping Related",)):
    transport = FakeTransport(responses, fail_on)
    return HelpdeskClient(transport, team_name=team, tag_names=tags, odoo_url=ODOO_URL), transport


TEAM_FOUND = {("helpdesk.team", "search_read"): [{"id": 3}], ("helpdesk.tag", "search_read"): [{"id": 79}]}


def calls_to(transport, model, method):
    return [call for call in transport.calls if call["model"] == model and call["method"] == method]


def test_creates_a_ticket_when_none_is_open():
    client, transport = make_client({**TEAM_FOUND, ("helpdesk.ticket", "search"): [], ("helpdesk.ticket", "create"): 501})

    assert client.raise_ticket(ALERT) == 501

    search = calls_to(transport, "helpdesk.ticket", "search")[0]
    assert search["kwargs"]["domain"] == [
        ["team_id", "=", 3],
        ["name", "ilike", "1000000001"],
        ["stage_id.fold", "=", False],
    ]
    vals = calls_to(transport, "helpdesk.ticket", "create")[0]["kwargs"]["vals_list"][0]
    assert vals["team_id"] == 3
    assert vals["tag_ids"] == [[6, 0, [79]]]
    assert vals["partner_id"] == 7538


def test_adds_an_internal_note_to_the_open_ticket_instead_of_a_duplicate():
    client, transport = make_client({**TEAM_FOUND, ("helpdesk.ticket", "search"): [42], ("helpdesk.ticket", "message_post"): 1})

    assert client.raise_ticket(ALERT) == 42

    assert calls_to(transport, "helpdesk.ticket", "create") == []
    post = calls_to(transport, "helpdesk.ticket", "message_post")[0]
    assert post["ids"] == [42]
    assert post["kwargs"]["subtype_xmlid"] == "mail.mt_note"
    assert post["kwargs"]["message_type"] == "comment"
    assert post["kwargs"]["body_is_html"] is True
    assert "[HP] Held for payment" in post["kwargs"]["body"]


def test_team_and_tags_are_looked_up_once():
    client, transport = make_client({**TEAM_FOUND, ("helpdesk.ticket", "search"): [42], ("helpdesk.ticket", "message_post"): 1})

    client.raise_ticket(ALERT)
    client.raise_ticket(ALERT)

    assert len(calls_to(transport, "helpdesk.team", "search_read")) == 1
    assert calls_to(transport, "helpdesk.team", "search_read")[0]["kwargs"]["domain"] == [["name", "=", "Logistics & Shipping"]]


def test_missing_team_means_no_ticket():
    client, transport = make_client({("helpdesk.team", "search_read"): []})

    assert client.raise_ticket(ALERT) is None
    assert calls_to(transport, "helpdesk.ticket", "create") == []


def test_unknown_tags_are_skipped():
    client, transport = make_client({
        ("helpdesk.team", "search_read"): [{"id": 3}],
        ("helpdesk.tag", "search_read"): [],
        ("helpdesk.ticket", "search"): [],
        ("helpdesk.ticket", "create"): 501,
    })

    client.raise_ticket(ALERT)

    assert calls_to(transport, "helpdesk.ticket", "create")[0]["kwargs"]["vals_list"][0]["tag_ids"] == [[6, 0, []]]


def test_no_tags_configured_skips_the_tag_lookup():
    client, transport = make_client(
        {("helpdesk.team", "search_read"): [{"id": 3}], ("helpdesk.ticket", "search"): [], ("helpdesk.ticket", "create"): 501},
        tags=(),
    )

    client.raise_ticket(ALERT)

    assert calls_to(transport, "helpdesk.tag", "search_read") == []


def test_odoo_errors_return_none():
    failing_create, _ = make_client({**TEAM_FOUND, ("helpdesk.ticket", "search"): []}, fail_on=[("helpdesk.ticket", "create")])
    failing_note, _ = make_client({**TEAM_FOUND, ("helpdesk.ticket", "search"): [42]}, fail_on=[("helpdesk.ticket", "message_post")])
    failing_team, _ = make_client(fail_on=[("helpdesk.team", "search_read")])

    assert failing_create.raise_ticket(ALERT) is None
    assert failing_note.raise_ticket(ALERT) is None
    assert failing_team.raise_ticket(ALERT) is None


def test_create_returning_a_list_of_ids_is_supported():
    client, _ = make_client({**TEAM_FOUND, ("helpdesk.ticket", "search"): [], ("helpdesk.ticket", "create"): [501]})

    assert client.raise_ticket(ALERT) == 501
