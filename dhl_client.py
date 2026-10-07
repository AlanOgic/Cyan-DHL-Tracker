"""Client for DHL's Shipment Tracking - Unified API, shared by all scripts.

Spec: API/track_v1.5.8.yaml. The default DHL service level allows 250 calls per
day and at most one call every 5 seconds; DHLTracker enforces the spacing.
"""
import logging
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

import requests

logger = logging.getLogger(__name__)

DHL_TRACKING_URL = "https://api-eu.dhl.com/track/shipments"
MIN_SECONDS_BETWEEN_CALLS = 5
REQUEST_TIMEOUT_SECONDS = 30

# HTTP 429 handling. DHL documents no Retry-After header, so it is optional.
HTTP_NOT_FOUND = 404
HTTP_TOO_MANY_REQUESTS = 429
RATE_LIMIT_PAUSE_SECONDS = 60          # pause before the single retry
MAX_INLINE_PAUSE_SECONDS = 5 * 60      # a longer Retry-After is not waited for inline
RATE_LIMIT_COOLDOWN_SECONDS = 30 * 60  # no calls after a failed retry; shorter than the hourly check
MAX_SUSPENSION_SECONDS = 24 * 60 * 60  # DHL's quota is daily; never trust a longer Retry-After

# status.statusCode is required by the spec and is one of:
# delivered, failure, pre-transit, transit, unknown
DELIVERED_STATUS_CODE = "delivered"

HTTP_ERROR_LABELS = {401: "Auth Error", 404: "Not Found", 429: "Rate Limited"}
REQUEST_FAILED_LABEL = "Request Failed"
NO_DATA_LABEL = "No data"
UNKNOWN_STATUS = "Unknown"


class DHLConfigError(ValueError):
    """Raised when the DHL API key is missing."""


def _current_status(tracking_data: dict) -> Optional[dict]:
    """High-level status of the first shipment, or None for errors and empty results."""
    if tracking_data.get("error"):
        return None
    shipments = tracking_data.get("shipments") or []
    if not shipments:
        return None
    return shipments[0].get("status")


def is_delivered(tracking_data: dict) -> bool:
    """True only when DHL's high-level statusCode says so.

    Free text is never used: descriptions such as "The shipment could not be
    delivered" or the Express event "Not delivered" also contain the word.
    """
    status = _current_status(tracking_data)
    if not status:
        return False
    return str(status.get("statusCode", "")).lower() == DELIVERED_STATUS_CODE


def get_status_info(tracking_data: dict) -> tuple:
    """(current status description, next steps), or (None, None) when there is no status."""
    status = _current_status(tracking_data)
    if not status:
        return None, None
    description = status.get("description") or status.get("status") or UNKNOWN_STATUS
    return description, status.get("nextSteps")


@dataclass(frozen=True)
class TrackingEvent:
    """One entry of a shipment's history. For Express, `code` is the two-letter event code (DF, HP, OK…)."""

    code: Optional[str]
    description: str
    timestamp: Optional[str]
    location: Optional[str]


def _event_time(event: dict) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(event.get("timestamp") or "")
    except (TypeError, ValueError):
        return None


def _newest(events: list) -> dict:
    """DHL lists events newest first, but the spec does not promise it: sort when every timestamp is readable."""
    times = [time_ for time_ in map(_event_time, events) if time_ is not None]
    if len(times) < len(events):
        return events[0]
    try:
        newest = max(times)
    except TypeError:  # naive and timezone-aware timestamps cannot be compared
        return events[0]
    return events[times.index(newest)]


def latest_event(tracking_data: dict) -> Optional[TrackingEvent]:
    """Most recent event of the first shipment, or None for errors and shipments without history."""
    if tracking_data.get("error"):
        return None
    shipments = tracking_data.get("shipments") or []
    events = shipments[0].get("events") if shipments else None
    if not events:
        return None
    newest = _newest(events)
    address = (newest.get("location") or {}).get("address") or {}
    return TrackingEvent(
        code=newest.get("status"),
        description=newest.get("description") or "",
        timestamp=newest.get("timestamp"),
        location=address.get("addressLocality"),
    )


