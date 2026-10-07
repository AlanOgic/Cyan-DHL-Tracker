from datetime import datetime, timezone

import pytest
import requests
import schedule

import automated_tracker
from automated_tracker import AutomatedTracker, WebhookConfigError, WebhookSender, main
from heartbeat import HeartbeatConfigError
from shipment_sync import ShipmentSync


def dhl_data(status_code, description, code, next_steps=None):
    status = {"statusCode": status_code, "description": description, **({"nextSteps": next_steps} if next_steps else {})}
    events = [{"timestamp": "2026-10-06T05:51:00-04:00", "statusCode": status_code, "status": code, "description": description}]
    return {"shipments": [{"id": "x", "status": status, "events": events}]}


IN_TRANSIT = dhl_data("transit", "Processed", "PL")
WEBHOOK_URL = "https://chat.example.com/hooks/secret-key"


class FakeOdoo:
    def __init__(self, shipments, write_ok=True):
        self.shipments = shipments
        self.write_ok = write_ok
        self.updates = []

    def connect(self):
        return True

    def get_recent_shipments(self, limit, since=None):
        return self.shipments

    def get_delivered_tracking_refs(self, limit, since=None):
        return set()

    def update_delivery_status(self, tracking_number, delivered=True, current_status=None, next_steps=None, event_code=None):
        self.updates.append((tracking_number, delivered, current_status, next_steps, event_code))
        return self.write_ok

    def expire_stale_tracking(self, older_than):
        self.expired_before = older_than
        return 0


class BrokenOdoo(FakeOdoo):
    """Every call fails with an error the tracker does not expect."""

    def connect(self):
        raise RuntimeError("unexpected Odoo failure")

    def get_recent_shipments(self, limit, since=None):
        raise RuntimeError("unexpected Odoo failure")


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
        if data.get("error"):
            return data
        # Like DHL, report the tracked number as the shipment id
        return {"shipments": [{**data["shipments"][0], "id": tracking_number}]}


class FakeWebhook:
    def __init__(self):
        self.summaries = []
        self.simple = []
        self.reports = []

    def send_webhook(self, data, is_startup=False):
        self.summaries.append(data)
        return True

    def send_webhook_simple(self, data):
        self.simple.append(data)
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


class FakeHeartbeat:
    def __init__(self):
        self.successes = 0

    def record_success(self):
        self.successes += 1


def shipment(tracking_number, last_status=""):
    return {
        "tracking_number": tracking_number, "partner_name": "Acme", "partner_id": 42, "shipment_ref": "WH/OUT/1",
        "picking_id": 1, "sale_order_id": None, "sale_order_name": None, "last_status": last_status,
    }


def make_tracker(shipments, responses, rate_limited_on=None, odoo=None):
    odoo = odoo or FakeOdoo(shipments)
    dispatcher = FakeDispatcher()
    tracker = AutomatedTracker(
        odoo_client=odoo,
        dhl_tracker=FakeDHL(responses, rate_limited_on),
        webhook_sender=FakeWebhook(),
        shipment_sync=ShipmentSync(odoo, dispatcher, clock=lambda: datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)),
        heartbeat=FakeHeartbeat(),
    )
    return tracker, odoo, dispatcher


@pytest.fixture
def clean_schedule():
    schedule.clear()
    yield
    schedule.clear()


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
    assert tracker.heartbeat.successes == 1


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


def test_hourly_check_without_shipments_counts_as_a_success():
    tracker, _, _ = make_tracker([], {})

    tracker.hourly_detailed_check()

    assert tracker.heartbeat.successes == 1


def test_hourly_check_is_not_a_success_when_odoo_cannot_be_read():
    tracker, _, _ = make_tracker(None, {})

    tracker.hourly_detailed_check()

    assert tracker.dhl_tracker.tracked == []
    assert tracker.webhook_sender.summaries == []
    assert tracker.heartbeat.successes == 0


