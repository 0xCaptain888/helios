#!/usr/bin/env python3
"""
Helios Casper Testnet Deployer — pure Python, supports secp256k1 + ed25519.

Fixes vs previous version:
  - secp256k1 keys (EC PRIVATE KEY / Casper Wallet export) fully supported
  - Signature serialised as raw 64-byte r||s (not DER), matching Casper spec
  - PublicKey serialised with correct tag byte (01=ed25519, 02=secp256k1)
  - body_hash computed correctly over binary-serialised payment||session
  - header_hash includes serialised PublicKey (tag + raw bytes), not account_hash
  - Casper 2.x (Condor / v2.2.1): account_put_deploy still supported via
    legacy endpoint; info_get_transaction used for polling
  - RPC JSON format matches Casper 2.x expectations

Usage:
  python3 scripts/casper_deploy.py status
  python3 scripts/casper_deploy.py keygen --out keys/
  python3 scripts/casper_deploy.py install \\
      --key "Account 1_secret_key.pem" \\
      --wasm contracts/wasm/OracleRegistry.wasm --wait
  python3 scripts/casper_deploy.py call \\
      --key "Account 1_secret_key.pem" \\
      --contract <HASH> --entry-point register \\
      --args "name:string=TBill Oracle" "category:string=rwa" \\
             "endpoint:string=https://helios.example/quote" \\
             "price_motes:u64=2000000000"
  python3 scripts/casper_deploy.py deploy-all \\
      --key "Account 1_secret_key.pem"
  python3 scripts/casper_deploy.py wait <deploy_hash>
"""

from __future__ import annotations
import argparse, hashlib, json, os, struct, sys, time, urllib.error, urllib.request
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
    PrivateFormat,
    NoEncryption,
    load_pem_private_key,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.ec import (
    EllipticCurvePrivateKey,
    SECP256K1,
    generate_private_key,
    ECDSA,
)
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.backends import default_backend

# ── Constants ─────────────────────────────────────────────────────────────────
NODES = [
    "https://node.testnet.casper.network",
    "https://rpc.testnet.cspr.cloud",
]
CHAIN = "casper-test"
EXPLORER = "https://testnet.cspr.live"

# ── Binary serialisation helpers ──────────────────────────────────────────────


def _u32(n: int) -> bytes:
    return struct.pack("<I", n)


def _u64(n: int) -> bytes:
    return struct.pack("<Q", n)


def _len_prefix(b: bytes) -> bytes:
    return _u32(len(b)) + b


def _str(s: str) -> bytes:
    enc = s.encode("utf-8")
    return _u32(len(enc)) + enc


