"""
POS sync webhook: external POS platforms (Toast, Square, ...) post ticket
state changes here so the floor view and turn-time estimates react without a
manual reload: `check.opened` seats a table, `check.paid` frees it.

Requests are authenticated with an HMAC-SHA256 signature of the raw body
(X-Pos-Signature: sha256=<hex>) using POS_WEBHOOK_SECRET.
"""

import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone

from sqlmodel import Session, select

from .. import models
from ..ops_events import OpsEvent, publish_capacity_update, publish_ops_event

logger = logging.getLogger(__name__)


def verify_signature(secret: str, raw_body: bytes, signature_header: str | None) -> bool:
    """Constant-time check of X-Pos-Signature (sha256=<hex> HMAC of the raw body)."""
    if not signature_header:
        return False
    expected = "sha256=" + hmac.new(
        secret.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def sign_body(secret: str, raw_body: bytes) -> str:
    """Build the signature header value (used by senders and tests)."""
    return "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()


def _find_table(session: Session, tenant_id: int, table_ref) -> models.Table | None:
    try:
        table_id = int(table_ref)
    except (TypeError, ValueError):
        return None
    return session.exec(
        select(models.Table).where(
            models.Table.tenant_id == tenant_id,
            models.Table.id == table_id,
        )
    ).first()


def _turn_minutes_since(started: datetime | None) -> int | None:
    if started is None:
        return None
    now = datetime.now(timezone.utc) if started.tzinfo else datetime.now()
    return max(0, round((now - started).total_seconds() / 60))


def handle_pos_event(platform: str, payload: dict, session: Session) -> dict:
    """
    Apply one POS ticket event to the floor and broadcast the change.

    - `check.opened`: the table is seated (is_active on).
    - `check.paid`:   the check closed and the table is free again; the actual
                      turn duration rides along in the TABLE_STATUS_CHANGED
                      event so the floor can learn real turn times.

    Unknown events are accepted and ignored (POS platforms send many kinds).
    """
    event = str(payload.get("event") or "")
    tenant_id = payload.get("organization_id") or payload.get("tenant_id")
    if not tenant_id:
        return {"handled": False, "reason": "missing organization_id"}
    try:
        tenant_id = int(tenant_id)
    except (TypeError, ValueError):
        return {"handled": False, "reason": "invalid organization_id"}

    table = (
        _find_table(session, tenant_id, payload.get("table_id"))
        if payload.get("table_id") is not None
        else None
    )
    check_id = payload.get("check_id")

    if event == "check.opened":
        if table and not table.is_active:
            table.is_active = True
            table.activated_at = datetime.now(timezone.utc)
            session.add(table)
            session.commit()
            publish_ops_event(
                tenant_id,
                OpsEvent.TABLE_STATUS_CHANGED,
                {
                    "table_id": table.id,
                    "status": "active",
                    "source": f"pos:{platform}",
                    "check_id": check_id,
                },
            )
        capacity = publish_capacity_update(session, tenant_id)
        return {
            "handled": True,
            "event": event,
            "table_id": table.id if table else None,
            "capacity": capacity,
        }

    if event == "check.paid":
        turn_minutes = None
        if table and table.is_active:
            turn_minutes = _turn_minutes_since(table.activated_at)
            table.is_active = False
            table.active_order_id = None
            session.add(table)
            session.commit()
            publish_ops_event(
                tenant_id,
                OpsEvent.TABLE_STATUS_CHANGED,
                {
                    "table_id": table.id,
                    "status": "free",
                    "source": f"pos:{platform}",
                    "check_id": check_id,
                    "turn_minutes": turn_minutes,
                },
            )
        capacity = publish_capacity_update(session, tenant_id)
        return {
            "handled": True,
            "event": event,
            "table_id": table.id if table else None,
            "turn_minutes": turn_minutes,
            "capacity": capacity,
        }

    logger.info("Ignoring unsupported POS event %r from %s", event, platform)
    return {"handled": False, "reason": f"unsupported event {event!r}"}


def parse_raw_body(raw: bytes) -> dict:
    """Decode the webhook body; raises ValueError on bad JSON."""
    data = json.loads(raw or b"{}")
    if not isinstance(data, dict):
        raise ValueError("body must be a JSON object")
    return data
