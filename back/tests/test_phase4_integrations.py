"""Phase 4 integrations: call-out voice broadcast and POS sync webhooks.

Covers:
- services/asterisk.py: eligible off-duty recipients, call-file spooling,
  no-op when ASTERISK_ENABLED=false, spool failures never break the call-out
- POST /shifts/{id}/pickup: IVR accept (token auth, idempotent cover)
- POST /webhooks/pos/{platform}: signature auth, check.opened/check.paid
  table seating/freeing, TABLE_STATUS_CHANGED + CAPACITY_UPDATED events
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import date, time, timedelta
from unittest import mock
from uuid import uuid4

from pg_client_mixin import PgClientTestCase

from app import models, security
from app.settings import settings


def _bearer_headers(user: models.User) -> dict[str, str]:
    data = {
        "sub": user.email,
        "tenant_id": user.tenant_id,
        "provider_id": getattr(user, "provider_id", None),
        "token_version": user.token_version,
    }
    token = security.create_access_token(data, expires_delta=timedelta(minutes=30))
    return {"Authorization": f"Bearer {token}"}


class FakeRedis:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def publish(self, channel: str, message: str) -> None:
        self.messages.append((channel, message))

    def ops_events(self) -> list[dict]:
        out = []
        for channel, message in self.messages:
            if channel.startswith("ops:tenant:"):
                out.append({"channel": channel, **json.loads(message)})
        return out


class _SettingsOverride:
    """Save/restore the settings singleton attributes a test touches."""

    def __init__(self, **overrides) -> None:
        self.overrides = overrides
        self._saved: dict = {}

    def __enter__(self):
        for key, value in self.overrides.items():
            self._saved[key] = getattr(settings, key)
            setattr(settings, key, value)
        return self

    def __exit__(self, *exc):
        for key, value in self._saved.items():
            setattr(settings, key, value)
        return False


class Phase4AsteriskTestCase(PgClientTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.spool_dir = tempfile.mkdtemp(prefix="asterisk-spool-")
        self.addCleanup(shutil.rmtree, self.spool_dir, ignore_errors=True)

        # Pick up the fake Redis so CAPACITY_UPDATED events are observable.
        import app.main as main_mod

        self.fake_redis = FakeRedis()
        self._orig_get_redis = main_mod.get_redis
        main_mod.get_redis = lambda: self.fake_redis  # type: ignore[assignment]
        self.addCleanup(self._restore_redis)

        tenant = models.Tenant(name=f"ASTERISK {uuid4().hex[:8]}")
        self.session.add(tenant)
        self.session.commit()
        self.session.refresh(tenant)
        self.tenant = tenant

        def make_user(role: models.UserRole, phone: str | None, tag: str) -> models.User:
            u = models.User(
                email=f"sms-{tag}-{uuid4().hex[:8]}@amvara.de",
                hashed_password=security.get_password_hash("x"),
                full_name=tag,
                tenant_id=tenant.id,
                role=role,
                phone=phone,
            )
            self.session.add(u)
            self.session.commit()
            self.session.refresh(u)
            return u

        self.owner = make_user(models.UserRole.owner, None, "owner")
        # Scheduled today -> must NOT be texted
        self.on_shift = make_user(models.UserRole.waiter, "+34611111111", "ons")
        # Off duty with phone -> must be texted
        self.off_duty = make_user(models.UserRole.waiter, "+34622222222", "off")
        # Off duty, no phone -> skipped
        self.no_phone = make_user(models.UserRole.waiter, None, "nophone")
        # Off duty, wrong role -> must NOT be texted
        self.kitchen = make_user(models.UserRole.kitchen, "+34633333333", "kitchen")

        self.session.add(
            models.Shift(
                tenant_id=tenant.id,
                user_id=self.on_shift.id,
                shift_date=date.today(),
                start_time=time(17, 0),
                end_time=time(23, 0),
            )
        )
        self.session.commit()

        # The shift that will be called out (on-shift waiter's shift).
        shift = models.Shift(
            tenant_id=tenant.id,
            user_id=self.on_shift.id,
            shift_date=date.today(),
            start_time=time(18, 0),
            end_time=time(23, 59),
        )
        self.session.add(shift)
        self.session.commit()
        self.session.refresh(shift)
        self.shift = shift

    def _restore_redis(self) -> None:
        import app.main as main_mod

        main_mod.get_redis = self._orig_get_redis

    def _configure_asterisk(self, token: str = ""):
        return _SettingsOverride(
            asterisk_enabled=True,
            asterisk_call_spool_dir=self.spool_dir,
            asterisk_outbound_context="from-internal",
            asterisk_pickup_token=token,
        )

    def _spooled_files(self) -> list[str]:
        import os

        return sorted(
            f for f in os.listdir(self.spool_dir) if f.endswith(".call")
        )

    def test_broadcast_spools_call_only_for_off_duty_same_role_with_phone(self) -> None:
        with self._configure_asterisk():
            r = self.client.post(
                f"/shifts/{self.shift.id}/call-out",
                params={"reason": "sick"},
                headers=_bearer_headers(self.owner),
            )
        self.assertEqual(r.status_code, 200, r.text)

        files = self._spooled_files()
        self.assertEqual(files, [f"callout_{self.shift.id}_+34622222222.call"])

        with open(f"{self.spool_dir}/{files[0]}") as f:
            content = f.read()
        self.assertIn("Channel: SIP/provider/+34622222222", content)
        self.assertIn("Context: from-internal", content)
        self.assertIn(f"Set: SHIFT_ID={self.shift.id}", content)
        self.assertIn("Set: SHIFT_ROLE=waiter", content)
        # On-duty, no-phone and wrong-role staff must not be called.
        self.assertNotIn("+34611111111", content)
        self.assertNotIn("+34633333333", content)

    def test_broadcast_is_noop_when_asterisk_disabled(self) -> None:
        # ASTERISK_ENABLED defaults to false in the container.
        with _SettingsOverride(asterisk_enabled=False, asterisk_call_spool_dir=self.spool_dir):
            r = self.client.post(
                f"/shifts/{self.shift.id}/call-out",
                headers=_bearer_headers(self.owner),
            )
        self.assertEqual(r.status_code, 200, r.text)  # call-out still succeeds
        self.assertEqual(self._spooled_files(), [])

    def test_spool_write_failure_does_not_raise(self) -> None:
        with self._configure_asterisk():
            with mock.patch("app.services.asterisk.os.replace", side_effect=OSError("disk full")):
                r = self.client.post(
                    f"/shifts/{self.shift.id}/call-out",
                    headers=_bearer_headers(self.owner),
                )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._spooled_files(), [])

    # --- POST /shifts/{id}/pickup (IVR accept) ---

    def _pickup(self, shift_id: int, token: str | None):
        headers = {"X-Asterisk-Token": token} if token is not None else {}
        return self.client.post(f"/shifts/{shift_id}/pickup", headers=headers)

    def test_pickup_unconfigured_returns_503(self) -> None:
        with _SettingsOverride(asterisk_pickup_token=""):
            r = self._pickup(self.shift.id, "anything")
        self.assertEqual(r.status_code, 503, r.text)

    def test_pickup_wrong_token_returns_403(self) -> None:
        with self._configure_asterisk(token="right-token"):
            r = self._pickup(self.shift.id, "wrong-token")
        self.assertEqual(r.status_code, 403, r.text)
        self.session.refresh(self.shift)
        self.assertNotEqual(self.shift.status, models.ShiftStatus.covered)

    def test_pickup_missing_token_returns_403(self) -> None:
        with self._configure_asterisk(token="right-token"):
            r = self._pickup(self.shift.id, None)
        self.assertEqual(r.status_code, 403, r.text)

    def test_pickup_covers_shift_and_publishes_capacity(self) -> None:
        with self._configure_asterisk(token="right-token"):
            r = self._pickup(self.shift.id, "right-token")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "covered")
        self.assertFalse(r.json()["already_covered"])

        self.session.refresh(self.shift)
        self.assertEqual(self.shift.status, models.ShiftStatus.covered)

        names = [e["event"] for e in self.fake_redis.ops_events()]
        self.assertIn("CAPACITY_UPDATED", names)
        self.assertTrue(
            all(e["organization_id"] == self.tenant.id for e in self.fake_redis.ops_events())
        )

    def test_pickup_is_idempotent(self) -> None:
        with self._configure_asterisk(token="t"):
            first = self._pickup(self.shift.id, "t")
            self.fake_redis.messages.clear()
            second = self._pickup(self.shift.id, "t")
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertTrue(second.json()["already_covered"])
        # No duplicate capacity storm on a repeated IVR callback.
        self.assertEqual(self.fake_redis.ops_events(), [])

    def test_pickup_unknown_shift_returns_404(self) -> None:
        with self._configure_asterisk(token="t"):
            r = self._pickup(99999999, "t")
        self.assertEqual(r.status_code, 404, r.text)


class Phase4PosWebhookTestCase(PgClientTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.fake_redis = FakeRedis()
        import app.main as main_mod

        self._orig_get_redis = main_mod.get_redis
        main_mod.get_redis = lambda: self.fake_redis  # type: ignore[assignment]

        tenant = models.Tenant(name=f"POS-WH {uuid4().hex[:8]}")
        self.session.add(tenant)
        self.session.commit()
        self.session.refresh(tenant)
        self.tenant = tenant

        owner = models.User(
            email=f"poswh-{uuid4().hex[:8]}@amvara.de",
            hashed_password=security.get_password_hash("x"),
            full_name="Owner",
            tenant_id=tenant.id,
            role=models.UserRole.owner,
        )
        self.session.add(owner)
        self.session.commit()
        self.session.refresh(owner)
        self.owner = owner

        table = models.Table(
            name="WH Table 1",
            token=f"tok-wh-{uuid4().hex}",
            tenant_id=tenant.id,
            x_position=0,
            y_position=0,
            rotation=0,
            shape="rectangle",
            width=1,
            height=1,
            seat_count=4,
            is_active=False,
        )
        self.session.add(table)
        self.session.commit()
        self.session.refresh(table)
        self.table = table

        self.secret = "pos-webhook-test-secret"

    def tearDown(self) -> None:
        import app.main as main_mod

        main_mod.get_redis = self._orig_get_redis
        super().tearDown()

    def _post(self, payload: dict, signature: str | None = None, raw: bytes | None = None):
        from app.services.pos_sync import sign_body

        body = raw if raw is not None else json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        if signature != "":
            headers["X-Pos-Signature"] = signature or sign_body(self.secret, body)
        with _SettingsOverride(pos_webhook_secret=self.secret):
            return self.client.post(
                "/webhooks/pos/toast", content=body, headers=headers
            )

    def test_unconfigured_returns_503(self) -> None:
        with _SettingsOverride(pos_webhook_secret=""):
            r = self.client.post(
                "/webhooks/pos/toast",
                content=b"{}",
                headers={"Content-Type": "application/json"},
            )
        self.assertEqual(r.status_code, 503, r.text)

    def test_bad_signature_returns_403(self) -> None:
        r = self._post(
            {"event": "check.paid", "organization_id": self.tenant.id},
            signature="sha256=" + "0" * 64,
        )
        self.assertEqual(r.status_code, 403, r.text)

    def test_missing_signature_returns_403(self) -> None:
        r = self._post(
            {"event": "check.paid", "organization_id": self.tenant.id},
            signature="",
        )
        self.assertEqual(r.status_code, 403, r.text)

    def test_check_opened_then_paid_frees_table_with_events(self) -> None:
        opened = self._post(
            {
                "event": "check.opened",
                "organization_id": self.tenant.id,
                "table_id": self.table.id,
                "check_id": "chk-1",
            }
        )
        self.assertEqual(opened.status_code, 200, opened.text)
        self.assertTrue(opened.json()["handled"])

        self.session.refresh(self.table)
        self.assertTrue(self.table.is_active)

        paid = self._post(
            {
                "event": "check.paid",
                "organization_id": self.tenant.id,
                "table_id": self.table.id,
                "check_id": "chk-1",
            }
        )
        self.assertEqual(paid.status_code, 200, paid.text)
        self.assertTrue(paid.json()["handled"])
        self.assertIn("turn_minutes", paid.json())

        self.session.refresh(self.table)
        self.assertFalse(self.table.is_active)

        events = self.fake_redis.ops_events()
        names = [e["event"] for e in events]
        self.assertIn("TABLE_STATUS_CHANGED", names)
        self.assertIn("CAPACITY_UPDATED", names)
        # Every event stays scoped to the organization
        self.assertTrue(
            all(e["organization_id"] == self.tenant.id for e in events)
        )
        statuses = [
            e["status"]
            for e in events
            if e["event"] == "TABLE_STATUS_CHANGED"
        ]
        self.assertEqual(statuses, ["active", "free"])

    def test_foreign_tenant_table_is_not_touched(self) -> None:
        other = models.Tenant(name=f"Other {uuid4().hex[:8]}")
        self.session.add(other)
        self.session.commit()
        self.session.refresh(other)

        r = self._post(
            {
                "event": "check.opened",
                "organization_id": self.tenant.id,
                "table_id": 999999,  # not in this tenant
            }
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(r.json()["table_id"])
        self.session.refresh(self.table)
        self.assertFalse(self.table.is_active)  # untouched

    def test_unknown_event_is_accepted_and_ignored(self) -> None:
        r = self._post(
            {"event": "item.voided", "organization_id": self.tenant.id}
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["handled"])

    def test_missing_organization_rejected(self) -> None:
        r = self._post({"event": "check.paid"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["handled"])
        self.assertIn("organization_id", r.json()["reason"])


if __name__ == "__main__":
    unittest.main()
