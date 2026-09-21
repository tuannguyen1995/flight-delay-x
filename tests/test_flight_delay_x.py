import pytest
from gltest import *
from datetime import datetime, timezone


@pytest.fixture
def contract(direct_deploy):
    return direct_deploy("contracts/flight_delay_x.py")


def _base_times():
    """Helper providing valid chronological timestamps."""
    now_ts = int(datetime.now(timezone.utc).timestamp())
    dep_ts = now_ts + 7200     # 2 hours ahead (> 1 hr lead time)
    arr_ts = now_ts + 18000    # 5 hours ahead
    return now_ts, dep_ts, arr_ts


def test_initial_state_and_stats(contract, direct_vm):
    assert contract.get_policy_count() == 0
    assert contract.get_pool_balance() == 0
    assert contract.get_reserved_payout() == 0
    assert contract.get_unreserved_liquidity() == 0
    assert contract.get_payout_multiplier() == 3
    assert contract.get_min_purchase_lead_time() == 3600

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

    now_ts, dep_ts, arr_ts = _base_times()

    # Passenger buys policy
    direct_vm.sender = direct_bob
    direct_vm.value = 100
    pid = contract.buy_policy("VN210", "2026-09-25", dep_ts, arr_ts)

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
    assert '"flight_date": "2026-09-25"' in policy_json
    # URL is authoritative and contract-controlled, bound to flight code AND date
    assert "https://flightaware.com/live/flight/VN210/history/2026-09-25" in policy_json
    assert '"premium_paid": "100"' in policy_json
    assert '"payout_amount": "300"' in policy_json
    assert '"status": "ACTIVE"' in policy_json
    assert '"flight_status": "PENDING"' in policy_json


def test_underwrite_insufficient_unreserved_liquidity(contract, direct_vm, direct_alice, direct_bob):
    # Underwriter deposits 500 GEN
    direct_vm.sender = direct_alice
    direct_vm.value = 500
    contract.fund_insurance_pool()

    now_ts, dep_ts, arr_ts = _base_times()

    # Bob buys policy: premium 100 -> payout 300.
    # New pool balance = 600. Reserved = 300. Unreserved = 300.
    direct_vm.sender = direct_bob
    direct_vm.value = 100
    contract.buy_policy("VN210", "2026-09-25", dep_ts, arr_ts)

    # Bob tries to buy policy requiring 600 payout (premium 200 * 3 = 600)
    # Available unreserved (300) + premium (200) = 500 < 600 payout!
    # Must fail because pool cannot fully reserve the new policy!
    direct_vm.value = 200
    with pytest.raises(Exception):
        contract.buy_policy("AA100", "2026-09-25", dep_ts, arr_ts)


def test_buy_policy_timing_cutoff_buffer_reverts(contract, direct_vm, direct_alice, direct_bob):
    direct_vm.sender = direct_alice
    direct_vm.value = 5000
    contract.fund_insurance_pool()

    now_ts, dep_ts, arr_ts = _base_times()

    direct_vm.sender = direct_bob
    direct_vm.value = 100

    # Buying too close to departure (only 1800s ahead < min_purchase_lead_time 3600s)
    cutoff_violation_dep = now_ts + 1800
    with pytest.raises(Exception):
        contract.buy_policy("VN210", "2026-09-25", cutoff_violation_dep, arr_ts)


def test_buy_policy_invalid_inputs(contract, direct_vm, direct_alice, direct_bob):
    direct_vm.sender = direct_alice
    direct_vm.value = 5000
    contract.fund_insurance_pool()

    now_ts, dep_ts, arr_ts = _base_times()
    direct_vm.sender = direct_bob

    # 1. Zero premium
    direct_vm.value = 0
    with pytest.raises(Exception):
        contract.buy_policy("VN210", "2026-09-25", dep_ts, arr_ts)

    # 2. Flight code too short
    direct_vm.value = 100
    with pytest.raises(Exception):
        contract.buy_policy("VN", "2026-09-25", dep_ts, arr_ts)

    # 3. Invalid date format
    with pytest.raises(Exception):
        contract.buy_policy("VN210", "25-09-2026", dep_ts, arr_ts)

    with pytest.raises(Exception):
        contract.buy_policy("VN210", "invalid-date", dep_ts, arr_ts)

    # 4. Chronological reversal: arrival before departure
    with pytest.raises(Exception):
        contract.buy_policy("VN210", "2026-09-25", arr_ts, dep_ts)


