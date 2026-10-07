"""Odoo access for DHL tracking: shipment pickings, delivery status and partners.

Read and write helpers log failures and return an empty result (``None``,
``set()``, ``0`` or ``False``) so that one failed call never stops a
tracking run.
"""
import logging
from datetime import datetime
from typing import Any, Optional

from odoo_json2 import Json2Client, OdooConfig, OdooError

logger = logging.getLogger(__name__)

PICKING_MODEL = "stock.picking"
PARTNER_MODEL = "res.partner"
USERS_MODEL = "res.users"

# Odoo Studio custom fields on stock.picking
DELIVERED_FIELD = "x_studio_delivered_"
LAST_STATUS_FIELD = "x_studio_last_status"

CARRIER_NAME_FILTER = "DHL"
ODOO_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"
SHIPMENT_FIELDS = ["carrier_tracking_ref", "partner_id", "name", "date_done", "sale_id", LAST_STATUS_FIELD]
SHIPMENT_ORDER = "date_done desc"
PARTNER_FIELDS = ["name", "email", "phone", "street", "city", "zip", "country_id"]
UNKNOWN_PARTNER = "Unknown"
# Undelivered pickings past the tracking window: DHL has no data for them any more, or the
# number was reused for a newer shipment. Flagged so that no script tracks them again.
TRACKING_EXPIRED_PREFIX = "Status: Tracking expired"
TRACKING_EXPIRED_STATUS = f"{TRACKING_EXPIRED_PREFIX} - no DHL delivery recorded within the tracking window"
NOT_EXPIRED = [LAST_STATUS_FIELD, "not like", TRACKING_EXPIRED_PREFIX]
DEFAULT_SHIPMENT_LIMIT = 20


def _dhl_picking_domain(delivered: bool, since: Optional[datetime] = None) -> list:
    domain = [
        ["carrier_tracking_ref", "!=", False],
        ["carrier_id.name", "ilike", CARRIER_NAME_FILTER],
        ["state", "=", "done"],
        [DELIVERED_FIELD, "=", delivered],
    ]
    if since is None:
        return domain
    return [*domain, ["date_done", ">=", since.strftime(ODOO_DATETIME_FORMAT)]]


def _to_shipment(picking: dict) -> dict:
    partner = picking["partner_id"]
    partner_id, partner_name = (partner[0], partner[1]) if isinstance(partner, list) else (partner, UNKNOWN_PARTNER)
    sale_order = picking.get("sale_id")
    sale_order_id, sale_order_name = (sale_order[0], sale_order[1]) if isinstance(sale_order, list) else (None, None)
    return {
        "tracking_number": picking["carrier_tracking_ref"],
        "partner_id": partner_id,
        "partner_name": partner_name,
        "shipment_ref": picking["name"],
        "date_done": picking["date_done"],
        "picking_id": picking["id"],
        "sale_order_id": sale_order_id,
        "sale_order_name": sale_order_name,
        "last_status": picking.get(LAST_STATUS_FIELD) or "",
    }


def format_status_text(
    current_status: Optional[str], next_steps: Optional[str], event_code: Optional[str] = None
) -> str:
    """Build the multi-line text stored in the last-status field.

    The DHL event code is written as "Status: [HP] …"; shipment_alerts reads it back to
    alert only once per code, across restarts.
    """
    status = " ".join(part for part in (f"[{event_code}]" if event_code else None, current_status) if part)
    lines = (("Status", status), ("Next Steps", next_steps))
    return "\n".join(f"{label}: {value}" for label, value in lines if value)


