#!/usr/bin/env bash
# Build all four Helios contracts using feature flags.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONTRACTS="$ROOT/contracts"
WASM_DIR="$CONTRACTS/wasm"

echo "== Helios contract builder (native cargo with feature flags) =="

cd "$CONTRACTS"

# Clean previous builds
rm -rf "$WASM_DIR"
mkdir -p "$WASM_DIR"

# Build each contract with its feature flag
echo "-- Building OracleRegistry.wasm (feature: oracle-registry)"
RUSTFLAGS="-C link-arg=--allow-undefined" cargo build --release --target wasm32-unknown-unknown --features oracle-registry --no-default-features
cp target/wasm32-unknown-unknown/release/helios_contracts.wasm "$WASM_DIR/OracleRegistry.wasm"
echo "   ✓ OracleRegistry.wasm ($(stat -c%s "$WASM_DIR/OracleRegistry.wasm" 2>/dev/null || stat -f%z "$WASM_DIR/OracleRegistry.wasm") bytes)"

echo "-- Building DataMarket.wasm (feature: data-market)"
RUSTFLAGS="-C link-arg=--allow-undefined" cargo build --release --target wasm32-unknown-unknown --features data-market --no-default-features
cp target/wasm32-unknown-unknown/release/helios_contracts.wasm "$WASM_DIR/DataMarket.wasm"
echo "   ✓ DataMarket.wasm ($(stat -c%s "$WASM_DIR/DataMarket.wasm" 2>/dev/null || stat -f%z "$WASM_DIR/DataMarket.wasm") bytes)"

echo "-- Building FundVault.wasm (feature: fund-vault)"
RUSTFLAGS="-C link-arg=--allow-undefined" cargo build --release --target wasm32-unknown-unknown --features fund-vault --no-default-features
cp target/wasm32-unknown-unknown/release/helios_contracts.wasm "$WASM_DIR/FundVault.wasm"
echo "   ✓ FundVault.wasm ($(stat -c%s "$WASM_DIR/FundVault.wasm" 2>/dev/null || stat -f%z "$WASM_DIR/FundVault.wasm") bytes)"

echo "-- Building Governance.wasm (feature: governance)"
RUSTFLAGS="-C link-arg=--allow-undefined" cargo build --release --target wasm32-unknown-unknown --features governance --no-default-features
cp target/wasm32-unknown-unknown/release/helios_contracts.wasm "$WASM_DIR/Governance.wasm"
echo "   ✓ Governance.wasm ($(stat -c%s "$WASM_DIR/Governance.wasm" 2>/dev/null || stat -f%z "$WASM_DIR/Governance.wasm") bytes)"

echo
echo "== Build complete =="
echo "WASM files are in: $WASM_DIR/"
ls -lh "$WASM_DIR/"