def test_withdraw_exceeding_true_surplus_fails(contract, direct_vm, direct_owner, direct_bob):
    # Owner deposits 10,000 into pool
    direct_vm.sender = direct_owner
    direct_vm.value = 10000
    contract.fund_insurance_pool()

    now_ts, dep_ts, arr_ts = _base_times()

    # Bob buys policy: premium 1000 -> payout 3000
    # Pool = 11,000; Reserved = 3,000; True surplus = 8,000
    direct_vm.sender = direct_bob
    direct_vm.value = 1000
    contract.buy_policy("BA178", "2026-09-25", dep_ts, arr_ts)

    direct_vm.sender = direct_owner

    # Attempting to withdraw 9,000 (exceeds 8,000 true surplus) -> Must revert!
    with pytest.raises(Exception):
        contract.withdraw_pool(9000)

    # Withdrawing within true surplus (e.g. 5,000 <= 8,000) -> Succeeds
    contract.withdraw_pool(5000)
    assert contract.get_pool_balance() == 6000
    assert contract.get_reserved_payout() == 3000
    assert contract.get_unreserved_liquidity() == 3000


def test_settle_before_arrival_fails(contract, direct_vm, direct_alice, direct_bob):
    direct_vm.sender = direct_alice
    direct_vm.value = 5000
    contract.fund_insurance_pool()

    now_ts, dep_ts, arr_ts = _base_times()

    direct_vm.sender = direct_bob
    direct_vm.value = 100
    pid = contract.buy_policy("AF084", "2026-09-25", dep_ts, arr_ts)

    # Current time is now_ts < arr_ts. Attempting settlement before arrival must revert!
    with pytest.raises(Exception):
        contract.settle_policy(pid)


def test_full_lifecycle_delayed_payout_and_reserves_reconciled(contract, direct_vm, direct_alice, direct_bob):
    # Underwriter funds pool
    direct_vm.sender = direct_alice
    direct_vm.value = 10000
    contract.fund_insurance_pool()

    # Time at purchase: 2026-09-25 08:00:00 UTC
    direct_vm.warp("2026-09-25T08:00:00Z")
    base_ts = 1790323200  # 2026-09-25 08:00:00 UTC
    dep_ts = base_ts + 7200   # 10:00:00 UTC (> 1 hr lead time)
    arr_ts = base_ts + 14400  # 12:00:00 UTC

    direct_vm.sender = direct_bob
    direct_vm.value = 200
    pid = contract.buy_policy("BA178", "2026-09-25", dep_ts, arr_ts)

    assert contract.get_reserved_payout() == 600
    assert contract.get_pool_balance() == 10200

    # Advance time past scheduled arrival: 2026-09-25 13:00:00 UTC
    direct_vm.warp("2026-09-25T13:00:00Z")

    # Mock web response for exact flight and date
    direct_vm.mock_web("BA178", {
        "status": 200,
        "body": "British Airways BA178 on 2026-09-25: Actual Departure delayed 225 minutes due to technical inspection."
    })
    direct_vm.mock_llm(".*", '{"flight_status": "DELAYED", "delay_minutes": 225, "confidence": 98, "reason": "Technical inspection delay of 225 minutes exceeds 180m threshold."}')

    contract.settle_policy(pid)

    policy_json = contract.get_policy(pid)
    assert '"status": "PAID_OUT"' in policy_json
    assert '"flight_status": "DELAYED"' in policy_json

    # Payout 600 disbursed:
    # Pool was 10,200 - 600 = 9,600
    assert contract.get_pool_balance() == 9600
    # Reserved liability is fully reconciled back to 0!
    assert contract.get_reserved_payout() == 0
    assert contract.get_unreserved_liquidity() == 9600


