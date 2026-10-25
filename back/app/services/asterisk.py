"""
Emergency shift call-out voice broadcast via Asterisk (replaces Twilio SMS).

When a manager calls out a shift, every eligible off-duty staff member with the
same role in the same tenant and a phone number on file gets an outbound call
queued in the Asterisk spool. The PBX dialplan (docs/asterisk/extensions.conf)
answers, plays the urgent-shift audio, and presses-through to
POST /shifts/{id}/pickup when the employee accepts.

Asterisk is optional: with ASTERISK_ENABLED=false (default) the service is a
no-op so call-outs never fail because telephony is unconfigured.
"""

import logging
import os
from typing import TYPE_CHECKING

from sqlmodel import Session, select

from .. import models
from ..phone_utils import normalize_phone_to_e164
from ..settings import settings

if TYPE_CHECKING:
    from ..models import Shift

logger = logging.getLogger(__name__)

# Cap the broadcast so one call-out cannot tie up the whole PBX at once.
MAX_CALL_RECIPIENTS = 25


def asterisk_configured() -> bool:
    """True when Asterisk call-out broadcasting is switched on."""
    return bool(getattr(settings, "asterisk_enabled", False))


def generate_call_file(phone_number: str, shift_id: str | int, role: str) -> str:
    """Asterisk spool call-file content for automated IVR dispatch."""
    return f"""Channel: SIP/provider/{phone_number}
MaxRetries: 2
RetryTime: 60
WaitTime: 30
Context: {settings.asterisk_outbound_context}
Extension: s
Priority: 1
Set: SHIFT_ID={shift_id}
Set: SHIFT_ROLE={role}
"""


def send_shift_callout_broadcast(
    phone_numbers: list[str],
    shift_id: str | int,
    role: str,
    stats: dict | None = None,
) -> bool:
    """
    Queue emergency shift broadcast calls by spooling call files for Asterisk.

    The file is written to a temp name inside the spool directory and then
    renamed atomically, so Asterisk (which watches for *.call) never reads a
    partially written file. Returns True when at least one call was queued.
    """
    if not asterisk_configured():
        logger.warning("Asterisk service is disabled in config. Skipping broadcast.")
        return False

    spool_dir = settings.asterisk_call_spool_dir
    try:
        os.makedirs(spool_dir, exist_ok=True)
    except OSError as e:
        logger.error("Cannot create Asterisk spool dir %s: %s", spool_dir, e)
        return False

    success_count = 0
    for phone in phone_numbers:
        try:
            call_content = generate_call_file(phone, shift_id, role)
            filename = f"callout_{shift_id}_{phone}.call"
            temp_path = os.path.join(spool_dir, filename + ".tmp")
            spool_path = os.path.join(spool_dir, filename)

            # Temp lives in the spool dir itself: os.rename across filesystems
            # (/tmp -> spool) would fail with EXDEV inside containers.
            with open(temp_path, "w") as f:
                f.write(call_content)

            os.replace(temp_path, spool_path)
            success_count += 1
            logger.info("Queued Asterisk call for shift %s to %s", shift_id, phone)
        except Exception as e:
            logger.error("Failed to queue Asterisk call for %s: %s", phone, e)

    if stats is not None:
        stats["queued"] = success_count
        stats["failed"] = len(phone_numbers) - success_count
    return success_count > 0


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


def broadcast_shift_callout(session: Session, shift: "Shift") -> dict:
    """
    Queue a broadcast call to every eligible off-duty staff member.

    Returns a summary dict and never raises: the call-out itself must succeed
    even when the PBX or spool directory is unavailable.
    """
    summary: dict = {
        "enabled": asterisk_configured(),
        "recipients": 0,
        "queued": 0,
        "failed": 0,
    }
    if not summary["enabled"]:
        logger.info(
            "Asterisk disabled; skipping call-out broadcast for shift %s", shift.id
        )
        return summary

    recipients = eligible_recipients(session, shift)
    phones: list[str] = []
    for staff in recipients:
        e164 = normalize_phone_to_e164(staff.phone or "")
        if e164:
            phones.append(e164)
        else:
            summary["failed"] += 1
    phones = phones[:MAX_CALL_RECIPIENTS]
    summary["recipients"] = len(recipients)

    if not phones:
        return summary

    shift_user = session.get(models.User, shift.user_id)
    role = shift_user.role.value if shift_user else "team"

    stats: dict = {"queued": 0, "failed": 0}
    send_shift_callout_broadcast(phones, shift.id, role, stats=stats)
    summary["queued"] = stats["queued"]
    summary["failed"] += stats["failed"]

    logger.info(
        "Call-out broadcast for shift %s: %s queued, %s failed of %s recipients",
        shift.id,
        summary["queued"],
        summary["failed"],
        summary["recipients"],
    )
    return summary
