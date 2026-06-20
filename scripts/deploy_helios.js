#!/usr/bin/env node
/**
 * Helios Testnet Deployer — Casper official JS SDK
 *
 * Prerequisites:
 *   node >= 18
 *   npm install casper-js-sdk
 *
 * Usage:
 *   node scripts/deploy_helios.js keygen
 *   node scripts/deploy_helios.js status
 *   node scripts/deploy_helios.js install \
 *       --key "keys/fund_agent/secret_key.pem" \
 *       --wasm contracts/wasm/OracleRegistry.wasm
 *   node scripts/deploy_helios.js call \
 *       --key "keys/oracle_tbill/secret_key.pem" \
 *       --contract <HASH> \
 *       --entry-point register \
 *       --args "name:string=TBill Oracle" "category:string=rwa" \
 *               "endpoint:string=http://localhost:8451/quote" "price_motes:u64=2000000000"
 *   node scripts/deploy_helios.js deploy-all
 */

const fs   = require("fs");
const path = require("path");

// ── Lazy-load SDK so `node deploy_helios.js` gives a clean error if not installed
let SDK;
function sdk() {
  if (!SDK) {
    try {
      SDK = require("casper-js-sdk");
    } catch {
      console.error("casper-js-sdk not installed. Run:  npm install casper-js-sdk");
      process.exit(1);
    }
  }
  return SDK;
}

const ROOT     = path.resolve(__dirname, "..");
const WASM_DIR = path.join(ROOT, "contracts", "wasm");
const KEYS_DIR = path.join(ROOT, "keys");
const AGENTS   = path.join(ROOT, "agents");

const NODE_URL  = process.env.CASPER_NODE || "https://rpc.testnet.cspr.cloud/rpc";
const CHAIN     = "casper-test";
const EXPLORER  = "https://testnet.cspr.live";
const GAS_INSTALL = "400000000000";   // 400 CSPR for wasm install
const GAS_CALL    = "5000000000";     // 5 CSPR for entry-point call

// ── Key helpers ────────────────────────────────────────────────────────────────

function loadKey(keyPath) {
  const { Keys } = sdk();
  return Keys.Ed25519.loadKeyPairFromPrivateFile(keyPath);
}

function keygen(outDir) {
  const { Keys } = sdk();
  fs.mkdirSync(outDir, { recursive: true });
  const kp = Keys.Ed25519.new();
  const secretPath = path.join(outDir, "secret_key.pem");
  const pubPath    = path.join(outDir, "public_key.pem");
  const hexPath    = path.join(outDir, "public_key_hex");
  fs.writeFileSync(secretPath, kp.exportPrivateKeyInPem());
  fs.writeFileSync(pubPath,    kp.exportPublicKeyInPem());
  fs.writeFileSync(hexPath,    kp.publicKey.toHex());
  return kp;
}

// ── RPC helpers ────────────────────────────────────────────────────────────────

async function rpc(method, params) {
  const { CasperServiceByJsonRPC } = sdk();
  const client = new CasperServiceByJsonRPC(NODE_URL);
  return client[method] ? client[method](...Object.values(params))
                        : Promise.reject(new Error(`Unknown method: ${method}`));
}

async function getClient() {
  const { CasperServiceByJsonRPC } = sdk();
  return new CasperServiceByJsonRPC(NODE_URL);
}

async function waitForDeploy(deployHash, timeoutMs = 300_000) {
  const client = await getClient();
  process.stdout.write(`   waiting for ${deployHash.slice(0,16)}…`);
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const result = await client.getDeployInfo(deployHash);
      if (result?.execution_results?.length > 0) {
        const er = result.execution_results[0].result;
        if (er.Failure) {
          console.log(" FAILED");
          throw new Error(`Deploy failed: ${JSON.stringify(er.Failure.error_message)}`);
        }
        console.log(" ✓");
        return result;
      }
    } catch (e) {
      if (e.message?.includes("Deploy failed")) throw e;
    }
    process.stdout.write(".");
    await new Promise(r => setTimeout(r, 4000));
  }
  console.log(" TIMEOUT");
  throw new Error(`Deploy ${deployHash} not finalised in ${timeoutMs / 1000}s`);
}

// ── Arg builders ───────────────────────────────────────────────────────────────

