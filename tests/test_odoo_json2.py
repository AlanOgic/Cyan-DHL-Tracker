import pytest
import requests

from odoo_json2 import Json2Client, OdooConfig, OdooConfigError, OdooError

NO_JSON = object()


class FakeResponse:
    def __init__(self, status_code=200, json_body=None, text="", reason="OK", headers=None):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text
        self.reason = reason
        self.headers = headers or {}

    @property
    def ok(self):
        return self.status_code < 400

    @property
    def is_redirect(self):
        return "Location" in self.headers and self.status_code in (301, 302, 303, 307, 308)

    def json(self):
        if self._json_body is NO_JSON:
            raise ValueError("Expecting value")
        return self._json_body


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None, allow_redirects=True):
        self.calls.append({
            "url": url, "json": json, "headers": headers, "timeout": timeout, "allow_redirects": allow_redirects,
        })
        if self.error:
            raise self.error
        return self.response


CONFIG = OdooConfig(url="https://odoo.example.com", api_key="secret-key", database="cyan")


def make_client(response=None, error=None, config=CONFIG):
    session = FakeSession(response=response, error=error)
    return Json2Client(config, session=session), session


# --- OdooConfig.from_env ----------------------------------------------------

def test_from_env_builds_config_and_strips_trailing_slash():
    config = OdooConfig.from_env({
        "ODOO_URL": "https://odoo.example.com/ ",
        "ODOO_API_KEY": "secret-key",
        "ODOO_DB": "cyan",
    })

    assert config.url == "https://odoo.example.com"
    assert config.api_key == "secret-key"
    assert config.database == "cyan"


def test_from_env_database_is_optional():
    config = OdooConfig.from_env({"ODOO_URL": "https://odoo.example.com", "ODOO_API_KEY": "k"})

    assert config.database is None


def test_from_env_reports_every_missing_setting():
    with pytest.raises(OdooConfigError) as excinfo:
        OdooConfig.from_env({"ODOO_URL": "  "})

    assert "ODOO_URL" in str(excinfo.value)
    assert "ODOO_API_KEY" in str(excinfo.value)


@pytest.mark.parametrize("url", ["odoo.example.com", "ftp://odoo.example.com", "https://"])
def test_from_env_rejects_non_http_urls(url):
    with pytest.raises(OdooConfigError, match="ODOO_URL"):
        OdooConfig.from_env({"ODOO_URL": url, "ODOO_API_KEY": "k"})


def test_from_env_requires_https_for_remote_hosts():
    with pytest.raises(OdooConfigError, match="https"):
        OdooConfig.from_env({"ODOO_URL": "http://odoo.example.com", "ODOO_API_KEY": "k"})


@pytest.mark.parametrize("url", ["http://localhost:8069", "http://127.0.0.1:8069", "http://[::1]:8069"])
def test_from_env_allows_plain_http_for_local_development(url):
    assert OdooConfig.from_env({"ODOO_URL": url, "ODOO_API_KEY": "k"}).url == url


def test_config_repr_hides_api_key():
    assert "secret-key" not in repr(CONFIG)


# --- Json2Client.call -------------------------------------------------------

def test_call_posts_named_arguments_with_bearer_auth():
    client, session = make_client(FakeResponse(json_body=[{"id": 1}]))

    result = client.call("res.partner", "search_read", domain=[["id", "=", 1]], fields=["name"])

    assert result == [{"id": 1}]
    call = session.calls[0]
    assert call["url"] == "https://odoo.example.com/json/2/res.partner/search_read"
    assert call["json"] == {"domain": [["id", "=", 1]], "fields": ["name"]}
    assert call["headers"]["Authorization"] == "bearer secret-key"
    assert call["headers"]["X-Odoo-Database"] == "cyan"
    assert call["headers"]["User-Agent"].startswith("cyan-dhl-tracker ")
    assert call["timeout"] == CONFIG.timeout
    assert call["allow_redirects"] is False


def test_call_reports_redirects_instead_of_following_them():
    response = FakeResponse(status_code=301, json_body=NO_JSON, reason="Moved Permanently",
                            headers={"Location": "https://other.example.com/json/2/res.users/context_get"})
    client, _ = make_client(response)

    with pytest.raises(OdooError, match="redirected to https://other.example.com") as excinfo:
        client.call("res.users", "context_get")

    assert excinfo.value.status_code == 301


def test_call_omits_database_header_when_not_configured():
    config = OdooConfig(url="https://odoo.example.com", api_key="k")
    client, session = make_client(FakeResponse(json_body={}), config=config)

    client.call("res.users", "context_get")

    assert "X-Odoo-Database" not in session.calls[0]["headers"]


def test_call_sends_ids_only_when_given():
    client, session = make_client(FakeResponse(json_body=True))

    client.call("stock.picking", "write", ids=(4, 5), vals={"x": 1})
    client.call("res.users", "context_get")

    assert session.calls[0]["json"] == {"ids": [4, 5], "vals": {"x": 1}}
    assert session.calls[1]["json"] == {}


def test_call_raises_odoo_error_from_json_error_body():
    body = {"name": "werkzeug.exceptions.Unauthorized", "message": "Invalid apikey", "debug": "Traceback..."}
    client, _ = make_client(FakeResponse(status_code=401, json_body=body, reason="Unauthorized"))

    with pytest.raises(OdooError) as excinfo:
        client.call("res.users", "context_get")

    error = excinfo.value
    assert error.status_code == 401
    assert error.name == "werkzeug.exceptions.Unauthorized"
    assert "Invalid apikey" in str(error)
    assert "Traceback" not in str(error)


def test_call_raises_odoo_error_from_non_json_error_body():
    response = FakeResponse(status_code=502, json_body=NO_JSON, text="<html>Bad Gateway</html>", reason="Bad Gateway")
    client, _ = make_client(response)

    with pytest.raises(OdooError) as excinfo:
        client.call("stock.picking", "search", domain=[])

    assert excinfo.value.status_code == 502
    assert excinfo.value.name is None
    assert "Bad Gateway" in str(excinfo.value)


def test_call_wraps_network_errors():
    client, _ = make_client(error=requests.ConnectionError("connection refused"))

    with pytest.raises(OdooError, match="connection refused") as excinfo:
        client.call("res.users", "context_get")

    assert excinfo.value.status_code is None


def test_call_rejects_success_response_that_is_not_json():
    client, _ = make_client(FakeResponse(status_code=200, json_body=NO_JSON, text="<html>login</html>"))

    with pytest.raises(OdooError, match="not valid JSON"):
        client.call("res.users", "context_get")
