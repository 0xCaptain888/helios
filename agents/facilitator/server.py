"""Helios x402 Facilitator (demo twin of the Casper x402 Facilitator).

Endpoints:
  POST /verify  — checks the payment payload: schema, signature, expiry, funds
  POST /settle  — executes the value transfer on the chain adapter and returns
                  the settlement transaction hash

In testnet mode the agents point at the real Casper Facilitator instead of
this server (set HELIOS_FACILITATOR_URL); this module is the offline twin so
the protocol path is identical either way.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..common import config, x402


class Facilitator:
    def __init__(self, chain, directory: dict[str, x402.Wallet]):
        """directory: address -> Wallet, used to resolve demo signature secrets
        (testnet swaps this for ed25519 public-key verification)."""
        self.chain = chain
        self.directory = directory

    def verify(self, payload: dict) -> tuple[bool, str]:
        try:
            if payload.get("x402Version") != x402.X402_VERSION:
                return False, "unsupported x402 version"
            if payload.get("scheme") != x402.SCHEME:
                return False, "unsupported scheme"
            inner = payload["payload"]
            auth = inner["authorization"]
            wallet = self.directory.get(auth["from"])
            if wallet is None:
                return False, "unknown payer"
            if not x402.Wallet.verify(wallet.secret, auth, inner["signature"]):
                return False, "bad signature"
            amount = int(auth["value"])
            if self.chain.balance(auth["from"]) < amount:
                return False, "insufficient funds"
            return True, "ok"
        except (KeyError, ValueError, TypeError) as exc:
            return False, f"malformed payload: {exc}"

    def settle(self, payload: dict) -> dict:
        ok, reason = self.verify(payload)
        if not ok:
            return {"success": False, "error": reason}
        auth = payload["payload"]["authorization"]
        amount = int(auth["value"])
        tx_hash = self.chain.transfer(
            auth["from"], auth["to"], amount,
            memo=f"x402:{auth['resource']}")
        return {
            "success": True,
            "txHash": tx_hash,
            "networkId": auth["network"],
            "payer": auth["from"],
            "payee": auth["to"],
            "amount": str(amount),
        }


class _Handler(BaseHTTPRequestHandler):
    facilitator: Facilitator = None  # injected

    def log_message(self, *args):  # silence default logging
        pass

    def _json(self, code: int, body: dict):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "invalid json"})
        if self.path == "/verify":
            ok, reason = self.facilitator.verify(payload)
            return self._json(200, {"isValid": ok, "invalidReason": None if ok else reason})
        if self.path == "/settle":
            result = self.facilitator.settle(payload)
            return self._json(200 if result.get("success") else 402, result)
        return self._json(404, {"error": "unknown endpoint"})


def serve_in_thread(facilitator: Facilitator, port: int) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (_Handler,), {"facilitator": facilitator})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