function parseArgs(rawArgs) {
  const { RuntimeArgs, CLValueBuilder, CLTypeTag } = sdk();
  const rArgs = RuntimeArgs.fromMap({});
  for (const a of (rawArgs || [])) {
    const colonIdx = a.indexOf(":");
    const eqIdx    = a.indexOf("=", colonIdx);
    const name = a.slice(0, colonIdx);
    const typ  = a.slice(colonIdx + 1, eqIdx);
    const val  = a.slice(eqIdx + 1);
    switch (typ) {
      case "string": rArgs.insert(name, CLValueBuilder.string(val));          break;
      case "u64":    rArgs.insert(name, CLValueBuilder.u64(BigInt(val)));      break;
      case "u32":    rArgs.insert(name, CLValueBuilder.u32(Number(val)));      break;
      case "u512":   rArgs.insert(name, CLValueBuilder.u512(BigInt(val)));     break;
      case "bool":   rArgs.insert(name, CLValueBuilder.bool(
                       val === "true" || val === "1" || val === "yes"));       break;
      default: throw new Error(`Unknown arg type '${typ}' in '${a}'`);
    }
  }
  return rArgs;
}

// ── Deploy functions ───────────────────────────────────────────────────────────

async function installWasm(keyPair, wasmPath, rawArgs, paymentMotes) {
  const { DeployUtil, RuntimeArgs } = sdk();
  const client = await getClient();
  const wasm   = new Uint8Array(fs.readFileSync(wasmPath));
  const args   = parseArgs(rawArgs);

  const deploy = DeployUtil.makeDeploy(
    new DeployUtil.DeployHeader(
      keyPair.publicKey,
      Date.now(),
      1_800_000,      // TTL 30 min
      1,              // gas price
      [],
      CHAIN,
    ),
    DeployUtil.ExecutableDeployItem.newModuleBytes(wasm, args),
    DeployUtil.standardPayment(paymentMotes || GAS_INSTALL),
  );
  const signed = DeployUtil.signDeploy(deploy, keyPair);
  const result = await client.deploy(signed);
  return result.deploy_hash;
}

async function callContract(keyPair, contractHash, entryPoint, rawArgs, paymentMotes) {
  const { DeployUtil, CLValueBuilder, contracts } = sdk();
  const client = await getClient();
  const args   = parseArgs(rawArgs);

  const deploy = DeployUtil.makeDeploy(
    new DeployUtil.DeployHeader(
      keyPair.publicKey,
      Date.now(),
      1_800_000,
      1,
      [],
      CHAIN,
    ),
    DeployUtil.ExecutableDeployItem.newStoredContractByHash(
      contractHash, entryPoint, args),
    DeployUtil.standardPayment(paymentMotes || GAS_CALL),
  );
  const signed = DeployUtil.signDeploy(deploy, keyPair);
  const result = await client.deploy(signed);
  return result.deploy_hash;
}

// ── Contract hash extractor ────────────────────────────────────────────────────

async function extractContractHash(deployHash) {
  const client = await getClient();
  await new Promise(r => setTimeout(r, 2000));
  try {
    const info = await client.getDeployInfo(deployHash);
    const transforms = info?.execution_results?.[0]
      ?.result?.Success?.effect?.transforms || [];
    for (const t of transforms) {
      if (t.transform?.WriteContract || t.transform?.WriteContractWasm) {
        return t.key.replace("hash-", "");
      }
    }
  } catch {}
  // fallback: prompt user
  console.log(`  Open: ${EXPLORER}/deploy/${deployHash}`);
  console.log("  Find 'WriteContract' in the execution effects → copy the hash");
  const readline = require("readline");
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  return new Promise(resolve => {
    rl.question("  Paste contract hash (without 'hash-' prefix): ", ans => {
      rl.close();
      resolve(ans.trim().replace(/^hash-/, ""));
    });
  });
}

// ── Full deployment flow ───────────────────────────────────────────────────────

