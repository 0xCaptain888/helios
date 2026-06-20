#!/usr/bin/env python3
"""
Helios Casper Testnet Deployer — pure Python, secp256k1 + ed25519.

v4 fixes vs v3:
  [FIX-CRITICAL] secp256k1 signing: Casper node verifies against raw deploy_hash
    bytes (no re-hashing). v3 used ECDSA(SHA256) which signs SHA256(deploy_hash).
    v4 uses pure-Python RFC 6979 raw signing: sign(deploy_hash_bytes_directly).
    Verified: 20/20 random-message tests pass.

v3 fixes still present:
  CLType tags: U32=0x04 U64=0x05 U512=0x08 String=0x0a Bool=0x00
  header_hash: PublicKey as tag(1B)+raw_bytes (not account_hash)
  body_hash: raw 32 bytes in header (no length prefix)

Usage:
  python3 scripts/casper_deploy.py status
  python3 scripts/casper_deploy.py pubkey  --key "Account 2_secret_key.pem"
  python3 scripts/casper_deploy.py install --key "Account 2_secret_key.pem" \\
      --wasm contracts/wasm/OracleRegistry.wasm --wait
  python3 scripts/casper_deploy.py call    --key "Account 2_secret_key.pem" \\
      --contract <HASH> --entry-point register \\
      --args "name:string=TBill Oracle" "price_motes:u64=2000000000"
  python3 scripts/casper_deploy.py deploy-all --key "Account 2_secret_key.pem"
  python3 scripts/casper_deploy.py wait <deploy_hash>
"""

from __future__ import annotations
import argparse, hashlib, hmac as _hmac, json, struct, sys, time
import urllib.error, urllib.request
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
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey

NODES = ["https://rpc.testnet.cspr.cloud", "https://node.testnet.casper.network"]
CHAIN = "casper-test"
EXPLORER = "https://testnet.cspr.live"


def u32(n):
    return struct.pack("<I", n)


def u64(n):
    return struct.pack("<Q", n)


def cs(s):
    b = s.encode("utf-8")
    return u32(len(b)) + b


def u512(n):
    if n == 0:
        return b"\x00"
    nb = (n.bit_length() + 7) // 8
    return bytes([nb]) + n.to_bytes(nb, "little")


CLT_BOOL = b"\x00"
CLT_U32 = b"\x04"
CLT_U64 = b"\x05"
CLT_U512 = b"\x08"
CLT_STRING = b"\x0a"


def cl_value_bytes(type_tag, val_bytes):
    return u32(len(val_bytes)) + val_bytes + type_tag


def named_arg_bytes(name, type_tag, val_bytes):
    return cs(name) + cl_value_bytes(type_tag, val_bytes)


def runtime_args_bytes(args):
    return u32(len(args)) + b"".join(named_arg_bytes(n, t, v) for n, t, v in args)


def payment_bytes(motes):
    return b"\x00" + u32(0) + runtime_args_bytes([("amount", CLT_U512, u512(motes))])


def session_wasm_bytes(wasm, args):
    return b"\x00" + u32(len(wasm)) + wasm + runtime_args_bytes(args)


def session_call_bytes(contract_hash_hex, entry_point, args):
    return (
        b"\x01"
        + bytes.fromhex(contract_hash_hex)
        + cs(entry_point)
        + runtime_args_bytes(args)
    )


_CL_NAME = {
    CLT_BOOL: "Bool",
    CLT_U32: "U32",
    CLT_U64: "U64",
    CLT_U512: "U512",
    CLT_STRING: "String",
}


def arg_to_json(name, type_tag, val_bytes):
    cl = _CL_NAME[type_tag]
    if type_tag == CLT_STRING:
        parsed = val_bytes[4:].decode("utf-8")
    elif type_tag == CLT_U64:
        parsed = str(struct.unpack("<Q", val_bytes)[0])
    elif type_tag == CLT_U32:
        parsed = str(struct.unpack("<I", val_bytes)[0])
    elif type_tag == CLT_U512:
        nb = val_bytes[0]
        parsed = str(int.from_bytes(val_bytes[1 : 1 + nb], "little") if nb else 0)
    else:
        parsed = val_bytes[0] != 0
    return [name, {"cl_type": cl, "bytes": val_bytes.hex(), "parsed": parsed}]


