from datetime import datetime

import pytest

from odoo_client import OdooClient, format_status_text
from odoo_json2 import Json2Client, OdooConfigError, OdooError

BASE_DOMAIN = [
    ["carrier_tracking_ref", "!=", False],
    ["carrier_id.name", "ilike", "DHL"],
    ["state", "=", "done"],
]


class FakeTransport:
    def __init__(self, responses=None, error=None):
        self.responses = responses or {}
        self.error = error
        self.calls = []

    def call(self, model, method, ids=None, **kwargs):
        self.calls.append({"model": model, "method": method, "ids": ids, "kwargs": kwargs})
        if self.error:
            raise self.error
        return self.responses[(model, method)]


def make_client(responses=None, error=None):
    transport = FakeTransport(responses, error)
    return OdooClient(transport=transport), transport


PICKING = {
    "id": 4930,
    "carrier_tracking_ref": "1234567890",
    "partner_id": [42, "Acme Broadcast"],
    "name": "WH/OUT/00042",
    "date_done": "2026-09-30 08:15:00",
    "sale_id": [3520, "SO0000-0001"],
    "x_studio_last_status": "Status: [DF] Departed",
}


# --- construction & connect -------------------------------------------------

def test_default_construction_reads_settings_from_environment(monkeypatch):
    monkeypatch.setenv("ODOO_URL", "https://odoo.example.com")
    monkeypatch.setenv("ODOO_API_KEY", "k")

    client = OdooClient()

    assert isinstance(client._transport, Json2Client)


def test_default_construction_fails_fast_without_settings(monkeypatch):
    monkeypatch.delenv("ODOO_URL", raising=False)
    monkeypatch.delenv("ODOO_API_KEY", raising=False)

    with pytest.raises(OdooConfigError):
        OdooClient()


def test_connect_checks_the_api_key():
    client, transport = make_client({("res.users", "context_get"): {"uid": 2, "lang": "en_US"}})

    assert client.connect() is True
    assert transport.calls[0]["model"] == "res.users"
    assert transport.calls[0]["method"] == "context_get"


def test_connect_returns_false_when_odoo_rejects_the_key():
    client, _ = make_client(error=OdooError("Invalid apikey", status_code=401))

    assert client.connect() is False


# --- get_recent_shipments ---------------------------------------------------

def test_get_recent_shipments_maps_pickings():
    client, transport = make_client({("stock.picking", "search_read"): [PICKING]})

    shipments = client.get_recent_shipments(limit=5)

    assert shipments == [{
        "tracking_number": "1234567890",
        "partner_id": 42,
        "partner_name": "Acme Broadcast",
        "shipment_ref": "WH/OUT/00042",
        "date_done": "2026-09-30 08:15:00",
        "picking_id": 4930,
        "sale_order_id": 3520,
        "sale_order_name": "SO0000-0001",
        "last_status": "Status: [DF] Departed",
    }]
    kwargs = transport.calls[0]["kwargs"]
    assert kwargs["domain"] == [
        *BASE_DOMAIN, ["x_studio_delivered_", "=", False], ["x_studio_last_status", "not like", "Status: Tracking expired"]
    ]
    assert kwargs["fields"] == [
        "carrier_tracking_ref", "partner_id", "name", "date_done", "sale_id", "x_studio_last_status"
    ]
    assert kwargs["limit"] == 5
    assert kwargs["order"] == "date_done desc"


def test_get_recent_shipments_filters_on_date_done_when_since_given():
    client, transport = make_client({("stock.picking", "search_read"): []})

    client.get_recent_shipments(limit=100, since=datetime(2026, 7, 8, 9, 30, 0))

    assert ["date_done", ">=", "2026-07-08 09:30:00"] in transport.calls[0]["kwargs"]["domain"]


def test_get_recent_shipments_handles_pickings_without_partner():
    client, _ = make_client({("stock.picking", "search_read"): [{**PICKING, "partner_id": False}]})

    shipment = client.get_recent_shipments()[0]

    assert shipment["partner_id"] is False
    assert shipment["partner_name"] == "Unknown"


def test_get_recent_shipments_handles_pickings_without_sale_order_or_status():
    client, _ = make_client({("stock.picking", "search_read"): [{**PICKING, "sale_id": False, "x_studio_last_status": False}]})

    shipment = client.get_recent_shipments()[0]

    assert shipment["sale_order_id"] is None
    assert shipment["sale_order_name"] is None
    assert shipment["last_status"] == ""


def test_expire_stale_tracking_flags_old_undelivered_pickings():
    client, transport = make_client({("stock.picking", "search"): [4, 5, 6], ("stock.picking", "write"): True})

    assert client.expire_stale_tracking(older_than=datetime(2026, 7, 8)) == 3

    search, write = transport.calls
    assert search["kwargs"]["domain"] == [
        *BASE_DOMAIN,
        ["x_studio_delivered_", "=", False],
        ["date_done", "<", "2026-07-08 00:00:00"],
        ["x_studio_last_status", "not like", "Status: Tracking expired"],
    ]
    assert write["ids"] == [4, 5, 6]
    assert write["kwargs"]["vals"]["x_studio_last_status"].startswith("Status: Tracking expired")


def test_expire_stale_tracking_without_candidates_writes_nothing():
    client, transport = make_client({("stock.picking", "search"): []})

    assert client.expire_stale_tracking(older_than=datetime(2026, 7, 8)) == 0
    assert len(transport.calls) == 1


def test_expire_stale_tracking_returns_zero_on_error():
    client, _ = make_client(error=OdooError("boom"))

    assert client.expire_stale_tracking(older_than=datetime(2026, 7, 8)) == 0


