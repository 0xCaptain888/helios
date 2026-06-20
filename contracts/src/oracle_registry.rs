extern crate alloc;
use alloc::string::{String, ToString};
use alloc::vec;
use casper_contract::{contract_api::{runtime, storage}, unwrap_or_revert::UnwrapOrRevert};
use casper_types::{CLType, EntityEntryPoint, EntryPointAccess, EntryPointType, EntryPointPayment, EntryPoints, Parameter, contracts::NamedKeys};

fn get_uref(name: &str) -> casper_types::URef {
    runtime::get_key(name).unwrap_or_revert().into_uref().unwrap_or_revert()
}
fn read_str(uref: casper_types::URef) -> String {
    storage::read::<String>(uref).unwrap_or_revert().unwrap_or_default()
}
fn write_str(uref: casper_types::URef, v: String) { storage::write(uref, v); }
fn read_u64(uref: casper_types::URef) -> u64 {
    storage::read::<u64>(uref).unwrap_or_revert().unwrap_or(0)
}
fn encode_rep(sett: u64, attest: u64, acc: u64, disp: u64, score: u32) -> String {
    let mut s = String::new();
    // simple concat with | separator
    s.push_str(&sett.to_string()); s.push('|');
    s.push_str(&attest.to_string()); s.push('|');
    s.push_str(&acc.to_string()); s.push('|');
    s.push_str(&disp.to_string()); s.push('|');
    s.push_str(&score.to_string());
    s
}
fn decode_rep(s: &str) -> (u64, u64, u64, u64, u32) {
    let mut parts = s.split('|');
    let mut p = |d: u64| parts.next().and_then(|x| x.parse().ok()).unwrap_or(d);
    let sett = p(0); let attest = p(0); let acc = p(0); let disp = p(0);
    let score = parts.next().and_then(|x| x.parse().ok()).unwrap_or(5000u32);
    (sett, attest, acc, disp, score)
}
fn compute_score(sett: u64, acc: u64, disp: u64) -> u32 {
    let scored = acc + disp;
    let accuracy: u64 = if scored == 0 { 5000 } else { acc * 10000 / scored };
    let activity = if sett < 100 { sett } else { 100 };
    let weight = 2000 + (10000 - 2000) * activity / 100;
    (accuracy * weight / 10000) as u32
}

#[no_mangle]
pub extern "C" fn call() {
    let mut eps = EntryPoints::new();
    eps.add_entry_point(EntityEntryPoint::new("register", vec![Parameter::new("name", CLType::String), Parameter::new("category", CLType::String), Parameter::new("endpoint", CLType::String), Parameter::new("price_motes", CLType::U64)], CLType::Unit, EntryPointAccess::Public, EntryPointType::Called, EntryPointPayment::Caller));
    eps.add_entry_point(EntityEntryPoint::new("post_attestation", vec![Parameter::new("feed_key", CLType::String), Parameter::new("value", CLType::String)], CLType::Unit, EntryPointAccess::Public, EntryPointType::Called, EntryPointPayment::Caller));
    eps.add_entry_point(EntityEntryPoint::new("credit_settlement", vec![Parameter::new("oracle", CLType::String)], CLType::Unit, EntryPointAccess::Public, EntryPointType::Called, EntryPointPayment::Caller));
    eps.add_entry_point(EntityEntryPoint::new("score_attestation", vec![Parameter::new("oracle", CLType::String), Parameter::new("accurate", CLType::Bool)], CLType::Unit, EntryPointAccess::Public, EntryPointType::Called, EntryPointPayment::Caller));
    eps.add_entry_point(EntityEntryPoint::new("set_market", vec![Parameter::new("market", CLType::String)], CLType::Unit, EntryPointAccess::Public, EntryPointType::Called, EntryPointPayment::Caller));
    eps.add_entry_point(EntityEntryPoint::new("get_oracle", vec![Parameter::new("oracle", CLType::String)], CLType::Unit, EntryPointAccess::Public, EntryPointType::Called, EntryPointPayment::Caller));
    eps.add_entry_point(EntityEntryPoint::new("get_reputation", vec![Parameter::new("oracle", CLType::String)], CLType::Unit, EntryPointAccess::Public, EntryPointType::Called, EntryPointPayment::Caller));
    let admin = runtime::get_caller();
    let mut nk = NamedKeys::new();
    nk.insert("admin".into(), storage::new_uref(admin.to_string()).into());
    nk.insert("oracle_count".into(), storage::new_uref(0u64).into());
    nk.insert("market".into(), storage::new_uref(String::new()).into());
    let (hash, _) = storage::new_contract(eps, Some(nk), Some("helios_oracle_registry_hash".into()), Some("helios_oracle_registry_access".into()), None);
    runtime::put_key("oracle_registry_contract_hash", hash.into());
}