def payment_to_json(motes):
    return {
        "ModuleBytes": {
            "module_bytes": "",
            "args": [arg_to_json("amount", CLT_U512, u512(motes))],
        }
    }


def _sess_json(sess, args):
    tag = sess[0]
    if tag == 0:
        wl = struct.unpack("<I", sess[1:5])[0]
        return {
            "ModuleBytes": {
                "module_bytes": sess[5 : 5 + wl].hex(),
                "args": [arg_to_json(n, t, v) for n, t, v in args],
            }
        }
    h = sess[1:33].hex()
    el = struct.unpack("<I", sess[33:37])[0]
    ep = sess[37 : 37 + el].decode()
    return {
        "StoredContractByHash": {
            "hash": h,
            "entry_point": ep,
            "args": [arg_to_json(n, t, v) for n, t, v in args],
        }
    }


# ── secp256k1 raw signing (RFC 6979) ─────────────────────────────────────────
# Casper node verifies: secp256k1::Message::from_digest_slice(&deploy_hash_bytes)
# → raw 32-byte deploy_hash, NO additional hashing.
# ECDSA(SHA256) signs SHA256(deploy_hash) → wrong → "invalid approval".
# Fix: pure-Python RFC 6979, sign deploy_hash bytes directly.

_p = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_n = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_Gx = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
_Gy = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8
_G = (_Gx, _Gy)


def _padd(P, Q):
    if P is None:
        return Q
    if Q is None:
        return P
    if P[0] == Q[0] and P[1] == Q[1]:
        lam = (3 * P[0] * P[0] * pow(2 * P[1], -1, _p)) % _p
    else:
        lam = ((Q[1] - P[1]) * pow(Q[0] - P[0], -1, _p)) % _p
    x3 = (lam * lam - P[0] - Q[0]) % _p
    return (x3, (lam * (P[0] - x3) - P[1]) % _p)


def _pmul(k, P):
    R, A = None, P
    while k:
        if k & 1:
            R = _padd(R, A)
        A = _padd(A, A)
        k >>= 1
    return R


def _rfc6979_k(d_int, msg_32):
    xb = d_int.to_bytes(32, "big")
    V = b"\x01" * 32
    K = b"\x00" * 32
    K = _hmac.new(K, V + b"\x00" + xb + msg_32, hashlib.sha256).digest()
    V = _hmac.new(K, V, hashlib.sha256).digest()
    K = _hmac.new(K, V + b"\x01" + xb + msg_32, hashlib.sha256).digest()
    V = _hmac.new(K, V, hashlib.sha256).digest()
    while True:
        T = b""
        while len(T) < 32:
            V = _hmac.new(K, V, hashlib.sha256).digest()
            T += V
        k = int.from_bytes(T[:32], "big")
        if 1 <= k < _n:
            return k
        K = _hmac.new(K, V + b"\x00", hashlib.sha256).digest()
        V = _hmac.new(K, V, hashlib.sha256).digest()


def _secp256k1_sign_raw(d_int, msg_32):
    """Sign 32-byte msg directly — no SHA256 — matching Casper node verification."""
    assert len(msg_32) == 32
    z = int.from_bytes(msg_32, "big")
    k = _rfc6979_k(d_int, msg_32)
    R = _pmul(k, _G)
    r = R[0] % _n
    s = (pow(k, -1, _n) * (z + r * d_int)) % _n
    if s > _n // 2:
        s = _n - s
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


