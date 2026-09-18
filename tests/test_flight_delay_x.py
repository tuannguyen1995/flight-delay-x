import pytest
from gltest import *


@pytest.fixture
def contract(direct_deploy):
    return direct_deploy("contracts/flight_delay_x.py")


def test_initial_state(contract):
    assert contract.get_policy_count() == 0
    assert contract.get_pool_balance() == 0
    assert contract.get_payout_multiplier() == 3

    stats = contract.get_contract_stats()
    assert '"insurance_pool_balance": "0"' in stats
    assert '"policy_count": "0"' in stats
    assert '"payout_multiplier": "3"' in stats


def test_fund_insurance_pool_success(contract, direct_vm, direct_alice):
    direct_vm.sender = direct_alice
    direct_vm.value = 10000

    contract.fund_insurance_pool()

    assert contract.get_pool_balance() == 10000
    stats = contract.get_contract_stats()
    assert '"insurance_pool_balance": "10000"' in stats


def test_fund_insurance_pool_zero_fails(contract, direct_vm, direct_alice):
    direct_vm.sender = direct_alice
    direct_vm.value = 0

    with pytest.raises(Exception):
        contract.fund_insurance_pool()


def test_buy_policy_success(contract, direct_vm, direct_alice, direct_bob):
    # Underwriter deposits liquidity to cover payouts
    direct_vm.sender = direct_alice
    direct_vm.value = 5000
    contract.fund_insurance_pool()

    # Passenger buys policy
    direct_vm.sender = direct_bob
    direct_vm.value = 100
    pid = contract.buy_policy(
        "VN210",
        "https://flightaware.com/live/flight/HVN210"
    )

    assert str(pid) == "1"
    assert contract.get_policy_count() == 1
    # Pool now has 5000 + 100 = 5100
    assert contract.get_pool_balance() == 5100

    policy_json = contract.get_policy("1")
    assert '"policy_id": "1"' in policy_json
    assert '"flight_code": "VN210"' in policy_json
    assert '"premium_paid": "100"' in policy_json
    assert '"payout_amount": "300"' in policy_json
    assert '"status": "ACTIVE"' in policy_json
    assert '"flight_status": "PENDING"' in policy_json


def test_buy_policy_rejections(contract, direct_vm, direct_alice, direct_bob):
    direct_vm.sender = direct_alice
    direct_vm.value = 100
    contract.fund_insurance_pool()

    direct_vm.sender = direct_bob

    # 1. Zero premium
    direct_vm.value = 0
    with pytest.raises(Exception):
        contract.buy_policy("AA100", "https://flightaware.com/live/flight/AAL100")

    # 2. Flight code too short
    direct_vm.value = 50
    with pytest.raises(Exception):
        contract.buy_policy("AA", "https://flightaware.com/live/flight/AAL100")

    # 3. Invalid URL schema
    direct_vm.value = 50
    with pytest.raises(Exception):
        contract.buy_policy("AA100", "ftp://flightaware.com/live/flight/AAL100")

    # 4. Solvency check failure: premium 500 -> payout 1500 > pool balance (100 + 500 = 600)
    direct_vm.value = 500
    with pytest.raises(Exception):
        contract.buy_policy("AA100", "https://flightaware.com/live/flight/AAL100")


def test_settle_policy_delayed_flight_pays_out(contract, direct_vm, direct_alice, direct_bob):
    # Underwriter funds pool
    direct_vm.sender = direct_alice
    direct_vm.value = 10000
    contract.fund_insurance_pool()

    # Passenger buys policy (premium 200 -> payout 600)
    direct_vm.sender = direct_bob
    direct_vm.value = 200
    pid = contract.buy_policy("BA178", "https://flightaware.com/live/BA178")

    # Mock web scraping response
    direct_vm.mock_web("BA178", {
        "status": 200,
        "body": "British Airways BA178: Scheduled 08:00, Actual Departure 11:45. Status: Delayed 225 minutes due to technical inspection."
    })

    # Mock LLM consensus response
    direct_vm.mock_llm(".*", '{"flight_status": "DELAYED", "delay_minutes": 225, "confidence": 98, "reason": "Technical inspection delay of 225 minutes exceeds 180m threshold."}')

    contract.settle_policy(pid)

    policy_json = contract.get_policy(pid)
    assert '"status": "PAID_OUT"' in policy_json
    assert '"flight_status": "DELAYED"' in policy_json
    assert "225 minutes" in policy_json

    # Pool was 10000 + 200 = 10200. After 600 payout, remaining is 9600.
    assert contract.get_pool_balance() == 9600


