extern crate alloc;
use alloc::string::{String, ToString};
use alloc::vec;
use casper_contract::{contract_api::{runtime, storage}, unwrap_or_revert::UnwrapOrRevert};
use casper_types::{CLType, EntityEntryPoint, EntryPointAccess, EntryPointType, EntryPointPayment, EntryPoints, Parameter, contracts::NamedKeys};

const STATE_PENDING: u8 = 0;
const STATE_APPROVED: u8 = 1;
const STATE_VETOED: u8 = 2;

fn get_uref(name: &str) -> casper_types::URef { runtime::get_key(name).unwrap_or_revert().into_uref().unwrap_or_revert() }
fn read_str(u: casper_types::URef) -> String { storage::read::<String>(u).unwrap_or_revert().unwrap_or_default() }
fn write_str(u: casper_types::URef, v: String) { storage::write(u, v); }
fn read_u64(u: casper_types::URef) -> u64 { storage::read::<u64>(u).unwrap_or_revert().unwrap_or(0) }
fn encode_proposal(desc: &str, at: u64, state: u8) -> String {
    let mut s = String::new();
    s.push_str(desc); s.push('|');
    s.push_str(&at.to_string()); s.push('|');
    s.push_str(&state.to_string()); s
}
fn decode_proposal(s: &str) -> Option<(String, u64, u8)> {
    let p: alloc::vec::Vec<&str> = s.splitn(3, '|').collect();
    if p.len() != 3 { return None; }
    Some((p[0].into(), p[1].parse().ok()?, p[2].parse().ok()?))
}

#[no_mangle]
pub extern "C" fn call() {
    let mut eps = EntryPoints::new();
    eps.add_entry_point(EntityEntryPoint::new("propose", vec![Parameter::new("description", CLType::String)], CLType::Unit, EntryPointAccess::Public, EntryPointType::Called, EntryPointPayment::Caller));
    eps.add_entry_point(EntityEntryPoint::new("veto", vec![Parameter::new("proposal_id", CLType::U64)], CLType::Unit, EntryPointAccess::Public, EntryPointType::Called, EntryPointPayment::Caller));
    eps.add_entry_point(EntityEntryPoint::new("finalize", vec![Parameter::new("proposal_id", CLType::U64)], CLType::Unit, EntryPointAccess::Public, EntryPointType::Called, EntryPointPayment::Caller));
    eps.add_entry_point(EntityEntryPoint::new("get_proposal", vec![Parameter::new("proposal_id", CLType::U64)], CLType::Unit, EntryPointAccess::Public, EntryPointType::Called, EntryPointPayment::Caller));
    eps.add_entry_point(EntityEntryPoint::new("proposal_count", vec![], CLType::Unit, EntryPointAccess::Public, EntryPointType::Called, EntryPointPayment::Caller));
    let proposer: String = runtime::get_named_arg("proposer");
    let risk_agent: String = runtime::get_named_arg("risk_agent");
    let veto_window_ms: u64 = runtime::get_named_arg("veto_window_ms");
    let mut nk = NamedKeys::new();
    nk.insert("proposer".into(), storage::new_uref(proposer).into());
    nk.insert("risk_agent".into(), storage::new_uref(risk_agent).into());
    nk.insert("veto_window_ms".into(), storage::new_uref(veto_window_ms).into());
    nk.insert("proposal_count".into(), storage::new_uref(0u64).into());
    let (hash, _) = storage::new_contract(eps, Some(nk), Some("helios_governance_hash".into()), Some("helios_governance_access".into()), None);
    runtime::put_key("governance_contract_hash", hash.into());
}

#[no_mangle]
pub extern "C" fn propose() {
    let description: String = runtime::get_named_arg("description");
    let count_uref = get_uref("proposal_count");
    let id = read_u64(count_uref);
    let now_ms: u64 = runtime::get_blocktime().into();
    let rec = encode_proposal(&description, now_ms, STATE_PENDING);
    let rec_key = alloc::format!("proposal:{}", id);
    runtime::put_key(&rec_key, storage::new_uref(rec).into());
    storage::write(count_uref, id + 1);
}

#[no_mangle]
pub extern "C" fn veto() {
    let proposal_id: u64 = runtime::get_named_arg("proposal_id");
    let key = alloc::format!("proposal:{}", proposal_id);
    let prop_uref = runtime::get_key(&key).unwrap_or_revert().into_uref().unwrap_or_revert();
    let rec = read_str(prop_uref);
    let (desc, proposed_at, state) = decode_proposal(&rec).unwrap_or_else(|| runtime::revert(casper_types::ApiError::User(31)));
    if state != STATE_PENDING { runtime::revert(casper_types::ApiError::User(32)); }
    let window_ms = read_u64(get_uref("veto_window_ms"));
    let now_ms: u64 = runtime::get_blocktime().into();
    if now_ms > proposed_at + window_ms { runtime::revert(casper_types::ApiError::User(33)); }
    write_str(prop_uref, encode_proposal(&desc, proposed_at, STATE_VETOED));
}

#[no_mangle]
pub extern "C" fn finalize() {
    let proposal_id: u64 = runtime::get_named_arg("proposal_id");
    let key = alloc::format!("proposal:{}", proposal_id);
    let prop_uref = runtime::get_key(&key).unwrap_or_revert().into_uref().unwrap_or_revert();
    let rec = read_str(prop_uref);
    let (desc, proposed_at, state) = decode_proposal(&rec).unwrap_or_else(|| runtime::revert(casper_types::ApiError::User(31)));
    if state != STATE_PENDING { runtime::revert(casper_types::ApiError::User(32)); }
    let window_ms = read_u64(get_uref("veto_window_ms"));
    let now_ms: u64 = runtime::get_blocktime().into();
    if now_ms <= proposed_at + window_ms { runtime::revert(casper_types::ApiError::User(34)); }
    write_str(prop_uref, encode_proposal(&desc, proposed_at, STATE_APPROVED));
}

#[no_mangle]
pub extern "C" fn get_proposal() {
    let proposal_id: u64 = runtime::get_named_arg("proposal_id");
    let key = alloc::format!("proposal:{}", proposal_id);
    let result = match runtime::get_key(&key) {
        Some(k) => read_str(k.into_uref().unwrap_or_revert()),
        None => String::new(),
    };
    runtime::put_key("result", storage::new_uref(result).into());
}

#[no_mangle]
pub extern "C" fn proposal_count() {
    let count = read_u64(get_uref("proposal_count"));
    runtime::put_key("result", storage::new_uref(count).into());
}
