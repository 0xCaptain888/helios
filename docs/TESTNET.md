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