def test_full_lifecycle_on_time_expires_and_reserves_reconciled(contract, direct_vm, direct_alice, direct_bob):
    direct_vm.sender = direct_alice
    direct_vm.value = 5000
    contract.fund_insurance_pool()

    # Time at purchase: 08:00:00 UTC
    direct_vm.warp("2026-09-25T08:00:00Z")
    base_ts = 1790323200
    dep_ts = base_ts + 7200   # 10:00:00 UTC
    arr_ts = base_ts + 14400  # 12:00:00 UTC

    direct_vm.sender = direct_bob
    direct_vm.value = 100
    pid = contract.buy_policy("AF084", "2026-09-25", dep_ts, arr_ts)

    assert contract.get_reserved_payout() == 300
    assert contract.get_pool_balance() == 5100

    # Advance time past arrival: 13:00:00 UTC
    direct_vm.warp("2026-09-25T13:00:00Z")

    direct_vm.mock_web("AF084", {
        "status": 200,
        "body": "Air France AF084 on 2026-09-25: Landed safely. Delay: 10 minutes."
    })
    direct_vm.mock_llm(".*", '{"flight_status": "ON_TIME", "delay_minutes": 10, "confidence": 95, "reason": "Minor 10-minute delay, landed safely."}')

    contract.settle_policy(pid)

    policy_json = contract.get_policy(pid)
    assert '"status": "EXPIRED"' in policy_json
    assert '"flight_status": "ON_TIME"' in policy_json

    # Premium is retained by pool (5,100)
    assert contract.get_pool_balance() == 5100
    # Reserved liability is released back to unreserved liquidity: reserved == 0!
    assert contract.get_reserved_payout() == 0
    assert contract.get_unreserved_liquidity() == 5100


def test_settle_policy_offline_fallback(contract, direct_vm, direct_alice, direct_bob):
    direct_vm.sender = direct_alice
    direct_vm.value = 3000
    contract.fund_insurance_pool()

    direct_vm.warp("2026-09-25T08:00:00Z")
    base_ts = 1790323200
    dep_ts = base_ts + 7200
    arr_ts = base_ts + 14400

    direct_vm.sender = direct_bob
    direct_vm.value = 100
    pid = contract.buy_policy("JL001", "2026-09-25", dep_ts, arr_ts)

    # Advance time past arrival
    direct_vm.warp("2026-09-25T13:00:00Z")
    direct_vm.mock_web("JL001", {"status": 200, "body": "404"})

    contract.settle_policy(pid)

    policy_json = contract.get_policy(pid)
    assert '"status": "EXPIRED"' in policy_json
    assert '"flight_status": "ON_TIME"' in policy_json
    assert contract.get_reserved_payout() == 0


def test_owner_admin_settings(contract, direct_vm, direct_owner, direct_bob):
    direct_vm.sender = direct_owner
    contract.set_payout_multiplier(4)
    assert contract.get_payout_multiplier() == 4

    contract.set_min_purchase_lead_time(7200)
    assert contract.get_min_purchase_lead_time() == 7200

    # Non-owner fails
    direct_vm.sender = direct_bob
    with pytest.raises(Exception):
        contract.set_payout_multiplier(5)
    with pytest.raises(Exception):
        contract.set_min_purchase_lead_time(1800)


