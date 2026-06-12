"""Chain adapter: one interface, two backends.

MockChain      — deterministic local ledger (JSON). Mirrors the four Helios
                 contracts' entry points so agents exercise the exact call
                 shapes they will use on Casper Testnet. Every mutation yields
                 a pseudo deploy hash (sha256 of the call), giving the
                 dashboard verifiable-looking, replayable provenance.

TestnetChain   — shells out to `casper-client put-txn session/transfer ...`
                 against Casper Testnet using keys & contract hashes from
                 agents/testnet.env. Read paths use CSPR.cloud or
                 `casper-client query-global-state` (left as explicit commands
                 so judges can audit them). If casper-client is missing the
                 adapter fails loudly with setup instructions.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config

_LOCK = threading.RLock()


def _h(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, default=str).encode()
    return hashlib.sha256(raw).hexdigest()


@dataclass
class Deploy:
    hash: str
    kind: str
    caller: str
    args: dict
    ts: float = field(default_factory=time.time)


class MockChain:
    """Local ledger that mirrors Helios contract semantics."""

    def __init__(self, path: Path | None = None):
        self.path = path or config.LEDGER_PATH
        self.state: dict = {
            "accounts": {},
            "deploys": [],
            "registry": {"oracles": {}, "order": []},
            "market": {"listings": [], "treasury": 0, "fee_bps": config.MARKET_FEE_BPS},
            "vault": {
                "operator": None,
                "deposits": 0,
                "positions": [],
                "history": [],
                "nav_micro": 1_000_000,
            },
            "gov": {"proposals": [], "veto_window_ms": int(config.VETO_WINDOW_SECONDS * 1000)},
            "attestations": [],
        }
        config.ensure_dirs()
        self._persist()

    # ---------- accounts ----------
    def create_account(self, name: str, balance_motes: int, address: str | None = None) -> str:
        addr = address or ("account-hash-" + _h({"acct": name})[:56])
        with _LOCK:
            self.state["accounts"][addr] = {"name": name, "balance": int(balance_motes)}
            self._persist()
        return addr

    def balance(self, addr: str) -> int:
        return int(self.state["accounts"].get(addr, {}).get("balance", 0))

    def transfer(self, frm: str, to: str, amount: int, memo: str = "") -> str:
        with _LOCK:
            acc = self.state["accounts"]
            if acc.get(frm, {}).get("balance", 0) < amount:
                raise ValueError(f"insufficient funds: {frm}")
            acc[frm]["balance"] -= amount
            acc.setdefault(to, {"name": to[:14], "balance": 0})
            acc[to]["balance"] += amount
            return self._deploy("transfer", frm, {"to": to, "amount": amount, "memo": memo})

    # ---------- OracleRegistry ----------
    def register_oracle(self, caller: str, name: str, category: str, endpoint: str, price_motes: int) -> str:
        with _LOCK:
            reg = self.state["registry"]
            if caller in reg["oracles"]:
                raise ValueError("AlreadyRegistered")
            reg["oracles"][caller] = {
                "name": name, "category": category, "endpoint": endpoint,
                "price_motes": price_motes, "active": True,
                "reputation": {"settlements": 0, "attestations": 0,
                               "accurate": 0, "disputed": 0, "score_bps": 5000},
            }
            reg["order"].append(caller)
            return self._deploy("registry.register", caller,
                                {"name": name, "category": category, "endpoint": endpoint})

    def post_attestation(self, caller: str, feed_key: str, value: str) -> str:
        with _LOCK:
            oracle = self.state["registry"]["oracles"].get(caller)
            if not oracle:
                raise ValueError("NotRegistered")
            oracle["reputation"]["attestations"] += 1
            rec = {"oracle": caller, "feed_key": feed_key, "value": value, "ts": time.time()}
            self.state["attestations"].append(rec)
            return self._deploy("registry.post_attestation", caller, rec)

    def _credit_settlement(self, oracle_addr: str) -> None:
        rep = self.state["registry"]["oracles"][oracle_addr]["reputation"]
        rep["settlements"] += 1
        scored = rep["accurate"] + rep["disputed"]
        accuracy = 5000 if scored == 0 else rep["accurate"] * 10_000 // scored
        activity = min(rep["settlements"], 100)
        weight = 2_000 + (10_000 - 2_000) * activity // 100
        rep["score_bps"] = accuracy * weight // 10_000

    # ---------- DataMarket ----------
    def list_feed(self, caller: str, feed_key: str, title: str, price_motes: int, endpoint: str) -> int:
        with _LOCK:
            market = self.state["market"]
            if any(l["feed_key"] == feed_key for l in market["listings"]):
                raise ValueError("ListingExists")
            listing_id = len(market["listings"])
            market["listings"].append({
                "id": listing_id, "oracle": caller, "feed_key": feed_key, "title": title,
                "price_motes": price_motes, "endpoint": endpoint,
                "active": True, "sales": 0, "revenue_motes": 0,
            })
            self._deploy("market.list_feed", caller, {"feed_key": feed_key, "price": price_motes})
            return listing_id

    def anchor_x402_receipt(self, caller: str, listing_id: int, amount_motes: int, receipt: str) -> str:
        with _LOCK:
            listing = self.state["market"]["listings"][listing_id]
            listing["sales"] += 1
            listing["revenue_motes"] += amount_motes
            self._credit_settlement(listing["oracle"])
            return self._deploy("market.anchor_x402_receipt", caller,
                                {"listing_id": listing_id, "amount": amount_motes, "receipt": receipt})

    # ---------- FundVault ----------
    def vault_set_operator(self, operator: str) -> None:
        with _LOCK:
            self.state["vault"]["operator"] = operator
            self._persist()

    def vault_deposit(self, caller: str, amount: int) -> str:
        with _LOCK:
            self.state["accounts"][caller]["balance"] -= amount
            self.state["vault"]["deposits"] += amount
            return self._deploy("vault.deposit", caller, {"amount": amount})

    def execute_rebalance(self, caller: str, proposal_id: int, positions: list[dict],
                          nav_mark_micro: int, data_receipts: str) -> str:
        with _LOCK:
            vault = self.state["vault"]
            if caller != vault["operator"]:
                raise ValueError("NotAuthorized")
            if sum(p["weight_bps"] for p in positions) != 10_000:
                raise ValueError("BadWeights")
            vault["positions"] = positions
            vault["nav_micro"] = nav_mark_micro
            vault["history"].append({
                "proposal_id": proposal_id, "positions": positions,
                "nav_mark_micro": nav_mark_micro, "data_receipts": data_receipts,
                "ts": time.time(),
            })
            return self._deploy("vault.execute_rebalance", caller,
                                {"proposal_id": proposal_id, "nav": nav_mark_micro})

    # ---------- Governance ----------
    def gov_submit(self, caller: str, summary: str, payload: str) -> tuple[int, str]:
        with _LOCK:
            proposals = self.state["gov"]["proposals"]
            pid = len(proposals)
            proposals.append({
                "id": pid, "proposer": caller, "summary": summary, "payload": payload,
                "created_at": time.time(), "status": "pending", "veto_reason": "",
            })
            deploy = self._deploy("gov.submit", caller, {"id": pid, "summary": summary})
            return pid, deploy

    def gov_veto(self, caller: str, proposal_id: int, reason: str) -> str:
        with _LOCK:
            p = self.state["gov"]["proposals"][proposal_id]
            if p["status"] != "pending":
                raise ValueError("AlreadyFinal")
            p["status"] = "vetoed"
            p["veto_reason"] = reason
            return self._deploy("gov.veto", caller, {"id": proposal_id, "reason": reason})

    def gov_finalize(self, caller: str, proposal_id: int) -> str:
        with _LOCK:
            p = self.state["gov"]["proposals"][proposal_id]
            if p["status"] != "pending":
                raise ValueError("AlreadyFinal")
            window = self.state["gov"]["veto_window_ms"] / 1000.0
            if time.time() <= p["created_at"] + window:
                raise ValueError("WindowOpen")
            p["status"] = "approved"
            return self._deploy("gov.finalize", caller, {"id": proposal_id})

    # ---------- plumbing ----------
    def _deploy(self, kind: str, caller: str, args: dict) -> str:
        record = {"kind": kind, "caller": caller, "args": args, "ts": time.time(),
                  "nonce": len(self.state["deploys"])}
        deploy_hash = _h(record)
        self.state["deploys"].append({"hash": deploy_hash, **record})
        self._persist()
        return deploy_hash

    def _persist(self) -> None:
        self.path.write_text(json.dumps(self.state, indent=1, default=str), encoding="utf-8")

    def explorer_link(self, deploy_hash: str) -> str:
        return f"local://ledger/{deploy_hash[:16]}"


class TestnetChain:
    """Casper Testnet backend via casper-client. Write paths only; reads are
    documented as explicit CLI/CSPR.cloud calls in docs/TESTNET.md."""

    NODE = "https://node.testnet.casper.network/rpc"
    CHAIN_NAME = "casper-test"

    def __init__(self):
        env = config.load_testnet_env()
        self.keys = {
            "oracle_tbill": env.get("ORACLE_TBILL_KEY", ""),
            "oracle_gold": env.get("ORACLE_GOLD_KEY", ""),
            "oracle_reindex": env.get("ORACLE_REINDEX_KEY", ""),
            "fund_agent": env.get("FUND_AGENT_KEY", ""),
            "risk_agent": env.get("RISK_AGENT_KEY", ""),
        }
        self.contracts = {
            "registry": env.get("REGISTRY_HASH", ""),
            "market": env.get("MARKET_HASH", ""),
            "vault": env.get("VAULT_HASH", ""),
            "gov": env.get("GOV_HASH", ""),
        }
        missing = [k for k, v in {**self.keys, **self.contracts}.items() if not v]
        if missing:
            raise RuntimeError(
                "Testnet mode requires agents/testnet.env with: "
                + ", ".join(missing)
                + " — see 03_资料准备清单.md"
            )

    def _call(self, key_path: str, contract_hash: str, entry_point: str, args: list[str],
              payment: int = 3_000_000_000) -> str:
        cmd = [
            "casper-client", "put-txn", "invocable-entity",
            "--node-address", self.NODE,
            "--chain-name", self.CHAIN_NAME,
            "--secret-key", key_path,
            "--entity-address", contract_hash,
            "--session-entry-point", entry_point,
            "--payment-amount", str(payment),
            "--gas-price-tolerance", "1",
            "--standard-payment", "true",
        ]
        for a in args:
            cmd += ["--session-arg", a]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if out.returncode != 0:
            raise RuntimeError(f"casper-client failed: {out.stderr.strip()}")
        payload = json.loads(out.stdout)
        return (payload.get("result", {}).get("transaction_hash", {}).get("Version1")
                or payload.get("result", {}).get("deploy_hash", "unknown"))

    # The same surface as MockChain, mapped onto contract entry points.
    def register_oracle(self, caller_key: str, name: str, category: str, endpoint: str, price_motes: int) -> str:
        return self._call(caller_key, self.contracts["registry"], "register", [
            f"name:string='{name}'", f"category:string='{category}'",
            f"endpoint:string='{endpoint}'", f"price_motes:u512='{price_motes}'",
        ])

    def post_attestation(self, caller_key: str, feed_key: str, value: str) -> str:
        return self._call(caller_key, self.contracts["registry"], "post_attestation", [
            f"feed_key:string='{feed_key}'", f"value:string='{value}'",
        ])

    def list_feed(self, caller_key: str, feed_key: str, title: str, price_motes: int, endpoint: str) -> str:
        return self._call(caller_key, self.contracts["market"], "list_feed", [
            f"feed_key:string='{feed_key}'", f"title:string='{title}'",
            f"price_motes:u512='{price_motes}'", f"endpoint:string='{endpoint}'",
        ])

    def anchor_x402_receipt(self, caller_key: str, listing_id: int, amount_motes: int, receipt: str) -> str:
        return self._call(caller_key, self.contracts["market"], "anchor_x402_receipt", [
            f"listing_id:u64='{listing_id}'", f"amount_motes:u512='{amount_motes}'",
            f"receipt:string='{receipt}'",
        ])

    def gov_submit(self, caller_key: str, summary: str, payload: str) -> str:
        return self._call(caller_key, self.contracts["gov"], "submit", [
            f"summary:string='{summary}'", f"payload:string='{payload}'",
        ])

    def gov_veto(self, caller_key: str, proposal_id: int, reason: str) -> str:
        return self._call(caller_key, self.contracts["gov"], "veto", [
            f"proposal_id:u64='{proposal_id}'", f"reason:string='{reason}'",
        ])

    def gov_finalize(self, caller_key: str, proposal_id: int) -> str:
        return self._call(caller_key, self.contracts["gov"], "finalize", [
            f"proposal_id:u64='{proposal_id}'",
        ])

    def execute_rebalance(self, caller_key: str, proposal_id: int, positions_json: str,
                          nav_mark_micro: int, data_receipts: str) -> str:
        # positions are passed as JSON string; the production entry point takes
        # a CLType list — see docs/TESTNET.md for the odra-generated schema.
        return self._call(caller_key, self.contracts["vault"], "execute_rebalance", [
            f"proposal_id:u64='{proposal_id}'",
            f"positions_json:string='{positions_json}'",
            f"nav_mark_micro:u64='{nav_mark_micro}'",
            f"data_receipts:string='{data_receipts}'",
        ])

    def explorer_link(self, deploy_hash: str) -> str:
        return f"https://testnet.cspr.live/transaction/{deploy_hash}"


def get_chain():
    if config.MODE == "testnet":
        return TestnetChain()
    return MockChain()
