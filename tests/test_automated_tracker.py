from datetime import datetime, timezone

from automated_tracker import AutomatedTracker, WebhookSender
from shipment_sync import ShipmentSync


def dhl_data(status_code, description, code, next_steps=None):
    status = {"statusCode": status_code, "description": description, **({"nextSteps": next_steps} if next_steps else {})}
    events = [{"timestamp": "2026-10-06T05:51:00-04:00", "statusCode": status_code, "status": code, "description": description}]
    return {"shipments": [{"id": "x", "status": status, "events": events}]}


IN_TRANSIT = dhl_data("transit", "Processed", "PL")


class FakeOdoo:
    def __init__(self, shipments):
        self.shipments = shipments
        self.updates = []

    def connect(self):
        return True

    def get_recent_shipments(self, limit, since=None):
        return self.shipments

    def update_delivery_status(self, tracking_number, delivered=True, current_status=None, next_steps=None, event_code=None):
        self.updates.append((tracking_number, delivered, current_status, next_steps, event_code))
        return True

    def expire_stale_tracking(self, older_than):
        self.expired_before = older_than
        return 0


class FakeDHL:
    def __init__(self, responses, rate_limited_on=None):
        self.responses = responses
        self.rate_limited_on = rate_limited_on
        self.rate_limited = False
        self.tracked = []

    def track_reference(self, tracking_number):
        self.tracked.append(tracking_number)
        if tracking_number == self.rate_limited_on:
            self.rate_limited = True
            return {"error": True, "status_code": 429, "message": "Too many requests"}
        data = self.responses[tracking_number]
        # Like DHL, report the tracked number as the shipment id
        return {"shipments": [{**data["shipments"][0], "id": tracking_number}]}


class FakeWebhook:
    def __init__(self):
        self.summaries = []
        self.reports = []

    def send_webhook(self, data, is_startup=False):
        self.summaries.append(data)
        return True

    def send_webhook_detailed_report(self, data):
        self.reports.append(data)
        return True


class FakeDispatcher:
    def __init__(self):
        self.alerts = []

    def dispatch(self, alert):
        self.alerts.append(alert)
        return True


def shipment(tracking_number, last_status=""):
    return {
        "tracking_number": tracking_number, "partner_name": "Acme", "partner_id": 42, "shipment_ref": "WH/OUT/1",
        "picking_id": 1, "sale_order_id": None, "sale_order_name": None, "last_status": last_status,
    }


def make_tracker(shipments, responses, rate_limited_on=None):
    odoo = FakeOdoo(shipments)
    dispatcher = FakeDispatcher()
    tracker = AutomatedTracker(
        odoo_client=odoo,
        dhl_tracker=FakeDHL(responses, rate_limited_on),
        webhook_sender=FakeWebhook(),
        shipment_sync=ShipmentSync(odoo, dispatcher, clock=lambda: datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)),
    )
    return tracker, odoo, dispatcher


def test_hourly_check_updates_odoo_and_reports():
    tracker, odoo, _ = make_tracker(
        [shipment("A"), shipment("B")],
        {"A": dhl_data("delivered", "Delivered", "OK"), "B": dhl_data("transit", "In transit", "DF", "Customs clearance")},
    )

    tracker.hourly_detailed_check()

    assert odoo.updates == [
        ("A", True, None, None, None),
        ("B", False, "In transit", "Customs clearance", "DF"),
    ]
    assert "A" in tracker.last_delivered_shipments
    summary = tracker.webhook_sender.summaries[0]["summary"]
    assert (summary["newly_delivered"], summary["in_transit"], summary["postponed"]) == (1, 1, 0)
    assert len(tracker.webhook_sender.reports) == 1
    assert tracker.odoo_client.expired_before is not None


def test_hourly_check_alerts_on_a_new_action_code_once():
    tracker, odoo, dispatcher = make_tracker(
        [shipment("A"), shipment("B", last_status="Status: [HP] On hold")],
        {"A": dhl_data("transit", "On hold", "HP"), "B": dhl_data("transit", "On hold", "HP")},
    )

    tracker.hourly_detailed_check()

    assert [alert.tracking_number for alert in dispatcher.alerts] == ["A"]
    assert [update[4] for update in odoo.updates] == ["HP", "HP"]


def test_hourly_check_stops_at_dhl_rate_limit_without_overwriting_odoo():
    tracker, odoo, _ = make_tracker(
        [shipment("A"), shipment("B"), shipment("C")], {"A": IN_TRANSIT, "C": IN_TRANSIT}, rate_limited_on="B"
    )

    tracker.hourly_detailed_check()

    assert tracker.dhl_tracker.tracked == ["A", "B"]
    assert [update[0] for update in odoo.updates] == ["A"]
    summary = tracker.webhook_sender.summaries[0]["summary"]
    assert (summary["in_transit"], summary["postponed"]) == (1, 2)


def test_mattermost_message_mentions_postponed_shipments_only_when_any():
    base = {"total_shipments": 3, "in_transit": 1, "newly_delivered": 0}

    partial = WebhookSender().format_mattermost_message({"summary": {**base, "postponed": 2}})
    complete = WebhookSender().format_mattermost_message({"summary": {**base, "postponed": 0}})

    assert "Postponed (DHL rate limit): 2" in partial["text"]
    assert "Postponed" not in complete["text"]
