"""
Server-side enforcement added for launch: short-lived Deepgram tokens, session
caps, and fail-closed Razorpay payments. No DB or network — the DB/Razorpay/
Deepgram calls are replaced with fakes.

Run: .venv/bin/python -m unittest test_cases.test_launch_hardening -v
"""
import hashlib
import hmac
import json
import unittest
from datetime import datetime, timezone
from unittest import mock
from uuid import uuid4

from fastapi.testclient import TestClient

import app.main as main
from modules import billing
from modules.billing import (
    PaymentError,
    SESSION_GRACE_SECONDS,
    SESSION_LIMIT_FREE_SECONDS,
    SESSION_LIMIT_PAID_SECONDS,
    check_paid_order,
    session_limit_for_tier,
    session_state,
)

USER = str(uuid4())
OTHER_USER = str(uuid4())
SESSION = str(uuid4())
PROFILE = str(uuid4())


def _ctx(elapsed=60.0, tier="free", ended_at=None):
    return {
        "resume_text": "resume",
        "job_title": "Engineer",
        "job_description": "Build things",
        "ended_at": ended_at,
        "elapsed_seconds": elapsed,
        "plan_tier": tier,
    }


def _order(plan="starter", user_id=USER, amount=None):
    return {
        "id": "order_1",
        "amount": billing.PLAN_CONFIG[plan]["amount"] if amount is None else amount,
        "currency": "INR",
        "notes": {"plan_id": plan, "user_id": user_id} if user_id else {"plan_id": plan},
    }


def _payment(plan="starter", status="captured", order_id="order_1", amount=None):
    return {
        "id": "pay_1",
        "order_id": order_id,
        "amount": billing.PLAN_CONFIG[plan]["amount"] if amount is None else amount,
        "currency": "INR",
        "status": status,
    }


class SessionRulesTest(unittest.TestCase):
    def test_limit_by_tier(self):
        self.assertEqual(session_limit_for_tier("free"), SESSION_LIMIT_FREE_SECONDS)
        self.assertEqual(session_limit_for_tier(None), SESSION_LIMIT_FREE_SECONDS)
        self.assertEqual(session_limit_for_tier("starter"), SESSION_LIMIT_PAID_SECONDS)
        self.assertEqual(session_limit_for_tier("pro"), SESSION_LIMIT_PAID_SECONDS)

    def test_state(self):
        free_cap = SESSION_LIMIT_FREE_SECONDS + SESSION_GRACE_SECONDS
        self.assertEqual(session_state(None, 30, "free"), "active")
        self.assertEqual(session_state(None, free_cap, "free"), "active")
        self.assertEqual(session_state(None, free_cap + 1, "free"), "expired")
        self.assertEqual(session_state(None, free_cap + 1, "pro"), "active")
        self.assertEqual(session_state(datetime.now(timezone.utc), 5, "pro"), "ended")


class CheckPaidOrderTest(unittest.TestCase):
    def test_captured_ok(self):
        r = check_paid_order(_order(), _payment())
        self.assertEqual(r, {"plan_id": "starter", "user_id": USER, "needs_capture": False})

    def test_authorized_needs_capture(self):
        self.assertTrue(check_paid_order(_order(), _payment(status="authorized"))["needs_capture"])

    def test_legacy_order_without_user(self):
        self.assertIsNone(check_paid_order(_order(user_id=None), _payment())["user_id"])

    def test_rejects(self):
        cases = {
            "other order": (_order(), _payment(order_id="order_2")),
            "unpaid": (_order(), _payment(status="failed")),
            "created": (_order(), _payment(status="created")),
            "underpaid order": (_order(amount=100), _payment(amount=100)),
            "plan price mismatch": (_order(plan="pro"), _payment(plan="starter")),
            "unknown plan": ({**_order(), "notes": {"plan_id": "gold"}}, _payment()),
        }
        for name, (order, payment) in cases.items():
            with self.subTest(name), self.assertRaises(PaymentError):
                check_paid_order(order, payment)


