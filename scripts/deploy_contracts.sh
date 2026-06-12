#!/usr/bin/env bash
# Deploy the four Helios contracts to Casper Testnet, in dependency order.
# Prereqs: cargo odra build done; casper-client 2.x installed; deployer key funded.
# Usage: ./scripts/deploy_contracts.sh keys/fund_agent/secret_key.pem
set -euo pipefail

KEY="${1:?usage: deploy_contracts.sh <deployer-secret-key.pem>}"
NODE="https://node.testnet.casper.network/rpc"
CHAIN="casper-test"
PAY="300000000000"   # 300 CSPR per install; raise if Out of gas
WASM_DIR="$(dirname "$0")/../contracts/wasm"

# HARD GATE: refuse to deploy any wasm that lacks the `call` export.
# A wasm exporting only `main` was built from a [[bin]] target and will fail
# on-chain with "Module doesn't have export call" (wasting gas + time).
echo "== pre-deploy gate: verifying wasm exports =="
python3 "$(dirname "$0")/check_wasm_exports.py" "${WASM_DIR}"/*.wasm || {
  echo "!! wasm verification FAILED — fix the build first (docs/TESTNET.md troubleshooting #3)" >&2
  exit 1
}
echo

deploy () {
  local wasm="$1"
  echo "-- deploying ${wasm}" >&2
  casper-client put-txn session \
    --node-address "$NODE" \
    --chain-name "$CHAIN" \
    --secret-key "$KEY" \
    --wasm-path "${WASM_DIR}/${wasm}" \
    --payment-amount "$PAY" \
    --gas-price-tolerance 1 \
    --standard-payment true \
    --install-upgrade
}

echo "Deploy order: OracleRegistry -> DataMarket(registry,fee) -> set_market -> FundVault(operator) -> Governance(fund,risk,window)"
echo "This script submits the WASM installs; constructor args and wiring calls"
echo "depend on the odra-generated entry points — follow docs/TESTNET.md step 2"
echo "to pass init args, then record the contract hashes into agents/testnet.env."
echo

deploy "OracleRegistry.wasm"
echo "==> wait for finality, note REGISTRY_HASH, then deploy DataMarket with init args (see docs/TESTNET.md)"