def test_hourly_check_is_not_a_success_when_every_odoo_write_fails():
    shipments = [shipment("A"), shipment("B")]
    tracker, _, _ = make_tracker(
        shipments,
        {"A": dhl_data("delivered", "Delivered", "OK"), "B": IN_TRANSIT},
        odoo=FakeOdoo(shipments, write_ok=False),
    )

    tracker.hourly_detailed_check()

    assert "A" not in tracker.last_delivered_shipments
    assert tracker.heartbeat.successes == 0


def test_refused_writes_are_not_masked_by_a_shipment_with_nothing_to_write():
    shipments = [shipment("A"), shipment("B")]
    tracker, _, _ = make_tracker(
        shipments,
        {"A": IN_TRANSIT, "B": {"error": True, "status_code": 503, "message": "DHL unavailable"}},
        odoo=FakeOdoo(shipments, write_ok=False),
    )

    tracker.hourly_detailed_check()

    assert tracker.heartbeat.successes == 0


def test_some_refused_writes_still_count_as_a_success():
    shipments = [shipment("A"), shipment("B")]
    odoo = FakeOdoo(shipments)
    odoo.update_delivery_status = lambda tracking_number, **kwargs: tracking_number == "A"
    tracker, _, _ = make_tracker(shipments, {"A": IN_TRANSIT, "B": IN_TRANSIT}, odoo=odoo)

    tracker.hourly_detailed_check()

    assert tracker.heartbeat.successes == 1


def test_hourly_check_stopped_by_the_dhl_rate_limit_still_counts_as_a_success():
    # DHL problems show in the logs; the heartbeat only says the tracker runs and Odoo works
    tracker, _, _ = make_tracker([shipment("A")], {}, rate_limited_on="A")

    tracker.hourly_detailed_check()

    assert tracker.heartbeat.successes == 1


def test_refused_odoo_write_is_logged(capsys):
    shipments = [shipment("A")]
    tracker, _, _ = make_tracker(shipments, {"A": IN_TRANSIT}, odoo=FakeOdoo(shipments, write_ok=False))

    tracker.hourly_detailed_check()

    assert "A - Odoo did not record the DHL status" in capsys.readouterr().out


def test_simple_check_reports_a_changed_count():
    tracker, _, _ = make_tracker([shipment("A"), shipment("B")], {})

    tracker.simple_check()
    tracker.simple_check()

    assert [data["summary"]["total_shipments"] for data in tracker.webhook_sender.simple] == [2]
    assert tracker.last_check_results["shipment_count"] == 2


def test_startup_loads_delivered_shipments_and_notifies():
    odoo = FakeOdoo([shipment("A")])
    odoo.get_delivered_tracking_refs = lambda limit, since=None: {"D1", "D2"}
    tracker, _, _ = make_tracker(None, {}, odoo=odoo)

    tracker.load_delivered_shipments()
    tracker.send_startup_notification()

    assert tracker.last_delivered_shipments == {"D1", "D2"}
    summary = tracker.webhook_sender.summaries[0]["summary"]
    assert (summary["total_shipments"], summary["in_transit"]) == (3, 1)


def test_startup_notification_is_skipped_when_odoo_cannot_be_read():
    tracker, _, _ = make_tracker(None, {})

    tracker.send_startup_notification()

    assert tracker.webhook_sender.summaries == []


def test_simple_check_skips_when_odoo_cannot_be_read():
    tracker, _, _ = make_tracker(None, {})
    tracker.last_check_results["shipment_count"] = 11

    tracker.simple_check()

    assert tracker.webhook_sender.simple == []
    assert tracker.last_check_results["shipment_count"] == 11


def test_failing_check_is_logged_and_does_not_stop_the_tracker(caplog):
    tracker, _, _ = make_tracker([], {})

    def hourly_detailed_check():
        raise KeyError("partner_name")

    tracker._run_safely(hourly_detailed_check)

    assert "hourly_detailed_check failed" in caplog.text
    assert "KeyError" in caplog.text


class StopScheduler(Exception):
    pass


