"""
Launch security hardening: health check, input limits, rate limits, password
reset, account export/deletion, desktop OAuth nonce, refresh-token reuse
detection, APP_SECRET_KEY, and LLM stream failures. No DB or network — every
DB/HTTP/LLM call is replaced with a fake.

Run: .venv/bin/python -m unittest test_cases.test_launch_security -v
"""
import asyncio
import json
import time
import unittest
import urllib.parse
from datetime import datetime, timezone
from unittest import mock
from uuid import uuid4

import anthropic
import httpx

import app.main as main
from modules import app_tokens, auth, auth_google, llm
from test_cases.test_launch_hardening import SESSION, USER, _ApiTest, _ctx

NONCE = "n" * 40


class HealthTest(_ApiTest):
    def test_ok(self):
        self.patch("_db_ping", new=mock.AsyncMock())
        r = self.client.get("/health")
        self.assertEqual((r.status_code, r.json()), (200, {"ok": True}))

    def test_db_down(self):
        self.patch("_db_ping", new=mock.AsyncMock(side_effect=asyncio.TimeoutError()))
        r = self.client.get("/health")
        self.assertEqual((r.status_code, r.json()), (503, {"ok": False}))


class InputLimitsTest(_ApiTest):
    def test_resume_too_large(self):
        extract = self.patch("extract_resume_text")
        r = self.client.post(
            "/save_profile",
            data={"job_role": "Eng", "name": "Me"},
            files={"resume": ("cv.pdf", b"x" * (main.MAX_RESUME_BYTES + 1), "application/pdf")},
        )
        self.assertEqual(r.status_code, 413)
        extract.assert_not_called()

    def test_screen_image_too_large(self):
        self.patch("get_session_prompt_context", new=mock.AsyncMock(return_value=_ctx()))
        extract = self.patch("extract_text", new=mock.AsyncMock(return_value="text"))
        r = self.client.post(
            "/analyse-screen",
            data={"session_id": SESSION},
            files={"file": ("s.png", b"x" * (main.MAX_SCREEN_IMAGE_BYTES + 1), "image/png")},
        )
        self.assertEqual(r.status_code, 413)
        extract.assert_not_awaited()

    def test_question_too_long(self):
        ctx = self.patch("get_session_prompt_context", new=mock.AsyncMock(return_value=_ctx()))
        r = self.client.post("/ai-answer", json={"session_id": SESSION, "question": "q" * 4001})
        self.assertEqual(r.status_code, 422)
        ctx.assert_not_awaited()


class RateLimitTest(_ApiTest):
    EXPECTED = {
        "app.main.refresh_session": "30 per 1 minute",
        "app.main.payment_verify": "10 per 1 minute",
        "app.main.ai_answer": "60 per 1 minute",
        "app.main.analyse_screen": "20 per 1 minute",
        "app.main.deepgram_token": "30 per 1 minute",
        "app.main.launch_signup": "5 per 1 minute",
        "app.main.password_reset_request": "5 per 1 minute",
        "modules.auth_google.google_mobile": "10 per 1 minute",
    }

    def test_decorators_present(self):
        limits = {k: [str(l.limit) for l in v] for k, v in main.limiter._route_limits.items()}
        for route, limit in self.EXPECTED.items():
            with self.subTest(route):
                self.assertIn(limit, limits.get(route, []))

    def test_limit_enforced(self):
        self.patch("request_password_reset", new=mock.AsyncMock())
        codes = [self.client.post("/auth/password-reset/request", json={"email": "a@b.co"}).status_code
                 for _ in range(6)]
        self.assertEqual(codes, [200] * 5 + [429])

    def test_ai_answer_body_shape_unchanged(self):
        async def gen(question, system_prompt=None):
            yield question
        self.patch("select_stream", return_value=gen)
        self.patch("insert_interview_response", new=mock.AsyncMock())
        self.patch("get_session_prompt_context", new=mock.AsyncMock(return_value=_ctx()))
        r = self.client.post("/ai-answer", json={"session_id": SESSION, "query": "hello", "provider": "openai"})
        self.assertEqual(r.text, "hello")


