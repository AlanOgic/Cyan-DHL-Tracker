# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Cyan-DHL-Tracker syncs DHL shipment status into Cyanview's Odoo (Odoo Online, saas~19.x). It reads outgoing DHL deliveries (`stock.picking`) from Odoo over the **JSON-2 API**, queries the DHL Unified Tracking API for each tracking number, writes the result back onto the picking, and (in automated mode) posts summaries to a Mattermost incoming webhook when `WEBHOOK_URL` is set. An empty `WEBHOOK_URL` disables all notifications.

## Commands

```bash
# Local setup
python -m venv .venvtrack && source .venvtrack/bin/activate
pip install -r requirements-dev.txt   # runtime deps + pytest/pytest-cov
cp .env.example .env                  # DHL_API_KEY, ODOO_URL, ODOO_API_KEY, ODOO_DB (optional), WEBHOOK_URL (optional)

# Tests (cover the shared modules; no network: Odoo, DHL and Mattermost are faked)
pytest
pytest tests/test_odoo_client.py::test_update_delivery_status_marks_delivered_and_clears_status
pytest --cov=odoo_json2 --cov=odoo_client --cov=dhl_client --cov=shipment_alerts --cov=odoo_helpdesk --cov=alert_dispatch --cov=shipment_sync --cov=dhl_codes_doc --cov=secret_url --cov=heartbeat --cov=automated_tracker --cov-report=term-missing

# Entry points — standalone scripts
python shiptracker.py         # interactive CLI menu (track / list recent / partner lookup)
python automated_tracker.py   # scheduler daemon, runs forever
python track_shipments.py     # one-shot batch: 20 latest pickings → DHL → Odoo write → JSON dump
python detailed_tracker.py    # one tracking number → full DHL detail JSON (no Odoo access)
python dhl_codes_doc.py       # regenerate docs/dhl-tracking-codes.md (DHL codes reference)

# Docker (runs automated_tracker.py; env comes from .env and is fixed at container creation —
# after editing .env use `up -d` to recreate, `restart` keeps the old values)
docker compose up -d --build
docker compose logs -f dhl-tracker
docker inspect --format '{{.State.Health.Status}}' dhl-automated-tracker   # healthy = an hourly check reached Odoo within 150 min
docker compose logs egress-proxy | grep refused   # hosts the egress allow-list blocked
docker compose down
```

No linter config or CI. `python -m py_compile *.py` is the only check for the scripts themselves.

## Architecture

**The four scripts are thin entry points over shared modules.**

