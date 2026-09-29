import pytest
from gltest import *
from datetime import datetime, timezone, timedelta


@pytest.fixture
def contract(direct_deploy):
    return direct_deploy("contracts/flight_delay_x.py")


def _future_date(days_ahead=2):
    """Helper returning a valid future date in YYYY-MM-DD format."""
    now = datetime.now(timezone.utc)
    target = now + timedelta(days=days_ahead)
    return target.strftime("%Y-%m-%d")


def test_initial_state_and_stats(contract, direct_vm):
    assert contract.get_policy_count() == 0
    assert contract.get_pool_balance() == 0
    assert contract.get_reserved_payout() == 0
    assert contract.get_unreserved_liquidity() == 0
    assert contract.get_payout_multiplier() == 3

    stats = contract.get_contract_stats()
    assert '"insurance_pool_balance": "0"' in stats
    assert '"total_reserved_payout": "0"' in stats
    assert '"unreserved_liquidity": "0"' in stats
    assert '"policy_count": "0"' in stats


def test_fund_insurance_pool_success(contract, direct_vm, direct_alice):
    direct_vm.sender = direct_alice
    direct_vm.value = 10000

    contract.fund_insurance_pool()

    assert contract.get_pool_balance() == 10000
    assert contract.get_unreserved_liquidity() == 10000
    assert contract.get_reserved_payout() == 0

    stats = contract.get_contract_stats()
    assert '"insurance_pool_balance": "10000"' in stats
    assert '"unreserved_liquidity": "10000"' in stats


def test_fund_insurance_pool_zero_fails(contract, direct_vm, direct_alice):
    direct_vm.sender = direct_alice
    direct_vm.value = 0

    with pytest.raises(Exception):
        contract.fund_insurance_pool()


def test_buy_policy_authoritative_url_and_reservation(contract, direct_vm, direct_alice, direct_bob):
    # Underwriter deposits liquidity
    direct_vm.sender = direct_alice
    direct_vm.value = 5000
    contract.fund_insurance_pool()

    flight_date = _future_date(2)

    # Passenger buys policy
    direct_vm.sender = direct_bob
    direct_vm.value = 100
    pid = contract.buy_policy("VN210", flight_date)

    assert str(pid) == "1"
    assert contract.get_policy_count() == 1
    # Pool now has 5000 + 100 = 5100
    assert contract.get_pool_balance() == 5100
    # Full payout (100 * 3 = 300) is locked in total_reserved_payout
    assert contract.get_reserved_payout() == 300
    # Unreserved liquidity = 5100 - 300 = 4800
    assert contract.get_unreserved_liquidity() == 4800

    policy_json = contract.get_policy("1")
    assert '"policy_id": "1"' in policy_json
    assert '"flight_code": "VN210"' in policy_json
    assert f'"flight_date": "{flight_date}"' in policy_json
    assert f"https://flightaware.com/live/flight/VN210/history/{flight_date}" in policy_json
    assert '"premium_paid": "100"' in policy_json
    assert '"payout_amount": "300"' in policy_json
    assert '"status": "ACTIVE"' in policy_json
    assert '"flight_status": "PENDING"' in policy_json


def test_underwrite_insufficient_unreserved_liquidity(contract, direct_vm, direct_alice, direct_bob):
    # Underwriter deposits 500 GEN
    direct_vm.sender = direct_alice
    direct_vm.value = 500
    contract.fund_insurance_pool()

    flight_date = _future_date(2)

    # Bob buys policy: premium 100 -> payout 300.
    # New pool balance = 600. Reserved = 300. Unreserved = 300.
    direct_vm.sender = direct_bob
    direct_vm.value = 100
    contract.buy_policy("VN210", flight_date)

    # Bob tries to buy policy requiring 600 payout (premium 200 * 3 = 600)
    # Available unreserved (300) + premium (200) = 500 < 600 payout!
    direct_vm.value = 200
    with pytest.raises(Exception):
        contract.buy_policy("AA100", flight_date)


