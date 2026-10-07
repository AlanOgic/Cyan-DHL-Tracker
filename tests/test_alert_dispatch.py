import pytest
import requests

from alert_dispatch import (
    DEFAULT_HELPDESK_TAGS,
    DEFAULT_HELPDESK_TEAM,
    DEFAULT_TICKET_SKIP_CODES,
    AlertConfigError,
    AlertDispatcher,
    build_alert_dispatcher,
)
from dhl_client import TrackingEvent
from shipment_alerts import build_alert

ODOO_URL = "https://odoo.example.com"
WEBHOOK = "https://chat.example.com/hooks/abc"

ALERT = build_alert(
    {
        "tracking_number": "1000000001",
        "shipment_ref": "SH0000-00001",
        "picking_id": 4930,
        "partner_id": 7538,
        "partner_name": "Example Broadcast",
        "sale_order_id": None,
        "sale_order_name": None,
        "last_status": "",
    },
    TrackingEvent("RD", "Refused delivery", "2026-10-06T10:00:00+02:00", "BRUSSELS"),
    None,
)


class FakeHelpdesk:
    def __init__(self, ticket_id=501):
        self.ticket_id = ticket_id
        self.alerts = []

    def raise_ticket(self, alert):
        self.alerts.append(alert)
        return self.ticket_id


class FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code
        self.text = "ok"


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response = response or FakeResponse()
        self.error = error
        self.posts = []

    def post(self, url, json=None, timeout=None):
        self.posts.append({"url": url, "json": json, "timeout": timeout})
        if self.error:
            raise self.error
        return self.response


def test_dispatch_opens_a_ticket_then_notifies_mattermost_with_its_link():
    helpdesk, session = FakeHelpdesk(), FakeSession()
    dispatcher = AlertDispatcher(helpdesk, WEBHOOK, ODOO_URL, session=session)

    assert dispatcher.dispatch(ALERT) is True

    assert helpdesk.alerts == [ALERT]
    post = session.posts[0]
    assert post["url"] == WEBHOOK
    assert post["timeout"] > 0
    fields = {field["title"]: field["value"] for field in post["json"]["attachments"][0]["fields"]}
    assert "helpdesk.ticket/501" in fields["Helpdesk"]


def test_dispatch_without_any_channel_succeeds_quietly():
    assert AlertDispatcher(None, None, ODOO_URL, session=FakeSession()).dispatch(ALERT) is True


def test_dispatch_counts_as_sent_when_one_channel_got_through():
    no_ticket = AlertDispatcher(FakeHelpdesk(ticket_id=None), WEBHOOK, ODOO_URL, session=FakeSession())
    no_mattermost = AlertDispatcher(FakeHelpdesk(), WEBHOOK, ODOO_URL, session=FakeSession(FakeResponse(500)))

    assert no_ticket.dispatch(ALERT) is True
    assert no_mattermost.dispatch(ALERT) is True


def test_dispatch_fails_when_every_channel_failed():
    dispatcher = AlertDispatcher(FakeHelpdesk(ticket_id=None), WEBHOOK, ODOO_URL, session=FakeSession(FakeResponse(500)))

    assert dispatcher.dispatch(ALERT) is False


@pytest.mark.parametrize("session", [FakeSession(FakeResponse(500)), FakeSession(error=requests.ConnectionError("down"))])
def test_dispatch_fails_when_mattermost_rejects_or_is_unreachable(session):
    assert AlertDispatcher(None, WEBHOOK, ODOO_URL, session=session).dispatch(ALERT) is False


def test_unreachable_mattermost_is_logged_without_the_webhook_key(caplog):
    # requests quotes the URL path in its errors, and a Mattermost webhook path is its secret key
    error = requests.ConnectionError(
        "HTTPSConnectionPool(host='chat.example.com', port=443): Max retries exceeded with url: /hooks/abc"
    )

    AlertDispatcher(None, WEBHOOK, ODOO_URL, session=FakeSession(error=error)).dispatch(ALERT)

    assert "ConnectionError" in caplog.text
    assert "/hooks/abc" not in caplog.text


