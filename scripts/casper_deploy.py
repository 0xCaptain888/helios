#!/usr/bin/env python3
"""
Helios Casper Testnet Deployer — pure Python, secp256k1 + ed25519.

v3 fixes:
  [1] CLType tags corrected (casper-types/src/cl_type.rs):
        U32=0x04  U64=0x05  U512=0x08  String=0x0a  Bool=0x00
  [2] body_hash: blake2b(payment.to_bytes() || session.to_bytes())
  [3] header_hash: PublicKey as tag(1B)+raw_bytes, NOT account_hash
  [4] body_hash stored as raw 32 bytes in header (no length prefix)
  [5] secp256k1 signature: raw 64-byte r||s (not DER)
  [6] Signature prefix: 01=ed25519, 02=secp256k1
  [7] JSON args "bytes": raw value hex (no CLType tag, no len prefix)
  [8] Casper 2.x: try info_get_transaction first, fallback info_get_deploy
  [9] RuntimeArgs: no outer len_prefix (matches casper-client serialization)
  [10] Timestamp: preserve milliseconds in ISO format

Usage:
  python3 scripts/casper_deploy.py status
  python3 scripts/casper_deploy.py pubkey  --key keys/account2_secret_key.pem
  python3 scripts/casper_deploy.py install --key keys/account2_secret_key.pem \\
      --wasm contracts/wasm/OracleRegistry.wasm --wait
  python3 scripts/casper_deploy.py call    --key keys/account2_secret_key.pem \\
      --contract <HASH> --entry-point register \\
      --args "name:string=TBill Oracle" "price_motes:u64=2000000000"
  python3 scripts/casper_deploy.py deploy-all --key keys/account2_secret_key.pem
  python3 scripts/casper_deploy.py wait <deploy_hash>
"""

from __future__ import annotations
import argparse, hashlib, json, struct, sys, time, urllib.error, urllib.request
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
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey, ECDSA
from cryptography.hazmat.primitives.hashes import SHA256

# ── Constants ─────────────────────────────────────────────────────────────────
NODES = ["https://node.testnet.casper.network", "https://rpc.testnet.cspr.cloud"]
CHAIN = "casper-test"
EXPLORER = "https://testnet.cspr.live"


# ── Binary helpers ────────────────────────────────────────────────────────────
def u32(n):
    return struct.pack("<I", n)


def u64(n):
    return struct.pack("<Q", n)


def cs(s):
    """Length-prefixed UTF-8 string (u32 len + bytes)."""
    b = s.encode("utf-8")
    return u32(len(b)) + b


def u512(n):
    """Casper U512 encoding: 1-byte size prefix + little-endian value."""
    if n == 0:
        return b"\x00"
    nb = (n.bit_length() + 7) // 8
    return bytes([nb]) + n.to_bytes(nb, "little")


# ── CLType tags (from casper-types/src/cl_type.rs) ───────────────────────────
CLT_BOOL = b"\x00"
CLT_U32 = b"\x04"
CLT_U64 = b"\x05"
CLT_U512 = b"\x08"
CLT_STRING = b"\x0a"

_CL_TYPE_NAME = {
    CLT_BOOL: "Bool",
    CLT_U32: "U32",
    CLT_U64: "U64",
    CLT_U512: "U512",
    CLT_STRING: "String",
}


# ── CLValue / RuntimeArgs serialization ──────────────────────────────────────
def cl_value_bytes(type_tag, val_bytes):
    """CLValue = u32(len(value)) + value_bytes + type_tag."""
    return u32(len(val_bytes)) + val_bytes + type_tag


def named_arg_bytes(name, type_tag, val_bytes):
    """NamedArg = cs(name) + CLValue."""
    return cs(name) + cl_value_bytes(type_tag, val_bytes)


def runtime_args_bytes(args):
    """RuntimeArgs = u32(count) + concat(named_args). No outer len_prefix."""
    encoded = b"".join(named_arg_bytes(n, t, v) for n, t, v in args)
    return u32(len(args)) + encoded


# ── Deploy body serialization ─────────────────────────────────────────────────
def payment_bytes(motes):
    """Standard payment: ModuleBytes tag(0x00) + empty wasm + args."""
    return b"\x00" + u32(0) + runtime_args_bytes([("amount", CLT_U512, u512(motes))])


def session_wasm_bytes(wasm, args):
    """Session::ModuleBytes: tag(0x00) + u32(len) + wasm + RuntimeArgs."""
    return b"\x00" + u32(len(wasm)) + wasm + runtime_args_bytes(args)


