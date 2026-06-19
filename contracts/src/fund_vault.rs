//! FundVault — operator-controlled treasury; the first paying DataMarket customer.
//!
//! Entry-points:
//!   deposit          (amount:u64)
//!   execute_rebalance(proposal_id:u64, targets:String, weights_bps:String)
//!   record_nav       (nav_motes:u64, yield_bps:u32)
//!   get_nav          ()               → writes to "result"
//!   set_governance   (governance_hash:String)  — operator only

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

// ── Pure arithmetic ───────────────────────────────────────────────────────────

pub fn nav_after_yield(nav_motes: u64, yield_bps: u32) -> u64 {
    nav_motes + (nav_motes as u128 * yield_bps as u128 / 10_000) as u64
}

pub fn weight_sum(weights: &[u32]) -> u32 { weights.iter().sum() }

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

// ── Phase 2: entry-point handlers ─────────────────────────────────────────────

#[cfg(feature = "fund-vault")]
#[no_mangle]
pub extern "C" fn deposit() {
    let amount: u64 = runtime::get_named_arg("amount");
    let nav_uref    = get_uref("nav_motes");
    let dep_uref    = get_uref("total_deposits");
    storage::write(nav_uref, read_u64(nav_uref) + amount);
    storage::write(dep_uref, read_u64(dep_uref) + amount);
}

#[cfg(feature = "fund-vault")]
#[no_mangle]
pub extern "C" fn execute_rebalance() {
    let proposal_id:  u64    = runtime::get_named_arg("proposal_id");
    let targets:      String = runtime::get_named_arg("targets");
    let weights_bps:  String = runtime::get_named_arg("weights_bps");

    let operator: String = read_str(get_uref("operator"));
    if runtime::get_caller().to_string() != operator {
        runtime::revert(casper_types::ApiError::User(2));
    }

    let weights: Vec<u32> = weights_bps
        .split(',')
        .filter_map(|s| s.trim().parse::<u32>().ok())
        .collect();
    if weights.iter().sum::<u32>() != 10_000 {
        runtime::revert(casper_types::ApiError::User(20));
    }

    let count_uref = get_uref("rebalance_count");
    let n = read_u64(count_uref);
    let rec_key  = format!("rebalance:{}", n);
    let rec_uref = storage::new_uref(
        format!("{}|{}|{}", proposal_id, targets, weights_bps)
    );
    runtime::put_key(&rec_key, rec_uref.into());
    storage::write(count_uref, n + 1);
}

#[cfg(feature = "fund-vault")]
#[no_mangle]
pub extern "C" fn record_nav() {
    let nav_motes: u64 = runtime::get_named_arg("nav_motes");
    let yield_bps: u32 = runtime::get_named_arg("yield_bps");

    let new_nav = nav_after_yield(nav_motes, yield_bps);
    storage::write(get_uref("nav_motes"), new_nav);

    let hist_count_key = "nav_history_count";
    let count_uref = match runtime::get_key(hist_count_key) {
        Some(k) => k.into_uref().unwrap_or_revert(),
        None    => {
            let u = storage::new_uref(0u64);
            runtime::put_key(hist_count_key, u.into());
            u
        }
    };
    let n = read_u64(count_uref);
    let hist_key  = format!("nav:{}", n);
    let hist_uref = storage::new_uref(format!("{}:{}", new_nav, yield_bps));
    runtime::put_key(&hist_key, hist_uref.into());
    storage::write(count_uref, n + 1);
}

#[cfg(feature = "fund-vault")]
#[no_mangle]
pub extern "C" fn get_nav() {
    let nav = read_u64(get_uref("nav_motes"));
    runtime::put_key("result", storage::new_uref(nav.to_string()).into());
}

#[cfg(feature = "fund-vault")]
#[no_mangle]
pub extern "C" fn set_governance() {
    let governance_hash: String = runtime::get_named_arg("governance_hash");
    let operator: String = read_str(get_uref("operator"));
    if runtime::get_caller().to_string() != operator {
        runtime::revert(casper_types::ApiError::User(2));
    }
    let gov_uref = match runtime::get_key("governance_hash") {
        Some(k) => k.into_uref().unwrap_or_revert(),
        None    => {
            let u = storage::new_uref(String::new());
            runtime::put_key("governance_hash", u.into());
            u
        }
    };
    storage::write(gov_uref, governance_hash);
}

// ── Phase 1: install ──────────────────────────────────────────────────────────

#[cfg(feature = "fund-vault")]
#[no_mangle]
pub extern "C" fn call() {
    let mut entry_points = EntryPoints::new();
    for (name, params) in &[
        ("deposit",           alloc::vec![Parameter::new("amount", CLType::U64)]),
        ("execute_rebalance", alloc::vec![
            Parameter::new("proposal_id",  CLType::U64),
            Parameter::new("targets",      CLType::String),
            Parameter::new("weights_bps",  CLType::String),
        ]),
        ("record_nav", alloc::vec![
            Parameter::new("nav_motes",  CLType::U64),
            Parameter::new("yield_bps",  CLType::U32),
        ]),
        ("get_nav",        alloc::vec![]),
        ("set_governance", alloc::vec![Parameter::new("governance_hash", CLType::String)]),
    ] {
        entry_points.add_entry_point(EntityEntryPoint::new(
            name.to_string(), params.clone(),
            CLType::Unit, EntryPointAccess::Public, EntryPointType::Called,
            EntryPointPayment::Caller,
        ));
    }

    let operator:        String = runtime::get_named_arg("operator");
    let governance_hash: String = runtime::get_named_arg("governance_hash");

    let mut named_keys = NamedKeys::new();
    named_keys.insert("operator".into(),        storage::new_uref(operator).into());
    named_keys.insert("governance_hash".into(), storage::new_uref(governance_hash).into());
    named_keys.insert("total_deposits".into(),  storage::new_uref(0u64).into());
    named_keys.insert("nav_motes".into(),       storage::new_uref(0u64).into());
    named_keys.insert("rebalance_count".into(), storage::new_uref(0u64).into());

    let (contract_hash, _) = storage::new_contract(
        entry_points, Some(named_keys),
        Some("helios_fund_vault_hash".into()),
        Some("helios_fund_vault_access".into()),
        None,
    );
    runtime::put_key("fund_vault_contract_hash", contract_hash.into());
}

// ── Unit tests ────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn nav_yield_calculation() {
        assert_eq!(nav_after_yield(100_000_000_000, 50), 100_500_000_000);
    }

    #[test]
    fn nav_yield_zero() {
        let nav = 200_000_000_000u64;
        assert_eq!(nav_after_yield(nav, 0), nav);
    }

    #[test]
    fn weight_sum_must_equal_10000() {
        assert_eq!(weight_sum(&[4_000, 3_000, 2_000, 1_000]), 10_000);
    }

    #[test]
    fn weight_sum_invalid_detected() {
        assert_ne!(weight_sum(&[5_000, 4_500]), 10_000);
    }
}
