#![no_std]

extern crate alloc;

use core::panic::PanicInfo;

// no_std requires an explicit global allocator for wasm32-unknown-unknown
#[cfg(not(test))]
#[global_allocator]
static ALLOC: wee_alloc::WeeAlloc = wee_alloc::WeeAlloc::INIT;

// no_std requires an explicit panic handler
#[cfg(not(test))]
#[panic_handler]
fn panic(_info: &PanicInfo) -> ! {
    loop {}
}

#[cfg(feature = "oracle-registry")]
pub mod oracle_registry;

#[cfg(feature = "data-market")]
pub mod data_market;

#[cfg(feature = "fund-vault")]
pub mod fund_vault;

#[cfg(feature = "governance")]
pub mod governance;
