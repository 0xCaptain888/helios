#!/usr/bin/env python3
"""
Helios Casper Testnet Deployer — pure Python, zero Rust/casper-client required.

How it works
------------
Casper's node RPC accepts JSON-RPC calls. A "Deploy" (Casper 1.x) or
"Transaction" (Casper 2.x) is a JSON structure containing:
  - account public key
  - payment amount
  - session (wasm bytes OR stored-contract call)
  - signatures

This script implements the minimal Casper deploy serialization needed to:
  1. Install a WASM contract  (deploy_wasm)
  2. Call a stored contract   (call_contract)
  3. Query global state       (query_contract)
  4. Poll until finalized     (wait_for_deploy)

Crypto: ed25519 via `cryptography` (already in your Python env).
Serialization: Casper's ToBytes spec (hand-rolled, no external SDK needed).
"""

from __future__ import annotations
import hashlib, json, os, struct, time, urllib.error, urllib.request
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
    PrivateFormat,
    NoEncryption,
    load_pem_private_key,
)

# ── Node endpoints (fallback list) ────────────────────────────────────────────
NODES = [
    "https://rpc.testnet.cspr.cloud",
    "https://node.testnet.casper.network",
]
CHAIN = "casper-test"
EXPLORER = "https://testnet.cspr.live"

# ── Low-level Casper serialization ────────────────────────────────────────────


def _u32_le(n: int) -> bytes:
    return struct.pack("<I", n)


def _u64_le(n: int) -> bytes:
    return struct.pack("<Q", n)


def _bytes_with_u32_len(b: bytes) -> bytes:
    return _u32_le(len(b)) + b


def _string(s: str) -> bytes:
    b = s.encode("utf-8")
    return _bytes_with_u32_len(b)


def _option_some(b: bytes) -> bytes:
    return b"\x01" + b


def _option_none() -> bytes:
    return b"\x00"


# CLType tags
_CL_BOOL = b"\x00"
_CL_U8 = b"\x07"
_CL_U32 = b"\x08"
_CL_U64 = b"\x09"
_CL_U512 = b"\x0b"
_CL_STRING = b"\x0a"
_CL_LIST = b"\x14"
_CL_UNIT = b"\x06"


def _cl_value(cl_type: bytes, value_bytes: bytes) -> bytes:
    """Serialise a CLValue: (u32 length)(value bytes)(cltype tag...)"""
    return _bytes_with_u32_len(value_bytes) + cl_type


