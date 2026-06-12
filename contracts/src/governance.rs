//! Governance — propose / veto / finalize lifecycle for FundVault rebalances.
//!
//! Entry-points:
//!   propose       (description:String)    — proposer only
//!   veto          (proposal_id:u64)       — risk_agent only, within window
//!   finalize      (proposal_id:u64)       — anyone, after veto window
//!   get_proposal  (proposal_id:u64)       → writes to "result"
//!   proposal_count()                      → writes to "result"

#![allow(unused_imports)]
use casper_contract::{
    contract_api::{runtime, storage},
    unwrap_or_revert::UnwrapOrRevert,
};
use casper_types::{
    CLType, EntryPoint, EntryPointAccess, EntryPointType, EntryPoints,
    Parameter, URef, contracts::NamedKeys,
};

// ── Proposal state constants ──────────────────────────────────────────────────

pub const STATE_PENDING:  u8 = 0;
pub const STATE_APPROVED: u8 = 1;
pub const STATE_VETOED:   u8 = 2;

// ── Serialisation helpers (pure, shared with tests) ───────────────────────────

pub fn encode_proposal(description: &str, proposed_at_ms: u64, state: u8) -> String {
    format!("{}|{}|{}", description, proposed_at_ms, state)
}

pub fn decode_proposal(s: &str) -> Option<(String, u64, u8)> {
    let parts: Vec<&str> = s.splitn(3, '|').collect();
    if parts.len() != 3 { return None; }
    Some((parts[0].into(), parts[1].parse().ok()?, parts[2].parse().ok()?))
}

