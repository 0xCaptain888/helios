"""Event bus: aggregates everything the agents do into frontend/data/feed.json
so the dashboard renders the live machine economy."""
from __future__ import annotations

import json
import threading
import time

from . import config

_LOCK = threading.RLock()


class Feed:
    def __init__(self):
        self.data = {
            "generated_at": time.time(),
            "mode": config.MODE,
            "network": config.NETWORK,
            "kpis": {
                "payments": 0,
                "volume_motes": 0,
                "oracles": 0,
                "listings": 0,
                "nav_micro": 1_000_000,
                "vetoes": 0,
                "attestations": 0,
            },
            "oracles": [],
            "payments": [],
            "attestations": [],
            "decisions": [],
            "nav_history": [{"round": 0, "nav_micro": 1_000_000, "ts": time.time()}],
            "governance": [],
            "deploys": [],
        }
        config.ensure_dirs()
        self.flush()

    # ---------- writers ----------
    def upsert_oracle(self, record: dict) -> None:
        with _LOCK:
            for i, o in enumerate(self.data["oracles"]):
                if o["address"] == record["address"]:
                    self.data["oracles"][i] = record
                    break
            else:
                self.data["oracles"].append(record)
            self.data["kpis"]["oracles"] = len(self.data["oracles"])
            self.flush()

    def set_listings_count(self, n: int) -> None:
        with _LOCK:
            self.data["kpis"]["listings"] = n
            self.flush()

    def payment(self, *, frm: str, to: str, feed_key: str, amount_motes: int,
                receipt: str, anchor_deploy: str, explorer: str) -> None:
        with _LOCK:
            self.data["payments"].insert(0, {
                "ts": time.time(), "from": frm, "to": to, "feed_key": feed_key,
                "amount_motes": amount_motes, "receipt": receipt,
                "anchor_deploy": anchor_deploy, "explorer": explorer, "kind": "x402",
            })
            self.data["kpis"]["payments"] += 1
            self.data["kpis"]["volume_motes"] += amount_motes
            self.flush()

    def attestation(self, *, oracle: str, feed_key: str, value: str, deploy: str) -> None:
        with _LOCK:
            self.data["attestations"].insert(0, {
                "ts": time.time(), "oracle": oracle, "feed_key": feed_key,
                "value": value, "deploy": deploy,
            })
            self.data["kpis"]["attestations"] += 1
            self.flush()

    def decision(self, record: dict) -> None:
        with _LOCK:
            self.data["decisions"].insert(0, record)
            self.flush()

    def governance(self, record: dict) -> None:
        with _LOCK:
            self.data["governance"].insert(0, record)
            if record.get("status") == "vetoed":
                self.data["kpis"]["vetoes"] += 1
            self.flush()

    def nav(self, round_no: int, nav_micro: int) -> None:
        with _LOCK:
            self.data["nav_history"].append(
                {"round": round_no, "nav_micro": nav_micro, "ts": time.time()})
            self.data["kpis"]["nav_micro"] = nav_micro
            self.flush()

    def deploy(self, *, hash_: str, kind: str, explorer: str) -> None:
        with _LOCK:
            self.data["deploys"].insert(0, {
                "ts": time.time(), "hash": hash_, "kind": kind, "explorer": explorer,
            })
            self.data["deploys"] = self.data["deploys"][:200]
            self.flush()

    def flush(self) -> None:
        with _LOCK:
            self.data["generated_at"] = time.time()
            config.FEED_PATH.write_text(
                json.dumps(self.data, indent=1, default=str), encoding="utf-8")