class ConfirmPaidOrderTest(unittest.TestCase):
    def test_captures_authorized(self):
        rz = mock.Mock()
        rz.order.fetch.return_value = _order()
        rz.payment.fetch.return_value = _payment(status="authorized")
        with mock.patch.object(billing, "rz_client", rz):
            r = billing.confirm_paid_order("order_1", "pay_1")
        rz.payment.capture.assert_called_once_with("pay_1", 79900, {"currency": "INR"})
        self.assertEqual(r, {"plan_id": "starter", "user_id": USER})

    def test_capture_race_already_captured(self):
        rz = mock.Mock()
        rz.order.fetch.return_value = _order()
        rz.payment.fetch.side_effect = [_payment(status="authorized"), _payment(status="captured")]
        rz.payment.capture.side_effect = RuntimeError("already captured")
        with mock.patch.object(billing, "rz_client", rz):
            self.assertEqual(billing.confirm_paid_order("order_1", "pay_1")["plan_id"], "starter")

    def test_razorpay_down_propagates(self):
        rz = mock.Mock()
        rz.order.fetch.side_effect = ConnectionError("down")
        with mock.patch.object(billing, "rz_client", rz), self.assertRaises(ConnectionError):
            billing.confirm_paid_order("order_1", "pay_1")


class _ApiTest(unittest.TestCase):
    def setUp(self):
        main.app.dependency_overrides[main.require_user_id] = lambda: USER
        self.client = TestClient(main.app)
        self.patches = []

    def tearDown(self):
        main.app.dependency_overrides.clear()
        for p in self.patches:
            p.stop()

    def patch(self, name, **kw):
        p = mock.patch.object(main, name, **kw)
        self.patches.append(p)
        return p.start()


class ProviderKeyEndpointsTest(_ApiTest):
    def test_raw_key_endpoints_and_listen_are_gone(self):
        self.assertEqual(self.client.get("/deepgram/api_key").status_code, 404)
        self.assertEqual(self.client.get("/gemini/api_key").status_code, 404)
        paths = {getattr(r, "path", None) for r in main.app.routes}
        self.assertNotIn("/listen", paths)

    def _fake_httpx(self, payload=None, error=None):
        calls = []

        class FakeResponse:
            def raise_for_status(self):
                if error:
                    raise error

            def json(self):
                return payload

        class FakeClient:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, headers=None, json=None):
                calls.append((url, headers, json))
                return FakeResponse()

        self.patch("httpx", new=mock.Mock(AsyncClient=FakeClient))
        return calls

    def test_token_for_active_session(self):
        self.patch("get_session_prompt_context", new=mock.AsyncMock(return_value=_ctx()))
        calls = self._fake_httpx({"access_token": "jwt", "expires_in": 60})
        with mock.patch.object(main.settings, "deepgram_api_key", "dg-secret"):
            r = self.client.post("/deepgram/token", json={"session_id": SESSION})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"access_token": "jwt", "expires_in": 60})
        self.assertNotIn("dg-secret", r.text)
        url, headers, body = calls[0]
        self.assertEqual(url, "https://api.deepgram.com/v1/auth/grant")
        self.assertEqual(headers["Authorization"], "Token dg-secret")
        self.assertEqual(body, {"ttl_seconds": 60})

    def test_token_refused_for_ended_session(self):
        self.patch("get_session_prompt_context",
                   new=mock.AsyncMock(return_value=_ctx(ended_at=datetime.now(timezone.utc))))
        calls = self._fake_httpx({"access_token": "jwt"})
        r = self.client.post("/deepgram/token", json={"session_id": SESSION})
        self.assertEqual(r.status_code, 410)
        self.assertEqual(r.json()["detail"]["error"], "session_ended")
        self.assertEqual(calls, [])

    def test_token_grant_failure_hides_details(self):
        self.patch("get_session_prompt_context", new=mock.AsyncMock(return_value=_ctx()))
        self._fake_httpx(error=RuntimeError("401 key dg-secret invalid"))
        with mock.patch.object(main.settings, "deepgram_api_key", "dg-secret"):
            r = self.client.post("/deepgram/token", json={"session_id": SESSION})
        self.assertEqual(r.status_code, 502)
        self.assertNotIn("dg-secret", r.text)


