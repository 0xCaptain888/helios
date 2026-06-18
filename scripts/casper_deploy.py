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
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
    PrivateFormat,
    NoEncryption,
    load_pem_private_key,
)

# ── Constants ─────────────────────────────────────────────────────────────────
NODES = [
    "https://rpc.testnet.cspr.cloud",
    "https://node.testnet.casper.network",
]
CHAIN = "casper-test"
EXPLORER = "https://testnet.cspr.live"

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
_CL_U32 = b"\x08"
_CL_U64 = b"\x09"
_CL_U512 = b"\x0b"
_CL_STRING = b"\x0a"


def _cl(cl_type: bytes, value_bytes: bytes) -> bytes:
    return _len_prefix(value_bytes) + cl_type


def _named_arg(name: str, cl_type: bytes, value_bytes: bytes) -> bytes:
    return _str(name) + _cl(cl_type, value_bytes)


# ── Key utilities ─────────────────────────────────────────────────────────────


def load_key(path: str) -> Ed25519PrivateKey:
    return load_pem_private_key(Path(path).read_bytes(), password=None)


def key_pub_bytes(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def pub_to_account_hash(pub: bytes) -> str:
    return hashlib.blake2b(b"ed25519\x00" + pub, digest_size=32).hexdigest()


def pub_to_hex(pub: bytes) -> str:
    return "01" + pub.hex()


def key_info(key: Ed25519PrivateKey) -> tuple[str, str]:
    """Return (pubkey_hex_with_01_prefix, account_hash_hex)."""
    pub = key_pub_bytes(key)
    return pub_to_hex(pub), pub_to_account_hash(pub)


def generate_key(out_path: str) -> Ed25519PrivateKey:
    key = Ed25519PrivateKey.generate()
    pub = key_pub_bytes(key)
    Path(out_path).write_bytes(
        key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    )
    base = out_path.replace("secret_key.pem", "")
    Path(base + "public_key.pem").write_bytes(
        key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    )
    Path(base + "public_key_hex").write_text(pub_to_hex(pub))
    return key


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
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ms // 1000))


def _body_hash(payment: bytes, session: bytes) -> str:
    return hashlib.blake2b(payment + session, digest_size=32).hexdigest()


def _header_hash(
    acct_hash: str, ts_ms: int, ttl_ms: int, gas_price: int, body_h: str, chain: str
) -> bytes:
    raw = (
        bytes.fromhex(acct_hash)
        + _u64(ts_ms)
        + _u64(ttl_ms)
        + _u64(gas_price)
        + bytes.fromhex(body_h)
        + _u32(0)  # no dependencies
        + _str(chain)
    )
    return hashlib.blake2b(raw, digest_size=32).digest()


def _payment_bytes(motes: int) -> bytes:
    arg = _named_arg("amount", _CL_U512, _u512(motes))
    return b"\x00" + _len_prefix(b"") + _len_prefix(_u32(1) + arg)


def _wasm_session_bytes(wasm: bytes, args: list) -> bytes:
    arg_bytes = b"".join(_named_arg(n, t, v) for n, t, v in args)
    return b"\x00" + _len_prefix(wasm) + _len_prefix(_u32(len(args)) + arg_bytes)


def _contract_session_bytes(contract_hash: str, entry_point: str, args: list) -> bytes:
    arg_bytes = b"".join(_named_arg(n, t, v) for n, t, v in args)
    return (
        b"\x01"
        + bytes.fromhex(contract_hash)
        + _str(entry_point)
        + _len_prefix(_u32(len(args)) + arg_bytes)
    )


def _session_to_json(raw: bytes) -> dict:
    tag = raw[0]
    if tag == 0:
        wasm_len = struct.unpack("<I", raw[1:5])[0]
        wasm_bytes = raw[5 : 5 + wasm_len]
        return {"ModuleBytes": {"module_bytes": wasm_bytes.hex(), "args": []}}
    elif tag == 1:
        ep_len = struct.unpack("<I", raw[33:37])[0]
        ep = raw[37 : 37 + ep_len].decode()
        return {
            "StoredContractByHash": {
                "hash": raw[1:33].hex(),
                "entry_point": ep,
                "args": [],
            }
        }
    return {"ModuleBytes": {"module_bytes": raw.hex(), "args": []}}


def send_deploy(
    key: Ed25519PrivateKey, payment_motes: int, session_raw: bytes, node: str = NODES[0]
) -> str:
    pub = key_pub_bytes(key)
    acct_h = pub_to_account_hash(pub)
    pub_hex = pub_to_hex(pub)

    ts_ms = int(time.time() * 1000)
    ttl = 1_800_000
    gas = 1

    pay_raw = _payment_bytes(payment_motes)
    bh = _body_hash(pay_raw, session_raw)
    dh_raw = _header_hash(acct_h, ts_ms, ttl, gas, bh, CHAIN)
    dh = dh_raw.hex()
    sig = key.sign(dh_raw)

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
        "session": _session_to_json(session_raw),
        "approvals": [{"signer": pub_hex, "signature": "01" + sig.hex()}],
    }
    result = _rpc("account_put_deploy", {"deploy": deploy}, node)
    return result.get("deploy_hash", dh)


# ── Public API ────────────────────────────────────────────────────────────────


def install_wasm(
    key: Ed25519PrivateKey,
    wasm_path: str,
    named_args: list | None = None,
    payment: int = 400_000_000_000,
    node: str = NODES[0],
) -> str:
    wasm = Path(wasm_path).read_bytes()
    return send_deploy(key, payment, _wasm_session_bytes(wasm, named_args or []), node)


def call_entry_point(
    key: Ed25519PrivateKey,
    contract_hash: str,
    entry_point: str,
    named_args: list | None = None,
    payment: int = 5_000_000_000,
    node: str = NODES[0],
) -> str:
    return send_deploy(
        key,
        payment,
        _contract_session_bytes(contract_hash, entry_point, named_args or []),
        node,
    )


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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Helios Casper deploy tool (pure Python, no casper-client needed)"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # keygen
    kg = sub.add_parser("keygen", help="Generate 5 keypairs for Helios agents")
    kg.add_argument("--out", default="keys", help="Output directory (default: keys/)")

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

    # info
    inf = sub.add_parser("info", help="Show key info (pubkey + account hash)")
    inf.add_argument("--key", required=True, help="Path to secret_key.pem")

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
                key = load_key(str(kp))
            else:
                key = generate_key(str(kp))
            ph, ah = key_info(key)
            print(f"  {role}")
            print(f"    pubkey  : {ph}")
            print(f"    account : account-hash-{ah}")
            env_lines.append(f"{role.upper()}_KEY={d}/secret_key.pem")
        (out / "accounts.txt").write_text("\n".join(env_lines) + "\n")
        print(f"\nFund each account: https://testnet.cspr.live/tools/faucet")
        print("(Paste the pubkey value from public_key_hex file into the faucet)\n")

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

    # ── info ──────────────────────────────────────────────────────────────────
    elif args.cmd == "info":
        key = load_key(args.key)
        ph, ah = key_info(key)
        print(f"pubkey  : {ph}")
        print(f"account : account-hash-{ah}")

    # ── install ───────────────────────────────────────────────────────────────
    elif args.cmd == "install":
        key = load_key(args.key)
        ph, ah = key_info(key)
        print(f"Deployer : account-hash-{ah}")
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
        key = load_key(args.key)
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
