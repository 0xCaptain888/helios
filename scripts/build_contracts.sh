#!/usr/bin/env bash
# Build all four Helios contracts to wasm.
# NO cargo-odra, NO glob patterns (zsh-safe), NO --import-memory.
# Requires: rustup target add wasm32-unknown-unknown
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONTRACTS="$ROOT/contracts"
WASM="$CONTRACTS/wasm"

# ── Guard: reject --import-memory before wasting compile time ─────────────────
# This flag makes the linker omit the memory section; Casper VM rejects such
# wasm with "Memory section should exist". It may come from:
#   • export RUSTFLAGS="... -C link-arg=--import-memory ..."  (terminal session)
#   • ~/.cargo/config.toml  [target.wasm32-unknown-unknown] rustflags = [...]
#   • an Odra tutorial you followed and never unset
if echo "${RUSTFLAGS:-}" | grep -q "import-memory"; then
  echo "ERROR: RUSTFLAGS contains '--import-memory'." >&2
  echo "  This causes Casper VM error: 'Memory section should exist'." >&2
  echo "  Fix: run  unset RUSTFLAGS  then re-run this script." >&2
  exit 1
fi
# Also check ~/.cargo/config.toml for the flag
CARGO_CONFIG="${CARGO_HOME:-$HOME/.cargo}/config.toml"
if [ -f "$CARGO_CONFIG" ] && grep -q "import-memory" "$CARGO_CONFIG"; then
  echo "ERROR: ~/.cargo/config.toml contains 'import-memory'." >&2
  echo "  Remove that line then re-run this script." >&2
  exit 1
fi

# ── Build ──────────────────────────────────────────────────────────────────────
echo "== Helios contract builder (native cargo, no cargo-odra) =="
rustup target add wasm32-unknown-unknown 2>/dev/null | grep -v "^info:" || true
mkdir -p "$WASM"

build_one() {
  local feature="$1"
  local outname="$2"
  echo "-- building $outname (feature: $feature)"
  # Explicitly set RUSTFLAGS to allow undefined Casper host functions during linking.
  # Casper VM provides these at runtime, so the linker must not fail on them.
  RUSTFLAGS="-C link-arg=--allow-undefined" cargo build \
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

# ── Pre-deploy gate ────────────────────────────────────────────────────────────
echo
echo "== pre-deploy gate: call export + memory section =="
python3 "$ROOT/scripts/check_wasm_exports.py" \
  "$WASM/OracleRegistry.wasm" \
  "$WASM/DataMarket.wasm" \
  "$WASM/FundVault.wasm" \
  "$WASM/Governance.wasm"
