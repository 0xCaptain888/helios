#![no_std]
#![no_main]

extern crate alloc;

use core::panic::PanicInfo;

#[global_allocator]
static ALLOC: wee_alloc::WeeAlloc = wee_alloc::WeeAlloc::INIT;

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
