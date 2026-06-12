#!/usr/bin/env python3
"""Pre-deploy gate: verify Casper contract wasm files are structurally valid.

Three-layer check (all required by Casper VM v1):
  Layer 1: exports `call` as a function — the Casper ABI entry-point.
  Layer 2: has an internal memory section — Casper forbids --import-memory;
           the VM requires the memory to be defined inside the wasm, not imported.
  Layer 3: ALL declared entry-point handlers are present in the wasm export table.
           (Prevents the "Function not found" error when metadata is registered
           but the actual #[no_mangle] pub extern "C" fn is missing or stripped).

Zero dependencies (no wabt / wasm-objdump needed) — parses wasm binary directly.

Usage:
    python3 scripts/check_wasm_exports.py contracts/wasm/*.wasm
Exit 0 = all good; 1 = at least one wasm is broken.
"""

import sys
import struct
import os

# Expected exports per contract (call + all entry-point handlers)
EXPECTED_EXPORTS = {
    "OracleRegistry.wasm": {
        "call",
        "register",
        "post_attestation",
        "credit_settlement",
        "score_attestation",
        "set_market",
        "get_oracle",
        "get_reputation",
    },
    "DataMarket.wasm": {
        "call",
        "list_feed",
        "purchase",
        "anchor_x402_receipt",
        "set_fee_bps",
        "get_listing",
        "listing_count",
    },
    "FundVault.wasm": {
        "call",
        "deposit",
        "execute_rebalance",
        "record_nav",
        "get_nav",
        "set_governance",
    },
    "Governance.wasm": {
        "call",
        "propose",
        "veto",
        "finalize",
        "get_proposal",
        "proposal_count",
    },
}


def read_leb128(buf: bytes, pos: int):
    result, shift = 0, 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7


def parse_wasm(path: str):
    """Return (exports, has_memory_section, has_import_memory)."""
    with open(path, "rb") as f:
        data = f.read()

    if data[:4] != b"\x00asm":
        raise ValueError(f"{path}: not a wasm file (bad magic bytes)")
    version = struct.unpack("<I", data[4:8])[0]
    if version != 1:
        raise ValueError(f"{path}: unsupported wasm version {version}")

    exports = []
    has_memory_section = False
    has_import_memory = False

    pos = 8
    while pos < len(data):
        sec_id = data[pos]
        pos += 1
        sec_size, pos = read_leb128(data, pos)
        sec_end = pos + sec_size

        if sec_id == 2:  # Import section
            count, p = read_leb128(data, pos)
            for _ in range(count):
                mod_len, p = read_leb128(data, p)
                p += mod_len
                fld_len, p = read_leb128(data, p)
                p += fld_len
                kind = data[p]
                p += 1
                if kind == 2:  # memory import
                    has_import_memory = True
                elif kind == 0:  # func import
                    _, p = read_leb128(data, p)
                elif kind == 1:  # table import
                    p += 1
                    flags = data[p]
                    p += 1
                    _, p = read_leb128(data, p)
                    if flags & 1:
                        _, p = read_leb128(data, p)
                elif kind == 3:  # global import
                    p += 2

        elif sec_id == 5:  # Memory section
            count, p = read_leb128(data, pos)
            if count > 0:
                has_memory_section = True

        elif sec_id == 7:  # Export section
            count, p = read_leb128(data, pos)
            for _ in range(count):
                name_len, p = read_leb128(data, p)
                name = data[p : p + name_len].decode("utf-8", "replace")
                p += name_len
                kind = data[p]
                p += 1
                _idx, p = read_leb128(data, p)
                kind_str = {0: "func", 1: "table", 2: "memory", 3: "global"}.get(
                    kind, f"kind{kind}"
                )
                exports.append((name, kind_str))

        pos = sec_end

    return exports, has_memory_section, has_import_memory


def check_file(path: str):
    """Return (errors_list, func_names_set, has_mem)."""
    errors = []
    try:
        exports, has_mem, has_import_mem = parse_wasm(path)
    except (OSError, ValueError) as exc:
        return [str(exc)], set(), False

    func_names_set = {n for n, k in exports if k == "func"}
    basename = os.path.basename(path)
    expected = EXPECTED_EXPORTS.get(basename, set())

    # Layer 1: call export exists
    if "call" not in func_names_set:
        listing = ", ".join(sorted(func_names_set)) or "(none)"
        msg = f"[Layer 1 FAIL] no `call` export — found [{listing}]"
        if "main" in func_names_set:
            msg += "\n             ^ built from [[bin]] target (not the contract lib)"
        errors.append(msg)

    # Layer 2: memory section exists AND no memory import
    if has_import_mem:
        errors.append(
            "[Layer 2 FAIL] compiled with --import-memory\n"
            "             Casper VM requires an internal memory section. Remove that flag and rebuild.\n"
            "             Quick fix: unset RUSTFLAGS && bash scripts/build_contracts.sh"
        )
    elif not has_mem:
        errors.append(
            "[Layer 2 FAIL] no memory section found\n"
            "             wasm may be malformed or stripped incorrectly.\n"
            "             Rebuild from source without --strip-all; use wasm-opt -Oz instead."
        )

    # Layer 3: all declared entry-point handlers are present
    missing_eps = expected - func_names_set
    if missing_eps:
        errors.append(
            f"[Layer 3 FAIL] missing entry-point exports: {sorted(missing_eps)}\n"
            f"             This will cause 'Function not found' on-chain.\n"
            f'             Ensure #[no_mangle] pub extern "C" fn exists for each, and feature flags are enabled during build.'
        )

    return errors, func_names_set, has_mem


def main(paths):
    if not paths:
        print("usage: check_wasm_exports.py <file.wasm> [...]", file=sys.stderr)
        return 1

    bad = 0
    for path in paths:
        errors, func_names_set, has_mem = check_file(path)

        if errors:
            bad += 1
            for i, e in enumerate(errors):
                print(f"{'FAIL' if i == 0 else '    '}  {path}: {e}")
        else:
            func_names = ", ".join(sorted(func_names_set))
            print(f"OK    {path}: exports [{func_names}] + memory section ✓")

    if bad:
        print(f"\n{bad} wasm file(s) REJECTED — fix before deploying.")
        return 1

    print(f"\nAll {len(paths)} wasm file(s) passed Casper pre-deploy 3-layer checks.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