#[no_mangle]
pub extern "C" fn register() {
    let name: String = runtime::get_named_arg("name");
    let category: String = runtime::get_named_arg("category");
    let endpoint: String = runtime::get_named_arg("endpoint");
    let price_motes: u64 = runtime::get_named_arg("price_motes");
    let caller = runtime::get_caller().to_string();
    let oracle_key = alloc::format!("oracle:{}", caller);
    let uref = match runtime::get_key(&oracle_key) {
        Some(k) => k.into_uref().unwrap_or_revert(),
        None => {
            let u = storage::new_uref(String::new());
            runtime::put_key(&oracle_key, u.into());
            let rep_key = alloc::format!("rep:{}", caller);
            let rep_uref = storage::new_uref(encode_rep(0, 0, 0, 0, 5000));
            runtime::put_key(&rep_key, rep_uref.into());
            let count_uref = get_uref("oracle_count");
            let count = read_u64(count_uref);
            storage::write(count_uref, count + 1);
            u
        }
    };
    let mut s = String::new();
    s.push_str(&name); s.push('|'); s.push_str(&category); s.push('|');
    s.push_str(&endpoint); s.push('|'); s.push_str(&price_motes.to_string()); s.push_str("|1");
    write_str(uref, s);
}

#[no_mangle]
pub extern "C" fn post_attestation() {
    let feed_key: String = runtime::get_named_arg("feed_key");
    let value: String = runtime::get_named_arg("value");
    let caller = runtime::get_caller().to_string();
    let rep_key = alloc::format!("rep:{}", caller);
    let rep_uref = match runtime::get_key(&rep_key) {
        Some(k) => k.into_uref().unwrap_or_revert(),
        None => runtime::revert(casper_types::ApiError::User(1)),
    };
    let (sett, attest, acc, disp, _) = decode_rep(&read_str(rep_uref));
    let score = compute_score(sett, acc, disp);
    write_str(rep_uref, encode_rep(sett, attest + 1, acc, disp, score));
    // Store attestation data
    let attest_key = alloc::format!("attest:{}:{}:{}", caller, feed_key, attest + 1);
    let attest_data = alloc::format!("{}|{}", feed_key, value);
    runtime::put_key(&attest_key, storage::new_uref(attest_data).into());
}

#[no_mangle]
pub extern "C" fn credit_settlement() {
    let oracle: String = runtime::get_named_arg("oracle");
    // Access control: only admin or market contract can credit settlements
    let caller = runtime::get_caller().to_string();
    let admin = read_str(get_uref("admin"));
    let market = read_str(get_uref("market"));
    if caller != admin && caller != market {
        runtime::revert(casper_types::ApiError::User(2)); // NotAuthorized
    }
    let rep_key = alloc::format!("rep:{}", oracle);
    let rep_uref = match runtime::get_key(&rep_key) {
        Some(k) => k.into_uref().unwrap_or_revert(),
        None => runtime::revert(casper_types::ApiError::User(3)),
    };
    let (sett, attest, acc, disp, _) = decode_rep(&read_str(rep_uref));
    let score = compute_score(sett + 1, acc, disp);
    write_str(rep_uref, encode_rep(sett + 1, attest, acc, disp, score));
}

#[no_mangle]
pub extern "C" fn score_attestation() {
    let oracle: String = runtime::get_named_arg("oracle");
    let accurate: bool = runtime::get_named_arg("accurate");
    // Access control: only admin can score attestations
    let caller = runtime::get_caller().to_string();
    let admin = read_str(get_uref("admin"));
    if caller != admin {
        runtime::revert(casper_types::ApiError::User(2)); // NotAuthorized
    }
    let rep_key = alloc::format!("rep:{}", oracle);
    let rep_uref = match runtime::get_key(&rep_key) {
        Some(k) => k.into_uref().unwrap_or_revert(),
        None => runtime::revert(casper_types::ApiError::User(3)),
    };
    let (sett, attest, acc, disp, _) = decode_rep(&read_str(rep_uref));
    let (na, nd) = if accurate { (acc + 1, disp) } else { (acc, disp + 1) };
    let score = compute_score(sett, na, nd);
    write_str(rep_uref, encode_rep(sett, attest, na, nd, score));
}

#[no_mangle]
pub extern "C" fn set_market() {
    let market: String = runtime::get_named_arg("market");
    // Access control: only admin can set market
    let caller = runtime::get_caller().to_string();
    let admin = read_str(get_uref("admin"));
    if caller != admin {
        runtime::revert(casper_types::ApiError::User(2)); // NotAuthorized
    }
    let market_uref = match runtime::get_key("market") {
        Some(k) => k.into_uref().unwrap_or_revert(),
        None => { let u = storage::new_uref(String::new()); runtime::put_key("market", u.into()); u }
    };
    write_str(market_uref, market);
}

#[no_mangle]
pub extern "C" fn get_oracle() {
    let oracle: String = runtime::get_named_arg("oracle");
    let key = alloc::format!("oracle:{}", oracle);
    let result = match runtime::get_key(&key) {
        Some(k) => read_str(k.into_uref().unwrap_or_revert()),
        None => String::new(),
    };
    runtime::put_key("result", storage::new_uref(result).into());
}

#[no_mangle]
pub extern "C" fn get_reputation() {
    let oracle: String = runtime::get_named_arg("oracle");
    let key = alloc::format!("rep:{}", oracle);
    let result = match runtime::get_key(&key) {
        Some(k) => read_str(k.into_uref().unwrap_or_revert()),
        None => encode_rep(0, 0, 0, 0, 5000),
    };
    runtime::put_key("result", storage::new_uref(result).into());
}