def session_call_bytes(contract_hash_hex, entry_point, args):
    """Session::StoredContractByHash: tag(0x01) + hash + cs(entry_point) + args."""
    return (
        b"\x01"
        + bytes.fromhex(contract_hash_hex)
        + cs(entry_point)
        + runtime_args_bytes(args)
    )


# ── JSON helpers (for the deploy JSON sent to the node) ───────────────────────
def arg_to_json(name, type_tag, val_bytes):
    """Convert a named arg to the JSON format expected by the RPC."""
    cl_name = _CL_TYPE_NAME[type_tag]
    if type_tag == CLT_STRING:
        parsed = val_bytes[4:].decode("utf-8")
    elif type_tag == CLT_U64:
        parsed = str(struct.unpack("<Q", val_bytes)[0])
    elif type_tag == CLT_U32:
        parsed = str(struct.unpack("<I", val_bytes)[0])
    elif type_tag == CLT_U512:
        n_b = val_bytes[0]
        parsed = str(int.from_bytes(val_bytes[1 : 1 + n_b], "little") if n_b else 0)
    elif type_tag == CLT_BOOL:
        parsed = val_bytes[0] != 0
    else:
        parsed = None
    return [name, {"cl_type": cl_name, "bytes": val_bytes.hex(), "parsed": parsed}]


def payment_to_json(motes):
    return {
        "ModuleBytes": {
            "module_bytes": "",
            "args": [arg_to_json("amount", CLT_U512, u512(motes))],
        }
    }


def session_wasm_to_json(wasm_hex, args):
    return {
        "ModuleBytes": {
            "module_bytes": wasm_hex,
            "args": [arg_to_json(n, t, v) for n, t, v in args],
        }
    }


def session_call_to_json(contract_hash_hex, entry_point, args):
    return {
        "StoredContractByHash": {
            "hash": contract_hash_hex,
            "entry_point": entry_point,
            "args": [arg_to_json(n, t, v) for n, t, v in args],
        }
    }


# ── Key handling ──────────────────────────────────────────────────────────────
class CasperKey:
    def __init__(self, priv):
        self._priv = priv
        if isinstance(priv, Ed25519PrivateKey):
            self._tag = 1
            self._pub = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        elif isinstance(priv, EllipticCurvePrivateKey):
            self._tag = 2
            self._pub = priv.public_key().public_bytes(
                Encoding.X962, PublicFormat.CompressedPoint
            )
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
        """Tag byte + raw public key bytes (for header hash)."""
        return bytes([self._tag]) + self._pub

    def account_hash(self):
        """blake2b(tag_prefix + pubkey) — NOT sha256."""
        prefix = b"ed25519\x00" if self._tag == 1 else b"secp256k1\x00"
        h = hashlib.blake2b(prefix + self._pub, digest_size=32).hexdigest()
        return f"account-hash-{h}"

    def sign(self, message):
        """Sign and return raw bytes. secp256k1: DER → raw r||s (64 bytes)."""
        if self._tag == 1:
            return self._priv.sign(message)
        der = self._priv.sign(message, ECDSA(SHA256()))
        return _der_to_raw64(der)

    def sig_hex(self, message):
        """Full signature hex: tag(2 hex chars) + raw_sig."""
        return f"{self._tag:02x}" + self.sign(message).hex()


def _der_to_raw64(der):
    """Convert DER-encoded ECDSA signature to raw 64-byte r||s."""
    assert der[0] == 0x30
    idx = 2
    assert der[idx] == 0x02
    idx += 1
    rlen = der[idx]
    idx += 1
    r = der[idx : idx + rlen]
    idx += rlen
    assert der[idx] == 0x02
    idx += 1
    slen = der[idx]
    idx += 1
    s = der[idx : idx + slen]
    return int.from_bytes(r, "big").to_bytes(32, "big") + int.from_bytes(
        s, "big"
    ).to_bytes(32, "big")


# ── Hash computation ──────────────────────────────────────────────────────────
def _body_hash(pay, sess):
    """blake2b(payment_bytes || session_bytes), 32 bytes, no length prefix."""
    return hashlib.blake2b(pay + sess, digest_size=32).digest()


def _header_serial(key, ts_ms, ttl_ms, gas, bh, chain):
    """DeployHeader binary serialization.
    Layout: pubkey_serial + u64(ts) + u64(ttl) + u64(gas) + bh(32B) + u32(0) + cs(chain)
    """
    return (
        key.pubkey_serial()
        + u64(ts_ms)
        + u64(ttl_ms)
        + u64(gas)
        + bh
        + u32(0)
        + cs(chain)
    )


