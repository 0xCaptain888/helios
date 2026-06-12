#!/usr/bin/env python3
"""Drive real Casper Testnet transactions for Helios.

Usage:
  HELIOS_MODE=testnet python3 scripts/testnet_round.py --register --list
  HELIOS_MODE=testnet python3 scripts/testnet_round.py --rounds 3

Requires agents/testnet.env (keys + contract hashes) — see docs/TESTNET.md.
Every action prints the resulting transaction hash with a testnet.cspr.live
link, ready to paste into the submission.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.common import config  # noqa: E402
from agents.common.chain import TestnetChain  # noqa: E402

ORACLES = [
    ("oracle_tbill", "Beacon Rates", "rates", "us_tbill_3m",
     "US 3M T-Bill yield (%)", config.PRICES_MOTES["us_tbill_3m"], 5.31),
    ("oracle_gold", "Aurum Desk", "commodities", "xau_usd",
     "Gold spot XAU/USD", config.PRICES_MOTES["xau_usd"], 2384.2),
    ("oracle_reindex", "Brick&Block", "real-estate", "re_index_us",
     "US tokenized RE index", config.PRICES_MOTES["re_index_us"], 189.7),
]


def show(label: str, tx: str, chain: TestnetChain) -> None:
    print(f"  {label:<28} {tx}")
    print(f"  {'':<28} {chain.explorer_link(tx)}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--register", action="store_true", help="register oracle identities")
    parser.add_argument("--list", action="store_true", help="list feeds on the DataMarket")
    parser.add_argument("--rounds", type=int, default=0, help="run N attest+anchor+governance rounds")
    args = parser.parse_args()

    if config.MODE != "testnet":
        print("Set HELIOS_MODE=testnet to use this script (see docs/TESTNET.md)")
        return 1

    chain = TestnetChain()

    if args.register:
        print("== register oracle identities ==")
        for key_name, name, cat, feed, title, price, _v in ORACLES:
            tx = chain.register_oracle(chain.keys[key_name], name, cat,
                                       f"https://helios.example/{feed}", price)
            show(f"registry.register {name}", tx, chain)
            time.sleep(2)

    if args.list:
        print("== list feeds on DataMarket ==")
        for key_name, name, cat, feed, title, price, _v in ORACLES:
            tx = chain.list_feed(chain.keys[key_name], feed, title, price,
                                 f"https://helios.example/{feed}")
            show(f"market.list_feed {feed}", tx, chain)
            time.sleep(2)

    for round_no in range(1, args.rounds + 1):
        print(f"== round {round_no} ==")
        receipts = []
        for i, (key_name, name, cat, feed, title, price, base) in enumerate(ORACLES):
            value = round(base * (1 + 0.001 * round_no), 4)
            tx = chain.post_attestation(chain.keys[key_name], feed, str(value))
            show(f"registry.post_attestation {feed}", tx, chain)
            tx2 = chain.anchor_x402_receipt(chain.keys[key_name], i, price,
                                            f"x402:round{round_no}:{feed}")
            show(f"market.anchor_x402_receipt {feed}", tx2, chain)
            receipts.append(tx2)
            time.sleep(2)

        positions = [
            {"asset": "CSPR", "weight_bps": 1500},
            {"asset": "US_TBILL_3M", "weight_bps": 4000},
            {"asset": "XAU", "weight_bps": 2500},
            {"asset": "RE_INDEX_US", "weight_bps": 2000},
        ]
        payload = json.dumps({"positions": positions, "data_receipts": receipts,
                              "max_data_age_seconds": 5})
        summary = f"Testnet round {round_no}: CSPR 15% TBILL 40% XAU 25% RE 20%"
        tx = chain.gov_submit(chain.keys["fund_agent"], summary, payload)
        show("gov.submit", tx, chain)
        proposal_id = round_no - 1  # adjust if governance already has proposals
        print("  waiting out the veto window (65s)...")
        time.sleep(65)
        tx = chain.gov_finalize(chain.keys["fund_agent"], proposal_id)
        show("gov.finalize", tx, chain)
        tx = chain.execute_rebalance(chain.keys["fund_agent"], proposal_id,
                                     json.dumps(positions), 1_000_000 + round_no * 1500,
                                     ",".join(receipts))
        show("vault.execute_rebalance", tx, chain)

    print("\nDone. Paste the cspr.live links above into the submission.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