def _u512_bytes(n: int) -> bytes:
    """U512 as little-endian with leading length byte (Casper compact encoding)."""
    if n == 0:
        return b"\x00"
    b = n.to_bytes((n.bit_length() + 7) // 8, "little")
    return bytes([len(b)]) + b


def _named_arg(name: str, cl_type: bytes, value_bytes: bytes) -> bytes:
    return _string(name) + _cl_value(cl_type, value_bytes)


# ── Account hash derivation ───────────────────────────────────────────────────


def pubkey_to_account_hash(pub_bytes: bytes) -> str:
    prefix = b"ed25519" + b"\x00" + pub_bytes
    return hashlib.blake2b(prefix, digest_size=32).hexdigest()


def pubkey_hex_with_algo(pub_bytes: bytes) -> str:
    """'01' prefix = ed25519 (Casper convention)."""
    return "01" + pub_bytes.hex()


# ── Key loading ───────────────────────────────────────────────────────────────


def load_key(path: str) -> Ed25519PrivateKey:
    pem = Path(path).read_bytes()
    return load_pem_private_key(pem, password=None)


def generate_key(out_path: str) -> Ed25519PrivateKey:
    """Generate a new ed25519 keypair and save as PEM."""
    key = Ed25519PrivateKey.generate()
    Path(out_path).write_bytes(
        key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    )
    pub = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    Path(out_path.replace("secret_key.pem", "public_key.pem")).write_bytes(
        key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    )
    Path(out_path.replace("secret_key.pem", "public_key_hex")).write_text(
        pubkey_hex_with_algo(pub)
    )
    return key


# ── RPC helpers ───────────────────────────────────────────────────────────────


def _rpc(method: str, params: dict | list, node: str = NODES[0]) -> Any:
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
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except urllib.error.URLError as e:
        raise RuntimeError(f"RPC error ({node}): {e}") from e
    if "error" in data:
        raise RuntimeError(f"RPC error: {data['error']}")
    return data.get("result", data)


def get_state_root_hash(node: str = NODES[0]) -> str:
    result = _rpc("chain_get_state_root_hash", {}, node)
    return result["state_root_hash"]


def get_account_info(account_hash: str, node: str = NODES[0]) -> dict:
    srh = get_state_root_hash(node)
    result = _rpc(
        "state_get_item",
        {
            "state_root_hash": srh,
            "key": f"account-hash-{account_hash}",
            "path": [],
        },
        node,
    )
    return result


def get_deploy_status(deploy_hash: str, node: str = NODES[0]) -> dict:
    return _rpc(
        "info_get_transaction", {"transaction_hash": {"Deploy": deploy_hash}}, node
    )


def wait_for_deploy(deploy_hash: str, timeout: int = 180, node: str = NODES[0]) -> dict:
    print(f"   waiting for {deploy_hash[:16]}…", end="", flush=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            result = get_deploy_status(deploy_hash, node)
            txn = result.get("transaction", {}) or result.get("deploy", {})
            exec_results = txn.get("execution_info", {}) or txn.get(
                "execution_results", []
            )
            if exec_results:
                print(" ✓")
                return result
        except Exception:
            pass
        print(".", end="", flush=True)
        time.sleep(4)
    print(" TIMEOUT")
    raise TimeoutError(f"Deploy {deploy_hash} not finalized within {timeout}s")


# ── Deploy construction ───────────────────────────────────────────────────────


def _make_deploy_hash(
    account_hash: str,
    timestamp_ms: int,
    ttl_ms: int,
    gas_price: int,
    body_hash: str,
    dependencies: list,
    chain_name: str,
) -> bytes:
    """Compute the Casper deploy hash (header hash)."""
    header_bytes = (
        bytes.fromhex(account_hash)  # account hash 32 bytes
        + _u64_le(timestamp_ms)
        + _u64_le(ttl_ms)
        + _u64_le(gas_price)
        + bytes.fromhex(body_hash)  # body hash 32 bytes
        + _u32_le(len(dependencies))  # no dependencies
        + _string(chain_name)
    )
    return hashlib.blake2b(header_bytes, digest_size=32).digest()


def _body_hash(payment_bytes: bytes, session_bytes: bytes) -> str:
    combined = payment_bytes + session_bytes
    return hashlib.blake2b(combined, digest_size=32).hexdigest()


def _standard_payment(amount_motes: int) -> bytes:
    """ModuleBytes payment with 'amount' arg."""
    module_bytes = b""  # empty = use standard payment contract
    args_bytes = _named_arg("amount", _CL_U512, _u512_bytes(amount_motes))
    named_args_count = _u32_le(1)
    args_serialized = named_args_count + args_bytes
    return (
        b"\x00"
        + _bytes_with_u32_len(module_bytes)
        + _bytes_with_u32_len(args_serialized)
    )


def _wasm_session(
    wasm_bytes: bytes, named_args: list[tuple[str, bytes, bytes]]
) -> bytes:
    """ModuleBytes session for WASM install."""
    args_list = b"".join(_named_arg(n, t, v) for n, t, v in named_args)
    args_serialized = _u32_le(len(named_args)) + args_list
    return (
        b"\x00" + _bytes_with_u32_len(wasm_bytes) + _bytes_with_u32_len(args_serialized)
    )


def _stored_contract_session(
    contract_hash: str, entry_point: str, named_args: list[tuple[str, bytes, bytes]]
) -> bytes:
    """StoredContractByHash session for calling deployed contracts."""
    args_list = b"".join(_named_arg(n, t, v) for n, t, v in named_args)
    args_serialized = _u32_le(len(named_args)) + args_list
    hash_bytes = bytes.fromhex(contract_hash)
    ep_bytes = _string(entry_point)
    return b"\x01" + hash_bytes + ep_bytes + _bytes_with_u32_len(args_serialized)


def build_and_send_deploy(
    key: Ed25519PrivateKey,
    payment_motes: int,
    session_bytes: bytes,
    node: str = NODES[0],
) -> str:
    """Sign and submit a deploy; return the deploy hash."""
    pub_bytes = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    account_hash = pubkey_to_account_hash(pub_bytes)
    pubkey_str = pubkey_hex_with_algo(pub_bytes)

    now_ms = int(time.time() * 1000)
    ttl_ms = 1_800_000  # 30 minutes
    gas_price = 1

    payment_ser = _standard_payment(payment_motes)
    body_h = _body_hash(payment_ser, session_bytes)
    deploy_hash_bytes = _make_deploy_hash(
        account_hash, now_ms, ttl_ms, gas_price, body_h, [], CHAIN
    )
    deploy_hash = deploy_hash_bytes.hex()

    signature_bytes = key.sign(deploy_hash_bytes)

    deploy = {
        "hash": deploy_hash,
        "header": {
            "account": pubkey_str,
            "timestamp": _ms_to_iso(now_ms),
            "ttl": "30m",
            "gas_price": gas_price,
            "body_hash": body_h,
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
                            "bytes": _u512_bytes(payment_motes).hex(),
                            "parsed": str(payment_motes),
                        },
                    ]
                ],
            }
        },
        "session": _session_json(session_bytes),
        "approvals": [
            {
                "signer": pubkey_str,
                "signature": "01" + signature_bytes.hex(),
            }
        ],
    }

    result = _rpc("account_put_deploy", {"deploy": deploy}, node)
    returned_hash = result.get("deploy_hash", deploy_hash)
    return returned_hash


