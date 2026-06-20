# Building Helios Contracts

## Dependencies

- **casper-contract v5** — Casper 2.x contract SDK
- **casper-types v6** — Casper 2.x type system
- **wee_alloc** — no_std global allocator for wasm32-unknown-unknown

All contracts use `#![no_std]` (no standard library) as required by the Casper VM.

## Build all four contracts

```bash
# 1. add the wasm target (once)
rustup target add wasm32-unknown-unknown

# 2. build all 4 contracts via feature flags
bash scripts/build_contracts.sh

# Output:
#   contracts/wasm/OracleRegistry.wasm (65KB)
#   contracts/wasm/DataMarket.wasm     (69KB)
#   contracts/wasm/FundVault.wasm      (60KB)
#   contracts/wasm/Governance.wasm     (61KB)
```

The build script uses `RUSTFLAGS="-C link-arg=--allow-undefined"` to allow
references to Casper VM host functions (casper_get_named_arg, casper_revert, etc.)
that are resolved at runtime by the VM.

### Manual build (single contract)

```bash
cd contracts

RUSTFLAGS="-C link-arg=--allow-undefined" cargo build --release \
  --target wasm32-unknown-unknown \
  --features oracle-registry \
  --no-default-features

cp target/wasm32-unknown-unknown/release/helios_contracts.wasm wasm/OracleRegistry.wasm
```

### Verify WASM exports

```bash
python3 scripts/check_wasm_exports.py wasm/*.wasm
```

Every WASM must export `call` (the Casper ABI entry point).

## Run unit tests (host build, no wasm target needed)

```bash
cd contracts
cargo test
```

## Why feature flags, not separate crates?

One crate, four feature-gated `#[no_mangle] pub extern "C" fn call()`.
Each build produces one wasm that is fully self-contained and exports exactly
one entry-point named `call` — the Casper ABI requirement.

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `undefined symbol: casper_*` | Missing `--allow-undefined` | Use `RUSTFLAGS="-C link-arg=--allow-undefined"` |
| `can't find crate for 'std'` | Missing `#![no_std]` | Already added in lib.rs |
| `Memory section should exist` | RUSTFLAGS has `--import-memory` | `unset RUSTFLAGS` before building |
| `version conflict casper-contract` | Stale cache | `rm -rf contracts/target/` then rebuild |