def test_buy_policy_invalid_inputs(contract, direct_vm, direct_alice, direct_bob):
    direct_vm.sender = direct_alice
    direct_vm.value = 5000
    contract.fund_insurance_pool()

    flight_date = _future_date(2)
    direct_vm.sender = direct_bob

    # 1. Zero premium
    direct_vm.value = 0
    with pytest.raises(Exception):
        contract.buy_policy("VN210", flight_date)

    # 2. Flight code too short
    direct_vm.value = 100
    with pytest.raises(Exception):
        contract.buy_policy("VN", flight_date)

    # 3. Invalid date format
    with pytest.raises(Exception):
        contract.buy_policy("VN210", "25-09-2026")

    with pytest.raises(Exception):
        contract.buy_policy("VN210", "invalid-date")


def test_buy_policy_historical_flight_date_reverts(contract, direct_vm, direct_alice, direct_bob):
    direct_vm.sender = direct_alice
    direct_vm.value = 5000
    contract.fund_insurance_pool()

    direct_vm.sender = direct_bob
    direct_vm.value = 100

    past_date = "2026-08-15"
    with pytest.raises(Exception, match="Cannot purchase policy for a historical flight date"):
        contract.buy_policy("VN210", past_date)


def test_settle_policy_not_concluded_reverts(contract, direct_vm, direct_alice, direct_bob):
    """
    If settlement is attempted while the flight is still scheduled in the future
    or in progress, AI reports NOT_CONCLUDED and transaction reverts.
    Policy remains ACTIVE for future settlement.
    """
    direct_vm.sender = direct_alice
    direct_vm.value = 5000
    contract.fund_insurance_pool()

    flight_date = _future_date(3)
    direct_vm.sender = direct_bob
    direct_vm.value = 100
    pid = contract.buy_policy("AF084", flight_date)

    direct_vm.mock_web("AF084", {
        "status": 200,
        "body": f"Air France AF084 on {flight_date}: Scheduled departure in 48 hours."
    })
    direct_vm.mock_llm(".*", '{"flight_status": "NOT_CONCLUDED", "is_historical_exploit": false, "confidence": 99, "reason": "Flight has not taken off yet."}')

    with pytest.raises(Exception, match="Flight has not concluded yet"):
        contract.settle_policy(pid)

    # Policy remains ACTIVE
    policy_json = contract.get_policy(pid)
    assert '"status": "ACTIVE"' in policy_json


def test_settle_policy_historical_exploit_expires_no_payout(contract, direct_vm, direct_alice, direct_bob):
    """
    CRITICAL STEWARD TEST:
    AI detects that the flight had already departed or concluded before purchase time.
    Flags as INVALID_CLAIM, releases reserve liability, and awards NO payout!
    """
    direct_vm.sender = direct_alice
    direct_vm.value = 5000
    contract.fund_insurance_pool()

    flight_date = _future_date(1)
    direct_vm.sender = direct_bob
    direct_vm.value = 100
    pid = contract.buy_policy("AA100", flight_date)

    assert contract.get_reserved_payout() == 300

    # Mock web response showing flight already landed/departed hours before purchase
    direct_vm.mock_web("AA100", {
        "status": 200,
        "body": f"American Airlines AA100 on {flight_date}: Flight completed 6 hours ago."
    })
    direct_vm.mock_llm(".*", '{"flight_status": "INVALID_CLAIM", "is_historical_exploit": true, "confidence": 100, "reason": "Flight concluded prior to policy purchase timestamp."}')

    contract.settle_policy(pid)

    policy_json = contract.get_policy(pid)
    assert '"status": "EXPIRED"' in policy_json
    assert '"flight_status": "INVALID_CLAIM"' in policy_json

    # Liability released back to pool, no payout transferred
    assert contract.get_reserved_payout() == 0
    assert contract.get_pool_balance() == 5100


