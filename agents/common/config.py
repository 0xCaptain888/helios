"""Helios shared configuration.

Two run modes, switched by env var HELIOS_MODE:
  * mock     (default) — a local ledger simulates the Casper chain so the whole
               machine economy runs end-to-end offline. Every state change is
               recorded as a pseudo-deploy with a deterministic hash.
  * testnet  — chain writes go through `casper-client` against Casper Testnet.
               Requires keys + deployed contract hashes in agents/testnet.env
               (see docs and 03_资料准备清单).
"""
from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "agents" / "_state"
FEED_PATH = REPO_ROOT / "frontend" / "data" / "feed.json"
LEDGER_PATH = DATA_DIR / "ledger.json"
TESTNET_ENV = REPO_ROOT / "agents" / "testnet.env"

MODE = os.environ.get("HELIOS_MODE", "mock").strip().lower()
NETWORK = "casper-test" if MODE != "mock" else "helios-localsim"

FACILITATOR_PORT = int(os.environ.get("HELIOS_FACILITATOR_PORT", "8402"))
ORACLE_PORTS = {
    "tbill": int(os.environ.get("HELIOS_ORACLE_TBILL_PORT", "8451")),
    "gold": int(os.environ.get("HELIOS_ORACLE_GOLD_PORT", "8452")),
    "reindex": int(os.environ.get("HELIOS_ORACLE_RE_PORT", "8453")),
}

# Demo economics (motes; 1 CSPR = 1_000_000_000 motes)
CSPR = 1_000_000_000
PRICES_MOTES = {
    "us_tbill_3m": 2 * CSPR,      # 2 CSPR per quote
    "xau_usd": 3 * CSPR,          # 3 CSPR per quote
    "re_index_us": 5 * CSPR,      # 5 CSPR per quote
}
MARKET_FEE_BPS = 250  # 2.5% protocol fee, mirrors DataMarket contract

VETO_WINDOW_SECONDS = float(os.environ.get("HELIOS_VETO_WINDOW", "1.5"))

# Risk policy enforced by the risk agent (mirrors the demo narrative)
RISK_MAX_SINGLE_RWA_BPS = 4_500   # no single RWA position above 45%
RISK_MIN_CASH_BPS = 1_000         # keep at least 10% in CSPR
RISK_MAX_DATA_AGE_SECONDS = 120.0


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    FEED_PATH.parent.mkdir(parents=True, exist_ok=True)


def load_testnet_env() -> dict:
    """Parse agents/testnet.env (KEY=VALUE lines) for testnet mode."""
    values: dict[str, str] = {}
    if TESTNET_ENV.exists():
        for line in TESTNET_ENV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip()
    return values