def test_settle_policy_cancelled_flight_pays_out(contract, direct_vm, direct_alice, direct_bob):
    direct_vm.sender = direct_alice
    direct_vm.value = 5000
    contract.fund_insurance_pool()

    direct_vm.sender = direct_bob
    direct_vm.value = 150
    pid = contract.buy_policy("LH430", "https://flightradar24.com/data/flights/LH430")

    direct_vm.mock_web("LH430", {
        "status": 200,
        "body": "Lufthansa LH430 Frankfurt to Chicago. Notice: Flight CANCELLED due to severe storm at destination airport."
    })

    direct_vm.mock_llm(".*", '{"flight_status": "CANCELLED", "delay_minutes": 0, "confidence": 99, "reason": "Flight cancelled due to severe blizzard."}')

    contract.settle_policy(pid)

    policy_json = contract.get_policy(pid)
    assert '"status": "PAID_OUT"' in policy_json
    assert '"flight_status": "CANCELLED"' in policy_json

    # Initial 5000 + 150 = 5150; Payout = 450 (150 * 3); Remaining = 4700
    assert contract.get_pool_balance() == 4700


def test_settle_policy_on_time_expires_without_payout(contract, direct_vm, direct_alice, direct_bob):
    direct_vm.sender = direct_alice
    direct_vm.value = 5000
    contract.fund_insurance_pool()

    direct_vm.sender = direct_bob
    direct_vm.value = 100
    pid = contract.buy_policy("AF084", "https://flightaware.com/live/AF084")

    direct_vm.mock_web("AF084", {
        "status": 200,
        "body": "Air France AF084 Paris to San Francisco. Landed on time at 13:10 local time. Delay: 10 minutes."
    })

    direct_vm.mock_llm(".*", '{"flight_status": "ON_TIME", "delay_minutes": 10, "confidence": 95, "reason": "Minor 10-minute delay, landed safely."}')

    contract.settle_policy(pid)

    policy_json = contract.get_policy(pid)
    assert '"status": "EXPIRED"' in policy_json
    assert '"flight_status": "ON_TIME"' in policy_json

    # Pool retains premium (5000 + 100 = 5100)
    assert contract.get_pool_balance() == 5100


def test_settle_policy_offline_page_fallback(contract, direct_vm, direct_alice, direct_bob):
    direct_vm.sender = direct_alice
    direct_vm.value = 3000
    contract.fund_insurance_pool()

    direct_vm.sender = direct_bob
    direct_vm.value = 100
    pid = contract.buy_policy("JL001", "https://unresponsive-flight-tracker.com/JL001")

    # Mock web with blank/unresponsive page (< 20 characters)
    direct_vm.mock_web("JL001", {
        "status": 200,
        "body": "404"
    })

    contract.settle_policy(pid)

    policy_json = contract.get_policy(pid)
    assert '"status": "EXPIRED"' in policy_json
    assert '"flight_status": "ON_TIME"' in policy_json
    assert "offline" in policy_json


def test_settle_already_settled_policy_fails(contract, direct_vm, direct_alice, direct_bob):
    direct_vm.sender = direct_alice
    direct_vm.value = 2000
    contract.fund_insurance_pool()

    direct_vm.sender = direct_bob
    direct_vm.value = 50
    pid = contract.buy_policy("VN100", "https://flightaware.com/live/VN100")

    direct_vm.mock_web("VN100", {"status": 200, "body": "Flight VN100 arrived on schedule."})
    direct_vm.mock_llm(".*", '{"flight_status": "ON_TIME", "delay_minutes": 0, "confidence": 95, "reason": "On time."}')

    contract.settle_policy(pid)

    # Calling settle_policy again must raise Exception
    with pytest.raises(Exception):
        contract.settle_policy(pid)


def test_owner_payout_multiplier_and_withdrawal(contract, direct_vm, direct_owner, direct_bob):
    # Owner updates multiplier
    direct_vm.sender = direct_owner
    contract.set_payout_multiplier(4)
    assert contract.get_payout_multiplier() == 4

    # Non-owner fails to update multiplier
    direct_vm.sender = direct_bob
    with pytest.raises(Exception):
        contract.set_payout_multiplier(5)

    # Fund pool and withdraw
    direct_vm.sender = direct_owner
    direct_vm.value = 5000
    contract.fund_insurance_pool()
    assert contract.get_pool_balance() == 5000

    # Non-owner cannot withdraw
    direct_vm.sender = direct_bob
    with pytest.raises(Exception):
        contract.withdraw_pool(1000)

    # Owner withdraws 2000
    direct_vm.sender = direct_owner
    contract.withdraw_pool(2000)
    assert contract.get_pool_balance() == 3000
