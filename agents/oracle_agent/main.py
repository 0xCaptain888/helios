"""Oracle agent — a data merchant in the machine economy.

Each oracle:
  1. registers an on-chain identity in OracleRegistry and lists its feed on
     DataMarket
  2. serves its feed over an x402-gated HTTP endpoint (402 until paid)
  3. verifies+settles payments through the Facilitator
  4. anchors every settlement on-chain (anchor_x402_receipt) — this is what
     grows its reputation score
  5. periodically posts signed attestations of its data on-chain

Data sources: a deterministic simulated RWA series by default (so the demo is
reproducible offline); plug a real HTTP source by overriding `sample()`.
"""
from __future__ import annotations

import json
import math
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..common import config, x402


class RwaSeries:
    """Deterministic pseudo-market series: trend + cycle + hash-noise."""

    def __init__(self, base: float, drift: float, amplitude: float, seed: int):
        self.base, self.drift, self.amplitude, self.seed = base, drift, amplitude, seed
        self.t = 0

    def next(self) -> float:
        self.t += 1
        noise = (hash((self.seed, self.t)) % 1000 - 500) / 5000.0
        value = (self.base
                 + self.drift * self.t
                 + self.amplitude * math.sin(self.t / 3.0)
                 + self.amplitude * noise)
        return round(value, 4)


class OracleAgent:
    def __init__(self, *, key: str, name: str, category: str, feed_key: str,
                 title: str, port: int, chain, feed_bus, facilitator_url: str,
                 series: RwaSeries):
        self.key = key
        self.name = name
        self.category = category
        self.feed_key = feed_key
        self.title = title
        self.port = port
        self.chain = chain
        self.bus = feed_bus
        self.facilitator_url = facilitator_url
        self.series = series
        self.wallet = x402.Wallet.create(key)
        self.price_motes = config.PRICES_MOTES[feed_key]
        self.listing_id: int | None = None
        self.last_value: float | None = None
        self.last_sampled: float = 0.0
        self._server: ThreadingHTTPServer | None = None

    # ---------- lifecycle ----------
    def onboard(self) -> None:
        """Register identity + list feed: two on-chain transactions."""
        endpoint = f"http://127.0.0.1:{self.port}/quote"
        self.chain.create_account(self.key, 50 * config.CSPR, self.wallet.address)
        deploy1 = self.chain.register_oracle(
            self.wallet.address, self.name, self.category, endpoint, self.price_motes)
        self.bus.deploy(hash_=deploy1, kind="registry.register",
                        explorer=self.chain.explorer_link(deploy1))
        self.listing_id = self.chain.list_feed(
            self.wallet.address, self.feed_key, self.title, self.price_motes, endpoint)
        self.publish_state()

    def sample(self) -> float:
        self.last_value = self.series.next()
        self.last_sampled = time.time()
        return self.last_value

    def attest(self) -> None:
        """Post a signed attestation of the latest value on-chain."""
        if self.last_value is None:
            self.sample()
        deploy = self.chain.post_attestation(
            self.wallet.address, self.feed_key, str(self.last_value))
        self.bus.attestation(oracle=self.name, feed_key=self.feed_key,
                             value=str(self.last_value), deploy=deploy)
        self.bus.deploy(hash_=deploy, kind="registry.post_attestation",
                        explorer=self.chain.explorer_link(deploy))
        self.publish_state()

    def publish_state(self) -> None:
        info = self.chain.state["registry"]["oracles"].get(self.wallet.address, {})
        rep = info.get("reputation", {})
        listing = (self.chain.state["market"]["listings"][self.listing_id]
                   if self.listing_id is not None else {})
        self.bus.upsert_oracle({
            "address": self.wallet.address,
            "name": self.name,
            "category": self.category,
            "feed_key": self.feed_key,
            "endpoint": f"http://127.0.0.1:{self.port}/quote",
            "price_motes": self.price_motes,
            "score_bps": rep.get("score_bps", 5000),
            "settlements": rep.get("settlements", 0),
            "attestations": rep.get("attestations", 0),
            "revenue_motes": listing.get("revenue_motes", 0),
            "last_value": self.last_value,
        })
        self.bus.set_listings_count(len(self.chain.state["market"]["listings"]))

    # ---------- x402 endpoint ----------
    def _facilitator(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            self.facilitator_url + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    def serve(self) -> None:
        agent = self

        class QuoteHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, code: int, body: dict, extra_headers: dict | None = None):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for k, v in (extra_headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path != "/quote":
                    return self._json(404, {"error": "unknown resource"})
                header = self.headers.get("X-PAYMENT")
                requirements = x402.payment_requirements(
                    network=config.NETWORK,
                    amount_motes=agent.price_motes,
                    pay_to=agent.wallet.address,
                    resource=f"http://127.0.0.1:{agent.port}/quote",
                    description=agent.title)
                if not header:
                    return self._json(402, requirements)
                try:
                    payload = x402.decode_payment_header(header)
                except Exception:
                    return self._json(402, {**requirements, "error": "bad X-PAYMENT header"})

                verdict = agent._facilitator("/verify", payload)
                if not verdict.get("isValid"):
                    return self._json(402, {**requirements,
                                            "error": verdict.get("invalidReason", "invalid")})
                settlement = agent._facilitator("/settle", payload)
                if not settlement.get("success"):
                    return self._json(402, {**requirements,
                                            "error": settlement.get("error", "settle failed")})

                # Anchor the x402 receipt on-chain -> reputation grows
                anchor = agent.chain.anchor_x402_receipt(
                    payload["payload"]["authorization"]["from"],
                    agent.listing_id, agent.price_motes,
                    f"x402:{settlement['txHash']}")
                agent.bus.payment(
                    frm=payload["payload"]["authorization"]["from"],
                    to=agent.wallet.address,
                    feed_key=agent.feed_key,
                    amount_motes=agent.price_motes,
                    receipt=settlement["txHash"],
                    anchor_deploy=anchor,
                    explorer=agent.chain.explorer_link(anchor))
                agent.bus.deploy(hash_=anchor, kind="market.anchor_x402_receipt",
                                 explorer=agent.chain.explorer_link(anchor))
                agent.publish_state()

                value = agent.sample()
                body = {
                    "feed_key": agent.feed_key,
                    "value": value,
                    "unit": "level",
                    "sampled_at": agent.last_sampled,
                    "oracle": agent.wallet.address,
                    "signature": agent.wallet.sign(
                        {"feed_key": agent.feed_key, "value": value,
                         "sampled_at": agent.last_sampled}),
                }
                return self._json(200, body, {
                    "X-PAYMENT-RESPONSE": x402.encode_settlement_header(settlement)})

        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), QuoteHandler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def shutdown(self) -> None:
        if self._server:
            self._server.shutdown()
