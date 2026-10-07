from datetime import datetime, timezone

import pytest

from shipment_sync import ShipmentSync, build_from_env

SHIPMENT = {
    "tracking_number": "1000000001",
    "shipment_ref": "SH0000-00001",
    "picking_id": 4930,
    "partner_id": 7538,
    "partner_name": "Example Broadcast",
    "sale_order_id": None,
    "sale_order_name": None,
    "last_status": "Status: [DF] Departed",
}


def dhl_data(status_code, description, code=None, next_steps=None):
    status = {"statusCode": status_code, "description": description}
    if next_steps:
        status["nextSteps"] = next_steps
    events = [{"timestamp": "2026-10-06T05:51:00-04:00", "statusCode": status_code, "status": code, "description": description}]
    return {"shipments": [{"id": "1000000001", "status": status, "events": events if code else []}]}


class FakeOdoo:
    def __init__(self, write_ok=True):
        self.write_ok = write_ok
        self.updates = []

    def update_delivery_status(self, tracking_number, delivered=True, current_status=None, next_steps=None, event_code=None):
        self.updates.append({
            "tracking_number": tracking_number, "delivered": delivered,
            "current_status": current_status, "next_steps": next_steps, "event_code": event_code,
        })
        return self.write_ok


class FakeDispatcher:
    def __init__(self, succeeds=True):
        self.succeeds = succeeds
        self.alerts = []

    def dispatch(self, alert):
        self.alerts.append(alert)
        return self.succeeds


NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


def make_sync(dispatcher_succeeds=True, now=NOW, write_ok=True):
    odoo, dispatcher = FakeOdoo(write_ok), FakeDispatcher(dispatcher_succeeds)
    return ShipmentSync(odoo, dispatcher, clock=lambda: now), odoo, dispatcher


def test_delivered_shipment_is_flagged():
    sync, odoo, dispatcher = make_sync()

    result = sync.apply(SHIPMENT, dhl_data("delivered", "Delivered", code="OK"))

    assert result.delivered is True
    assert result.written is True
    assert odoo.updates == [{"tracking_number": "1000000001", "delivered": True,
                             "current_status": None, "next_steps": None, "event_code": None}]
    assert dispatcher.alerts == []


def test_in_transit_status_is_written_with_its_event_code():
    sync, odoo, dispatcher = make_sync()

    result = sync.apply(SHIPMENT, dhl_data("transit", "Processed", code="PL", next_steps="Wait"))

    assert (result.status, result.next_steps, result.event_code, result.alerted) == ("Processed", "Wait", "PL", False)
    assert odoo.updates[0]["event_code"] == "PL"
    assert odoo.updates[0]["current_status"] == "Processed"
    assert dispatcher.alerts == []


def test_new_action_code_raises_an_alert_and_records_the_code():
    sync, odoo, dispatcher = make_sync()

    result = sync.apply(SHIPMENT, dhl_data("transit", "On hold for payment", code="HP", next_steps="Pay duties"))

    assert result.alerted is True
    assert dispatcher.alerts[0].alert_code.code == "HP"
    assert dispatcher.alerts[0].next_steps == "Pay duties"
    assert odoo.updates[0]["event_code"] == "HP"


def test_alert_names_the_dhl_number_that_has_the_problem():
    sync, _, dispatcher = make_sync()
    data = dhl_data("transit", "On hold for payment", code="HP")
    data = {"shipments": [{**data["shipments"][0], "id": "1000000003"}]}

    sync.apply({**SHIPMENT, "tracking_number": "1000000002,1000000003"}, data)

    assert dispatcher.alerts[0].tracking_number == "1000000003"


def test_failed_alert_leaves_the_code_out_so_it_is_retried():
    sync, odoo, _ = make_sync(dispatcher_succeeds=False)

    result = sync.apply(SHIPMENT, dhl_data("transit", "On hold for payment", code="HP"))

    assert result.alerted is False
    assert odoo.updates[0]["event_code"] is None


def test_same_action_code_is_not_alerted_twice():
    sync, odoo, dispatcher = make_sync()

    sync.apply({**SHIPMENT, "last_status": "Status: [HP] On hold"}, dhl_data("transit", "On hold", code="HP"))

    assert dispatcher.alerts == []
    assert odoo.updates[0]["event_code"] == "HP"