def test_get_recent_shipments_returns_none_on_error():
    # None, not []: an Odoo failure must not look like "no shipment to track"
    client, _ = make_client(error=OdooError("boom"))

    assert client.get_recent_shipments() is None


# --- get_delivered_tracking_refs --------------------------------------------

def test_get_delivered_tracking_refs_returns_tracking_numbers():
    records = [{"carrier_tracking_ref": "A1"}, {"carrier_tracking_ref": "B2"}]
    client, transport = make_client({("stock.picking", "search_read"): records})

    refs = client.get_delivered_tracking_refs(limit=1000, since=datetime(2026, 7, 8))

    assert refs == {"A1", "B2"}
    kwargs = transport.calls[0]["kwargs"]
    assert kwargs["domain"] == [
        *BASE_DOMAIN,
        ["x_studio_delivered_", "=", True],
        ["date_done", ">=", "2026-07-08 00:00:00"],
    ]
    assert kwargs["fields"] == ["carrier_tracking_ref"]
    assert kwargs["limit"] == 1000
    assert kwargs["order"] == "date_done desc"


def test_get_delivered_tracking_refs_returns_empty_set_on_error():
    client, _ = make_client(error=OdooError("boom"))

    assert client.get_delivered_tracking_refs(limit=10) == set()


# --- update_delivery_status -------------------------------------------------

def test_update_delivery_status_marks_delivered_and_clears_status():
    client, transport = make_client({("stock.picking", "search"): [7, 8], ("stock.picking", "write"): True})

    assert client.update_delivery_status("1234567890", delivered=True) is True

    search, write = transport.calls
    assert search["kwargs"]["domain"] == [
        ["carrier_tracking_ref", "=", "1234567890"],
        ["carrier_id.name", "ilike", "DHL"],
    ]
    assert write["ids"] == [7, 8]
    assert write["kwargs"]["vals"] == {"x_studio_delivered_": True, "x_studio_last_status": ""}


def test_update_delivery_status_writes_status_and_next_steps_in_transit():
    client, transport = make_client({("stock.picking", "search"): [7], ("stock.picking", "write"): True})

    result = client.update_delivery_status(
        "1234567890", delivered=False, current_status="In transit", next_steps="Customs clearance"
    )

    assert result is True
    assert transport.calls[1]["kwargs"]["vals"] == {
        "x_studio_last_status": "Status: In transit\nNext Steps: Customs clearance"
    }


def test_update_delivery_status_records_the_dhl_event_code():
    client, transport = make_client({("stock.picking", "search"): [7], ("stock.picking", "write"): True})

    client.update_delivery_status("1234567890", delivered=False, current_status="On hold", next_steps="Pay", event_code="HP")

    assert transport.calls[1]["kwargs"]["vals"] == {"x_studio_last_status": "Status: [HP] On hold\nNext Steps: Pay"}


def test_update_delivery_status_returns_false_when_no_picking_matches():
    client, transport = make_client({("stock.picking", "search"): []})

    assert client.update_delivery_status("unknown") is False
    assert len(transport.calls) == 1


def test_update_delivery_status_returns_false_on_error():
    client, _ = make_client(error=OdooError("boom"))

    assert client.update_delivery_status("1234567890") is False


@pytest.mark.parametrize(
    ("status", "next_steps", "expected"),
    [
        ("In transit", "Customs", "Status: In transit\nNext Steps: Customs"),
        ("In transit", None, "Status: In transit"),
        (None, "Customs", "Next Steps: Customs"),
        (None, None, ""),
    ],
)
def test_format_status_text(status, next_steps, expected):
    assert format_status_text(status, next_steps) == expected


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [("On hold", "HP", "Status: [HP] On hold"), (None, "HP", "Status: [HP]"), ("On hold", None, "Status: On hold")],
)
def test_format_status_text_prefixes_the_event_code(status, code, expected):
    assert format_status_text(status, None, event_code=code) == expected


# --- get_partner_info -------------------------------------------------------

PARTNER = {
    "id": 42,
    "name": "Acme Broadcast",
    "email": "ops@acme.example",
    "phone": "+32 2 000 00 00",
    "street": "Rue de l'Exemple 1",
    "city": "Brussels",
    "zip": "1000",
    "country_id": [21, "Belgium"],
}


def test_get_partner_info_by_id_adds_country_name():
    client, transport = make_client({("res.partner", "search_read"): [PARTNER]})

    partner = client.get_partner_info(partner_id=42)

    assert partner == {**PARTNER, "country": "Belgium"}
    assert "country" not in PARTNER
    kwargs = transport.calls[0]["kwargs"]
    assert kwargs["domain"] == [["id", "=", 42]]
    assert kwargs["limit"] == 1


def test_get_partner_info_by_name_uses_ilike():
    client, transport = make_client({("res.partner", "search_read"): [{**PARTNER, "country_id": False}]})

    partner = client.get_partner_info(name="acme")

    assert "country" not in partner
    assert transport.calls[0]["kwargs"]["domain"] == [["name", "ilike", "acme"]]


def test_get_partner_info_without_criteria_returns_none_without_calling_odoo():
    client, transport = make_client()

    assert client.get_partner_info() is None
    assert transport.calls == []


def test_get_partner_info_returns_none_when_not_found_or_on_error():
    not_found, _ = make_client({("res.partner", "search_read"): []})
    failing, _ = make_client(error=OdooError("boom"))

    assert not_found.get_partner_info(partner_id=1) is None
    assert failing.get_partner_info(partner_id=1) is None
