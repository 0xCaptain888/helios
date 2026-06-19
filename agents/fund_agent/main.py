"""Fund agent — the marketplace's first customer.

Each round it:
  1. discovers active listings on DataMarket (reads chain state / MCP in prod)
  2. pays each oracle per-request via x402 (real value transfer + on-chain
     receipt anchoring, growing the oracles' reputation)
  3. runs its decision engine over the purchased data
       - default: transparent momentum/yield rules (reproducible offline)
       - optional: set ANTHROPIC_API_KEY to have Claude write the allocation
         rationale (HELIOS_USE_LLM=1)
  4. submits the rebalance proposal to Governance (on-chain)
  5. waits out the risk agent's veto window; if vetoed, repairs the allocation
     to comply and resubmits; if clear, finalizes and executes on FundVault

Every economic action leaves an on-chain trace. That is the demo.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request

from ..common import config, x402


class FundAgent:
    def __init__(self, chain, bus, risk_agent):
        self.chain = chain
        self.bus = bus
        self.risk = risk_agent
        self.wallet = x402.Wallet.create("fund_agent")
        self.chain.create_account("fund_agent", 500 * config.CSPR, self.wallet.address)
        self.chain.vault_set_operator(self.wallet.address)
        deposit = self.chain.vault_deposit(self.wallet.address, 200 * config.CSPR)
        self.bus.deploy(hash_=deposit, kind="vault.deposit",
                        explorer=self.chain.explorer_link(deposit))
        self.history: dict[str, list[float]] = {}
        self.nav_micro = 1_000_000
        self.round_no = 0

    # ---------- market discovery + x402 purchasing ----------
    def discover_listings(self) -> list[dict]:
        return [l for l in self.chain.state["market"]["listings"] if l["active"]]

    def buy_quote(self, listing: dict) -> dict:
        """Full x402 dance: 402 -> sign -> pay -> data."""
        url = listing["endpoint"]
        # 1st request: expect 402 with payment requirements
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                first = json.loads(resp.read())
                status = resp.status
        except urllib.error.HTTPError as err:
            status = err.code
            first = json.loads(err.read())
        if status != 402:
            raise RuntimeError(f"expected 402 from {url}, got {status}")

        header = x402.build_payment_header(self.wallet, first)
        req = urllib.request.Request(url, headers={"X-PAYMENT": header})
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
            settlement = x402.decode_settlement_header(
                resp.headers["X-PAYMENT-RESPONSE"])
        body["_settlement"] = settlement
        body["_price_motes"] = listing["price_motes"]
        return body

    # ---------- decision engine ----------
    def decide(self, quotes: dict[str, dict], aggressive: bool) -> tuple[list[dict], str]:
        """Return (positions, rationale). `aggressive` deliberately violates the
        risk policy once in the demo so judges see the veto fire."""
        for key, q in quotes.items():
            self.history.setdefault(key, []).append(float(q["value"]))

        def momentum(key: str) -> float:
            series = self.history[key]
            if len(series) < 2:
                return 0.0
            return (series[-1] - series[-2]) / max(abs(series[-2]), 1e-9)

        signals = {
            "US_TBILL_3M": max(0.0, float(quotes["us_tbill_3m"]["value"]) / 10.0),
            "XAU": max(0.0, 0.5 + momentum("xau_usd") * 8),
            "RE_INDEX_US": max(0.0, 0.5 + momentum("re_index_us") * 8),
        }
        if aggressive:
            # over-concentrate into the hottest signal and drain the cash floor
            hottest = max(signals, key=signals.get)
            positions = [
                {"asset": "CSPR", "weight_bps": 500},
                {"asset": hottest, "weight_bps": 7000},
            ]
            rest = [a for a in signals if a != hottest]
            split = (10_000 - 7_500) // len(rest)
            positions += [{"asset": a, "weight_bps": split} for a in rest]
            gap = 10_000 - sum(p["weight_bps"] for p in positions)
            positions[0]["weight_bps"] += gap
            rationale = (f"Momentum on {hottest} is the strongest signal this round; "
                         f"concentrating 70% to capture it (aggressive mode).")
            return positions, rationale

        total = sum(signals.values()) or 1.0
        cash_bps = 1_500
        budget = 10_000 - cash_bps
        positions = [{"asset": "CSPR", "weight_bps": cash_bps}]
        acc = 0
        items = sorted(signals.items())
        for i, (asset, sig) in enumerate(items):
            if i == len(items) - 1:
                w = budget - acc
            else:
                w = min(int(budget * sig / total), config.RISK_MAX_SINGLE_RWA_BPS)
                acc += w
            positions.append({"asset": asset, "weight_bps": w})
        # repair any cap breach on the remainder bucket
        for p in positions[1:]:
            if p["weight_bps"] > config.RISK_MAX_SINGLE_RWA_BPS:
                overflow = p["weight_bps"] - config.RISK_MAX_SINGLE_RWA_BPS
                p["weight_bps"] = config.RISK_MAX_SINGLE_RWA_BPS
                positions[0]["weight_bps"] += overflow
        rationale = self._rationale(quotes, signals, positions)
        return positions, rationale

    def _rationale(self, quotes, signals, positions) -> str:
        if os.environ.get("HELIOS_USE_LLM") == "1" and os.environ.get("ANTHROPIC_API_KEY"):
            try:
                return self._claude_rationale(quotes, positions)
            except Exception as exc:  # fall back transparently
                return self._rule_rationale(quotes, signals) + f" (LLM offline: {exc})"
        return self._rule_rationale(quotes, signals)

    @staticmethod
    def _rule_rationale(quotes, signals) -> str:
        tb = quotes["us_tbill_3m"]["value"]
        return (f"3M T-Bill yields {tb}%, anchoring the rates sleeve; gold and "
                f"real-estate sleeves sized by momentum "
                f"({signals['XAU']:.2f} vs {signals['RE_INDEX_US']:.2f}), "
                f"15% CSPR kept liquid per policy.")

    def _claude_rationale(self, quotes, positions) -> str:
        body = json.dumps({
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 200,
            "messages": [{
                "role": "user",
                "content": ("You are the Helios fund agent. In 2 sentences, justify "
                            f"this allocation {positions} given data {quotes}. "
                            "Be specific, cite the numbers."),
            }],
        }).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=body,
            headers={"Content-Type": "application/json",
                     "x-api-key": os.environ["ANTHROPIC_API_KEY"],
                     "anthropic-version": "2023-06-01"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        return "".join(b.get("text", "") for b in data.get("content", []))

    # ---------- the full round ----------
    def run_round(self, aggressive: bool = False) -> None:
        self.round_no += 1
        listings = self.discover_listings()
        quotes: dict[str, dict] = {}
        receipts: list[str] = []
        spent = 0
        newest = time.time()
        for listing in listings:
            quote = self.buy_quote(listing)
            quotes[listing["feed_key"]] = quote
            receipts.append(quote["_settlement"]["txHash"])
            spent += quote["_price_motes"]
            newest = min(newest, quote["sampled_at"])

        positions, rationale = self.decide(quotes, aggressive)
        payload = json.dumps({
            "positions": positions,
            "data_receipts": receipts,
            "max_data_age_seconds": time.time() - newest,
        })
        summary = f"Round {self.round_no}: " + ", ".join(
            f"{p['asset']} {p['weight_bps']/100:.1f}%" for p in positions)

        pid, submit_deploy = self.chain.gov_submit(self.wallet.address, summary, payload)
        self.bus.deploy(hash_=submit_deploy, kind="gov.submit",
                        explorer=self.chain.explorer_link(submit_deploy))
        self.bus.governance({
            "ts": time.time(), "id": pid, "summary": summary, "status": "pending",
            "reason": "", "deploy": submit_deploy,
            "explorer": self.chain.explorer_link(submit_deploy),
        })

        vetoed, reason = self.risk.review(pid)
        decision_record = {
            "round": self.round_no, "rationale": rationale,
            "positions": positions, "proposal_id": pid,
            "spent_motes": spent, "receipts": receipts,
            "status": "vetoed" if vetoed else "pending",
            "veto_reason": reason, "ts": time.time(),
        }

        if vetoed:
            self.bus.decision(decision_record)
            # self-correct: rebuild a compliant allocation and resubmit
            positions, rationale = self.decide(quotes, aggressive=False)
            payload = json.dumps({"positions": positions, "data_receipts": receipts,
                                  "max_data_age_seconds": time.time() - newest})
            summary = (f"Round {self.round_no} (repaired): " + ", ".join(
                f"{p['asset']} {p['weight_bps']/100:.1f}%" for p in positions))
            pid, submit_deploy = self.chain.gov_submit(self.wallet.address, summary, payload)
            self.bus.deploy(hash_=submit_deploy, kind="gov.submit",
                            explorer=self.chain.explorer_link(submit_deploy))
            self.bus.governance({
                "ts": time.time(), "id": pid, "summary": summary, "status": "pending",
                "reason": "", "deploy": submit_deploy,
                "explorer": self.chain.explorer_link(submit_deploy),
            })
            second_veto, reason = self.risk.review(pid)
            if second_veto:  # should not happen; abort the round safely
                self.bus.decision({**decision_record, "status": "aborted",
                                   "veto_reason": reason})
                return
            decision_record = {
                "round": self.round_no, "rationale": rationale + " (repaired after veto)",
                "positions": positions, "proposal_id": pid,
                "spent_motes": spent, "receipts": receipts,
                "status": "pending", "veto_reason": "", "ts": time.time(),
            }

        time.sleep(config.VETO_WINDOW_SECONDS + 0.2)
        finalize_deploy = self.chain.gov_finalize(self.wallet.address, pid)
        self.bus.deploy(hash_=finalize_deploy, kind="gov.finalize",
                        explorer=self.chain.explorer_link(finalize_deploy))
        self.bus.governance({
            "ts": time.time(), "id": pid,
            "summary": f"Proposal #{pid} approved", "status": "approved", "reason": "",
            "deploy": finalize_deploy,
            "explorer": self.chain.explorer_link(finalize_deploy),
        })

        self.nav_micro = self._mark_nav(quotes)
        exec_deploy = self.chain.execute_rebalance(
            self.wallet.address, pid, positions, self.nav_micro, ",".join(receipts))
        self.bus.deploy(hash_=exec_deploy, kind="vault.execute_rebalance",
                        explorer=self.chain.explorer_link(exec_deploy))
        self.bus.nav(self.round_no, self.nav_micro)
        decision_record["status"] = "executed"
        decision_record["exec_deploy"] = exec_deploy
        self.bus.decision(decision_record)

    def _mark_nav(self, quotes) -> int:
        """Toy NAV mark: yield carry + weighted momentum of held assets."""
        carry = float(quotes["us_tbill_3m"]["value"]) / 100.0 / 52.0
        drift = 0.0
        for key in ("xau_usd", "re_index_us"):
            series = self.history.get(key, [])
            if len(series) >= 2:
                drift += (series[-1] - series[-2]) / max(abs(series[-2]), 1e-9) / 2
        factor = 1.0 + carry + drift * 0.3
        return max(1, int(self.nav_micro * factor))
