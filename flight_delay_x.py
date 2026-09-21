# v0.2.16
# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
from genlayer import *
from dataclasses import dataclass
from datetime import datetime, timezone
import json

try:
    _UserError = UserError
except NameError:
    class _UserError(Exception):
        pass


def _addr_str(addr: Address) -> str:
    """Safely format an Address instance into a lowercase hex string."""
    try:
        return addr.as_hex.lower()
    except Exception:
        return str(addr).lower()


def _get_sender() -> Address:
    """Safely obtain transaction sender across GenVM runtime versions."""
    try:
        return gl.message.sender
    except Exception:
        try:
            return gl.message.sender_address
        except Exception:
            raise _UserError("Cannot resolve sender address.")


def _validate_date_format(d_str: str) -> None:
    """Validate that date string matches YYYY-MM-DD format."""
    if len(d_str) != 10 or d_str[4] != "-" or d_str[7] != "-":
        raise _UserError("flight_date must be in YYYY-MM-DD format.")
    try:
        datetime.strptime(d_str, "%Y-%m-%d")
    except Exception:
        raise _UserError("Invalid calendar date in flight_date.")


@allow_storage
@dataclass
class InsurancePolicy:
    """Storage struct representing a flight parametric insurance policy."""
    policy_id: str
    passenger: Address
    flight_code: str              # e.g., "VN210", "AA100"
    flight_date: str              # e.g., "2026-09-25" (YYYY-MM-DD)
    departure_timestamp: bigint   # Scheduled departure Unix timestamp (seconds)
    arrival_timestamp: bigint     # Scheduled arrival Unix timestamp (seconds)
    tracking_url: str             # Contract-generated authoritative URL
    premium_paid: bigint          # Price paid for the policy
    payout_amount: bigint         # Guaranteed payout if delayed/cancelled
    status: str                   # "ACTIVE", "PAID_OUT", "EXPIRED"
    flight_status: str            # "PENDING", "DELAYED", "CANCELLED", "ON_TIME"
    reason: str                   # AI Consensus explanation
    created_at: bigint
    resolved_at: bigint


