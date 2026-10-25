"""
Real-time floor sync: event publishing and live capacity math.

Every state mutation that the floor depends on publishes a structured event on
the Redis channel ``ops:tenant:{tenant_id}``. The ws-bridge pattern-subscribes
to that channel and forwards the payload to the WebSockets connected for that
tenant, so only connections belonging to the organization ever see the event.

Payloads always carry ``organization_id`` (and ``tenant_id``, which existing
consumers use) so a client can double-check the event belongs to its tenant.
"""

import json
from datetime import date, datetime, timezone
from enum import Enum
from math import ceil

from sqlmodel import Session, select

from . import models

# Baseline table turn time for a fully staffed floor. Turn time stretches as
# staffing drops below plan, so we stop promising quick turns while short.
BASE_TURN_MINUTES = 75

# Roles that do not seat guests or run the pass; they never cap floor capacity.
_NON_FLOOR_ROLES = {
    models.UserRole.courier,
    models.UserRole.provider,
    models.UserRole.platform_operator,
}


class OpsEvent(str, Enum):
    MENU_ITEM_86ED = "MENU_ITEM_86ED"
    SHIFT_CALLED_OUT = "SHIFT_CALLED_OUT"
    CAPACITY_UPDATED = "CAPACITY_UPDATED"
    TABLE_STATUS_CHANGED = "TABLE_STATUS_CHANGED"


def publish_ops_event(
    tenant_id: int | None,
    event: OpsEvent | str,
    payload: dict | None = None,
) -> None:
    """Publish one org-scoped event. Never raises: Redis outages must not fail a request."""
    if tenant_id is None:
        return
    event_value = event.value if isinstance(event, OpsEvent) else str(event)
    body = {
        "event": event_value,
        "organization_id": tenant_id,
        "tenant_id": tenant_id,
        "at": datetime.now(timezone.utc).isoformat(),
        **(payload or {}),
    }
    try:
        from .main import get_redis  # local import: main wires the Redis client

        r = get_redis()
        if r:
            r.publish(f"ops:tenant:{tenant_id}", json.dumps(body, default=str))
    except Exception:
        pass  # Fail silently if Redis is unavailable (same as order events)


def _required_by_role(role: models.UserRole, table_count: int) -> int:
    """How many people this role needs to run a floor with `table_count` tables."""
    if role in _NON_FLOOR_ROLES:
        return 0
    if role == models.UserRole.owner or role == models.UserRole.admin:
        return 1
    if role == models.UserRole.receptionist:
        return 2 if table_count > 6 else 1
    if role == models.UserRole.waiter:
        return max(1, ceil(table_count / 4))
    if role == models.UserRole.kitchen:
        return max(1, ceil(table_count / 8))
    if role == models.UserRole.bartender:
        return 1
    return 0


def compute_live_capacity(session: Session, tenant_id: int) -> dict:
    """
    Sellable seats / open tables / estimated turn time, derived from the floor
    and today's staffing ratios (the shift-to-table sync).

    - A shift counts as staffed when it is `active` or `covered`; `called_out`
      counts as missing until someone covers it.
    - Coverage is floored by the weakest role: extra waiters cannot paper over
      a missing line cook.
    - Sellable seats never drop below the seats already occupied.
    """
    tables = session.exec(
        select(models.Table).where(models.Table.tenant_id == tenant_id)
    ).all()
    total_tables = len(tables)
    total_seats = sum(t.seat_count for t in tables)
    occupied = [t for t in tables if t.is_active or t.active_order_id]
    occupied_seats = sum(t.seat_count for t in occupied)
    open_tables = total_tables - len(occupied)

    today = date.today()
    shifts = session.exec(
        select(models.Shift)
        .where(models.Shift.tenant_id == tenant_id)
        .where(models.Shift.shift_date == today)
    ).all()

    user_ids = {s.user_id for s in shifts}
    users = (
        session.exec(select(models.User).where(models.User.id.in_(user_ids))).all()
        if user_ids
        else []
    )
    role_by_user = {u.id: u.role for u in users}

    roles: list[dict] = []
    have = 0
    need = 0
    per_role_ratio: list[float] = []
    outstanding_callouts = 0

    for role in (
        models.UserRole.owner,
        models.UserRole.admin,
        models.UserRole.receptionist,
        models.UserRole.waiter,
        models.UserRole.kitchen,
        models.UserRole.bartender,
    ):
        role_shifts = [s for s in shifts if role_by_user.get(s.user_id) == role]
        missing = sum(
            1 for s in role_shifts if s.status == models.ShiftStatus.called_out
        )
        scheduled = len(role_shifts) - missing
        outstanding_callouts += missing
        required = _required_by_role(role, total_tables)
        have += scheduled
        need += required
        per_role_ratio.append(1.0 if required == 0 else min(1.0, scheduled / required))
        roles.append(
            {
                "role": role.value,
                "required": required,
                "scheduled": scheduled,
                "missing": missing,
            }
        )

    overall = 1.0 if need == 0 else have / need
    coverage = max(0.0, min(1.0, min([overall, *per_role_ratio])))
    weakest = next(
        (r["role"] for r in roles if r["required"] > 0 and r["scheduled"] < r["required"]),
        None,
    )

    sellable_seats = max(occupied_seats, round(total_seats * coverage))
    estimated_turn = round(BASE_TURN_MINUTES * (1 + (1 - coverage) * 0.8))

    return {
        "organization_id": tenant_id,
        "tenant_id": tenant_id,
        "date": today.isoformat(),
        "total_tables": total_tables,
        "open_tables": open_tables,
        "total_seats": total_seats,
        "occupied_seats": occupied_seats,
        "sellable_seats": sellable_seats,
        "coverage_ratio": round(coverage, 3),
        "estimated_turn_minutes": estimated_turn,
        "outstanding_callouts": outstanding_callouts,
        "weakest_role": weakest,
        "roles": roles,
    }


def publish_capacity_update(session: Session, tenant_id: int | None) -> dict | None:
    """Recompute capacity and broadcast it; used after staffing/floor changes."""
    if tenant_id is None:
        return None
    snapshot = compute_live_capacity(session, tenant_id)
    publish_ops_event(tenant_id, OpsEvent.CAPACITY_UPDATED, {"capacity": snapshot})
    return snapshot
