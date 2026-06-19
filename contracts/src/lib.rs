//! Helios — RWA Data Exchange on Casper
//!
//! Four contracts compiled from one crate via feature flags:
//!   cargo build --release --target wasm32-unknown-unknown --features oracle-registry --no-default-features
//!   cargo build --release --target wasm32-unknown-unknown --features data-market    --no-default-features
//!   cargo build --release --target wasm32-unknown-unknown --features fund-vault     --no-default-features
//!   cargo build --release --target wasm32-unknown-unknown --features governance     --no-default-features

// ── no_std for Casper VM ──────────────────────────────────────────────────────
#![no_std]
// Note: #![no_main] is for binary crates. This is a cdylib — entry points are
// exported via #[no_mangle] pub extern "C" fn call() in each module.

extern crate alloc;

// ── Global allocator (required for no_std with heap allocation) ───────────────
#[cfg(not(test))]
#[global_allocator]
static ALLOC: wee_alloc::WeeAlloc = wee_alloc::WeeAlloc::INIT;

// ── Panic handler (required for no_std) ───────────────────────────────────────
#[cfg(not(test))]
#[panic_handler]
fn panic(_info: &core::panic::PanicInfo) -> ! {
    loop {}
}

pub mod oracle_registry;
pub mod data_market;
pub mod fund_vault;
pub mod governance;
