# Cyan-DHL-Tracker

Tracks DHL shipments for Cyanview's Odoo deliveries. It reads outgoing DHL pickings from Odoo, checks each tracking number against the DHL Shipment Tracking API, and writes the delivery status back to Odoo.

## Features

- Fetch shipment tracking data from the DHL Shipment Tracking (Unified) API
- Read DHL deliveries and partner information from Odoo
- Write delivery status and DHL "next steps" back onto Odoo pickings
- Interactive CLI for tracking single shipments, listing recent shipments and looking up partners
- Automated tracker (Docker) that keeps Odoo up to date around the clock
- Optional Mattermost webhook notifications

## Requirements

- Python 3.10+ (required by current `requests` and `python-dotenv`; the Docker image uses Python 3.11)
- A DHL API key with access to Shipment Tracking - Unified
- Odoo 19 or later (uses the JSON-2 API) and an Odoo API key for a user with read/write access to `stock.picking` and read access to `res.partner`. Create the key under **Preferences → Account Security → New API Key**. A dedicated bot user with minimal rights is recommended.
- Docker with Compose v2 for the automated tracker

### Odoo prerequisites

The tracker reads and writes two custom Odoo Studio fields on `stock.picking`. Both must exist in the target database:

| Field | Type | Purpose |
|---|---|---|
| `x_studio_delivered_` | Boolean | Set to true once DHL's `statusCode` is `delivered` |
| `x_studio_last_status` | Text | Latest DHL status and next steps for shipments still in transit |

A picking is tracked when it has a `carrier_tracking_ref`, its carrier name contains "DHL", its state is `done`, and `x_studio_delivered_` is not set.

## Installation

1. Clone this repository:
   ```bash
   git clone https://github.com/AlanOgic/Cyan-DHL-Tracker.git
   cd Cyan-DHL-Tracker
   ```

2. Create a virtual environment and install dependencies:
   ```bash
   python -m venv .venvtrack
   source .venvtrack/bin/activate  # On Windows: .venvtrack\Scripts\activate
   pip install -r requirements.txt
   ```

3. Configure your environment:
   ```bash
   cp .env.example .env
   ```

4. Edit `.env` with your credentials:
   ```
   # DHL API credentials
   DHL_API_KEY=your_dhl_api_key

   # Odoo connection details (JSON-2 API)
   ODOO_URL=https://your-odoo-instance.odoo.com
   ODOO_API_KEY=your_odoo_api_key
   ODOO_DB=your_odoo_database   # optional: only needed when the server hosts several databases

   # Mattermost incoming webhook (optional; leave empty to disable notifications)
   WEBHOOK_URL=

   # Alerts on DHL problems (see "Alerts on DHL problems" below)
   ALERT_WEBHOOK_URL=
   HELPDESK_TEAM="Logistics & Shipping"
   HELPDESK_TAGS="Shipping Related"
   HELPDESK_SKIP_CODES=OH,MD,NH
   ```

   `.env` is git-ignored. Never commit it.

## Usage

> **Writes to Odoo:** `automated_tracker.py`, `track_shipments.py` and option 2 of `shiptracker.py` update the pickings in the Odoo database configured in `.env`, and open Helpdesk tickets when DHL reports a problem. Point `.env` at a test database while developing.

### Interactive CLI

```bash
python shiptracker.py
```

1. **Track a shipment**: enter a tracking number to see its full DHL status and history, plus the partner's details from Odoo. Read-only.
2. **View recent shipments**: list recent undelivered DHL shipments from Odoo with their live DHL status. This **updates Odoo** for every listed shipment.
3. **Get partner information**: search for a partner by ID or name and list their recent shipments. Read-only.
4. **Exit**

### One-shot scripts

```bash
python track_shipments.py    # Tracks the 20 most recent undelivered shipments, updates Odoo,
                             # and saves the results to dhl_tracking_results_<timestamp>.json
python detailed_tracker.py   # Prompts for one tracking number (and optional DHL service),
                             # saves the full DHL response to dhl_tracking_<number>_<timestamp>.json.
                             # DHL only; never touches Odoo
```

DHL calls are spaced 5 seconds apart, so a batch of 20 shipments takes about 2 minutes.

### DHL rate limits and delivery detection

- DHL's default service level for Shipment Tracking - Unified allows **250 calls per day** and **at most one call every 5 seconds**. `dhl_client.DHLTracker` enforces the 5-second spacing. The daily quota is shared by every script that uses the same `DHL_API_KEY`. DHL says this level is meant for development; for production, request an upgrade from **My Apps → your app → Request Upgrade** on the DHL developer portal.
- **When DHL answers HTTP 429** (too many requests), the tracker pauses (60 s, or the `Retry-After` header if DHL sends one) and retries once. If DHL still refuses, DHL calls are suspended for 30 minutes. While suspended, the automated tracker postpones the remaining shipments to the next hourly check, and no script overwrites the Odoo status with "Rate Limited".
- A shipment counts as delivered only when DHL's high-level `statusCode` is `delivered`. Free-text descriptions are never used, because many non-delivery statuses contain the word (e.g. "The shipment could not be delivered…", Express "Not delivered").

## Several DHL numbers in one reference

Odoo appends `,<number>` to a picking's tracking reference each time a DHL label is created from it or from a picking linked to it (a re-generated label, a return). These are separate DHL shipments, not parcels: a multi-parcel Express shipment keeps a single number. The tracker splits the reference and tracks every number; the shipment with the most recent DHL event decides the picking's status, alerts and delivery. A return still on its way therefore keeps the picking tracked after the outbound shipment was delivered, while an unused re-generated label (no events) never blocks it. If one of the numbers cannot be read (network, rate limit, DHL error), nothing is written and the picking is checked again next time. An alert and its ticket name the DHL number that has the problem.

