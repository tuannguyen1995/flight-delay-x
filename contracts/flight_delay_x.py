# v0.2.16
# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
from genlayer import *
from dataclasses import dataclass
import json


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
            raise gl.UserError("Cannot resolve sender address.")


@allow_storage
@dataclass
class InsurancePolicy:
    """Storage struct representing a flight parametric insurance policy."""
    policy_id: str
    passenger: Address
    flight_code: str       # e.g., "VN210", "AA100"
    tracking_url: str      # Public flight tracking status page
    premium_paid: bigint   # Price paid for the policy
    payout_amount: bigint  # Guaranteed payout if delayed/cancelled
    status: str            # "ACTIVE", "PAID_OUT", "EXPIRED"
    flight_status: str     # "PENDING", "DELAYED", "CANCELLED", "ON_TIME"
    reason: str            # AI Consensus explanation
    created_at: bigint
    resolved_at: bigint


class Contract(gl.Contract):
    """
    FlightDelayX: Parametric Flight Delay & Cancellation Insurance Protocol
    Track: Prediction Markets & Real-World Settlement
    """
    owner: Address
    insurance_pool_balance: bigint
    policy_count: bigint
    payout_multiplier: bigint  # e.g., 3x premium as default payout
    policies: TreeMap[str, InsurancePolicy]

    def __init__(self):
        # GenVM automatically initializes TreeMap and DynArray fields.
        # DO NOT reassign self.policies = TreeMap() here to prevent AssertionError.
        self.owner = _get_sender()
        self.insurance_pool_balance = bigint(0)
        self.policy_count = bigint(0)
        self.payout_multiplier = bigint(3)  # 3x payout multiplier

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
            raise gl.UserError("Pool deposit must be greater than 0 GEN.")
        self.insurance_pool_balance += deposit

    @gl.public.write.payable
    def buy_policy(self, flight_code: str, tracking_url: str) -> str:
        """
        Passengers buy insurance for their flight by paying a premium.
        Guaranteed payout is defined as premium_paid * payout_multiplier.
        """
        premium = bigint(gl.message.value)
        if premium <= bigint(0):
            raise gl.UserError("Premium paid must be greater than 0 GEN.")

        clean_flight = flight_code.strip().upper()
        clean_url = tracking_url.strip()

        if len(clean_flight) < 3:
            raise gl.UserError("Invalid flight_code format.")
        if not clean_url.startswith("http://") and not clean_url.startswith("https://"):
            raise gl.UserError("tracking_url must start with http:// or https://")

        payout = premium * self.payout_multiplier

        # Verify pool solvency for this policy
        if payout > self.insurance_pool_balance + premium:
            raise gl.UserError("Insurance pool has insufficient liquidity to underwrite this policy.")

        self.insurance_pool_balance += premium
        self.policy_count += bigint(1)
        pid = str(self.policy_count)

        self.policies[pid] = InsurancePolicy(
            policy_id=pid,
            passenger=_get_sender(),
            flight_code=clean_flight,
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
        Triggers decentralized AI assessment of flight status from the public tracking page.
        If delayed >= 180 mins or cancelled, auto-disburses payout to passenger.
        """
        if policy_id not in self.policies:
            raise gl.UserError("Policy not found.")

        policy = self.policies[policy_id]
        if policy.status != "ACTIVE":
            raise gl.UserError("Policy is not active or already settled.")

        # Extract storage data to local variables BEFORE nondet block
        flight_code_local = str(policy.flight_code)
        tracking_url_local = str(policy.tracking_url)

        def leader_fn():
            # 1. Fetch live flight status webpage
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

            # 2. Formulate Flight Status Audit Prompt
            prompt = f"""You are an Autonomous Flight Claim Auditor on the GenLayer decentralized consensus network.
Analyze the following flight status page content for FLIGHT CODE: {flight_code_local}.
Determine whether the flight was CANCELLED, DELAYED by 180 minutes or more, or arrived ON_TIME / minor delay.

FLIGHT TRACKING CONTENT:
\"\"\"
{snippet}
\"\"\"

ASSESSMENT RULES:
- "CANCELLED": Flight was explicitly cancelled, aborted, or diverted without reaching destination.
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

        policy.flight_status = flight_status
        policy.reason = reason
        policy.resolved_at = self.policy_count

        passenger_addr = policy.passenger
        payout = policy.payout_amount

        # Automatic trigger: Payout if CANCELLED or DELAYED >= 180 min
        if flight_status in ("CANCELLED", "DELAYED"):
            policy.status = "PAID_OUT"
            if payout > self.insurance_pool_balance:
                payout = self.insurance_pool_balance

            self.insurance_pool_balance -= payout
            self.policies[policy_id] = policy

            # Transfer payout to passenger (cast to u256)
            if payout > bigint(0):
                gl.get_contract_at(passenger_addr).emit_transfer(value=u256(payout))
        else:
            # Flight was on-time: premium retained by pool
            policy.status = "EXPIRED"
            self.policies[policy_id] = policy

    @gl.public.write
    def withdraw_pool(self, amount: int) -> None:
        """Underwriters / contract owner withdraws available surplus liquidity."""
        if _addr_str(_get_sender()) != _addr_str(self.owner):
            raise gl.UserError("Only contract owner can withdraw pool liquidity.")
        amt = bigint(amount)
        if amt <= bigint(0):
            raise gl.UserError("Withdrawal amount must be greater than 0.")
        if amt > self.insurance_pool_balance:
            raise gl.UserError("Insufficient pool liquidity.")
        self.insurance_pool_balance -= amt
        gl.get_contract_at(self.owner).emit_transfer(value=u256(amt))

    @gl.public.write
    def set_payout_multiplier(self, multiplier: int) -> None:
        """Adjust the payout multiplier for new insurance policies (owner only)."""
        if _addr_str(_get_sender()) != _addr_str(self.owner):
            raise gl.UserError("Only contract owner can adjust payout multiplier.")
        if multiplier < 1:
            raise gl.UserError("Multiplier must be at least 1.")
        self.payout_multiplier = bigint(multiplier)

    @gl.public.view
    def get_policy(self, policy_id: str) -> str:
        """Retrieve details of an insurance policy as a JSON string."""
        if policy_id not in self.policies:
            raise gl.UserError("Policy not found.")
        p = self.policies[policy_id]
        return json.dumps({
            "policy_id": p.policy_id,
            "passenger": _addr_str(p.passenger),
            "flight_code": p.flight_code,
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
        return int(self.insurance_pool_balance)

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
            "policy_count": str(self.policy_count),
            "payout_multiplier": str(self.payout_multiplier)
        })
