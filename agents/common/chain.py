"""Chain adapter — MockChain (local) and TestnetChain (Casper Testnet).

TestnetChain uses casper_deploy.py directly (pure Python, no casper-client).
Supports secp256k1 Casper Wallet keys. Entry-point names match Rust contracts.
"""

from __future__ import annotations
import hashlib, json, sys, threading, time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from . import config

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

_LOCK = threading.RLock()


def _h(payload):
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
    """Local ledger mirroring Helios contract semantics."""

    def __init__(self, path=None):
        self.path = path or config.LEDGER_PATH
        self.state = {
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
            "gov": {
                "proposals": [],
                "veto_window_ms": int(config.VETO_WINDOW_SECONDS * 1000),
            },
            "attestations": [],
        }
        config.ensure_dirs()
        self._persist()

    def create_account(self, name, balance_motes, address=None):
        addr = address or ("account-hash-" + _h({"acct": name})[:56])
        with _LOCK:
            self.state["accounts"][addr] = {"name": name, "balance": int(balance_motes)}
            self._persist()
        return addr

    def balance(self, addr):
        return int(self.state["accounts"].get(addr, {}).get("balance", 0))

    def transfer(self, frm, to, amount, memo=""):
        with _LOCK:
            acc = self.state["accounts"]
            if acc.get(frm, {}).get("balance", 0) < amount:
                raise ValueError(f"insufficient funds: {frm}")
            acc[frm]["balance"] -= amount
            acc.setdefault(to, {"name": to[:14], "balance": 0})
            acc[to]["balance"] += amount
            return self._deploy(
                "transfer", frm, {"to": to, "amount": amount, "memo": memo}
            )

    def register_oracle(self, caller, name, category, endpoint, price_motes):
        with _LOCK:
            reg = self.state["registry"]
            if caller in reg["oracles"]:
                raise ValueError("AlreadyRegistered")
            reg["oracles"][caller] = {
                "name": name,
                "category": category,
                "endpoint": endpoint,
                "price_motes": price_motes,
                "active": True,
                "reputation": {
                    "settlements": 0,
                    "attestations": 0,
                    "accurate": 0,
                    "disputed": 0,
                    "score_bps": 5000,
                },
            }
            reg["order"].append(caller)
            return self._deploy(
                "registry.register",
                caller,
                {"name": name, "category": category, "endpoint": endpoint},
            )

    def post_attestation(self, caller, feed_key, value):
        with _LOCK:
            oracle = self.state["registry"]["oracles"].get(caller)
            if not oracle:
                raise ValueError("NotRegistered")
            oracle["reputation"]["attestations"] += 1
            rec = {
                "oracle": caller,
                "feed_key": feed_key,
                "value": value,
                "ts": time.time(),
            }
            self.state["attestations"].append(rec)
            return self._deploy("registry.post_attestation", caller, rec)

    def _credit_settlement(self, oracle_addr):
        rep = self.state["registry"]["oracles"][oracle_addr]["reputation"]
        rep["settlements"] += 1
        scored = rep["accurate"] + rep["disputed"]
        accuracy = 5000 if scored == 0 else rep["accurate"] * 10_000 // scored
        activity = min(rep["settlements"], 100)
        weight = 2_000 + (10_000 - 2_000) * activity // 100
        rep["score_bps"] = accuracy * weight // 10_000

    def list_feed(self, caller, feed_key, title, price_motes, endpoint):
        with _LOCK:
            market = self.state["market"]
            if any(l["feed_key"] == feed_key for l in market["listings"]):
                raise ValueError("ListingExists")
            lid = len(market["listings"])
            market["listings"].append(
                {
                    "id": lid,
                    "oracle": caller,
                    "feed_key": feed_key,
                    "title": title,
                    "price_motes": price_motes,
                    "endpoint": endpoint,
                    "active": True,
                    "sales": 0,
                    "revenue_motes": 0,
                }
            )
            self._deploy(
                "market.list_feed", caller, {"feed_key": feed_key, "price": price_motes}
            )
            return lid

    def anchor_x402_receipt(self, caller, listing_id, amount_motes, receipt):
        with _LOCK:
            listing = self.state["market"]["listings"][listing_id]
            listing["sales"] += 1
            listing["revenue_motes"] += amount_motes
            self._credit_settlement(listing["oracle"])
            return self._deploy(
                "market.anchor_x402_receipt",
                caller,
                {"listing_id": listing_id, "amount": amount_motes, "receipt": receipt},
            )

    def vault_set_operator(self, operator):
        with _LOCK:
            self.state["vault"]["operator"] = operator
            self._persist()

    def vault_deposit(self, caller, amount):
        with _LOCK:
            self.state["accounts"][caller]["balance"] -= amount
            self.state["vault"]["deposits"] += amount
            return self._deploy("vault.deposit", caller, {"amount": amount})

    def execute_rebalance(
        self, caller, proposal_id, positions, nav_mark_micro, data_receipts
    ):
        with _LOCK:
            vault = self.state["vault"]
            if caller != vault["operator"]:
                raise ValueError("NotAuthorized")
            if sum(p["weight_bps"] for p in positions) != 10_000:
                raise ValueError("BadWeights")
            vault["positions"] = positions
            vault["nav_micro"] = nav_mark_micro
            vault["history"].append(
                {
                    "proposal_id": proposal_id,
                    "positions": positions,
                    "nav_mark_micro": nav_mark_micro,
                    "data_receipts": data_receipts,
                    "ts": time.time(),
                }
            )
            return self._deploy(
                "vault.execute_rebalance",
                caller,
                {"proposal_id": proposal_id, "nav": nav_mark_micro},
            )

    def gov_submit(self, caller, summary, payload):
        with _LOCK:
            proposals = self.state["gov"]["proposals"]
            pid = len(proposals)
            proposals.append(
                {
                    "id": pid,
                    "proposer": caller,
                    "summary": summary,
                    "payload": payload,
                    "created_at": time.time(),
                    "status": "pending",
                    "veto_reason": "",
                }
            )
            deploy = self._deploy(
                "gov.propose", caller, {"id": pid, "summary": summary}
            )
            return pid, deploy

    def gov_veto(self, caller, proposal_id, reason):
        with _LOCK:
            p = self.state["gov"]["proposals"][proposal_id]
            if p["status"] != "pending":
                raise ValueError("AlreadyFinal")
            p["status"] = "vetoed"
            p["veto_reason"] = reason
            return self._deploy(
                "gov.veto", caller, {"id": proposal_id, "reason": reason}
            )

    def gov_finalize(self, caller, proposal_id):
        with _LOCK:
            p = self.state["gov"]["proposals"][proposal_id]
            if p["status"] != "pending":
                raise ValueError("AlreadyFinal")
            window = self.state["gov"]["veto_window_ms"] / 1000.0
            if time.time() <= p["created_at"] + window:
                raise ValueError("WindowOpen")
            p["status"] = "approved"
            return self._deploy("gov.finalize", caller, {"id": proposal_id})

    def _deploy(self, kind, caller, args):
        record = {
            "kind": kind,
            "caller": caller,
            "args": args,
            "ts": time.time(),
            "nonce": len(self.state["deploys"]),
        }
        dh = _h(record)
        self.state["deploys"].append({"hash": dh, **record})
        self._persist()
        return dh

    def _persist(self):
        self.path.write_text(
            json.dumps(self.state, indent=1, default=str), encoding="utf-8"
        )

    def explorer_link(self, deploy_hash):
        return f"local://ledger/{deploy_hash[:16]}"


