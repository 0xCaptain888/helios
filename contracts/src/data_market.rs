extern crate alloc;
use alloc::string::{String, ToString};
use alloc::vec;
use casper_contract::{contract_api::{runtime, storage}, unwrap_or_revert::UnwrapOrRevert};
use casper_types::{CLType, EntryPoint, EntryPointAccess, EntryPointType, EntryPoints, Parameter, contracts::NamedKeys};

fn get_uref(name: &str) -> casper_types::URef { runtime::get_key(name).unwrap_or_revert().into_uref().unwrap_or_revert() }
fn read_str(u: casper_types::URef) -> String { storage::read::<String>(u).unwrap_or_revert().unwrap_or_default() }
fn write_str(u: casper_types::URef, v: String) { storage::write(u, v); }
fn read_u64(u: casper_types::URef) -> u64 { storage::read::<u64>(u).unwrap_or_revert().unwrap_or(0) }
fn read_u32(u: casper_types::URef) -> u32 { storage::read::<u32>(u).unwrap_or_revert().unwrap_or(0) }
fn encode_listing(o: &str, fk: &str, t: &str, p: u64, ep: &str, s: u64, r: u64) -> String {
    let mut s2 = String::new();
    s2.push_str(o); s2.push('|'); s2.push_str(fk); s2.push('|'); s2.push_str(t); s2.push('|');
    s2.push_str(&p.to_string()); s2.push('|'); s2.push_str(ep); s2.push('|');
    s2.push_str(&s.to_string()); s2.push('|'); s2.push_str(&r.to_string()); s2
}
fn decode_listing(s: &str) -> Option<(String,String,String,u64,String,u64,u64)> {
    let p: alloc::vec::Vec<&str> = s.splitn(7, '|').collect();
    if p.len() < 7 { return None; }
    Some((p[0].into(), p[1].into(), p[2].into(), p[3].parse().unwrap_or(0), p[4].into(), p[5].parse().unwrap_or(0), p[6].parse().unwrap_or(0)))
}

#[no_mangle]
pub extern "C" fn call() {
    let mut eps = EntryPoints::new();
    eps.add_entry_point(EntryPoint::new("list_feed", vec![Parameter::new("feed_key", CLType::String), Parameter::new("title", CLType::String), Parameter::new("price_motes", CLType::U64), Parameter::new("endpoint", CLType::String)], CLType::Unit, EntryPointAccess::Public, EntryPointType::Contract));
    eps.add_entry_point(EntryPoint::new("purchase", vec![Parameter::new("listing_id", CLType::U64)], CLType::Unit, EntryPointAccess::Public, EntryPointType::Contract));
    eps.add_entry_point(EntryPoint::new("anchor_x402_receipt", vec![Parameter::new("listing_id", CLType::U64), Parameter::new("oracle", CLType::String), Parameter::new("amount_motes", CLType::U64), Parameter::new("receipt_hash", CLType::String)], CLType::Unit, EntryPointAccess::Public, EntryPointType::Contract));
    eps.add_entry_point(EntryPoint::new("set_fee_bps", vec![Parameter::new("fee_bps", CLType::U32)], CLType::Unit, EntryPointAccess::Public, EntryPointType::Contract));
    eps.add_entry_point(EntryPoint::new("get_listing", vec![Parameter::new("listing_id", CLType::U64)], CLType::Unit, EntryPointAccess::Public, EntryPointType::Contract));
    eps.add_entry_point(EntryPoint::new("listing_count", vec![], CLType::Unit, EntryPointAccess::Public, EntryPointType::Contract));
    let admin = runtime::get_caller();
    let registry_hash: String = runtime::get_named_arg("registry_hash");
    let fee_bps: u32 = runtime::get_named_arg("fee_bps");
    let mut nk = NamedKeys::new();
    nk.insert("admin".into(), storage::new_uref(admin.to_string()).into());
    nk.insert("registry_hash".into(), storage::new_uref(registry_hash).into());
    nk.insert("fee_bps".into(), storage::new_uref(if fee_bps < 1000 { fee_bps } else { 1000 }).into());
    nk.insert("listing_count".into(), storage::new_uref(0u64).into());
    nk.insert("treasury_motes".into(), storage::new_uref(0u64).into());
    let (hash, _) = storage::new_contract(eps, Some(nk), Some("helios_data_market_hash".into()), Some("helios_data_market_access".into()));
    runtime::put_key("data_market_contract_hash", hash.into());
}

