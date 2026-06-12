#!/usr/bin/env python3
"""Helios one-command demo.

    python3 demo.py                 # 6 rounds, then serves the dashboard
    python3 demo.py --rounds 8      # more rounds
    python3 demo.py --no-serve      # run the economy, skip the web server
    python3 demo.py --fast          # no pauses between rounds (CI / smoke test)

What it does, end to end (all offline, zero dependencies):
  1. boots the x402 Facilitator (verify/settle)
  2. onboards three RWA oracle agents — each registers an on-chain identity,
     lists its feed on the DataMarket, and serves an x402-gated endpoint
  3. boots the risk agent (on-chain veto power) and the fund agent (vault
     operator, the marketplace's first customer)
  4. runs N rounds: the fund agent buys data via x402, decides, proposes,
     survives (or not) the veto window, executes the rebalance on-chain
     — round 3 deliberately breaches risk policy so the veto fires on camera
  5. every action is written to frontend/data/feed.json; open the dashboard
     to watch the machine economy live

Set HELIOS_MODE=testnet (+ agents/testnet.env) to route writes to Casper
Testnet instead of the local ledger. See docs/TESTNET.md.
"""
from __future__ import annotations

import argparse
import functools
import http.server
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agents.common import config  # noqa: E402
from agents.common.bus import Feed  # noqa: E402
from agents.common.chain import MockChain  # noqa: E402
from agents.facilitator.server import Facilitator, serve_in_thread  # noqa: E402
from agents.fund_agent.main import FundAgent  # noqa: E402
from agents.oracle_agent.main import OracleAgent, RwaSeries  # noqa: E402
from agents.risk_agent.main import RiskAgent  # noqa: E402


def banner(text: str) -> None:
    print(f"\n\033[1;33m== {text} ==\033[0m")


def main() -> int:
    parser = argparse.ArgumentParser(description="Helios machine-economy demo")
    parser.add_argument("--rounds", type=int, default=6)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--no-serve", action="store_true")
    parser.add_argument("--fast", action="store_true")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    if args.fast:
        args.interval = 0.0

    if config.MODE == "testnet":
        print("HELIOS_MODE=testnet — demo orchestration currently drives the "
              "mock ledger; use scripts/testnet_round.py for chain writes. "
              "Falling back to mock for the local dashboard.")

    banner("Boot: ledger + x402 facilitator")
    chain = MockChain()
    feed = Feed()

    oracles = [
        OracleAgent(key="oracle_tbill", name="Beacon Rates", category="rates",
                    feed_key="us_tbill_3m", title="US 3M T-Bill yield (%)",
                    port=config.ORACLE_PORTS["tbill"], chain=chain, feed_bus=feed,
                    facilitator_url=f"http://127.0.0.1:{config.FACILITATOR_PORT}",
                    series=RwaSeries(base=5.20, drift=0.005, amplitude=0.08, seed=11)),
        OracleAgent(key="oracle_gold", name="Aurum Desk", category="commodities",
                    feed_key="xau_usd", title="Gold spot XAU/USD",
                    port=config.ORACLE_PORTS["gold"], chain=chain, feed_bus=feed,
                    facilitator_url=f"http://127.0.0.1:{config.FACILITATOR_PORT}",
                    series=RwaSeries(base=2350.0, drift=2.4, amplitude=14.0, seed=23)),
        OracleAgent(key="oracle_reindex", name="Brick&Block", category="real-estate",
                    feed_key="re_index_us", title="US tokenized RE index",
                    port=config.ORACLE_PORTS["reindex"], chain=chain, feed_bus=feed,
                    facilitator_url=f"http://127.0.0.1:{config.FACILITATOR_PORT}",
                    series=RwaSeries(base=187.0, drift=0.35, amplitude=1.8, seed=37)),
    ]

    directory = {o.wallet.address: o.wallet for o in oracles}

    banner("Onboard oracles: identity + listing (on-chain)")
    for oracle in oracles:
        oracle.onboard()
        print(f"  {oracle.name:<12} {oracle.feed_key:<13} "
              f"{oracle.price_motes // config.CSPR} CSPR/req  "
              f"endpoint :{oracle.port}")

    banner("Boot agents: risk (veto power) + fund (vault operator)")
    risk = RiskAgent(chain, feed)
    fund = FundAgent(chain, feed, risk)
    directory[fund.wallet.address] = fund.wallet
    directory[risk.wallet.address] = risk.wallet

    facilitator = Facilitator(chain, directory)
    fac_server = serve_in_thread(facilitator, config.FACILITATOR_PORT)
    for oracle in oracles:
        oracle.serve()
    time.sleep(0.3)  # let servers bind

    banner(f"Machine economy: {args.rounds} rounds")
    for round_no in range(1, args.rounds + 1):
        aggressive = (round_no == 3)
        tag = "  [aggressive — expect veto]" if aggressive else ""
        print(f"\n-- round {round_no}{tag}")
        for oracle in oracles:
            oracle.attest()
        fund.run_round(aggressive=aggressive)
        nav = feed.data["kpis"]["nav_micro"] / 1_000_000
        paid = feed.data["kpis"]["volume_motes"] / config.CSPR
        print(f"   paid {paid:.0f} CSPR via x402 total | NAV {nav:.6f} | "
              f"vetoes {feed.data['kpis']['vetoes']}")
        if args.interval:
            time.sleep(args.interval)

    banner("Summary")
    k = feed.data["kpis"]
    print(f"  x402 payments settled : {k['payments']}")
    print(f"  volume                : {k['volume_motes'] / config.CSPR:.0f} CSPR")
    print(f"  oracles / listings    : {k['oracles']} / {k['listings']}")
    print(f"  attestations          : {k['attestations']}")
    print(f"  vetoes fired          : {k['vetoes']}")
    print(f"  fund NAV              : {k['nav_micro'] / 1_000_000:.6f}")
    print(f"  on-chain deploys      : {len(chain.state['deploys'])}")
    print(f"  feed for dashboard    : {config.FEED_PATH}")

    for oracle in oracles:
        oracle.shutdown()
    fac_server.shutdown()

    if not args.no_serve:
        frontend = Path(__file__).resolve().parent / "frontend"
        handler = functools.partial(
            http.server.SimpleHTTPRequestHandler, directory=str(frontend))
        print(f"\nDashboard: http://127.0.0.1:{args.port}  (Ctrl-C to stop)")
        try:
            http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler
                                            ).serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
