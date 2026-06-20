#!/usr/bin/env bash
# Build all four Helios contracts to wasm.
# Requirements: rustup target add wasm32-unknown-unknown
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONTRACTS="$ROOT/contracts"
WASM="$CONTRACTS/wasm"

# ── Guard: --import-memory breaks Casper VM ────────────────────────────────────
if echo "${RUSTFLAGS:-}" | grep -q "import-memory"; then
  echo "ERROR: RUSTFLAGS contains '--import-memory'." >&2
  echo "  Casper VM requires the memory section to exist." >&2
  echo "  Fix: run  unset RUSTFLAGS  then re-run this script." >&2
  exit 1
fi
CARGO_CONFIG="${CARGO_HOME:-$HOME/.cargo}/config.toml"
if [ -f "$CARGO_CONFIG" ] && grep -q "import-memory" "$CARGO_CONFIG"; then
  echo "ERROR: ~/.cargo/config.toml contains 'import-memory'. Remove it." >&2
  exit 1
fi

echo "== Helios contract builder (casper-contract v5 / casper-types v6) =="
rustup target add wasm32-unknown-unknown 2>/dev/null | grep -v "^info:" || true
mkdir -p "$WASM"

build_one() {
  local feature="$1"
  local outname="$2"
  echo "-- building $outname (--features $feature)"
  # Disable bulk-memory and reference-types for Casper VM compatibility
  RUSTFLAGS="-C link-arg=--allow-undefined -C target-feature=-bulk-memory,-reference-types,-sign-ext" cargo build \
    --release \
    --manifest-path "$CONTRACTS/Cargo.toml" \
    --target wasm32-unknown-unknown \
    --features "$feature" \
    --no-default-features 2>&1
  cp "$CONTRACTS/target/wasm32-unknown-unknown/release/helios_contracts.wasm" \
     "$WASM/$outname"
  echo "   written: $WASM/$outname ($(wc -c < "$WASM/$outname") bytes)"
}

build_one oracle-registry  OracleRegistry.wasm
build_one data-market      DataMarket.wasm
build_one fund-vault       FundVault.wasm
build_one governance       Governance.wasm

echo
echo "== Checking wasm exports =="
python3 "$ROOT/scripts/check_wasm_exports.py" \
  "$WASM/OracleRegistry.wasm" \
  "$WASM/DataMarket.wasm" \
  "$WASM/FundVault.wasm" \
  "$WASM/Governance.wasm"

echo
echo "== Build complete! Next: node scripts/deploy_helios.js deploy-all =="