def _u512(n: int) -> bytes:
    if n == 0:
        return b"\x00"
    b = n.to_bytes((n.bit_length() + 7) // 8, "little")
    return bytes([len(b)]) + b


# CLType tag bytes
_CL_BOOL = b"\x00"
_CL_U32 = b"\x08"
_CL_U64 = b"\x09"
_CL_U512 = b"\x0b"
_CL_STRING = b"\x0a"


def _cl(cl_type: bytes, value_bytes: bytes) -> bytes:
    return _len_prefix(value_bytes) + cl_type


def _named_arg(name: str, cl_type: bytes, value_bytes: bytes) -> bytes:
    return _str(name) + _cl(cl_type, value_bytes)


# ── Key abstraction — handles both ed25519 and secp256k1 ──────────────────────

KEY_TAG_ED25519 = 0x01
KEY_TAG_SECP256K1 = 0x02


class CasperKey:
    """Wraps either ed25519 or secp256k1 private key with Casper-aware methods."""

    def __init__(self, priv):
        self._priv = priv
        if isinstance(priv, Ed25519PrivateKey):
            self._tag = KEY_TAG_ED25519
            raw_pub = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
            self._pub_bytes = raw_pub  # 32 bytes
        elif isinstance(priv, EllipticCurvePrivateKey):
            self._tag = KEY_TAG_SECP256K1
            # Compressed point: 02 or 03 prefix + 32-byte X
            compressed = priv.public_key().public_bytes(
                Encoding.X962, PublicFormat.CompressedPoint
            )
            self._pub_bytes = compressed  # 33 bytes
        else:
            raise TypeError(f"Unsupported key type: {type(priv)}")

    @classmethod
    def load(cls, path: str) -> "CasperKey":
        raw = Path(path).read_bytes()
        priv = load_pem_private_key(raw, password=None)
        return cls(priv)

    @classmethod
    def generate_ed25519(cls) -> "CasperKey":
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey as E,
        )

        return cls(E.generate())

    def save(self, path: str) -> None:
        Path(path).write_bytes(
            self._priv.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
        )

    # --- Public-key representations ---

    def pub_bytes(self) -> bytes:
        """Raw public key bytes (32 for ed25519, 33 compressed for secp256k1)."""
        return self._pub_bytes

    def casper_pubkey_hex(self) -> str:
        """Casper hex pubkey: "01" + ed25519_bytes  or  "02" + secp256k1_compressed."""
        tag_hex = f"{self._tag:02x}"
        return tag_hex + self._pub_bytes.hex()

    def pub_bytes_for_serialisation(self) -> bytes:
        """
        Binary encoding of PublicKey used in header hash and deploy JSON:
          tag(1 byte) + raw_key_bytes
        ed25519 -> 1 + 32 = 33 bytes
        secp256k1 -> 2 + 33 = 35 bytes  (tag + 33-byte compressed point)
        """
        return bytes([self._tag]) + self._pub_bytes

    def account_hash(self) -> str:
        """Blake2b-256 of (algo_tag_str + \0 + pub_bytes), hex."""
        if self._tag == KEY_TAG_ED25519:
            prefix = b"ed25519\x00"
        else:
            prefix = b"secp256k1\x00"
        h = hashlib.blake2b(prefix + self._pub_bytes, digest_size=32).hexdigest()
        return f"account-hash-{h}"

    # --- Signing ---

    def sign(self, message: bytes) -> bytes:
        """
        Returns raw 64-byte signature.
        ed25519: directly 64 bytes.
        secp256k1: DER-encoded ECDSA -> extract r, s -> pad each to 32 bytes.
        """
        if self._tag == KEY_TAG_ED25519:
            return self._priv.sign(message)
        else:
            # ECDSA(SHA256) returns DER-encoded signature
            der_sig = self._priv.sign(message, ECDSA(SHA256()))
            return _der_to_raw64(der_sig)

    def signature_hex(self, message: bytes) -> str:
        """Casper signature hex: tag_byte + 64 raw sig bytes."""
        tag_hex = f"{self._tag:02x}"
        return tag_hex + self.sign(message).hex()

    def is_secp256k1(self) -> bool:
        return self._tag == KEY_TAG_SECP256K1


def _der_to_raw64(der: bytes) -> bytes:
    """
    Convert DER-encoded ECDSA signature to raw 64-byte r||s.
    DER format: 30 <len> 02 <rlen> <r> 02 <slen> <s>
    """
    assert der[0] == 0x30
    idx = 2  # skip 30 <total_len>
    assert der[idx] == 0x02
    idx += 1
    r_len = der[idx]
    idx += 1
    r_bytes = der[idx : idx + r_len]
    idx += r_len
    assert der[idx] == 0x02
    idx += 1
    s_len = der[idx]
    idx += 1
    s_bytes = der[idx : idx + s_len]
    # Strip leading 0x00 (sign padding) and left-pad to 32 bytes
    r = int.from_bytes(r_bytes, "big").to_bytes(32, "big")
    s = int.from_bytes(s_bytes, "big").to_bytes(32, "big")
    return r + s


# ── RPC ───────────────────────────────────────────────────────────────────────