pub fn finalize_state(current: u8) -> u8 {
    if current == STATE_PENDING { STATE_APPROVED } else { current }
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

// ── Phase 2: entry-point handlers ─────────────────────────────────────────────

#[cfg(feature = "governance")]
#[no_mangle]
pub extern "C" fn propose() {
    let description: String = runtime::get_named_arg("description");

    let proposer: String = read_str(get_uref("proposer"));
    if runtime::get_caller().to_string() != proposer {
        runtime::revert(casper_types::ApiError::User(2));
    }

    let count_uref = get_uref("proposal_count");
    let id = read_u64(count_uref);

    // use block_time as timestamp (milliseconds since epoch)
    let now_ms = runtime::get_blocktime().into();
    let rec = encode_proposal(&description, now_ms, STATE_PENDING);
    let rec_uref = storage::new_uref(rec);
    runtime::put_key(&format!("proposal:{}", id), rec_uref.into());
    storage::write(count_uref, id + 1);
}

#[cfg(feature = "governance")]
#[no_mangle]
pub extern "C" fn veto() {
    let proposal_id: u64 = runtime::get_named_arg("proposal_id");

    let risk_agent: String = read_str(get_uref("risk_agent"));
    if runtime::get_caller().to_string() != risk_agent {
        runtime::revert(casper_types::ApiError::User(2));
    }

    let key      = format!("proposal:{}", proposal_id);
    let prop_uref = runtime::get_key(&key)
        .unwrap_or_revert_with(casper_types::ApiError::User(30))
        .into_uref()
        .unwrap_or_revert();

    let rec = read_str(prop_uref);
    let (desc, proposed_at, state) =
        decode_proposal(&rec).unwrap_or_revert_with(casper_types::ApiError::User(31));

    if state != STATE_PENDING {
        runtime::revert(casper_types::ApiError::User(32)); // AlreadyFinal
    }

    // check veto window still open
    let window_ms   = read_u64(get_uref("veto_window_ms"));
    let now_ms: u64 = runtime::get_blocktime().into();
    if now_ms > proposed_at + window_ms {
        runtime::revert(casper_types::ApiError::User(33)); // WindowClosed
    }

    storage::write(prop_uref, encode_proposal(&desc, proposed_at, STATE_VETOED));
}

#[cfg(feature = "governance")]
#[no_mangle]
pub extern "C" fn finalize() {
    let proposal_id: u64 = runtime::get_named_arg("proposal_id");

    let key      = format!("proposal:{}", proposal_id);
    let prop_uref = runtime::get_key(&key)
        .unwrap_or_revert_with(casper_types::ApiError::User(30))
        .into_uref()
        .unwrap_or_revert();

    let rec = read_str(prop_uref);
    let (desc, proposed_at, state) =
        decode_proposal(&rec).unwrap_or_revert_with(casper_types::ApiError::User(31));

    if state != STATE_PENDING {
        runtime::revert(casper_types::ApiError::User(32)); // AlreadyFinal
    }

    let window_ms   = read_u64(get_uref("veto_window_ms"));
    let now_ms: u64 = runtime::get_blocktime().into();
    if now_ms <= proposed_at + window_ms {
        runtime::revert(casper_types::ApiError::User(34)); // WindowStillOpen
    }

    storage::write(prop_uref, encode_proposal(&desc, proposed_at, STATE_APPROVED));
}

#[cfg(feature = "governance")]
#[no_mangle]
pub extern "C" fn get_proposal() {
    let proposal_id: u64 = runtime::get_named_arg("proposal_id");
    let key = format!("proposal:{}", proposal_id);
    let result = match runtime::get_key(&key) {
        Some(k) => read_str(k.into_uref().unwrap_or_revert()),
        None    => String::new(),
    };
    runtime::put_key("result", storage::new_uref(result).into());
}

#[cfg(feature = "governance")]
#[no_mangle]
pub extern "C" fn proposal_count() {
    let count = read_u64(get_uref("proposal_count"));
    runtime::put_key("result", storage::new_uref(count.to_string()).into());
}

// ── Phase 1: install ──────────────────────────────────────────────────────────

#[cfg(feature = "governance")]
#[no_mangle]
pub extern "C" fn call() {
    let mut entry_points = EntryPoints::new();
    for (name, params) in &[
        ("propose",        vec![Parameter::new("description", CLType::String)]),
        ("veto",           vec![Parameter::new("proposal_id", CLType::U64)]),
        ("finalize",       vec![Parameter::new("proposal_id", CLType::U64)]),
        ("get_proposal",   vec![Parameter::new("proposal_id", CLType::U64)]),
        ("proposal_count", vec![]),
    ] {
        entry_points.add_entry_point(EntryPoint::new(
            name.to_string(), params.clone(),
            CLType::Unit, EntryPointAccess::Public, EntryPointType::Contract,
        ));
    }

    let proposer:      String = runtime::get_named_arg("proposer");
    let risk_agent:    String = runtime::get_named_arg("risk_agent");
    let veto_window_ms: u64  = runtime::get_named_arg("veto_window_ms");

    let mut named_keys = NamedKeys::new();
    named_keys.insert("proposer".into(),       storage::new_uref(proposer).into());
    named_keys.insert("risk_agent".into(),     storage::new_uref(risk_agent).into());
    named_keys.insert("veto_window_ms".into(), storage::new_uref(veto_window_ms).into());
    named_keys.insert("proposal_count".into(), storage::new_uref(0u64).into());

    let (contract_hash, _) = storage::new_contract(
        entry_points, Some(named_keys),
        Some("helios_governance_hash".into()),
        Some("helios_governance_access".into()),
    );
    runtime::put_key("governance_contract_hash", contract_hash.into());
}

// ── Unit tests ────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn encode_decode_roundtrip() {
        let s = encode_proposal("Increase T-Bill weight to 60%", 1_700_000_000_000, STATE_PENDING);
        let (desc, at, state) = decode_proposal(&s).unwrap();
        assert_eq!(desc,  "Increase T-Bill weight to 60%");
        assert_eq!(at,    1_700_000_000_000);
        assert_eq!(state, STATE_PENDING);
    }

    #[test]
    fn finalize_pending_becomes_approved() {
        assert_eq!(finalize_state(STATE_PENDING), STATE_APPROVED);
    }

    #[test]
    fn finalize_vetoed_stays_vetoed() {
        assert_eq!(finalize_state(STATE_VETOED), STATE_VETOED);
    }

    #[test]
    fn finalize_approved_stays_approved() {
        assert_eq!(finalize_state(STATE_APPROVED), STATE_APPROVED);
    }
}
