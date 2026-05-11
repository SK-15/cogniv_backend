"""
Unit tests for purchase gate logic.
Run: cd /home/saurav/Projects/chatbot/backend && .venv/bin/python test_cases/test_billing_gate.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"

def check(label, cond, detail=""):
    status = PASS if cond else FAIL
    print(f"  [{status}] {label}" + (f" — {detail}" if detail else ""))
    return cond

def test_plan_tier_rank():
    print("\n1. Plan tier rank ordering")
    from modules.billing import PLAN_TIER_RANK
    check("free < starter", PLAN_TIER_RANK["free"] < PLAN_TIER_RANK["starter"])
    check("starter < pro",  PLAN_TIER_RANK["starter"] < PLAN_TIER_RANK["pro"])

def test_gate_blocks_same_plan_with_sessions():
    print("\n2. Gate blocks same-plan repurchase when sessions remain")
    from modules.billing import _gate_purchase
    from fastapi import HTTPException
    try:
        _gate_purchase(sessions_remaining=3, current_tier="starter", requested_plan="starter")
        check("should have raised", False)
    except HTTPException as e:
        check("raises 409", e.status_code == 409)

def test_gate_blocks_downgrade_with_sessions():
    print("\n3. Gate blocks downgrade when sessions remain")
    from modules.billing import _gate_purchase
    from fastapi import HTTPException
    try:
        _gate_purchase(sessions_remaining=5, current_tier="pro", requested_plan="starter")
        check("should have raised", False)
    except HTTPException as e:
        check("raises 409", e.status_code == 409)

def test_gate_allows_upgrade_with_sessions():
    print("\n4. Gate allows upgrade when sessions remain")
    from modules.billing import _gate_purchase
    try:
        _gate_purchase(sessions_remaining=3, current_tier="starter", requested_plan="pro")
        check("no exception raised (upgrade allowed)", True)
    except Exception as e:
        check("no exception raised", False, str(e))

def test_gate_allows_any_plan_when_no_sessions():
    print("\n5. Gate allows any plan when sessions = 0")
    from modules.billing import _gate_purchase
    for plan in ["starter", "pro"]:
        try:
            _gate_purchase(sessions_remaining=0, current_tier="pro", requested_plan=plan)
            check(f"{plan} allowed at 0 sessions", True)
        except Exception as e:
            check(f"{plan} allowed at 0 sessions", False, str(e))

def test_gate_allows_free_tier_to_buy_anything():
    print("\n6. Free tier user can buy any plan")
    from modules.billing import _gate_purchase
    for plan in ["starter", "pro"]:
        try:
            _gate_purchase(sessions_remaining=3, current_tier="free", requested_plan=plan)
            check(f"free → {plan} allowed", True)
        except Exception as e:
            check(f"free → {plan} allowed", False, str(e))

if __name__ == "__main__":
    test_plan_tier_rank()
    test_gate_blocks_same_plan_with_sessions()
    test_gate_blocks_downgrade_with_sessions()
    test_gate_allows_upgrade_with_sessions()
    test_gate_allows_any_plan_when_no_sessions()
    test_gate_allows_free_tier_to_buy_anything()
    print()
