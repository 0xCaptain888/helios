//! OracleRegistry — on-chain identity & reputation for RWA data oracles.
//!
//! Entry-points:
//!   register            (name, category, endpoint, price_motes:u64)
//!   post_attestation    (feed_key, value)
//!   credit_settlement   (oracle) — called by DataMarket after a sale
//!   score_attestation   (oracle, accurate:bool) — admin only
//!   set_market          (market) — admin only
//!   get_oracle          (oracle) → writes result to named key "result"
//!   get_reputation      (oracle) → writes result to named key "result"

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
    contracts::NamedKeys,
};

// ── Storage helpers ───────────────────────────────────────────────────────────

fn read_str(uref: URef) -> String {
    storage::read(uref).unwrap_or_revert().unwrap_or_default()
}

fn write_str(uref: URef, v: String) {
    storage::write(uref, v);
}

fn get_uref(name: &str) -> URef {
    runtime::get_key(name)
        .unwrap_or_revert_with(casper_types::ApiError::MissingKey)
        .into_uref()
        .unwrap_or_revert()
}

// Oracle record: name|category|endpoint|price_motes|active(1/0)
fn encode_oracle(name: &str, category: &str, endpoint: &str, price: u64, active: bool) -> String {
    format!("{}|{}|{}|{}|{}", name, category, endpoint, price, if active { 1 } else { 0 })
}

// Reputation: settlements|attestations|accurate|disputed|score_bps
fn encode_rep(settlements: u64, attestations: u64, accurate: u64, disputed: u64, score: u32) -> String {
    format!("{}|{}|{}|{}|{}", settlements, attestations, accurate, disputed, score)
}

fn decode_rep(s: &str) -> (u64, u64, u64, u64, u32) {
    let p: Vec<&str> = s.splitn(5, '|').collect();
    if p.len() < 5 { return (0, 0, 0, 0, 5000); }
    (
        p[0].parse().unwrap_or(0),
        p[1].parse().unwrap_or(0),
        p[2].parse().unwrap_or(0),
        p[3].parse().unwrap_or(0),
        p[4].parse().unwrap_or(5000),
    )
}

// ── Reputation arithmetic ─────────────────────────────────────────────────────

pub fn compute_score(settlements: u64, accurate: u64, disputed: u64) -> u32 {
    let scored = accurate + disputed;
    let accuracy_bps: u64 = if scored == 0 { 5_000 } else { accurate * 10_000 / scored };
    let activity = core::cmp::min(settlements, 100);
    let floor = 2_000u64;
    let weight = floor + (10_000 - floor) * activity / 100;
    (accuracy_bps * weight / 10_000) as u32
}

// ── Phase 2: entry-point handlers ─────────────────────────────────────────────

#[cfg(feature = "oracle-registry")]
#[no_mangle]
pub extern "C" fn register() {
    let name:        String = runtime::get_named_arg("name");
    let category:    String = runtime::get_named_arg("category");
    let endpoint:    String = runtime::get_named_arg("endpoint");
    let price_motes: u64   = runtime::get_named_arg("price_motes");

    let caller = runtime::get_caller().to_string();
    let oracle_key = format!("oracle:{}", caller);

    let uref = match runtime::get_key(&oracle_key) {
        Some(k) => k.into_uref().unwrap_or_revert(),
        None    => {
            let u = storage::new_uref(String::new());
            runtime::put_key(&oracle_key, u.into());
            let rep_key = format!("rep:{}", caller);
            let rep_uref = storage::new_uref(encode_rep(0, 0, 0, 0, 5000));
            runtime::put_key(&rep_key, rep_uref.into());
            let count_uref = get_uref("oracle_count");
            let count: u64 = storage::read(count_uref).unwrap_or_revert().unwrap_or(0);
            storage::write(count_uref, count + 1);
            u
        }
    };
    write_str(uref, encode_oracle(&name, &category, &endpoint, price_motes, true));
}

#[cfg(feature = "oracle-registry")]
#[no_mangle]
pub extern "C" fn post_attestation() {
    let feed_key: String = runtime::get_named_arg("feed_key");
    let value:    String = runtime::get_named_arg("value");
    let caller = runtime::get_caller().to_string();

    let rep_key  = format!("rep:{}", caller);
    let rep_uref = match runtime::get_key(&rep_key) {
        Some(k) => k.into_uref().unwrap_or_revert(),
        None    => runtime::revert(casper_types::ApiError::User(1)),
    };
    let (sett, attest, acc, disp, _) = decode_rep(&read_str(rep_uref));
    let score = compute_score(sett, acc, disp);
    write_str(rep_uref, encode_rep(sett, attest + 1, acc, disp, score));

    let att_key = format!("att:{}", feed_key);
    let att_uref = match runtime::get_key(&att_key) {
        Some(k) => k.into_uref().unwrap_or_revert(),
        None    => {
            let u = storage::new_uref(String::new());
            runtime::put_key(&att_key, u.into());
            u
        }
    };
    write_str(att_uref, format!("{}:{}", caller, value));
}

#[cfg(feature = "oracle-registry")]
#[no_mangle]
pub extern "C" fn credit_settlement() {
    let oracle: String = runtime::get_named_arg("oracle");

    let market_uref = get_uref("market");
    let market: String = read_str(market_uref);
    let caller = runtime::get_caller().to_string();
    let admin_uref  = get_uref("admin");
    let admin: String = read_str(admin_uref);
    if caller != market && caller != admin {
        runtime::revert(casper_types::ApiError::User(2));
    }

    let rep_key = format!("rep:{}", oracle);
    let rep_uref = match runtime::get_key(&rep_key) {
        Some(k) => k.into_uref().unwrap_or_revert(),
        None    => runtime::revert(casper_types::ApiError::User(3)),
    };
    let (sett, attest, acc, disp, _) = decode_rep(&read_str(rep_uref));
    let score = compute_score(sett + 1, acc, disp);
    write_str(rep_uref, encode_rep(sett + 1, attest, acc, disp, score));
}

