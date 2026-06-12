"""Minimal-but-faithful x402 implementation for the Helios demo.

Flow (mirrors the x402 spec used by the Casper Facilitator):

  1. Client GETs a paid resource with no payment
       -> server replies HTTP 402 + JSON `payment requirements`
  2. Client constructs a signed `payment payload`, base64-encodes it into the
     `X-PAYMENT` request header, retries the request
  3. Server forwards the payload to the Facilitator:
       POST /verify  -> signature + funds check
       POST /settle  -> moves value on-chain, returns settlement tx hash
  4. Server replies 200 + resource body + `X-PAYMENT-RESPONSE` header
     containing the settlement receipt

In mock mode signatures are HMAC-SHA256 over the canonical payload using each
agent's demo secret (stand-in for an ed25519 Casper key signature; the
interface is identical, swap `sign`/`verify` for casper key ops in testnet
mode — see agents/common/x402.py docstrings and docs/TESTNET.md).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass

X402_VERSION = 1
SCHEME = "exact"


def _canon(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


@dataclass
class Wallet:
    """Demo wallet: address + HMAC secret (ed25519 stand-in)."""
    name: str
    address: str
    secret: str

    @classmethod
    def create(cls, name: str) -> "Wallet":
        seed = hashlib.sha256(f"helios::{name}".encode()).hexdigest()
        return cls(name=name,
                   address="account-hash-" + seed[:56],
                   secret=hashlib.sha256(f"secret::{name}".encode()).hexdigest())

    def sign(self, message: dict) -> str:
        return hmac.new(self.secret.encode(), _canon(message), hashlib.sha256).hexdigest()

    @staticmethod
    def verify(secret: str, message: dict, signature: str) -> bool:
        expected = hmac.new(secret.encode(), _canon(message), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)


def payment_requirements(*, network: str, amount_motes: int, pay_to: str,
                         resource: str, description: str) -> dict:
    """The JSON body a paid endpoint returns with HTTP 402."""
    return {
        "x402Version": X402_VERSION,
        "error": "Payment required",
        "accepts": [{
            "scheme": SCHEME,
            "network": network,
            "maxAmountRequired": str(amount_motes),
            "asset": "CSPR",
            "payTo": pay_to,
            "resource": resource,
            "description": description,
            "maxTimeoutSeconds": 60,
        }],
    }


def build_payment_header(wallet: Wallet, requirements: dict) -> str:
    """Client side: pick the first accepted option, sign an authorization,
    return the base64 X-PAYMENT header value."""
    accept = requirements["accepts"][0]
    authorization = {
        "from": wallet.address,
        "to": accept["payTo"],
        "value": accept["maxAmountRequired"],
        "asset": accept["asset"],
        "network": accept["network"],
        "resource": accept["resource"],
        "nonce": uuid.uuid4().hex,
        "validUntil": int(time.time()) + accept.get("maxTimeoutSeconds", 60),
    }
    payload = {
        "x402Version": X402_VERSION,
        "scheme": SCHEME,
        "network": accept["network"],
        "payload": {
            "authorization": authorization,
            "signature": wallet.sign(authorization),
        },
    }
    return base64.b64encode(_canon(payload)).decode()


def decode_payment_header(header_value: str) -> dict:
    return json.loads(base64.b64decode(header_value.encode()))


def encode_settlement_header(receipt: dict) -> str:
    return base64.b64encode(_canon(receipt)).decode()


def decode_settlement_header(header_value: str) -> dict:
    return json.loads(base64.b64decode(header_value.encode()))