def _ms_to_iso(ms: int) -> str:
    t = time.gmtime(ms // 1000)
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", t)


def _session_json(session_bytes: bytes) -> dict:
    """Reverse-engineer the deploy session type from our serialized bytes."""
    tag = session_bytes[0:1]
    if tag == b"\x00":
        # ModuleBytes
        rest = session_bytes[1:]
        mb_len = struct.unpack("<I", rest[:4])[0]
        mb = rest[4 : 4 + mb_len]
        return {"ModuleBytes": {"module_bytes": mb.hex(), "args": []}}
    elif tag == b"\x01":
        # StoredContractByHash — parse back to JSON
        hash_hex = session_bytes[1:33].hex()
        rest = session_bytes[33:]
        ep_len = struct.unpack("<I", rest[:4])[0]
        ep = rest[4 : 4 + ep_len].decode()
        return {
            "StoredContractByHash": {"hash": hash_hex, "entry_point": ep, "args": []}
        }
    return {"ModuleBytes": {"module_bytes": session_bytes.hex(), "args": []}}


# ── High-level deploy functions ───────────────────────────────────────────────


def install_wasm(
    key: Ed25519PrivateKey,
    wasm_path: str,
    named_args: list | None = None,
    payment: int = 350_000_000_000,
    node: str = NODES[0],
) -> str:
    wasm = Path(wasm_path).read_bytes()
    session = _wasm_session(wasm, named_args or [])
    return build_and_send_deploy(key, payment, session, node)


def call_entry_point(
    key: Ed25519PrivateKey,
    contract_hash: str,
    entry_point: str,
    named_args: list | None = None,
    payment: int = 5_000_000_000,
    node: str = NODES[0],
) -> str:
    session = _stored_contract_session(contract_hash, entry_point, named_args or [])
    return build_and_send_deploy(key, payment, session, node)


# ── Arg helpers ───────────────────────────────────────────────────────────────


def arg_string(name: str, value: str):
    return (name, _CL_STRING, _string(value))


def arg_u64(name: str, value: int):
    return (name, _CL_U64, _u64_le(value))


def arg_u32(name: str, value: int):
    return (name, _CL_U32, _u32_le(value))


def arg_u512(name: str, value: int):
    return (name, _CL_U512, _u512_bytes(value))


# ── CLI entry-point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Helios Casper deploy tool (pure Python)"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # keygen
    kg = sub.add_parser("keygen", help="Generate 5 keypairs for Helios agents")
    kg.add_argument("--out", default="keys", help="Output directory (default: keys/)")

    # status
    st = sub.add_parser("status", help="Check testnet node status")
    st.add_argument("--node", default=NODES[0])

    # install
    inst = sub.add_parser("install", help="Install a WASM contract")
    inst.add_argument("--key", required=True, help="Path to secret_key.pem")
    inst.add_argument("--wasm", required=True, help="Path to .wasm file")
    inst.add_argument(
        "--args",
        nargs="*",
        default=[],
        help="name:type=value  (types: string, u64, u32, u512)",
    )
    inst.add_argument("--payment", type=int, default=350_000_000_000)
    inst.add_argument("--node", default=NODES[0])
    inst.add_argument("--wait", action="store_true")

    # call
    cl = sub.add_parser("call", help="Call a stored contract entry-point")
    cl.add_argument("--key", required=True)
    cl.add_argument("--contract", required=True, help="Contract hash (hex, no prefix)")
    cl.add_argument("--entry-point", required=True)
    cl.add_argument(
        "--args",
        nargs="*",
        default=[],
        help="name:type=value  (types: string, u64, u32, u512)",
    )
    cl.add_argument("--payment", type=int, default=5_000_000_000)
    cl.add_argument("--node", default=NODES[0])
    cl.add_argument("--wait", action="store_true")

    # wait
    wt = sub.add_parser("wait", help="Wait for a deploy to finalize")
    wt.add_argument("deploy_hash")
    wt.add_argument("--node", default=NODES[0])

    args = parser.parse_args()

    if args.cmd == "keygen":
        roles = [
            "oracle_tbill",
            "oracle_gold",
            "oracle_reindex",
            "fund_agent",
            "risk_agent",
        ]
        out_dir = Path(args.out)
        env_lines = []
        print(f"\nGenerating {len(roles)} keypairs in {out_dir}/\n")
        for role in roles:
            role_dir = out_dir / role
            role_dir.mkdir(parents=True, exist_ok=True)
            key = generate_key(str(role_dir / "secret_key.pem"))
            pub = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
            acct = pubkey_to_account_hash(pub)
            print(f"  {role}")
            print(f"    pubkey : {pubkey_hex_with_algo(pub)}")
            print(f"    account: account-hash-{acct}")
            env_lines.append(f"{role.upper()}_KEY={role_dir}/secret_key.pem")
        env_path = out_dir / "accounts.txt"
        env_path.write_text("\n".join(env_lines) + "\n")
        print(f"\nKey paths written to {env_path}")
        print("Fund each account at https://testnet.cspr.live/tools/faucet")
        print("Use the public_key_hex file contents as the faucet input.\n")

    elif args.cmd == "status":
        try:
            result = _rpc("info_get_status", {}, args.node)
            print(f"Node: {args.node}")
            print(f"Chain: {result.get('chainspec_name', '?')}")
            print(f"Peers: {result.get('peers', {})}")
            print("✓ Testnet reachable")
        except Exception as e:
            print(f"✗ Cannot reach node: {e}")

    elif args.cmd == "install":
        key = load_key(args.key)
        pub = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        print(f"Deployer: account-hash-{pubkey_to_account_hash(pub)}")
        print(f"WASM: {args.wasm} ({Path(args.wasm).stat().st_size:,} bytes)")

        named = []
        for a in args.args:
            name, rest = a.split(":", 1)
            typ, val = rest.split("=", 1)
            if typ == "string":
                named.append(arg_string(name, val))
            elif typ == "u64":
                named.append(arg_u64(name, int(val)))
            elif typ == "u32":
                named.append(arg_u32(name, int(val)))
            elif typ == "u512":
                named.append(arg_u512(name, int(val)))

        deploy_hash = install_wasm(
            key, args.wasm, named_args=named, payment=args.payment, node=args.node
        )
        print(f"Deploy hash : {deploy_hash}")
        print(f"Explorer    : {EXPLORER}/deploy/{deploy_hash}")
        if args.wait:
            wait_for_deploy(deploy_hash, node=args.node)

    elif args.cmd == "call":
        key = load_key(args.key)
        named = []
        for a in args.args:
            name, rest = a.split(":", 1)
            typ, val = rest.split("=", 1)
            if typ == "string":
                named.append(arg_string(name, val))
            elif typ == "u64":
                named.append(arg_u64(name, int(val)))
            elif typ == "u32":
                named.append(arg_u32(name, int(val)))
            elif typ == "u512":
                named.append(arg_u512(name, int(val)))
        deploy_hash = call_entry_point(
            key, args.contract, args.entry_point, named, args.payment, args.node
        )
        print(f"Deploy hash : {deploy_hash}")
        print(f"Explorer    : {EXPLORER}/deploy/{deploy_hash}")
        if args.wait:
            wait_for_deploy(deploy_hash, node=args.node)

    elif args.cmd == "wait":
        wait_for_deploy(args.deploy_hash, node=args.node)