#[cfg(feature = "oracle-registry")]
#[no_mangle]
pub extern "C" fn score_attestation() {
    let oracle:   String = runtime::get_named_arg("oracle");
    let accurate: bool   = runtime::get_named_arg("accurate");

    let admin: String = read_str(get_uref("admin"));
    if runtime::get_caller().to_string() != admin {
        runtime::revert(casper_types::ApiError::User(2));
    }

    let rep_key  = format!("rep:{}", oracle);
    let rep_uref = match runtime::get_key(&rep_key) {
        Some(k) => k.into_uref().unwrap_or_revert(),
        None    => runtime::revert(casper_types::ApiError::User(3)),
    };
    let (sett, attest, acc, disp, _) = decode_rep(&read_str(rep_uref));
    let (new_acc, new_disp) = if accurate { (acc + 1, disp) } else { (acc, disp + 1) };
    let score = compute_score(sett, new_acc, new_disp);
    write_str(rep_uref, encode_rep(sett, attest, new_acc, new_disp, score));
}

#[cfg(feature = "oracle-registry")]
#[no_mangle]
pub extern "C" fn set_market() {
    let market: String = runtime::get_named_arg("market");
    let admin: String  = read_str(get_uref("admin"));
    if runtime::get_caller().to_string() != admin {
        runtime::revert(casper_types::ApiError::User(2));
    }
    let market_uref = match runtime::get_key("market") {
        Some(k) => k.into_uref().unwrap_or_revert(),
        None    => {
            let u = storage::new_uref(String::new());
            runtime::put_key("market", u.into());
            u
        }
    };
    write_str(market_uref, market);
}

#[cfg(feature = "oracle-registry")]
#[no_mangle]
pub extern "C" fn get_oracle() {
    let oracle: String = runtime::get_named_arg("oracle");
    let key = format!("oracle:{}", oracle);
    let result = match runtime::get_key(&key) {
        Some(k) => read_str(k.into_uref().unwrap_or_revert()),
        None    => String::new(),
    };
    runtime::put_key("result", storage::new_uref(result).into());
}

#[cfg(feature = "oracle-registry")]
#[no_mangle]
pub extern "C" fn get_reputation() {
    let oracle: String = runtime::get_named_arg("oracle");
    let key = format!("rep:{}", oracle);
    let result = match runtime::get_key(&key) {
        Some(k) => read_str(k.into_uref().unwrap_or_revert()),
        None    => encode_rep(0, 0, 0, 0, 5000),
    };
    runtime::put_key("result", storage::new_uref(result).into());
}

// ── Phase 1: install (runs once at deploy time) ───────────────────────────────

#[cfg(feature = "oracle-registry")]
#[no_mangle]
pub extern "C" fn call() {
    let mut entry_points = EntryPoints::new();
    for (name, params) in &[
        ("register", alloc::vec![
            Parameter::new("name",        CLType::String),
            Parameter::new("category",    CLType::String),
            Parameter::new("endpoint",    CLType::String),
            Parameter::new("price_motes", CLType::U64),
        ]),
        ("post_attestation", alloc::vec![
            Parameter::new("feed_key", CLType::String),
            Parameter::new("value",    CLType::String),
        ]),
        ("credit_settlement", alloc::vec![Parameter::new("oracle", CLType::String)]),
        ("score_attestation", alloc::vec![
            Parameter::new("oracle",   CLType::String),
            Parameter::new("accurate", CLType::Bool),
        ]),
        ("set_market",     alloc::vec![Parameter::new("market", CLType::String)]),
        ("get_oracle",     alloc::vec![Parameter::new("oracle", CLType::String)]),
        ("get_reputation", alloc::vec![Parameter::new("oracle", CLType::String)]),
    ] {
        entry_points.add_entry_point(EntityEntryPoint::new(
            name.to_string(), params.clone(),
            CLType::Unit, EntryPointAccess::Public, EntryPointType::Called,
            EntryPointPayment::Caller,
        ));
    }

    let admin = runtime::get_caller();
    let mut named_keys = NamedKeys::new();
    named_keys.insert("admin".into(),        storage::new_uref(admin.to_string()).into());
    named_keys.insert("oracle_count".into(), storage::new_uref(0u64).into());
    named_keys.insert("market".into(),       storage::new_uref(String::new()).into());

    let (contract_hash, _version) = storage::new_contract(
        entry_points,
        Some(named_keys),
        Some("helios_oracle_registry_hash".into()),
        Some("helios_oracle_registry_access".into()),
        None,
    );
    runtime::put_key("oracle_registry_contract_hash", contract_hash.into());
}

// ── Unit tests (host build — std enabled via dev-dependencies) ────────────────

#[cfg(test)]
mod tests {
    use super::compute_score;

    #[test]
    fn new_oracle_gets_neutral_score() {
        let score = compute_score(0, 0, 0);
        assert_eq!(score, (5_000u64 * 2_000 / 10_000) as u32);
    }

    #[test]
    fn perfect_accuracy_full_activity() {
        let score = compute_score(100, 10, 0);
        assert_eq!(score, 10_000);
    }

    #[test]
    fn partial_accuracy_and_activity() {
        let score = compute_score(50, 7, 3);
        let expected = (7_000u64 * 6_000 / 10_000) as u32;
        assert_eq!(score, expected);
    }
}