class OdooClient:
    def __init__(self, config: Optional[OdooConfig] = None, transport: Optional[Any] = None):
        """Use ``transport`` when given, otherwise a JSON-2 client built from ``config`` or the environment."""
        self._transport = transport or Json2Client(config or OdooConfig.from_env())

    def connect(self) -> bool:
        """Check that Odoo accepts the API key."""
        try:
            context = self._transport.call(USERS_MODEL, "context_get")
        except OdooError as exc:
            logger.error("Odoo connection failed: %s", exc)
            return False
        logger.info("Connected to Odoo as user %s", context.get("uid"))
        return True

    def get_recent_shipments(
        self, limit: int = DEFAULT_SHIPMENT_LIMIT, since: Optional[datetime] = None
    ) -> Optional[list]:
        """Undelivered DHL shipments, newest first, optionally only those done since ``since``.

        None when Odoo could not be read, so that a failure never looks like "nothing to track".
        """
        try:
            pickings = self._transport.call(
                PICKING_MODEL,
                "search_read",
                domain=[*_dhl_picking_domain(delivered=False, since=since), NOT_EXPIRED],
                fields=SHIPMENT_FIELDS,
                limit=limit,
                order=SHIPMENT_ORDER,
            )
        except OdooError as exc:
            logger.error("Error fetching shipments: %s", exc)
            return None
        return [_to_shipment(picking) for picking in pickings]

    def expire_stale_tracking(self, older_than: datetime) -> int:
        """Flag undelivered DHL pickings done before `older_than` as "tracking expired". Returns how many."""
        domain = [
            *_dhl_picking_domain(delivered=False),
            ["date_done", "<", older_than.strftime(ODOO_DATETIME_FORMAT)],
            NOT_EXPIRED,
        ]
        try:
            picking_ids = self._transport.call(PICKING_MODEL, "search", domain=domain)
            if not picking_ids:
                return 0
            self._transport.call(PICKING_MODEL, "write", ids=picking_ids, vals={LAST_STATUS_FIELD: TRACKING_EXPIRED_STATUS})
        except OdooError as exc:
            logger.error("Error expiring stale DHL tracking: %s", exc)
            return 0
        logger.info("Tracking expired for %d DHL pickings done before %s", len(picking_ids), older_than.date())
        return len(picking_ids)

    def get_delivered_tracking_refs(self, limit: int, since: Optional[datetime] = None) -> set:
        """Tracking numbers of DHL shipments already flagged as delivered."""
        try:
            pickings = self._transport.call(
                PICKING_MODEL,
                "search_read",
                domain=_dhl_picking_domain(delivered=True, since=since),
                fields=["carrier_tracking_ref"],
                limit=limit,
                order=SHIPMENT_ORDER,
            )
        except OdooError as exc:
            logger.error("Error loading delivered shipments: %s", exc)
            return set()
        return {picking["carrier_tracking_ref"] for picking in pickings}

    def update_delivery_status(
        self,
        tracking_number: str,
        delivered: bool = True,
        current_status: Optional[str] = None,
        next_steps: Optional[str] = None,
        event_code: Optional[str] = None,
    ) -> bool:
        """Flag the pickings as delivered, or store the latest DHL status on them."""
        try:
            picking_ids = self._transport.call(
                PICKING_MODEL,
                "search",
                domain=[
                    ["carrier_tracking_ref", "=", tracking_number],
                    ["carrier_id.name", "ilike", CARRIER_NAME_FILTER],
                ],
            )
            if not picking_ids:
                logger.warning("No stock.picking found for tracking number %s", tracking_number)
                return False

            vals = (
                {DELIVERED_FIELD: True, LAST_STATUS_FIELD: ""}
                if delivered
                else {LAST_STATUS_FIELD: format_status_text(current_status, next_steps, event_code)}
            )
            self._transport.call(PICKING_MODEL, "write", ids=picking_ids, vals=vals)
        except OdooError as exc:
            logger.error("Error updating delivery status for %s: %s", tracking_number, exc)
            return False

        logger.debug("Updated %s for tracking %s: %s", PICKING_MODEL, tracking_number, vals)
        return True

    def get_partner_info(self, partner_id: Optional[int] = None, name: Optional[str] = None) -> Optional[dict]:
        """First partner matching ``partner_id``, or else ``name`` (case-insensitive)."""
        if partner_id:
            domain = [["id", "=", partner_id]]
        elif name:
            domain = [["name", "ilike", name]]
        else:
            return None

        try:
            partners = self._transport.call(PARTNER_MODEL, "search_read", domain=domain, fields=PARTNER_FIELDS, limit=1)
        except OdooError as exc:
            logger.error("Error fetching partner info: %s", exc)
            return None

        if not partners:
            return None
        partner = partners[0]
        country = partner.get("country_id")
        if isinstance(country, list):
            return {**partner, "country": country[1]}
        return partner