class SessionEnforcementTest(_ApiTest):
    def _stream(self):
        async def gen(question, system_prompt=None):
            yield "answer"
        self.patch("select_stream", return_value=gen)
        self.patch("insert_interview_response", new=mock.AsyncMock())

    def test_answer_in_active_session(self):
        self._stream()
        self.patch("get_session_prompt_context", new=mock.AsyncMock(return_value=_ctx()))
        r = self.client.post("/ai-answer", json={"session_id": SESSION, "question": "q?"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.text, "answer")

    def test_answer_refused_after_end(self):
        self._stream()
        self.patch("get_session_prompt_context",
                   new=mock.AsyncMock(return_value=_ctx(ended_at=datetime.now(timezone.utc))))
        r = self.client.post("/ai-answer", json={"session_id": SESSION, "question": "q?"})
        self.assertEqual(r.status_code, 410)

    def test_overrun_session_is_closed_at_cap(self):
        self._stream()
        self.patch("get_session_prompt_context",
                   new=mock.AsyncMock(return_value=_ctx(elapsed=3 * 3600, tier="free")))
        close = self.patch("close_expired_session", new=mock.AsyncMock())
        r = self.client.post("/ai-answer", json={"session_id": SESSION, "question": "q?"})
        self.assertEqual(r.status_code, 410)
        close.assert_awaited_once()
        self.assertEqual(close.await_args.args[2], SESSION_LIMIT_FREE_SECONDS)

    def test_screen_refused_after_end(self):
        self.patch("get_session_prompt_context",
                   new=mock.AsyncMock(return_value=_ctx(ended_at=datetime.now(timezone.utc))))
        extract = self.patch("extract_text", new=mock.AsyncMock(return_value="text"))
        r = self.client.post(
            "/analyse-screen",
            data={"session_id": SESSION},
            files={"file": ("s.png", b"img", "image/png")},
        )
        self.assertEqual(r.status_code, 410)
        extract.assert_not_awaited()

    def test_unknown_session(self):
        self.patch("get_session_prompt_context", new=mock.AsyncMock(return_value=None))
        r = self.client.post("/ai-answer", json={"session_id": SESSION, "question": "q?"})
        self.assertEqual(r.status_code, 404)


class StartSessionTest(_ApiTest):
    body = {"profile_id": PROFILE, "job_title": "Engineer", "job_description": "Build things"}

    def _open(self, elapsed, **over):
        from uuid import UUID
        row = {"id": uuid4(), "profile_id": UUID(PROFILE), "job_title": "Engineer",
               "job_description": "Build things", "elapsed_seconds": elapsed, "plan_tier": "free"}
        row.update(over)
        return row

    def _setup(self, open_session=None, credit=True, created=True):
        self.patch("get_open_interview_session", new=mock.AsyncMock(return_value=open_session))
        self.consume = self.patch("consume_session_credit", new=mock.AsyncMock(return_value=credit))
        self.refund = self.patch("refund_session_credit", new=mock.AsyncMock())
        self.create = self.patch("create_interview_session",
                                 new=mock.AsyncMock(return_value=uuid4() if created else None))
        self.close = self.patch("close_expired_session", new=mock.AsyncMock())
        self.end = self.patch("end_interview_session", new=mock.AsyncMock(return_value=True))

    def test_new_session_spends_a_credit(self):
        self._setup()
        r = self.client.post("/interview/session", json=self.body)
        self.assertEqual(r.status_code, 200)
        self.consume.assert_awaited_once_with(USER)
        self.assertNotIn("reused", r.json())

    def test_out_of_credits(self):
        self._setup(credit=False)
        r = self.client.post("/interview/session", json=self.body)
        self.assertEqual(r.status_code, 402)
        self.assertEqual(r.json()["detail"]["error"], "quota_exceeded")
        self.create.assert_not_awaited()

    def test_double_submit_reuses_without_charge(self):
        open_session = self._open(30)
        self._setup(open_session=open_session, credit=False)
        r = self.client.post("/interview/session", json=self.body)
        self.assertEqual(r.json(), {"session_id": str(open_session["id"]), "reused": True})
        self.consume.assert_not_awaited()

    def test_different_job_closes_old_session(self):
        self._setup(open_session=self._open(30, job_title="Designer"))
        r = self.client.post("/interview/session", json=self.body)
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("reused", r.json())
        self.end.assert_awaited_once()
        self.consume.assert_awaited_once()

    def test_expired_open_session_is_not_reused(self):
        self._setup(open_session=self._open(5 * 3600))
        r = self.client.post("/interview/session", json=self.body)
        self.assertNotIn("reused", r.json())
        self.close.assert_awaited_once()
        self.assertEqual(self.close.await_args.args[2], SESSION_LIMIT_FREE_SECONDS)

    def test_bad_profile_refunds(self):
        self._setup(created=False)
        r = self.client.post("/interview/session", json=self.body)
        self.assertEqual(r.status_code, 404)
        self.refund.assert_awaited_once_with(USER)


class PaymentVerifyTest(_ApiTest):
    def _body(self, plan="starter"):
        return {"razorpay_order_id": "order_1", "razorpay_payment_id": "pay_1",
                "razorpay_signature": "sig", "plan_id": plan}

    def _setup(self, confirm):
        self.patch("verify_payment_signature", return_value=True)
        self.patch("confirm_paid_order", **confirm)
        self.credit = self.patch("credit_sessions", new=mock.AsyncMock(return_value=True))

    def test_ok(self):
        self._setup({"return_value": {"plan_id": "starter", "user_id": USER}})
        r = self.client.post("/payment/verify", json=self._body())
        self.assertEqual(r.json(), {"ok": True, "sessions_credited": 7})
        self.credit.assert_awaited_once_with(USER, "starter", "order_1", "pay_1")

    def test_razorpay_unreachable_fails_closed(self):
        self._setup({"side_effect": ConnectionError("down")})
        r = self.client.post("/payment/verify", json=self._body())
        self.assertEqual(r.status_code, 502)
        self.credit.assert_not_awaited()

    def test_cheaper_order_cannot_buy_pro(self):
        self._setup({"return_value": {"plan_id": "starter", "user_id": USER}})
        r = self.client.post("/payment/verify", json=self._body(plan="pro"))
        self.assertEqual(r.status_code, 400)
        self.credit.assert_not_awaited()

    def test_someone_elses_order(self):
        self._setup({"return_value": {"plan_id": "starter", "user_id": OTHER_USER}})
        r = self.client.post("/payment/verify", json=self._body())
        self.assertEqual(r.status_code, 403)
        self.credit.assert_not_awaited()

    def test_unpaid(self):
        self._setup({"side_effect": PaymentError("Payment is 'failed', not paid")})
        r = self.client.post("/payment/verify", json=self._body())
        self.assertEqual(r.status_code, 400)
        self.credit.assert_not_awaited()

    def test_bad_signature(self):
        self.patch("verify_payment_signature", return_value=False)
        confirm = self.patch("confirm_paid_order")
        r = self.client.post("/payment/verify", json=self._body())
        self.assertEqual(r.status_code, 400)
        confirm.assert_not_called()


class PaymentWebhookTest(_ApiTest):
    SECRET = "whsec"

    def setUp(self):
        super().setUp()
        p = mock.patch.object(main.settings, "razorpay_webhook_secret", self.SECRET)
        p.start()
        self.patches.append(p)
        self.credit = self.patch("credit_sessions", new=mock.AsyncMock(return_value=True))

    def _post(self, event, signature=None):
        body = json.dumps(event).encode()
        sig = signature or hmac.new(self.SECRET.encode(), body, hashlib.sha256).hexdigest()
        return self.client.post("/payment/webhook", content=body,
                                headers={"X-Razorpay-Signature": sig, "Content-Type": "application/json"})

    def _event(self, name="payment.captured"):
        return {"event": name, "payload": {"payment": {"entity": {"id": "pay_1", "order_id": "order_1"}}}}

    def test_credits_order_owner(self):
        self.patch("confirm_paid_order", return_value={"plan_id": "pro", "user_id": USER})
        r = self._post(self._event())
        self.assertEqual(r.status_code, 200)
        self.credit.assert_awaited_once_with(USER, "pro", "order_1", "pay_1")

    def test_bad_signature(self):
        confirm = self.patch("confirm_paid_order")
        r = self._post(self._event(), signature="0" * 64)
        self.assertEqual(r.status_code, 400)
        confirm.assert_not_called()

    def test_razorpay_unreachable_asks_for_retry(self):
        self.patch("confirm_paid_order", side_effect=ConnectionError("down"))
        r = self._post(self._event())
        self.assertEqual(r.status_code, 503)
        self.credit.assert_not_awaited()

    def test_other_events_ignored(self):
        confirm = self.patch("confirm_paid_order")
        r = self._post(self._event("payment.failed"))
        self.assertEqual(r.json(), {"ok": True, "ignored": True})
        confirm.assert_not_called()

    def test_legacy_order_without_owner_not_credited(self):
        self.patch("confirm_paid_order", return_value={"plan_id": "pro", "user_id": None})
        r = self._post(self._event("order.paid"))
        self.assertEqual(r.status_code, 200)
        self.credit.assert_not_awaited()

    def test_unconfigured(self):
        with mock.patch.object(main.settings, "razorpay_webhook_secret", ""):
            r = self._post(self._event(), signature="x")
        self.assertEqual(r.status_code, 503)


if __name__ == "__main__":
    unittest.main()
