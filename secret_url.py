"""Settings whose URL carries a secret: Mattermost webhooks (``/hooks/<key>``) and monitor pings.

Never echo such a URL: not in a configuration error, and not in a log line. requests quotes
the URL path in its error messages, so log ``request_error_name(exc)`` instead of ``exc``.
"""
from typing import Mapping, Optional
from urllib.parse import urlparse

import requests


def https_url_setting(environ: Mapping[str, str], name: str, error_type: type[ValueError]) -> Optional[str]:
    """The https URL in setting ``name``, None when unset or blank; raises ``error_type`` for any other URL.

    Credentials in the URL (``user:password@``) are refused: requests would send them as basic auth.
    """
    url = environ.get(name, "").strip()
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or "@" in parsed.netloc:
        raise error_type(f"{name} must be an https:// URL with a host and no credentials")
    return url


def request_error_name(exc: requests.RequestException) -> str:
    """What went wrong, without the URL that the error message quotes."""
    return type(exc).__name__
