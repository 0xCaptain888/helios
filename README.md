# Helios — RWA Data Exchange for the Machine Economy

**The first marketplace on Casper where AI agents sell, buy, and build reputation on real-world-asset data — settled per-request with x402 micropayments.**

Built for the **Casper Agentic Buildathon 2026**.

---

## The problem

Every serious DeFi or RWA protocol needs reliable real-world data. Today that means trusting a handful of monolithic oracle networks — expensive, subscription-based, and built for humans signing contracts, not for autonomous agents paying per request.

Meanwhile, AI agents are becoming first-class economic actors. They can fetch, verify, and price data far better than any cron job. What they lack is a **venue**: a place to sell their data services, get paid machine-to-machine, and accumulate verifiable trust.

## What Helios is

Helios is that venue — three composable layers, all on Casper:

1. **Oracle Registry** (`contracts/src/oracle_registry.rs`) — any agent registers an on-chain identity for its data service: category, endpoint, price. Reputation is **earned, not claimed**: every settled sale and every posted attestation updates an on-chain score.
2. **Data Market** (`contracts/src/data_market.rs`) — feeds are listed and sold per-request. Two settlement paths, both leaving on-chain receipts:
   - fully on-chain `purchase` (payable, protocol fee, instant payout), or
   - **x402 micropayments** over HTTP, with the facilitator's settlement hash anchored on-chain via `anchor_x402_receipt` — so off-band sales still grow reputation.
3. **First customer built in** — an autonomous fund (`fund_vault.rs` + `governance.rs`) that *buys its own data on the exchange*, decides, and rebalances — with a **risk agent holding on-chain veto power** over every proposal. Autonomous, never unchecked.

The flywheel: more sales → higher on-chain reputation → more buyers → more sales. Reputation becomes revenue.

## Quick start (60 seconds, zero dependencies)

```bash
python3 demo.py            # pure stdlib — no pip install
# open http://127.0.0.1:8080
```

This boots the full machine economy locally: an x402 facilitator, three oracle agents (T-Bill yields, gold, tokenized real estate), a risk agent, and the fund agent. Six rounds of: x402-paid data purchases → allocation decisions → governance proposals → veto checks → on-chain rebalances. Round 3 deliberately breaches risk policy so you can watch the veto fire.

Everything streams to the dashboard: settlement tape, reputation meters, NAV, decision rationales, veto reasons, and a full transaction log.

## Architecture

```
 ┌────────────┐   x402 (HTTP 402 → pay → data)   ┌────────────┐
 │ Fund agent │ ────────────────────────────────▶ │ Oracle     │
 │ (consumer) │ ◀──────── signed data ─────────── │ agents (3) │
 └─────┬──────┘                                   └─────┬──────┘
       │ propose                 anchor receipts +      │ register,
       ▼                         credit settlements     ▼ attest
 ┌────────────┐  veto window  ┌────────────┐    ┌──────────────┐
 │ Governance │◀──────────────│ Risk agent │    │OracleRegistry│
 └─────┬──────┘               └────────────┘    └──────▲───────┘
       │ approved                                      │ reputation
       ▼                                               │
 ┌────────────┐               ┌────────────┐           │
 │ FundVault  │               │ DataMarket │───────────┘
 └────────────┘               └────────────┘
```

Full details in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Repository layout

| Path | What |
|---|---|
| `contracts/` | Four Odra (Rust) smart contracts + unit tests |
| `agents/` | Oracle / fund / risk agents + x402 facilitator (pure Python stdlib) |
| `frontend/` | Zero-build dashboard (vanilla HTML/CSS/JS) |
| `scripts/` | Testnet deployment + round driver |
| `docs/` | Architecture & Casper Testnet runbook |
| `demo.py` | One-command local machine economy |

## Smart contracts

```bash
cd contracts
cargo install cargo-odra --locked   # once
cargo odra test                     # 12 unit tests, OdraVM
cargo odra test -b casper           # against the Casper backend VM
cargo odra build                    # produces WASM in wasm/
```

Deployment to Casper Testnet: [`docs/TESTNET.md`](docs/TESTNET.md) and `scripts/deploy_contracts.sh`.

## Modes

| Mode | Switch | What happens |
|---|---|---|
| `mock` (default) | — | Local deterministic ledger mirrors the contracts so the whole economy runs offline; every action gets a pseudo deploy hash |
| `testnet` | `HELIOS_MODE=testnet` + `agents/testnet.env` | Writes go through `casper-client` to the deployed contracts on Casper Testnet; receipts link to testnet.cspr.live |

Optional: `HELIOS_USE_LLM=1` + `ANTHROPIC_API_KEY` makes the fund agent write its allocation rationales with Claude.

## x402 in one breath

`GET /quote` → `402` + payment requirements → agent signs payment authorization → retry with `X-PAYMENT` header → facilitator `/verify` + `/settle` → `200` + data + `X-PAYMENT-RESPONSE` settlement receipt → receipt anchored on-chain → oracle reputation grows. See `agents/common/x402.py`.

## Why this matters for Casper

- **Infrastructure, not a demo**: every future RWA/DeFi project on Casper needs data; Helios gives any data provider a one-afternoon path to selling it.
- **The natural x402 showcase**: per-request data is the killer use case for micropayments — subscriptions are exactly what x402 makes unnecessary.
- **Trust as a primitive**: on-chain reputation earned through settlements is reusable by any protocol that needs to rank oracles.

## License

Apache-2.0 — see [LICENSE](LICENSE).
