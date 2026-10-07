import pytest
import requests

from dhl_client import (
    DHL_TRACKING_URL,
    MAX_SUSPENSION_SECONDS,
    RATE_LIMIT_COOLDOWN_SECONDS,
    RATE_LIMIT_PAUSE_SECONDS,
    DHLConfigError,
    DHLTracker,
    TrackingEvent,
    get_status_info,
    is_delivered,
    is_transient_error,
    latest_event,
    most_relevant,
    split_tracking_reference,
    summarize_status,
)

NO_JSON = object()


class FakeResponse:
    def __init__(self, status_code=200, json_body=None, text="", headers=None):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text
        self.headers = headers or {}

    def json(self):
        if self._json_body is NO_JSON:
            raise ValueError("Expecting value")
        return self._json_body


class FakeSession:
    def __init__(self, response=None, error=None, responses=None):
        self.response = response
        self.error = error
        self.responses = list(responses or [])
        self.calls = []
        self.on_get = None

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "params": params, "timeout": timeout})
        if self.on_get:
            self.on_get()
        if self.error:
            raise self.error
        if self.responses:
            item = self.responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return self.response


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def shipment_data(status_code, description="", status=None, next_steps=None):
    status_info = {"statusCode": status_code, "description": description}
    if status is not None:
        status_info["status"] = status
    if next_steps is not None:
        status_info["nextSteps"] = next_steps
    return {"shipments": [{"id": "1234567890", "service": "express", "status": status_info}]}


def make_tracker(response=None, error=None, clock=None, responses=None):
    session = FakeSession(response=response, error=error, responses=responses)
    clock = clock or FakeClock()
    tracker = DHLTracker(api_key="dhl-key", session=session, clock=clock, sleep=clock.sleep)
    return tracker, session, clock


# --- configuration ----------------------------------------------------------

def test_api_key_defaults_to_environment(monkeypatch):
    monkeypatch.setenv("DHL_API_KEY", "from-env")

    assert DHLTracker().api_key == "from-env"


def test_missing_api_key_fails_fast(monkeypatch):
    monkeypatch.delenv("DHL_API_KEY", raising=False)

    with pytest.raises(DHLConfigError, match="DHL_API_KEY"):
        DHLTracker()


# --- is_delivered -----------------------------------------------------------

def test_is_delivered_trusts_the_delivered_status_code():
    assert is_delivered(shipment_data("delivered", "Delivered")) is True


def test_is_delivered_status_code_check_ignores_case():
    assert is_delivered(shipment_data("Delivered", "")) is True


@pytest.mark.parametrize(
    ("status_code", "description"),
    [
        ("failure", "The shipment could not be delivered to the recipient"),
        ("failure", "The shipment could not be delivered and will be returned to the sender"),
        ("transit", "Not delivered"),
        ("transit", "Refused delivery"),
        ("transit", "Arrival in delivery facility"),
        ("ok", "Delivery"),
        ("unknown", ""),
    ],
)
def test_is_delivered_ignores_free_text_and_non_enum_codes(status_code, description):
    assert is_delivered(shipment_data(status_code, description)) is False


@pytest.mark.parametrize(
    "tracking_data",
    [
        {"error": True, "status_code": 404, "message": "Not found"},
        {"shipments": []},
        {"shipments": [{"id": "1"}]},
        {},
    ],
)
def test_is_delivered_is_false_without_a_status(tracking_data):
    assert is_delivered(tracking_data) is False


# --- get_status_info --------------------------------------------------------

def test_get_status_info_returns_description_and_next_steps():
    data = shipment_data("transit", "Departed from facility", next_steps="Customs clearance")

    assert get_status_info(data) == ("Departed from facility", "Customs clearance")


def test_get_status_info_falls_back_to_status_then_unknown():
    assert get_status_info(shipment_data("transit", "", status="102")) == ("102", None)
    assert get_status_info(shipment_data("transit", "")) == ("Unknown", None)


def test_get_status_info_is_empty_on_error_or_missing_data():
    assert get_status_info({"error": True, "status_code": 500}) == (None, None)
    assert get_status_info({"shipments": []}) == (None, None)


# --- track_shipment ---------------------------------------------------------

def test_track_shipment_queries_the_unified_api():
    tracker, session, _ = make_tracker(FakeResponse(json_body=shipment_data("transit")))

    result = tracker.track_shipment("1234567890")

    assert result == shipment_data("transit")
    call = session.calls[0]
    assert call["url"] == DHL_TRACKING_URL
    assert call["headers"] == {"DHL-API-Key": "dhl-key", "Accept": "application/json"}
    assert call["params"] == {"trackingNumber": "1234567890"}
    assert call["timeout"] > 0


