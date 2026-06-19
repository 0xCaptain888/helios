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

**Status**: Contracts successfully built and ready for deployment (casper-contract v5 / casper-types v6 / no_std).

```bash
# Build all four contracts to WASM
bash scripts/build_contracts.sh

# Produces four WASM files in contracts/wasm/:
# - OracleRegistry.wasm (exports: call, register, post_attestation, credit_settlement, score_attestation, set_market, get_oracle, get_reputation)
# - DataMarket.wasm (exports: call, list_feed, purchase, anchor_x402_receipt, set_fee_bps, get_listing, listing_count)
# - FundVault.wasm (exports: call, deposit, execute_rebalance, record_nav, get_nav, set_governance)
# - Governance.wasm (exports: call, propose, veto, finalize, get_proposal, proposal_count)
```

### Contract versions
- `casper-contract` v5.1.1
- `casper-types` v6.1.0
- `wee_alloc` for no_std heap allocation
- All contracts use `#![no_std]` with `EntityEntryPoint` API (Casper 2.x compatible)

### Deployment to Casper Testnet

See [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) for full deployment guide.

```bash
# Install dependencies
npm install casper-js-sdk

# Deploy all contracts
node scripts/deploy_helios.js deploy-all

# Or deploy manually
node scripts/deploy_helios.js install --key keys/fund_agent/secret_key.pem --wasm contracts/wasm/OracleRegistry.wasm
```

**Testnet Node**: https://node.testnet.casper.network/rpc  
**Explorer**: https://testnet.cspr.live

### Current deployment status

- ✅ Contracts compile successfully with casper-contract v5 / casper-types v6
- ✅ All WASM exports verified (call + entry points + memory section)
- ✅ Wallet keys configured (5 accounts, all funded with test CSPR)
- 🔄 Deployment in progress

**Account balances** (as of 2026-06-19):
- oracle_tbill: 1100 CSPR
- oracle_gold: 5000 CSPR
- oracle_reindex: 5000 CSPR
- fund_agent: 5000 CSPR (deployer)
- risk_agent: 5000 CSPR

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
