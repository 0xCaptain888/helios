# Helios Architecture

## Design thesis

> A data marketplace only works if trust is cheap to verify. Helios makes
> reputation a *byproduct of settlement*: you cannot buy a score, you can only
> earn it by selling — on-chain, one receipt at a time.

Three layers, loosely coupled:

```
Layer 3  Consumers        fund agent (built-in first customer), any external
                          agent / protocol / developer
Layer 2  Settlement       x402 micropayments (HTTP) + on-chain anchoring,
                          or fully on-chain payable purchase
Layer 1  Trust & state    OracleRegistry · DataMarket · FundVault · Governance
                          (native Casper contracts: casper-contract v5 / casper-types v6, #![no_std])
```

## Contracts

### OracleRegistry
| Entry point | Caller | Effect |
|---|---|---|
| `register(name, category, endpoint, price_motes)` | any account | creates oracle identity, reputation starts neutral (5000 bps) |
| `post_attestation(feed_key, value)` | registered oracle | emits `AttestationPosted`, increments attestation count |
| `credit_settlement(oracle)` | **DataMarket only** | settlements += 1, recomputes `score_bps` |
| `score_attestation(oracle, accurate)` | admin / dispute module | accuracy bookkeeping, recomputes score |

Reputation formula (`compute_score`):

```
accuracy_bps = accurate / (accurate + disputed)        (neutral 5000 if unscored)
activity     = min(settlements, 100) / 100             (saturating)
score_bps    = accuracy_bps × (0.2 + 0.8 × activity)
```

A brand-new oracle keeps 20% of its accuracy score; full weight requires 100
settled sales. Buying volume is the only way to look established.

### DataMarket
| Entry point | Notes |
|---|---|
| `list_feed(feed_key, title, price_motes, endpoint)` | one listing per feed key |
| `purchase(listing_id)` **payable** | exact price required; 2.5% protocol fee to treasury, remainder paid out to the oracle instantly; credits settlement |
| `anchor_x402_receipt(listing_id, oracle, amount_motes, receipt_hash)` | records an off-band x402 settlement (facilitator tx hash) on-chain; updates listing sales/revenue |
| `set_fee_bps(fee_bps)` | admin |

> **Note:** `purchase()` and `anchor_x402_receipt()` update local listing state
> (sales count, revenue, treasury). The cross-contract call to
> `registry.credit_settlement(oracle)` that grows oracle reputation on-chain
> is planned for a future upgrade — currently reputation is tracked in the
> local state mirror maintained by the agents.

### FundVault
Operator-gated (`execute_rebalance` callable only by the fund agent account).
Stores positions (weights in bps, must sum to 10 000), a NAV mark per
rebalance, and the **x402 receipts that justified the decision** — auditable
provenance from data purchase to portfolio change.

### Governance
Single-purpose: a proposal queue with a **risk-agent veto window**.
`submit` (fund agent) → `veto(reason)` (risk agent, inside window) or
`finalize` (anyone, after window). Statuses: pending / vetoed / approved.

## Agents

| Agent | Role | Key files |
|---|---|---|
| Oracle ×3 | data merchants: register, list, serve x402 endpoint, anchor receipts, attest | `agents/oracle_agent/main.py` |
| Fund | first customer: discover → pay via x402 → decide → propose → execute | `agents/fund_agent/main.py` |
| Risk | adversarial reviewer with on-chain veto power | `agents/risk_agent/main.py` |
| Facilitator | x402 `/verify` + `/settle` (offline twin of the Casper Facilitator) | `agents/facilitator/server.py` |

The fund agent's decision engine is transparent momentum/yield rules by
default (reproducible offline); set `HELIOS_USE_LLM=1` + `ANTHROPIC_API_KEY`
to have Claude write the rationale. Round 3 of the demo deliberately
over-concentrates to show the veto firing and the agent self-repairing.

Risk policy (mirrored in `agents/common/config.py`):
- no single RWA position > 45%
- CSPR liquidity floor ≥ 10%
- decision data must be < 120s old

## x402 flow

```
fund agent                oracle endpoint            facilitator           chain
    │  GET /quote               │                        │                  │
    │──────────────────────────▶│                        │                  │
    │  402 + payment reqs       │                        │                  │
    │◀──────────────────────────│                        │                  │
    │  sign authorization       │                        │                  │
    │  GET /quote  X-PAYMENT    │                        │                  │
    │──────────────────────────▶│  POST /verify          │                  │
    │                           │───────────────────────▶│                  │
    │                           │  POST /settle          │   transfer       │
    │                           │───────────────────────▶│─────────────────▶│
    │                           │  anchor_x402_receipt   │                  │
    │                           │───────────────────────────────────────────▶│
    │  200 + data               │      (credits oracle reputation)          │
    │  + X-PAYMENT-RESPONSE     │                        │                  │
    │◀──────────────────────────│                        │                  │
```

## Mode switching

`agents/common/chain.py` exposes one interface with two backends:

- **MockChain** — deterministic local ledger mirroring all four contracts'
  entry points; every mutation yields a sha256 pseudo deploy hash. This is
  what `demo.py` drives, so the demo is reproducible anywhere with zero deps.
- **TestnetChain** — same surface, but each call goes through
  `scripts/casper_deploy.py` (pure Python) against Casper Testnet using keys
  and contract hashes from `agents/testnet.env`. Maintains a local `state`
  mirror so agents can read registry/market/gov/vault data identically to
  mock mode. See `docs/DEPLOYMENT_GUIDE.md`.

## Frontend

Zero-build vanilla HTML/CSS/JS (`frontend/`). Polls
`frontend/data/feed.json` (written by the event bus) every 2 seconds.
Signature element: the **settlement tape** — a live ledger strip of x402
payments. Reputation meters, NAV sparkline, decision rationales with veto
reasons, governance statuses, and the raw transaction log with explorer links
(testnet mode links to `testnet.cspr.live`).

## Threat model notes (qualification scope)

- Demo signatures are HMAC stand-ins for ed25519 Casper key signatures; the
  facilitator interface is identical, so testnet mode swaps verification
  without touching the protocol path.
- `anchor_x402_receipt` currently trusts the caller's receipt string; the
  finals roadmap adds facilitator-signed receipts verified on-chain.
- Reputation accrues per settlement regardless of data quality;
  `score_attestation` is the hook for the finals' dispute/challenge module.