def test_old_action_event_is_recorded_without_alerting():
    sync, odoo, dispatcher = make_sync(now=datetime(2026, 12, 1, tzinfo=timezone.utc))

    result = sync.apply(SHIPMENT, dhl_data("transit", "Returned to consignor", code="RT"))

    assert result.alerted is False
    assert dispatcher.alerts == []
    assert odoo.updates[0]["event_code"] == "RT"


def test_delivered_with_damage_alerts_before_closing():
    sync, odoo, dispatcher = make_sync()

    result = sync.apply(SHIPMENT, dhl_data("delivered", "Delivered damaged", code="DD"))

    assert result.delivered is True and result.alerted is True
    assert dispatcher.alerts[0].alert_code.code == "DD"
    assert odoo.updates[0]["delivered"] is True


@pytest.mark.parametrize("status_code", [None, 429, 500, 503])
def test_transient_dhl_errors_keep_the_last_known_odoo_status(status_code):
    sync, odoo, dispatcher = make_sync()

    result = sync.apply(SHIPMENT, {"error": True, "status_code": status_code, "message": "x"})

    assert odoo.updates == []
    assert result.delivered is False
    assert result.written is None  # nothing written, which is not a refused write
    assert dispatcher.alerts == []


def test_failed_odoo_write_keeps_a_delivered_shipment_tracked():
    sync, odoo, _ = make_sync(write_ok=False)

    result = sync.apply(SHIPMENT, dhl_data("delivered", "Delivered", code="OK"))

    assert odoo.updates[0]["delivered"] is True
    assert result.delivered is False
    assert result.written is False


def test_failed_odoo_write_of_a_status_is_reported():
    sync, _, _ = make_sync(write_ok=False)

    result = sync.apply(SHIPMENT, dhl_data("transit", "Processed", code="PL"))

    assert result.written is False


def test_alert_sent_before_a_failed_odoo_write_is_not_repeated():
    sync, _, dispatcher = make_sync(write_ok=False)
    data = dhl_data("transit", "On hold for payment", code="HP")

    sync.apply(SHIPMENT, data)
    sync.apply(SHIPMENT, data)

    assert len(dispatcher.alerts) == 1


def test_dhl_error_never_replaces_a_known_status():
    sync, odoo, _ = make_sync()

    result = sync.apply({**SHIPMENT, "last_status": "Status: [HP] On hold"}, {"error": True, "status_code": 404})

    assert result.status == "Not Found"
    assert odoo.updates == []


def test_shipment_without_new_event_keeps_its_code():
    sync, odoo, dispatcher = make_sync()

    sync.apply({**SHIPMENT, "last_status": "Status: [HP] On hold"}, dhl_data("transit", "Information received"))

    assert odoo.updates[0]["event_code"] == "HP"
    assert dispatcher.alerts == []


def test_failed_alert_on_a_delivered_shipment_keeps_it_tracked():
    sync, odoo, _ = make_sync(dispatcher_succeeds=False)

    result = sync.apply(SHIPMENT, dhl_data("delivered", "Delivered damaged", code="DD"))

    assert result.alerted is False
    assert odoo.updates[0]["delivered"] is False
    assert odoo.updates[0]["event_code"] is None


def test_pickings_sharing_a_tracking_number_alert_once():
    sync, odoo, dispatcher = make_sync()
    data = dhl_data("transit", "On hold for payment", code="HP")

    sync.apply(SHIPMENT, data)
    sync.apply({**SHIPMENT, "picking_id": 4931, "shipment_ref": "SH0000-00002"}, data)

    assert len(dispatcher.alerts) == 1
    assert [update["event_code"] for update in odoo.updates] == ["HP", "HP"]


def test_unknown_tracking_number_is_written_to_odoo():
    sync, odoo, _ = make_sync()

    result = sync.apply({**SHIPMENT, "last_status": ""}, {"error": True, "status_code": 404, "message": "x"})

    assert result.status == "Not Found"
    assert odoo.updates[0]["current_status"] == "Not Found"
    assert odoo.updates[0]["event_code"] is None


def test_build_from_env_wires_the_odoo_client_and_dispatcher(monkeypatch):
    monkeypatch.setenv("ODOO_URL", "https://odoo.example.com")
    monkeypatch.setenv("ODOO_API_KEY", "k")
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)

    odoo_client, sync = build_from_env()

    assert sync.odoo_client is odoo_client
    assert sync.dispatcher.odoo_url == "https://odoo.example.com"
    assert sync._clock().tzinfo is not None