def alert_with_code(code):
    return build_alert(
        {"tracking_number": "1", "shipment_ref": "SH1", "picking_id": 1, "partner_id": 7, "partner_name": "Acme",
         "sale_order_id": None, "sale_order_name": None, "last_status": ""},
        TrackingEvent(code, "x", "2026-10-06T10:00:00+02:00", None),
        None,
    )


def test_codes_without_ticket_still_reach_mattermost():
    helpdesk, session = FakeHelpdesk(), FakeSession()
    dispatcher = AlertDispatcher(helpdesk, WEBHOOK, ODOO_URL, session=session, ticket_skip_codes=frozenset({"NH"}))

    assert dispatcher.dispatch(alert_with_code("NH")) is True
    assert helpdesk.alerts == []
    fields = {field["title"] for field in session.posts[0]["json"]["attachments"][0]["fields"]}
    assert "Helpdesk" not in fields


def test_other_codes_still_open_a_ticket_when_some_are_skipped():
    helpdesk = FakeHelpdesk()
    dispatcher = AlertDispatcher(helpdesk, None, ODOO_URL, session=FakeSession(), ticket_skip_codes=frozenset({"NH"}))

    dispatcher.dispatch(alert_with_code("RD"))

    assert len(helpdesk.alerts) == 1


def test_code_without_ticket_and_no_webhook_counts_as_handled():
    dispatcher = AlertDispatcher(FakeHelpdesk(), None, ODOO_URL, session=FakeSession(), ticket_skip_codes=frozenset({"OH"}))

    assert dispatcher.dispatch(alert_with_code("OH")) is True


# --- configuration ----------------------------------------------------------

class Transport:
    pass


def test_build_uses_defaults_when_settings_are_absent():
    dispatcher = build_alert_dispatcher(Transport(), ODOO_URL, environ={})

    assert dispatcher.webhook_url is None
    assert dispatcher.ticket_skip_codes == DEFAULT_TICKET_SKIP_CODES == frozenset({"OH", "MD", "NH"})
    assert dispatcher.helpdesk.team_name == DEFAULT_HELPDESK_TEAM
    assert dispatcher.helpdesk.tag_names == DEFAULT_HELPDESK_TAGS


def test_build_reads_team_tags_and_webhook():
    dispatcher = build_alert_dispatcher(Transport(), ODOO_URL, environ={
        "HELPDESK_TEAM": " Support ",
        "HELPDESK_TAGS": "Shipping Related, delay ,,",
        "ALERT_WEBHOOK_URL": f" {WEBHOOK} ",
    })

    assert dispatcher.webhook_url == WEBHOOK
    assert dispatcher.helpdesk.team_name == "Support"
    assert dispatcher.helpdesk.tag_names == ("Shipping Related", "delay")


def test_build_reads_ticket_skip_codes():
    dispatcher = build_alert_dispatcher(Transport(), ODOO_URL, environ={"HELPDESK_SKIP_CODES": " oh, ss ,"})
    no_skip = build_alert_dispatcher(Transport(), ODOO_URL, environ={"HELPDESK_SKIP_CODES": ""})

    assert dispatcher.ticket_skip_codes == frozenset({"OH", "SS"})
    assert no_skip.ticket_skip_codes == frozenset()


def test_unknown_ticket_skip_code_fails_fast():
    with pytest.raises(AlertConfigError, match="XX"):
        build_alert_dispatcher(Transport(), ODOO_URL, environ={"HELPDESK_SKIP_CODES": "OH,XX"})


def test_empty_team_disables_tickets():
    assert build_alert_dispatcher(Transport(), ODOO_URL, environ={"HELPDESK_TEAM": ""}).helpdesk is None


@pytest.mark.parametrize("url", ["http://chat.example.com/hooks/abc", "chat.example.com/hooks/abc"])
def test_webhook_must_use_https(url):
    with pytest.raises(AlertConfigError, match="ALERT_WEBHOOK_URL"):
        build_alert_dispatcher(Transport(), ODOO_URL, environ={"ALERT_WEBHOOK_URL": url})
