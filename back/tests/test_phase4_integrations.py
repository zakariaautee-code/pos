"""Phase 4 integrations: call-out SMS broadcast and POS sync webhooks.

Covers:
- services/sms.py: eligible off-duty recipients, Twilio no-op when unconfigured
- POST /webhooks/pos/{platform}: signature auth, check.opened/check.paid
  table seating/freeing, TABLE_STATUS_CHANGED + CAPACITY_UPDATED events
"""
from __future__ import annotations

import json
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


class Phase4SmsTestCase(PgClientTestCase):
    def setUp(self) -> None:
        super().setUp()
        tenant = models.Tenant(name=f"SMS {uuid4().hex[:8]}")
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

    def _configure_twilio(self):
        return _SettingsOverride(
            twilio_account_sid="AC_test",
            twilio_auth_token="token_test",
            twilio_sms_from="+14155238886",
            public_app_base_url="https://pos.example.com",
        )

    def test_broadcast_reaches_only_off_duty_same_role_with_phone(self) -> None:
        with self._configure_twilio():
            with mock.patch("app.services.sms.requests.post") as post:
                post.return_value = mock.Mock(status_code=201)
                r = self.client.post(
                    f"/shifts/{self.shift.id}/call-out",
                    params={"reason": "sick"},
                    headers=_bearer_headers(self.owner),
                )
        self.assertEqual(r.status_code, 200, r.text)

        sent_to = {call.kwargs["data"]["To"] for call in post.call_args_list}
        self.assertEqual(sent_to, {"+34622222222"})

        body = post.call_args_list[0].kwargs["data"]["Body"]
        self.assertIn("cover needed", body)
        self.assertIn("sick", body)
        # One-tap pickup link
        self.assertIn(
            f"/working-plan?cover_shift={self.shift.id}", body
        )

    def test_broadcast_is_noop_without_twilio(self) -> None:
        # Settings are empty in the container by default.
        with mock.patch("app.services.sms.requests.post") as post:
            r = self.client.post(
                f"/shifts/{self.shift.id}/call-out",
                headers=_bearer_headers(self.owner),
            )
        self.assertEqual(r.status_code, 200, r.text)  # call-out still succeeds
        post.assert_not_called()

    def test_single_send_failure_does_not_raise(self) -> None:
        with self._configure_twilio():
            with mock.patch("app.services.sms.requests.post") as post:
                post.side_effect = RuntimeError("network down")
                r = self.client.post(
                    f"/shifts/{self.shift.id}/call-out",
                    headers=_bearer_headers(self.owner),
                )
        self.assertEqual(r.status_code, 200, r.text)


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