def test_full_lifecycle_delayed_payout_and_reserves_reconciled(contract, direct_vm, direct_alice, direct_bob):
    direct_vm.sender = direct_alice
    direct_vm.value = 10000
    contract.fund_insurance_pool()

    flight_date = _future_date(2)
    direct_vm.sender = direct_bob
    direct_vm.value = 200
    pid = contract.buy_policy("BA178", flight_date)

    assert contract.get_reserved_payout() == 600
    assert contract.get_pool_balance() == 10200

    direct_vm.mock_web("BA178", {
        "status": 200,
        "body": f"British Airways BA178 on {flight_date}: Landed. Delayed 225 minutes."
    })
    direct_vm.mock_llm(".*", '{"flight_status": "DELAYED", "is_historical_exploit": false, "confidence": 98, "reason": "Delay of 225 minutes exceeds 180m."}')

    contract.settle_policy(pid)

    policy_json = contract.get_policy(pid)
    assert '"status": "PAID_OUT"' in policy_json
    assert '"flight_status": "DELAYED"' in policy_json

    # Payout 600 disbursed:
    assert contract.get_pool_balance() == 9600
    assert contract.get_reserved_payout() == 0
    assert contract.get_unreserved_liquidity() == 9600


def test_full_lifecycle_cancelled_payout(contract, direct_vm, direct_alice, direct_bob):
    direct_vm.sender = direct_alice
    direct_vm.value = 5000
    contract.fund_insurance_pool()

    flight_date = _future_date(2)
    direct_vm.sender = direct_bob
    direct_vm.value = 100
    pid = contract.buy_policy("JL001", flight_date)

    direct_vm.mock_web("JL001", {
        "status": 200,
        "body": f"Japan Airlines JL001 on {flight_date}: Flight CANCELLED due to mechanical check."
    })
    direct_vm.mock_llm(".*", '{"flight_status": "CANCELLED", "is_historical_exploit": false, "confidence": 99, "reason": "Flight cancelled."}')

    contract.settle_policy(pid)

    policy_json = contract.get_policy(pid)
    assert '"status": "PAID_OUT"' in policy_json
    assert '"flight_status": "CANCELLED"' in policy_json
    assert contract.get_pool_balance() == 4800
    assert contract.get_reserved_payout() == 0


def test_full_lifecycle_on_time_expires_and_reserves_reconciled(contract, direct_vm, direct_alice, direct_bob):
    direct_vm.sender = direct_alice
    direct_vm.value = 5000
    contract.fund_insurance_pool()

    flight_date = _future_date(2)
    direct_vm.sender = direct_bob
    direct_vm.value = 100
    pid = contract.buy_policy("AF084", flight_date)

    assert contract.get_reserved_payout() == 300
    assert contract.get_pool_balance() == 5100

    direct_vm.mock_web("AF084", {
        "status": 200,
        "body": f"Air France AF084 on {flight_date}: Landed on schedule. Delay: 5 minutes."
    })
    direct_vm.mock_llm(".*", '{"flight_status": "ON_TIME", "is_historical_exploit": false, "confidence": 95, "reason": "Landed on schedule."}')

    contract.settle_policy(pid)

    policy_json = contract.get_policy(pid)
    assert '"status": "EXPIRED"' in policy_json
    assert '"flight_status": "ON_TIME"' in policy_json
    assert contract.get_pool_balance() == 5100
    assert contract.get_reserved_payout() == 0
    assert contract.get_unreserved_liquidity() == 5100


def test_settle_policy_offline_fallback(contract, direct_vm, direct_alice, direct_bob):
    direct_vm.sender = direct_alice
    direct_vm.value = 3000
    contract.fund_insurance_pool()

    flight_date = _future_date(2)
    direct_vm.sender = direct_bob
    direct_vm.value = 100
    pid = contract.buy_policy("JL001", flight_date)

    direct_vm.mock_web("JL001", {"status": 200, "body": "404"})

    contract.settle_policy(pid)

    policy_json = contract.get_policy(pid)
    assert '"status": "EXPIRED"' in policy_json
    assert '"flight_status": "ON_TIME"' in policy_json
    assert contract.get_reserved_payout() == 0


def test_withdraw_exceeding_true_surplus_fails(contract, direct_vm, direct_owner, direct_bob):
    direct_vm.sender = direct_owner
    direct_vm.value = 10000
    contract.fund_insurance_pool()

    flight_date = _future_date(2)
    direct_vm.sender = direct_bob
    direct_vm.value = 1000
    contract.buy_policy("BA178", flight_date)

    direct_vm.sender = direct_owner

    # Attempting to withdraw 9,000 (exceeds 8,000 true surplus) -> Must revert!
    with pytest.raises(Exception):
        contract.withdraw_pool(9000)

    # Withdrawing within true surplus (5,000 <= 8,000) -> Succeeds
    contract.withdraw_pool(5000)
    assert contract.get_pool_balance() == 6000
    assert contract.get_reserved_payout() == 3000
    assert contract.get_unreserved_liquidity() == 3000


