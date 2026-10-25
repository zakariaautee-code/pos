"""
Emergency shift call-out SMS broadcast via Twilio.

When a manager calls out a shift, every eligible off-duty staff member with the
same role in the same tenant and a phone number on file receives an SMS with a
link to pick up the shift. Twilio is optional: without credentials the service
is a no-op so call-outs never fail because notifications are unconfigured.
"""

import logging
from typing import TYPE_CHECKING

import requests
from sqlmodel import Session, select

from .. import models
from ..phone_utils import normalize_phone_to_e164
from ..settings import settings

if TYPE_CHECKING:
    from ..models import Shift

logger = logging.getLogger(__name__)

# Cap the broadcast so one call-out cannot burn the whole SMS quota at once.
MAX_SMS_RECIPIENTS = 25


def sms_configured() -> bool:
    """True when Twilio SMS credentials and a sender number are present."""
    return bool(
        settings.twilio_account_sid
        and settings.twilio_auth_token
        and settings.twilio_sms_from
    )


def _cover_link(shift_id: int) -> str:
    """Deep link into the shift planner so a pickup is one tap."""
    base = (settings.public_app_base_url or "").rstrip("/")
    return f"{base}/working-plan?cover_shift={shift_id}" if base else ""


def eligible_recipients(session: Session, shift: "Shift") -> list[models.User]:
    """
    Off-duty staff of the same role in the same tenant who have a phone on file.

    On-duty means any shift that day (any status): someone already scheduled is
    not a coverage candidate. The user who called out is excluded as well.
    """
    shift_user = session.get(models.User, shift.user_id)
    if not shift_user:
        return []

    on_duty_ids = {
        s.user_id
        for s in session.exec(
            select(models.Shift).where(
                models.Shift.tenant_id == shift.tenant_id,
                models.Shift.shift_date == shift.shift_date,
            )
        ).all()
    }
    candidates = session.exec(
        select(models.User).where(
            models.User.tenant_id == shift.tenant_id,
            models.User.role == shift_user.role,
        )
    ).all()
    return [
        u
        for u in candidates
        if u.id not in on_duty_ids
        and u.id != shift.user_id
        and (u.phone or "").strip()
    ]


def _message_body(
    tenant_name: str,
    role_label: str,
    shift: "Shift",
    reason: str | None,
    link: str,
) -> str:
    body = (
        f"{tenant_name}: cover needed. {role_label} shift "
        f"{shift.shift_date.isoformat()} "
        f"{shift.start_time.strftime('%H:%M')}-{shift.end_time.strftime('%H:%M')}"
    )
    if reason:
        body += f" (reason: reason)".replace("reason", reason)
    if link:
        body += f" - pick it up: {link}"
    return body


def broadcast_shift_callout(session: Session, shift: "Shift") -> dict:
    """
    SMS every eligible off-duty staff member about a shift call-out.

    Returns a summary dict and never raises: the call-out itself must succeed
    even when Twilio or the network is down.
    """
    summary: dict = {
        "configured": sms_configured(),
        "recipients": 0,
        "sent": 0,
        "failed": 0,
    }
    if not summary["configured"]:
        logger.info(
            "Twilio SMS not configured; skipping call-out broadcast for shift %s",
            shift.id,
        )
        return summary

    recipients = eligible_recipients(session, shift)[:MAX_SMS_RECIPIENTS]
    summary["recipients"] = len(recipients)
    if not recipients:
        return summary

    tenant = session.get(models.Tenant, shift.tenant_id)
    shift_user = session.get(models.User, shift.user_id)
    role_label = (
        shift_user.role.value if shift_user else "team"
    ).replace("_", " ").title()
    body = _message_body(
        tenant.name if tenant else "Restaurant",
        role_label,
        shift,
        shift.callout_reason,
        _cover_link(shift.id),
    )

    url = (
        "https://api.twilio.com/2010-04-01/Accounts/"
        f"{settings.twilio_account_sid}/Messages.json"
    )
    auth = (settings.twilio_account_sid, settings.twilio_auth_token)
    from_number = settings.twilio_sms_from.strip()
    if not from_number.startswith("+"):
        from_number = "+" + from_number

    for staff in recipients:
        e164 = normalize_phone_to_e164(staff.phone or "")
        if not e164:
            summary["failed"] += 1
            continue
        try:
            resp = requests.post(
                url,
                auth=auth,
                data={"From": from_number, "To": e164, "Body": body},
                timeout=15,
            )
            if resp.status_code in (200, 201):
                summary["sent"] += 1
            else:
                summary["failed"] += 1
                logger.error(
                    "Twilio SMS failed (%s): %s", resp.status_code, resp.text[:200]
                )
        except Exception:
            summary["failed"] += 1
            logger.exception("Twilio SMS error for shift %s", shift.id)

    logger.info(
        "Call-out broadcast for shift %s: %s sent, %s failed of %s recipients",
        shift.id,
        summary["sent"],
        summary["failed"],
        summary["recipients"],
    )
    return summary