async function deployAll() {
  const roles = ["oracle_tbill","oracle_gold","oracle_reindex","fund_agent","risk_agent"];

  // Step 1: ensure keys exist
  console.log("\n[1] Loading / generating keypairs…");
  const keys = {};
  for (const role of roles) {
    const dir = path.join(KEYS_DIR, role);
    const kp  = path.join(dir, "secret_key.pem");
    if (fs.existsSync(kp)) {
      keys[role] = loadKey(kp);
      console.log(`    ${role}: loaded`);
    } else {
      fs.mkdirSync(dir, { recursive: true });
      keys[role] = keygen(dir);
      console.log(`    ${role}: generated`);
    }
  }

  // Step 2: faucet prompt
  console.log("\n[2] Fund these accounts at https://testnet.cspr.live/tools/faucet");
  for (const role of roles) {
    const hexFile = path.join(KEYS_DIR, role, "public_key_hex");
    const hex = fs.existsSync(hexFile)
      ? fs.readFileSync(hexFile, "utf8").trim()
      : keys[role].publicKey.toHex();
    console.log(`    ${role.padEnd(20)}  ${hex}`);
  }
  const readline = require("readline");
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  await new Promise(r => rl.question("\n    Press ENTER when all accounts are funded…", () => { rl.close(); r(); }));

  const deployer = keys["fund_agent"];

  // Step 3: check wasm files
  const wasms = ["OracleRegistry","DataMarket","FundVault","Governance"];
  for (const w of wasms) {
    const p = path.join(WASM_DIR, `${w}.wasm`);
    if (!fs.existsSync(p)) {
      console.error(`\nMissing: ${p}\nRun: bash scripts/build_contracts.sh`);
      process.exit(1);
    }
  }
  console.log("\n[3] WASM files OK");

  // Step 4-7: deploy contracts
  console.log("\n[4] Deploying OracleRegistry…");
  let h = await installWasm(deployer, path.join(WASM_DIR, "OracleRegistry.wasm"), []);
  console.log(`    deploy: ${h}\n    ${EXPLORER}/deploy/${h}`);
  await waitForDeploy(h);
  const registryHash = await extractContractHash(h);
  console.log(`    hash: ${registryHash}`);

  console.log("\n[5] Deploying DataMarket…");
  h = await installWasm(deployer, path.join(WASM_DIR, "DataMarket.wasm"), [
    `registry_hash:string=${registryHash}`,
    "fee_bps:u32=250",
  ]);
  console.log(`    deploy: ${h}\n    ${EXPLORER}/deploy/${h}`);
  await waitForDeploy(h);
  const marketHash = await extractContractHash(h);
  console.log(`    hash: ${marketHash}`);

  console.log("\n[5b] Wiring OracleRegistry.set_market…");
  h = await callContract(deployer, registryHash, "set_market",
      [`market:string=${marketHash}`]);
  await waitForDeploy(h);
  console.log(`    wired ✓  ${EXPLORER}/deploy/${h}`);

  console.log("\n[6] Deploying FundVault…");
  // Use SDK's accountHashStr which correctly applies Blake2b (not sha256).
  const fundAcct = keys["fund_agent"].publicKey.toAccountHashStr();   // "account-hash-<hex>"
  const riskAcctHash = keys["risk_agent"].publicKey.toAccountHashStr();
  h = await installWasm(deployer, path.join(WASM_DIR, "FundVault.wasm"), [
    `operator:string=${fundAcct}`,
    "governance_hash:string=pending",
  ]);
  console.log(`    deploy: ${h}\n    ${EXPLORER}/deploy/${h}`);
  await waitForDeploy(h);
  const vaultHash = await extractContractHash(h);
  console.log(`    hash: ${vaultHash}`);

  console.log("\n[7] Deploying Governance…");
  const riskAcct  = riskAcctHash;
  h = await installWasm(deployer, path.join(WASM_DIR, "Governance.wasm"), [
    `proposer:string=${fundAcct}`,
    `risk_agent:string=${riskAcct}`,
    "veto_window_ms:u64=90000",
  ]);
  console.log(`    deploy: ${h}\n    ${EXPLORER}/deploy/${h}`);
  await waitForDeploy(h);
  const govHash = await extractContractHash(h);
  console.log(`    hash: ${govHash}`);

  // Step 8: write testnet.env
  const envContent = [
    `ORACLE_TBILL_KEY=${path.join(KEYS_DIR,"oracle_tbill","secret_key.pem")}`,
    `ORACLE_GOLD_KEY=${path.join(KEYS_DIR,"oracle_gold","secret_key.pem")}`,
    `ORACLE_REINDEX_KEY=${path.join(KEYS_DIR,"oracle_reindex","secret_key.pem")}`,
    `FUND_AGENT_KEY=${path.join(KEYS_DIR,"fund_agent","secret_key.pem")}`,
    `RISK_AGENT_KEY=${path.join(KEYS_DIR,"risk_agent","secret_key.pem")}`,
    `REGISTRY_HASH=${registryHash}`,
    `MARKET_HASH=${marketHash}`,
    `VAULT_HASH=${vaultHash}`,
    `GOV_HASH=${govHash}`,
  ].join("\n") + "\n";
  fs.writeFileSync(path.join(AGENTS, "testnet.env"), envContent);
  console.log("\n[8] agents/testnet.env written ✓");

  console.log("\n" + "═".repeat(50));
  console.log("  DEPLOYMENT COMPLETE");
  console.log("═".repeat(50));
  console.log(`  OracleRegistry: ${EXPLORER}/contract/${registryHash}`);
  console.log(`  DataMarket    : ${EXPLORER}/contract/${marketHash}`);
  console.log(`  FundVault     : ${EXPLORER}/contract/${vaultHash}`);
  console.log(`  Governance    : ${EXPLORER}/contract/${govHash}`);
  console.log("\n  Next: export HELIOS_MODE=testnet && python3 scripts/testnet_round.py --rounds 3\n");
}

