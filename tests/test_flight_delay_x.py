import pytest
from gltest import *
from datetime import datetime, timezone


@pytest.fixture
def contract(direct_deploy):
    return direct_deploy("contracts/flight_delay_x.py")


def _base_fixture(lead_seconds=7200, duration_seconds=10800):
    """
    Helper providing synchronized flight_date, dep_ts, and arr_ts
    strictly matching each other in UTC.
    """
    now_dt = datetime.now(timezone.utc)
    now_ts = int(now_dt.timestamp())
    dep_ts = now_ts + lead_seconds
    flight_date = datetime.fromtimestamp(dep_ts, tz=timezone.utc).strftime("%Y-%m-%d")
    arr_ts = dep_ts + duration_seconds
    return flight_date, now_ts, dep_ts, arr_ts


def test_initial_state_and_stats(contract, direct_vm):
    assert contract.get_policy_count() == 0
    assert contract.get_pool_balance() == 0
    assert contract.get_reserved_payout() == 0
    assert contract.get_unreserved_liquidity() == 0
    assert contract.get_payout_multiplier() == 3
    assert contract.get_min_purchase_lead_time() == 3600
    assert contract.get_max_settlement_window() == 1209600

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

    flight_date, now_ts, dep_ts, arr_ts = _base_fixture()

    # Passenger buys policy
    direct_vm.sender = direct_bob
    direct_vm.value = 100
    pid = contract.buy_policy("VN210", flight_date, dep_ts, arr_ts)

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
    # URL is authoritative and contract-controlled, bound to flight code AND date
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

    flight_date, now_ts, dep_ts, arr_ts = _base_fixture()

    # Bob buys policy: premium 100 -> payout 300.
    # New pool balance = 600. Reserved = 300. Unreserved = 300.
    direct_vm.sender = direct_bob
    direct_vm.value = 100
    contract.buy_policy("VN210", flight_date, dep_ts, arr_ts)

    # Bob tries to buy policy requiring 600 payout (premium 200 * 3 = 600)
    # Available unreserved (300) + premium (200) = 500 < 600 payout!
    # Must fail because pool cannot fully reserve the new policy!
    direct_vm.value = 200
    with pytest.raises(Exception):
        contract.buy_policy("AA100", flight_date, dep_ts, arr_ts)


def test_buy_policy_timing_cutoff_buffer_reverts(contract, direct_vm, direct_alice, direct_bob):
    direct_vm.sender = direct_alice
    direct_vm.value = 5000
    contract.fund_insurance_pool()

    now_dt = datetime.now(timezone.utc)
    now_ts = int(now_dt.timestamp())
    # Buying too close to departure (only 1800s ahead < min_purchase_lead_time 3600s)
    cutoff_violation_dep = now_ts + 1800
    flight_date = datetime.fromtimestamp(cutoff_violation_dep, tz=timezone.utc).strftime("%Y-%m-%d")
    arr_ts = cutoff_violation_dep + 7200

    direct_vm.sender = direct_bob
    direct_vm.value = 100

    with pytest.raises(Exception):
        contract.buy_policy("VN210", flight_date, cutoff_violation_dep, arr_ts)


def test_buy_policy_invalid_inputs(contract, direct_vm, direct_alice, direct_bob):
    direct_vm.sender = direct_alice
    direct_vm.value = 5000
    contract.fund_insurance_pool()

    flight_date, now_ts, dep_ts, arr_ts = _base_fixture()
    direct_vm.sender = direct_bob

    # 1. Zero premium
    direct_vm.value = 0
    with pytest.raises(Exception):
        contract.buy_policy("VN210", flight_date, dep_ts, arr_ts)

    # 2. Flight code too short
    direct_vm.value = 100
    with pytest.raises(Exception):
        contract.buy_policy("VN", flight_date, dep_ts, arr_ts)

    # 3. Invalid date format
    with pytest.raises(Exception):
        contract.buy_policy("VN210", "25-09-2026", dep_ts, arr_ts)

    with pytest.raises(Exception):
        contract.buy_policy("VN210", "invalid-date", dep_ts, arr_ts)

    # 4. Chronological reversal: arrival before departure
    with pytest.raises(Exception):
        contract.buy_policy("VN210", flight_date, arr_ts, dep_ts)