class ClientIpTest(_ApiTest):
    def test_track_ignores_spoofed_forwarded_for(self):
        insert = self.patch("insert_site_event", new=mock.AsyncMock(return_value={"id": uuid4()}))
        self.patch("update_event_geo", new=mock.AsyncMock())
        r = self.client.post("/track", json={"event": "page_view"}, headers={"X-Forwarded-For": "9.9.9.9"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(insert.await_args.kwargs["ip"], "testclient")

    def test_track_uses_cloudflare_client_ip(self):
        insert = self.patch("insert_site_event", new=mock.AsyncMock(return_value={"id": uuid4()}))
        self.patch("update_event_geo", new=mock.AsyncMock())
        r = self.client.post(
            "/track",
            json={"event": "page_view"},
            headers={"CF-Connecting-IP": "203.0.113.7", "X-Forwarded-For": "9.9.9.9"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(insert.await_args.kwargs["ip"], "203.0.113.7")

    def test_rate_limit_key(self):
        from starlette.requests import Request as StarletteRequest
        from modules.rate_limit import client_ip

        def req(headers):
            return StarletteRequest({"type": "http", "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
                                     "client": ("10.0.0.1", 1234)})
        self.assertEqual(client_ip(req({"cf-connecting-ip": "203.0.113.7"})), "203.0.113.7")
        self.assertEqual(client_ip(req({"x-forwarded-for": "9.9.9.9"})), "10.0.0.1")


class LaunchSignupTest(_ApiTest):
    body = {"email": "a@b.co", "phone": "999", "name": "Asha", "profession": "Dev"}

    def test_no_echo(self):
        row = {"id": uuid4(), **self.body, "created_at": datetime.now(timezone.utc)}
        self.patch("insert_launch_signup", new=mock.AsyncMock(return_value=row))
        r = self.client.post("/launch_signup", json=self.body)
        self.assertEqual(set(r.json()), {"message", "id"})

    def test_error_hides_details(self):
        self.patch("insert_launch_signup", new=mock.AsyncMock(side_effect=RuntimeError("relation secret_tbl")))
        r = self.client.post("/launch_signup", json=self.body)
        self.assertEqual(r.status_code, 500)
        self.assertNotIn("secret_tbl", r.text)


class PasswordResetEndpointTest(_ApiTest):
    def test_request_always_ok(self):
        for effect in (None, RuntimeError("neon down")):
            with self.subTest(effect=effect):
                main.limiter.reset()
                req = self.patch("request_password_reset", new=mock.AsyncMock(side_effect=effect))
                r = self.client.post("/auth/password-reset/request", json={"email": " A@B.co "})
                self.assertEqual((r.status_code, r.json()), (200, {"ok": True}))
                req.assert_awaited_once_with("a@b.co")

    def _confirm(self, effect=None, user_id=USER, password="new-password"):
        self.patch("get_password_reset_user_id", new=mock.AsyncMock(return_value=user_id))
        self.confirm = self.patch("confirm_password_reset", new=mock.AsyncMock(side_effect=effect))
        self.revoke = self.patch("revoke_all_refresh_tokens", new=mock.AsyncMock())
        return self.client.post("/auth/password-reset/confirm",
                                json={"token": "tok", "new_password": password})

    def test_confirm_ok_revokes_sessions(self):
        r = self._confirm()
        self.assertEqual(r.json(), {"ok": True})
        self.revoke.assert_awaited_once_with(USER)

    def test_confirm_unknown_user_skips_revoke(self):
        r = self._confirm(user_id=None)
        self.assertEqual(r.status_code, 200)
        self.revoke.assert_not_awaited()

    def test_confirm_bad_token(self):
        r = self._confirm(effect=auth.PasswordResetError("400"))
        self.assertEqual((r.status_code, r.json()), (400, {"detail": "invalid_or_expired_token"}))
        self.revoke.assert_not_awaited()

    def test_confirm_neon_down(self):
        r = self._confirm(effect=ConnectionError("down"))
        self.assertEqual(r.status_code, 502)

    def test_confirm_short_password_not_logged(self):
        with self.assertLogs("app.main", level="WARNING") as logs:
            r = self._confirm(password="short-p")
        self.assertEqual(r.status_code, 422)
        self.confirm.assert_not_awaited()
        self.assertNotIn("short-p", "\n".join(logs.output))


class NeonAuthPasswordResetTest(unittest.TestCase):
    """The HTTP calls to Neon Auth (Better Auth) themselves."""

    def _post(self, status):
        return mock.patch.object(auth.requests, "post",
                                 return_value=mock.Mock(status_code=status, json=lambda: {"status": True},
                                                        raise_for_status=mock.Mock()))

    def test_request_shape(self):
        with self._post(200) as post:
            asyncio.run(auth.request_password_reset("a@b.co"))
        url = post.call_args.args[0]
        self.assertTrue(url.endswith("/request-password-reset"))
        self.assertEqual(post.call_args.kwargs["json"],
                         {"email": "a@b.co", "redirectTo": auth.settings.password_reset_redirect_url})
        self.assertIn("Origin", post.call_args.kwargs["headers"])

    def test_confirm_maps_4xx(self):
        with self._post(400), self.assertRaises(auth.PasswordResetError):
            asyncio.run(auth.confirm_password_reset("tok", "new-password"))

    def test_confirm_ok(self):
        with self._post(200) as post:
            asyncio.run(auth.confirm_password_reset("tok", "new-password"))
        self.assertTrue(post.call_args.args[0].endswith("/reset-password"))
        self.assertEqual(post.call_args.kwargs["json"], {"token": "tok", "newPassword": "new-password"})


class AccountDataTest(_ApiTest):
    def test_export(self):
        data = {"account": {"id": USER, "email": "a@b.co", "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)},
                "interview_profiles": [{"resume_text": "cv"}]}
        export = self.patch("export_user_data", new=mock.AsyncMock(return_value=data))
        r = self.client.get("/user/me/export")
        self.assertEqual(r.status_code, 200)
        self.assertIn('attachment; filename="cogniv-data.json"', r.headers["content-disposition"])
        self.assertEqual(r.json()["interview_profiles"][0]["resume_text"], "cv")
        export.assert_awaited_once_with(USER)

    def test_export_unknown_user(self):
        self.patch("export_user_data", new=mock.AsyncMock(return_value=None))
        self.assertEqual(self.client.get("/user/me/export").status_code, 404)

    def test_delete_requires_confirmation(self):
        delete = self.patch("delete_user_data", new=mock.AsyncMock())
        r = self.client.request("DELETE", "/user/me", json={"confirm": "yes"})
        self.assertEqual(r.status_code, 400)
        delete.assert_not_awaited()

    def test_delete(self):
        delete = self.patch("delete_user_data", new=mock.AsyncMock())
        r = self.client.request("DELETE", "/user/me", json={"confirm": "DELETE"})
        self.assertEqual(r.json(), {"ok": True})
        delete.assert_awaited_once_with(USER)

    def test_delete_failure_hides_details(self):
        self.patch("delete_user_data", new=mock.AsyncMock(side_effect=RuntimeError("fk violation x")))
        r = self.client.request("DELETE", "/user/me", json={"confirm": "DELETE"})
        self.assertEqual(r.status_code, 500)
        self.assertNotIn("fk violation", r.text)


class GoogleDesktopNonceTest(_ApiTest):
    def _state_from(self, location):
        state = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)["state"][0]
        return auth_google._signer.loads(state)

    def test_start_signs_nonce(self):
        r = self.client.get("/auth/google/start", params={"local_port": 5555, "nonce": NONCE},
                            follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self._state_from(r.headers["location"])["app_nonce"], NONCE)

    def test_start_without_nonce_still_works(self):
        r = self.client.get("/auth/google/start", params={"local_port": 5555}, follow_redirects=False)
        self.assertNotIn("app_nonce", self._state_from(r.headers["location"]))

    def test_start_rejects_bad_nonce(self):
        for bad in ("short", "x" * 129, "a" * 31 + "<"):
            r = self.client.get("/auth/google/start", params={"local_port": 5555, "nonce": bad},
                                follow_redirects=False)
            self.assertEqual(r.status_code, 422, bad)

    def _google(self, profile):
        class FakeClient:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, data=None):
                return mock.Mock(status_code=200, json=lambda: {"access_token": "g"})

            async def get(self, url, headers=None):
                return mock.Mock(status_code=200, json=lambda: profile)

        p = mock.patch.object(auth_google, "httpx", mock.Mock(AsyncClient=FakeClient))
        p.start()
        self.patches.append(p)
        for name, value in (("_find_or_create_user", {"id": USER, "email": "a@b.co"}),
                            ("issue_refresh_token", "rt")):
            p = mock.patch.object(auth_google, name, new=mock.AsyncMock(return_value=value))
            self.patches.append(p)
            setattr(self, name.strip("_"), p.start())

    def _callback(self, nonce=NONCE, **params):
        payload = {"nonce": "cogniv-oauth", "port": 5555}
        if nonce:
            payload["app_nonce"] = nonce
        params.setdefault("state", auth_google._signer.dumps(payload))
        r = self.client.get("/auth/google/callback", params=params, follow_redirects=False)
        loc = urllib.parse.urlparse(r.headers["location"])
        return loc, urllib.parse.parse_qs(loc.query)

    def test_callback_success_echoes_nonce(self):
        self._google({"email": "a@b.co", "email_verified": True})
        loc, q = self._callback(code="c")
        self.assertEqual(loc.netloc, "127.0.0.1:5555")
        self.assertEqual(q["nonce"], [NONCE])
        self.assertEqual(q["refresh_token"], ["rt"])

    def test_callback_error_echoes_nonce(self):
        _, q = self._callback(error="access_denied")
        self.assertEqual((q["error"], q["nonce"]), (["access_denied"], [NONCE]))

    def test_callback_without_nonce(self):
        self._google({"email": "a@b.co", "email_verified": True})
        _, q = self._callback(nonce=None, code="c")
        self.assertNotIn("nonce", q)
        self.assertIn("access_token", q)

    def test_callback_unverified_email_refused(self):
        self._google({"email": "a@b.co", "email_verified": False})
        _, q = self._callback(code="c")
        self.assertEqual(q["error"], ["email_not_verified"])
        self.find_or_create_user.assert_not_awaited()


class GoogleEmailVerifiedTest(unittest.TestCase):
    def _verify(self, claims):
        with mock.patch.object(auth_google.settings, "google_android_client_id", "aud"), \
             mock.patch.object(auth_google.google_id_token, "verify_oauth2_token", return_value=claims):
            return auth_google._verify_google_id_token("tok")

    def test_mobile_requires_verified_email(self):
        base = {"iss": "accounts.google.com", "aud": "aud", "email": "a@b.co"}
        for verified in (None, False, "false"):
            claims = dict(base)
            if verified is not None:
                claims["email_verified"] = verified
            with self.subTest(verified=verified), self.assertRaisesRegex(ValueError, "email_not_verified"):
                self._verify(claims)
        self.assertEqual(self._verify({**base, "email_verified": "true"})["email"], "a@b.co")


class _FakeConn:
    """asyncpg connection stand-in: fetchrow answers come from a queue."""

    def __init__(self, rows):
        self.rows = list(rows)
        self.executed = []

    async def fetchrow(self, query, *args):
        return self.rows.pop(0)

    async def execute(self, query, *args):
        self.executed.append(query)

    def transaction(self):
        return self

    def acquire(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class RefreshRotationTest(unittest.TestCase):
    def _rotate(self, rows):
        conn = _FakeConn(rows)
        with mock.patch.object(app_tokens, "get_pool", new=mock.AsyncMock(return_value=conn)), \
             mock.patch.object(app_tokens, "revoke_all_refresh_tokens", new=mock.AsyncMock()) as revoke_all:
            result = asyncio.run(app_tokens.rotate_refresh_token("raw"))
        return result, revoke_all, conn

    def test_valid_token_rotates(self):
        result, revoke_all, conn = self._rotate([{"user_id": USER}])
        self.assertEqual(result[0], USER)
        self.assertTrue(result[1])
        self.assertIn("INSERT INTO public.refresh_tokens", conn.executed[0])
        revoke_all.assert_not_awaited()

    def test_reuse_of_revoked_token_revokes_all(self):
        # The stale-token query only matches tokens revoked before the grace window.
        result, revoke_all, conn = self._rotate([None, {"user_id": USER}])
        self.assertIsNone(result)
        revoke_all.assert_awaited_once_with(USER)

    def test_unknown_expired_or_recent_retry(self):
        result, revoke_all, _ = self._rotate([None, None])
        self.assertIsNone(result)
        revoke_all.assert_not_awaited()


class AppSecretKeyTest(unittest.TestCase):
    def test_required(self):
        for empty in ("", "   "):
            with mock.patch.object(app_tokens.settings, "app_secret_key", empty), self.assertRaises(RuntimeError):
                app_tokens.require_app_secret_key()
        with mock.patch.object(app_tokens.settings, "app_secret_key", "k"):
            self.assertEqual(app_tokens.require_app_secret_key(), "k")


class StreamFailureTest(_ApiTest):
    def _wait_for(self, mock_fn):
        deadline = time.time() + 2
        while not mock_fn.await_count and time.time() < deadline:
            time.sleep(0.01)

    def test_answer_interrupted_marker(self):
        async def gen(question, system_prompt=None):
            yield "partial"
            raise RuntimeError("provider timeout")
        self.patch("select_stream", return_value=gen)
        save = self.patch("insert_interview_response", new=mock.AsyncMock())
        self.patch("get_session_prompt_context", new=mock.AsyncMock(return_value=_ctx()))
        r = self.client.post("/ai-answer", json={"session_id": SESSION, "question": "q?"})
        self.assertEqual(r.text, "partial" + main.STREAM_INTERRUPTED_MARKER)
        self._wait_for(save)
        save.assert_awaited_once()
        self.assertEqual(save.await_args.args[3], "partial")

    def test_screen_interrupted_marker(self):
        async def gen(prompt):
            raise RuntimeError("boom")
            yield  # pragma: no cover
        self.patch("select_stream", return_value=gen)
        save = self.patch("insert_interview_response", new=mock.AsyncMock())
        self.patch("get_session_prompt_context", new=mock.AsyncMock(return_value=_ctx()))
        self.patch("extract_text", new=mock.AsyncMock(return_value="text"))
        r = self.client.post("/analyse-screen", data={"session_id": SESSION},
                             files={"file": ("s.png", b"img", "image/png")})
        self.assertTrue(r.text.endswith(main.STREAM_INTERRUPTED_MARKER))
        save.assert_not_awaited()  # nothing streamed, nothing saved


class AnthropicStreamTest(unittest.TestCase):
    """Opus 5.5 request shape; thinking deltas never reach the client."""

    EVENTS = [
        {"type": "message_start", "message": {"id": "m", "type": "message", "role": "assistant",
                                              "model": "claude-opus-5-5", "content": [], "stop_reason": None,
                                              "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 0}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "hidden"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Hi"}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
         "usage": {"output_tokens": 2}},
        {"type": "message_stop"},
    ]

    def test_stream(self):
        seen = {}

        def handler(request):
            seen["beta"] = request.headers.get("anthropic-beta")
            seen["body"] = json.loads(request.content)
            sse = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in self.EVENTS)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=sse)

        client = anthropic.AsyncAnthropic(
            api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

        async def run():
            return [t async for t in llm.stream_anthropic("q", system_prompt="s")]

        with mock.patch.object(llm, "_anthropic_client", client):
            out = asyncio.run(run())
        self.assertEqual(out, ["Hi"])
        self.assertEqual(seen["beta"], "server-side-fallback-2026-07-01")
        body = seen["body"]
        self.assertEqual(body["model"], "claude-opus-5-5")
        self.assertEqual(body["fallbacks"], "default")
        self.assertEqual(body["output_config"], {"effort": "low"})
        self.assertNotIn("thinking", body)

    def test_clients_have_timeouts(self):
        self.assertEqual(llm._anthropic_client.timeout, llm.LLM_TIMEOUT_SECONDS)
        self.assertEqual(llm._openai_client.timeout, llm.LLM_TIMEOUT_SECONDS)
        self.assertLessEqual(llm._anthropic_client.max_retries, 2)


if __name__ == "__main__":
    unittest.main()
