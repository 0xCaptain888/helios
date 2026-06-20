# Helios — RWA Data Exchange for the Machine Economy

**The first marketplace on Casper where AI agents sell, buy, and build reputation on real-world-asset data — settled per-request with x402 micropayments.**

Built for the **Casper Agentic Buildathon 2026**.

---

## Current Status

| Component | Status |
|-----------|--------|
| Smart Contracts (4x) | `#![no_std]` + casper-contract v5 + casper-types v6 — compiled to WASM |
| Deploy Script | Pure Python, secp256k1 + ed25519 support, CLType serialization fixed |
| WASM Binaries | OracleRegistry (65KB), DataMarket (69KB), FundVault (60KB), Governance (61KB) |
| Testnet Wallets | 5x secp256k1 accounts ready |
| Testnet Deployment | Ready — requires CSPR faucet funding |

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
| `contracts/` | Four native Casper smart contracts (Rust `#![no_std]`, casper-contract v5 / casper-types v6) + WASM binaries |
| `contracts/wasm/` | Pre-built WASM binaries ready for testnet deployment |
| `agents/` | Oracle / fund / risk agents + x402 facilitator (pure Python stdlib) |
| `frontend/` | Zero-build dashboard (vanilla HTML/CSS/JS) |
| `scripts/` | Testnet deployment (`casper_deploy.py`) + build scripts |
| `docs/` | Architecture, deployment guide, fix report |
| `demo.py` | One-command local machine economy |

## Smart contracts

Built natively with `cargo` on **casper-contract v5** and **casper-types v6**, requiring **zero Odra toolchain dependencies**. All contracts use `#![no_std]` with `wee_alloc` for the Casper VM.

```bash
cd contracts
cargo test                          # unit tests (host build)

# Build all 4 contracts to WASM (with feature flags)
bash ../scripts/build_contracts.sh

# Pre-deploy 3-layer gate: verifies `call` export, internal memory section, 
# and all Phase 2 entry-point handlers exist in the WASM export table.
python3 ../scripts/check_wasm_exports.py wasm/*.wasm
```

### Testnet deployment

Pure Python deploy script — no `casper-client` or Rust toolchain needed. Supports both **secp256k1** (Casper Wallet) and **ed25519** keys.

```bash
# Check key info
python3 scripts/casper_deploy.py pubkey --key "Account 1_secret_key.pem"

# One-click deploy all 4 contracts + wiring
python3 scripts/casper_deploy.py deploy-all --key "Account 1_secret_key.pem"

# Or deploy individually
python3 scripts/casper_deploy.py install --key "Account 1_secret_key.pem" \
    --wasm contracts/wasm/OracleRegistry.wasm --wait

# Call contract entry-points
python3 scripts/casper_deploy.py call --key "Account 1_secret_key.pem" \
    --contract <HASH> --entry-point register \
    --args "name:string=TBill Oracle" "category:string=rwa" \
           "endpoint:string=https://helios.example/quote" "price_motes:u64=2000000000" \
    --wait
```

Full deployment guide: [`docs/DEPLOYMENT_GUIDE.md`](docs/DEPLOYMENT_GUIDE.md)

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
