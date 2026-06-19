# Helios — Testnet Deployment Guide

> Updated: 2026-06-19  
> Contracts: casper-contract v5 / casper-types v6 / wee_alloc  
> Chain: casper-test

---

## Prerequisites

```bash
# Rust wasm target
rustup target add wasm32-unknown-unknown

# Node.js ≥ 18 + casper-js-sdk
npm install

# Python 3 (for the wasm check script only)
pip install wasmtime --break-system-packages  # optional
```

---

## Step 1 — Build contracts

```bash
bash scripts/build_contracts.sh
```

This produces four files in `contracts/wasm/`:
- `OracleRegistry.wasm`
- `DataMarket.wasm`
- `FundVault.wasm`
- `Governance.wasm`

### Common build errors

| Error | Fix |
|-------|-----|
| `error[E0463]: can't find crate for 'std'` | Missing `#![no_std]` — already fixed in this version |
| `error: requires 'panic_handler'` | Missing panic handler — already in `lib.rs` |
| `Memory section should exist` | Your env has `--import-memory` in RUSTFLAGS. Run `unset RUSTFLAGS` |
| `version conflict casper-contract` | Cargo.toml now pins v5/v6 — delete `contracts/target/` and rebuild |
| `wee_alloc` not found | Run `cargo fetch` inside `contracts/` first |

---

## Step 2 — Generate keys

```bash
node scripts/deploy_helios.js keygen
```

Creates key pairs under `keys/` for: `oracle_tbill`, `oracle_gold`, `oracle_reindex`, `fund_agent`, `risk_agent`.

---

## Step 3 — Fund accounts

1. Open https://testnet.cspr.live/tools/faucet
2. For each account printed by `keygen`, request **1000 CSPR** (total need: ~2000 CSPR for all ops)

---

## Step 4 — Deploy (automated)

```bash
node scripts/deploy_helios.js deploy-all
```

This deploys all four contracts in order, wires them together, and writes `agents/testnet.env`.

---

## Step 4b — Deploy (manual via Casper Wallet web UI)

Use this if the SDK deploy fails.

### 4b.1 Install OracleRegistry

1. Go to https://testnet.cspr.live/deploy-contract
2. Select **Deploy WASM** → upload `contracts/wasm/OracleRegistry.wasm`
3. **Args**: none (admin = deployer is set automatically in `call()`)
4. **Gas**: 400,000,000,000 motes (400 CSPR)
5. Sign with Casper Wallet → submit
6. Copy the deploy hash → wait for it on https://testnet.cspr.live

### 4b.2 Install DataMarket

- WASM: `contracts/wasm/DataMarket.wasm`
- Args:
  - `registry_hash` (String) = the contract hash from step 4b.1 (without `hash-` prefix)
  - `fee_bps` (U32) = `250`
- Gas: 400 CSPR

### 4b.3 Wire OracleRegistry → DataMarket

Call entry point `set_market` on OracleRegistry:
- Contract hash: OracleRegistry hash
- Entry point: `set_market`
- Args: `market` (String) = DataMarket contract hash
- Gas: 5 CSPR

### 4b.4 Install FundVault

- WASM: `contracts/wasm/FundVault.wasm`
- Args:
  - `operator` (String) = your account hash (`account-hash-<hex>`)
  - `governance_hash` (String) = `pending` (will wire after Governance deployed)
- Gas: 400 CSPR

### 4b.5 Install Governance

- WASM: `contracts/wasm/Governance.wasm`
- Args:
  - `proposer` (String) = fund_agent account hash
  - `risk_agent` (String) = risk_agent account hash
  - `veto_window_ms` (U64) = `90000` (90 seconds for testnet demo)
- Gas: 400 CSPR

### 4b.6 Wire FundVault → Governance

Call `set_governance` on FundVault:
- Args: `governance_hash` (String) = Governance contract hash

---

## Step 5 — Run testnet transactions

```bash
export HELIOS_MODE=testnet
python3 scripts/testnet_round.py --rounds 3
```

Or call manually:

```bash
# Register an oracle
node scripts/deploy_helios.js call \
  --key keys/oracle_tbill/secret_key.pem \
  --contract <REGISTRY_HASH> \
  --entry-point register \
  --args "name:string=TBill Oracle" "category:string=rwa" \
         "endpoint:string=https://helios-oracle.example/quote" \
         "price_motes:u64=2000000000"

# List a feed
node scripts/deploy_helios.js call \
  --key keys/oracle_tbill/secret_key.pem \
  --contract <MARKET_HASH> \
  --entry-point list_feed \
  --args "feed_key:string=tbill-3m" "title:string=US T-Bill 3M" \
         "price_motes:u64=2000000000" \
         "endpoint:string=https://helios-oracle.example/quote"

# Purchase a feed
node scripts/deploy_helios.js call \
  --key keys/fund_agent/secret_key.pem \
  --contract <MARKET_HASH> \
  --entry-point purchase \
  --args "listing_id:u64=0"

# Submit a governance proposal
node scripts/deploy_helios.js call \
  --key keys/fund_agent/secret_key.pem \
  --contract <GOV_HASH> \
  --entry-point propose \
  --args "description:string=Increase T-Bill weight to 60%"

# Veto a proposal (risk_agent only, within 90s window)
node scripts/deploy_helios.js call \
  --key keys/risk_agent/secret_key.pem \
  --contract <GOV_HASH> \
  --entry-point veto \
  --args "proposal_id:u64=0"
```

---

## Troubleshooting

### `invalid body hash`

This was caused by the Python deploy script serializing the body incorrectly. **Use the JS SDK** (`deploy_helios.js`) instead — it handles serialization via the official SDK.

### `User error 1` (OracleRegistry)

Oracle not registered. Call `register` before `post_attestation`.

### `User error 2`

Unauthorized caller. Check you're using the right key for admin/operator/proposer operations.

### `User error 20` (FundVault)

Weights don't sum to 10,000 bps. The `weights_bps` arg must be comma-separated values summing to exactly 10000.

### `User error 33/34` (Governance)

Veto window timing issue. `33` = window closed, `34` = window still open (for finalize). On testnet with `veto_window_ms=90000`, wait 90 seconds before calling `finalize`.

---

## Contract hash locations

After successful deployment, contract hashes are stored in `agents/testnet.env`:

```
REGISTRY_HASH=<hex>
MARKET_HASH=<hex>
VAULT_HASH=<hex>
GOV_HASH=<hex>
```

They also appear in each deployer's named keys on-chain, accessible via:
```
https://testnet.cspr.live/account/<account-hash>
```
Look for keys: `oracle_registry_contract_hash`, `data_market_contract_hash`, etc.
