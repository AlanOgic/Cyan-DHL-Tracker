"""Minimal client for Odoo's JSON-2 external API (Odoo 19+).

Every call is ``POST {url}/json/2/<model>/<method>`` with named arguments in a
JSON body, authenticated by an API key sent as a bearer token. Each call runs in
its own database transaction.
"""
import os
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import urlparse

import requests

DEFAULT_TIMEOUT_SECONDS = 30
USER_AGENT = "cyan-dhl-tracker"
REQUIRED_ENV_VARS = ("ODOO_URL", "ODOO_API_KEY")
ERROR_BODY_PREVIEW_CHARS = 200
# The API key travels in every request, so plain http is only accepted for a local server.
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class OdooConfigError(ValueError):
    """Raised when the Odoo connection settings are missing or invalid."""


class OdooError(Exception):
    """Raised when a JSON-2 call fails, either at the HTTP level or in Odoo."""

    def __init__(self, message: str, status_code: Optional[int] = None, name: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.name = name


@dataclass(frozen=True)
class OdooConfig:
    url: str
    api_key: str = field(repr=False)
    database: Optional[str] = None
    timeout: float = DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> "OdooConfig":
        """Build the config from ODOO_URL, ODOO_API_KEY and the optional ODOO_DB."""
        missing = [name for name in REQUIRED_ENV_VARS if not environ.get(name, "").strip()]
        if missing:
            raise OdooConfigError(f"Missing Odoo settings: {', '.join(missing)} (see .env.example)")

        url = environ["ODOO_URL"].strip().rstrip("/")
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise OdooConfigError(f"ODOO_URL must be an http(s) URL, got {url!r}")
        if parsed.scheme == "http" and parsed.hostname not in LOCAL_HOSTS:
            raise OdooConfigError(f"ODOO_URL must use https outside localhost, got {url!r}")

        return cls(
            url=url,
            api_key=environ["ODOO_API_KEY"].strip(),
            database=environ.get("ODOO_DB", "").strip() or None,
        )


def _build_headers(config: OdooConfig) -> dict:
    headers = {
        "Authorization": f"bearer {config.api_key}",
        "User-Agent": f"{USER_AGENT} {requests.utils.default_user_agent()}",
    }
    if config.database:
        # Only required when the server hosts several databases without a Host-based dbfilter.
        return {**headers, "X-Odoo-Database": config.database}
    return headers


def _error_from_response(endpoint: str, response: requests.Response) -> OdooError:
    """Turn a 4xx/5xx response into an OdooError, leaving out the server traceback."""
    try:
        body = response.json()
    except ValueError:
        body = None

    if isinstance(body, dict):
        name = body.get("name")
        detail = body.get("message") or response.reason
    else:
        name = None
        detail = response.text[:ERROR_BODY_PREVIEW_CHARS] or response.reason

    return OdooError(
        f"{endpoint} failed with HTTP {response.status_code}: {detail}",
        status_code=response.status_code,
        name=name,
    )


class Json2Client:
    def __init__(self, config: OdooConfig, session: Optional[requests.Session] = None):
        self._config = config
        self._session = session or requests.Session()
        self._headers = _build_headers(config)

    def call(self, model: str, method: str, ids: Optional[Sequence[int]] = None, **kwargs: Any) -> Any:
        """Call ``model.method`` and return its JSON-decoded result.

        ``ids`` selects the records the method runs on; leave it out for
        ``@api.model`` methods such as ``search_read``. Other arguments must be
        passed by name (e.g. ``domain=...``, ``fields=...``, ``vals=...``).
        """
        endpoint = f"{model}.{method}"
        payload = {**kwargs, "ids": list(ids)} if ids is not None else dict(kwargs)

        try:
            response = self._session.post(
                f"{self._config.url}/json/2/{model}/{method}",
                json=payload,
                headers=self._headers,
                timeout=self._config.timeout,
                # A followed 301/302 would silently turn the POST into a GET
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise OdooError(f"{endpoint} request failed: {exc}") from exc

        if response.is_redirect:
            raise OdooError(
                f"{endpoint} was redirected to {response.headers.get('Location')}; check ODOO_URL",
                status_code=response.status_code,
            )
        if not response.ok:
            raise _error_from_response(endpoint, response)

        try:
            return response.json()
        except ValueError as exc:
            raise OdooError(
                f"{endpoint} returned a response that is not valid JSON", status_code=response.status_code
            ) from exc
