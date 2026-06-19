# Building Helios Contracts

## Why no `cargo odra build`?

`cargo odra build` depends on `odra-build` → `odra-schema` which silently
pulls `odra-core >=1.5.1`. Combined with the `[[bin]]` zsh-glob trap
(`rm -rf wasm/*.wasm` on an empty dir kills the whole command in zsh before
cargo even runs), these two issues make the toolchain unreliable. We use
plain `cargo build --target wasm32-unknown-unknown` instead.

## Build all four contracts

```bash
# 1. add the wasm target (once)
rustup target add wasm32-unknown-unknown

# 2. compile each contract via its feature flag
cd contracts

mkdir -p wasm

cargo build --release --target wasm32-unknown-unknown --features oracle-registry \
  && cp target/wasm32-unknown-unknown/release/helios_contracts.wasm wasm/OracleRegistry.wasm

cargo build --release --target wasm32-unknown-unknown --features data-market \
  && cp target/wasm32-unknown-unknown/release/helios_contracts.wasm wasm/DataMarket.wasm

cargo build --release --target wasm32-unknown-unknown --features fund-vault \
  && cp target/wasm32-unknown-unknown/release/helios_contracts.wasm wasm/FundVault.wasm

cargo build --release --target wasm32-unknown-unknown --features governance \
  && cp target/wasm32-unknown-unknown/release/helios_contracts.wasm wasm/Governance.wasm

# 3. verify every wasm exports `call` (not `main`)
python3 ../scripts/check_wasm_exports.py wasm/*.wasm
```

## Run the 13 unit tests (host, no wasm target needed)

```bash
cargo test
```

## Why feature flags, not separate crates?

One crate, four feature-gated `#[no_mangle] pub extern "C" fn call()`.
Each build produces one wasm that is fully self-contained and exports exactly
one entry-point named `call` — the Casper ABI requirement. No odra-build,
no odra-schema, no [[bin]] targets, no zsh glob bombs.