class Contract(gl.Contract):
    """
    FlightDelayX: Parametric Flight Delay & Cancellation Insurance Protocol
    Track: Prediction Markets & Real-World Settlement
    """
    owner: Address
    insurance_pool_balance: bigint
    total_reserved_payout: bigint   # Total liability locked for all active policies
    policy_count: bigint
    payout_multiplier: bigint       # e.g., 3x premium as default payout
    min_purchase_lead_time: bigint  # Cutoff buffer in seconds before departure (default 3600s)
    policies: TreeMap[str, InsurancePolicy]

    def __init__(self):
        # GenVM automatically initializes TreeMap and DynArray fields.
        # DO NOT reassign self.policies = TreeMap() here to prevent AssertionError.
        self.owner = _get_sender()
        self.insurance_pool_balance = bigint(0)
        self.total_reserved_payout = bigint(0)
        self.policy_count = bigint(0)
        self.payout_multiplier = bigint(3)          # 3x payout multiplier
        self.min_purchase_lead_time = bigint(3600)  # 1 hour cutoff buffer

    def _get_current_timestamp(self) -> bigint:
        """Derive trusted timestamp from transaction execution context or datetime."""
        try:
            if hasattr(gl, "message_raw") and isinstance(gl.message_raw, dict) and "datetime" in gl.message_raw:
                dt_str = str(gl.message_raw["datetime"])
                if dt_str:
                    dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                    return bigint(int(dt.timestamp()))
        except Exception:
            pass
        try:
            if hasattr(gl.message, "datetime"):
                dt_val = gl.message.datetime
                if isinstance(dt_val, str):
                    dt = datetime.fromisoformat(dt_val.replace("Z", "+00:00"))
                    return bigint(int(dt.timestamp()))
                elif hasattr(dt_val, "timestamp"):
                    return bigint(int(dt_val.timestamp()))
        except Exception:
            pass
        return bigint(int(datetime.now(timezone.utc).timestamp()))

    def _parse_llm_json(self, text: str) -> dict:
        """Safely parse LLM responses, stripping markdown wrappers if present."""
        try:
            cleaned = str(text).strip()
            if cleaned.startswith("```json"):
                cleaned = cleaned[7:]
            elif cleaned.startswith("```"):
                cleaned = cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            return json.loads(cleaned.strip())
        except Exception as e:
            return {
                "flight_status": "ON_TIME",
                "delay_minutes": 0,
                "confidence": 0,
                "reason": f"Failed to parse LLM JSON: {str(e)[:100]}"
            }

    @gl.public.write.payable
    def fund_insurance_pool(self) -> None:
        """Underwriters deposit GEN liquidity to cover future flight claim payouts."""
        deposit = bigint(gl.message.value)
        if deposit <= bigint(0):
            raise _UserError("Pool deposit must be greater than 0 GEN.")
        self.insurance_pool_balance += deposit

    @gl.public.write.payable
    def buy_policy(
        self,
        flight_code: str,
        flight_date: str,
        departure_timestamp: int,
        arrival_timestamp: int
    ) -> str:
        """
        Passengers buy insurance for a specific flight on a specific date.
        Enforces purchase before departure cutoff and underwrites strictly
        against unreserved liquidity.
        """
        premium = bigint(gl.message.value)
        if premium <= bigint(0):
            raise _UserError("Premium paid must be greater than 0 GEN.")

        clean_flight = flight_code.strip().upper()
        clean_date = flight_date.strip()

        if len(clean_flight) < 3:
            raise _UserError("Invalid flight_code format (must be at least 3 characters).")
        _validate_date_format(clean_date)

        dep_ts = bigint(departure_timestamp)
        arr_ts = bigint(arrival_timestamp)

        if dep_ts >= arr_ts:
            raise _UserError("departure_timestamp must be strictly earlier than arrival_timestamp.")

        # Enforce purchase before outcome is known (lead time buffer)
        current_ts = self._get_current_timestamp()
        if current_ts + self.min_purchase_lead_time > dep_ts:
            raise _UserError(
                "Policy purchase must be made before flight departure lead time buffer."
            )

        # Authoritative contract-controlled source URL bound to flight code AND date
        clean_url = f"https://flightaware.com/live/flight/{clean_flight}/history/{clean_date}"

        payout = premium * self.payout_multiplier

        # Verify pool solvency against unreserved liquidity:
        # unreserved = pool_balance - total_reserved_payout
        # available = unreserved + premium
        unreserved_liquidity = self.insurance_pool_balance - self.total_reserved_payout
        if payout > unreserved_liquidity + premium:
            raise _UserError(
                "Insurance pool has insufficient unreserved liquidity to underwrite this policy."
            )

        # Lock full liability in total_reserved_payout
        self.insurance_pool_balance += premium
        self.total_reserved_payout += payout
        self.policy_count += bigint(1)
        pid = str(self.policy_count)

        self.policies[pid] = InsurancePolicy(
            policy_id=pid,
            passenger=_get_sender(),
            flight_code=clean_flight,
            flight_date=clean_date,
            departure_timestamp=dep_ts,
            arrival_timestamp=arr_ts,
            tracking_url=clean_url,
            premium_paid=premium,
            payout_amount=payout,
            status="ACTIVE",
            flight_status="PENDING",
            reason="Flight coverage active. Awaiting settlement check.",
            created_at=self.policy_count,
            resolved_at=bigint(0)
        )

        return pid

    @gl.public.write
    def settle_policy(self, policy_id: str) -> None:
        """
        Triggers decentralized AI assessment of flight status from the authoritative tracking page.
        Settlement is only eligible after scheduled arrival timestamp.
        Guarantees full payout or rejects settlement without altering policy.
        Reconciles total_reserved_payout in all cases.
        """
        if policy_id not in self.policies:
            raise _UserError("Policy not found.")

        policy = self.policies[policy_id]
        if policy.status != "ACTIVE":
            raise _UserError("Policy is not active or already settled.")

        # Enforce settlement only after eligibility (flight arrival time)
        current_ts = self._get_current_timestamp()
        if current_ts < policy.arrival_timestamp:
            raise _UserError(
                "Policy cannot be settled before scheduled arrival time."
            )

        # Extract storage data to local variables BEFORE nondet block
        flight_code_local = str(policy.flight_code)
        flight_date_local = str(policy.flight_date)
        tracking_url_local = str(policy.tracking_url)

        def leader_fn():
            # 1. Fetch authoritative flight status webpage
            web_text = ""
            try:
                res = gl.nondet.web.render(tracking_url_local, mode="text")
                web_text = res.content if hasattr(res, "content") else str(res)
            except Exception:
                web_text = ""

            if len(web_text.strip()) < 20:
                return {
                    "flight_status": "ON_TIME",
                    "delay_minutes": 0,
                    "confidence": 100,
                    "reason": "Tracking page offline or blank; defaulted to on-time."
                }

            snippet = web_text[:4000]

            # 2. Formulate Flight Status Audit Prompt strictly bound to flight code & date
            prompt = f"""You are an Autonomous Flight Claim Auditor on the GenLayer decentralized consensus network.
Analyze the following flight status page content for FLIGHT CODE: {flight_code_local} on DATE: {flight_date_local}.
Determine whether this specific flight was CANCELLED, DELAYED by 180 minutes or more, or arrived ON_TIME / minor delay.

AUTHORITATIVE TRACKING CONTENT:
\"\"\"
{snippet}
\"\"\"

ASSESSMENT RULES:
- "CANCELLED": Flight {flight_code_local} on {flight_date_local} was explicitly cancelled, aborted, or diverted without reaching destination.
- "DELAYED": Flight arrival was delayed by 180 minutes (3 hours) or more compared to scheduled time.
- "ON_TIME": Flight landed on schedule, early, or with a minor delay strictly under 180 minutes.

OUTPUT FORMAT:
Respond ONLY with a VALID JSON object (no markdown, no backticks):
{{
  "flight_status": "CANCELLED" or "DELAYED" or "ON_TIME",
  "delay_minutes": <integer>,
  "confidence": <integer 0 to 100>,
  "reason": "<concise explanation max 200 characters>"
}}"""

            try:
                raw_res = gl.nondet.exec_prompt(prompt, response_format="json")
                parsed = None
                if isinstance(raw_res, dict):
                    parsed = raw_res
                elif hasattr(raw_res, "content") and isinstance(raw_res.content, dict):
                    parsed = raw_res.content
                else:
                    text = raw_res.content if hasattr(raw_res, "content") else str(raw_res)
                    cleaned = str(text).strip()
                    if cleaned.startswith("```json"):
                        cleaned = cleaned[7:]
                    elif cleaned.startswith("```"):
                        cleaned = cleaned[3:]
                    if cleaned.endswith("```"):
                        cleaned = cleaned[:-3]
                    parsed = json.loads(cleaned.strip())

                status = str(parsed.get("flight_status", "ON_TIME")).strip().upper()
                if status not in ("CANCELLED", "DELAYED", "ON_TIME"):
                    status = "ON_TIME"

                try:
                    delay_m = int(parsed.get("delay_minutes", 0))
                except Exception:
                    delay_m = 0

                try:
                    conf = int(parsed.get("confidence", 0))
                    conf = max(0, min(100, conf))
                except Exception:
                    conf = 50

                reason_str = str(parsed.get("reason", "Flight status evaluated by AI."))[:200]

                return {
                    "flight_status": status,
                    "delay_minutes": delay_m,
                    "confidence": conf,
                    "reason": reason_str
                }
            except Exception as e:
                return {
                    "flight_status": "ON_TIME",
                    "delay_minutes": 0,
                    "confidence": 0,
                    "reason": f"Audit execution failed: {str(e)[:100]}"
                }

        def validator_fn(leader_res) -> bool:
            if not isinstance(leader_res, gl.vm.Return):
                return False
            leader = leader_res.calldata
            if not isinstance(leader, dict) or "flight_status" not in leader:
                return False

            mine = leader_fn()
            # CRITICAL: Compare semantic flight_status ONLY
            s_mine = str(mine.get("flight_status", "")).strip().upper()
            s_leader = str(leader.get("flight_status", "")).strip().upper()
            return s_mine == s_leader

        adjudication_res = gl.vm.run_nondet(leader_fn, validator_fn)
        if isinstance(adjudication_res, dict):
            final_res = adjudication_res
        else:
            final_res = self._parse_llm_json(str(adjudication_res))

        flight_status = str(final_res.get("flight_status", "ON_TIME")).strip().upper()
        if flight_status not in ("CANCELLED", "DELAYED", "ON_TIME"):
            flight_status = "ON_TIME"

        reason = str(final_res.get("reason", "Consensus concluded."))

        passenger_addr = policy.passenger
        payout = policy.payout_amount

        # Automatic trigger: Payout if CANCELLED or DELAYED >= 180 min
        if flight_status in ("CANCELLED", "DELAYED"):
            # Enforce full payout guarantee without haircut:
            # If pool balance is insufficient, reject settlement without modifying policy!
            if self.insurance_pool_balance < payout:
                raise _UserError(
                    "Pool balance insufficient to disburse full guaranteed payout. Settlement rejected without modifying policy."
                )

            # Deduct both liquid balance and reserved liability
            self.insurance_pool_balance -= payout
            self.total_reserved_payout -= payout

            policy.status = "PAID_OUT"
            policy.flight_status = flight_status
            policy.reason = reason
            policy.resolved_at = self.policy_count
            self.policies[policy_id] = policy

            # Transfer payout to passenger (cast to u256)
            if payout > bigint(0):
                gl.get_contract_at(passenger_addr).emit_transfer(value=u256(payout))
        else:
            # Flight was on-time: release reserved liability back to unreserved pool
            self.total_reserved_payout -= payout

            policy.status = "EXPIRED"
            policy.flight_status = "ON_TIME"
            policy.reason = reason
            policy.resolved_at = self.policy_count
            self.policies[policy_id] = policy

    @gl.public.write
    def withdraw_pool(self, amount: int) -> None:
        """
        Underwriters / contract owner withdraws available surplus liquidity.
        Strictly restricted to true surplus (pool balance minus total reserved liability).
        """
        if _addr_str(_get_sender()) != _addr_str(self.owner):
            raise _UserError("Only contract owner can withdraw pool liquidity.")
        amt = bigint(amount)
        if amt <= bigint(0):
            raise _UserError("Withdrawal amount must be greater than 0.")

        # Restrict withdrawals to true surplus
        true_surplus = self.insurance_pool_balance - self.total_reserved_payout
        if amt > true_surplus:
            raise _UserError(
                "Cannot withdraw reserved liability; only unreserved surplus can be withdrawn."
            )

        self.insurance_pool_balance -= amt
        gl.get_contract_at(self.owner).emit_transfer(value=u256(amt))

    @gl.public.write
    def set_payout_multiplier(self, multiplier: int) -> None:
        """Adjust the payout multiplier for new insurance policies (owner only)."""
        if _addr_str(_get_sender()) != _addr_str(self.owner):
            raise _UserError("Only contract owner can adjust payout multiplier.")
        if multiplier < 1:
            raise _UserError("Multiplier must be at least 1.")
        self.payout_multiplier = bigint(multiplier)

    @gl.public.write
    def set_min_purchase_lead_time(self, lead_time_seconds: int) -> None:
        """Adjust the purchase cutoff lead time buffer in seconds (owner only)."""
        if _addr_str(_get_sender()) != _addr_str(self.owner):
            raise _UserError("Only contract owner can adjust lead time buffer.")
        if lead_time_seconds < 0:
            raise _UserError("Lead time cannot be negative.")
        self.min_purchase_lead_time = bigint(lead_time_seconds)

    @gl.public.view
    def get_policy(self, policy_id: str) -> str:
        """Retrieve details of an insurance policy as a JSON string."""
        if policy_id not in self.policies:
            raise _UserError("Policy not found.")
        p = self.policies[policy_id]
        return json.dumps({
            "policy_id": p.policy_id,
            "passenger": _addr_str(p.passenger),
            "flight_code": p.flight_code,
            "flight_date": p.flight_date,
            "departure_timestamp": str(p.departure_timestamp),
            "arrival_timestamp": str(p.arrival_timestamp),
            "tracking_url": p.tracking_url,
            "premium_paid": str(p.premium_paid),
            "payout_amount": str(p.payout_amount),
            "status": p.status,
            "flight_status": p.flight_status,
            "reason": p.reason,
            "created_at": str(p.created_at),
            "resolved_at": str(p.resolved_at)
        })

    @gl.public.view
    def get_pool_balance(self) -> int:
        """Total funds currently in the insurance contract pool."""
        return int(self.insurance_pool_balance)

    @gl.public.view
    def get_reserved_payout(self) -> int:
        """Total liabilities reserved for active policies."""
        return int(self.total_reserved_payout)

    @gl.public.view
    def get_unreserved_liquidity(self) -> int:
        """Unreserved surplus liquidity available to underwrite new policies."""
        return int(self.insurance_pool_balance - self.total_reserved_payout)

    @gl.public.view
    def get_policy_count(self) -> int:
        return int(self.policy_count)

    @gl.public.view
    def get_payout_multiplier(self) -> int:
        return int(self.payout_multiplier)

    @gl.public.view
    def get_min_purchase_lead_time(self) -> int:
        return int(self.min_purchase_lead_time)

    @gl.public.view
    def get_contract_stats(self) -> str:
        return json.dumps({
            "owner": _addr_str(self.owner),
            "insurance_pool_balance": str(self.insurance_pool_balance),
            "total_reserved_payout": str(self.total_reserved_payout),
            "unreserved_liquidity": str(self.insurance_pool_balance - self.total_reserved_payout),
            "policy_count": str(self.policy_count),
            "payout_multiplier": str(self.payout_multiplier),
            "min_purchase_lead_time": str(self.min_purchase_lead_time)
        })
