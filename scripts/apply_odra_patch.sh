#!/usr/bin/env bash
# One-shot fixer for the Odra build traps — v2, VERSION-AWARE.
# Run ON YOUR MACHINE (requires network). Idempotent.
#
# Why v2: cargo's `[patch]` only engages when the patched version EXACTLY
# matches what the resolver picked. Our original `version = "1.4.0"` means
# `^1.4.0`, so cargo silently resolved odra 1.5.1 and IGNORED a 1.4.0 patch.
# v2 pins exact versions with `=`, vendors the matching tag, and — crucially —
# VERIFIES the patch actually took over before declaring success.
#
# Usage:
#   bash scripts/apply_odra_patch.sh            # default ODRA_VER=1.5.1
#   ODRA_VER=1.4.0 bash scripts/apply_odra_patch.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONTRACTS="$ROOT/contracts"
VENDOR="$CONTRACTS/vendor/odra"
ODRA_VER="${ODRA_VER:-1.5.1}"
ODRA_TAG="${ODRA_TAG:-v${ODRA_VER}}"

echo "== target odra version: ${ODRA_VER} (tag ${ODRA_TAG}) =="

echo "== [1/5] pinning exact versions (=${ODRA_VER}) in Cargo.toml =="
python3 - "$CONTRACTS/Cargo.toml" "$ODRA_VER" <<'EOF'
import pathlib, re, sys
p, ver = pathlib.Path(sys.argv[1]), sys.argv[2]
text = p.read_text()
new = re.sub(
    r'((?:odra|odra-test|odra-build)\s*=\s*\{\s*version\s*=\s*")[=^]?[\d.]+(")',
    lambda m: m.group(1) + "=" + ver + m.group(2),
    text,
)
if new != text:
    p.write_text(new)
    print(f"   pinned odra / odra-test / odra-build to ={ver}")
else:
    print("   already pinned — skipping")
EOF

echo "== [2/5] vendoring odra source =="
if [ -d "$VENDOR" ]; then
  CUR_TAG="$(git -C "$VENDOR" describe --tags 2>/dev/null || echo unknown)"
  if [ "$CUR_TAG" != "$ODRA_TAG" ]; then
    echo "   existing vendor is ${CUR_TAG}, need ${ODRA_TAG} — re-cloning"
    rm -rf "$VENDOR"
  fi
fi
if [ ! -d "$VENDOR" ]; then
  git clone --depth 1 --branch "$ODRA_TAG" https://github.com/odradev/odra "$VENDOR" || {
    echo "!! clone failed. List real tags with:" >&2
    echo "   git ls-remote --tags https://github.com/odradev/odra | grep ${ODRA_VER}" >&2
    echo "   then: ODRA_VER=${ODRA_VER} ODRA_TAG=<actual> bash scripts/apply_odra_patch.sh" >&2
    exit 1
  }
else
  echo "   vendor already at ${ODRA_TAG} — skipping clone"
fi

echo "== [3/5] removing #[no_mangle] from the panic handler =="
python3 - "$VENDOR" <<'EOF'
import pathlib, re, sys
vendor = pathlib.Path(sys.argv[1])
hits = 0
for rs in vendor.rglob("*.rs"):
    if "wasm-env" not in str(rs):
        continue
    text = rs.read_text()
    if "#[panic_handler]" not in text:
        continue
    lines = text.splitlines(keepends=True)
    out, removed = [], 0
    for i, line in enumerate(lines):
        if re.match(r"\s*#\[no_mangle\]\s*$", line) and any(
            "#[panic_handler]" in lines[j]
            for j in range(i + 1, min(i + 4, len(lines)))
        ):
            removed += 1
            continue
        out.append(line)
    if removed:
        rs.write_text("".join(out))
        print(f"   patched {rs} (removed {removed} line)")
        hits += removed
if hits == 0:
    print("   nothing to remove (already patched, or fixed upstream) — continuing")
EOF

echo "== [4/5] enabling [patch.crates-io] =="
# NOTE: With [[bin]] and odra-build removed from Cargo.toml, the version-mismatch
# trap (Trap 4) and the odra-schema conflict (Trap 5) are both eliminated.
# This patch script is now only needed if you re-introduce odra as a dependency.
python3 - "$CONTRACTS/Cargo.toml" <<'EOF'
import pathlib, sys
p = pathlib.Path(sys.argv[1])
text = p.read_text()
if "\n[patch.crates-io]" in text:
    print("   patch block already active — skipping")
else:
    text = text.replace("# [patch.crates-io]", "[patch.crates-io]")
    text = text.replace(
        '# odra-casper-wasm-env = { path = "vendor/odra/odra-casper/wasm-env" }',
        'odra-casper-wasm-env = { path = "vendor/odra/odra-casper/wasm-env" }',
    )
    p.write_text(text)
    print("   patch block enabled")
EOF
# locate the wasm-env crate inside the vendored workspace (layout can shift between tags)
WASMENV_DIR="$(dirname "$(grep -rl --include=Cargo.toml '^name = "odra-casper-wasm-env"' "$VENDOR" | head -1)")" || true
if [ -n "${WASMENV_DIR:-}" ] && [ "$WASMENV_DIR" != "$VENDOR/odra-casper/wasm-env" ]; then
  REL="vendor/odra${WASMENV_DIR#$VENDOR}"
  echo "   wasm-env lives at non-default path — fixing patch path to ${REL}"
  python3 - "$CONTRACTS/Cargo.toml" "$REL" <<'EOF'
import pathlib, sys
p, rel = pathlib.Path(sys.argv[1]), sys.argv[2]
text = p.read_text().replace("vendor/odra/odra-casper/wasm-env", rel)
p.write_text(text)
EOF
fi

echo "== [5/5] resolving deps + VERIFYING the patch actually engaged =="
cd "$CONTRACTS"
cargo update -p base64ct --precise 1.6.0 2>/dev/null || cargo update
if cargo tree -i odra-casper-wasm-env 2>/dev/null | grep -q "vendor"; then
  echo "   ✓ patch ENGAGED — odra-casper-wasm-env resolves to vendor/"
else
  echo "!! patch NOT engaged — cargo is still compiling the registry copy." >&2
  echo "   Diagnose with:  cargo tree -i odra-casper-wasm-env" >&2
  echo "   The version it shows MUST equal ${ODRA_VER} and point into vendor/." >&2
  echo "   If it shows a different version, re-run with ODRA_VER=<that version>." >&2
  exit 1
fi
[ -f Cargo.lock ] && echo "   ✓ Cargo.lock generated"

echo
echo "Done. Next:"
echo "  1. cargo odra test                                         # 13 tests"
echo "  2. cargo odra build"
echo "  3. python3 ../scripts/check_wasm_exports.py wasm/*.wasm    # 4x OK required"
echo "  4. git add contracts/vendor contracts/Cargo.lock contracts/Cargo.toml"
echo "     git commit -m 'fix: pin odra =${ODRA_VER}, vendored wasm-env patch, locked deps'"