def test_buy_policy_historical_flight_date_reverts(contract, direct_vm, direct_alice, direct_bob):
    """
    CRITICAL STEWARD TEST:
    Verifies that purchasing insurance for a past date is strictly rejected.
    """
    direct_vm.sender = direct_alice
    direct_vm.value = 5000
    contract.fund_insurance_pool()

    direct_vm.sender = direct_bob
    direct_vm.value = 100

    # Historical flight date in the past
    past_date = "2026-08-15"
    past_dep = 1786780800  # 2026-08-15 08:00:00 UTC
    past_arr = past_dep + 7200

    with pytest.raises(Exception, match="Cannot purchase policy for a historical flight date"):
        contract.buy_policy("VN210", past_date, past_dep, past_arr)


def test_buy_policy_future_timestamp_with_historical_date_reverts(contract, direct_vm, direct_alice, direct_bob):
    """
    EXACT STEWARD ATTACK VECTOR TEST:
    Verifies that supplying future timestamps for a historical flight date is strictly blocked!
    """
    direct_vm.sender = direct_alice
    direct_vm.value = 5000
    contract.fund_insurance_pool()

    direct_vm.sender = direct_bob
    direct_vm.value = 100

    # Historical flight date
    historical_flight_date = "2026-09-01"
    # Attacker tries to supply a future timestamp (e.g. year 2027) to bypass lead time
    future_fake_dep = 1800000000
    future_fake_arr = future_fake_dep + 7200

    # Must revert: either historical date rejection or departure_timestamp mismatch with flight_date
    with pytest.raises(Exception):
        contract.buy_policy("AA100", historical_flight_date, future_fake_dep, future_fake_arr)


def test_buy_policy_mismatched_dep_ts_and_flight_date_reverts(contract, direct_vm, direct_alice, direct_bob):
    """
    Verifies that departure_timestamp must fall strictly on flight_date UTC.
    """
    direct_vm.sender = direct_alice
    direct_vm.value = 5000
    contract.fund_insurance_pool()

    direct_vm.sender = direct_bob
    direct_vm.value = 100

    # Future date
    flight_date = "2026-11-20"
    # dep_ts scheduled for 2026-11-25 (5 days later!)
    mismatched_dep_ts = int(datetime(2026, 11, 25, 10, 0, 0, tzinfo=timezone.utc).timestamp())
    arr_ts = mismatched_dep_ts + 7200

    with pytest.raises(Exception, match="departure_timestamp does not fall on the selected flight_date"):
        contract.buy_policy("BA178", flight_date, mismatched_dep_ts, arr_ts)


def test_buy_policy_unrealistic_flight_duration_reverts(contract, direct_vm, direct_alice, direct_bob):
    """
    Verifies that arrival_timestamp must be realistic (min 30 min, max 24 hours).
    """
    direct_vm.sender = direct_alice
    direct_vm.value = 5000
    contract.fund_insurance_pool()

    flight_date, now_ts, dep_ts, _ = _base_fixture()
    direct_vm.sender = direct_bob
    direct_vm.value = 100

    # 1. Too short: only 15 minutes (< 30 min = 1800s)
    too_short_arr = dep_ts + 900
    with pytest.raises(Exception, match="at least 30 minutes"):
        contract.buy_policy("VN210", flight_date, dep_ts, too_short_arr)

    # 2. Too long: 30 hours (> 24 hours = 86400s)
    too_long_arr = dep_ts + 108000
    with pytest.raises(Exception, match="cannot exceed 24 hours"):
        contract.buy_policy("VN210", flight_date, dep_ts, too_long_arr)


def test_withdraw_exceeding_true_surplus_fails(contract, direct_vm, direct_owner, direct_bob):
    # Owner deposits 10,000 into pool
    direct_vm.sender = direct_owner
    direct_vm.value = 10000
    contract.fund_insurance_pool()

    flight_date, now_ts, dep_ts, arr_ts = _base_fixture()

    # Bob buys policy: premium 1000 -> payout 3000
    # Pool = 11,000; Reserved = 3,000; True surplus = 8,000
    direct_vm.sender = direct_bob
    direct_vm.value = 1000
    contract.buy_policy("BA178", flight_date, dep_ts, arr_ts)

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

    flight_date, now_ts, dep_ts, arr_ts = _base_fixture()

    direct_vm.sender = direct_bob
    direct_vm.value = 100
    pid = contract.buy_policy("AF084", flight_date, dep_ts, arr_ts)

    # Current time is now_ts < arr_ts. Attempting settlement before arrival must revert!
    with pytest.raises(Exception, match="cannot be settled before scheduled arrival time"):
        contract.settle_policy(pid)


