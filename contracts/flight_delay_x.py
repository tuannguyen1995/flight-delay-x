# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
from genlayer import *
from dataclasses import dataclass
import json


class UserError(Exception):
    """Custom contract user error for GenVM reverts."""
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
            raise UserError("Cannot resolve sender address.")


def _safe_transfer(recipient: Address, amount: bigint) -> None:
    """Safely disburse native GEN to an address using official GenLayer SDK pattern."""
    if amount <= bigint(0):
        return
    gl.get_contract_at(recipient).emit_transfer(value=u256(int(amount)))


@allow_storage
@dataclass
class InsurancePolicy:
    """Storage struct representing a flight parametric insurance policy."""
    policy_id: str
    passenger: Address
    flight_code: str              # e.g., "VN210", "AA100"
    flight_date: str              # e.g., "2026-10-05" (YYYY-MM-DD)
    tracking_url: str             # Authoritative tracking source
    premium_paid: bigint
    payout_amount: bigint
    status: str                   # "ACTIVE", "PAID_OUT", "EXPIRED"
    flight_status: str            # "PENDING", "DELAYED", "CANCELLED", "ON_TIME", "INVALID_CLAIM"
    purchase_timestamp: bigint
    reason: str                   # AI Consensus breakdown
    created_at: bigint
    resolved_at: bigint