#[no_mangle]
pub extern "C" fn list_feed() {
    let feed_key: String = runtime::get_named_arg("feed_key");
    let title: String = runtime::get_named_arg("title");
    let price_motes: u64 = runtime::get_named_arg("price_motes");
    let endpoint: String = runtime::get_named_arg("endpoint");
    let caller = runtime::get_caller().to_string();
    let count_uref = get_uref("listing_count");
    let id = read_u64(count_uref);
    let key = alloc::format!("listing:{}", id);
    let uref = storage::new_uref(encode_listing(&caller, &feed_key, &title, price_motes, &endpoint, 0, 0));
    runtime::put_key(&key, uref.into());
    storage::write(count_uref, id + 1);
}

#[no_mangle]
pub extern "C" fn purchase() {
    let listing_id: u64 = runtime::get_named_arg("listing_id");
    let key = alloc::format!("listing:{}", listing_id);
    let listing_uref = runtime::get_key(&key).unwrap_or_revert().into_uref().unwrap_or_revert();
    let rec = read_str(listing_uref);
    let (oracle, fk, title, price, ep, sales, revenue) = decode_listing(&rec).unwrap_or_else(|| runtime::revert(casper_types::ApiError::User(11)));
    let fee_bps = read_u32(get_uref("fee_bps"));
    let fee = price as u128 * fee_bps as u128 / 10000;
    let treas_uref = get_uref("treasury_motes");
    storage::write(treas_uref, read_u64(treas_uref) + fee as u64);
    storage::write(listing_uref, encode_listing(&oracle, &fk, &title, price, &ep, sales + 1, revenue + price));
}

#[no_mangle]
pub extern "C" fn anchor_x402_receipt() {
    let listing_id: u64 = runtime::get_named_arg("listing_id");
    let oracle: String = runtime::get_named_arg("oracle");
    let amount_motes: u64 = runtime::get_named_arg("amount_motes");
    let receipt_hash: String = runtime::get_named_arg("receipt_hash");
    let key = alloc::format!("listing:{}", listing_id);
    let listing_uref = runtime::get_key(&key).unwrap_or_revert().into_uref().unwrap_or_revert();
    let rec = read_str(listing_uref);
    let (l_oracle, fk, title, price, ep, sales, revenue) = decode_listing(&rec).unwrap_or_else(|| runtime::revert(casper_types::ApiError::User(11)));
    let fee_bps = read_u32(get_uref("fee_bps"));
    let fee = amount_motes as u128 * fee_bps as u128 / 10000;
    let treas_uref = get_uref("treasury_motes");
    storage::write(treas_uref, read_u64(treas_uref) + fee as u64);
    storage::write(listing_uref, encode_listing(&l_oracle, &fk, &title, price, &ep, sales + 1, revenue + amount_motes));
    let rkey = alloc::format!("receipt:{}", receipt_hash);
    let rval = alloc::format!("{}:{}", oracle, amount_motes);
    runtime::put_key(&rkey, storage::new_uref(rval).into());
}

#[no_mangle]
pub extern "C" fn set_fee_bps() {
    let fee_bps: u32 = runtime::get_named_arg("fee_bps");
    storage::write(get_uref("fee_bps"), if fee_bps < 1000 { fee_bps } else { 1000 });
}

#[no_mangle]
pub extern "C" fn get_listing() {
    let listing_id: u64 = runtime::get_named_arg("listing_id");
    let key = alloc::format!("listing:{}", listing_id);
    let result = match runtime::get_key(&key) {
        Some(k) => read_str(k.into_uref().unwrap_or_revert()),
        None => String::new(),
    };
    runtime::put_key("result", storage::new_uref(result).into());
}

#[no_mangle]
pub extern "C" fn listing_count() {
    let count = read_u64(get_uref("listing_count"));
    runtime::put_key("result", storage::new_uref(count).into());
}
