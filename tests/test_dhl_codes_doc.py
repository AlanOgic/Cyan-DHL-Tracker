import re

import dhl_codes_doc
from alert_dispatch import DEFAULT_TICKET_SKIP_CODES
from odoo_client import format_status_text
from shipment_alerts import ALERT_CODES, FAMILIES


def test_committed_page_matches_the_tracker_code():
    committed = dhl_codes_doc.OUTPUT_PATH.read_text(encoding="utf-8")

    assert committed == dhl_codes_doc.render(), "docs are stale: run `python dhl_codes_doc.py`"


def test_page_is_the_english_reference():
    assert dhl_codes_doc.OUTPUT_PATH.name == "dhl-tracking-codes.md"
    assert dhl_codes_doc.render().startswith("# DHL tracking codes\n")


def test_family_titles_are_the_labels_used_in_mattermost_alerts():
    assert {key: dhl_codes_doc.GROUP_TITLES[key] for key in FAMILIES} == {
        key: family.label for key, family in FAMILIES.items()
    }


def test_catalogue_lists_each_express_event_code_once():
    codes = [event.code for event in dhl_codes_doc.EVENT_CODES]

    assert len(codes) == len(set(codes)) == dhl_codes_doc.EXPRESS_EVENT_CODE_COUNT
    assert {event.group for event in dhl_codes_doc.EVENT_CODES} == set(dhl_codes_doc.GROUP_TITLES)


def test_every_alert_code_sits_in_its_alert_family():
    groups = {event.code: event.group for event in dhl_codes_doc.EVENT_CODES}

    assert {code: groups.get(code) for code in ALERT_CODES} == {
        code: alert.family.key for code, alert in ALERT_CODES.items()
    }


EXPECTED_PRIORITY = {"customs": "High", "delivery": "High", "return": "Urgent", "incident": "Medium"}


def test_alert_column_follows_the_tracker_rules():
    assert dhl_codes_doc.alert_label("DF") == "—"
    assert dhl_codes_doc.alert_label("OH") == "Mattermost"
    assert dhl_codes_doc.alert_label("HP") == "**Ticket** · High"
    for code, alert in ALERT_CODES.items():
        expected = (
            "Mattermost" if code in DEFAULT_TICKET_SKIP_CODES
            else f"**Ticket** · {EXPECTED_PRIORITY[alert.family.key]}"
        )
        assert dhl_codes_doc.alert_label(code) == expected, code


def test_priority_table_lists_each_ticket_code_under_its_family():
    page = dhl_codes_doc.render()

    for family in FAMILIES.values():
        codes = " ".join(
            f"`{code}`" for code, alert in ALERT_CODES.items()
            if alert.family == family and code not in DEFAULT_TICKET_SKIP_CODES
        )
        title = dhl_codes_doc.GROUP_TITLES[family.key]
        assert f"| {title} | {EXPECTED_PRIORITY[family.key]} | `{family.color}` | {codes} |" in page


def test_page_shows_the_status_marker_exactly_as_the_tracker_writes_it():
    example = format_status_text(*dhl_codes_doc.STATUS_EXAMPLE)

    assert f"```\n{example}\n```" in dhl_codes_doc.render()


def test_page_lists_each_family_colour_for_gitlab_swatches():
    page = dhl_codes_doc.render()

    assert all(f"`{family.color}`" in page for family in FAMILIES.values())


def test_page_uses_no_real_tracking_number():
    numbers = set(re.findall(r"\b\d{10}\b", dhl_codes_doc.render()))

    assert numbers <= {dhl_codes_doc.EXAMPLE_TRACKING_NUMBER}


def test_main_writes_the_page(tmp_path):
    output = tmp_path / "codes.md"

    dhl_codes_doc.main(output)

    assert output.read_text(encoding="utf-8") == dhl_codes_doc.render()
