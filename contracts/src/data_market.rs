//! DataMarket — x402 receipt anchoring + direct purchase for RWA data feeds.
//!
//! Entry-points:
//!   list_feed           (feed_key, title, price_motes:u64, endpoint)
//!   purchase            (listing_id:u64)    — direct CSPR sale
//!   anchor_x402_receipt (listing_id:u64, oracle, amount_motes:u64, receipt_hash)
//!   set_fee_bps         (fee_bps:u32)       — admin only
//!   get_listing         (listing_id:u64)    → writes to "result"
//!   listing_count       ()                  → writes to "result"

use alloc::{
    format,
    string::{String, ToString},
    vec::Vec,
};
use casper_contract::{
    contract_api::{runtime, storage},
    unwrap_or_revert::UnwrapOrRevert,
};
use casper_types::{
    CLType, EntityEntryPoint, EntryPointAccess, EntryPointPayment, EntryPointType,
    EntryPoints, Parameter, URef,
    contracts::{ContractHash, NamedKeys},
};

// ── Pure arithmetic ───────────────────────────────────────────────────────────

pub fn protocol_fee(amount_motes: u64, fee_bps: u32) -> u64 {
    (amount_motes as u128 * fee_bps as u128 / 10_000) as u64
}

pub fn oracle_proceeds(amount_motes: u64, fee_bps: u32) -> u64 {
    amount_motes - protocol_fee(amount_motes, fee_bps)
}

// ── Storage helpers ───────────────────────────────────────────────────────────

fn get_uref(name: &str) -> URef {
    runtime::get_key(name)
        .unwrap_or_revert_with(casper_types::ApiError::MissingKey)
        .into_uref()
        .unwrap_or_revert()
}

fn read_str(uref: URef) -> String {
    storage::read::<String>(uref).unwrap_or_revert().unwrap_or_default()
}

fn read_u64(uref: URef) -> u64 {
    storage::read::<u64>(uref).unwrap_or_revert().unwrap_or(0)
}

fn read_u32(uref: URef) -> u32 {
    storage::read::<u32>(uref).unwrap_or_revert().unwrap_or(0)
}

// Listing: oracle|feed_key|title|price_motes|endpoint|sales|revenue_motes
fn encode_listing(oracle: &str, feed_key: &str, title: &str,
                  price: u64, endpoint: &str, sales: u64, revenue: u64) -> String {
    format!("{}|{}|{}|{}|{}|{}|{}", oracle, feed_key, title, price, endpoint, sales, revenue)
}

fn decode_listing(s: &str) -> Option<(String, String, String, u64, String, u64, u64)> {
    let p: Vec<&str> = s.splitn(7, '|').collect();
    if p.len() < 7 { return None; }
    Some((
        p[0].into(), p[1].into(), p[2].into(),
        p[3].parse().unwrap_or(0),
        p[4].into(),
        p[5].parse().unwrap_or(0),
        p[6].parse().unwrap_or(0),
    ))
}

// Cross-contract call to OracleRegistry::credit_settlement
fn credit_oracle(registry: &str, oracle: String) {
    if registry.is_empty() { return; }
    if let Ok(hash) = ContractHash::from_formatted_str(registry) {
        runtime::call_contract::<()>(
            hash,
            "credit_settlement",
            casper_types::runtime_args! { "oracle" => oracle },
        );
    }
}

// ── Phase 2: entry-point handlers ─────────────────────────────────────────────

#[cfg(feature = "data-market")]
#[no_mangle]
pub extern "C" fn list_feed() {
    let feed_key:    String = runtime::get_named_arg("feed_key");
    let title:       String = runtime::get_named_arg("title");
    let price_motes: u64   = runtime::get_named_arg("price_motes");
    let endpoint:    String = runtime::get_named_arg("endpoint");

    let caller = runtime::get_caller().to_string();
    let count_uref = get_uref("listing_count");
    let id = read_u64(count_uref);

    let key = format!("listing:{}", id);
    let uref = storage::new_uref(
        encode_listing(&caller, &feed_key, &title, price_motes, &endpoint, 0, 0)
    );
    runtime::put_key(&key, uref.into());
    storage::write(count_uref, id + 1);
}

#[cfg(feature = "data-market")]
#[no_mangle]
pub extern "C" fn purchase() {
    let listing_id: u64 = runtime::get_named_arg("listing_id");

    let key = format!("listing:{}", listing_id);
    let listing_uref = runtime::get_key(&key)
        .unwrap_or_revert_with(casper_types::ApiError::User(10))
        .into_uref()
        .unwrap_or_revert();

    let rec = read_str(listing_uref);
    let (oracle, feed_key, title, price, endpoint, sales, revenue) =
        decode_listing(&rec).unwrap_or_revert_with(casper_types::ApiError::User(11));

    let fee_bps = read_u32(get_uref("fee_bps"));
    let fee     = protocol_fee(price, fee_bps);

    let treas_uref = get_uref("treasury_motes");
    storage::write(treas_uref, read_u64(treas_uref) + fee);

    storage::write(listing_uref,
        encode_listing(&oracle, &feed_key, &title, price, &endpoint,
                       sales + 1, revenue + price));

    let registry = read_str(get_uref("registry_hash"));
    credit_oracle(&registry, oracle);
}

