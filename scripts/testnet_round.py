#!/usr/bin/env python3
"""Drive real Casper Testnet transactions for Helios.

Usage:
  HELIOS_MODE=testnet python3 scripts/testnet_round.py --register
  HELIOS_MODE=testnet python3 scripts/testnet_round.py --list
  HELIOS_MODE=testnet python3 scripts/testnet_round.py --rounds 3
  HELIOS_MODE=testnet python3 scripts/testnet_round.py --register --list --rounds 3
  HELIOS_MODE=testnet python3 scripts/testnet_round.py --rounds 3 --veto-round 2
"""

from __future__ import annotations
import argparse, json, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.common import config
from agents.common.chain import TestnetChain

ORACLES = [
    (
        "oracle_tbill",
        "Beacon Rates",
        "rates",
        "us_tbill_3m",
        "US 3M T-Bill yield (%)",
        2 * config.CSPR,
        5.31,
    ),
    (
        "oracle_gold",
        "Aurum Desk",
        "commodities",
        "xau_usd",
        "Gold spot XAU/USD",
        3 * config.CSPR,
        2384.20,
    ),
    (
        "oracle_reindex",
        "Brick&Block",
        "real-estate",
        "re_index_us",
        "US tokenized RE index",
        5 * config.CSPR,
        189.70,
    ),
]

POSITIONS = [
    {"asset": "CSPR", "weight_bps": 1500},
    {"asset": "US_TBILL_3M", "weight_bps": 4000},
    {"asset": "XAU", "weight_bps": 2500},
    {"asset": "RE_INDEX_US", "weight_bps": 2000},
]
assert sum(p["weight_bps"] for p in POSITIONS) == 10_000


def show(label, dh, chain):
    print(f"  ✓ {label:<36} {dh[:16]}…")
    print(f"    {chain.explorer_link(dh)}")


def sleep(seconds, label=""):
    tag = f" ({label})" if label else ""
    print(f"  ⏳ waiting {seconds:.0f}s{tag}…", flush=True)
    time.sleep(seconds)


def step_register(chain):
    print("\n== Step: register oracle identities ==")
    for role, name, cat, feed, _title, price, _ in ORACLES:
        dh = chain.register_oracle(
            role, name, cat, f"https://helios-demo.example/{feed}", price
        )
        show(f"OracleRegistry.register [{name}]", dh, chain)
        sleep(3, "nonce gap")


def step_list_feeds(chain):
    print("\n== Step: list feeds on DataMarket ==")
    for role, name, _cat, feed, title, price, _ in ORACLES:
        dh = chain.list_feed(
            role, feed, title, price, f"https://helios-demo.example/{feed}"
        )
        show(f"DataMarket.list_feed [{feed}]", dh, chain)
        sleep(3, "nonce gap")


def step_round(chain, round_no, veto=False):
    print(f"\n== Round {round_no}{' [VETO]' if veto else ''} ==")

    receipts = []
    for i, (role, name, _cat, feed, _title, price, base_val) in enumerate(ORACLES):
        value = round(base_val * (1 + 0.001 * round_no), 4)
        dh_att = chain.post_attestation(role, feed, str(value))
        show(f"OracleRegistry.post_attestation [{feed}]", dh_att, chain)
        receipt_id = f"x402:r{round_no}:{feed}"
        dh_anc = chain.anchor_x402_receipt(role, i, price, receipt_id)
        show(f"DataMarket.anchor_x402_receipt [{feed}]", dh_anc, chain)
        receipts.append(dh_anc)
        sleep(3, "nonce gap")

    summary = f"Round {round_no}: CSPR 15% TBILL 40% XAU 25% RE 20%" + (
        " — VETO TEST" if veto else ""
    )
    pid, dh_prop = chain.gov_submit(
        "fund_agent",
        summary,
        json.dumps(
            {
                "positions": POSITIONS,
                "receipts": receipts,
            }
        ),
    )
    show(f"Governance.propose [id={pid}]", dh_prop, chain)

    if veto:
        sleep(5, "let proposal settle")
        dh_veto = chain.gov_veto("risk_agent", pid, "Test veto")
        show(f"Governance.veto [id={pid}]", dh_veto, chain)
        print(f"  → Proposal {pid} vetoed ✓")
    else:
        sleep(95, f"veto window proposal {pid}")
        dh_fin = chain.gov_finalize("fund_agent", pid)
        show(f"Governance.finalize [id={pid}]", dh_fin, chain)
        nav_motes = (1_000_000 + round_no * 1_500) * config.CSPR // 1_000_000
        dh_reb = chain.execute_rebalance(
            "fund_agent", pid, POSITIONS, nav_motes, ",".join(receipts)
        )
        show("FundVault.execute_rebalance", dh_reb, chain)
        dh_nav = chain.record_nav("fund_agent", nav_motes, yield_bps=50)
        show("FundVault.record_nav", dh_nav, chain)


def main():
    parser = argparse.ArgumentParser(description="Helios testnet transaction driver")
    parser.add_argument("--register", action="store_true")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--rounds", type=int, default=0)
    parser.add_argument("--veto-round", type=int, default=None, metavar="N")
    args = parser.parse_args()

    if config.MODE != "testnet":
        print("ERROR: export HELIOS_MODE=testnet")
        return 1
    if not any([args.register, args.list, args.rounds > 0]):
        parser.print_help()
        return 0

    try:
        chain = TestnetChain()
    except (RuntimeError, FileNotFoundError) as exc:
        print(f"ERROR: {exc}")
        return 1

    print("Connected. Contracts:")
    for name, h in chain.contracts.items():
        print(f"  {name:<10} {h}")

    if args.register:
        step_register(chain)
    if args.list:
        step_list_feeds(chain)
    for r in range(1, args.rounds + 1):
        step_round(chain, r, veto=(args.veto_round == r))

    print("\nDone. Paste cspr.live links into your submission.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
