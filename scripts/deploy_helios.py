#!/usr/bin/env python3
"""
Helios full deployment pipeline — pure Python, no casper-client needed.

Steps:
  1. Generate keys (if not already done)
  2. Print faucet instructions and wait for confirmation
  3. Deploy OracleRegistry
  4. Deploy DataMarket (with registry hash)
  5. Wire them: call set_market on OracleRegistry
  6. Deploy FundVault
  7. Deploy Governance
  8. Wire FundVault → Governance
  9. Write agents/testnet.env
  10. Produce first on-chain activity (register + attest + anchor)
  11. Print explorer links for all transactions

Usage:
  python3 scripts/deploy_helios.py
  python3 scripts/deploy_helios.py --keys-dir keys --node https://rpc.testnet.cspr.cloud
  python3 scripts/deploy_helios.py --skip-keygen   # if keys already exist
"""

from __future__ import annotations
import argparse, json, os, sys, time
from pathlib import Path

# Add project root so we can import casper_deploy
sys.path.insert(0, str(Path(__file__).parent))
from casper_deploy import (
    CasperKey,
    generate_key,
    install_wasm,
    call_entry_point,
    wait_for_deploy,
    arg_string,
    arg_u64,
    arg_u32,
    arg_u512,
    _rpc,
    NODES,
    EXPLORER,
)

ROOT = Path(__file__).parent.parent
WASM = ROOT / "contracts" / "wasm"
AGENTS = ROOT / "agents"

# ── helpers ───────────────────────────────────────────────────────────────────


def wait_and_get_contract_hash(deploy_hash: str, contract_name: str, node: str) -> str:
    """Wait for deploy, then extract contract hash from execution result."""
    wait_for_deploy(deploy_hash, node=node)
    time.sleep(2)  # allow state to propagate
    try:
        result = _rpc(
            "info_get_transaction", {"transaction_hash": {"Deploy": deploy_hash}}, node
        )
        txn = result.get("transaction", result.get("deploy", {}))
        # execution_info path (Casper 2.x)
        exec_info = txn.get("execution_info", {})
        if exec_info:
            effects = exec_info.get("execution_result", {}).get("effects", {})
            for transform in effects.get("transforms", []):
                if transform.get("kind", {}).get("WriteContract") is not None:
                    return transform["key"].replace("hash-", "")
        # fallback: user reads from cspr.live
        print(f"  ⚠ Could not auto-extract {contract_name} hash.")
        print(f"    Open {EXPLORER}/deploy/{deploy_hash}")
        print(
            f"    Find the 'WriteContract' entry → copy the hash (without 'hash-' prefix)"
        )
        return input(f"  Paste {contract_name} hash: ").strip().replace("hash-", "")
    except Exception as e:
        print(f"  ⚠ Hash extraction error ({e}), please paste manually:")
        print(f"    Explorer: {EXPLORER}/deploy/{deploy_hash}")
        return input(f"  Paste {contract_name} hash: ").strip().replace("hash-", "")