#[cfg(feature = "data-market")]
#[no_mangle]
pub extern "C" fn anchor_x402_receipt() {
    let listing_id:   u64    = runtime::get_named_arg("listing_id");
    let oracle:       String = runtime::get_named_arg("oracle");
    let amount_motes: u64   = runtime::get_named_arg("amount_motes");
    let receipt_hash: String = runtime::get_named_arg("receipt_hash");

    let key = format!("listing:{}", listing_id);
    let listing_uref = runtime::get_key(&key)
        .unwrap_or_revert_with(casper_types::ApiError::User(10))
        .into_uref()
        .unwrap_or_revert();

    let rec = read_str(listing_uref);
    let (l_oracle, feed_key, title, price, endpoint, sales, revenue) =
        decode_listing(&rec).unwrap_or_revert_with(casper_types::ApiError::User(11));

    let fee_bps = read_u32(get_uref("fee_bps"));
    let fee     = protocol_fee(amount_motes, fee_bps);

    let treas_uref = get_uref("treasury_motes");
    storage::write(treas_uref, read_u64(treas_uref) + fee);

    storage::write(listing_uref,
        encode_listing(&l_oracle, &feed_key, &title, price, &endpoint,
                       sales + 1, revenue + amount_motes));

    let rkey   = format!("receipt:{}", receipt_hash);
    let r_uref = storage::new_uref(format!("{}:{}", oracle, amount_motes));
    runtime::put_key(&rkey, r_uref.into());

    let registry = read_str(get_uref("registry_hash"));
    credit_oracle(&registry, oracle);
}

#[cfg(feature = "data-market")]
#[no_mangle]
pub extern "C" fn set_fee_bps() {
    let fee_bps: u32 = runtime::get_named_arg("fee_bps");
    let admin: String = read_str(get_uref("admin"));
    if runtime::get_caller().to_string() != admin {
        runtime::revert(casper_types::ApiError::User(2));
    }
    storage::write(get_uref("fee_bps"), fee_bps.min(1_000));
}

#[cfg(feature = "data-market")]
#[no_mangle]
pub extern "C" fn get_listing() {
    let listing_id: u64 = runtime::get_named_arg("listing_id");
    let key = format!("listing:{}", listing_id);
    let result = match runtime::get_key(&key) {
        Some(k) => read_str(k.into_uref().unwrap_or_revert()),
        None    => String::new(),
    };
    runtime::put_key("result", storage::new_uref(result).into());
}

#[cfg(feature = "data-market")]
#[no_mangle]
pub extern "C" fn listing_count() {
    let count = read_u64(get_uref("listing_count"));
    runtime::put_key("result", storage::new_uref(count.to_string()).into());
}

// ── Phase 1: install ──────────────────────────────────────────────────────────

#[cfg(feature = "data-market")]
#[no_mangle]
pub extern "C" fn call() {
    let mut entry_points = EntryPoints::new();
    for (name, params) in &[
        ("list_feed", alloc::vec![
            Parameter::new("feed_key",    CLType::String),
            Parameter::new("title",       CLType::String),
            Parameter::new("price_motes", CLType::U64),
            Parameter::new("endpoint",    CLType::String),
        ]),
        ("purchase",           alloc::vec![Parameter::new("listing_id", CLType::U64)]),
        ("anchor_x402_receipt", alloc::vec![
            Parameter::new("listing_id",   CLType::U64),
            Parameter::new("oracle",       CLType::String),
            Parameter::new("amount_motes", CLType::U64),
            Parameter::new("receipt_hash", CLType::String),
        ]),
        ("set_fee_bps",   alloc::vec![Parameter::new("fee_bps", CLType::U32)]),
        ("get_listing",   alloc::vec![Parameter::new("listing_id", CLType::U64)]),
        ("listing_count", alloc::vec![]),
    ] {
        entry_points.add_entry_point(EntityEntryPoint::new(
            name.to_string(), params.clone(),
            CLType::Unit, EntryPointAccess::Public, EntryPointType::Called,
            EntryPointPayment::Caller,
        ));
    }

    let registry_hash: String = runtime::get_named_arg("registry_hash");
    let fee_bps: u32          = runtime::get_named_arg("fee_bps");
    let admin = runtime::get_caller();

    let mut named_keys = NamedKeys::new();
    named_keys.insert("admin".into(),          storage::new_uref(admin.to_string()).into());
    named_keys.insert("registry_hash".into(),  storage::new_uref(registry_hash).into());
    named_keys.insert("fee_bps".into(),        storage::new_uref(fee_bps.min(1_000)).into());
    named_keys.insert("listing_count".into(),  storage::new_uref(0u64).into());
    named_keys.insert("treasury_motes".into(), storage::new_uref(0u64).into());

    let (contract_hash, _) = storage::new_contract(
        entry_points, Some(named_keys),
        Some("helios_data_market_hash".into()),
        Some("helios_data_market_access".into()),
        None,
    );
    runtime::put_key("data_market_contract_hash", contract_hash.into());
}

// ── Unit tests ────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fee_calculation_250bps() {
        let motes = 1_000_000_000u64;
        assert_eq!(protocol_fee(motes, 250), 25_000_000);
        assert_eq!(oracle_proceeds(motes, 250), 975_000_000);
    }

    #[test]
    fn fee_zero() {
        assert_eq!(protocol_fee(5_000_000_000, 0), 0);
        assert_eq!(oracle_proceeds(5_000_000_000, 0), 5_000_000_000);
    }

    #[test]
    fn fee_max_cap_semantics() {
        let motes = 10_000_000_000u64;
        assert_eq!(protocol_fee(motes, 1_000), 1_000_000_000);
        assert_eq!(oracle_proceeds(motes, 1_000), 9_000_000_000);
    }
}