def test_track_shipment_passes_service_when_given():
    tracker, session, _ = make_tracker(FakeResponse(json_body={}))

    tracker.track_shipment("1234567890", service="express")

    assert session.calls[0]["params"] == {"trackingNumber": "1234567890", "service": "express"}


def test_track_shipment_returns_error_dict_on_http_error():
    tracker, _, _ = make_tracker(FakeResponse(status_code=404, text="No shipment found"))

    assert tracker.track_shipment("000") == {"error": True, "status_code": 404, "message": "No shipment found"}


def test_track_shipment_returns_error_dict_on_network_error():
    tracker, _, _ = make_tracker(error=requests.ConnectionError("connection reset"))

    result = tracker.track_shipment("000")

    assert result["error"] is True
    assert result["status_code"] is None
    assert "connection reset" in result["message"]


def test_track_shipment_returns_error_dict_on_invalid_json():
    tracker, _, _ = make_tracker(FakeResponse(status_code=200, json_body=NO_JSON))

    result = tracker.track_shipment("000")

    assert result["error"] is True
    assert result["status_code"] == 200


def test_track_shipment_spaces_calls_by_the_dhl_rate_limit():
    tracker, _, clock = make_tracker(FakeResponse(json_body={}))

    tracker.track_shipment("A")
    clock.now += 2
    tracker.track_shipment("B")
    clock.now += 9
    tracker.track_shipment("C")

    assert clock.sleeps == [3]


def test_rate_limit_is_measured_from_the_end_of_the_previous_call():
    tracker, session, clock = make_tracker(FakeResponse(json_body={}))
    session.on_get = lambda: setattr(clock, "now", clock.now + 4)  # each DHL call takes 4 s

    tracker.track_shipment("A")
    tracker.track_shipment("B")

    assert clock.sleeps == [5]


def test_rate_limit_also_counts_failed_calls():
    tracker, _, clock = make_tracker(error=requests.ConnectionError("reset"))

    tracker.track_shipment("A")
    tracker.track_shipment("B")

    assert clock.sleeps == [5]


# --- latest_event / summarize_status / is_transient_error -------------------

def with_events(*events):
    return {"shipments": [{"id": "1", "status": {"statusCode": "transit"}, "events": list(events)}]}


def event(timestamp, code, description="", city=None):
    data = {"timestamp": timestamp, "statusCode": "transit", "status": code, "description": description}
    if city:
        data["location"] = {"address": {"addressLocality": city}}
    return data


def test_latest_event_picks_the_most_recent_whatever_the_order():
    data = with_events(
        event("2026-10-05T18:29:00-04:00", "RR"),
        event("2026-10-06T05:51:00-04:00", "DF", "Departed", "CINCINNATI HUB"),
        event("2026-10-06T11:00:00+02:00", "PL"),
    )

    assert latest_event(data) == TrackingEvent("DF", "Departed", "2026-10-06T05:51:00-04:00", "CINCINNATI HUB")


def test_latest_event_falls_back_to_dhl_order_when_timestamps_are_unreadable():
    data = with_events(event("yesterday", "WC"), event("2026-10-06T05:51:00-04:00", "DF"))

    assert latest_event(data).code == "WC"


def test_latest_event_handles_mixed_naive_and_aware_timestamps():
    data = with_events(event("2026-10-06T05:51:00", "WC"), event("2026-10-06T05:51:00-04:00", "DF"))

    assert latest_event(data).code == "WC"


@pytest.mark.parametrize(
    "tracking_data",
    [{"error": True, "status_code": 404}, {"shipments": []}, with_events(), {"shipments": [{"id": "1"}]}],
)
def test_latest_event_is_none_without_events(tracking_data):
    assert latest_event(tracking_data) is None


def test_summarize_status_matches_get_shipment_status():
    assert summarize_status(shipment_data("delivered", "Delivered", next_steps="x")) == ("Delivered", None, True)
    assert summarize_status({"error": True, "status_code": 404}) == ("Not Found", None, False)
    assert summarize_status({"shipments": []}) == ("No data", None, False)


@pytest.mark.parametrize(
    ("tracking_data", "expected"),
    [
        ({"error": True, "status_code": None}, True),
        ({"error": True, "status_code": 429}, True),
        ({"error": True, "status_code": 502}, True),
        ({"error": True, "status_code": 404}, False),
        ({"error": True, "status_code": 401}, False),
        ({"shipments": []}, False),
    ],
)
def test_is_transient_error(tracking_data, expected):
    assert is_transient_error(tracking_data) is expected


