extern crate alloc;
use alloc::string::{String, ToString};
use alloc::vec;
use casper_contract::{contract_api::{runtime, storage}, unwrap_or_revert::UnwrapOrRevert};
use casper_types::{CLType, EntityEntryPoint, EntryPointAccess, EntryPointType, EntryPointPayment, EntryPoints, Parameter, contracts::NamedKeys};

fn get_uref(name: &str) -> casper_types::URef { runtime::get_key(name).unwrap_or_revert().into_uref().unwrap_or_revert() }
fn read_str(u: casper_types::URef) -> String { storage::read::<String>(u).unwrap_or_revert().unwrap_or_default() }
fn read_u64(u: casper_types::URef) -> u64 { storage::read::<u64>(u).unwrap_or_revert().unwrap_or(0) }

#[no_mangle]
pub extern "C" fn call() {
    let mut eps = EntryPoints::new();
    eps.add_entry_point(EntityEntryPoint::new("deposit", vec![Parameter::new("amount", CLType::U64)], CLType::Unit, EntryPointAccess::Public, EntryPointType::Called, EntryPointPayment::Caller));
    eps.add_entry_point(EntityEntryPoint::new("execute_rebalance", vec![Parameter::new("proposal_id", CLType::U64), Parameter::new("targets", CLType::String), Parameter::new("weights_bps", CLType::String)], CLType::Unit, EntryPointAccess::Public, EntryPointType::Called, EntryPointPayment::Caller));
    eps.add_entry_point(EntityEntryPoint::new("record_nav", vec![Parameter::new("nav_motes", CLType::U64), Parameter::new("yield_bps", CLType::U32)], CLType::Unit, EntryPointAccess::Public, EntryPointType::Called, EntryPointPayment::Caller));
    eps.add_entry_point(EntityEntryPoint::new("get_nav", vec![], CLType::Unit, EntryPointAccess::Public, EntryPointType::Called, EntryPointPayment::Caller));
    eps.add_entry_point(EntityEntryPoint::new("set_governance", vec![Parameter::new("governance_hash", CLType::String)], CLType::Unit, EntryPointAccess::Public, EntryPointType::Called, EntryPointPayment::Caller));
    let operator: String = runtime::get_named_arg("operator");
    let governance_hash: String = runtime::get_named_arg("governance_hash");
    let mut nk = NamedKeys::new();
    nk.insert("operator".into(), storage::new_uref(operator).into());
    nk.insert("governance_hash".into(), storage::new_uref(governance_hash).into());
    nk.insert("total_deposits".into(), storage::new_uref(0u64).into());
    nk.insert("nav_motes".into(), storage::new_uref(0u64).into());
    nk.insert("rebalance_count".into(), storage::new_uref(0u64).into());
    let (hash, _) = storage::new_contract(eps, Some(nk), Some("helios_fund_vault_hash".into()), Some("helios_fund_vault_access".into()), None);
    runtime::put_key("fund_vault_contract_hash", hash.into());
}

#[no_mangle]
pub extern "C" fn deposit() {
    let amount: u64 = runtime::get_named_arg("amount");
    let nav_uref = get_uref("nav_motes");
    let dep_uref = get_uref("total_deposits");
    storage::write(nav_uref, read_u64(nav_uref) + amount);
    storage::write(dep_uref, read_u64(dep_uref) + amount);
}

#[no_mangle]
pub extern "C" fn execute_rebalance() {
    let proposal_id: u64 = runtime::get_named_arg("proposal_id");
    let targets: String = runtime::get_named_arg("targets");
    let weights_bps: String = runtime::get_named_arg("weights_bps");
    let count_uref = get_uref("rebalance_count");
    let n = read_u64(count_uref);
    let rec_key = alloc::format!("rebalance:{}", n);
    let rec_val = alloc::format!("{}|{}|{}", proposal_id, targets, weights_bps);
    runtime::put_key(&rec_key, storage::new_uref(rec_val).into());
    storage::write(count_uref, n + 1);
}

#[no_mangle]
pub extern "C" fn record_nav() {
    let nav_motes: u64 = runtime::get_named_arg("nav_motes");
    let yield_bps: u32 = runtime::get_named_arg("yield_bps");
    let new_nav = nav_motes + (nav_motes as u128 * yield_bps as u128 / 10000) as u64;
    storage::write(get_uref("nav_motes"), new_nav);
}

#[no_mangle]
pub extern "C" fn get_nav() {
    let nav = read_u64(get_uref("nav_motes"));
    runtime::put_key("result", storage::new_uref(nav).into());
}

#[no_mangle]
pub extern "C" fn set_governance() {
    let governance_hash: String = runtime::get_named_arg("governance_hash");
    let gov_uref = match runtime::get_key("governance_hash") {
        Some(k) => k.into_uref().unwrap_or_revert(),
        None => { let u = storage::new_uref(String::new()); runtime::put_key("governance_hash", u.into()); u }
    };
    storage::write(gov_uref, governance_hash);
}
