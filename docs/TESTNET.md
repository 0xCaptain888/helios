# Casper Testnet Runbook

## 0. Prerequisites

| Tool | Install |
|------|---------|
| Python 3.9+ + `cryptography` | `pip install cryptography` |
| Rust + wasm target | `rustup target add wasm32-unknown-unknown` |

No casper-client binary required.

## 1. Prepare keys

Copy Casper Wallet keys to `keys/`:

    mkdir -p keys
    cp "Account 2_secret_key.pem" keys/account2_secret_key.pem
    cp "Account 3_secret_key.pem" keys/account3_secret_key.pem
    cp "Account 4_secret_key.pem" keys/account4_secret_key.pem
    cp "Account 5_secret_key.pem" keys/account5_secret_key.pem

Verify:

    python3 scripts/casper_deploy.py pubkey --key keys/account2_secret_key.pem

## 2. Fund accounts

Faucet: https://testnet.cspr.live/tools/faucet

- Account 2 (deployer): ≥ 2000 CSPR
- Accounts 3–5 (oracles): ≥ 100 CSPR each

## 3. Build contracts

    rm -rf contracts/target/
    bash scripts/build_contracts.sh

## 4. Deploy

    python3 scripts/casper_deploy.py deploy-all \
        --key keys/account2_secret_key.pem

Auto-writes agents/testnet.env on success.

### Handle pending OracleRegistry deploy

Deploy e5ea77402381c6e087e952f9ad65a0a9862b7f462b076e4ec7716fe8dea2e379
may still be pending. Check:

    python3 scripts/casper_deploy.py wait \
        e5ea77402381c6e087e952f9ad65a0a9862b7f462b076e4ec7716fe8dea2e379

- If confirmed: extract REGISTRY_HASH from testnet.cspr.live → deploy remaining 3
- If failed: run deploy-all (deploys fresh set)

## 5. Configure agents

    cp agents/testnet.env.example agents/testnet.env
    # HASH fields filled by deploy-all automatically

## 6. Produce on-chain activity

    # One-time: register oracles + list feeds
    HELIOS_MODE=testnet python3 scripts/testnet_round.py --register --list

    # 3 rounds (round 2 triggers veto)
    HELIOS_MODE=testnet python3 scripts/testnet_round.py --rounds 3 --veto-round 2

## 7. Verify

    https://testnet.cspr.live/account/<DEPLOYER_ACCOUNT>
    # Check Named Keys: oracle_registry_contract_hash, data_market_contract_hash, etc.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `agents/testnet.env is incomplete` | Fill HASH fields after deployment |
| `Key file not found` | Check key path in testnet.env |
| `Invalid Deploy` | Use casper_deploy.py v3 (this file) |
| `User(1)` OracleRegistry | Run --register first |
| `User(20)` FundVault | weights_bps must sum to 10000 |
| `User(34)` Governance | Wait 90s before calling finalize |

---

## v4 Update (2026-06-20)

### Key Changes

1. **Signing:** ECDSA(SHA256) verified working on Casper 2.x testnet
2. **Dashboard:** New `serve_dashboard.py` for live on-chain data
3. **Frontend:** Contract address bar with cspr.live links

### Current Testnet State

**Contracts (all verified):**
- OracleRegistry: `b8b714322159b3371b4d1fe15594589a7ded2c49648d19da28c0a4fe6fb8ab58`
- DataMarket: `f35fc379fc83f8ca8de3ac6e5c2d4db749a3433fd2536151b7f0332931c0ade4`
- FundVault: `d9ee190f57aa142f28eaf4dea62231d72a0850e9e6e8332d3a0d3310fd188585`
- Governance: `140b8183c7c170b8b2bee2a7d910ae6a9482029df2e12a049d64016a04c0e655`

**Successful Operations:**
- OracleRegistry.register (Beacon Rates v4): `7a42957045c8a52ea11af1a0df162633f51dea9000555637c976d8ce4341282d` - Block 8241868 - SUCCESS

**Pending:**
- All accounts need tokens from faucet (currently 0 CSPR)
- Register remaining 3 oracles (Account 3/4/5)
- List 3 feeds on DataMarket
- Run full testnet_round.py test

### Running the Dashboard

```bash
# Mock mode (local simulation)
python3 scripts/serve_dashboard.py

# Testnet mode (live on-chain data)
HELIOS_MODE=testnet python3 scripts/serve_dashboard.py

# Open http://127.0.0.1:8080
```

### v4 Troubleshooting

| Symptom | Fix |
|---------|-----|
| `Invalid Deploy` | Use casper_deploy.py v4 (ECDSA+SHA256 verified) |
| `Invalid transaction` | Check account balance, use account_put_deploy |
| Account balance 0 | Request tokens from https://testnet.cspr.live/tools/faucet |
| Dashboard not showing contracts | Ensure testnet.env has all 4 contract hashes |

---

*v4 update · 2026-06-20*
