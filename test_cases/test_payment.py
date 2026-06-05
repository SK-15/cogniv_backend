"""
Payment API smoke tests.
Run: .venv/bin/python test_cases/test_payment.py [BASE_URL] [TOKEN]

BASE_URL defaults to http://localhost:8000
TOKEN    defaults to None (skips authenticated endpoint tests)
"""
import sys
import os
import hmac
import hashlib
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
TOKEN = sys.argv[2] if len(sys.argv) > 2 else None

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
SKIP = "\033[93mSKIP\033[0m"


def check(label, cond, detail=""):
    status = PASS if cond else FAIL
    print(f"  [{status}] {label}" + (f" — {detail}" if detail else ""))
    return cond


# ── 1. Signature verification logic (pure, no server needed) ───────────────

def test_signature():
    print("\n1. Signature verification (local)")
    from modules.config import settings
    key_secret = settings.razorpay_key_secret
    payment_id = "pay_test123"
    subscription_id = "sub_test456"
    message = f"{payment_id}|{subscription_id}"
    sig = hmac.new(
        key_secret.encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()

    # correct signature
    from modules.billing import verify_payment_signature
    check("correct signature accepted", verify_payment_signature(payment_id, subscription_id, sig))

    # tampered signature
    check("tampered signature rejected", not verify_payment_signature(payment_id, subscription_id, sig + "x"))

    # wrong order (order-style, not subscription-style) is rejected
    wrong_message = f"{subscription_id}|{payment_id}"
    wrong_sig = hmac.new(key_secret.encode(), wrong_message.encode(), hashlib.sha256).hexdigest()
    check("order-style signature rejected", not verify_payment_signature(payment_id, subscription_id, wrong_sig))


# ── 2. Razorpay API connectivity ───────────────────────────────────────────

def test_razorpay_connection():
    print("\n2. Razorpay API connectivity")
    try:
        import razorpay
        from modules.config import settings
        if not settings.razorpay_key_id or not settings.razorpay_key_secret:
            check("credentials set", False, "KEY_ID or KEY_SECRET empty in .env")
            return
        rz = razorpay.Client(auth=(settings.razorpay_key_id, settings.razorpay_key_secret))
        # Lightweight call: fetch plans list (doesn't create anything)
        plans = rz.plan.all({"count": 1})
        check("Razorpay API reachable", True, f"plans endpoint returned {len(plans.get('items', []))} item(s)")

        if not settings.razorpay_pro_plan_id:
            print(f"  [{SKIP}] RAZORPAY_PRO_PLAN_ID not set — create a plan in dashboard and add to .env")
        else:
            plan = rz.plan.fetch(settings.razorpay_pro_plan_id)
            check("pro plan exists", plan.get("id") == settings.razorpay_pro_plan_id,
                  f"name={plan.get('item', {}).get('name')}, interval={plan.get('interval')}")
    except Exception as e:
        check("Razorpay API reachable", False, str(e))


# ── 3. HTTP endpoint tests (require running server + token) ────────────────

def test_endpoints():
    import urllib.request
    print("\n3. HTTP endpoint tests")

    def get(path, token=None):
        req = urllib.request.Request(BASE_URL + path)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
        except Exception as e:
            return None, str(e)

    def post(path, body=None, token=None):
        data = json.dumps(body or {}).encode()
        req = urllib.request.Request(BASE_URL + path, data=data,
                                     headers={"Content-Type": "application/json"})
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
        except Exception as e:
            return None, str(e)

    # Server reachable
    status, body = get("/docs")
    if not check("server reachable", status == 200, f"GET /docs → {status}"):
        print("  Start server: .venv/bin/uvicorn app.main:app --reload")
        return

    if not TOKEN:
        print(f"  [{SKIP}] No token — skipping authenticated tests")
        print("  Get token: curl -X POST http://localhost:8000/login -H 'Content-Type: application/json' -d '{\"email\":\"...\",\"password\":\"...\"}'")
        return

    # /subscription/status
    status, body = get("/subscription/status", TOKEN)
    check("/subscription/status returns 200", status == 200, str(body))
    if status == 200:
        check("plan_tier present",          "plan_tier" in body,          str(body))
        check("current_period_end present", "current_period_end" in body, str(body))
        check("free_session_limit = 3", body.get("free_session_limit") == 3 or body.get("plan_tier") == "pro")

    # /payment/create-order blocked when same plan and sessions remain
    status2, body2 = post("/payment/create-order", {"plan_id": "starter"}, token=TOKEN)
    if status2 == 409:
        check("/payment/create-order gate returns 409 for blocked purchase", True, body2.get("detail", ""))
    elif status2 == 200:
        check("/payment/create-order allowed (no active sessions or upgrading)", True)
    else:
        check("/payment/create-order returns 200 or 409", False, f"{status2} — {body2}")

    # /payment/verify with bad signature → must return 400
    status, body = post("/payment/verify", {
        "razorpay_payment_id": "pay_fake",
        "razorpay_subscription_id": "sub_fake",
        "razorpay_signature": "badsig",
    }, token=TOKEN)
    check("/payment/verify rejects bad signature", status == 400, str(body))

    # webhook with bad signature → must return 400
    status, body = post("/subscription/webhook", {"event": "subscription.charged"})
    check("/subscription/webhook rejects missing sig", status == 400, str(body))


# ── Run ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    test_signature()
    test_razorpay_connection()
    test_endpoints()
    print()