def shipment_number(tracking_data: dict) -> Optional[str]:
    """DHL number of the first shipment in a tracking result, if any."""
    shipments = [] if tracking_data.get("error") else tracking_data.get("shipments") or []
    return shipments[0].get("id") if shipments else None


def split_tracking_reference(reference: Optional[str]) -> tuple:
    """DHL numbers in an Odoo tracking reference, in order, without duplicates.

    Odoo appends ",<number>" to a picking's reference each time a DHL label is created
    from it or from a linked picking (a re-generated label, a return): these are separate
    DHL shipments, not the parcels of one shipment (those share a single number).
    """
    numbers = (part.strip() for part in (reference or "").split(","))
    return tuple(dict.fromkeys(number for number in numbers if number))


_NO_EVENT_TIME = datetime.min.replace(tzinfo=timezone.utc)


def _latest_event_time(tracking_data: dict) -> datetime:
    event = latest_event(tracking_data)
    moment = _event_time({"timestamp": event.timestamp}) if event else None
    if moment is None:
        return _NO_EVENT_TIME
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)


def most_relevant(results: list) -> dict:
    """The result that speaks for a reference holding several DHL numbers.

    The shipment with the newest event decides, so a return still on its way keeps a
    picking open after the outbound shipment was delivered, while an unused re-generated
    label (no events) never blocks it. If any number could not be read (network, rate
    limit, DHL error), that transient error is returned: the picture is incomplete, so
    Odoo keeps its last known status until the next check.
    """
    transient = [result for result in results if is_transient_error(result)]
    if transient:
        return transient[0]
    found = [result for result in results if not result.get("error")]
    return max(found, key=_latest_event_time) if found else results[0]


def is_transient_error(tracking_data: dict) -> bool:
    """Network failures, rate limits and DHL server errors: the shipment's real status is unknown, not changed."""
    if not tracking_data.get("error"):
        return False
    status_code = tracking_data.get("status_code")
    return status_code is None or status_code == HTTP_TOO_MANY_REQUESTS or status_code >= 500


def _retry_after_seconds(response: requests.Response) -> float:
    """Pause requested by a 429 response: Retry-After (delta-seconds) or the default."""
    try:
        seconds = float(response.headers.get("Retry-After", ""))
    except ValueError:
        return RATE_LIMIT_PAUSE_SECONDS
    if not math.isfinite(seconds):
        return RATE_LIMIT_PAUSE_SECONDS
    return min(max(seconds, MIN_SECONDS_BETWEEN_CALLS), MAX_SUSPENSION_SECONDS)


def _error_label(status_code: Optional[int]) -> str:
    if status_code is None:
        return REQUEST_FAILED_LABEL
    return HTTP_ERROR_LABELS.get(status_code, f"Error {status_code}")


def summarize_status(tracking_data: dict) -> tuple:
    """(status description, next steps, is delivered). Next steps are dropped once delivered."""
    if tracking_data.get("error"):
        return _error_label(tracking_data.get("status_code")), None, False

    description, next_steps = get_status_info(tracking_data)
    if description is None:
        return NO_DATA_LABEL, None, False

    delivered = is_delivered(tracking_data)
    return description, None if delivered else next_steps, delivered


