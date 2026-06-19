#!/usr/bin/env python3
"""Pre-deploy gate: verify Casper contract wasm files are structurally valid.

Two checks per file (both required by Casper VM v1):
  1. exports `call` as a function  — the Casper ABI entry-point
  2. has an internal memory section — Casper forbids --import-memory;
     the VM requires the memory to be defined inside the wasm, not imported

Zero dependencies (no wabt / wasm-objdump needed) — parses wasm binary directly.

Usage:
    python3 scripts/check_wasm_exports.py contracts/wasm/*.wasm
Exit 0 = all good; 1 = at least one wasm is broken.
"""
import sys
import struct


def read_leb128(buf: bytes, pos: int):
    result, shift = 0, 0
    while True:
        b = buf[pos]; pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7


def parse_wasm(path: str):
    """Return (exports, has_memory_section, has_import_memory).

    exports           — list of (name, kind_str) for all exports
    has_memory_section — True if section id=5 (Memory) exists with ≥1 entry
    has_import_memory  — True if any import has external_kind=2 (Memory);
                         this is the --import-memory footprint Casper rejects
    """
    with open(path, "rb") as f:
        data = f.read()

    if data[:4] != b"\x00asm":
        raise ValueError(f"{path}: not a wasm file (bad magic bytes)")
    version = struct.unpack("<I", data[4:8])[0]
    if version != 1:
        raise ValueError(f"{path}: unsupported wasm version {version}")

    exports = []
    has_memory_section = False
    has_import_memory  = False

    pos = 8
    while pos < len(data):
        sec_id = data[pos]; pos += 1
        sec_size, pos = read_leb128(data, pos)
        sec_end = pos + sec_size

        if sec_id == 2:  # Import section — look for memory imports
            count, p = read_leb128(data, pos)
            for _ in range(count):
                # module string
                mod_len, p = read_leb128(data, p)
                p += mod_len
                # field string
                fld_len, p = read_leb128(data, p)
                p += fld_len
                # external_kind: 0=func 1=table 2=memory 3=global
                kind = data[p]; p += 1
                if kind == 2:   # memory import
                    has_import_memory = True
                elif kind == 0:  # func import — skip type index
                    _, p = read_leb128(data, p)
                elif kind == 1:  # table import — skip table type
                    p += 1  # elem_type
                    flags = data[p]; p += 1
                    _, p = read_leb128(data, p)  # initial
                    if flags & 1:
                        _, p = read_leb128(data, p)  # maximum
                elif kind == 3:  # global import — skip valtype + mutability
                    p += 2

        elif sec_id == 5:  # Memory section
            count, p = read_leb128(data, pos)
            if count > 0:
                has_memory_section = True

        elif sec_id == 7:  # Export section
            count, p = read_leb128(data, pos)
            for _ in range(count):
                name_len, p = read_leb128(data, p)
                name = data[p: p + name_len].decode("utf-8", "replace"); p += name_len
                kind = data[p]; p += 1
                _idx, p = read_leb128(data, p)
                kind_str = {0:"func", 1:"table", 2:"memory", 3:"global"}.get(kind, f"kind{kind}")
                exports.append((name, kind_str))

        pos = sec_end

    return exports, has_memory_section, has_import_memory


def check_file(path: str) -> list[str]:
    """Return list of error strings (empty = OK)."""
    errors = []
    try:
        exports, has_mem, has_import_mem = parse_wasm(path)
    except (OSError, ValueError) as exc:
        return [str(exc)]

    func_exports = [n for n, k in exports if k == "func"]

    # Check 1: must export `call`
    if "call" not in func_exports:
        listing = ", ".join(func_exports) or "(none)"
        msg = f"no `call` export — found [{listing}]"
        if "main" in func_exports:
            msg += "\n     ^ built from [[bin]] target (not the contract lib)"
        errors.append(msg)

    # Check 2: must NOT use --import-memory (no memory import)
    if has_import_mem:
        errors.append(
            "compiled with --import-memory (RUSTFLAGS contains '-C link-arg=--import-memory')\n"
            "     Casper VM requires an internal memory section — remove that flag and rebuild.\n"
            "     Quick fix:  unset RUSTFLAGS && bash scripts/build_contracts.sh"
        )

    # Check 3: must HAVE a memory section (complement of check 2, but also
    # catches stripped/malformed wasms that lost their memory section)
    if not has_mem and not has_import_mem:
        errors.append(
            "no memory section found — wasm may be malformed or stripped incorrectly.\n"
            "     Rebuild from source without --strip-all; use wasm-opt -Oz instead."
        )

    return errors


# Expected exports per contract (call + all entry-point handlers + memory section)
EXPECTED_EXPORTS = {
    "OracleRegistry.wasm": {"call","register","post_attestation","credit_settlement",
                             "score_attestation","set_market","get_oracle","get_reputation"},
    "DataMarket.wasm":     {"call","list_feed","purchase","anchor_x402_receipt",
                             "set_fee_bps","get_listing","listing_count"},
    "FundVault.wasm":      {"call","deposit","execute_rebalance","record_nav",
                             "get_nav","set_governance"},
    "Governance.wasm":     {"call","propose","veto","finalize",
                             "get_proposal","proposal_count"},
}


def main(paths):
    if not paths:
        print("usage: check_wasm_exports.py <file.wasm> [...]", file=sys.stderr)
        return 1
    bad = 0
    for path in paths:
        errors = check_file(path)
        if errors:
            bad += 1
            for i, e in enumerate(errors):
                print(f"{'FAIL' if i==0 else '    '}  {path}: {e}")
        else:
            try:
                exports, _, _ = parse_wasm(path)
                func_names_set = {n for n, k in exports if k == "func"}
                func_names = ", ".join(sorted(func_names_set))
                # warn about missing entry-point handlers
                import os
                basename = os.path.basename(path)
                expected = EXPECTED_EXPORTS.get(basename, set())
                missing_eps = expected - func_names_set
                if missing_eps:
                    print(f"WARN  {path}: missing entry-point exports: {sorted(missing_eps)}")
                    print( "      (will cause 'Function not found' on-chain — rebuild after adding handlers)")
                    bad += 1
                else:
                    print(f"OK    {path}: exports [{func_names}] + memory section ✓")
            except Exception:
                print(f"OK    {path}")
    if bad:
        print(f"\n{bad} wasm file(s) REJECTED — fix before deploying.")
        return 1
    print(f"\nAll {len(paths)} wasm file(s) passed Casper pre-deploy checks.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
