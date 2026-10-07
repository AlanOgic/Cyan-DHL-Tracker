"""Tell whether the automated tracker still works: Docker healthcheck and optional external monitor.

After each hourly check that could read Odoo, the tracker calls ``Heartbeat.record_success()``,
which touches SUCCESS_FILE and, when HEARTBEAT_URL is set, pings that URL. The Docker
healthcheck runs ``python heartbeat.py``, which fails once the last success is older than
MAX_SILENCE. Docker only marks the container unhealthy; the pings let an external monitor
(healthchecks.io check, Uptime Kuma push monitor) alert someone when they stop.

Setting (optional):
- HEARTBEAT_URL: https URL pinged with GET after each successful hourly check. It holds the
  monitor's secret token, so it is never logged.
"""
import logging
import os
import stat
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Mapping, Optional

import requests

from secret_url import https_url_setting, request_error_name

logger = logging.getLogger(__name__)

SUCCESS_FILE = Path("/tmp/dhl-tracker-last-success")
SUCCESS_FILE_MODE = 0o600
# Two hourly checks plus their run time: one missed check (Odoo down, long DHL pause) is tolerated
MAX_SILENCE = timedelta(minutes=150)
PING_TIMEOUT_SECONDS = 10


class HeartbeatConfigError(ValueError):
    """Raised when HEARTBEAT_URL is invalid."""


class Heartbeat:
    def __init__(
        self,
        ping_url: Optional[str] = None,
        success_file: Path = SUCCESS_FILE,
        session: Optional[requests.Session] = None,
    ):
        self.ping_url = ping_url
        self.success_file = success_file
        self._session = session or requests.Session()

    def record_success(self) -> None:
        """Record that a check worked; a failure here is logged, never raised into the tracker."""
        try:
            self._touch_success_file()
        except OSError as exc:
            logger.error("Could not record the tracker heartbeat in %s: %s", self.success_file, exc)
        if self.ping_url:
            self._ping(self.ping_url)

    def _touch_success_file(self) -> None:
        # O_NOFOLLOW: never write through a symlink planted in a shared /tmp
        fd = os.open(self.success_file, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, SUCCESS_FILE_MODE)
        try:
            os.utime(fd)
        finally:
            os.close(fd)

    def _ping(self, url: str) -> None:
        try:
            # No redirects: the URL is a secret, and only the monitor itself may receive it
            response = self._session.get(url, timeout=PING_TIMEOUT_SECONDS, allow_redirects=False)
        except requests.RequestException as exc:
            logger.warning("Heartbeat ping failed: %s", request_error_name(exc))
            return
        if not 200 <= response.status_code < 300:
            logger.warning("Heartbeat ping returned HTTP %s", response.status_code)


def build_from_env(environ: Mapping[str, str] = os.environ) -> Heartbeat:
    return Heartbeat(https_url_setting(environ, "HEARTBEAT_URL", HeartbeatConfigError))


def main(success_file: Path = SUCCESS_FILE) -> int:
    """Docker healthcheck: 0 when the last successful check is recent enough, 1 otherwise."""
    try:
        info = success_file.lstat()
    except FileNotFoundError:
        print("unhealthy: no successful check yet", file=sys.stderr)
        return 1
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        # A symlink or another user's file proves nothing about this tracker
        print(f"unhealthy: {success_file} is not a regular file of this user", file=sys.stderr)
        return 1
    minutes = (time.time() - info.st_mtime) / 60
    if minutes > MAX_SILENCE.total_seconds() / 60:
        print(f"unhealthy: last successful check {minutes:.0f} min ago", file=sys.stderr)
        return 1
    print(f"healthy: last successful check {minutes:.0f} min ago")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