// ── CLI ────────────────────────────────────────────────────────────────────────

const [,, cmd, ...rest] = process.argv;

const argMap = {};
let   lastFlag = null;
for (const a of rest) {
  if (a.startsWith("--")) { lastFlag = a.slice(2); argMap[lastFlag] = []; }
  else if (lastFlag)       { argMap[lastFlag].push(a); }
}
const flag  = k => argMap[k]?.[0];
const flags = k => argMap[k] || [];

(async () => {
  switch (cmd) {

    case "keygen": {
      const roles = ["oracle_tbill","oracle_gold","oracle_reindex","fund_agent","risk_agent"];
      for (const role of roles) {
        const dir = path.join(KEYS_DIR, role);
        if (fs.existsSync(path.join(dir, "secret_key.pem"))) {
          console.log(`${role}: already exists`); continue;
        }
        keygen(dir);
        console.log(`${role}: generated → ${dir}/`);
      }
      console.log(`\nFund at: https://testnet.cspr.live/tools/faucet`);
      break;
    }

    case "status": {
      const { CasperServiceByJsonRPC } = sdk();
      const client = new CasperServiceByJsonRPC(NODE_URL);
      try {
        const info = await client.getStatus();
        console.log(`✓ ${NODE_URL}`);
        console.log(`  chain: ${info.chainspec_name}`);
        console.log(`  peers: ${info.peers?.length ?? "?"}`);
      } catch (e) {
        console.error(`✗ ${NODE_URL}: ${e.message}`); process.exit(1);
      }
      break;
    }

    case "install": {
      const kp = loadKey(flag("key"));
      console.log(`Deployer: ${kp.publicKey.toHex()}`);
      const wasmPath = flag("wasm");
      console.log(`WASM: ${wasmPath} (${fs.statSync(wasmPath).size.toLocaleString()} bytes)`);
      const dh = await installWasm(kp, wasmPath, flags("args"),
                                   flag("payment") || GAS_INSTALL);
      console.log(`Deploy   : ${dh}`);
      console.log(`Explorer : ${EXPLORER}/deploy/${dh}`);
      if ("wait" in argMap) await waitForDeploy(dh);
      break;
    }

    case "call": {
      const kp = loadKey(flag("key"));
      const dh = await callContract(kp, flag("contract"), flag("entry-point"),
                                    flags("args"), flag("payment") || GAS_CALL);
      console.log(`Deploy   : ${dh}`);
      console.log(`Explorer : ${EXPLORER}/deploy/${dh}`);
      if ("wait" in argMap) await waitForDeploy(dh);
      break;
    }

    case "deploy-all":
      await deployAll();
      break;

    case "wait":
      await waitForDeploy(rest[0]);
      break;

    default:
      console.log(`Commands: keygen | status | install | call | wait | deploy-all`);
      console.log(`Run with --help on any command for details.`);
  }
})().catch(e => { console.error("Error:", e.message); process.exit(1); });