# ── main deployment flow ──────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Deploy Helios to Casper Testnet")
    parser.add_argument("--keys-dir", default="keys")
    parser.add_argument("--node", default=NODES[0])
    parser.add_argument("--skip-keygen", action="store_true")
    parser.add_argument(
        "--skip-activity",
        action="store_true",
        help="Skip the post-deploy on-chain activity step",
    )
    parser.add_argument(
        "--resume",
        type=str,
        default=None,
        metavar="testnet.env",
        help="Resume from an existing testnet.env (skip deploys)",
    )
    args = parser.parse_args()

    keys_dir = ROOT / args.keys_dir
    node = args.node

    print("═" * 60)
    print("  Helios Testnet Deployment  (pure Python)")
    print("═" * 60)

    # ── Step 0: check node ────────────────────────────────────────────────────
    print("\n[0] Checking node connectivity…")
    try:
        status = _rpc("info_get_status", {}, node)
        print(f"    ✓ {node} — chain: {status.get('chainspec_name', '?')}")
    except Exception as e:
        print(f"    ✗ Cannot reach {node}: {e}")
        for alt in NODES[1:]:
            try:
                _rpc("info_get_status", {}, alt)
                print(f"    ✓ Falling back to {alt}")
                node = alt
                break
            except Exception:
                pass
        else:
            print("    ✗ All nodes unreachable. Check your connection.")
            sys.exit(1)

    # ── Step 1: keygen ────────────────────────────────────────────────────────
    roles = [
        "oracle_tbill",
        "oracle_gold",
        "oracle_reindex",
        "fund_agent",
        "risk_agent",
    ]
    if args.resume:
        print(f"\n[1] Resuming — loading keys from {keys_dir}")
    elif not args.skip_keygen:
        print(f"\n[1] Generating keypairs in {keys_dir}/")
        keys_dir.mkdir(parents=True, exist_ok=True)
        for role in roles:
            rdir = keys_dir / role
            key_path = rdir / "secret_key.pem"
            if key_path.exists():
                print(f"    {role}: already exists — skipping")
                continue
            rdir.mkdir(parents=True, exist_ok=True)
            k = generate_key(str(key_path))
            print(f"    {role}: {k.account_hash()}")
    else:
        print(f"\n[1] Skipping keygen (--skip-keygen)")

    keys = {r: CasperKey(str(keys_dir / r / "secret_key.pem")) for r in roles}
    deployer_key = keys["fund_agent"]

    # ── Faucet prompt ─────────────────────────────────────────────────────────
    if not args.resume:
        print("\n[2] Fund accounts via faucet BEFORE deploying")
        print("    URL: https://testnet.cspr.live/tools/faucet")
        print("    Each account needs ≥ 1000 test CSPR. Accounts:\n")
        for role, key in keys.items():
            print(f"    {role:20s}  {key.pubkey_hex()}")
        print()
        input("    Press ENTER when all accounts are funded… ")

    # ── Wasm check ────────────────────────────────────────────────────────────
    if not args.resume:
        needed = [
            "OracleRegistry.wasm",
            "DataMarket.wasm",
            "FundVault.wasm",
            "Governance.wasm",
        ]
        missing_wasm = [w for w in needed if not (WASM / w).exists()]
        if missing_wasm:
            print(f"\n✗ Missing wasm files: {missing_wasm}")
            print(f"  Run:  bash scripts/build_contracts.sh")
            sys.exit(1)
        print("\n[3] WASM files found ✓")
        # Verify exports
        sys.path.insert(0, str(ROOT / "scripts"))
        import check_wasm_exports

        bad = 0
        for w in needed:
            errors, func_names, has_mem = check_wasm_exports.check_file(str(WASM / w))
            if errors:
                print(f"  ✗ {w}: {errors[0]}")
                bad += 1
            else:
                print(f"  ✓ {w}")
        if bad:
            print("  Fix build first: bash scripts/build_contracts.sh")
            sys.exit(1)

    # ── Deploy or resume ──────────────────────────────────────────────────────
    if args.resume:
        env = {}
        for line in Path(args.resume).read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
        registry_hash = env.get("REGISTRY_HASH", "")
        market_hash = env.get("MARKET_HASH", "")
        vault_hash = env.get("VAULT_HASH", "")
        gov_hash = env.get("GOV_HASH", "")
        print(f"\n[3-7] Resuming from existing env:")
        print(f"    REGISTRY: {registry_hash}")
        print(f"    MARKET:   {market_hash}")
        print(f"    VAULT:    {vault_hash}")
        print(f"    GOV:      {gov_hash}")
    else:
        txns = {}

        print("\n[4] Deploying OracleRegistry…")
        h = install_wasm(deployer_key, str(WASM / "OracleRegistry.wasm"), node=node)
        print(f"    deploy: {h}\n    explorer: {EXPLORER}/deploy/{h}")
        txns["registry_deploy"] = h
        registry_hash = wait_and_get_contract_hash(h, "OracleRegistry", node)
        print(f"    OracleRegistry hash: {registry_hash}")

        print("\n[5] Deploying DataMarket…")
        h = install_wasm(
            deployer_key,
            str(WASM / "DataMarket.wasm"),
            named_args=[
                arg_string("registry_hash", registry_hash),
                arg_u32("fee_bps", 250),
            ],
            node=node,
        )
        print(f"    deploy: {h}\n    explorer: {EXPLORER}/deploy/{h}")
        txns["market_deploy"] = h
        market_hash = wait_and_get_contract_hash(h, "DataMarket", node)
        print(f"    DataMarket hash: {market_hash}")

        print("\n[5b] Wiring: OracleRegistry.set_market…")
        h = call_entry_point(
            deployer_key,
            registry_hash,
            "set_market",
            [arg_string("market", market_hash)],
            node=node,
        )
        wait_for_deploy(h, node=node)
        txns["set_market"] = h
        print(f"    wired ✓  deploy: {h}")

        print("\n[6] Deploying FundVault…")
        fund_acct = deployer_key.account_hash()
        h = install_wasm(
            deployer_key,
            str(WASM / "FundVault.wasm"),
            named_args=[
                arg_string("operator", fund_acct),
                arg_string("governance_hash", "pending"),  # updated after step 7
            ],
            node=node,
        )
        print(f"    deploy: {h}\n    explorer: {EXPLORER}/deploy/{h}")
        txns["vault_deploy"] = h
        vault_hash = wait_and_get_contract_hash(h, "FundVault", node)
        print(f"    FundVault hash: {vault_hash}")

        print("\n[7] Deploying Governance…")
        risk_acct = keys["risk_agent"].account_hash()
        h = install_wasm(
            deployer_key,
            str(WASM / "Governance.wasm"),
            named_args=[
                arg_string("proposer", fund_acct),
                arg_string("risk_agent", risk_acct),
                arg_u64("veto_window_ms", 90_000),
            ],
            node=node,
        )
        print(f"    deploy: {h}\n    explorer: {EXPLORER}/deploy/{h}")
        txns["gov_deploy"] = h
        gov_hash = wait_and_get_contract_hash(h, "Governance", node)
        print(f"    Governance hash: {gov_hash}")

        print("\n[7b] Wiring: FundVault.set_governance…")
        h = call_entry_point(
            deployer_key,
            vault_hash,
            "set_governance",
            [arg_string("governance_hash", gov_hash)],
            node=node,
        )
        wait_for_deploy(h, node=node)
        txns["set_governance"] = h
        print(f"    wired ✓  deploy: {h}")

        # save all deploy hashes for reference
        Path("deploy_hashes.json").write_text(json.dumps(txns, indent=2))
        print(f"\n    All deploy hashes saved to deploy_hashes.json")

    # ── Write testnet.env ──────────────────────────────────────────────────────
    env_path = AGENTS / "testnet.env"
    env_content = f"""# Helios Testnet Config — generated by deploy_helios.py
ORACLE_TBILL_KEY={keys_dir}/oracle_tbill/secret_key.pem
ORACLE_GOLD_KEY={keys_dir}/oracle_gold/secret_key.pem
ORACLE_REINDEX_KEY={keys_dir}/oracle_reindex/secret_key.pem
FUND_AGENT_KEY={keys_dir}/fund_agent/secret_key.pem
RISK_AGENT_KEY={keys_dir}/risk_agent/secret_key.pem
REGISTRY_HASH={registry_hash}
MARKET_HASH={market_hash}
VAULT_HASH={vault_hash}
GOV_HASH={gov_hash}
"""
    env_path.write_text(env_content)
    print(f"\n[8] agents/testnet.env written ✓")

    # ── On-chain activity ──────────────────────────────────────────────────────
    if not args.skip_activity:
        print("\n[9] Producing on-chain activity (register + attest + anchor)…")
        activity_txns = []

        for role, feed_key, title, price in [
            ("oracle_tbill", "us_tbill_3m", "US T-Bill 3M Yield", 2_000_000_000),
            ("oracle_gold", "gold_spot_usd", "Gold Spot Price (USD)", 3_000_000_000),
            ("oracle_reindex", "cn_reindex_chz", "Zhongshan RE Index", 5_000_000_000),
        ]:
            k = keys[role]
            acct = k.account_hash()
            print(f"\n  Registering {role}…")
            h = call_entry_point(
                k,
                registry_hash,
                "register",
                [
                    arg_string("name", title),
                    arg_string("category", "rwa"),
                    arg_string("endpoint", f"https://helios.example/{feed_key}"),
                    arg_u64("price_motes", price),
                ],
                node=node,
            )
            wait_for_deploy(h, node=node)
            activity_txns.append({"action": f"register:{role}", "hash": h})
            print(f"    register: {EXPLORER}/deploy/{h}")

            print(f"  Posting attestation for {feed_key}…")
            h = call_entry_point(
                k,
                registry_hash,
                "post_attestation",
                [
                    arg_string("feed_key", feed_key),
                    arg_string("value", "42.0"),
                ],
                node=node,
            )
            wait_for_deploy(h, node=node)
            activity_txns.append({"action": f"attest:{feed_key}", "hash": h})
            print(f"    attest: {EXPLORER}/deploy/{h}")

        Path("activity_hashes.json").write_text(json.dumps(activity_txns, indent=2))
        print(f"\n  Activity hashes saved to activity_hashes.json")

    # ── Summary ────────────────────────────────────────────────────────────────
    print("\n" + "═" * 60)
    print("  DEPLOYMENT COMPLETE")
    print("═" * 60)
    print(f"  OracleRegistry : {EXPLORER}/contract/{registry_hash}")
    print(f"  DataMarket     : {EXPLORER}/contract/{market_hash}")
    print(f"  FundVault      : {EXPLORER}/contract/{vault_hash}")
    print(f"  Governance     : {EXPLORER}/contract/{gov_hash}")
    print()
    print("  Next: run the testnet agent loop")
    print("    export HELIOS_MODE=testnet")
    print("    python3 scripts/testnet_round.py --rounds 3")
    print()


if __name__ == "__main__":
    main()