- `odoo_json2.py`: generic JSON-2 transport. `OdooConfig.from_env()` (fails fast with `OdooConfigError`), `Json2Client.call(model, method, ids=None, **kwargs)`, and `OdooError` (status code + exception name; the server traceback is dropped).
- `odoo_client.py`: `OdooClient`, the DHL-shipment view of Odoo, used by `automated_tracker.py`, `shiptracker.py` and `track_shipments.py`. Its helpers log failures and return an empty result (`None`, `set()`, `0`, `False`) instead of raising, so one bad call never stops a run. `get_recent_shipments` returns `None` (not `[]`) when Odoo cannot be read, so a failure never reads as "nothing to track". Each script passes its own query window/limits (automated: last 90 days, limit 100; CLIs: no date filter, limit 20).
- `shipment_sync.py`: `ShipmentSync.apply(shipment, tracking_data)` is the **only** path that writes a DHL result to Odoo (automated tracker, `shiptracker.py` option 2, `track_shipments.py`). Build it with `build_from_env()` (OdooClient + alert dispatcher sharing one JSON-2 connection). Never call `update_delivery_status` directly from a script, or the alert marker below goes out of sync. `SyncResult.delivered` is True only once Odoo recorded the delivery; `written` is None when nothing was written (DHL status unknown) and False when Odoo refused the write (the picking stays tracked and is written again at the next check). The per-process code memory is kept even when the write fails, so an alert already sent is not repeated; after a restart, a marker that never reached Odoo means that alert is sent once more.
- `heartbeat.py`: `Heartbeat.record_success()` touches `/tmp/dhl-tracker-last-success` and pings `HEARTBEAT_URL` (optional, https, a healthchecks.io or Uptime Kuma push URL) after each successful hourly check; `python heartbeat.py` is the Docker healthcheck (unhealthy after `MAX_SILENCE` = 150 min without success). `secret_url.py`: `https_url_setting()` validates a URL setting without echoing it, and `request_error_name()` logs a `requests` error by type only. Webhook and monitor URLs carry their secret in the path, and `requests` quotes the path in its error messages, so never log the exception itself.
- `shipment_alerts.py` (pure): the 19 problem codes in 4 families (`ALERT_CODES`, `FAMILIES` with ticket priority and colour), `needs_alert(event_code, known_code)`, `is_recent` (7-day window), Mattermost payload (outside text Markdown-escaped, `@` neutralised) and ticket HTML (`html.escape`).
- `odoo_helpdesk.py`: `HelpdeskClient.raise_ticket(alert)`: internal note on the open ticket for that tracking number (name ilike, stage not folded) or a new ticket. Only `mail.mt_note` notes are posted: the linked customer (auto-subscribed by helpdesk on create) is never e-mailed. `alert_dispatch.py`: Helpdesk (skipped for `HELPDESK_SKIP_CODES`, default OH/MD/NH, validated against `ALERT_CODES` at startup) then Mattermost; `dispatch()` is True when at least one channel succeeded (a failed channel is logged, not retried, so the other is not repeated).
- `dhl_client.py`: `DHLTracker` (fails fast with `DHLConfigError` without `DHL_API_KEY`) plus pure helpers `is_delivered()`, `get_status_info()`, `summarize_status()`, `latest_event()` (newest by timestamp, else DHL order), `is_transient_error()` (network, 429, 5xx), `split_tracking_reference()` and `most_relevant()`. Scripts call `track_reference(picking_ref)`, which tracks every comma-separated number and keeps one result: the newest event decides (so an in-transit return keeps a delivered outbound picking open), and any transient error wins so a partial picture is never written; `track_shipment(number)` is for a single number typed by a user. `track_shipment()` never raises; it returns the raw DHL JSON or `{"error": True, "status_code", "message"}`, with `status_code=None` for network errors. The instance waits 5 s after the previous call *returned* (DHL's limit as seen server-side), so callers must not add their own sleeps.

### Odoo contract (JSON-2: `POST {ODOO_URL}/json/2/<model>/<method>`)

- Auth is `Authorization: bearer <ODOO_API_KEY>`. There is no login, uid or password. `X-Odoo-Database` is sent only when `ODOO_DB` is set. `connect()` just validates the key via `res.users/context_get`.
- **All method arguments must be named** (`domain=`, `fields=`, `vals=`…). The server binds them with `inspect.signature(...).bind`. Record ids go in `ids=` and must be omitted for `@api.model` methods like `search_read`/`search`, which would otherwise return 422. Each call is its own DB transaction.
- Candidate shipments: `carrier_tracking_ref != False`, `carrier_id.name ilike 'DHL'`, `state = 'done'`, `x_studio_delivered_ = False`, ordered by `date_done desc`.
- Depends on two **Odoo Studio custom fields** on `stock.picking`: `x_studio_delivered_` (boolean, and the trailing underscore is part of the name) and `x_studio_last_status` (text).
- **Comma-separated references** (`1000000002,1000000003`) are separate DHL shipments, not parcels: Odoo's `send_to_shipper` appends every new label's number to the picking and to all pickings linked through stock moves (re-generated labels, returns). Never send the whole string to DHL (404).
- **Tracking expired**: `expire_stale_tracking(older_than)` (run each hourly check with the 90-day window) writes `Status: Tracking expired - …` on undelivered DHL pickings older than the window; `get_recent_shipments` excludes them (`not like`, which keeps empty statuses). Past ~90 days DHL returns 404 or data of a newer shipment that reused the AWB number.
- `update_delivery_status`: delivered → `x_studio_delivered_=True`, `x_studio_last_status=''`; otherwise `x_studio_last_status = "Status: …\nNext Steps: …"`. Pickings are matched by `carrier_tracking_ref` + DHL carrier only, so every picking sharing a tracking ref is written. Once flagged delivered, a picking is never queried again.

### Alert marker (dedup across restarts)

`x_studio_last_status` starts with the latest Express event code: `Status: [HP] …` (`format_status_text(..., event_code)`). `ShipmentSync` alerts when the new code is a problem code, differs from the known code (Odoo marker, or this process's memory per tracking number so pickings sharing a ref alert once) and is recent. Rules that keep it from re-alerting: transient DHL errors write nothing; any DHL error never overwrites a status that has a marker; no new event keeps the previous code; if every channel fails the marker is omitted so the alert retries; a delivered shipment whose alert failed is not closed.

### DHL contract

`GET https://api-eu.dhl.com/track/shipments?trackingNumber=…` with header `DHL-API-Key`. Only `shipments[0]` is read.

- **Delivered means `status.statusCode == "delivered"`, nothing else.** `statusCode` is required by the spec, and its enum is `delivered`, `failure`, `pre-transit`, `transit` and `unknown`. Never match on free text: 53 DHL descriptions read "…could not be delivered…", Express has "Not delivered"/"Refused delivery", and a false positive permanently stops tracking that picking. For Express, `status.status` holds the numeric event code (101 delivered, 102 transit, 104 info received). `ok`/`OK` is an Express event code, not a `statusCode`.
- Errors map to labels in `get_shipment_status`: "Not Found" (404), "Auth Error" (401), "Rate Limited" (429), "Request Failed" (network), "Error N".
- **HTTP 429:** `DHLTracker` pauses (`Retry-After` delta-seconds if present, ≥5 s, else 60 s) and retries once. A pause over 5 min is not waited for inline. If DHL still refuses, `DHLTracker` suspends calls for max(pause, 30 min), capped at 24 h (DHL's quota is daily). A network error on the retry also suspends. `rate_limited` is True meanwhile, and `track_shipment` returns a synthetic 429 error without calling DHL. Callers check `dhl_tracker.rate_limited` right after a call and must then skip the Odoo write, so the last known status is never overwritten with "Rate Limited". The automated tracker breaks its loop and logs the rest as postponed.
- **Quota:** the default service level is 250 calls/day and 1 call per 5 s. The hourly check costs 24 × tracked shipments per day, so about 10 tracked shipments use up the default quota, far below `MAX_TRACKED_SHIPMENTS = 100`.

DHL reference material lives in `API/`: the OpenAPI spec `track_v1.5.8.yaml`, `status_1.csv` (per-service status codes) and the Parcel DE ICE event/RIC code table.

`docs/dhl-tracking-codes.md` is the team-facing reference of the 40 Express event codes (English, rendered by GitLab/GitHub; the problem families reuse the Mattermost alert labels from `FAMILIES`). It is generated by `dhl_codes_doc.py`, which takes the alert columns, priorities, colours and limits from `shipment_alerts`, `alert_dispatch`, `dhl_client`, `odoo_client` and `automated_tracker`. Never edit the Markdown by hand: change the code, run `python dhl_codes_doc.py`, and `tests/test_dhl_codes_doc.py` checks the committed page matches. The GitHub repo is public, so the page uses a fictitious tracking number. `docs/` and the generator are excluded from the Docker image.

### Automated tracker (`AutomatedTracker.start_scheduler`)

1. `load_delivered_shipments()` seeds an in-memory set of delivered tracking refs (`TRACKING_WINDOW_DAYS`, `MAX_DELIVERED_PRELOAD`); those are skipped in detailed checks.
2. Sends a startup webhook, then runs `hourly_detailed_check()` immediately.
3. Uses the `schedule` library: `simple_check` every 10 min (Odoo count only; webhook only if the count changed since the last check) and `hourly_detailed_check` every hour (DHL per shipment → Odoo write → summary webhook, plus a separate "next steps" report webhook if any shipment has `nextSteps`).
4. `OdooClient.connect()` re-validates the API key on every check. All tracker state is in memory and resets on restart.
5. Webhook payloads use Mattermost format (`text`, `username`, `icon_emoji`). The in-transit list is capped at 10 entries.
6. Every startup step and scheduled check runs through `_run_safely`: an unexpected exception is logged with its traceback and the scheduler carries on. A crash would restart the container straight into a full DHL pass, and the in-memory 429 suspension would be lost. An error outside the checks exits with code 1.
7. An hourly check is a success (`heartbeat.record_success()`) when Odoo could be read and did not refuse every status write it got; shipments with nothing to write are left out of that count. A DHL outage or rate limit still counts as a success; those show in the logs only.
8. `WEBHOOK_URL`, `ALERT_WEBHOOK_URL` and `HEARTBEAT_URL` must be https URLs with a host and no credentials (`https_url_setting`); an invalid one stops the tracker at startup.

Scripts mostly `print()`; the Odoo modules use `logging`, configured by each script's `main()` via `logging.basicConfig`. Everything goes to stdout/stderr (Docker json-file driver).

## Gotchas

- **`automated_tracker.py`, `track_shipments.py`, and `shiptracker.py` menu option 2 write to whichever Odoo `.env` points at, which is production.** `detailed_tracker.py` and `shiptracker.py` options 1/3 are read-only.
- Alerts create real Helpdesk tickets in production (team `HELPDESK_TEAM`, default "Logistics & Shipping"). Tests use fakes; never point a manual experiment at the real team.
- Give `ODOO_API_KEY` to a dedicated Odoo bot user with only the rights the tracker needs (pickings, helpdesk tickets), as the Odoo docs recommend for integrations, never to a personal or admin account.
- Docker marks an unhealthy container but never restarts it; only the `HEARTBEAT_URL` monitor alerts a person.
- The image is built from an allow-list (`.dockerignore`): top-level `*.py` (minus `dhl_codes_doc.py`) and `requirements.txt` only. The container runs with a read-only filesystem, no capabilities and memory/CPU limits (`docker-compose.yml`); anything that needs to write a file must use `/tmp` (tmpfs).
- In Docker the tracker has no direct network access. It sits on an `internal` network whose bridge has no host address (`gateway_mode_ipv4: isolated`; a plain internal network still lets containers reach host services on 0.0.0.0). It reaches the outside only through `egress-proxy` (tinyproxy, `proxy/`). The proxy's filter matches the whole request target (`FilterURLs`): only `^host:443$` CONNECT tunnels to the hosts in `EGRESS_ALLOWED_HOSTS` (required in `.env`) pass, and plain HTTP never does. A new external host (another URL setting, a new API) must be added there. `requests` finds the proxy through `HTTPS_PROXY`, so never set `trust_env = False` or `proxies=` on a session.
- `requirements.txt` and the Dockerfile base image are pinned (with the dependencies of `requests`). Bump them on purpose: change the pins, run the tests, rebuild.
- `track_shipments.py` and `detailed_tracker.py` write `dhl_tracking_*.json` to the current directory (git- and docker-ignored).