class DHLTracker:
    def __init__(
        self,
        api_key: Optional[str] = None,
        session: Optional[requests.Session] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.api_key = api_key or os.getenv("DHL_API_KEY", "").strip()
        if not self.api_key:
            raise DHLConfigError("Missing DHL setting: DHL_API_KEY (see .env.example)")
        self._session = session or requests.Session()
        self._headers = {"DHL-API-Key": self.api_key, "Accept": "application/json"}
        self._clock = clock
        self._sleep = sleep
        self._last_call_at: Optional[float] = None
        self._suspended_until: Optional[float] = None

    @property
    def rate_limited(self) -> bool:
        """True while DHL calls are suspended because DHL kept answering HTTP 429."""
        return self._suspended_until is not None and self._clock() < self._suspended_until

    def _wait_for_rate_limit(self) -> None:
        """Wait until MIN_SECONDS_BETWEEN_CALLS have passed since the previous call *returned*.

        Measuring from the end (not the start) of the previous call guarantees the
        spacing as seen by DHL, however long each request takes.
        """
        if self._last_call_at is None:
            return
        remaining = MIN_SECONDS_BETWEEN_CALLS - (self._clock() - self._last_call_at)
        if remaining > 0:
            self._sleep(remaining)

    def _get(self, params: dict) -> requests.Response:
        self._wait_for_rate_limit()
        try:
            return self._session.get(
                DHL_TRACKING_URL, headers=self._headers, params=params, timeout=REQUEST_TIMEOUT_SECONDS
            )
        finally:
            self._last_call_at = self._clock()

    def _get_with_rate_limit_retry(self, params: dict, tracking_number: str) -> requests.Response:
        """On HTTP 429, pause and retry once; if DHL still refuses, suspend calls for a while."""
        response = self._get(params)
        if response.status_code != HTTP_TOO_MANY_REQUESTS:
            return response

        pause = _retry_after_seconds(response)
        if pause <= MAX_INLINE_PAUSE_SECONDS:
            logger.warning("DHL rate limit hit while tracking %s; retrying in %.0f s", tracking_number, pause)
            self._sleep(pause)
            try:
                response = self._get(params)
            except requests.RequestException:
                self._suspend(pause)  # DHL was already refusing calls; don't hammer it on the next shipment
                raise
            if response.status_code != HTTP_TOO_MANY_REQUESTS:
                return response
            pause = _retry_after_seconds(response)

        self._suspend(pause)
        return response

    def _suspend(self, pause: float) -> None:
        suspension = max(pause, RATE_LIMIT_COOLDOWN_SECONDS)
        self._suspended_until = self._clock() + suspension
        logger.warning("DHL rate limit still reached; suspending DHL calls for %.0f min", suspension / 60)

    def track_shipment(self, tracking_number: str, service: Optional[str] = None) -> dict:
        """Raw DHL tracking response, or {"error": True, "status_code", "message"} on failure."""
        if self.rate_limited:
            return {
                "error": True,
                "status_code": HTTP_TOO_MANY_REQUESTS,
                "message": "DHL calls suspended after repeated rate-limit responses",
            }

        params = {"trackingNumber": tracking_number, **({"service": service} if service else {})}
        try:
            response = self._get_with_rate_limit_retry(params, tracking_number)
        except requests.RequestException as exc:
            logger.warning("DHL tracking request for %s failed: %s", tracking_number, exc)
            return {"error": True, "status_code": None, "message": str(exc)}

        if response.status_code != 200:
            logger.warning("DHL tracking for %s returned HTTP %s", tracking_number, response.status_code)
            return {"error": True, "status_code": response.status_code, "message": response.text}

        try:
            return response.json()
        except ValueError:
            return {"error": True, "status_code": response.status_code, "message": "DHL response is not valid JSON"}

    def track_reference(self, reference: str) -> dict:
        """Track every DHL number of an Odoo tracking reference; return the most relevant result."""
        numbers = split_tracking_reference(reference)
        if not numbers:
            return {"error": True, "status_code": HTTP_NOT_FOUND, "message": "No DHL tracking number in the reference"}
        results = []
        for number in numbers:
            results.append(self.track_shipment(number))
            if self.rate_limited:
                break
        return most_relevant(results)

    def get_shipment_status(self, tracking_number: str) -> tuple:
        """(status description, next steps, is delivered) for one tracking number."""
        return summarize_status(self.track_shipment(tracking_number))
