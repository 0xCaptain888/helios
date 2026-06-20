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
// Using a simple bump allocator to avoid bulk memory operations
use core::alloc::{GlobalAlloc, Layout};
use core::cell::UnsafeCell;

struct BumpAllocator {
    heap: UnsafeCell<[u8; 65536]>,  // 64KB heap
    offset: UnsafeCell<usize>,
}

unsafe impl Sync for BumpAllocator {}

impl BumpAllocator {
    const fn new() -> Self {
        BumpAllocator {
            heap: UnsafeCell::new([0; 65536]),
            offset: UnsafeCell::new(0),
        }
    }
}

unsafe impl GlobalAlloc for BumpAllocator {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        let offset = &mut *self.offset.get();
        let align = layout.align();
        let aligned_offset = (*offset + align - 1) & !(align - 1);
        let new_offset = aligned_offset + layout.size();
        if new_offset > 65536 {
            return core::ptr::null_mut();
        }
        *offset = new_offset;
        let heap = &mut *self.heap.get();
        heap.as_mut_ptr().add(aligned_offset)
    }

    unsafe fn dealloc(&self, _ptr: *mut u8, _layout: Layout) {
        // Bump allocator doesn't support deallocation
    }
}

#[cfg(not(test))]
#[global_allocator]
static ALLOC: BumpAllocator = BumpAllocator::new();

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