def test_owner_admin_settings(contract, direct_vm, direct_owner, direct_bob):
    direct_vm.sender = direct_owner
    contract.set_payout_multiplier(4)
    assert contract.get_payout_multiplier() == 4

    direct_vm.sender = direct_bob
    with pytest.raises(Exception):
        contract.set_payout_multiplier(5)


def test_settlement_reverts_when_underfunded_preserves_active_state(contract, direct_vm, direct_alice, direct_bob, direct_owner):
    direct_vm.sender = direct_alice
    direct_vm.value = 10000
    contract.fund_insurance_pool()

    flight_date = _future_date(2)
    direct_vm.sender = direct_bob
    direct_vm.value = 500  # Payout = 1500
    pid = contract.buy_policy("BA178", flight_date)

    direct_vm.sender = direct_owner
    contract.withdraw_pool(9000)
    assert contract.get_pool_balance() == 1500
    assert contract.get_reserved_payout() == 1500

    direct_vm.mock_web("BA178", {"status": 200, "body": f"Flight {flight_date} DELAYED 200 minutes."})
    direct_vm.mock_llm(".*", '{"flight_status": "DELAYED", "is_historical_exploit": false, "confidence": 98, "reason": "Delayed 200m"}')

    contract.settle_policy(pid)
    assert contract.get_pool_balance() == 0
    assert contract.get_reserved_payout() == 0

    policy_json = contract.get_policy(pid)
    assert '"status": "PAID_OUT"' in policy_json


def test_concurrent_mixed_policies_reserves_reconciliation(contract, direct_vm, direct_alice, direct_bob):
    direct_vm.sender = direct_alice
    direct_vm.value = 20000
    contract.fund_insurance_pool()

    flight_date = _future_date(2)
    direct_vm.sender = direct_bob

    # Policy 1: Premium 100 -> Payout 300
    direct_vm.value = 100
    p1 = contract.buy_policy("VN210", flight_date)

    # Policy 2: Premium 200 -> Payout 600
    direct_vm.value = 200
    p2 = contract.buy_policy("AA100", flight_date)

    # Policy 3: Premium 300 -> Payout 900
    direct_vm.value = 300
    p3 = contract.buy_policy("BA178", flight_date)

    assert contract.get_reserved_payout() == 1800
    assert contract.get_pool_balance() == 20600
    assert contract.get_unreserved_liquidity() == 18800

    # Settle P1: ON_TIME -> Expires and releases 300
    direct_vm.clear_mocks()
    direct_vm.mock_web("VN210", {"status": 200, "body": "VN210 landed on time."})
    direct_vm.mock_llm(".*", '{"flight_status": "ON_TIME", "is_historical_exploit": false, "confidence": 99, "reason": "On time."}')
    contract.settle_policy(p1)
    assert contract.get_reserved_payout() == 1500
    assert contract.get_pool_balance() == 20600

    # Settle P2: CANCELLED -> Paid out 600
    direct_vm.clear_mocks()
    direct_vm.mock_web("AA100", {"status": 200, "body": "American Airlines AA100: Flight CANCELLED."})
    direct_vm.mock_llm(".*", '{"flight_status": "CANCELLED", "is_historical_exploit": false, "confidence": 99, "reason": "Cancelled."}')
    contract.settle_policy(p2)
    assert contract.get_reserved_payout() == 900
    assert contract.get_pool_balance() == 20000

    # Settle P3: DELAYED -> Paid out 900
    direct_vm.clear_mocks()
    direct_vm.mock_web("BA178", {"status": 200, "body": "British Airways BA178: Flight arrival DELAYED 240 minutes."})
    direct_vm.mock_llm(".*", '{"flight_status": "DELAYED", "is_historical_exploit": false, "confidence": 99, "reason": "Delayed 4h."}')
    contract.settle_policy(p3)

    assert contract.get_reserved_payout() == 0
    assert contract.get_pool_balance() == 19100
    assert contract.get_unreserved_liquidity() == 19100
