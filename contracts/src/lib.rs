//! Helios — RWA Data Exchange on Casper (native casper-contract, no Odra toolchain).
//!
//! Four contracts compiled from one crate via feature flags:
//!   cargo build --release --target wasm32-unknown-unknown --features oracle-registry
//!   cargo build --release --target wasm32-unknown-unknown --features data-market
//!   cargo build --release --target wasm32-unknown-unknown --features fund-vault
//!   cargo build --release --target wasm32-unknown-unknown --features governance
//!
//! Each binary exports a single `call` entry-point (the Casper ABI).
//! Tests compile with `cargo test` (no wasm target needed).

pub mod oracle_registry;
pub mod data_market;
pub mod fund_vault;
pub mod governance;

// ── Storage-key helpers shared across contracts ───────────────────────────────
use casper_contract::contract_api::storage;
use casper_types::{URef, CLValue, CLTyped};

pub fn put<T: CLTyped + casper_types::bytesrepr::ToBytes>(name: &str, value: T) {
    // store by name in the calling contract's named keys
    let uref: URef = storage::new_uref(value);
    casper_contract::contract_api::runtime::put_key(name, uref.into());
}

pub fn get<T: CLTyped + casper_types::bytesrepr::FromBytes + Default>(name: &str) -> T {
    let key = casper_contract::contract_api::runtime::get_key(name);
    match key {
        Some(k) => {
            let uref = k.into_uref().unwrap();
            storage::read(uref).unwrap().unwrap_or_default()
        }
        None => T::default(),
    }
}

pub fn put_uref<T: CLTyped + casper_types::bytesrepr::ToBytes>(name: &str, value: T) -> URef {
    let uref = storage::new_uref(value);
    casper_contract::contract_api::runtime::put_key(name, uref.into());
    uref
}