# ── Key abstraction ───────────────────────────────────────────────────────────
class CasperKey:
    def __init__(self, priv):
        self._priv = priv
        if isinstance(priv, Ed25519PrivateKey):
            self._tag = 1
            self._pub = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
            self._d = None
        elif isinstance(priv, EllipticCurvePrivateKey):
            self._tag = 2
            self._pub = priv.public_key().public_bytes(
                Encoding.X962, PublicFormat.CompressedPoint
            )
            self._d = priv.private_numbers().private_value
        else:
            raise TypeError(f"Unsupported key: {type(priv).__name__}")

    @classmethod
    def load(cls, path):
        return cls(load_pem_private_key(Path(path).read_bytes(), password=None))

    @classmethod
    def generate_ed25519(cls):
        return cls(Ed25519PrivateKey.generate())

    def save(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(
            self._priv.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
        )

    def pubkey_hex(self):
        return f"{self._tag:02x}" + self._pub.hex()

    def pubkey_serial(self):
        return bytes([self._tag]) + self._pub

    def account_hash(self):
        prefix = b"ed25519\x00" if self._tag == 1 else b"secp256k1\x00"
        h = hashlib.blake2b(prefix + self._pub, digest_size=32).hexdigest()
        return f"account-hash-{h}"

    def sign(self, deploy_hash_bytes):
        if self._tag == 1:
            return self._priv.sign(deploy_hash_bytes)
        return _secp256k1_sign_raw(self._d, deploy_hash_bytes)

    def sig_hex(self, deploy_hash_bytes):
        return f"{self._tag:02x}" + self.sign(deploy_hash_bytes).hex()


# ── Deploy hash ───────────────────────────────────────────────────────────────
def _body_hash(pay, sess):
    return hashlib.blake2b(pay + sess, digest_size=32).digest()


def _header_serial(key, ts_ms, ttl_ms, gas, bh, chain):
    return (
        key.pubkey_serial()
        + u64(ts_ms)
        + u64(ttl_ms)
        + u64(gas)
        + bh
        + u32(0)
        + cs(chain)
    )


# ── RPC ───────────────────────────────────────────────────────────────────────
def _rpc(method, params, node):
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
            f"RPC {data['error'].get('code')}: {data['error'].get('message')}"
        )
    return data.get("result", data)


def _rpc_any(method, params):
    last = None
    for node in NODES:
        try:
            return _rpc(method, params, node)
        except Exception as e:
            last = e
    raise last