def deploy_hash_from_header(header):
    return hashlib.blake2b(header, digest_size=32).digest()


def _ms_to_iso(ms):
    """Convert milliseconds to ISO-8601 with millisecond precision."""
    secs = ms // 1000
    millis = ms % 1000
    return time.strftime(f"%Y-%m-%dT%H:%M:%S.{millis:03d}Z", time.gmtime(secs))


# ── RPC helpers ───────────────────────────────────────────────────────────────
def _rpc(method, params, node):
    body = json.dumps(
        {"id": 1, "jsonrpc": "2.0", "method": method, "params": params}
    ).encode()
    url = node if node.endswith("/rpc") else f"{node}/rpc"
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
    except urllib.error.URLError as e:
        raise RuntimeError(f"RPC ({node}): {e}") from e
    if "error" in data:
        raise RuntimeError(
            f"RPC error {data['error'].get('code')}: "
            f"{data['error'].get('message')} — {data['error'].get('data', '')}"
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
    """Wait for deploy to be included in a block."""
    print(f"   waiting for {dh[:16]}…", end="", flush=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        for method, params, path in [
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
                if path(r):
                    print(" ✓")
                    return r
            except Exception:
                pass
        print(".", end="", flush=True)
        time.sleep(4)
    print(" TIMEOUT")
    raise TimeoutError(f"{dh} not finalised in {timeout}s")


# ── Deploy construction & sending ─────────────────────────────────────────────
def _sess_json(sess, args):
    """Convert binary session to JSON format for the deploy."""
    tag = sess[0]
    if tag == 0:
        wasm_len = struct.unpack("<I", sess[1:5])[0]
        return session_wasm_to_json(sess[5 : 5 + wasm_len].hex(), args)
    elif tag == 1:
        h = sess[1:33].hex()
        ep_len = struct.unpack("<I", sess[33:37])[0]
        ep = sess[37 : 37 + ep_len].decode()
        return session_call_to_json(h, ep, args)
    return {"ModuleBytes": {"module_bytes": sess.hex(), "args": []}}


def send_deploy(key, pay_motes, pay, sess, args):
    """Build and send a deploy. Returns deploy hash."""
    ts_ms = int(time.time() * 1000)
    ttl_ms, gas = 1_800_000, 1
    bh = _body_hash(pay, sess)
    header = _header_serial(key, ts_ms, ttl_ms, gas, bh, CHAIN)
    dh_raw = deploy_hash_from_header(header)
    dh = dh_raw.hex()
    deploy = {
        "hash": dh,
        "header": {
            "account": key.pubkey_hex(),
            "timestamp": _ms_to_iso(ts_ms),
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
    result = _rpc_any("account_put_deploy", {"deploy": deploy})
    return result.get("deploy_hash", dh)


def install_wasm(key, wasm_path, args=None, motes=400_000_000_000):
    """Deploy a WASM contract. Returns deploy hash."""
    a = args or []
    wasm = Path(wasm_path).read_bytes()
    pay = payment_bytes(motes)
    sess = session_wasm_bytes(wasm, a)
    return send_deploy(key, motes, pay, sess, a)


def call_contract(key, contract_hash, entry_point, args=None, motes=5_000_000_000):
    """Call a contract entry point. Returns deploy hash."""
    a = args or []
    pay = payment_bytes(motes)
    sess = session_call_bytes(contract_hash, entry_point, a)
    return send_deploy(key, motes, pay, sess, a)


# ── Typed arg constructors ────────────────────────────────────────────────────
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
    """Parse CLI arg format: name:type=value."""
    name, rest = s.split(":", 1)
    typ, val = rest.split("=", 1)
    return {
        "string": lambda: arg_s(name, val),
        "u64": lambda: arg_u64(name, int(val)),
        "u32": lambda: arg_u32(name, int(val)),
        "u512": lambda: arg_u512(name, int(val)),
        "bool": lambda: arg_bool(name, val.lower() in ("true", "1", "yes")),
    }[typ.strip().lower()]()


# ── Contract hash extraction ──────────────────────────────────────────────────
def _extract_contract_hash(deploy_hash):
    """Extract contract hash from deploy execution effects."""
    time.sleep(2)
    for node in NODES:
        for method, params in [
            ("info_get_transaction", {"transaction_hash": {"Deploy": deploy_hash}}),
            ("info_get_deploy", {"deploy_hash": deploy_hash}),
        ]:
            try:
                r = _rpc(method, params, node)
                # Casper 2.x: transaction.execution_info
                ei = (r.get("transaction") or {}).get("execution_info") or {}
                transforms = (
                    ei.get("execution_result", {})
                    .get("Success", {})
                    .get("effect", {})
                    .get("transforms", [])
                )
                if not transforms:
                    # Casper 1.x fallback: deploy.execution_results
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
                        v = t.get("transform", {})
                        if any(
                            x in v
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
    print("  Find 'WriteContract' → copy hash (without 'hash-' prefix)")
    return input("  Paste hash: ").strip().lstrip("hash-")


# ── deploy-all: one-click deployment ──────────────────────────────────────────
def deploy_all(key):
    root = Path(__file__).parent.parent
    wasm_dir = root / "contracts" / "wasm"
    for name in ["OracleRegistry", "DataMarket", "FundVault", "Governance"]:
        p = wasm_dir / f"{name}.wasm"
        if not p.exists():
            sys.exit(f"Missing {p} — run: bash scripts/build_contracts.sh")

    acct = key.account_hash()
    print(f"\nDeployer: {key.pubkey_hex()}")
    print(f"  {acct}")
    input("\nPress ENTER when account has ≥ 2000 CSPR on testnet…")

    def deploy_wasm(label, name, args=None):
        print(f"\n[{label}] Deploying {name}…")
        dh = install_wasm(key, str(wasm_dir / f"{name}.wasm"), args)
        print(f"    deploy: {dh}\n    {EXPLORER}/deploy/{dh}")
        wait_for_deploy(dh)
        h = _extract_contract_hash(dh)
        print(f"    contract hash: {h}")
        return h

    def wire(label, desc, contract, ep, args):
        print(f"\n[{label}] {desc}…")
        dh = call_contract(key, contract, ep, args)
        wait_for_deploy(dh)
        print(f"    ✓  {EXPLORER}/deploy/{dh}")

    registry = deploy_wasm("1", "OracleRegistry")
    market = deploy_wasm(
        "2", "DataMarket", [arg_s("registry_hash", registry), arg_u32("fee_bps", 250)]
    )
    wire(
        "2b",
        "OracleRegistry.set_market",
        registry,
        "set_market",
        [arg_s("market", market)],
    )
    vault = deploy_wasm(
        "3", "FundVault", [arg_s("operator", acct), arg_s("governance_hash", "pending")]
    )
    gov = deploy_wasm(
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
                f"ORACLE_TBILL_KEY=keys/account3_secret_key.pem",
                f"ORACLE_GOLD_KEY=keys/account4_secret_key.pem",
                f"ORACLE_REINDEX_KEY=keys/account5_secret_key.pem",
                f"FUND_AGENT_KEY=keys/account2_secret_key.pem",
                f"RISK_AGENT_KEY=keys/account2_secret_key.pem",
                f"REGISTRY_HASH={registry}",
                f"MARKET_HASH={market}",
                f"VAULT_HASH={vault}",
                f"GOV_HASH={gov}",
                f"DEPLOYER_ACCOUNT={acct}",
            ]
        )
        + "\n"
    )
    print(f"\n[5] {env} written ✓")
    print("\n" + "═" * 52)
    print("  DEPLOYMENT COMPLETE")
    print("═" * 52)
    for label, h in [
        ("OracleRegistry", registry),
        ("DataMarket", market),
        ("FundVault", vault),
        ("Governance", gov),
    ]:
        print(f"  {label:<15}: {EXPLORER}/contract/{h}")


# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(description="Helios Casper deployer")
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
            print(f"✓ Node reachable")
            print(f"  api_version: {r.get('api_version', '?')}")
            print(f"  chain: {r.get('chainspec_name', '?')}")
        except Exception as e:
            sys.exit(f"✗ {e}")
    elif a.cmd == "keygen":
        key = CasperKey.generate_ed25519()
        key.save(a.out)
        print(f"Generated → {a.out}")
        print(f"  pubkey: {key.pubkey_hex()}")
        print(f"  account_hash: {key.account_hash()}")
    elif a.cmd == "pubkey":
        key = CasperKey.load(a.key)
        t = "secp256k1" if key._tag == 2 else "ed25519"
        print(f"key_type:     {t}")
        print(f"pubkey:       {key.pubkey_hex()}")
        print(f"account_hash: {key.account_hash()}")
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
