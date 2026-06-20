#!/usr/bin/env python3
"""
Helios Casper Testnet Deployer — pure Python, zero Rust/casper-client required.

Usage examples:
  python3 scripts/casper_deploy.py status
  python3 scripts/casper_deploy.py keygen --out keys/
  python3 scripts/casper_deploy.py install --key keys/fund_agent/secret_key.pem \
      --wasm contracts/wasm/OracleRegistry.wasm --wait
  python3 scripts/casper_deploy.py call --key keys/oracle_tbill/secret_key.pem \
      --contract <HASH> --entry-point register \
      --args "name:string=TBill Oracle" "category:string=rwa" \
             "endpoint:string=http://localhost:8451/quote" "price_motes:u64=2000000000"
  python3 scripts/casper_deploy.py wait <deploy_hash>
"""

from __future__ import annotations
import argparse, hashlib, json, os, struct, sys, time, urllib.error, urllib.request
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.ec import (
    EllipticCurvePrivateKey,
    SECP256K1,
    derive_private_key,
    ECDSA,
)
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
    PrivateFormat,
    NoEncryption,
    load_pem_private_key,
)

# ── Constants ─────────────────────────────────────────────────────────────────
NODES = [
    "https://node.testnet.casper.network",
    "https://rpc.testnet.cspr.cloud",
]
CHAIN = "casper-test"
EXPLORER = "https://testnet.cspr.live"


def _rpc_url(node: str) -> str:
    """Build the RPC endpoint URL. Some providers use /rpc suffix, some don't."""
    if node.endswith("/rpc"):
        return node
    return f"{node}/rpc"


# ── Casper serialisation helpers ──────────────────────────────────────────────


def _u32(n: int) -> bytes:
    return struct.pack("<I", n)


def _u64(n: int) -> bytes:
    return struct.pack("<Q", n)


def _len_prefix(b: bytes) -> bytes:
    return _u32(len(b)) + b


def _str(s: str) -> bytes:
    return _len_prefix(s.encode())