# --- references holding several DHL numbers ---------------------------------

@pytest.mark.parametrize(
    ("reference", "numbers"),
    [
        ("1000000002,1000000003", ("1000000002", "1000000003")),
        (" 1000000004 , 1000000005,1000000004,", ("1000000004", "1000000005")),
        ("22 4640 4554", ("22 4640 4554",)),
        ("", ()),
        (None, ()),
    ],
)
def test_split_tracking_reference(reference, numbers):
    assert split_tracking_reference(reference) == numbers


def found(number, status_code, timestamp, code="DF"):
    data = with_events(event(timestamp, code))
    shipment = {**data["shipments"][0], "id": number, "status": {"statusCode": status_code, "description": code}}
    return {"shipments": [shipment]}


def test_most_recent_shipment_decides_even_if_an_older_one_was_delivered():
    delivered_outbound = found("A", "delivered", "2026-09-01T10:00:00+02:00", "OK")
    return_in_transit = found("B", "transit", "2026-10-06T10:00:00+02:00")

    assert most_relevant([delivered_outbound, return_in_transit]) is return_in_transit


def test_unused_label_does_not_block_a_delivered_shipment():
    unused_label = {"error": True, "status_code": 404}
    delivered = found("B", "delivered", "2026-10-06T10:00:00+02:00", "OK")

    assert most_relevant([unused_label, delivered]) is delivered


def test_most_relevant_otherwise_takes_the_newest_event():
    older = found("A", "transit", "2026-10-01T10:00:00+02:00")
    newer = found("B", "transit", "2026-10-06T08:00:00-04:00")
    no_events = {"shipments": [{"id": "C", "status": {"statusCode": "pre-transit"}, "events": []}]}

    assert most_relevant([older, no_events, newer]) is newer


def test_most_relevant_compares_naive_and_aware_times():
    naive = found("A", "transit", "2026-10-06T09:00:00")
    aware = found("B", "transit", "2026-10-06T08:00:00+00:00")

    assert most_relevant([aware, naive]) is naive


def test_most_relevant_prefers_data_over_errors():
    data = found("B", "transit", "2026-10-06T10:00:00+02:00")

    assert most_relevant([{"error": True, "status_code": 404}, data]) is data


def test_one_unreadable_number_makes_the_whole_reference_wait():
    transient = {"error": True, "status_code": 503}

    assert most_relevant([found("A", "transit", "2026-10-06T10:00:00+02:00"), transient]) is transient


def test_most_relevant_keeps_transient_errors_over_not_found():
    transient = {"error": True, "status_code": 429}

    assert most_relevant([{"error": True, "status_code": 404}, transient]) is transient
    assert most_relevant([{"error": True, "status_code": 404, "message": "a"}])["message"] == "a"


def test_track_reference_tracks_every_number():
    tracker, session, _ = make_tracker(responses=[
        FakeResponse(json_body=found("A", "transit", "2026-10-01T10:00:00+02:00")),
        FakeResponse(json_body=found("B", "transit", "2026-10-06T10:00:00+02:00")),
    ])

    result = tracker.track_reference("A,B")

    assert [call["params"]["trackingNumber"] for call in session.calls] == ["A", "B"]
    assert result["shipments"][0]["id"] == "B"


def test_track_reference_stops_when_dhl_suspends_calls():
    tracker, session, _ = make_tracker(responses=[too_many_requests(), too_many_requests()])

    result = tracker.track_reference("A,B")

    assert len(session.calls) == 2
    assert result["status_code"] == 429


def test_track_reference_cut_short_by_the_rate_limit_returns_the_rate_limit():
    tracker, session, _ = make_tracker(responses=[
        FakeResponse(json_body=found("A", "transit", "2026-10-01T10:00:00+02:00")),
        too_many_requests(),
        too_many_requests(),
    ])

    result = tracker.track_reference("A,B,C")

    assert result["status_code"] == 429
    assert [call["params"]["trackingNumber"] for call in session.calls] == ["A", "B", "B"]


def test_track_reference_without_any_number_is_not_found():
    tracker, session, _ = make_tracker()

    assert tracker.track_reference(" , ")["status_code"] == 404
    assert session.calls == []


# --- HTTP 429 handling ------------------------------------------------------

def too_many_requests(retry_after=None):
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return FakeResponse(status_code=429, text="Too many requests", headers=headers)


