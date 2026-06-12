"""Risk agent — the on-chain veto power. Autonomous but adversarial by design:
it does NOT trust the fund agent. It re-checks every proposal against hard
policy limits and vetoes on-chain with a written reason when breached."""
from __future__ import annotations

import json
import time

from ..common import config, x402


class RiskAgent:
    def __init__(self, chain, bus):
        self.chain = chain
        self.bus = bus
        self.wallet = x402.Wallet.create("risk_agent")
        self.chain.create_account("risk_agent", 20 * config.CSPR, self.wallet.address)

    def review(self, proposal_id: int) -> tuple[bool, str]:
        """Return (vetoed, reason)."""
        proposal = self.chain.state["gov"]["proposals"][proposal_id]
        payload = json.loads(proposal["payload"])
        positions = payload["positions"]
        data_age = payload.get("max_data_age_seconds", 0.0)

        reasons = []
        cash_bps = 0
        for p in positions:
            if p["asset"] == "CSPR":
                cash_bps = p["weight_bps"]
            elif p["weight_bps"] > config.RISK_MAX_SINGLE_RWA_BPS:
                reasons.append(
                    f"{p['asset']} at {p['weight_bps']/100:.1f}% breaches the "
                    f"{config.RISK_MAX_SINGLE_RWA_BPS/100:.0f}% single-RWA cap")
        if cash_bps < config.RISK_MIN_CASH_BPS:
            reasons.append(
                f"CSPR reserve {cash_bps/100:.1f}% is below the "
                f"{config.RISK_MIN_CASH_BPS/100:.0f}% liquidity floor")
        if data_age > config.RISK_MAX_DATA_AGE_SECONDS:
            reasons.append(f"decision data is {data_age:.0f}s old (limit "
                           f"{config.RISK_MAX_DATA_AGE_SECONDS:.0f}s)")

        if not reasons:
            return False, ""
        reason = "; ".join(reasons)
        deploy = self.chain.gov_veto(self.wallet.address, proposal_id, reason)
        self.bus.governance({
            "ts": time.time(), "id": proposal_id,
            "summary": proposal["summary"], "status": "vetoed", "reason": reason,
            "deploy": deploy, "explorer": self.chain.explorer_link(deploy),
        })
        self.bus.deploy(hash_=deploy, kind="gov.veto",
                        explorer=self.chain.explorer_link(deploy))
        return True, reason
