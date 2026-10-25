"""Floor sync endpoints: instant 86, emergency call-out, live capacity.

Covers Phase 3 of the production-readiness plan:
- PATCH /products/{id}/86
- POST /shifts/{id}/call-out
- GET /capacity/live
- RBAC on the new routes
- org-scoped Redis events (MENU_ITEM_86ED, SHIFT_CALLED_OUT, CAPACITY_UPDATED)
"""
from __future__ import annotations

import json
import unittest
from datetime import date, time, timedelta
from uuid import uuid4

from pg_client_mixin import PgClientTestCase

from app import models, security


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
    """Captures publishes instead of talking to a real Redis."""

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


class OpsFloorSyncTestCase(PgClientTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.fake_redis = FakeRedis()
        import app.main as main_mod

        self._orig_get_redis = main_mod.get_redis
        main_mod.get_redis = lambda: self.fake_redis  # type: ignore[assignment]

        tenant = models.Tenant(name=f"OpsSync {uuid4().hex[:8]}")
        self.session.add(tenant)
        self.session.commit()
        self.session.refresh(tenant)
        self.tenant = tenant

        owner = models.User(
            email=f"ops-owner-{uuid4().hex[:8]}@amvara.de",
            hashed_password=security.get_password_hash("x"),
            full_name="Owner",
            tenant_id=tenant.id,
            role=models.UserRole.owner,
        )
        waiter = models.User(
            email=f"ops-waiter-{uuid4().hex[:8]}@amvara.de",
            hashed_password=security.get_password_hash("x"),
            full_name="Waiter",
            tenant_id=tenant.id,
            role=models.UserRole.waiter,
        )
        self.session.add_all([owner, waiter])
        self.session.commit()
        self.session.refresh(owner)
        self.session.refresh(waiter)
        self.owner = owner
        self.waiter = waiter

        # Two tables so capacity math has real seats to work with.
        for idx in range(2):
            self.session.add(
                models.Table(
                    name=f"T-ops-{idx}",
                    token=f"tok-ops-{uuid4().hex}",
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
            )
        self.session.commit()

        product = models.Product(
            name="Ops Tagine",
            price_cents=2000,
            tenant_id=tenant.id,
        )
        self.session.add(product)
        self.session.commit()
        self.session.refresh(product)
        self.product = product

        shift = models.Shift(
            tenant_id=tenant.id,
            user_id=self.waiter.id,
            shift_date=date.today(),
            start_time=time(17, 0),
            end_time=time(23, 0),
        )
        self.session.add(shift)
        self.session.commit()
        self.session.refresh(shift)
        self.shift = shift

    def tearDown(self) -> None:
        import app.main as main_mod

        main_mod.get_redis = self._orig_get_redis
        super().tearDown()

    # --- Phase 3.1: endpoints -------------------------------------------------

    def test_menu_86_toggles_and_broadcasts(self) -> None:
        h = _bearer_headers(self.owner)

        r = self.client.patch(f"/products/{self.product.id}/86", headers=h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["is_86ed"])

        # Second call flips it back
        r = self.client.patch(f"/products/{self.product.id}/86", headers=h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["is_86ed"])

        events = self.fake_redis.ops_events()
        menu_events = [e for e in events if e["event"] == "MENU_ITEM_86ED"]
        self.assertEqual(len(menu_events), 2)
        # Scoped to the organization that owns the product
        self.assertEqual(menu_events[0]["organization_id"], self.tenant.id)
        self.assertEqual(menu_events[0]["product_id"], self.product.id)

    def test_menu_86_requires_product_write_permission(self) -> None:
        h = _bearer_headers(self.waiter)
        r = self.client.patch(f"/products/{self.product.id}/86", headers=h)
        self.assertEqual(r.status_code, 403, r.text)

    def test_shift_call_out_sets_status_and_reason(self) -> None:
        h = _bearer_headers(self.owner)
        r = self.client.post(
            f"/shifts/{self.shift.id}/call-out",
            params={"reason": "sick"},
            headers=h,
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["status"], "called_out")
        self.assertEqual(body["callout_reason"], "sick")

        self.session.refresh(self.shift)
        self.assertEqual(self.shift.status, models.ShiftStatus.called_out)
        self.assertEqual(self.shift.callout_reason, "sick")

        events = self.fake_redis.ops_events()
        names = {e["event"] for e in events}
        self.assertIn("SHIFT_CALLED_OUT", names)
        self.assertIn("CAPACITY_UPDATED", names)

        callout = next(e for e in events if e["event"] == "SHIFT_CALLED_OUT")
        self.assertEqual(callout["organization_id"], self.tenant.id)
        self.assertEqual(callout["shift_id"], self.shift.id)

    def test_call_out_reports_short_role_in_capacity(self) -> None:
        h = _bearer_headers(self.owner)
        self.client.post(f"/shifts/{self.shift.id}/call-out", headers=h)

        r = self.client.get("/capacity/live", headers=h)
        self.assertEqual(r.status_code, 200, r.text)
        cap = r.json()
        self.assertEqual(cap["organization_id"], self.tenant.id)
        self.assertGreaterEqual(cap["outstanding_callouts"], 1)
        waiter_role = next(x for x in cap["roles"] if x["role"] == "waiter")
        self.assertGreaterEqual(waiter_role["missing"], 1)
        self.assertGreaterEqual(cap["estimated_turn_minutes"], 75)

    def test_live_capacity_shape_and_seat_floor(self) -> None:
        h = _bearer_headers(self.owner)
        r = self.client.get("/capacity/live", headers=h)
        self.assertEqual(r.status_code, 200, r.text)
        cap = r.json()
        for key in (
            "total_seats",
            "sellable_seats",
            "open_tables",
            "total_tables",
            "coverage_ratio",
            "estimated_turn_minutes",
            "roles",
        ):
            self.assertIn(key, cap)
        self.assertEqual(cap["total_seats"], 8)
        # Never sell fewer seats than are already occupied (0 here)
        self.assertGreaterEqual(cap["sellable_seats"], 0)
        self.assertLessEqual(cap["sellable_seats"], cap["total_seats"])

    def test_capacity_requires_auth(self) -> None:
        r = self.client.get("/capacity/live")
        self.assertIn(r.status_code, (401, 403), r.text)


if __name__ == "__main__":
    unittest.main()