class TestnetChain:
    """Casper Testnet — pure Python via casper_deploy.py, no casper-client."""

    EXPLORER = "https://testnet.cspr.live"

    def __init__(self):
        from casper_deploy import (
            CasperKey,
            call_contract,
            install_wasm,
            wait_for_deploy,
        )

        self._CasperKey = CasperKey
        self._call_contract = call_contract
        self._install_wasm = install_wasm
        self._wait = wait_for_deploy

        env = config.load_testnet_env()
        self.key_paths = {
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
        missing_k = [k for k, v in self.key_paths.items() if not v]
        missing_c = [k for k, v in self.contracts.items() if not v]
        if missing_k or missing_c:
            raise RuntimeError(
                "agents/testnet.env is incomplete.\n"
                + (f"  Missing keys: {missing_k}\n" if missing_k else "")
                + (f"  Missing hashes: {missing_c}\n" if missing_c else "")
                + "  See docs/TESTNET.md"
            )
        self._key_cache = {}
        self._proposal_count = 0

        # Local state mirror for agent read paths
        self.state = {
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
            "gov": {"proposals": [], "veto_window_ms": 90_000},
            "attestations": [],
        }

    def _key(self, role):
        if role not in self._key_cache:
            path = self.key_paths[role]
            if not Path(path).exists():
                raise FileNotFoundError(f"Key not found for {role}: {path}")
            self._key_cache[role] = self._CasperKey.load(path)
        return self._key_cache[role]

    def _call(
        self, role, contract, entry_point, args, payment=5_000_000_000, wait=True
    ):
        key = self._key(role)
        dh = self._call_contract(
            key, self.contracts[contract], entry_point, args, payment
        )
        if wait:
            self._wait(dh)
        return dh

    # OracleRegistry
    def register_oracle(self, role, name, category, endpoint, price_motes):
        from casper_deploy import arg_s, arg_u64

        return self._call(
            role,
            "registry",
            "register",
            [
                arg_s("name", name),
                arg_s("category", category),
                arg_s("endpoint", endpoint),
                arg_u64("price_motes", price_motes),
            ],
        )

    def post_attestation(self, role, feed_key, value):
        from casper_deploy import arg_s

        return self._call(
            role,
            "registry",
            "post_attestation",
            [arg_s("feed_key", feed_key), arg_s("value", value)],
        )

    def set_market(self, role, market_hash):
        from casper_deploy import arg_s

        return self._call(
            role, "registry", "set_market", [arg_s("market", market_hash)]
        )

    # DataMarket
    def list_feed(self, role, feed_key, title, price_motes, endpoint):
        from casper_deploy import arg_s, arg_u64

        return self._call(
            role,
            "market",
            "list_feed",
            [
                arg_s("feed_key", feed_key),
                arg_s("title", title),
                arg_u64("price_motes", price_motes),
                arg_s("endpoint", endpoint),
            ],
        )

    def anchor_x402_receipt(self, role, listing_id, amount_motes, receipt_hash):
        from casper_deploy import arg_s, arg_u64

        oracle_acct = self._key(role).account_hash()
        return self._call(
            role,
            "market",
            "anchor_x402_receipt",
            [
                arg_u64("listing_id", listing_id),
                arg_s("oracle", oracle_acct),
                arg_u64("amount_motes", amount_motes),
                arg_s("receipt_hash", receipt_hash),
            ],
        )

    # FundVault
    def vault_deposit(self, role, amount):
        from casper_deploy import arg_u64

        return self._call(role, "vault", "deposit", [arg_u64("amount", amount)])

    def execute_rebalance(
        self, role, proposal_id, positions, nav_mark_micro, data_receipts
    ):
        from casper_deploy import arg_s, arg_u64

        targets = ",".join(p["asset"] for p in positions)
        weights_bps = ",".join(str(p["weight_bps"]) for p in positions)
        return self._call(
            role,
            "vault",
            "execute_rebalance",
            [
                arg_u64("proposal_id", proposal_id),
                arg_s("targets", targets),
                arg_s("weights_bps", weights_bps),
            ],
        )

    def record_nav(self, role, nav_motes, yield_bps=0):
        from casper_deploy import arg_u64, arg_u32

        return self._call(
            role,
            "vault",
            "record_nav",
            [arg_u64("nav_motes", nav_motes), arg_u32("yield_bps", yield_bps)],
        )

    # Governance
    def gov_submit(self, role, summary, payload):
        from casper_deploy import arg_s

        dh = self._call(role, "gov", "propose", [arg_s("description", summary)])
        pid = self._proposal_count
        self._proposal_count += 1
        return pid, dh

    def gov_veto(self, role, proposal_id, reason=""):
        from casper_deploy import arg_u64

        return self._call(role, "gov", "veto", [arg_u64("proposal_id", proposal_id)])

    def gov_finalize(self, role, proposal_id):
        from casper_deploy import arg_u64

        return self._call(
            role, "gov", "finalize", [arg_u64("proposal_id", proposal_id)]
        )

    def explorer_link(self, deploy_hash):
        return f"{self.EXPLORER}/deploy/{deploy_hash}"


def get_chain():
    if config.MODE == "testnet":
        return TestnetChain()
    return MockChain()
