#!/usr/bin/env bash
# Build all four Helios contracts to WASM for Casper Testnet.
#
# Key requirement: disable bulk-memory and reference-types features.
# Casper's wasmi VM only supports the WASM MVP subset.
# The .cargo/config.toml in contracts/ handles this automatically,
# but we also set RUSTFLAGS here as an explicit safety net.
#
# Usage: bash scripts/build_contracts.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONTRACTS="$ROOT/contracts"
WASM_OUT="$CONTRACTS/wasm"

echo "== Helios contract builder (casper-contract v5 / casper-types v6) =="
echo "   Target: wasm32-unknown-unknown (MVP only — bulk-memory disabled)"
echo ""

# ── Sanity checks ─────────────────────────────────────────────────────────────
if ! command -v cargo &>/dev/null; then
    echo "ERROR: cargo not found. Install Rust: curl https://sh.rustup.rs | sh"
    exit 1
fi
if ! rustup target list --installed 2>/dev/null | grep -q "wasm32-unknown-unknown"; then
    echo "Installing wasm32-unknown-unknown target..."
    rustup target add wasm32-unknown-unknown
fi

# ── Warn if RUSTFLAGS env var would conflict ──────────────────────────────────
if echo "${RUSTFLAGS:-}" | grep -q "import-memory"; then
    echo "ERROR: RUSTFLAGS contains '--import-memory'."
    echo "  This breaks Casper VM. Run: unset RUSTFLAGS"
    exit 1
fi

# Disable bulk-memory + reference-types (backup for .cargo/config.toml)
export RUSTFLAGS="-C target-feature=-bulk-memory,-bulk-memory-opt,-reference-types -C link-arg=--allow-undefined"

mkdir -p "$WASM_OUT"

# ── Build each contract with its feature flag ─────────────────────────────────
build_one() {
    local feature="$1"
    local outname="$2"
    echo "-- building $outname"
    cargo build \
        --release \
        --manifest-path "$CONTRACTS/Cargo.toml" \
        --target wasm32-unknown-unknown \
        --features "$feature" \
        --no-default-features \
        2>&1
    cp "$CONTRACTS/target/wasm32-unknown-unknown/release/helios_contracts.wasm" \
       "$WASM_OUT/$outname"
    echo "   written: $WASM_OUT/$outname ($(wc -c < "$WASM_OUT/$outname") bytes)"
}

build_one oracle-registry  OracleRegistry.wasm
build_one data-market      DataMarket.wasm
build_one fund-vault       FundVault.wasm
build_one governance       Governance.wasm

# ── Verify: no bulk-memory (0xFC) instructions, call export present ───────────
echo ""
echo "== Verifying WASM compatibility =="
python3 "$ROOT/scripts/check_wasm_exports.py" \
    "$WASM_OUT/OracleRegistry.wasm" \
    "$WASM_OUT/DataMarket.wasm" \
    "$WASM_OUT/FundVault.wasm" \
    "$WASM_OUT/Governance.wasm"

echo ""
echo "== Build complete! All contracts ready for deployment. =="
echo "   Next: python3 scripts/casper_deploy.py deploy-all --key \"keys/Account 2_secret_key.pem\""