## Tracking expired

An undelivered DHL picking validated more than 90 days ago gets the status `Status: Tracking expired - …` and is no longer tracked by any script. Past that point DHL either no longer knows the number or, since DHL reuses Express numbers, returns events of an unrelated newer shipment. The hourly check applies this rule on every run, without any DHL call. Expired pickings stay "not delivered" in Odoo.

## Alerts on DHL problems

A shipment gets a Helpdesk ticket only when DHL reports a problem, never just because it ships. The tracker raises an alert when all four conditions hold:

1. The latest DHL event of the shipment has one of the 19 problem codes: customs and payment (HP, CD, UD), failed delivery or address (ND, NH, MD, CA, CC, BA, CM), refused, returned or damaged (RD, RT, DD, PD, DS), or a DHL incident (OH, SS, MS, MC).
2. Odoo does not already show that code for the shipment.
3. The event is less than 7 days old. Older events are recorded in Odoo without an alert, so an old picking listed by the CLI cannot flood the Helpdesk.
4. DHL answered normally. Network errors, rate limits and DHL server errors never change what Odoo shows.

An alert then:

- **Opens a ticket**, except for the codes in `HELPDESK_SKIP_CODES` (default `OH,MD,NH`: on hold, missed delivery cycle, not home, which DHL usually resolves itself; they are reported on Mattermost only), in the `HELPDESK_TEAM` team with the `HELPDESK_TAGS` tags, the customer and sale order linked, and a priority by family (refused/returned/damaged: urgent; customs and failed delivery: high; DHL incidents: medium). If the shipment already has an open ticket, the new problem is added to it as an internal note instead. The tracker only writes internal notes, so the customer is never e-mailed.
- **Posts to Mattermost** when `ALERT_WEBHOOK_URL` is set (https only).
- **Records the code in Odoo**: the delivery's status field reads `Status: [HP] …`, which is how the tracker knows a problem was already reported.

An alert counts as sent as soon as one channel received it. If every channel fails, the code is not recorded and the alert is retried at the next check. A delivered shipment whose alert failed (for example DD, delivered damaged) stays tracked until the alert gets through.

A shareable reference of every code, with what each one means, is published as the "Codes de suivi DHL" page.

## Tests

The shared modules (Odoo and DHL clients, alerting, Helpdesk, the DHL-to-Odoo sync) have a pytest suite. Odoo, DHL and Mattermost are faked, so the tests need no network or credentials:

```bash
pip install -r requirements-dev.txt
pytest
pytest --cov=odoo_json2 --cov=odoo_client --cov=dhl_client --cov=shipment_alerts --cov=odoo_helpdesk --cov=alert_dispatch --cov=shipment_sync --cov-report=term-missing
```

`tests/test_automated_tracker.py` also covers the hourly check loop with fake Odoo, DHL and webhook clients.

```bash
pytest tests/test_automated_tracker.py
```

## Automated Tracking with Docker

`automated_tracker.py` runs as a long-lived container that restarts automatically.

### Quick start

```bash
cp .env.example .env            # fill in credentials
docker compose up -d --build    # build and start
docker compose logs -f dhl-tracker
docker compose down             # stop
```

The container reads `.env` when it is created. After editing `.env`, run `docker compose up -d` to recreate it; a plain restart keeps the old values.

### Schedule

| When | What happens |
|---|---|
| Startup | Loads shipments already marked delivered (last 90 days) so they are skipped, sends a startup notification, then runs a detailed check straight away |
| Every 10 minutes | **Simple check**: counts undelivered DHL shipments in Odoo and sends a short notification only if the count changed. No DHL calls |
| Every hour | **Detailed check**: flags undelivered pickings older than 90 days as tracking expired, then queries DHL for every undelivered shipment from the last 90 days (up to 100), updates `x_studio_delivered_` / `x_studio_last_status` in Odoo, sends a summary notification (in transit, newly delivered), and sends a separate report for shipments that have DHL next steps |

Tracker state is kept in memory and rebuilt from Odoo on every start.

Each detailed check makes one DHL call per tracked shipment, so it uses `24 × shipments` calls a day. With the default 250-call quota, that covers about 10 tracked shipments. Ask DHL for a higher service level before the 90-day window regularly holds more.

### Notifications

Notifications are Mattermost-formatted messages sent to `WEBHOOK_URL`. When `WEBHOOK_URL` is empty, nothing is sent and the tracker just logs `No webhook URL configured`. Odoo updates carry on as normal.

### Logs and health

The tracker logs to stdout; view the logs with `docker compose logs`. Docker keeps three rotated files of up to 10 MB each. The container health check only confirms that Python and `requests` load. It does not confirm that tracking is working, so check the logs.

## DHL API Documentation

- [Shipment Tracking - Unified API reference](https://developer.dhl.com/api-reference/shipment-tracking): endpoint `GET https://api-eu.dhl.com/track/shipments`, authenticated with the `DHL-API-Key` header
- `API/track_v1.5.8.yaml`: OpenAPI spec of the Unified Shipment Tracking API (v1.5.8)
- `API/status_1.csv`: status codes and descriptions per DHL service
- `API/parcel_de_ice_event_ric_combinations_July_2024.csv`: Parcel DE ICE event/RIC code table

## Odoo API Documentation

- [External JSON-2 API](https://www.odoo.com/documentation/19.0/developer/reference/external_api.html): `POST /json/2/<model>/<method>` with a bearer API key. It replaces XML-RPC/JSON-RPC, which are scheduled for removal in Odoo Online 21.1 (winter 2027) and Odoo 22.