def _u512(n: int) -> bytes:
    if n == 0:
        return b"\x00"
    b = n.to_bytes((n.bit_length() + 7) // 8, "little")
    return bytes([len(b)]) + b


_CL_BOOL = b"\x00"
_CL_U32 = b"\x04"
_CL_U64 = b"\x05"
_CL_U512 = b"\x08"
_CL_STRING = b"\x0a"


def _cl(cl_type: bytes, value_bytes: bytes) -> bytes:
    return _len_prefix(value_bytes) + cl_type


def _named_arg(name: str, cl_type: bytes, value_bytes: bytes) -> bytes:
    return _str(name) + _cl(cl_type, value_bytes)


# ── Key utilities ─────────────────────────────────────────────────────────────


class CasperKey:
    """Unified key wrapper supporting both ed25519 and secp256k1."""

    def __init__(self, path: str):
        pem = Path(path).read_bytes()
        self._raw = load_pem_private_key(pem, password=None)
        if isinstance(self._raw, Ed25519PrivateKey):
            self._tag = 0x01  # ed25519
            self._pub = self._raw.public_key().public_bytes(
                Encoding.Raw, PublicFormat.Raw
            )
            self._kind = "ed25519"
        elif isinstance(self._raw, EllipticCurvePrivateKey):
            self._tag = 0x02  # secp256k1
            self._pub = self._raw.public_key().public_bytes(
                Encoding.X962, PublicFormat.CompressedPoint
            )
            self._kind = "secp256k1"
        else:
            raise ValueError(f"Unsupported key type: {type(self._raw)}")

    def pubkey_serial(self) -> bytes:
        """Tag byte + raw public key bytes (for binary header)."""
        return bytes([self._tag]) + self._pub

    def pubkey_hex(self) -> str:
        """Casper public key hex string (e.g. '0203ee00a5...')."""
        return f"{self._tag:02x}" + self._pub.hex()

    def account_hash(self) -> str:
        """Casper account hash string."""
        prefix = b"ed25519\x00" if self._tag == 0x01 else b"secp256k1\x00"
        h = hashlib.blake2b(prefix + self._pub, digest_size=32).hexdigest()
        return f"account-hash-{h}"

    def account_hash_hex(self) -> str:
        """Just the hex part of the account hash (no 'account-hash-' prefix)."""
        prefix = b"ed25519\x00" if self._tag == 0x01 else b"secp256k1\x00"
        return hashlib.blake2b(prefix + self._pub, digest_size=32).hexdigest()

    def sign(self, data: bytes) -> bytes:
        """Sign data and return raw signature bytes (no DER, no tag prefix)."""
        if self._tag == 0x01:
            # ed25519: 64 bytes raw
            return self._raw.sign(data)
        else:
            # secp256k1: ECDSA with SHA-256, convert DER → raw r||s (64 bytes)
            der_sig = self._raw.sign(data, ECDSA(hashes.SHA256()))
            return self._der_to_raw64(der_sig)

    def sign_deploy(self, header_hash_bytes: bytes) -> bytes:
        """Sign the header hash for a deploy. Returns raw 64-byte signature."""
        if self._tag == 0x01:
            # ed25519: sign directly, returns 64 bytes
            return self._raw.sign(header_hash_bytes)
        else:
            # secp256k1: ECDSA with SHA-256, convert DER → raw r||s (64 bytes)
            der_sig = self._raw.sign(header_hash_bytes, ECDSA(hashes.SHA256()))
            return self._der_to_raw64(der_sig)

    @staticmethod
    def _der_to_raw64(der: bytes) -> bytes:
        """Convert DER ECDSA signature to raw r||s (64 bytes)."""
        # DER: 30 <len> 02 <rlen> <r> 02 <slen> <s>
        if der[0] != 0x30:
            raise ValueError(f"Invalid DER signature: first byte is {der[0]:02x}")
        idx = 2
        if der[idx] != 0x02:
            raise ValueError(f"Invalid DER: expected 0x02 at idx {idx}")
        idx += 1
        rlen = der[idx]
        idx += 1
        r = der[idx : idx + rlen]
        idx += rlen
        if der[idx] != 0x02:
            raise ValueError(f"Invalid DER: expected 0x02 at idx {idx}")
        idx += 1
        slen = der[idx]
        idx += 1
        s = der[idx : idx + slen]
        # Strip leading 0x00 padding, then pad each to 32 bytes
        r_int = int.from_bytes(r, "big")
        s_int = int.from_bytes(s, "big")
        return r_int.to_bytes(32, "big") + s_int.to_bytes(32, "big")


def load_key(path: str) -> CasperKey:
    return CasperKey(path)


def generate_key(out_path: str) -> CasperKey:
    """Generate a new ed25519 keypair."""
    raw_key = Ed25519PrivateKey.generate()
    pub = raw_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    Path(out_path).write_bytes(
        raw_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    )
    base = out_path.replace("secret_key.pem", "")
    Path(base + "public_key.pem").write_bytes(
        raw_key.public_key().public_bytes(
            Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
        )
    )
    Path(base + "public_key_hex").write_text("01" + pub.hex())
    return CasperKey(out_path)


# ── RPC ───────────────────────────────────────────────────────────────────────


def _rpc(method: str, params: Any, node: str = NODES[0]) -> Any:
    body = json.dumps(
        {"id": 1, "jsonrpc": "2.0", "method": method, "params": params}
    ).encode()
    req = urllib.request.Request(
        _rpc_url(node),
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
    except urllib.error.URLError as e:
        raise RuntimeError(f"RPC ({node}): {e}") from e
    if "error" in data:
        raise RuntimeError(f"RPC error: {data['error']}")
    return data.get("result", data)


def get_state_root(node: str = NODES[0]) -> str:
    return _rpc("chain_get_state_root_hash", {}, node)["state_root_hash"]


def wait_for_deploy(deploy_hash: str, timeout: int = 300, node: str = NODES[0]) -> dict:
    print(f"   waiting for {deploy_hash[:16]}…", end="", flush=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = _rpc(
                "info_get_transaction",
                {"transaction_hash": {"Deploy": deploy_hash}},
                node,
            )
            txn = r.get("transaction") or r.get("deploy") or {}
            if txn.get("execution_info") or txn.get("execution_results"):
                print(" ✓")
                return r
        except Exception:
            pass
        print(".", end="", flush=True)
        time.sleep(4)
    print(" TIMEOUT")
    raise TimeoutError(f"{deploy_hash} not finalised in {timeout}s")


# ── Deploy construction ───────────────────────────────────────────────────────


def _ms_to_iso(ms: int) -> str:
    secs = ms // 1000
    millis = ms % 1000
    return time.strftime(f"%Y-%m-%dT%H:%M:%S.{millis:03d}Z", time.gmtime(secs))


def _body_hash(payment: bytes, session: bytes) -> str:
    return hashlib.blake2b(payment + session, digest_size=32).hexdigest()


def _header_hash(
    pub_serial: bytes, ts_ms: int, ttl_ms: int, gas_price: int, body_h: str, chain: str
) -> bytes:
    """Compute deploy header hash.

    pub_serial = tag_byte + raw_pub_bytes (33 bytes for ed25519, 34 for secp256k1)
    body_h = hex string of 32-byte body hash (NO length prefix in header)
    """
    raw = (
        pub_serial
        + _u64(ts_ms)
        + _u64(ttl_ms)
        + _u64(gas_price)
        + bytes.fromhex(body_h)  # 32 bytes, NO length prefix
        + _u32(0)  # no dependencies
        + _str(chain)
    )
    return hashlib.blake2b(raw, digest_size=32).digest()


def _payment_bytes(motes: int) -> bytes:
    arg = _named_arg("amount", _CL_U512, _u512(motes))
    return b"\x00" + _len_prefix(b"") + _u32(1) + arg


def _wasm_session_bytes(wasm: bytes, args: list) -> bytes:
    arg_bytes = b"".join(_named_arg(n, t, v) for n, t, v in args)
    return b"\x00" + _len_prefix(wasm) + _u32(len(args)) + arg_bytes


def _contract_session_bytes(contract_hash: str, entry_point: str, args: list) -> bytes:
    arg_bytes = b"".join(_named_arg(n, t, v) for n, t, v in args)
    return (
        b"\x01"
        + bytes.fromhex(contract_hash)
        + _str(entry_point)
        + _u32(len(args))
        + arg_bytes
    )


_CL_TYPE_NAMES = {
    b"\x00": "Bool",
    b"\x01": "I32",
    b"\x02": "I64",
    b"\x03": "U8",
    b"\x04": "U32",
    b"\x05": "U64",
    b"\x06": "U128",
    b"\x07": "U256",
    b"\x08": "U512",
    b"\x09": "Unit",
    b"\x0a": "String",
    b"\x0b": "Key",
    b"\x0c": "URef",
    b"\x0d": "Option",
    b"\x0e": "List",
    b"\x0f": "FixedList",
    b"\x10": "Result",
    b"\x11": "Pair",
    b"\x12": "Map",
    b"\x13": "Any",
}


def _args_to_json(named_args: list) -> list:
    result = []
    for name, cl_type_bytes, value_bytes in named_args:
        cl_type_str = _CL_TYPE_NAMES.get(cl_type_bytes, "Any")
        parsed = ""
        if cl_type_bytes == _CL_STRING:
            parsed = value_bytes[4:].decode("utf-8", errors="replace")
        elif cl_type_bytes == _CL_U64:
            parsed = str(struct.unpack("<Q", value_bytes)[0])
        elif cl_type_bytes == _CL_U32:
            parsed = str(struct.unpack("<I", value_bytes)[0])
        elif cl_type_bytes == _CL_U512:
            parsed = (
                str(int.from_bytes(value_bytes[1:], "little"))
                if value_bytes[0] > 0
                else "0"
            )
        elif cl_type_bytes == _CL_BOOL:
            parsed = "true" if value_bytes[0] else "false"
        result.append(
            [
                name,
                {"cl_type": cl_type_str, "bytes": value_bytes.hex(), "parsed": parsed},
            ]
        )
    return result


def send_deploy(
    key: CasperKey,
    payment_motes: int,
    session_raw: bytes,
    session_args: list,
    node: str = NODES[0],
) -> str:
    pub_serial = key.pubkey_serial()  # tag + raw pub bytes
    acct_h = key.account_hash_hex()
    pub_hex = key.pubkey_hex()

    ts_ms = int(time.time() * 1000)
    ttl = 1_800_000
    gas = 1

    pay_raw = _payment_bytes(payment_motes)
    bh = _body_hash(pay_raw, session_raw)
    dh_raw = _header_hash(pub_serial, ts_ms, ttl, gas, bh, CHAIN)
    dh = dh_raw.hex()
    sig = key.sign_deploy(dh_raw)

    tag = session_raw[0]
    if tag == 0:
        wasm_len = struct.unpack("<I", session_raw[1:5])[0]
        wasm_bytes = session_raw[5 : 5 + wasm_len]
        session_json = {
            "ModuleBytes": {
                "module_bytes": wasm_bytes.hex(),
                "args": _args_to_json(session_args),
            }
        }
    elif tag == 1:
        ep_len = struct.unpack("<I", session_raw[33:37])[0]
        ep = session_raw[37 : 37 + ep_len].decode()
        session_json = {
            "StoredContractByHash": {
                "hash": session_raw[1:33].hex(),
                "entry_point": ep,
                "args": _args_to_json(session_args),
            }
        }
    else:
        session_json = {"ModuleBytes": {"module_bytes": session_raw.hex(), "args": []}}

    # Signature hex: tag byte + raw signature bytes
    sig_tag = f"{key._tag:02x}"
    deploy = {
        "hash": dh,
        "header": {
            "account": pub_hex,
            "timestamp": _ms_to_iso(ts_ms),
            "ttl": "30m",
            "gas_price": gas,
            "body_hash": bh,
            "dependencies": [],
            "chain_name": CHAIN,
        },
        "payment": {
            "ModuleBytes": {
                "module_bytes": "",
                "args": [
                    [
                        "amount",
                        {
                            "cl_type": "U512",
                            "bytes": _u512(payment_motes).hex(),
                            "parsed": str(payment_motes),
                        },
                    ]
                ],
            }
        },
        "session": session_json,
        "approvals": [{"signer": pub_hex, "signature": sig_tag + sig.hex()}],
    }
    result = _rpc("account_put_deploy", {"deploy": deploy}, node)
    return result.get("deploy_hash", dh)


# ── Public API ────────────────────────────────────────────────────────────────


def install_wasm(
    key: CasperKey,
    wasm_path: str,
    named_args: list | None = None,
    payment: int = 400_000_000_000,
    node: str = NODES[0],
) -> str:
    wasm = Path(wasm_path).read_bytes()
    args = named_args or []
    session_raw = _wasm_session_bytes(wasm, args)
    return send_deploy(key, payment, session_raw, args, node)


def call_entry_point(
    key: CasperKey,
    contract_hash: str,
    entry_point: str,
    named_args: list | None = None,
    payment: int = 5_000_000_000,
    node: str = NODES[0],
) -> str:
    args = named_args or []
    session_raw = _contract_session_bytes(contract_hash, entry_point, args)
    return send_deploy(key, payment, session_raw, args, node)


# ── Arg helpers ───────────────────────────────────────────────────────────────


def arg_string(name: str, v: str):
    return (name, _CL_STRING, _str(v))


def arg_u64(name: str, v: int):
    return (name, _CL_U64, _u64(v))


def arg_u32(name: str, v: int):
    return (name, _CL_U32, _u32(v))


def arg_u512(name: str, v: int):
    return (name, _CL_U512, _u512(v))


def arg_bool(name: str, v: bool):
    return (name, _CL_BOOL, bytes([1 if v else 0]))


# ── CLI ───────────────────────────────────────────────────────────────────────


# ── deploy-all ────────────────────────────────────────────────────────────────


def _deploy_all(args) -> None:
    """One-click deployment of all 4 contracts + wiring."""
    key = CasperKey(args.key)
    acct_hash = key.account_hash()
    acct_hash_hex = key.account_hash_hex()
    print(f"\n{'=' * 52}")
    print(f"  HELIOS DEPLOY-ALL")
    print(f"{'=' * 52}")
    print(f"  Deployer : {acct_hash}")
    print(f"  Key type : {key._kind}")
    print(f"  Node     : {args.node}")
    print()

    wasm_dir = Path(args.wasm_dir) if args.wasm_dir else Path("contracts/wasm")
    if not wasm_dir.exists():
        print(f"ERROR: WASM directory not found: {wasm_dir}")
        print("Run 'bash scripts/build_contracts.sh' first.")
        sys.exit(1)

    payment = args.payment
    hashes = {}

    # Step 1: Deploy OracleRegistry
    print("Step 1: Deploy OracleRegistry")
    wasm = wasm_dir / "OracleRegistry.wasm"
    if not wasm.exists():
        print(f"  ERROR: {wasm} not found")
        sys.exit(1)
    dh = install_wasm(key, str(wasm), payment=payment, node=args.node)
    print(f"  deploy: {dh}")
    if args.node:
        try:
            wait_for_deploy(dh, node=args.node)
        except Exception as e:
            print(f"  WARNING: wait failed: {e}")
    hashes["oracle"] = _extract_contract_hash(dh, args.node)
    print(f"  contract_hash: {hashes['oracle']}")
    print()

    # Step 2: Deploy DataMarket
    print("Step 2: Deploy DataMarket")
    wasm = wasm_dir / "DataMarket.wasm"
    if not wasm.exists():
        print(f"  ERROR: {wasm} not found")
        sys.exit(1)
    reg_hash = hashes["oracle"]
    dh = install_wasm(
        key,
        str(wasm),
        named_args=[arg_string("registry_hash", reg_hash), arg_u32("fee_bps", 250)],
        payment=payment,
        node=args.node,
    )
    print(f"  deploy: {dh}")
    try:
        wait_for_deploy(dh, node=args.node)
    except Exception as e:
        print(f"  WARNING: wait failed: {e}")
    hashes["market"] = _extract_contract_hash(dh, args.node)
    print(f"  contract_hash: {hashes['market']}")
    print()

    # Step 2b: Wire OracleRegistry → DataMarket
    print("Step 2b: Wire OracleRegistry.set_market → DataMarket")
    dh = call_entry_point(
        key,
        hashes["oracle"],
        "set_market",
        named_args=[arg_string("market", hashes["market"])],
        node=args.node,
    )
    print(f"  deploy: {dh}")
    try:
        wait_for_deploy(dh, node=args.node)
    except Exception as e:
        print(f"  WARNING: wait failed: {e}")
    print()

    # Step 3: Deploy FundVault
    print("Step 3: Deploy FundVault")
    wasm = wasm_dir / "FundVault.wasm"
    if not wasm.exists():
        print(f"  ERROR: {wasm} not found")
        sys.exit(1)
    dh = install_wasm(
        key,
        str(wasm),
        named_args=[
            arg_string("operator", acct_hash),
            arg_string("governance_hash", "pending"),
        ],
        payment=payment,
        node=args.node,
    )
    print(f"  deploy: {dh}")
    try:
        wait_for_deploy(dh, node=args.node)
    except Exception as e:
        print(f"  WARNING: wait failed: {e}")
    hashes["vault"] = _extract_contract_hash(dh, args.node)
    print(f"  contract_hash: {hashes['vault']}")
    print()

    # Step 4: Deploy Governance
    print("Step 4: Deploy Governance")
    wasm = wasm_dir / "Governance.wasm"
    if not wasm.exists():
        print(f"  ERROR: {wasm} not found")
        sys.exit(1)
    dh = install_wasm(
        key,
        str(wasm),
        named_args=[
            arg_string("proposer", acct_hash),
            arg_string("risk_agent", acct_hash),
            arg_u64("veto_window_ms", 90000),
        ],
        payment=payment,
        node=args.node,
    )
    print(f"  deploy: {dh}")
    try:
        wait_for_deploy(dh, node=args.node)
    except Exception as e:
        print(f"  WARNING: wait failed: {e}")
    hashes["governance"] = _extract_contract_hash(dh, args.node)
    print(f"  contract_hash: {hashes['governance']}")
    print()

    # Step 4b: Wire FundVault → Governance
    print("Step 4b: Wire FundVault.set_governance → Governance")
    dh = call_entry_point(
        key,
        hashes["vault"],
        "set_governance",
        named_args=[arg_string("governance_hash", hashes["governance"])],
        node=args.node,
    )
    print(f"  deploy: {dh}")
    try:
        wait_for_deploy(dh, node=args.node)
    except Exception as e:
        print(f"  WARNING: wait failed: {e}")
    print()

    # Write env file
    env_path = Path(args.env_out)
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_content = (
        f"# Helios testnet deployment — {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}\n"
        f"REGISTRY_HASH={hashes['oracle']}\n"
        f"MARKET_HASH={hashes['market']}\n"
        f"VAULT_HASH={hashes['vault']}\n"
        f"GOV_HASH={hashes['governance']}\n"
        f"DEPLOYER_ACCOUNT={acct_hash}\n"
    )
    env_path.write_text(env_content)
    print(f"Env file written: {env_path}")
    print()

    # Summary
    print(f"{'=' * 52}")
    print(f"  DEPLOYMENT COMPLETE")
    print(f"{'=' * 52}")
    print(f"  OracleRegistry : {EXPLORER}/contract/{hashes['oracle']}")
    print(f"  DataMarket     : {EXPLORER}/contract/{hashes['market']}")
    print(f"  FundVault      : {EXPLORER}/contract/{hashes['vault']}")
    print(f"  Governance     : {EXPLORER}/contract/{hashes['governance']}")
    print()


def _extract_contract_hash(deploy_hash: str, node: str) -> str:
    """Extract the contract hash from a deploy result."""
    try:
        r = wait_for_deploy(deploy_hash, node=node)
        txn = r.get("transaction") or r.get("deploy") or {}

        # Casper 2.x: execution_info.results[].result.Success.effects[]
        exec_info = txn.get("execution_info", {})
        results = exec_info.get("results", [])

        if results:
            effects = results[0].get("result", {}).get("Success", {}).get("effects", [])
            for effect in effects:
                transform = effect.get("transform", {})
                # Look for WriteContract or WriteContractPackage
                if "WriteContractPackage" in transform:
                    key = effect.get("key", "")
                    if key.startswith("contract-package-wasm"):
                        # The actual contract hash is in a subsequent WriteContract effect
                        continue
                if "WriteContract" in transform or "Write" in str(transform):
                    key = effect.get("key", "")
                    if key.startswith("contract-") and not key.startswith(
                        "contract-package"
                    ):
                        return key.replace("contract-", "")

        # Casper 1.x fallback: execution_results[].result.Success.effect.transforms[]
        exec_results = txn.get("execution_results", [])
        if exec_results:
            transforms = (
                exec_results[0]
                .get("result", {})
                .get("Success", {})
                .get("effect", {})
                .get("transforms", [])
            )
            for transform in transforms:
                for t in transform:
                    if "WriteContract" in t or "Write" in str(t):
                        key = t.get("key", "")
                        if key.startswith("contract-") and not key.startswith(
                            "contract-package"
                        ):
                            return key.replace("contract-", "")

        print(f"  ⚠ Could not auto-extract contract hash from deploy {deploy_hash}")
        print(
            f"    Please check {EXPLORER}/deploy/{deploy_hash} and manually extract the contract hash"
        )
    except Exception as e:
        print(f"  ⚠ Error extracting contract hash: {e}")

    return deploy_hash  # Fallback - caller should handle this


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Helios Casper deploy tool (pure Python, no casper-client needed)"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # keygen
    kg = sub.add_parser("keygen", help="Generate 5 keypairs for Helios agents")
    kg.add_argument("--out", default="keys", help="Output directory (default: keys/)")

    # pubkey — show key info (supports secp256k1 PEM files)
    pk = sub.add_parser("pubkey", help="Show public key and account hash")
    pk.add_argument("--key", required=True, help="Path to secret_key.pem")

    # status
    st = sub.add_parser("status", help="Check testnet node connectivity")
    st.add_argument("--node", default=NODES[0])

    # install
    ins = sub.add_parser("install", help="Install a WASM contract to testnet")
    ins.add_argument("--key", required=True, help="Path to secret_key.pem")
    ins.add_argument("--wasm", required=True, help="Path to .wasm file")
    ins.add_argument(
        "--payment",
        type=int,
        default=400_000_000_000,
        help="Payment in motes (default 400 CSPR)",
    )
    ins.add_argument("--node", default=NODES[0])
    ins.add_argument("--wait", action="store_true", help="Wait for finalization")
    ins.add_argument(
        "--args",
        nargs="*",
        default=[],
        help="Named args: name:type=value (types: string u64 u32 u512 bool)",
    )

    # call
    cl = sub.add_parser("call", help="Call a stored contract entry-point")
    cl.add_argument("--key", required=True)
    cl.add_argument("--contract", required=True, help="Contract hash (hex, no prefix)")
    cl.add_argument("--entry-point", required=True, dest="entry_point")
    cl.add_argument(
        "--args",
        nargs="*",
        default=[],
        help="name:type=value  (types: string u64 u32 u512 bool)",
    )
    cl.add_argument("--payment", type=int, default=5_000_000_000)
    cl.add_argument("--node", default=NODES[0])
    cl.add_argument("--wait", action="store_true")

    # wait
    wt = sub.add_parser("wait", help="Wait for a deploy hash to finalize")
    wt.add_argument("deploy_hash")
    wt.add_argument("--node", default=NODES[0])

    # info (alias for pubkey)
    inf = sub.add_parser("info", help="Show key info (pubkey + account hash)")
    inf.add_argument("--key", required=True, help="Path to secret_key.pem")

    # deploy-all — one-click deployment of all 4 contracts
    da = sub.add_parser("deploy-all", help="Deploy all 4 contracts + wire them up")
    da.add_argument("--key", required=True, help="Path to secret_key.pem (deployer)")
    da.add_argument(
        "--wasm-dir", default=None, help="WASM directory (default: contracts/wasm)"
    )
    da.add_argument("--payment", type=int, default=400_000_000_000)
    da.add_argument("--node", default=NODES[0])
    da.add_argument("--env-out", default="agents/testnet.env", help="Output env file")

    args = parser.parse_args()

    # ── keygen ────────────────────────────────────────────────────────────────
    if args.cmd == "keygen":
        roles = [
            "oracle_tbill",
            "oracle_gold",
            "oracle_reindex",
            "fund_agent",
            "risk_agent",
        ]
        out = Path(args.out)
        print(f"\nGenerating {len(roles)} keypairs in {out}/\n")
        env_lines = []
        for role in roles:
            d = out / role
            d.mkdir(parents=True, exist_ok=True)
            kp = d / "secret_key.pem"
            if kp.exists():
                print(f"  {role}: already exists — skipping")
                key = CasperKey(str(kp))
            else:
                generate_key(str(kp))
                key = CasperKey(str(kp))
            print(f"  {role}")
            print(f"    key_type: {key._kind}")
            print(f"    pubkey  : {key.pubkey_hex()}")
            print(f"    account : {key.account_hash()}")
            env_lines.append(f"{role.upper()}_KEY={d}/secret_key.pem")
        (out / "accounts.txt").write_text("\n".join(env_lines) + "\n")
        print(f"\nFund each account: https://testnet.cspr.live/tools/faucet")
        print("(Paste the pubkey value from public_key_hex file into the faucet)\n")

    # ── pubkey / info ─────────────────────────────────────────────────────────
    elif args.cmd in ("pubkey", "info"):
        key = CasperKey(args.key)
        print(f"key_type:     {key._kind}")
        print(f"pubkey:       {key.pubkey_hex()}")
        print(f"account_hash: {key.account_hash()}")

    # ── status ────────────────────────────────────────────────────────────────
    elif args.cmd == "status":
        try:
            r = _rpc("info_get_status", {}, args.node)
            print(f"✓ {args.node}")
            print(f"  chain : {r.get('chainspec_name', '?')}")
            print(f"  peers : {len(r.get('peers', []))}")
        except Exception as e:
            print(f"✗ {args.node}: {e}")
            sys.exit(1)

    # ── install ───────────────────────────────────────────────────────────────
    elif args.cmd == "install":
        key = CasperKey(args.key)
        print(f"Deployer : {key.account_hash()}")
        size = Path(args.wasm).stat().st_size
        print(f"WASM     : {args.wasm} ({size:,} bytes)")

        named = _parse_args(args.args)
        dh = install_wasm(
            key, args.wasm, named_args=named, payment=args.payment, node=args.node
        )
        print(f"Deploy   : {dh}")
        print(f"Explorer : {EXPLORER}/deploy/{dh}")
        if args.wait:
            wait_for_deploy(dh, node=args.node)

    # ── call ──────────────────────────────────────────────────────────────────
    elif args.cmd == "call":
        key = CasperKey(args.key)
        named = _parse_args(args.args)
        dh = call_entry_point(
            key, args.contract, args.entry_point, named, args.payment, args.node
        )
        print(f"Deploy   : {dh}")
        print(f"Explorer : {EXPLORER}/deploy/{dh}")
        if args.wait:
            wait_for_deploy(dh, node=args.node)

    # ── wait ──────────────────────────────────────────────────────────────────
    elif args.cmd == "wait":
        wait_for_deploy(args.deploy_hash, node=args.node)

    # ── deploy-all ────────────────────────────────────────────────────────────
    elif args.cmd == "deploy-all":
        _deploy_all(args)


def _parse_args(raw: list[str]) -> list:
    """Parse 'name:type=value' strings into (name, cl_type_bytes, value_bytes) tuples."""
    result = []
    for a in raw:
        name, rest = a.split(":", 1)
        typ, val = rest.split("=", 1)
        if typ == "string":
            result.append(arg_string(name, val))
        elif typ == "u64":
            result.append(arg_u64(name, int(val)))
        elif typ == "u32":
            result.append(arg_u32(name, int(val)))
        elif typ == "u512":
            result.append(arg_u512(name, int(val)))
        elif typ == "bool":
            result.append(arg_bool(name, val.lower() in ("1", "true", "yes")))
        else:
            raise ValueError(
                f"Unknown arg type '{typ}' in '{a}' — use string/u64/u32/u512/bool"
            )
    return result


if __name__ == "__main__":
    main()
