# Helios 部署指南

> 项目：Helios — RWA Data Exchange on Casper  
> 版本：v3.0（secp256k1 + Casper 2.x 完整修复版）  
> 更新：2026-06-19  
> 链：`casper-test`

---

## 目录

1. [环境准备](#1-环境准备)
2. [密钥说明](#2-密钥说明)
3. [构建合约 WASM](#3-构建合约-wasm)
4. [测试网账户充值](#4-测试网账户充值)
5. [部署合约](#5-部署合约)
6. [合约连线验证](#6-合约连线验证)
7. [链上业务逻辑测试](#7-链上业务逻辑测试)
8. [错误代码速查](#8-错误代码速查)
9. [截图清单](#9-截图清单)
10. [快速命令参考](#10-快速命令参考)

---

## 1. 环境准备

### 1.1 系统依赖

| 工具 | 版本 | 安装 |
|------|------|------|
| Python | 3.9+ | 系统自带 |
| `cryptography` | 40+ | `pip install cryptography` |
| Rust | 1.75+ | `curl https://sh.rustup.rs \| sh` |
| Node.js | 18+（备用） | https://nodejs.org |

### 1.2 安装 Python 签名库

```bash
pip install cryptography
# 或
pip3 install cryptography --break-system-packages
```

### 1.3 添加 Rust WASM 编译目标

```bash
rustup target add wasm32-unknown-unknown
rustup update stable
```

### 1.4 清理旧编译缓存（必做）

升级 `casper-contract` 4→5 后，**必须**删除旧缓存，否则 Cargo 版本解析冲突：

```bash
rm -rf helios/contracts/target/
cd helios/contracts && cargo fetch
```

> ⚠️ **重要**：如果终端环境变量 `RUSTFLAGS` 含有 `--import-memory`，Casper VM 会报 `Memory section should exist`。构建前执行：
> ```bash
> unset RUSTFLAGS
> ```

### 1.5 替换修复后的文件

将以下文件替换到对应路径：

```
casper_deploy.py          → helios/scripts/casper_deploy.py
helios_fixed.zip 解压内容 → helios/contracts/
```

---

## 2. 密钥说明

### 2.1 两种密钥来源

Helios 支持两种密钥来源：

**方式 A：Casper Wallet 导出的密钥**（如果你有的话）

你的 5 个密钥文件（`Account 1~5_secret_key.pem`）全部是 **secp256k1** 格式：

```
-----BEGIN EC PRIVATE KEY-----
MHQCAQEEICrBYtViY894hv2u...oAcGBSuBBAAK
-----END EC PRIVATE KEY-----
```

`casper_deploy.py` 已完整支持 secp256k1，**直接使用这些文件即可**，无需转换。

**方式 B：使用 keygen 生成的密钥**（推荐用于 agent 系统）

```bash
python3 scripts/casper_deploy.py keygen --out keys/
```

这会生成 5 个 agent 密钥：
- `keys/oracle_tbill/secret_key.pem`
- `keys/oracle_gold/secret_key.pem`
- `keys/oracle_reindex/secret_key.pem`
- `keys/fund_agent/secret_key.pem`
- `keys/risk_agent/secret_key.pem`

### 2.2 查看密钥信息

**Casper Wallet 密钥：**
```bash
python3 scripts/casper_deploy.py pubkey --key "Account 1_secret_key.pem"
```

**生成的密钥：**
```bash
python3 scripts/casper_deploy.py pubkey --key keys/fund_agent/secret_key.pem
```

输出示例：

```
key_type:     secp256k1
pubkey:       0203ee00a572cce248dacead89ffb12552ebef7c7bae9df716c0eb2fbf2c698b5497
account_hash: account-hash-d0fb6aff3393675ce38e704fdff260c5b800af7ff44529b055ed47015f0c13f0
```

### 2.3 5 个账户信息速查

| 账户 | Account Hash（用于充值和链上配置） |
|------|----------------------------------|
| Account 1 | `account-hash-d0fb6aff3393675ce38e704fdff260c5b800af7ff44529b055ed47015f0c13f0` |
| Account 2 | `account-hash-133b4fd8051e6acbdef6c55206be976ac5c3736fa632972cb9e50b6d1c5bd822` |
| Account 3 | `account-hash-2031494034ac845d9d920c2532bb1edab7c9f659c2b480b718dce6db98f87e1f` |
| Account 4 | `account-hash-6d7757bdf999aec7ac71da99e5ed1af32fa84b776632d07c95f76f6603444e64` |
| Account 5 | `account-hash-62eb31bd386bd93ac8e7151b3d6921dec20bafa95f2e427d97d120bb72e21d0c` |

---

## 3. 构建合约 WASM

### 3.1 运行构建脚本

```bash
cd helios/
bash scripts/build_contracts.sh
```

脚本依次构建 4 个合约，输出：

```
== Helios contract builder (casper-contract v5 / casper-types v6) ==
-- building OracleRegistry.wasm (feature: oracle-registry)
   written: contracts/wasm/OracleRegistry.wasm (45321 bytes)
-- building DataMarket.wasm (feature: data-market)
   written: contracts/wasm/DataMarket.wasm (48102 bytes)
-- building FundVault.wasm (feature: fund-vault)
   written: contracts/wasm/FundVault.wasm (41876 bytes)
-- building Governance.wasm (feature: governance)
   written: contracts/wasm/Governance.wasm (39540 bytes)
== Build complete!
```

### 3.2 手动构建单个合约

```bash
cd contracts/

# OracleRegistry
RUSTFLAGS='' cargo build --release \
  --target wasm32-unknown-unknown \
  --features oracle-registry \
  --no-default-features

# DataMarket
RUSTFLAGS='' cargo build --release \
  --target wasm32-unknown-unknown \
  --features data-market \
  --no-default-features

# FundVault
RUSTFLAGS='' cargo build --release \
  --target wasm32-unknown-unknown \
  --features fund-vault \
  --no-default-features

# Governance
RUSTFLAGS='' cargo build --release \
  --target wasm32-unknown-unknown \
  --features governance \
  --no-default-features
```

### 3.3 构建错误排查

| 错误信息 | 原因 | 解决方法 |
|----------|------|----------|
| `can't find crate for 'std'` | `#![no_std]` 缺失 | 已在修复版中添加 |
| `requires 'panic_handler'` | no_std 需要 panic 处理器 | 已在修复版 `lib.rs` 中添加 |
| `Memory section should exist` | `RUSTFLAGS` 含 `--import-memory` | 执行 `unset RUSTFLAGS` |
| `version conflict casper-contract` | 旧缓存干扰 | `rm -rf contracts/target/` 后重建 |
| `couldn't find crate wee_alloc` | 依赖未下载 | `cd contracts && cargo fetch` |
| `call export not found` | feature flag 错误 | 确认使用了 `--no-default-features` |

---

## 4. 测试网账户充值

### 4.1 打开 Faucet

```
https://testnet.cspr.live/tools/faucet
```

### 4.2 获取每个账户的公钥

```bash
for i in 1 2 3 4 5; do
  echo "=== Account $i ==="
  python3 scripts/casper_deploy.py pubkey --key "Account ${i}_secret_key.pem"
done
```

将每个账户的 `pubkey` 粘贴到 Faucet 申请测试 CSPR。

### 4.3 Gas 预算

| 操作 | 单次 Gas | 次数 | 合计 |
|------|----------|------|------|
| 部署 4 个合约 | 400 CSPR | 4 | 1600 CSPR |
| 合约连线（set_market / set_governance） | 5 CSPR | 2 | 10 CSPR |
| 业务逻辑测试（register / purchase / propose…） | 2~5 CSPR | ~10 | ~30 CSPR |
| **总计（建议余额）** | | | **≥ 2000 CSPR** |

Account 1 充值 2000 CSPR 即可完成所有操作（deploy-all 使用同一账户）。

### 4.4 确认余额

```
https://testnet.cspr.live/account/<account-hash>
```

---

## 5. 部署合约

### 5.1 方式 A：一键自动部署（推荐）

**使用 Casper Wallet 密钥：**
```bash
python3 scripts/casper_deploy.py deploy-all \
    --key "Account 1_secret_key.pem"
```

**使用生成的密钥：**
```bash
python3 scripts/casper_deploy.py deploy-all \
    --key keys/fund_agent/secret_key.pem
```

脚本自动完成全部步骤，按回车确认充值后全程无需干预：

```
步骤 1: 部署 OracleRegistry → 等待确认 → 提取合约 hash
步骤 2: 部署 DataMarket（传入 registry_hash）→ 等待确认
步骤 2b: 调用 OracleRegistry.set_market 完成连线
步骤 3: 部署 FundVault（operator = 你的 account hash）
步骤 4: 部署 Governance（proposer = risk_agent = 你的 account hash）
步骤 4b: 调用 FundVault.set_governance 完成连线
步骤 5: 写入 agents/testnet.env
```

成功输出：

```
════════════════════════════════════════════════════
  DEPLOYMENT COMPLETE
════════════════════════════════════════════════════
  OracleRegistry : https://testnet.cspr.live/contract/<hash>
  DataMarket     : https://testnet.cspr.live/contract/<hash>
  FundVault      : https://testnet.cspr.live/contract/<hash>
  Governance     : https://testnet.cspr.live/contract/<hash>
```

合约 hash 自动保存到 `agents/testnet.env`：

```bash
# agents/testnet.env
REGISTRY_HASH=<hex>
MARKET_HASH=<hex>
VAULT_HASH=<hex>
GOV_HASH=<hex>
DEPLOYER_ACCOUNT=account-hash-<hex>
```

---

### 5.2 方式 B：手动分步部署

如需单独控制每个步骤：

**步骤 1：部署 OracleRegistry**

```bash
# 使用 Casper Wallet 密钥
python3 scripts/casper_deploy.py install \
    --key "Account 1_secret_key.pem" \
    --wasm contracts/wasm/OracleRegistry.wasm \
    --wait

# 或使用生成的密钥
python3 scripts/casper_deploy.py install \
    --key keys/fund_agent/secret_key.pem \
    --wasm contracts/wasm/OracleRegistry.wasm \
    --wait

# 记录输出的合约 hash
REGISTRY=<从 testnet.cspr.live/deploy/<hash> 的 WriteContract 中复制>
```

**步骤 2：部署 DataMarket**

```bash
python3 scripts/casper_deploy.py install \
    --key "Account 1_secret_key.pem" \
    --wasm contracts/wasm/DataMarket.wasm \
    --args "registry_hash:string=$REGISTRY" "fee_bps:u32=250" \
    --wait

MARKET=<合约 hash>
```

**步骤 2b：连线 OracleRegistry → DataMarket**

```bash
python3 scripts/casper_deploy.py call \
    --key "Account 1_secret_key.pem" \
    --contract $REGISTRY \
    --entry-point set_market \
    --args "market:string=$MARKET" \
    --wait
```

**步骤 3：部署 FundVault**

```bash
# 先获取 account hash
ACCT=$(python3 scripts/casper_deploy.py pubkey \
    --key "Account 1_secret_key.pem" | grep account_hash | cut -d' ' -f2)

python3 scripts/casper_deploy.py install \
    --key "Account 1_secret_key.pem" \
    --wasm contracts/wasm/FundVault.wasm \
    --args "operator:string=$ACCT" "governance_hash:string=pending" \
    --wait

VAULT=<合约 hash>
```

**步骤 4：部署 Governance**

```bash
python3 scripts/casper_deploy.py install \
    --key "Account 1_secret_key.pem" \
    --wasm contracts/wasm/Governance.wasm \
    --args "proposer:string=$ACCT" \
           "risk_agent:string=$ACCT" \
           "veto_window_ms:u64=90000" \
    --wait

GOV=<合约 hash>
```

**步骤 4b：连线 FundVault → Governance**

```bash
python3 scripts/casper_deploy.py call \
    --key "Account 1_secret_key.pem" \
    --contract $VAULT \
    --entry-point set_governance \
    --args "governance_hash:string=$GOV" \
    --wait
```

---

### 5.3 方式 C：Casper Wallet 网页端手动部署

如果 Python 脚本因网络问题无法连接节点，使用浏览器手动部署。

**前提**：

- 安装 Casper Wallet 浏览器扩展：https://www.casperwallet.io
- 导入 `Account 1_secret_key.pem` 到 Casper Wallet

**部署界面**：

```
https://testnet.cspr.live/deploy-contract
```

选择 **Deploy WASM** 模式，依次部署：

| 步骤 | WASM 文件 | 构造参数 | Gas (motes) |
|------|-----------|----------|-------------|
| 1 | `OracleRegistry.wasm` | 无 | `400000000000` |
| 2 | `DataMarket.wasm` | `registry_hash:String=<hash>` `fee_bps:U32=250` | `400000000000` |
| 3 | `FundVault.wasm` | `operator:String=account-hash-<hex>` `governance_hash:String=pending` | `400000000000` |
| 4 | `Governance.wasm` | `proposer:String=account-hash-<hex>` `risk_agent:String=account-hash-<hex>` `veto_window_ms:U64=90000` | `400000000000` |

连线步骤选择 **Call Entry Point** 模式：

| 步骤 | 合约 | Entry Point | 参数 |
|------|------|-------------|------|
| 2b | OracleRegistry | `set_market` | `market:String=<DataMarket hash>` |
| 4b | FundVault | `set_governance` | `governance_hash:String=<Governance hash>` |

---

## 6. 合约连线验证

部署完成后，验证各合约 named key 已正确写入：

```
https://testnet.cspr.live/account/<DEPLOYER_ACCOUNT>
```

在 **Named Keys** 标签页应能看到：

```
oracle_registry_contract_hash  →  hash-<hex>
data_market_contract_hash      →  hash-<hex>
fund_vault_contract_hash       →  hash-<hex>
governance_contract_hash       →  hash-<hex>
```

---

## 7. 链上业务逻辑测试

以下命令产生真实链上交易，用于比赛截图和演示。

**加载环境变量**：

```bash
source agents/testnet.env
```

### 7.1 注册预言机

```bash
python3 scripts/casper_deploy.py call \
    --key "Account 1_secret_key.pem" \
    --contract $REGISTRY_HASH \
    --entry-point register \
    --args "name:string=TBill Oracle" \
           "category:string=rwa" \
           "endpoint:string=https://helios.example/quote" \
           "price_motes:u64=2000000000" \
    --wait
```

### 7.2 创建数据列表

```bash
python3 scripts/casper_deploy.py call \
    --key "Account 1_secret_key.pem" \
    --contract $MARKET_HASH \
    --entry-point list_feed \
    --args "feed_key:string=tbill-3m" \
           "title:string=US T-Bill 3M" \
           "price_motes:u64=2000000000" \
           "endpoint:string=https://helios.example/quote" \
    --wait
```

### 7.3 购买数据

```bash
python3 scripts/casper_deploy.py call \
    --key "Account 2_secret_key.pem" \
    --contract $MARKET_HASH \
    --entry-point purchase \
    --args "listing_id:u64=0" \
    --wait
```

### 7.4 提交治理提案

```bash
python3 scripts/casper_deploy.py call \
    --key "Account 1_secret_key.pem" \
    --contract $GOV_HASH \
    --entry-point propose \
    --args "description:string=Increase T-Bill weight to 60%" \
    --wait
```

### 7.5 否决提案（90 秒内执行）

```bash
python3 scripts/casper_deploy.py call \
    --key "Account 1_secret_key.pem" \
    --contract $GOV_HASH \
    --entry-point veto \
    --args "proposal_id:u64=0" \
    --wait
```

### 7.6 执行再平衡

```bash
python3 scripts/casper_deploy.py call \
    --key "Account 1_secret_key.pem" \
    --contract $VAULT_HASH \
    --entry-point execute_rebalance \
    --args "proposal_id:u64=0" \
           "targets:string=tbill,gold,reindex" \
           "weights_bps:string=6000,3000,1000" \
    --wait
```

> ⚠️ `weights_bps` 各值之和**必须等于 10000**（bps）。`6000 + 3000 + 1000 = 10000` ✓

### 7.7 提交 x402 收款凭证

```bash
python3 scripts/casper_deploy.py call \
    --key "Account 3_secret_key.pem" \
    --contract $MARKET_HASH \
    --entry-point anchor_x402_receipt \
    --args "listing_id:u64=0" \
           "oracle:string=$DEPLOYER_ACCOUNT" \
           "amount_motes:u64=2000000000" \
           "receipt_hash:string=0xabc123" \
    --wait
```

---

## 8. 错误代码速查

| 错误码 | 合约 | 含义 | 解决方法 |
|--------|------|------|----------|
| `User(1)` | OracleRegistry | 预言机未注册 | 先调用 `register` |
| `User(2)` | 所有 | 调用者无权限 | 确认使用正确账户（admin / operator / proposer） |
| `User(3)` | OracleRegistry | 指定预言机不存在 | 检查传入的 `oracle` 参数 |
| `User(10)` | DataMarket | `listing_id` 不存在 | 先调用 `list_feed` 创建列表 |
| `User(11)` | DataMarket | listing 数据格式损坏 | 检查合约状态 |
| `User(20)` | FundVault | 权重总和 ≠ 10000 | `weights_bps` 各值之和须等于 10000 |
| `User(30)` | Governance | `proposal_id` 不存在 | 检查 id（从 0 开始） |
| `User(32)` | Governance | 提案已最终确定 | 不能对已 veto/approve 的提案再操作 |
| `User(33)` | Governance | 否决窗口已过（>90s） | 需在 `veto_window_ms` 内调用 `veto` |
| `User(34)` | Governance | 窗口未关闭（finalize 太早） | 等待 90 秒后调用 `finalize` |
| `MissingKey` | 所有 | 合约内部 named key 丢失 | 通常是连线步骤（5.2b/4b）未完成 |
| `Invalid Deploy` | 节点 | 序列化错误 | 已在 `casper_deploy.py` v3 中修复（CLType tag 错误） |
| `invalid body hash` | 节点 | header_hash 计算错误 | 已修复（body_hash 无前缀，PublicKey 正确序列化） |

---

## 9. 截图清单

部署完成后，截取以下链接的截图用于比赛提交：

| # | 截图内容 | URL 格式 | 关键信息 |
|---|----------|----------|----------|
| 1 | OracleRegistry 部署交易 | `testnet.cspr.live/deploy/<hash>` | 显示 Executed + WriteContract |
| 2 | DataMarket 部署交易 | `testnet.cspr.live/deploy/<hash>` | 显示 Executed + WriteContract |
| 3 | FundVault 部署交易 | `testnet.cspr.live/deploy/<hash>` | 显示 Executed + WriteContract |
| 4 | Governance 部署交易 | `testnet.cspr.live/deploy/<hash>` | 显示 Executed + WriteContract |
| 5 | set_market 连线交易 | `testnet.cspr.live/deploy/<hash>` | 显示 Executed |
| 6 | register 预言机 | `testnet.cspr.live/deploy/<hash>` | 显示 Executed |
| 7 | list_feed 创建列表 | `testnet.cspr.live/deploy/<hash>` | 显示 Executed |
| 8 | purchase 购买数据 | `testnet.cspr.live/deploy/<hash>` | 显示 Executed |
| 9 | propose 提交提案 | `testnet.cspr.live/deploy/<hash>` | 显示 Executed |
| 10 | veto / execute_rebalance | `testnet.cspr.live/deploy/<hash>` | 显示 Executed |

每张截图应包含：
- 顶部 deploy hash
- **Execution Status: Executed** 标识
- Transforms 区域显示 WriteContract 或存储写入

---

## 10. 快速命令参考

```bash
# 检查节点连通性
python3 scripts/casper_deploy.py status

# 查看密钥信息（支持 secp256k1）
python3 scripts/casper_deploy.py pubkey --key "Account 1_secret_key.pem"

# 生成新 ed25519 密钥
python3 scripts/casper_deploy.py keygen --out keys/new_key.pem

# 构建所有合约
bash scripts/build_contracts.sh

# 一键部署（最常用）
python3 scripts/casper_deploy.py deploy-all --key "Account 1_secret_key.pem"

# 部署单个 WASM
python3 scripts/casper_deploy.py install \
    --key "Account 1_secret_key.pem" \
    --wasm contracts/wasm/OracleRegistry.wasm --wait

# 调用合约 entry-point
python3 scripts/casper_deploy.py call \
    --key "Account 1_secret_key.pem" \
    --contract <HASH> \
    --entry-point <ENTRY_POINT> \
    --args "name:type=value" \
    --wait

# 等待特定 deploy 确认
python3 scripts/casper_deploy.py wait <deploy_hash>

# 清除旧编译缓存
rm -rf contracts/target/

# 预下载 v5/v6 依赖
cd contracts && cargo fetch
```

### 支持的参数类型

| 格式 | CLType | 示例 |
|------|--------|------|
| `name:string=value` | String | `"name:string=TBill Oracle"` |
| `name:u64=value` | U64 | `"price_motes:u64=2000000000"` |
| `name:u32=value` | U32 | `"fee_bps:u32=250"` |
| `name:u512=value` | U512 | `"amount:u512=400000000000"` |
| `name:bool=value` | Bool | `"active:bool=true"` |

---

## 附录：合约 Entry-point 一览

### OracleRegistry

| Entry Point | 参数 | 调用者 |
|-------------|------|--------|
| `register` | `name:string` `category:string` `endpoint:string` `price_motes:u64` | 任意（成为预言机） |
| `post_attestation` | `feed_key:string` `value:string` | 已注册预言机 |
| `credit_settlement` | `oracle:string` | DataMarket 或 admin |
| `score_attestation` | `oracle:string` `accurate:bool` | admin |
| `set_market` | `market:string` | admin |
| `get_oracle` | `oracle:string` | 任意（只读） |
| `get_reputation` | `oracle:string` | 任意（只读） |

### DataMarket

| Entry Point | 参数 | 调用者 |
|-------------|------|--------|
| `list_feed` | `feed_key:string` `title:string` `price_motes:u64` `endpoint:string` | 已注册预言机 |
| `purchase` | `listing_id:u64` | 任意 |
| `anchor_x402_receipt` | `listing_id:u64` `oracle:string` `amount_motes:u64` `receipt_hash:string` | 任意 |
| `set_fee_bps` | `fee_bps:u32` | admin |
| `get_listing` | `listing_id:u64` | 任意（只读） |
| `listing_count` | 无 | 任意（只读） |

### FundVault

| Entry Point | 参数 | 调用者 |
|-------------|------|--------|
| `deposit` | `amount:u64` | 任意 |
| `execute_rebalance` | `proposal_id:u64` `targets:string` `weights_bps:string` | operator |
| `record_nav` | `nav_motes:u64` `yield_bps:u32` | operator |
| `get_nav` | 无 | 任意（只读） |
| `set_governance` | `governance_hash:string` | operator |

### Governance

| Entry Point | 参数 | 调用者 |
|-------------|------|--------|
| `propose` | `description:string` | proposer |
| `veto` | `proposal_id:u64` | risk_agent（90s 内） |
| `finalize` | `proposal_id:u64` | 任意（90s 后） |
| `get_proposal` | `proposal_id:u64` | 任意（只读） |
| `proposal_count` | 无 | 任意（只读） |

---

*Helios Team · 2026-06-19*
