#!/usr/bin/env python3
"""Verify Casper WASM compatibility.

Checks:
  1. The WASM exports a `call` function (required by Casper VM)
  2. No bulk-memory instructions (0xFC 0x08-0x0B) — not supported by Casper wasmi

Usage:
  python3 scripts/check_wasm_exports.py contracts/wasm/*.wasm
"""

import struct
import sys
from pathlib import Path


def parse_leb128_u(data: bytes, pos: int) -> tuple[int, int]:
    """Parse unsigned LEB128, return (value, new_pos)."""
    result, shift = 0, 0
    while True:
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return result, pos
        shift += 7


def check_wasm(path: str) -> list[str]:
    """Return list of error strings (empty = OK)."""
    errors = []
    data = Path(path).read_bytes()
    name = Path(path).name

    # ── Check WASM magic ──────────────────────────────────────────────────────
    if data[:4] != b"\x00asm":
        return [f"{name}: not a valid WASM file"]

    # ── Scan for bulk-memory instructions (0xFC 0x08-0x0B) ───────────────────
    # 0xFC is the prefix byte for extended instructions.
    # Bulk-memory ops (0x08-0x0B) are NOT supported by Casper wasmi.
    # Saturating float-to-int (0x00-0x07) ARE supported.
    bulk_memory_ops = {
        0x08: "memory.init",
        0x09: "data.drop",
        0x0A: "memory.copy",
        0x0B: "memory.fill",
    }
    found_bulk = []
    for i in range(len(data) - 1):
        if data[i] == 0xFC and data[i + 1] in bulk_memory_ops:
            found_bulk.append(bulk_memory_ops[data[i + 1]])

    if found_bulk:
        errors.append(
            f"{name}: FAIL — {len(found_bulk)} bulk-memory instruction(s) found: "
            f"{', '.join(set(found_bulk))}. "
            f"Casper VM will reject this WASM. "
            f"Fix: rebuild with RUSTFLAGS='-C target-feature=-bulk-memory,-bulk-memory-opt,-reference-types'"
        )

    # ── Parse export section to find `call` ───────────────────────────────────
    pos = 8  # skip magic + version
    found_call = False

    while pos < len(data):
        if pos >= len(data):
            break
        section_id = data[pos]
        pos += 1
        section_len, pos = parse_leb128_u(data, pos)
        section_end = pos + section_len

        if section_id == 7:  # Export section
            count, pos = parse_leb128_u(data, pos)
            for _ in range(count):
                name_len, pos = parse_leb128_u(data, pos)
                export_name = data[pos : pos + name_len].decode(
                    "utf-8", errors="replace"
                )
                pos += name_len
                export_kind = data[pos]
                pos += 1
                _export_idx, pos = parse_leb128_u(data, pos)
                if export_name == "call" and export_kind == 0:  # 0 = function
                    found_call = True
            break

        pos = section_end

    if not found_call:
        errors.append(
            f"{name}: FAIL — no `call` export found. "
            f"Casper VM requires a `call` function export. "
            f"Check that the correct --features flag was used."
        )

    return errors


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python3 check_wasm_exports.py <file.wasm> [...]")
        return 1

    all_errors = []
    for path in sys.argv[1:]:
        errors = check_wasm(path)
        if errors:
            for e in errors:
                print(f"  FAIL  {e}")
            all_errors.extend(errors)
        else:
            size = Path(path).stat().st_size
            print(
                f"  OK    {Path(path).name}: exports `call`, "
                f"no bulk-memory ({size:,} bytes)"
            )

    if all_errors:
        print(f"\n{len(all_errors)} check(s) failed.")
        print("\nTo fix bulk-memory errors:")
        print("  1. Ensure contracts/.cargo/config.toml contains:")
        print("       [target.wasm32-unknown-unknown]")
        print(
            '       rustflags = ["-C", "target-feature=-bulk-memory,-bulk-memory-opt,-reference-types", "-C", "link-arg=--allow-undefined"]'
        )
        print("  2. Delete contracts/target/ and rebuild:")
        print("       rm -rf contracts/target/")
        print("       bash scripts/build_contracts.sh")
        return 1

    print(f"\nAll {len(sys.argv) - 1} WASM file(s) are Casper-compatible ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
