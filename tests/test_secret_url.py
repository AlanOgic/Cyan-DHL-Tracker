import pytest
import requests

from secret_url import https_url_setting, request_error_name


class SettingError(ValueError):
    pass


@pytest.mark.parametrize("environ", [{}, {"HOOK_URL": "  "}])
def test_unset_or_blank_setting_is_none(environ):
    assert https_url_setting(environ, "HOOK_URL", SettingError) is None


def test_https_url_is_returned_without_surrounding_spaces():
    environ = {"HOOK_URL": " https://chat.example.com/hooks/abc "}

    assert https_url_setting(environ, "HOOK_URL", SettingError) == "https://chat.example.com/hooks/abc"


@pytest.mark.parametrize("url", [
    "http://chat.example.com/hooks/abc",
    "chat.example.com/hooks/abc",
    "https:///hooks/abc",
    "https://:443/hooks/abc",
    "https://user:pw@chat.example.com/hooks/abc",
])
def test_non_https_url_fails_without_echoing_it(url):
    with pytest.raises(SettingError, match="HOOK_URL") as error:
        https_url_setting({"HOOK_URL": url}, "HOOK_URL", SettingError)

    assert "abc" not in str(error.value)


def test_request_error_name_leaves_the_url_out():
    error = requests.ConnectionError(
        "HTTPSConnectionPool(host='chat.example.com', port=443): Max retries exceeded with url: /hooks/abc"
    )

    assert request_error_name(error) == "ConnectionError"
