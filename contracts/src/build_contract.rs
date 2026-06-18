//! Odra build contract entry point.
//! This file is required by cargo-odra to generate WASM files.

#![no_main]
#![no_std]

use odra::prelude::*;

// Re-export all contract modules
pub use helios_contracts::*;