class Contract(gl.Contract):
    """
    FlightDelayX: Parametric Flight Delay & Cancellation Insurance Protocol
    Track: Prediction Markets & Real-World Settlement
    """
    owner: Address
    insurance_pool_balance: bigint
    total_reserved_payout: bigint
    policy_count: bigint
    payout_multiplier: bigint
    policies: TreeMap[str, InsurancePolicy]

    def __init__(self):
        # GenVM automatically initializes TreeMap and DynArray fields.
        self.owner = _get_sender()
        self.insurance_pool_balance = bigint(0)
        self.total_reserved_payout = bigint(0)
        self.policy_count = bigint(0)
        self.payout_multiplier = bigint(3)

    def _get_current_timestamp(self) -> bigint:
        """Derive trusted timestamp from transaction execution context or datetime."""
        from datetime import datetime, timezone
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

    def _validate_date_format(self, d_str: str) -> None:
        """Validate that date string matches YYYY-MM-DD format and calendar validity."""
        from datetime import datetime
        if len(d_str) != 10 or d_str[4] != "-" or d_str[7] != "-":
            raise UserError("flight_date must be in YYYY-MM-DD format.")
        try:
            datetime.strptime(d_str, "%Y-%m-%d")
        except Exception:
            raise UserError("Invalid calendar date in flight_date.")

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
                "is_historical_exploit": False,
                "confidence": 0,
                "reason": f"Failed to parse LLM JSON: {str(e)[:100]}"
            }

    @gl.public.write.payable
    def fund_insurance_pool(self) -> None:
        """Underwriters deposit GEN liquidity to cover future flight claim payouts."""
        deposit = bigint(gl.message.value)
        if deposit <= bigint(0):
            raise UserError("Pool deposit must be greater than 0 GEN.")
        self.insurance_pool_balance += deposit

    @gl.public.write.payable
    def buy_policy(self, flight_code: str, flight_date: str) -> str:
        """
        Passengers buy insurance for a specific flight on a specific date.
        Generates canonical tracking URL and reserves liquidity under strict solvency checks.
        Rejects strictly historical dates on-chain.
        """
        premium = bigint(gl.message.value)
        if premium <= bigint(0):
            raise UserError("Premium paid must be greater than 0 GEN.")

        clean_flight = flight_code.strip().upper()
        clean_date = flight_date.strip()

        if len(clean_flight) < 3:
            raise UserError("Invalid flight_code format.")
        self._validate_date_format(clean_date)

        current_ts = self._get_current_timestamp()

        # Reject historical flight dates on-chain
        from datetime import datetime, timezone
        flight_dt = datetime.strptime(clean_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        day_end_ts = int(flight_dt.timestamp()) + 86400
        if day_end_ts <= int(current_ts):
            raise UserError("Cannot purchase policy for a historical flight date.")

        # Contract binds strictly to flight code and date on canonical tracker
        canonical_url = f"https://flightaware.com/live/flight/{clean_flight}/history/{clean_date}"
        payout = premium * self.payout_multiplier

        # Verify pool solvency against unreserved liquidity
        unreserved_liquidity = self.insurance_pool_balance - self.total_reserved_payout
        if payout > unreserved_liquidity + premium:
            raise UserError("Insurance pool has insufficient unreserved liquidity.")

        self.insurance_pool_balance += premium
        self.total_reserved_payout += payout
        self.policy_count += bigint(1)
        pid = str(self.policy_count)

        self.policies[pid] = InsurancePolicy(
            policy_id=pid,
            passenger=_get_sender(),
            flight_code=clean_flight,
            flight_date=clean_date,
            tracking_url=canonical_url,
            premium_paid=premium,
            payout_amount=payout,
            status="ACTIVE",
            flight_status="PENDING",
            purchase_timestamp=current_ts,
            reason="Policy active. Bound to verified flight date and code.",
            created_at=self.policy_count,
            resolved_at=bigint(0)
        )

        return pid

    @gl.public.write
    def settle_policy(self, policy_id: str) -> None:
        """
        AI validators inspect the authoritative flight history page on-chain.
        Validators independently cross-reference the flight date and actual operational times
        to ensure coverage was not purchased after the outcome was already determined.
        """
        if policy_id not in self.policies:
            raise UserError("Policy not found.")

        policy = self.policies[policy_id]
        if policy.status != "ACTIVE":
            raise UserError("Policy is not active or already settled.")

        flight_code_local = str(policy.flight_code)
        flight_date_local = str(policy.flight_date)
        tracking_url_local = str(policy.tracking_url)
        purchase_ts_local = int(policy.purchase_timestamp)

        def leader_fn():
            web_text = ""
            try:
                res = gl.nondet.web.render(tracking_url_local, mode="text")
                web_text = res.content if hasattr(res, "content") else str(res)
            except Exception:
                web_text = ""

            if len(web_text.strip()) < 20:
                return {
                    "flight_status": "ON_TIME",
                    "is_historical_exploit": False,
                    "confidence": 100,
                    "reason": "Tracking page offline or blank; defaulted to on-time."
                }

            snippet = web_text[:4000]

            prompt = f"""You are an Autonomous Flight Claim Auditor on the GenLayer decentralized consensus network.
Analyze the following flight tracking content for FLIGHT: {flight_code_local} on DATE: {flight_date_local}.
The insurance policy was purchased at Unix timestamp: {purchase_ts_local}.

TRACKING DATA:
\"\"\"
{snippet}
\"\"\"

LIFECYCLE & ELIGIBILITY VERIFICATION RULES:
1. Verify if this flight record corresponds to flight {flight_code_local} on date {flight_date_local}.
2. Check if the flight has completed its journey:
   - If the flight is still scheduled in the future, delayed but not yet departed/landed, or currently in air:
     return "flight_status": "NOT_CONCLUDED", "is_historical_exploit": false.
3. Check for historical exploit:
   - If the flight had already departed, was cancelled, or concluded BEFORE policy purchase timestamp {purchase_ts_local}:
     return "flight_status": "INVALID_CLAIM", "is_historical_exploit": true.
4. For legitimate completed flights purchased prior to departure:
   - "CANCELLED": Flight was explicitly cancelled or diverted.
   - "DELAYED": Flight arrival was delayed by 180 minutes (3 hours) or more compared to scheduled arrival.
   - "ON_TIME": Flight landed on schedule or with minor delay under 180 minutes. Also return "ON_TIME" if data shows normal on-time operations.

OUTPUT FORMAT:
Respond ONLY with a VALID JSON object (no markdown, no backticks):
{{
  "flight_status": "CANCELLED" | "DELAYED" | "ON_TIME" | "NOT_CONCLUDED" | "INVALID_CLAIM",
  "is_historical_exploit": true or false,
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
                if status not in ("CANCELLED", "DELAYED", "ON_TIME", "NOT_CONCLUDED", "INVALID_CLAIM"):
                    status = "ON_TIME"

                is_exploit = bool(parsed.get("is_historical_exploit", False))
                if is_exploit:
                    status = "INVALID_CLAIM"

                try:
                    conf = int(parsed.get("confidence", 0))
                    conf = max(0, min(100, conf))
                except Exception:
                    conf = 50

                reason_str = str(parsed.get("reason", "Evaluated by AI consensus."))[:200]

                return {
                    "flight_status": status,
                    "is_historical_exploit": is_exploit,
                    "confidence": conf,
                    "reason": reason_str
                }
            except Exception as e:
                return {
                    "flight_status": "ON_TIME",
                    "is_historical_exploit": False,
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
            # CRITICAL: STRICT MATCH ON FLIGHT STATUS & EXPLOIT DETECTION
            return (
                str(leader.get("flight_status", "")).strip().upper() == str(mine.get("flight_status", "")).strip().upper()
                and bool(leader.get("is_historical_exploit", False)) == bool(mine.get("is_historical_exploit", False))
            )

        adjudication_res = gl.vm.run_nondet(leader_fn, validator_fn)
        if isinstance(adjudication_res, dict):
            final_res = adjudication_res
        else:
            final_res = self._parse_llm_json(str(adjudication_res))

        flight_status = str(final_res.get("flight_status", "ON_TIME")).strip().upper()
        if flight_status not in ("CANCELLED", "DELAYED", "ON_TIME", "NOT_CONCLUDED", "INVALID_CLAIM"):
            flight_status = "ON_TIME"

        is_exploit = bool(final_res.get("is_historical_exploit", False))
        if is_exploit:
            flight_status = "INVALID_CLAIM"

        # If flight has not concluded yet, reject settlement so it can be retried later
        if flight_status == "NOT_CONCLUDED":
            raise UserError("Flight has not concluded yet. Settlement only eligible after arrival or cancellation.")

        reason = str(final_res.get("reason", "Consensus concluded."))
        passenger_addr = policy.passenger
        payout = policy.payout_amount

        # Automatic trigger: Payout ONLY if CANCELLED or DELAYED >= 180 min
        if flight_status in ("CANCELLED", "DELAYED"):
            if self.insurance_pool_balance < payout:
                raise UserError("Pool balance insufficient to disburse full payout.")

            self.insurance_pool_balance -= payout
            self.total_reserved_payout -= payout

            policy.status = "PAID_OUT"
            policy.flight_status = flight_status
            policy.reason = reason
            policy.resolved_at = self.policy_count
            self.policies[policy_id] = policy

            _safe_transfer(passenger_addr, payout)
        else:
            # ON_TIME or INVALID_CLAIM (Historical exploit attempt): Policy expires with no payout
            self.total_reserved_payout -= payout

            policy.status = "EXPIRED"
            policy.flight_status = flight_status
            policy.reason = reason
            policy.resolved_at = self.policy_count
            self.policies[policy_id] = policy

    @gl.public.write
    def withdraw_pool(self, amount: int) -> None:
        """Underwriters / contract owner withdraws available surplus liquidity."""
        if _addr_str(_get_sender()) != _addr_str(self.owner):
            raise UserError("Only contract owner can withdraw pool liquidity.")
        amt = bigint(amount)
        if amt <= bigint(0):
            raise UserError("Withdrawal amount must be greater than 0.")

        true_surplus = self.insurance_pool_balance - self.total_reserved_payout
        if amt > true_surplus:
            raise UserError("Cannot withdraw reserved liability; only unreserved surplus can be withdrawn.")

        self.insurance_pool_balance -= amt
        _safe_transfer(self.owner, amt)

    @gl.public.write
    def set_payout_multiplier(self, multiplier: int) -> None:
        """Adjust the payout multiplier for new insurance policies (owner only)."""
        if _addr_str(_get_sender()) != _addr_str(self.owner):
            raise UserError("Only contract owner can adjust payout multiplier.")
        if multiplier < 1:
            raise UserError("Multiplier must be at least 1.")
        self.payout_multiplier = bigint(multiplier)

    @gl.public.view
    def get_policy(self, policy_id: str) -> str:
        """Retrieve details of an insurance policy as a JSON string."""
        if policy_id not in self.policies:
            raise UserError("Policy not found.")
        p = self.policies[policy_id]
        return json.dumps({
            "policy_id": p.policy_id,
            "passenger": _addr_str(p.passenger),
            "flight_code": p.flight_code,
            "flight_date": p.flight_date,
            "tracking_url": p.tracking_url,
            "premium_paid": str(p.premium_paid),
            "payout_amount": str(p.payout_amount),
            "status": p.status,
            "flight_status": p.flight_status,
            "purchase_timestamp": str(p.purchase_timestamp),
            "reason": p.reason,
            "created_at": str(p.created_at),
            "resolved_at": str(p.resolved_at)
        })

    @gl.public.view
    def get_pool_balance(self) -> int:
        return int(self.insurance_pool_balance)

    @gl.public.view
    def get_reserved_payout(self) -> int:
        return int(self.total_reserved_payout)

    @gl.public.view
    def get_unreserved_liquidity(self) -> int:
        return int(self.insurance_pool_balance - self.total_reserved_payout)

    @gl.public.view
    def get_policy_count(self) -> int:
        return int(self.policy_count)

    @gl.public.view
    def get_payout_multiplier(self) -> int:
        return int(self.payout_multiplier)

    @gl.public.view
    def get_contract_stats(self) -> str:
        return json.dumps({
            "owner": _addr_str(self.owner),
            "insurance_pool_balance": str(self.insurance_pool_balance),
            "total_reserved_payout": str(self.total_reserved_payout),
            "unreserved_liquidity": str(self.insurance_pool_balance - self.total_reserved_payout),
            "policy_count": str(self.policy_count),
            "payout_multiplier": str(self.payout_multiplier)
        })