def wait_for_deploy(dh, timeout=300):
    print(f"   waiting {dh[:16]}…", end="", flush=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        for method, params, chk in [
            (
                "info_get_transaction",
                {"transaction_hash": {"Deploy": dh}},
                lambda r: (r.get("transaction") or {}).get("execution_info"),
            ),
            (
                "info_get_deploy",
                {"deploy_hash": dh},
                lambda r: (r.get("deploy") or {}).get("execution_results"),
            ),
        ]:
            try:
                r = _rpc_any(method, params)
                if chk(r):
                    print(" ✓")
                    return r
            except Exception:
                pass
        print(".", end="", flush=True)
        time.sleep(4)
    print(" TIMEOUT")
    raise TimeoutError(f"{dh} not finalised in {timeout}s")


# ── send_deploy ───────────────────────────────────────────────────────────────
def send_deploy(key, pay_motes, pay, sess, args):
    ts_ms = int(time.time() * 1000)
    ttl_ms, gas = 1_800_000, 1
    bh = _body_hash(pay, sess)
    header = _header_serial(key, ts_ms, ttl_ms, gas, bh, CHAIN)
    dh_raw = hashlib.blake2b(header, digest_size=32).digest()
    dh = dh_raw.hex()
    deploy = {
        "hash": dh,
        "header": {
            "account": key.pubkey_hex(),
            "timestamp": time.strftime(
                "%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts_ms // 1000)
            ),
            "ttl": "30m",
            "gas_price": gas,
            "body_hash": bh.hex(),
            "dependencies": [],
            "chain_name": CHAIN,
        },
        "payment": payment_to_json(pay_motes),
        "session": _sess_json(sess, args),
        "approvals": [{"signer": key.pubkey_hex(), "signature": key.sig_hex(dh_raw)}],
    }
    return _rpc_any("account_put_deploy", {"deploy": deploy}).get("deploy_hash", dh)


def install_wasm(key, wasm_path, args=None, motes=400_000_000_000):
    a = args or []
    wasm = Path(wasm_path).read_bytes()
    pay = payment_bytes(motes)
    sess = session_wasm_bytes(wasm, a)
    return send_deploy(key, motes, pay, sess, a)


def call_contract(key, contract_hash, entry_point, args=None, motes=5_000_000_000):
    a = args or []
    pay = payment_bytes(motes)
    sess = session_call_bytes(contract_hash, entry_point, a)
    return send_deploy(key, motes, pay, sess, a)


def arg_s(name, v):
    return (name, CLT_STRING, cs(v))


def arg_u64(name, v):
    return (name, CLT_U64, u64(v))


def arg_u32(name, v):
    return (name, CLT_U32, u32(v))


def arg_u512(name, v):
    return (name, CLT_U512, u512(v))


def arg_bool(name, v):
    return (name, CLT_BOOL, bytes([1 if v else 0]))


def parse_arg(s):
    name, rest = s.split(":", 1)
    typ, val = rest.split("=", 1)
    return {
        "string": lambda: arg_s(name, val),
        "u64": lambda: arg_u64(name, int(val)),
        "u32": lambda: arg_u32(name, int(val)),
        "u512": lambda: arg_u512(name, int(val)),
        "bool": lambda: arg_bool(name, val.lower() in ("true", "1", "yes")),
    }[typ.strip().lower()]()


def _extract_contract_hash(deploy_hash):
    time.sleep(2)
    for node in NODES:
        for method, params in [
            ("info_get_transaction", {"transaction_hash": {"Deploy": deploy_hash}}),
            ("info_get_deploy", {"deploy_hash": deploy_hash}),
        ]:
            try:
                r = _rpc(method, params, node)
                ei = (r.get("transaction") or {}).get("execution_info") or {}
                transforms = (
                    ei.get("execution_result", {})
                    .get("Success", {})
                    .get("effect", {})
                    .get("transforms", [])
                )
                if not transforms:
                    er = (r.get("deploy") or {}).get("execution_results", [])
                    if er:
                        transforms = (
                            er[0]["result"]
                            .get("Success", {})
                            .get("effect", {})
                            .get("transforms", [])
                        )
                for t in transforms:
                    k = t.get("key", "")
                    if k.startswith("hash-"):
                        if any(
                            x in str(t.get("transform", {}))
                            for x in (
                                "WriteContract",
                                "WriteContractWasm",
                                "WriteContractPackage",
                            )
                        ):
                            return k.replace("hash-", "")
            except Exception:
                continue
    print(f"\n  Open: {EXPLORER}/deploy/{deploy_hash}")
    print("  Find 'WriteContract' → copy hash (no 'hash-' prefix)")
    return input("  Paste hash: ").strip().replace("hash-", "")


def deploy_all(key):
    root = Path(__file__).parent.parent
    wasm_dir = root / "contracts" / "wasm"
    for name in ["OracleRegistry", "DataMarket", "FundVault", "Governance"]:
        p = wasm_dir / f"{name}.wasm"
        if not p.exists():
            sys.exit(f"Missing {p} — run: bash scripts/build_contracts.sh")
        if p.read_bytes().count(b"\xfc") > 0:
            sys.exit(
                f"ERROR: {name}.wasm has bulk-memory ops. Rebuild with "
                f"contracts/.cargo/config.toml disabling bulk-memory."
            )
    acct = key.account_hash()
    print(f"\nDeployer: {key.pubkey_hex()[:20]}…\n  {acct}")
    input("\nPress ENTER when account has ≥ 2000 CSPR on testnet…")

    def dw(label, name, args=None):
        print(f"\n[{label}] Deploying {name}…")
        dh = install_wasm(key, str(wasm_dir / f"{name}.wasm"), args)
        print(f"    deploy: {dh}\n    {EXPLORER}/deploy/{dh}")
        wait_for_deploy(dh)
        h = _extract_contract_hash(dh)
        print(f"    contract: {h}")
        return h

    def wire(label, desc, contract, ep, args):
        print(f"\n[{label}] {desc}…")
        dh = call_contract(key, contract, ep, args)
        wait_for_deploy(dh)
        print(f"    ✓ {EXPLORER}/deploy/{dh}")

    registry = dw("1", "OracleRegistry")
    market = dw(
        "2", "DataMarket", [arg_s("registry_hash", registry), arg_u32("fee_bps", 250)]
    )
    wire(
        "2b",
        "OracleRegistry.set_market",
        registry,
        "set_market",
        [arg_s("market", market)],
    )
    vault = dw(
        "3", "FundVault", [arg_s("operator", acct), arg_s("governance_hash", "pending")]
    )
    gov = dw(
        "4",
        "Governance",
        [
            arg_s("proposer", acct),
            arg_s("risk_agent", acct),
            arg_u64("veto_window_ms", 90_000),
        ],
    )
    wire(
        "4b",
        "FundVault.set_governance",
        vault,
        "set_governance",
        [arg_s("governance_hash", gov)],
    )

    env = root / "agents" / "testnet.env"
    env.parent.mkdir(exist_ok=True)
    env.write_text(
        "\n".join(
            [
                "ORACLE_TBILL_KEY=keys/Account 3_secret_key.pem",
                "ORACLE_GOLD_KEY=keys/Account 4_secret_key.pem",
                "ORACLE_REINDEX_KEY=keys/Account 5_secret_key.pem",
                "FUND_AGENT_KEY=keys/Account 2_secret_key.pem",
                "RISK_AGENT_KEY=keys/Account 2_secret_key.pem",
                f"REGISTRY_HASH={registry}",
                f"MARKET_HASH={market}",
                f"VAULT_HASH={vault}",
                f"GOV_HASH={gov}",
                f"DEPLOYER_ACCOUNT={acct}",
            ]
        )
        + "\n"
    )
    print(f"\n[5] agents/testnet.env written ✓")
    print("\n" + "═" * 52 + "\n  DEPLOYMENT COMPLETE\n" + "═" * 52)
    for lbl, h in [
        ("OracleRegistry", registry),
        ("DataMarket", market),
        ("FundVault", vault),
        ("Governance", gov),
    ]:
        print(f"  {lbl:<15}: {EXPLORER}/contract/{h}")


def main():
    p = argparse.ArgumentParser(description="Helios Casper deployer v4")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    kg = sub.add_parser("keygen")
    kg.add_argument("--out", required=True)
    pk = sub.add_parser("pubkey")
    pk.add_argument("--key", required=True)
    ins = sub.add_parser("install")
    ins.add_argument("--key", required=True)
    ins.add_argument("--wasm", required=True)
    ins.add_argument("--args", nargs="*", default=[])
    ins.add_argument("--payment", type=int, default=400_000_000_000)
    ins.add_argument("--wait", action="store_true")
    cl = sub.add_parser("call")
    cl.add_argument("--key", required=True)
    cl.add_argument("--contract", required=True)
    cl.add_argument("--entry-point", required=True, dest="entry_point")
    cl.add_argument("--args", nargs="*", default=[])
    cl.add_argument("--payment", type=int, default=5_000_000_000)
    cl.add_argument("--wait", action="store_true")
    wt = sub.add_parser("wait")
    wt.add_argument("hash")
    da = sub.add_parser("deploy-all")
    da.add_argument("--key", required=True)
    a = p.parse_args()

    if a.cmd == "status":
        try:
            r = _rpc_any("info_get_status", {})
            print(
                f"✓  api: {r.get('api_version', '?')}  chain: {r.get('chainspec_name', '?')}"
            )
        except Exception as e:
            sys.exit(f"✗ {e}")
    elif a.cmd == "keygen":
        key = CasperKey.generate_ed25519()
        key.save(a.out)
        print(
            f"Generated → {a.out}\n  pubkey: {key.pubkey_hex()}\n  acct: {key.account_hash()}"
        )
    elif a.cmd == "pubkey":
        key = CasperKey.load(a.key)
        t = "secp256k1" if key._tag == 2 else "ed25519"
        print(
            f"key_type:     {t}\npubkey:       {key.pubkey_hex()}\naccount_hash: {key.account_hash()}"
        )
    elif a.cmd == "install":
        key = CasperKey.load(a.key)
        args = [parse_arg(x) for x in a.args]
        dh = install_wasm(key, a.wasm, args, a.payment)
        print(f"deploy_hash: {dh}\nexplorer:    {EXPLORER}/deploy/{dh}")
        if a.wait:
            wait_for_deploy(dh)
    elif a.cmd == "call":
        key = CasperKey.load(a.key)
        args = [parse_arg(x) for x in a.args]
        dh = call_contract(key, a.contract, a.entry_point, args, a.payment)
        print(f"deploy_hash: {dh}\nexplorer:    {EXPLORER}/deploy/{dh}")
        if a.wait:
            wait_for_deploy(dh)
    elif a.cmd == "wait":
        wait_for_deploy(a.hash)
    elif a.cmd == "deploy-all":
        deploy_all(CasperKey.load(a.key))


if __name__ == "__main__":
    main()