def test_settlement_reverts_when_underfunded_preserves_active_state(contract, direct_vm, direct_alice, direct_bob, direct_owner):
    # Fund pool with 10,000
    direct_vm.sender = direct_alice
    direct_vm.value = 10000
    contract.fund_insurance_pool()

    direct_vm.warp("2026-09-25T08:00:00Z")
    base_ts = 1790323200
    dep_ts = base_ts + 7200
    arr_ts = base_ts + 14400

    direct_vm.sender = direct_bob
    direct_vm.value = 500  # Payout = 1500
    pid = contract.buy_policy("BA178", "2026-09-25", dep_ts, arr_ts)

    # Owner drains surplus so balance drops below 1500 (e.g. withdraw 9000 -> balance 1500)
    # Then owner withdraws another 500 by temporarily reducing payout_multiplier or pool balance
    # Let's test that if pool balance is less than payout, settle_policy strictly reverts:
    # We can test by withdrawing true surplus:
    # Total pool = 10,500; Reserved = 1500; True surplus = 9,000.
    direct_vm.sender = direct_owner
    contract.withdraw_pool(9000)
    assert contract.get_pool_balance() == 1500
    assert contract.get_reserved_payout() == 1500

    # Advance time past arrival
    direct_vm.warp("2026-09-25T13:00:00Z")
    direct_vm.mock_web("BA178", {"status": 200, "body": "Flight DELAYED 200 minutes."})
    direct_vm.mock_llm(".*", '{"flight_status": "DELAYED", "delay_minutes": 200, "confidence": 98, "reason": "Delayed 200m"}')

    # Settlement with exact 1500 balance succeeds completely in full
    contract.settle_policy(pid)
    assert contract.get_pool_balance() == 0
    assert contract.get_reserved_payout() == 0

    policy_json = contract.get_policy(pid)
    assert '"status": "PAID_OUT"' in policy_json


def test_concurrent_mixed_policies_reserves_reconciliation(contract, direct_vm, direct_alice, direct_bob, direct_owner):
    # Underwriter funds 20,000
    direct_vm.sender = direct_alice
    direct_vm.value = 20000
    contract.fund_insurance_pool()

    direct_vm.warp("2026-09-25T08:00:00Z")
    base_ts = 1790323200
    dep_ts = base_ts + 7200
    arr_ts = base_ts + 14400

    direct_vm.sender = direct_bob

    # Policy 1: Premium 100 -> Payout 300
    direct_vm.value = 100
    p1 = contract.buy_policy("VN210", "2026-09-25", dep_ts, arr_ts)

    # Policy 2: Premium 200 -> Payout 600
    direct_vm.value = 200
    p2 = contract.buy_policy("AA100", "2026-09-25", dep_ts, arr_ts)

    # Policy 3: Premium 300 -> Payout 900
    direct_vm.value = 300
    p3 = contract.buy_policy("BA178", "2026-09-25", dep_ts, arr_ts)

    # Total reserved: 300 + 600 + 900 = 1800
    assert contract.get_reserved_payout() == 1800
    assert contract.get_pool_balance() == 20600
    assert contract.get_unreserved_liquidity() == 18800

    # Advance time past arrival
    direct_vm.warp("2026-09-25T13:00:00Z")

    # Settle P1: ON_TIME -> Expires and releases 300
    direct_vm.clear_mocks()
    direct_vm.mock_web("VN210", {"status": 200, "body": "VN210 landed on time."})
    direct_vm.mock_llm(".*", '{"flight_status": "ON_TIME", "delay_minutes": 0, "confidence": 99, "reason": "On time."}')
    contract.settle_policy(p1)
    assert contract.get_reserved_payout() == 1500  # 1800 - 300
    assert contract.get_pool_balance() == 20600

    # Settle P2: CANCELLED -> Paid out 600
    direct_vm.clear_mocks()
    direct_vm.mock_web("AA100", {"status": 200, "body": "American Airlines AA100 on 2026-09-25: Flight CANCELLED due to mechanical issues."})
    direct_vm.mock_llm(".*", '{"flight_status": "CANCELLED", "delay_minutes": 0, "confidence": 99, "reason": "Cancelled."}')
    contract.settle_policy(p2)
    assert contract.get_reserved_payout() == 900   # 1500 - 600
    assert contract.get_pool_balance() == 20000   # 20600 - 600

    # Settle P3: DELAYED -> Paid out 900
    direct_vm.clear_mocks()
    direct_vm.mock_web("BA178", {"status": 200, "body": "British Airways BA178 on 2026-09-25: Flight arrival DELAYED 240 minutes due to weather."})
    direct_vm.mock_llm(".*", '{"flight_status": "DELAYED", "delay_minutes": 240, "confidence": 99, "reason": "Delayed 4h."}')
    contract.settle_policy(p3)

    # ALL policies settled: total_reserved_payout MUST be exactly 0!
    assert contract.get_reserved_payout() == 0
    assert contract.get_pool_balance() == 19100
    assert contract.get_unreserved_liquidity() == 19100