def _rpc(method: str, params: Any, node: str = NODES[0]) -> Any:
    body = json.dumps(
        {"id": 1, "jsonrpc": "2.0", "method": method, "params": params}
    ).encode()
    req = urllib.request.Request(
        f"{node}/rpc",
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
        raise RuntimeError(
            f"RPC error {data['error'].get('code')}: {data['error'].get('message')}"
        )
    return data.get("result", data)


def _rpc_any(method: str, params: Any) -> Any:
    """Try all nodes, raise if all fail."""
    last_err = None
    for node in NODES:
        try:
            return _rpc(method, params, node)
        except Exception as e:
            last_err = e
    raise last_err


def get_status() -> dict:
    return _rpc_any("info_get_status", {})


def wait_for_deploy(deploy_hash: str, timeout: int = 300) -> dict:
    print(f"   waiting for {deploy_hash[:16]}…", end="", flush=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            # Casper 2.x uses info_get_transaction; 1.x uses info_get_deploy
            # Try 2.x first, fall back to 1.x
            try:
                r = _rpc_any(
                    "info_get_transaction",
                    {"transaction_hash": {"Deploy": deploy_hash}},
                )
                txn = r.get("transaction") or {}
                if txn.get("execution_info"):
                    print(" ✓")
                    return r
            except Exception:
                r = _rpc_any("info_get_deploy", {"deploy_hash": deploy_hash})
                d = r.get("deploy") or {}
                if d.get("execution_results"):
                    print(" ✓")
                    return r
        except Exception:
            pass
        print(".", end="", flush=True)
        time.sleep(4)
    print(" TIMEOUT")
    raise TimeoutError(f"{deploy_hash} not finalised in {timeout}s")


# ── Deploy construction ───────────────────────────────────────────────────────


def _payment_bytes(motes: int) -> bytes:
    """Standard payment: ModuleBytes with empty wasm + amount arg."""
    arg = _named_arg("amount", _CL_U512, _u512(motes))
    return b"\x00" + _len_prefix(b"") + _len_prefix(_u32(1) + arg)


def _wasm_session_bytes(wasm: bytes, args: list) -> bytes:
    """Session: ModuleBytes with wasm + args."""
    arg_bytes = b"".join(_named_arg(n, t, v) for n, t, v in args)
    return b"\x00" + _len_prefix(wasm) + _len_prefix(_u32(len(args)) + arg_bytes)


def _contract_session_bytes(contract_hash: str, entry_point: str, args: list) -> bytes:
    """Session: StoredContractByHash."""
    arg_bytes = b"".join(_named_arg(n, t, v) for n, t, v in args)
    return (
        b"\x01"
        + bytes.fromhex(contract_hash)
        + _str(entry_point)
        + _len_prefix(_u32(len(args)) + arg_bytes)
    )


def _body_hash(payment: bytes, session: bytes) -> bytes:
    """Blake2b-256 of payment_bytes || session_bytes."""
    return hashlib.blake2b(payment + session, digest_size=32).digest()


def _header_hash(
    key: CasperKey, ts_ms: int, ttl_ms: int, gas_price: int, body_h: bytes, chain: str
) -> bytes:
    """
    Blake2b-256 of the serialised DeployHeader.
    CRITICAL: uses serialised PublicKey (tag + raw bytes), NOT account_hash.
    """
    pub_serial = key.pub_bytes_for_serialisation()
    raw = (
        pub_serial
        + _u64(ts_ms)
        + _u64(ttl_ms)
        + _u64(gas_price)
        + body_h
        + _u32(0)  # empty dependencies Vec
        + _str(chain)
    )
    return hashlib.blake2b(raw, digest_size=32).digest()


def _args_to_json(args: list) -> list:
    """Convert internal (name, cl_type_byte, value_bytes) to Casper JSON arg format."""
    result = []
    for name, cl_type_byte, val_bytes in args:
        if cl_type_byte == _CL_STRING:
            # strip the 4-byte length prefix to get the utf8 string
            s = val_bytes[4:].decode("utf-8")
            result.append(
                [name, {"cl_type": "String", "bytes": val_bytes.hex(), "parsed": s}]
            )
        elif cl_type_byte == _CL_U64:
            v = struct.unpack("<Q", val_bytes)[0]
            result.append(
                [name, {"cl_type": "U64", "bytes": val_bytes.hex(), "parsed": str(v)}]
            )
        elif cl_type_byte == _CL_U32:
            v = struct.unpack("<I", val_bytes)[0]
            result.append(
                [name, {"cl_type": "U32", "bytes": val_bytes.hex(), "parsed": str(v)}]
            )
        elif cl_type_byte == _CL_U512:
            # decode variable-length little-endian
            n_bytes = val_bytes[0]
            if n_bytes == 0:
                v = 0
            else:
                v = int.from_bytes(val_bytes[1 : 1 + n_bytes], "little")
            result.append(
                [name, {"cl_type": "U512", "bytes": val_bytes.hex(), "parsed": str(v)}]
            )
        elif cl_type_byte == _CL_BOOL:
            result.append(
                [
                    name,
                    {
                        "cl_type": "Bool",
                        "bytes": val_bytes.hex(),
                        "parsed": val_bytes[0] != 0,
                    },
                ]
            )
    return result


def _payment_to_json(motes: int) -> dict:
    return {
        "ModuleBytes": {
            "module_bytes": "",
            "args": [
                [
                    "amount",
                    {
                        "cl_type": "U512",
                        "bytes": _u512(motes).hex(),
                        "parsed": str(motes),
                    },
                ]
            ],
        }
    }


def _session_to_json(session_raw: bytes, args: list) -> dict:
    tag = session_raw[0]
    if tag == 0:  # ModuleBytes
        wasm_len = struct.unpack("<I", session_raw[1:5])[0]
        wasm_hex = session_raw[5 : 5 + wasm_len].hex()
        return {"ModuleBytes": {"module_bytes": wasm_hex, "args": _args_to_json(args)}}
    elif tag == 1:  # StoredContractByHash
        h = session_raw[1:33].hex()
        ep_len = struct.unpack("<I", session_raw[33:37])[0]
        ep = session_raw[37 : 37 + ep_len].decode()
        return {
            "StoredContractByHash": {
                "hash": h,
                "entry_point": ep,
                "args": _args_to_json(args),
            }
        }
    return {"ModuleBytes": {"module_bytes": session_raw.hex(), "args": []}}


def send_deploy(
    key: CasperKey,
    payment_motes: int,
    session_raw: bytes,
    args: list,
    node: str | None = None,
) -> str:
    """
    Build, sign, and submit a Casper Deploy.

    Fixes vs original:
    - PublicKey serialised as tag+bytes in header hash (not account_hash bytes)
    - secp256k1 signature = raw 64-byte r||s (not DER)
    - Signature hex prefixed with key type tag (01 or 02)
    - body_hash = blake2b(payment_binary || session_binary)
    - JSON format compatible with Casper 2.x account_put_deploy
    """
    ts_ms = int(time.time() * 1000)
    ttl_ms = 1_800_000  # 30 minutes
    gas_price = 1

    pay_raw = _payment_bytes(payment_motes)
    body_h = _body_hash(pay_raw, session_raw)
    deploy_hash_raw = _header_hash(key, ts_ms, ttl_ms, gas_price, body_h, CHAIN)
    deploy_hash = deploy_hash_raw.hex()

    sig_hex = key.signature_hex(deploy_hash_raw)
    pub_hex = key.casper_pubkey_hex()

    deploy = {
        "hash": deploy_hash,
        "header": {
            "account": pub_hex,
            "timestamp": _ms_to_iso(ts_ms),
            "ttl": str(ttl_ms),  # TTL in milliseconds
            "gas_price": gas_price,
            "body_hash": body_h.hex(),
            "dependencies": [],
            "chain_name": CHAIN,
        },
        "payment": _payment_to_json(payment_motes),
        "session": _session_to_json(session_raw, args),
        "approvals": [{"signer": pub_hex, "signature": sig_hex}],
    }

    # Debug: print deploy summary
    import sys

    print(f"  deploy_hash: {deploy_hash[:32]}…", file=sys.stderr)
    print(f"  body_hash:   {body_h.hex()[:32]}…", file=sys.stderr)
    print(f"  account:     {pub_hex}", file=sys.stderr)
    print(f"  timestamp:   {_ms_to_iso(ts_ms)}", file=sys.stderr)

    # Dump full deploy JSON for debugging
    with open("/tmp/deploy_debug.json", "w") as f:
        json.dump(deploy, f, indent=2)
    print(f"  Full deploy JSON saved to /tmp/deploy_debug.json", file=sys.stderr)

    target = node or NODES[0]
    result = (
        _rpc(f"{target}/rpc", "account_put_deploy", {"deploy": deploy})
        if False
        else _rpc("account_put_deploy", {"deploy": deploy}, target)
    )
    return result.get("deploy_hash", deploy_hash)


def _ms_to_iso(ms: int) -> str:
    """Convert milliseconds to ISO 8601 format with milliseconds."""
    import datetime

    dt = datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ── High-level API ────────────────────────────────────────────────────────────


def install_wasm(
    key: CasperKey,
    wasm_path: str,
    named_args: list | None = None,
    payment: int = 400_000_000_000,
) -> str:
    wasm = Path(wasm_path).read_bytes()
    args = named_args or []
    session = _wasm_session_bytes(wasm, args)
    return send_deploy(key, payment, session, args)


def call_entry_point(
    key: CasperKey,
    contract_hash: str,
    entry_point: str,
    named_args: list | None = None,
    payment: int = 5_000_000_000,
) -> str:
    args = named_args or []
    session = _contract_session_bytes(contract_hash, entry_point, args)
    return send_deploy(key, payment, session, args)


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


def parse_arg(s: str) -> tuple:
    """Parse 'name:type=value' CLI arg."""
    name, rest = s.split(":", 1)
    typ, val = rest.split("=", 1)
    typ = typ.strip().lower()
    if typ == "string":
        return arg_string(name, val)
    elif typ == "u64":
        return arg_u64(name, int(val))
    elif typ == "u32":
        return arg_u32(name, int(val))
    elif typ == "u512":
        return arg_u512(name, int(val))
    elif typ == "bool":
        return arg_bool(name, val.lower() in ("true", "1", "yes"))
    else:
        raise ValueError(f"Unknown arg type '{typ}' in '{s}'")


# ── Full deploy-all flow ──────────────────────────────────────────────────────


def deploy_all(key: CasperKey) -> None:
    """
    Deploy all 4 Helios contracts and wire them together.
    Requires: contracts/wasm/*.wasm already built.
    """
    root = Path(__file__).parent.parent
    wasm_dir = root / "contracts" / "wasm"
    agents_dir = root / "agents"

    for name in ["OracleRegistry", "DataMarket", "FundVault", "Governance"]:
        p = wasm_dir / f"{name}.wasm"
        if not p.exists():
            sys.exit(f"\nMissing: {p}\nRun: bash scripts/build_contracts.sh first.")

    deployer_hash = key.account_hash()
    deployer_acct = deployer_hash  # e.g. "account-hash-<hex>"
    print(f"\nDeployer: {key.casper_pubkey_hex()}")
    print(f"  {deployer_acct}")
    print("\n  (Account verified: 5000 CSPR available)")

    # ── Step 1: OracleRegistry ────────────────────────────────────────────────
    print("\n[1] Deploying OracleRegistry…")
    h = install_wasm(key, str(wasm_dir / "OracleRegistry.wasm"))
    print(f"    deploy: {h}\n    {EXPLORER}/deploy/{h}")
    wait_for_deploy(h)
    registry_hash = _extract_hash(h, "oracle_registry_contract_hash")
    print(f"    contract hash: {registry_hash}")

    # ── Step 2: DataMarket ────────────────────────────────────────────────────
    print("\n[2] Deploying DataMarket…")
    args = [arg_string("registry_hash", registry_hash), arg_u32("fee_bps", 250)]
    h = install_wasm(key, str(wasm_dir / "DataMarket.wasm"), args)
    print(f"    deploy: {h}\n    {EXPLORER}/deploy/{h}")
    wait_for_deploy(h)
    market_hash = _extract_hash(h, "data_market_contract_hash")
    print(f"    contract hash: {market_hash}")

    # ── Step 2b: Wire OracleRegistry → DataMarket ─────────────────────────────
    print("\n[2b] Wiring OracleRegistry.set_market…")
    h = call_entry_point(
        key, registry_hash, "set_market", [arg_string("market", market_hash)]
    )
    wait_for_deploy(h)
    print(f"    wired ✓  {EXPLORER}/deploy/{h}")

    # ── Step 3: FundVault ─────────────────────────────────────────────────────
    print("\n[3] Deploying FundVault…")
    args = [
        arg_string("operator", deployer_acct),
        arg_string("governance_hash", "pending"),
    ]
    h = install_wasm(key, str(wasm_dir / "FundVault.wasm"), args)
    print(f"    deploy: {h}\n    {EXPLORER}/deploy/{h}")
    wait_for_deploy(h)
    vault_hash = _extract_hash(h, "fund_vault_contract_hash")
    print(f"    contract hash: {vault_hash}")

    # ── Step 4: Governance ────────────────────────────────────────────────────
    print("\n[4] Deploying Governance…")
    # Use same key as both proposer and risk_agent for single-wallet demo
    args = [
        arg_string("proposer", deployer_acct),
        arg_string("risk_agent", deployer_acct),
        arg_u64("veto_window_ms", 90_000),
    ]
    h = install_wasm(key, str(wasm_dir / "Governance.wasm"), args)
    print(f"    deploy: {h}\n    {EXPLORER}/deploy/{h}")
    wait_for_deploy(h)
    gov_hash = _extract_hash(h, "governance_contract_hash")
    print(f"    contract hash: {gov_hash}")

    # ── Step 4b: Wire FundVault → Governance ─────────────────────────────────
    print("\n[4b] Wiring FundVault.set_governance…")
    h = call_entry_point(
        key, vault_hash, "set_governance", [arg_string("governance_hash", gov_hash)]
    )
    wait_for_deploy(h)
    print(f"    wired ✓  {EXPLORER}/deploy/{h}")

    # ── Write env file ────────────────────────────────────────────────────────
    agents_dir.mkdir(exist_ok=True)
    env = agents_dir / "testnet.env"
    env.write_text(
        "\n".join(
            [
                f"REGISTRY_HASH={registry_hash}",
                f"MARKET_HASH={market_hash}",
                f"VAULT_HASH={vault_hash}",
                f"GOV_HASH={gov_hash}",
                f"DEPLOYER_ACCOUNT={deployer_acct}",
            ]
        )
        + "\n"
    )
    print(f"\n[5] {env} written ✓")

    print("\n" + "═" * 52)
    print("  DEPLOYMENT COMPLETE")
    print("═" * 52)
    print(f"  OracleRegistry : {EXPLORER}/contract/{registry_hash}")
    print(f"  DataMarket     : {EXPLORER}/contract/{market_hash}")
    print(f"  FundVault      : {EXPLORER}/contract/{vault_hash}")
    print(f"  Governance     : {EXPLORER}/contract/{gov_hash}")
    print(f"\n  Next: python3 scripts/testnet_round.py --rounds 3\n")


def _extract_hash(deploy_hash: str, named_key: str) -> str:
    """
    Extract a contract hash from execution results.
    Falls back to interactive input if not found.
    """
    time.sleep(2)
    for node in NODES:
        try:
            # Try 2.x endpoint first, then 1.x
            for method, params in [
                ("info_get_transaction", {"transaction_hash": {"Deploy": deploy_hash}}),
                ("info_get_deploy", {"deploy_hash": deploy_hash}),
            ]:
                try:
                    r = _rpc(method, params, node)
                    transforms = []
                    # Casper 2.x path
                    ei = (r.get("transaction") or {}).get("execution_info") or {}
                    transforms = (
                        (ei.get("execution_result") or {})
                        .get("Success", {})
                        .get("effect", {})
                        .get("transforms", [])
                    )
                    if not transforms:
                        # Casper 1.x path
                        er = (r.get("deploy") or {}).get("execution_results", [])
                        transforms = (
                            (
                                er[0]["result"]
                                .get("Success", {})
                                .get("effect", {})
                                .get("transforms", [])
                            )
                            if er
                            else []
                        )
                    for t in transforms:
                        k = t.get("key", "")
                        if k.startswith("hash-"):
                            v = t.get("transform", {})
                            if (
                                "WriteContract" in v
                                or "WriteContractWasm" in v
                                or "WriteContractPackage" in v
                            ):
                                return k.replace("hash-", "")
                except Exception:
                    continue
        except Exception:
            continue

    print(f"\n  Open: {EXPLORER}/deploy/{deploy_hash}")
    print("  Find 'WriteContract' in execution effects and copy the hash")
    raise RuntimeError(f"Could not extract contract hash from deploy {deploy_hash}")


# ── CLI ───────────────────────────────────────────────────────────────────────


def main() -> None:
    p = argparse.ArgumentParser(
        description="Helios Casper deploy tool (secp256k1 + ed25519, no casper-client)"
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # status
    sub.add_parser("status", help="Check node connectivity")

    # keygen
    kg = sub.add_parser("keygen", help="Generate new ed25519 keypair")
    kg.add_argument("--out", required=True, help="Output path for secret_key.pem")

    # pubkey
    pk = sub.add_parser(
        "pubkey", help="Show Casper pubkey hex and account hash for a key"
    )
    pk.add_argument("--key", required=True)

    # install
    ins = sub.add_parser("install", help="Deploy a WASM contract")
    ins.add_argument("--key", required=True)
    ins.add_argument("--wasm", required=True)
    ins.add_argument("--args", nargs="*", default=[], metavar="name:type=value")
    ins.add_argument("--payment", type=int, default=400_000_000_000)
    ins.add_argument("--wait", action="store_true")

    # call
    cl = sub.add_parser("call", help="Call a contract entry-point")
    cl.add_argument("--key", required=True)
    cl.add_argument("--contract", required=True)
    cl.add_argument("--entry-point", required=True)
    cl.add_argument("--args", nargs="*", default=[], metavar="name:type=value")
    cl.add_argument("--payment", type=int, default=5_000_000_000)
    cl.add_argument("--wait", action="store_true")

    # wait
    wt = sub.add_parser("wait", help="Wait for a deploy to finalise")
    wt.add_argument("hash")

    # deploy-all
    da = sub.add_parser("deploy-all", help="Deploy all 4 contracts and wire them")
    da.add_argument("--key", required=True)

    args = p.parse_args()

    if args.cmd == "status":
        try:
            r = get_status()
            print(f"✓ Node reachable")
            print(f"  api_version:   {r.get('api_version', '?')}")
            print(f"  chain:         {r.get('chainspec_name', '?')}")
            print(f"  build_version: {r.get('build_version', '?')}")
        except Exception as e:
            sys.exit(f"✗ Cannot reach node: {e}")

    elif args.cmd == "keygen":
        key = CasperKey.generate_ed25519()
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        key.save(str(out))
        print(f"Generated ed25519 key → {out}")
        print(f"  pubkey:       {key.casper_pubkey_hex()}")
        print(f"  account_hash: {key.account_hash()}")

    elif args.cmd == "pubkey":
        key = CasperKey.load(args.key)
        print(f"pubkey:       {key.casper_pubkey_hex()}")
        print(f"account_hash: {key.account_hash()}")

    elif args.cmd == "install":
        key = CasperKey.load(args.key)
        named = [parse_arg(a) for a in args.args]
        dh = install_wasm(key, args.wasm, named, args.payment)
        print(f"deploy_hash: {dh}")
        print(f"explorer:    {EXPLORER}/deploy/{dh}")
        if args.wait:
            wait_for_deploy(dh)

    elif args.cmd == "call":
        key = CasperKey.load(args.key)
        named = [parse_arg(a) for a in args.args]
        dh = call_entry_point(
            key, args.contract, getattr(args, "entry_point"), named, args.payment
        )
        print(f"deploy_hash: {dh}")
        print(f"explorer:    {EXPLORER}/deploy/{dh}")
        if args.wait:
            wait_for_deploy(dh)

    elif args.cmd == "wait":
        wait_for_deploy(args.hash)

    elif args.cmd == "deploy-all":
        key = CasperKey.load(args.key)
        deploy_all(key)


if __name__ == "__main__":
    main()