def test_rate_limited_call_pauses_then_retries_once():
    data = shipment_data("transit", "Departed")
    tracker, session, clock = make_tracker(responses=[too_many_requests(), FakeResponse(json_body=data)])

    result = tracker.track_shipment("A")

    assert result == data
    assert len(session.calls) == 2
    assert clock.sleeps == [RATE_LIMIT_PAUSE_SECONDS]
    assert tracker.rate_limited is False


@pytest.mark.parametrize(("retry_after", "expected_pause"), [("12", 12), ("1", 5), ("soon", RATE_LIMIT_PAUSE_SECONDS), ("inf", RATE_LIMIT_PAUSE_SECONDS)])
def test_rate_limit_pause_follows_retry_after(retry_after, expected_pause):
    tracker, _, clock = make_tracker(responses=[too_many_requests(retry_after), FakeResponse(json_body={})])

    tracker.track_shipment("A")

    assert clock.sleeps == [expected_pause]


def test_second_rate_limit_starts_a_cooldown_without_further_calls():
    tracker, session, clock = make_tracker(responses=[too_many_requests(), too_many_requests()])

    first = tracker.track_shipment("A")
    clock.now += 60
    second = tracker.get_shipment_status("B")

    assert first["error"] is True and first["status_code"] == 429
    assert tracker.rate_limited is True
    assert second == ("Rate Limited", None, False)
    assert len(session.calls) == 2
    assert clock.sleeps == [RATE_LIMIT_PAUSE_SECONDS]


def test_calls_resume_once_the_cooldown_is_over():
    tracker, session, clock = make_tracker(
        responses=[too_many_requests(), too_many_requests(), FakeResponse(json_body={"shipments": []})]
    )
    tracker.track_shipment("A")

    clock.now += RATE_LIMIT_COOLDOWN_SECONDS

    assert tracker.rate_limited is False
    assert tracker.track_shipment("B") == {"shipments": []}
    assert len(session.calls) == 3


def test_long_retry_after_skips_the_inline_retry_and_is_respected():
    tracker, session, clock = make_tracker(responses=[too_many_requests("7200")])

    tracker.track_shipment("A")

    assert len(session.calls) == 1
    assert clock.sleeps == []
    clock.now += RATE_LIMIT_COOLDOWN_SECONDS
    assert tracker.rate_limited is True
    clock.now += 7200 - RATE_LIMIT_COOLDOWN_SECONDS
    assert tracker.rate_limited is False


def test_suspension_is_capped_whatever_retry_after_says():
    tracker, _, clock = make_tracker(responses=[too_many_requests("1e12")])

    tracker.track_shipment("A")

    clock.now += MAX_SUSPENSION_SECONDS
    assert tracker.rate_limited is False


def test_retry_that_fails_on_the_network_still_suspends_calls():
    tracker, session, _ = make_tracker(responses=[too_many_requests(), requests.Timeout("timed out")])

    result = tracker.track_shipment("A")

    assert result["status_code"] is None
    assert tracker.rate_limited is True
    assert tracker.get_shipment_status("B") == ("Rate Limited", None, False)
    assert len(session.calls) == 2


# --- get_shipment_status ----------------------------------------------------

def test_get_shipment_status_for_delivered_shipment():
    tracker, _, _ = make_tracker(FakeResponse(json_body=shipment_data("delivered", "Delivered", next_steps="None")))

    assert tracker.get_shipment_status("1") == ("Delivered", None, True)


def test_get_shipment_status_for_failed_delivery_keeps_tracking():
    data = shipment_data("failure", "The shipment could not be delivered", next_steps="Contact DHL")
    tracker, _, _ = make_tracker(FakeResponse(json_body=data))

    assert tracker.get_shipment_status("1") == ("The shipment could not be delivered", "Contact DHL", False)


@pytest.mark.parametrize(
    ("status_code", "label"),
    [(404, "Not Found"), (401, "Auth Error"), (429, "Rate Limited"), (500, "Error 500")],
)
def test_get_shipment_status_maps_http_errors_to_labels(status_code, label):
    tracker, _, _ = make_tracker(FakeResponse(status_code=status_code, text="err"))

    assert tracker.get_shipment_status("1") == (label, None, False)


def test_get_shipment_status_labels_network_errors():
    tracker, _, _ = make_tracker(error=requests.Timeout("timed out"))

    assert tracker.get_shipment_status("1") == ("Request Failed", None, False)


def test_get_shipment_status_without_shipment_data():
    tracker, _, _ = make_tracker(FakeResponse(json_body={"shipments": []}))

    assert tracker.get_shipment_status("1") == ("No data", None, False)