def test_scheduler_survives_unexpected_errors(monkeypatch, clean_schedule):
    tracker, _, _ = make_tracker(None, {}, odoo=BrokenOdoo(None))

    def stop(seconds):
        raise StopScheduler

    monkeypatch.setattr(automated_tracker.time, "sleep", stop)

    with pytest.raises(StopScheduler):
        tracker.start_scheduler()  # the startup steps fail, the loop still starts

    jobs = schedule.get_jobs()
    assert len(jobs) == 2
    for job in jobs:
        job.run()  # the scheduled checks fail too, without escaping


def stub_tracker(error):
    class StubTracker:
        def __init__(self):
            if isinstance(error, HeartbeatConfigError):
                raise error

        def start_scheduler(self):
            raise error

    return StubTracker


def test_unexpected_crash_exits_with_an_error_code(monkeypatch, caplog):
    monkeypatch.setattr(automated_tracker, "AutomatedTracker", stub_tracker(RuntimeError("boom")))

    with pytest.raises(SystemExit) as stopped:
        main()

    assert stopped.value.code == 1
    assert "RuntimeError: boom" in caplog.text


def test_invalid_heartbeat_url_stops_at_startup(monkeypatch):
    monkeypatch.setattr(
        automated_tracker, "AutomatedTracker", stub_tracker(HeartbeatConfigError("HEARTBEAT_URL must be an https:// URL"))
    )

    with pytest.raises(SystemExit, match="Invalid configuration: HEARTBEAT_URL"):
        main()


def test_summary_webhook_must_use_https(monkeypatch):
    monkeypatch.setenv("WEBHOOK_URL", "http://chat.example.com/hooks/secret-key")

    with pytest.raises(WebhookConfigError, match="WEBHOOK_URL") as error:
        WebhookSender()

    assert "secret-key" not in str(error.value)


def test_invalid_summary_webhook_stops_at_startup(monkeypatch):
    monkeypatch.setenv("WEBHOOK_URL", "http://chat.example.com/hooks/secret-key")
    monkeypatch.setenv("DHL_API_KEY", "test-key")
    monkeypatch.setattr(automated_tracker, "build_from_env", lambda: (FakeOdoo([]), None))

    with pytest.raises(SystemExit, match="Invalid configuration: WEBHOOK_URL"):
        main()


def test_mattermost_message_mentions_postponed_shipments_only_when_any():
    base = {"total_shipments": 3, "in_transit": 1, "newly_delivered": 0}

    partial = WebhookSender().format_mattermost_message({"summary": {**base, "postponed": 2}})
    complete = WebhookSender().format_mattermost_message({"summary": {**base, "postponed": 0}})

    assert "Postponed (DHL rate limit): 2" in partial["text"]
    assert "Postponed" not in complete["text"]


class RejectedResponse:
    status_code = 403
    text = "invalid webhook secret-key"


def send_every_webhook(sender):
    return [
        sender.send_webhook({"summary": {}}),
        sender.send_webhook_simple({"summary": {"total_shipments": 1}}),
        sender.send_webhook_detailed_report({"shipments_with_next_steps": []}),
    ]


def test_unreachable_webhook_is_logged_without_its_key(monkeypatch, capsys):
    def unreachable(url, **kwargs):
        raise requests.ConnectionError("Max retries exceeded with url: /hooks/secret-key")

    monkeypatch.setenv("WEBHOOK_URL", WEBHOOK_URL)
    monkeypatch.setattr(automated_tracker.requests, "post", unreachable)

    results = send_every_webhook(WebhookSender())

    output = capsys.readouterr().out
    assert results == [False, False, False]
    assert "ConnectionError" in output
    assert "secret-key" not in output


def test_rejected_webhook_does_not_print_the_response_body(monkeypatch, capsys):
    monkeypatch.setenv("WEBHOOK_URL", WEBHOOK_URL)
    monkeypatch.setattr(automated_tracker.requests, "post", lambda url, **kwargs: RejectedResponse())

    results = send_every_webhook(WebhookSender())

    assert results == [False, False, False]
    assert "secret-key" not in capsys.readouterr().out