def test_settle_after_max_window_fails(contract, direct_vm, direct_alice, direct_bob):
    """
    Verifies that claims cannot be settled after the 14-day claim window expires.
    """
    direct_vm.sender = direct_alice
    direct_vm.value = 5000
    contract.fund_insurance_pool()

    # Set up flight on 2026-10-01
    direct_vm.warp("2026-10-01T08:00:00Z")
    dep_ts = int(datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc).timestamp())
    arr_ts = dep_ts + 7200

    direct_vm.sender = direct_bob
    direct_vm.value = 100
    pid = contract.buy_policy("AF084", "2026-10-01", dep_ts, arr_ts)

    # Advance time 15 days past arrival (> 14 day max window = 1209600s)
    direct_vm.warp("2026-10-17T00:00:00Z")

    with pytest.raises(Exception, match="Policy settlement window has expired"):
        contract.settle_policy(pid)


def test_full_lifecycle_delayed_payout_and_reserves_reconciled(contract, direct_vm, direct_alice, direct_bob):
    # Underwriter funds pool
    direct_vm.sender = direct_alice
    direct_vm.value = 10000
    contract.fund_insurance_pool()

    # Time at purchase: 2026-10-05 08:00:00 UTC
    direct_vm.warp("2026-10-05T08:00:00Z")
    base_ts = int(datetime(2026, 10, 5, 8, 0, 0, tzinfo=timezone.utc).timestamp())
    dep_ts = base_ts + 7200   # 10:00:00 UTC (> 1 hr lead time)
    arr_ts = base_ts + 14400  # 12:00:00 UTC

    direct_vm.sender = direct_bob
    direct_vm.value = 200
    pid = contract.buy_policy("BA178", "2026-10-05", dep_ts, arr_ts)

    assert contract.get_reserved_payout() == 600
    assert contract.get_pool_balance() == 10200

    # Advance time past scheduled arrival: 2026-10-05 13:00:00 UTC
    direct_vm.warp("2026-10-05T13:00:00Z")

    # Mock web response for exact flight and date
    direct_vm.mock_web("BA178", {
        "status": 200,
        "body": "British Airways BA178 on 2026-10-05: Actual Departure delayed 225 minutes due to technical inspection."
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

    # Time at purchase: 2026-10-05 08:00:00 UTC
    direct_vm.warp("2026-10-05T08:00:00Z")
    base_ts = int(datetime(2026, 10, 5, 8, 0, 0, tzinfo=timezone.utc).timestamp())
    dep_ts = base_ts + 7200   # 10:00:00 UTC
    arr_ts = base_ts + 14400  # 12:00:00 UTC

    direct_vm.sender = direct_bob
    direct_vm.value = 100
    pid = contract.buy_policy("AF084", "2026-10-05", dep_ts, arr_ts)

    assert contract.get_reserved_payout() == 300
    assert contract.get_pool_balance() == 5100

    # Advance time past arrival: 2026-10-05 13:00:00 UTC
    direct_vm.warp("2026-10-05T13:00:00Z")

    direct_vm.mock_web("AF084", {
        "status": 200,
        "body": "Air France AF084 on 2026-10-05: Landed safely. Delay: 10 minutes."
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

    direct_vm.warp("2026-10-05T08:00:00Z")
    base_ts = int(datetime(2026, 10, 5, 8, 0, 0, tzinfo=timezone.utc).timestamp())
    dep_ts = base_ts + 7200
    arr_ts = base_ts + 14400

    direct_vm.sender = direct_bob
    direct_vm.value = 100
    pid = contract.buy_policy("JL001", "2026-10-05", dep_ts, arr_ts)

    # Advance time past arrival
    direct_vm.warp("2026-10-05T13:00:00Z")
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

    contract.set_max_settlement_window(604800)
    assert contract.get_max_settlement_window() == 604800

    # Non-owner fails
    direct_vm.sender = direct_bob
    with pytest.raises(Exception):
        contract.set_payout_multiplier(5)
    with pytest.raises(Exception):
        contract.set_min_purchase_lead_time(1800)
    with pytest.raises(Exception):
        contract.set_max_settlement_window(86400)


def test_settlement_reverts_when_underfunded_preserves_active_state(contract, direct_vm, direct_alice, direct_bob, direct_owner):
    # Fund pool with 10,000
    direct_vm.sender = direct_alice
    direct_vm.value = 10000
    contract.fund_insurance_pool()

    direct_vm.warp("2026-10-05T08:00:00Z")
    base_ts = int(datetime(2026, 10, 5, 8, 0, 0, tzinfo=timezone.utc).timestamp())
    dep_ts = base_ts + 7200
    arr_ts = base_ts + 14400

    direct_vm.sender = direct_bob
    direct_vm.value = 500  # Payout = 1500
    pid = contract.buy_policy("BA178", "2026-10-05", dep_ts, arr_ts)

    # Owner withdraws true surplus:
    # Total pool = 10,500; Reserved = 1500; True surplus = 9,000.
    direct_vm.sender = direct_owner
    contract.withdraw_pool(9000)
    assert contract.get_pool_balance() == 1500
    assert contract.get_reserved_payout() == 1500

    # Advance time past arrival
    direct_vm.warp("2026-10-05T13:00:00Z")
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

    direct_vm.warp("2026-10-05T08:00:00Z")
    base_ts = int(datetime(2026, 10, 5, 8, 0, 0, tzinfo=timezone.utc).timestamp())
    dep_ts = base_ts + 7200
    arr_ts = base_ts + 14400

    direct_vm.sender = direct_bob

    # Policy 1: Premium 100 -> Payout 300
    direct_vm.value = 100
    p1 = contract.buy_policy("VN210", "2026-10-05", dep_ts, arr_ts)

    # Policy 2: Premium 200 -> Payout 600
    direct_vm.value = 200
    p2 = contract.buy_policy("AA100", "2026-10-05", dep_ts, arr_ts)

    # Policy 3: Premium 300 -> Payout 900
    direct_vm.value = 300
    p3 = contract.buy_policy("BA178", "2026-10-05", dep_ts, arr_ts)

    # Total reserved: 300 + 600 + 900 = 1800
    assert contract.get_reserved_payout() == 1800
    assert contract.get_pool_balance() == 20600
    assert contract.get_unreserved_liquidity() == 18800

    # Advance time past arrival
    direct_vm.warp("2026-10-05T13:00:00Z")

    # Settle P1: ON_TIME -> Expires and releases 300
    direct_vm.clear_mocks()
    direct_vm.mock_web("VN210", {"status": 200, "body": "VN210 landed on time."})
    direct_vm.mock_llm(".*", '{"flight_status": "ON_TIME", "delay_minutes": 0, "confidence": 99, "reason": "On time."}')
    contract.settle_policy(p1)
    assert contract.get_reserved_payout() == 1500  # 1800 - 300
    assert contract.get_pool_balance() == 20600

    # Settle P2: CANCELLED -> Paid out 600
    direct_vm.clear_mocks()
    direct_vm.mock_web("AA100", {"status": 200, "body": "American Airlines AA100 on 2026-10-05: Flight CANCELLED due to mechanical issues."})
    direct_vm.mock_llm(".*", '{"flight_status": "CANCELLED", "delay_minutes": 0, "confidence": 99, "reason": "Cancelled."}')
    contract.settle_policy(p2)
    assert contract.get_reserved_payout() == 900   # 1500 - 600
    assert contract.get_pool_balance() == 20000   # 20600 - 600

    # Settle P3: DELAYED -> Paid out 900
    direct_vm.clear_mocks()
    direct_vm.mock_web("BA178", {"status": 200, "body": "British Airways BA178 on 2026-10-05: Flight arrival DELAYED 240 minutes due to weather."})
    direct_vm.mock_llm(".*", '{"flight_status": "DELAYED", "delay_minutes": 240, "confidence": 99, "reason": "Delayed 4h."}')
    contract.settle_policy(p3)

    # ALL policies settled: total_reserved_payout MUST be exactly 0!
    assert contract.get_reserved_payout() == 0
    assert contract.get_pool_balance() == 19100
    assert contract.get_unreserved_liquidity() == 19100
